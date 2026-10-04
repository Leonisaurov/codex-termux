#!/usr/bin/env python3
"""Fast regression tests for the content-addressed build state helper."""

import json
import importlib.util
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "scripts" / "build-state.py"
CACHE_CONTRACT = ROOT / "scripts" / "cache-contract.py"
REPO_ROOT = ROOT.parent


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["python3", str(STATE), *args], cwd=ROOT, text=True, capture_output=True)


def main() -> None:
    os.environ.setdefault("TMPDIR", "/data/data/com.termux/files/usr/tmp")
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
        root = Path(directory)
        source = root / "source.txt"
        output = root / "output.txt"
        source.write_text("one\n", encoding="utf-8")
        command = ["python3", "-c", "from pathlib import Path; Path('output.txt').write_text(Path('source.txt').read_text())"]
        base = ["run", "--root", str(root), "--state-dir", "state", "--node", "demo", "--input", "source.txt", "--output", "output.txt", "--"] + command
        first = subprocess.run(["python3", str(STATE), *base], cwd=root, text=True, capture_output=True)
        assert "BUILD_STATE=miss" in first.stdout, first.stderr
        second = subprocess.run(["python3", str(STATE), *base], cwd=root, text=True, capture_output=True)
        assert "BUILD_STATE=hit" in second.stdout, second.stderr
        verified = subprocess.run(
            ["python3", str(STATE), "verify", "--root", str(root), "--state-dir", "state", "--node", "demo"],
            cwd=root, text=True, capture_output=True,
        )
        assert verified.returncode == 0 and "BUILD_STATE=verified" in verified.stdout, verified.stderr
        output.write_text("corrupt\n", encoding="utf-8")
        corrupt = subprocess.run(
            ["python3", str(STATE), "verify", "--root", str(root), "--state-dir", "state", "--node", "demo"],
            cwd=root, text=True, capture_output=True,
        )
        assert corrupt.returncode != 0 and "output-mismatch" in corrupt.stderr
        repaired = subprocess.run(["python3", str(STATE), *base], cwd=root, text=True, capture_output=True)
        assert "BUILD_STATE=miss" in repaired.stdout, repaired.stderr
        source.write_text("two\n", encoding="utf-8")
        third = subprocess.run(["python3", str(STATE), *base], cwd=root, text=True, capture_output=True)
        assert "BUILD_STATE=miss" in third.stdout, third.stderr
        manifest = json.loads((root / "state/nodes/demo.json").read_text(encoding="utf-8"))
        assert manifest["status"] == "valid"
        assert manifest["outputs"]

        stale_lock = root / "state/locks/stale.lock"
        stale_lock.mkdir(parents=True)
        (stale_lock / "owner").write_text("pid=999999999\ntime=0\n", encoding="utf-8")
        stale = subprocess.run(
            [
                "python3", str(STATE), "run", "--root", str(root), "--state-dir", "state",
                "--node", "stale", "--input", "source.txt", "--output", "stale.txt", "--",
                "python3", "-c", "from pathlib import Path; Path('stale.txt').write_text('stale')",
            ],
            cwd=root, text=True, capture_output=True,
        )
        assert stale.returncode == 0, stale.stderr

        failed_command = [
            "run", "--root", str(root), "--state-dir", "state", "--node", "checkpoint",
            "--input", "source.txt", "--output", "partial.txt", "--",
            "python3", "-c",
            "import os; from pathlib import Path; assert os.environ['BUILD_STATE_MISS'] == '1'; Path('partial.txt').write_text('partial'); raise SystemExit(7)",
        ]
        failed = subprocess.run(["python3", str(STATE), *failed_command], cwd=root, text=True, capture_output=True)
        assert failed.returncode == 7, failed.stderr
        checkpoint = json.loads((root / "state/nodes/checkpoint.json").read_text(encoding="utf-8"))
        assert checkpoint["status"] == "failed"
        assert checkpoint["partial_outputs"]

        interrupted_command = [
            "run", "--root", str(root), "--state-dir", "state", "--node", "interrupted",
            "--input", "source.txt", "--output", "interrupted.txt", "--",
            "python3", "-c",
            "import os,signal; os.kill(os.getpid(), signal.SIGTERM)",
        ]
        interrupted = subprocess.run(
            ["python3", str(STATE), *interrupted_command],
            cwd=root, text=True, capture_output=True,
        )
        assert interrupted.returncode == 143, interrupted.stderr
        interrupted_manifest = json.loads((root / "state/nodes/interrupted.json").read_text(encoding="utf-8"))
        assert interrupted_manifest["status"] == "interrupted"
        assert interrupted_manifest["signal"] == "SIGTERM"

        missing_output_command = [
            "run", "--root", str(root), "--state-dir", "state", "--node", "missing-output",
            "--input", "source.txt", "--output", "missing.txt", "--",
            "python3", "-c", """raise SystemExit(0)""",
        ]
        missing_output = subprocess.run(
            ["python3", str(STATE), *missing_output_command],
            cwd=root, text=True, capture_output=True,
        )
        assert missing_output.returncode != 0
        missing_manifest = json.loads((root / "state/nodes/missing-output.json").read_text(encoding="utf-8"))
        assert missing_manifest["status"] == "failed"

        parent_manifest_path = root / "state/nodes/parent-interrupted.json"
        parent = subprocess.Popen(
            [
                "python3", str(STATE), "run", "--root", str(root), "--state-dir", "state",
                "--node", "parent-interrupted", "--input", "source.txt", "--output", "parent.txt", "--",
                "python3", "-c", "import time; time.sleep(30)",
            ],
            cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 5
            while not parent_manifest_path.exists() and parent.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            assert parent_manifest_path.exists(), parent.stderr.read() if parent.stderr else ""
            parent.send_signal(signal.SIGTERM)
            assert parent.wait(timeout=5) == 143
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()
        parent_interrupted = json.loads(parent_manifest_path.read_text(encoding="utf-8"))
        assert parent_interrupted["status"] == "interrupted"
        assert parent_interrupted["signal"] == "SIGTERM"

        child = [
            "run", "--root", str(root), "--state-dir", "state", "--node", "child",
            "--dep", "state/nodes/demo.json", "--output", "child.txt", "--",
            "python3", "-c", "from pathlib import Path; Path('child.txt').write_text('child')",
        ]
        child_first = subprocess.run(["python3", str(STATE), *child], cwd=root, text=True, capture_output=True)
        assert "BUILD_STATE=miss" in child_first.stdout, child_first.stderr
        demo_manifest_path = root / "state/nodes/demo.json"
        demo_manifest = json.loads(demo_manifest_path.read_text(encoding="utf-8"))
        demo_manifest["status"] = "failed"
        demo_manifest_path.write_text(json.dumps(demo_manifest), encoding="utf-8")
        child_failed_dependency = subprocess.run(
            ["python3", str(STATE), *child], cwd=root, text=True, capture_output=True
        )
        assert "BUILD_STATE=miss" in child_failed_dependency.stdout, child_failed_dependency.stderr
        source.write_text("three\n", encoding="utf-8")
        subprocess.run(["python3", str(STATE), *base], cwd=root, check=True, capture_output=True, text=True)
        child_second = subprocess.run(["python3", str(STATE), *child], cwd=root, text=True, capture_output=True)
        assert "BUILD_STATE=miss" in child_second.stdout, child_second.stderr
        status = subprocess.run(["python3", str(STATE), "status", "--root", str(root), "--state-dir", "state"], cwd=root, text=True, capture_output=True)
        assert "demo: valid" in status.stdout and "child: valid" in status.stdout, status.stderr
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
        root = Path(directory)
        source = root / "input.txt"
        output = root / "output.bin"
        source.write_text("contract\n", encoding="utf-8")
        output.write_bytes(b"artifact\n")
        base = ["--root", str(root), "--product", "demo", "--path", "input.txt", "--value", "ANDROID_API=24"]
        key = subprocess.run([sys.executable, str(CACHE_CONTRACT), "key", *base], text=True, capture_output=True, check=True)
        assert key.stdout.startswith("ci-cache-v2-demo-")
        manifest = "state/cache.json"
        output_args = ["--output", "output.bin"]
        subprocess.run([sys.executable, str(CACHE_CONTRACT), "write", *base, *output_args, "--manifest", manifest], check=True)
        valid = subprocess.run([sys.executable, str(CACHE_CONTRACT), "validate", *base, *output_args, "--manifest", manifest], text=True, capture_output=True)
        assert valid.returncode == 0 and "CACHE_CONTRACT=valid" in valid.stdout
        output.write_bytes(b"corrupt\n")
        corrupt = subprocess.run([sys.executable, str(CACHE_CONTRACT), "validate", *base, *output_args, "--manifest", manifest], text=True, capture_output=True)
        assert corrupt.returncode != 0 and "output-mismatch" in corrupt.stderr
        source.write_text("changed\n", encoding="utf-8")
        invalid = subprocess.run([sys.executable, str(CACHE_CONTRACT), "validate", *base, *output_args, "--manifest", manifest], text=True, capture_output=True)
        assert invalid.returncode != 0 and "key-mismatch" in invalid.stderr

        spec = importlib.util.spec_from_file_location("cache_contract", CACHE_CONTRACT)
        assert spec and spec.loader
        cache_contract = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cache_contract)
        metadata = cache_contract.contract(REPO_ROOT, "demo", [], [])
        assert set(metadata["cache_engine"]) == set(cache_contract.ENGINE_PATHS)
        assert all(value != "missing" for value in metadata["cache_engine"].values())
    print("build-state tests: OK")


if __name__ == "__main__":
    main()
