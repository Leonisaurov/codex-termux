#!/usr/bin/env python3
"""Validate that build inputs are tracked, vendored source trees."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def run(*args: str, cwd: Path) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    root = args.root.resolve()
    manifest_path = root / "ci/source-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []

    for name, source in manifest["sources"].items():
        path = root / source["path"]
        if not path.is_dir():
            errors.append(f"{name}: vendored source missing at {path}")
            continue
        if (path / ".git").exists():
            errors.append(f"{name}: nested git metadata remains at {path}")
        tracked = run("git", "ls-files", "--", source["path"], cwd=root)
        if not tracked:
            errors.append(f"{name}: no vendored files are tracked for {path}")
        index_entries = run("git", "ls-files", "-s", "--", source["path"], cwd=root)
        if any(line.split()[0] == "160000" for line in index_entries.splitlines()):
            errors.append(f"{name}: source is still represented by a gitlink")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"validated {len(manifest['sources'])} repository-owned source trees")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
