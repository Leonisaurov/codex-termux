#!/usr/bin/env python3
"""Content-addressed incremental build state for the Android port.

The command intentionally has no third-party dependencies so it works in
Termux, CI containers, and the host build images alike.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCHEMA = 1
CHUNK = 1024 * 1024


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK):
            hasher.update(chunk)
    return hasher.hexdigest()


def digest_path(path: Path) -> str:
    """Hash a file or directory without timestamps or absolute paths."""
    if path.is_symlink():
        return digest_bytes(f"symlink\0{path.readlink()}".encode())
    if path.is_file():
        return digest_bytes(f"file\0{path.name}\0{digest_file(path)}".encode())
    if not path.is_dir():
        return digest_bytes(f"missing\0{path.name}".encode())

    entries: list[bytes] = []
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        if child.name in {".git", ".zig-cache", "node_modules", "target"}:
            continue
        entries.append(f"{child.name}\0".encode() + digest_path(child).encode())
    return digest_bytes(b"dir\0" + b"\n".join(entries))


def resolve(path: str, root: Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else root / candidate


def record_name(path: Path, raw: str, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.name


def input_digest(args: argparse.Namespace, root: Path) -> tuple[str, list[dict[str, str]]]:
    records: list[dict[str, str]] = []
    for value in sorted(args.value):
        if "=" not in value:
            raise ValueError(f"--value requires NAME=VALUE: {value}")
        name, content = value.split("=", 1)
        records.append({"kind": "value", "name": name, "digest": digest_bytes(content.encode())})
    for raw in sorted(args.input):
        path = resolve(raw, root)
        records.append({"kind": "path", "name": record_name(path, raw, root), "digest": digest_path(path)})
    for raw in sorted(args.dep):
        path = resolve(raw, root)
        if not path.is_file():
            raise ValueError(f"dependency manifest does not exist: {raw}")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        records.append(
            {
                "kind": "dependency",
                "name": record_name(path, raw, root),
                "status": str(manifest.get("status", "missing")),
                "digest": manifest.get("digest", ""),
            }
        )
    payload = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    return digest_bytes(payload), records


def output_records(args: argparse.Namespace, root: Path) -> list[dict[str, str]]:
    records = []
    for raw in sorted(args.output):
        path = resolve(raw, root)
        if not path.exists():
            raise ValueError(f"required output does not exist: {raw}")
        records.append({"name": record_name(path, raw, root), "digest": digest_path(path)})
    return records


def partial_output_records(args: argparse.Namespace, root: Path) -> list[dict[str, str]]:
    """Record outputs that exist when a node stops before completion.

    These records are diagnostic and deliberately do not make a failed node
    reusable. The compiler/build-system directories remain the resumable
    source of truth; the manifest only explains what was left behind.
    """
    records = []
    for raw in sorted(args.output):
        path = resolve(raw, root)
        if path.exists():
            try:
                # A failed build may leave a multi-gigabyte output tree or
                # binary. This field is diagnostic only, so record presence
                # without walking or hashing it and delaying checkpoint persistence.
                digest = "present-directory" if path.is_dir() else "present-file"
                records.append({"name": record_name(path, raw, root), "digest": digest})
            except OSError:
                records.append({"name": record_name(path, raw, root), "digest": "unreadable"})
    return records


def manifest_path(state_dir: Path, node: str) -> Path:
    return state_dir / "nodes" / f"{node}.json"


def read_manifest(state_dir: Path, node: str) -> dict | None:
    path = manifest_path(state_dir, node)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def is_valid(manifest: dict | None, digest: str, outputs: list[dict[str, str]]) -> bool:
    return bool(
        manifest
        and manifest.get("schema") == SCHEMA
        and manifest.get("status") == "valid"
        and manifest.get("digest") == digest
        and manifest.get("outputs") == outputs
    )


def atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    # The rename is atomic, but syncing the directory also makes the new
    # manifest survive a runner crash or power loss instead of leaving the
    # checkpoint one filesystem transaction behind the build tree.
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def acquire_lock(state_dir: Path, node: str) -> Path:
    lock = state_dir / "locks" / f"{node}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
    except FileExistsError as error:
        # A hard runner kill cannot execute `finally`, so a checkpoint may
        # contain the old directory lock. Reclaim it only when its recorded
        # owner PID is definitely gone; malformed or live locks remain safe.
        owner = lock / "owner"
        stale = False
        try:
            fields = dict(
                line.split("=", 1)
                for line in owner.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            pid = int(fields["pid"])
            if pid > 0:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    stale = True
                except PermissionError:
                    pass
        except (OSError, KeyError, ValueError):
            pass
        if stale:
            shutil.rmtree(lock, ignore_errors=True)
            try:
                lock.mkdir()
            except FileExistsError as retry_error:
                raise RuntimeError(f"node is already being built: {node} ({lock})") from retry_error
        else:
            raise RuntimeError(f"node is already being built: {node} ({lock})") from error
    (lock / "owner").write_text(f"pid={os.getpid()}\ntime={time.time()}\n", encoding="utf-8")
    return lock


def run(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    state_dir = resolve(args.state_dir, root)
    digest, inputs = input_digest(args, root)
    existing = read_manifest(state_dir, args.node)
    try:
        outputs = output_records(args, root)
    except ValueError:
        outputs = []
    if not args.force and is_valid(existing, digest, outputs):
        print(f"BUILD_STATE=hit node={args.node} digest={digest}")
        return 0
    if args.dry_run:
        print(f"BUILD_STATE=miss node={args.node} digest={digest}")
        return 0

    lock = acquire_lock(state_dir, args.node)
    manifest = {
        "schema": SCHEMA,
        "node": args.node,
        "digest": digest,
        "status": "building",
        "inputs": inputs,
        "outputs": [],
        "started_at": int(time.time()),
    }
    atomic_write(manifest_path(state_dir, args.node), manifest)
    previous_handlers = {}
    process: subprocess.Popen | None = None

    def mark_interrupted(signum: int, _frame: object) -> None:
        """Persist a non-reusable marker before a runner terminates us."""
        try:
            signal_name = signal.Signals(signum).name
        except ValueError:
            signal_name = f"SIG{signum}"
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signum)
            except OSError:
                pass
        manifest["status"] = "interrupted"
        manifest["signal"] = signal_name
        try:
            manifest["partial_outputs"] = partial_output_records(args, root)
            atomic_write(manifest_path(state_dir, args.node), manifest)
        except OSError:
            pass
        raise SystemExit(128 + signum)

    # A normal child failure is handled below. SIGTERM/SIGINT need an explicit
    # marker because a runner cancellation can otherwise leave `building` as
    # the last state written, even though the compiler directories are useful.
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, mark_interrupted)
    command_env = os.environ.copy()
    command_env.update(
        {
            "BUILD_STATE_ACTIVE": "1",
            "BUILD_STATE_MISS": "1",
            "BUILD_STATE_NODE": args.node,
            "BUILD_STATE_DIGEST": digest,
        }
    )
    try:
        print(f"BUILD_STATE=miss node={args.node} digest={digest}")
        process = subprocess.Popen(
            args.command,
            cwd=root,
            env=command_env,
            start_new_session=True,
        )
        returncode = process.wait()
        if returncode:
            if returncode < 0:
                exit_code = 128 - returncode
                manifest["status"] = "interrupted"
                manifest["signal"] = signal.Signals(-returncode).name
            else:
                exit_code = returncode
                manifest["status"] = "failed"
            manifest["returncode"] = exit_code
            manifest["partial_outputs"] = partial_output_records(args, root)
            atomic_write(manifest_path(state_dir, args.node), manifest)
            return exit_code
        digest, inputs = input_digest(args, root)
        outputs = output_records(args, root)
        manifest.update({"status": "valid", "digest": digest, "inputs": inputs, "outputs": outputs, "finished_at": int(time.time())})
        atomic_write(manifest_path(state_dir, args.node), manifest)
        return 0
    except BaseException as error:
        # A runner cancellation, signal, or exec error can interrupt the child
        # before it returns a normal status. Keep an explicit checkpoint marker
        # so the next runner knows the files are resumable, never final.
        manifest["status"] = "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed"
        manifest["error"] = f"{type(error).__name__}: {error}"
        try:
            manifest["partial_outputs"] = partial_output_records(args, root)
            atomic_write(manifest_path(state_dir, args.node), manifest)
        except OSError:
            pass
        raise
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.wait()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        shutil.rmtree(lock, ignore_errors=True)


def check(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    state_dir = resolve(args.state_dir, root)
    digest, _ = input_digest(args, root)
    manifest = read_manifest(state_dir, args.node)
    try:
        outputs = output_records(args, root)
    except ValueError:
        outputs = []
    valid = is_valid(manifest, digest, outputs)
    print(f"BUILD_STATE={'valid' if valid else 'invalid'} node={args.node} digest={digest}")
    return 0 if valid else 1


def verify(args: argparse.Namespace) -> int:
    """Verify a valid manifest and every output digest it records.

    This is intentionally independent of the cache key. The key proves that
    the requested contract was selected; this command proves that the bytes
    restored under that key still match the state the compiler produced.
    """
    root = Path(args.root).resolve()
    state_dir = resolve(args.state_dir, root)
    manifest = read_manifest(state_dir, args.node)
    if not manifest or manifest.get("schema") != SCHEMA or manifest.get("status") != "valid":
        print(f"BUILD_STATE=invalid node={args.node} reason=manifest", file=sys.stderr)
        return 1
    outputs = manifest.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        print(f"BUILD_STATE=invalid node={args.node} reason=outputs", file=sys.stderr)
        return 1
    seen: set[str] = set()
    for record in outputs:
        if not isinstance(record, dict):
            print(f"BUILD_STATE=invalid node={args.node} reason=output-record", file=sys.stderr)
            return 1
        name = record.get("name")
        expected = record.get("digest")
        if not isinstance(name, str) or not isinstance(expected, str) or name in seen:
            print(f"BUILD_STATE=invalid node={args.node} reason=output-record", file=sys.stderr)
            return 1
        seen.add(name)
        path = resolve(name, root)
        try:
            path.resolve().relative_to(root)
        except ValueError:
            print(f"BUILD_STATE=invalid node={args.node} reason=output-outside-root path={name}", file=sys.stderr)
            return 1
        if not path.exists() or digest_path(path) != expected:
            print(f"BUILD_STATE=invalid node={args.node} reason=output-mismatch path={name}", file=sys.stderr)
            return 1
    print(f"BUILD_STATE=verified node={args.node}")
    return 0


def status(args: argparse.Namespace) -> int:
    state_dir = resolve(args.state_dir, Path(args.root).resolve())
    manifests = sorted((state_dir / "nodes").glob("*.json"))
    if args.node:
        manifests = [manifest_path(state_dir, args.node)]
    for path in manifests:
        manifest = read_manifest(state_dir, path.stem)
        if manifest is None:
            print(f"{path.stem}: missing-or-invalid-manifest")
            continue
        print(f"{path.stem}: {manifest.get('status', 'unknown')} digest={manifest.get('digest', '')}")
    return 0


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "check", "verify", "status"])
    parser.add_argument("--root", default=".")
    parser.add_argument("--state-dir", default="build/state")
    parser.add_argument("--node")
    parser.add_argument("--input", action="append", default=[])
    parser.add_argument("--value", action="append", default=[])
    parser.add_argument("--dep", action="append", default=[])
    parser.add_argument("--output", action="append", default=[])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    raw = sys.argv[1:]
    separator = raw.index("--") if "--" in raw else len(raw)
    args = parser().parse_args(raw[:separator])
    if args.command in {"run", "check"} and not args.node:
        print(f"{args.command} requires --node", file=sys.stderr)
        return 2
    if args.command == "run":
        command_args = raw[separator + 1 :]
        if not command_args:
            print("run requires a command after --", file=sys.stderr)
            return 2
        args.command = command_args
        return run(args)
    if args.command == "check":
        return check(args)
    if args.command == "verify":
        if not args.node:
            print("verify requires --node", file=sys.stderr)
            return 2
        return verify(args)
    return status(args)


if __name__ == "__main__":
    raise SystemExit(main())
