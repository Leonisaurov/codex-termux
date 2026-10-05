#!/usr/bin/env python3
"""Regression tests for the publish decision: which source trees become a release.

The point of the ladder is that publishing depends on the vendored tree, not on how
the run was triggered, and that a published release is never rewritten. These tests
describe a repository as data (tags, releases, the previous manifest) and assert the
answer, without a network or a git checkout.
"""
from __future__ import annotations

import importlib.util, json, os, pathlib, subprocess, sys, tempfile, unittest

HERE = pathlib.Path(__file__).resolve().parent


def load(filename: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DECIDE = load("release-decision.py", "release_decision")

BASE = "0.155.1"
TREE_A = "a" * 40
TREE_B = "b" * 40
COMMIT = "be2951ea34f0d295ed0becf97079f92fa5f6950e"
ARCHIVE = "codex-%s-android-aarch64.tar.gz" % COMMIT
FULL_ASSETS = [ARCHIVE, "manifest.json"]


def manifest(tree: str | None = None) -> dict:
    """A codex-termux manifest; without `tree` it is a pre-identity release."""
    component = {"version": COMMIT, "tag": COMMIT, "asset": ARCHIVE}
    if tree is not None:
        component["source"] = {"commit": COMMIT, "tree": tree}
    return {"schema": "codex-termux/v1", "components": {"codex": component}}


def releases(*entries: dict) -> dict:
    return {entry["tag"]: entry["assets"] for entry in entries}


def release_entry(tag: str, assets: list[str] | None = None) -> dict:
    return {"tag": tag, "assets": list(FULL_ASSETS if assets is None else assets)}


class LadderTests(unittest.TestCase):
    def test_bare_and_suffixed_tags_share_one_ladder(self):
        tags = ["codex-v0.155.1", "codex-v0.155.1-1", "codex-v0.155.1-2", "codex-v0.155.2"]
        self.assertEqual(DECIDE.ladder(tags, BASE),
                         {0: "codex-v0.155.1", 1: "codex-v0.155.1-1", 2: "codex-v0.155.1-2"})

    def test_a_longer_version_is_not_mistaken_for_a_suffix(self):
        # codex-v0.155.10 belongs to base 0.155.10, not to a 0.155.1 ladder.
        self.assertEqual(DECIDE.ladder(["codex-v0.155.10"], BASE), {})

    def test_release_field_is_the_tag_without_its_prefix(self):
        outcome = DECIDE.decide(["codex-v0.155.1-7"], releases(release_entry("codex-v0.155.1-7")),
                                BASE, TREE_A, COMMIT, manifest(TREE_B))
        self.assertEqual((outcome["tag"], outcome["release"]), ("codex-v0.155.1-8", "0.155.1-8"))


class DecisionTests(unittest.TestCase):
    def test_first_release_of_a_base_uses_the_bare_tag(self):
        outcome = DECIDE.decide([], {}, BASE, TREE_A, COMMIT, None)
        self.assertEqual((outcome["tag"], outcome["publish"], outcome["reason"]),
                         ("codex-v0.155.1", "true", "no-release-for-base"))

    def test_identical_tree_is_not_published_again(self):
        tags = ["codex-v0.155.1-2"]
        outcome = DECIDE.decide(tags, releases(release_entry(tags[0])), BASE, TREE_A, COMMIT,
                                manifest(TREE_A))
        self.assertEqual((outcome["publish"], outcome["reason"], outcome["tag"]),
                         ("false", "source-unchanged", tags[0]))

    def test_a_changed_tree_moves_the_ladder(self):
        tags = ["codex-v0.155.1-2"]
        outcome = DECIDE.decide(tags, releases(release_entry(tags[0])), BASE, TREE_A, COMMIT,
                                manifest(TREE_B))
        self.assertEqual((outcome["publish"], outcome["reason"], outcome["tag"]),
                         ("true", "source-changed", "codex-v0.155.1-3"))
        self.assertEqual(outcome["previous_tree"], TREE_B)

    def test_a_release_without_identity_never_gets_rewritten(self):
        # El estado real del repo: bare, -1 y -2 publicados antes de registrar el arbol.
        tags = ["codex-v0.155.1", "codex-v0.155.1-1", "codex-v0.155.1-2"]
        outcome = DECIDE.decide(tags, releases(*[release_entry(t) for t in tags]),
                                BASE, TREE_A, COMMIT, manifest(None))
        self.assertEqual((outcome["publish"], outcome["reason"], outcome["tag"], outcome["republish"]),
                         ("true", "previous-release-without-identity", "codex-v0.155.1-3", "false"))

    def test_an_orphan_tag_is_completed_instead_of_bumped(self):
        tags = ["codex-v0.155.1", "codex-v0.155.1-1"]
        outcome = DECIDE.decide(tags, releases(release_entry("codex-v0.155.1")),
                                BASE, TREE_A, COMMIT, manifest(TREE_B))
        self.assertEqual((outcome["tag"], outcome["republish"], outcome["reason"]),
                         ("codex-v0.155.1-1", "true", "tag-without-release"))

    def test_a_release_without_a_manifest_is_republished_under_the_same_tag(self):
        tags = ["codex-v0.155.1-4"]
        outcome = DECIDE.decide(tags, releases(release_entry(tags[0], [ARCHIVE])),
                                BASE, TREE_A, COMMIT, None)
        self.assertEqual((outcome["tag"], outcome["republish"], outcome["reason"]),
                         ("codex-v0.155.1-4", "true", "release-without-manifest"))

    def test_a_missing_manifest_is_fatal_rather_than_a_silent_bump(self):
        tags, entry = ["codex-v0.155.1"], release_entry("codex-v0.155.1")
        with self.assertRaises(SystemExit):
            DECIDE.decide(tags, releases(entry), BASE, TREE_A, COMMIT, None)

    def test_dry_run_reports_the_tag_without_publishing(self):
        outcome = DECIDE.decide([], {}, BASE, TREE_A, COMMIT, None, dry_run=True)
        self.assertEqual((outcome["publish"], outcome["would_publish"], outcome["reason"]),
                         ("false", "codex-v0.155.1", "dry-run"))

    def test_dry_run_keeps_a_no_publish_decision(self):
        tags = ["codex-v0.155.1"]
        outcome = DECIDE.decide(tags, releases(release_entry(tags[0])), BASE, TREE_A, COMMIT,
                                manifest(TREE_A), dry_run=True)
        self.assertEqual((outcome["publish"], outcome["reason"]), ("false", "source-unchanged"))


class StageNewestTests(unittest.TestCase):
    """El workflow usa esta etapa para saber que asset debe bajar antes de decidir."""

    def test_needs_manifest_is_true_only_for_a_release_that_has_it(self):
        tags = ["codex-v0.155.1", "codex-v0.155.1-2"]
        full = DECIDE.decide(tags, releases(release_entry(tags[1])), BASE, TREE_A, COMMIT, manifest(TREE_B))
        self.assertEqual(full["newest_tag"], tags[1])
        partial = DECIDE.decide(tags, releases(release_entry(tags[1], [ARCHIVE])),
                                BASE, TREE_A, COMMIT, None)
        self.assertEqual(partial["reason"], "release-without-manifest")
        orphan = DECIDE.decide(tags, releases(release_entry(tags[0])), BASE, TREE_A, COMMIT, manifest(TREE_A))
        self.assertEqual(orphan["newest_tag"], tags[1])


class CommandLineTests(unittest.TestCase):
    """The workflow drives this script through its CLI and its --out-file."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="release-decision.",
                                                 dir=os.environ.get("TMPDIR", "/data/data/com.termux/files/usr/tmp")))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name: str, payload) -> pathlib.Path:
        path = self.tmp / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def run_stage(self, stage: str, tags, releases_payload, previous=None, extra=()):
        tags_file = self.write("tags.json", tags)
        releases_file = self.write("releases.json", releases_payload)
        out_file = self.tmp / "out.txt"
        command = [sys.executable, str(HERE / "release-decision.py"), "--stage", stage,
                   "--tags-file", str(tags_file), "--releases-file", str(releases_file),
                   "--base", BASE, "--source-tree", TREE_A, "--codex-commit", COMMIT,
                   "--out-file", str(out_file), *extra]
        if previous is not None:
            command += ["--previous-manifest", str(self.write("manifest.json", previous))]
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout), dict(line.split("=", 1) for line in
                                               out_file.read_text(encoding="utf-8").splitlines())

    def test_newest_stage_announces_the_tag_to_download(self):
        tags = ["codex-v0.155.1-2"]
        printed, written = self.run_stage("newest", tags, [release_entry(tags[0])])
        self.assertEqual((printed["newest_tag"], printed["needs_manifest"]),
                         ("codex-v0.155.1-2", "true"))
        self.assertEqual(written["newest_tag"], tags[0])

    def test_decide_stage_writes_the_outputs_the_workflow_reads(self):
        tags = ["codex-v0.155.1-2"]
        printed, written = self.run_stage("decide", tags, [release_entry(tags[0])],
                                          manifest(TREE_B))
        self.assertEqual(written["tag"], "codex-v0.155.1-3")
        self.assertEqual((written["publish"], written["republish"]), ("true", "false"))
        self.assertEqual(printed["source_tree"], TREE_A)

    def test_dry_run_flag_reaches_the_outputs(self):
        printed, written = self.run_stage("decide", [], [], extra=("--dry-run",))
        self.assertEqual((written["publish"], written["would_publish"]),
                         ("false", "codex-v0.155.1"))
        self.assertEqual(printed["reason"], "dry-run")

    def test_identity_expression_matches_the_cache_contract(self):
        workflow = (HERE.parents[1] / ".github/workflows/build-codex.yml").read_text(encoding="utf-8")
        self.assertIn(DECIDE.IDENTITY, workflow)


if __name__ == "__main__":
    unittest.main(verbosity=2)
