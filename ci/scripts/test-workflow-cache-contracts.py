#!/usr/bin/env python3
"""Verify that cache contracts are complete and shared by producers/consumers.

Codex's durable cache key is computed by ``ci/scripts/cache-contract.py`` from an
explicit list of paths and values. If a consumer recomputes the same product key
with a different list, its restore key never matches the producer and the cached
artifact silently stops being reused. If a build input is absent from every list,
editing it does not invalidate the cache and a stale artifact is restored.

This test parses the workflow argument arrays and enforces both invariants
without a runner.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"

ASSIGN = re.compile(r"(\w+)=\((.+?)\)\n", re.DOTALL)
PRODUCT = re.compile(r"--product\s+(\S+)")
PATH = re.compile(r"--path\s+(\S+)")
VALUE = re.compile(r"--value\s+\"([^\"=]+)=")

STACK_WORKFLOWS = (
    "build-android.yml",
    "build-core.yml",
    "build-bun.yml",
    "build-bun-target.yml",
    "build-opentui.yml",
    "build-opencode.yml",
    "build-kilo.yml",
)


def blocks(path: pathlib.Path) -> dict[str, tuple[set[str], set[str]]]:
    """Map each ``cache-contract.py`` product to its (paths, values)."""
    result: dict[str, tuple[set[str], set[str]]] = {}
    text = path.read_text(encoding="utf-8")
    for match in ASSIGN.finditer(text):
        body = match.group(2)
        product = PRODUCT.search(body)
        if not product:
            continue
        paths = set(PATH.findall(body))
        values = set(VALUE.findall(body))
        result.setdefault(product.group(1), (paths, values))
    return result


def main() -> None:
    codex = blocks(WORKFLOWS / "build-codex.yml")
    rusty = blocks(WORKFLOWS / "build-rusty-v8-android.yml")

    # The vendored source tree carries the Android port patches, so its git tree
    # hash and its pin must both be part of the Codex key.
    assert "codex" in codex, "codex cache contract missing"
    assert "CODEX_REF" in codex["codex"][1]
    assert "V8_VERSION" in codex["codex"][1]
    assert "CODEX_SOURCE_TREE" in codex["codex"][1]
    assert "codex/src/codex-rs/Cargo.lock" in codex["codex"][0]
    assert "rusty-v8" in rusty, "rusty-v8 cache contract missing"
    assert "V8_VERSION" in rusty["rusty-v8"][1]

    # ci/source-manifest.json aggregates every pinned commit. Including it in a
    # per-product key makes an unrelated pin change invalidate this cache, so keys
    # must rely on the product's own source tree/commit instead.
    for group in (codex, rusty):
        for product, (paths, _values) in group.items():
            assert "ci/source-manifest.json" not in paths, f"{product}: global manifest in cache key"

    # The build script and the shared environment are inputs of the Codex key; if
    # either stops being hashed, an edit to it silently restores a stale binary.
    assert "codex/scripts/build-codex-android.sh" in codex["codex"][0]
    assert "ci/scripts/env.sh" in codex["codex"][0]

    # The NDK must use one shared absolute path so actions/cache (which versions
    # by path string) stores a single entry instead of one per product workspace.
    # It lives under the gitignored `.ci/` so clean-tree checks stay intact.
    shared = "${{ github.workspace }}/.ci/android-ndk"
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        text = workflow.read_text(encoding="utf-8")
        for value in re.findall(r"ANDROID_NDK_HOME:\s*['\"]?(.+?)['\"]?\s*$", text, re.MULTILINE):
            assert value == shared, f"{workflow.name}: NDK path is not shared: {value}"

    # The Zig toolchain must also use one shared path; only the prefix-restored
    # intermediate Zig compiler caches stay per product.
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        text = workflow.read_text(encoding="utf-8")
        assert "${{ env.WORK_DIR }}/zig-${{ env.ZIG_VERSION }}" not in text, (
            f"{workflow.name}: per-product Zig toolchain path"
        )
    setup = (ROOT / "ci/scripts/setup-runner.sh").read_text(encoding="utf-8")
    assert "${GITHUB_WORKSPACE}/.ci/zig-${ZIG_VERSION}" in setup

    # The orchestrator must wire both producers, consume the producer's artifact
    # in the same run, and publish according to the source identity.
    android = (WORKFLOWS / "build.yml").read_text(encoding="utf-8")
    assert "uses: ./.github/workflows/build-rusty-v8-android.yml" in android
    assert "uses: ./.github/workflows/build-codex.yml" in android
    assert "use_existing_v8: true" in android
    assert "needs: [contracts, rusty-v8]" in android
    assert "needs: [contracts, codex]" in android
    assert "ci/scripts/package-release.py" in android
    assert "--source-tree" in android

    # Publishing is a property of the vendored tree, not of how the run started: a
    # manual-only gate would put every release back behind a human dispatch.
    assert "ci/scripts/release-decision.py" in android
    assert "github.event_name == 'workflow_dispatch'" not in android
    assert "steps.decision.outputs.publish == 'true'" in android
    # Only main owns the tag ladder; a branch run may decide but must not write.
    assert "github.ref == 'refs/heads/main'" in android
    assert "group: codex-release-${{ github.ref }}" in android

    # The tag ladder and the cache contract must key off the same expression, or a
    # release could be called "unchanged" for a tree the cache considers new.
    identity = "git ls-tree HEAD codex/src"
    assert identity in (WORKFLOWS / "build-codex.yml").read_text(encoding="utf-8")
    assert identity in (ROOT / "ci/scripts/release-decision.py").read_text(encoding="utf-8")

    # This repository owns Codex only. The stack products live in
    # Leonisaurov/opencode-termux; their workflows must not reappear here.
    for name in STACK_WORKFLOWS:
        assert not (WORKFLOWS / name).exists(), f"{name}: stack workflow in the codex repository"


if __name__ == "__main__":
    main()
