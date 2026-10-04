#!/usr/bin/env python3
"""Produce and validate the repository's versioned CI cache contract.

The key deliberately describes the toolchain and inputs that can change ABI or
compiler output.  This helper is also usable outside GitHub Actions, which
makes cache-key changes testable without a runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
from pathlib import Path


SCHEMA = "ci-cache-v2"
CHUNK = 1024 * 1024

# Cache correctness depends on the code that computes and validates the
# contract, not just on the product sources. Keep these files in every exact
# contract so changing cache semantics cannot resurrect an artifact created by
# an older validator or state runner. Stable intermediate restore prefixes do
# not include the contract digest on purpose: native build systems can still
# reuse compatible object files after a cache-engine change.
ENGINE_PATHS = (
    "ci/scripts/cache-contract.py",
    "ci/scripts/build-state.py",
    "ci/scripts/validate-source-tree.py",
    "ci/scripts/validate-android-bundle.py",
    "ci/actions/incremental-cache/action.yml",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(CHUNK):
            digest.update(data)
    return digest.hexdigest()


def path_digest(path: Path) -> str:
    if path.is_symlink():
        return hashlib.sha256(f"symlink\0{path.readlink()}".encode()).hexdigest()
    if path.is_file():
        return f"file:{sha256(path)}"
    if not path.is_dir():
        return "missing"
    entries = []
    for child in sorted(path.rglob("*")):
        if child.is_dir() or ".git" in child.parts:
            continue
        entries.append(f"{child.relative_to(path)}={path_digest(child)}")
    return hashlib.sha256("\n".join(entries).encode()).hexdigest()


def value(name: str, fallback: str = "unknown") -> str:
    return os.environ.get(name, fallback)


def contract(root: Path, product: str, paths: list[str], values: list[str]) -> dict:
    inputs = {
        "schema": SCHEMA,
        "product": product,
        "runner_os": value("RUNNER_OS", platform.system().lower()),
        "host_arch": value("RUNNER_ARCH", platform.machine()),
        "target": value("ANDROID_TRIPLE_API", "aarch64-linux-android24"),
        "android_api": value("ANDROID_API", "24"),
        "ndk": value("ANDROID_NDK_VERSION"),
        "zig": value("ZIG_VERSION"),
        "rust": value("RUST_TOOLCHAIN", "unknown"),
        "upstream": value("UPSTREAM_COMMIT", "current"),
        "values": dict(item.split("=", 1) for item in values),
        "cache_engine": {
            path: path_digest((root / path).resolve())
            for path in ENGINE_PATHS
        },
        "paths": {
            path: path_digest((root / path).resolve())
            for path in sorted(set(paths))
        },
    }
    encoded = json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    inputs["digest"] = hashlib.sha256(encoded).hexdigest()
    return inputs


def output_records(root: Path, outputs: list[str]) -> list[dict[str, str]]:
    records = []
    for output in sorted(set(outputs)):
        path = (root / output).resolve()
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"missing output: {output}")
        records.append({"path": output, "sha256": sha256(path), "size": str(path.stat().st_size)})
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["key", "write", "validate"])
    parser.add_argument("--root", default=".")
    parser.add_argument("--product", required=True)
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--value", action="append", default=[])
    parser.add_argument("--manifest")
    parser.add_argument("--output", action="append", default=[])
    parser.add_argument("--machine")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    metadata = contract(root, args.product, args.path, args.value)
    key = f"{SCHEMA}-{args.product}-{metadata['digest']}"
    if args.command == "key":
        print(key)
        return 0
    if args.command == "write":
        if not args.manifest:
            parser.error("write requires --manifest")
        try:
            outputs = output_records(root, args.output)
        except ValueError as error:
            print(f"CACHE_CONTRACT=invalid reason={error}", file=sys.stderr)
            return 1
        manifest = {"key": key, "contract": metadata, "outputs": outputs}
        manifest_path = (root / args.manifest).resolve()
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    if args.manifest:
        manifest_path = (root / args.manifest).resolve()
        if not manifest_path.is_file():
            print("CACHE_CONTRACT=invalid reason=missing-manifest", file=sys.stderr)
            return 1
        saved = json.loads(manifest_path.read_text(encoding="utf-8"))
        if saved.get("key") != key:
            print("CACHE_CONTRACT=invalid reason=key-mismatch", file=sys.stderr)
            return 1
    try:
        current_outputs = output_records(root, args.output)
    except ValueError as error:
        print(f"CACHE_CONTRACT=invalid reason={error}", file=sys.stderr)
        return 1
    if args.manifest and saved.get("outputs") != current_outputs:
        print("CACHE_CONTRACT=invalid reason=output-mismatch", file=sys.stderr)
        return 1
    if args.machine:
        readelf = shutil.which("readelf")
        if readelf:
            import subprocess
            for output in args.output:
                path = (root / output).resolve()
                result = subprocess.run([readelf, "-h", str(path)], text=True, capture_output=True)
                if result.returncode or args.machine not in result.stdout:
                    print(f"CACHE_CONTRACT=invalid reason=machine-mismatch path={output}", file=sys.stderr)
                    return 1
    print(f"CACHE_CONTRACT=valid key={key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
