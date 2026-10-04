#!/usr/bin/env python3
import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SUMMARY = ROOT / "ci-summary.sh"


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "summary.txt"
        env = os.environ.copy()
        env.update(
            GITHUB_STEP_SUMMARY=str(output),
            BUILD_SCENARIO="warm-identical",
            OUTPUT_VALIDATION="valid",
            RUNNER_OS="Linux",
            RUNNER_ARCH="X64",
            BUILD_START_SECONDS=str(int(__import__("time").time()) - 2),
        )
        result = subprocess.run(
            ["bash", "-c", f'source "{SUMMARY}"; ci_summary'],
            env=env,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        report = output.read_text(encoding="utf-8")
        assert "BUILD_SCENARIO=warm-identical" in report
        assert "OUTPUT_VALIDATION=valid" in report
        assert "RUNNER_OS=Linux" in report
        assert "BUILD_DURATION_SECONDS=" in report


if __name__ == "__main__":
    main()
