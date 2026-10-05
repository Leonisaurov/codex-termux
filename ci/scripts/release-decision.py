#!/usr/bin/env python3
"""Decide whether a built Codex tree must become a release, and under which tag.

Publishing is a function of the vendored source identity, not of how the run was
triggered. The identity is ``git ls-tree HEAD codex/src`` — the same expression the
product cache contract hashes (build-codex.yml) — so "did the source change?" and
"must we rebuild the binaries?" are one question.

Everything repository-side (tags, releases, the previous release's manifest) is read
by the workflow and handed to this script as JSON files, so every branch of the
decision is testable without a network or a checkout:

  --stage newest  -> newest tag of the base, and whether its manifest must be fetched
  --stage decide  -> tag, publish, republish, reason
"""
from __future__ import annotations

import argparse, json, os, pathlib, re, shlex, subprocess

TAG_PREFIX = "codex-v"
MANIFEST_ASSET = "manifest.json"
# Single source of truth for the identity expression; the workflow contract test
# asserts this exact string also appears in build-codex.yml.
IDENTITY = "git ls-tree HEAD codex/src"


def base_version(cargo: pathlib.Path) -> str:
    """Read ``[workspace.package] version``, the version the tag is built on."""
    header, found = None, None
    for line in cargo.read_text(encoding="utf-8").splitlines():
        bracket = re.match(r"^\[([^\]]+)\]", line)
        if bracket:
            header = bracket.group(1).strip()
            continue
        if header == "workspace.package":
            match = re.match(r'\s*version\s*=\s*"([^"]+)"', line)
            if match:
                found = match.group(1)
                break
    if not found:
        raise SystemExit(f"{cargo}: no hay version en [workspace.package]")
    return found


def source_tree(root: pathlib.Path) -> str:
    listing = subprocess.run(shlex.split(IDENTITY), cwd=root, text=True,
                             capture_output=True, check=True).stdout
    fields = listing.split()
    # `040000 tree <sha>\t<path>`: the digest is the third field, as in build-codex.yml.
    digest = fields[2] if len(fields) > 2 else ""
    if not re.fullmatch(r"[0-9a-f]{40}", digest):
        raise SystemExit(f"{IDENTITY}: no devolvió un tree: {listing.strip()!r}")
    return digest


def ladder(tags: list[str], base: str) -> dict[int, str]:
    """Map suffix -> tag for `codex-v<base>` (0) and `codex-v<base>-N` (N).

    The remainder must be empty or a plain `-{digits}`: `0.155.1` must not pick up a
    hypothetical `0.155.10` release.
    """
    prefix, result = TAG_PREFIX + base, {}
    for tag in tags:
        if not tag.startswith(prefix):
            continue
        rest = tag[len(prefix):]
        if rest == "":
            result[0] = tag
        elif re.fullmatch(r"-\d+", rest):
            result[int(rest[1:])] = tag
    return result


def recorded_tree(component: dict) -> str | None:
    source = component.get("source")
    if not isinstance(source, dict):
        return None
    tree = source.get("tree")
    return tree if isinstance(tree, str) and tree else None


def decide(tags: list[str], releases: dict, base: str, tree: str, commit: str,
           previous: dict | None, dry_run: bool = False) -> dict:
    rungs = ladder(tags, base)
    prefix = TAG_PREFIX + base
    newest = rungs[max(rungs)] if rungs else ""
    outcome = {"base": base, "source_tree": tree, "codex_commit": commit,
               "newest_tag": newest, "tag": prefix,
               "publish": "true", "republish": "false", "reason": "no-release-for-base"}
    if not rungs:
        return finalize(outcome, dry_run)

    assets = releases.get(newest)
    outcome["tag"] = newest
    if assets is None:
        return finalize(outcome | {"reason": "tag-without-release", "republish": "true"}, dry_run)
    if MANIFEST_ASSET not in assets:
        return finalize(outcome | {"reason": "release-without-manifest", "republish": "true"}, dry_run)
    if previous is None:
        raise SystemExit(f"{newest}: hace falta su manifest.json para decidir")

    component = previous.get("components", {}).get("codex")
    recorded = recorded_tree(component) if isinstance(component, dict) else None
    if recorded is None:
        # A release published before identities were recorded describes an unknown
        # tree; it stays untouched and the ladder moves on.
        outcome.update(tag=f"{prefix}-{max(rungs) + 1}", reason="previous-release-without-identity")
    elif recorded == tree:
        outcome.update(publish="false", reason="source-unchanged")
    else:
        outcome.update(tag=f"{prefix}-{max(rungs) + 1}", reason="source-changed",
                       previous_tree=recorded)
    return finalize(outcome, dry_run)


def finalize(outcome: dict, dry_run: bool) -> dict:
    # `release` es el tag sin su prefijo: manifest.release y tag_name no pueden
    # divergir nunca, porque se derivan del mismo valor.
    outcome = {**outcome, "release": outcome["tag"][len(TAG_PREFIX):]}
    if dry_run and outcome["publish"] == "true":
        return {**outcome, "publish": "false", "would_publish": outcome["tag"],
                "reason": "dry-run"}
    return outcome


def read_json(path: pathlib.Path | None, default=None):
    if not path:
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", choices=("newest", "decide"), required=True)
    parser.add_argument("--tags-file", type=pathlib.Path, required=True,
                        help="JSON array con los refs de tag (matching-refs/tags/codex-v)")
    parser.add_argument("--releases-file", type=pathlib.Path,
                        help="JSON array de {tag, assets} del repo")
    parser.add_argument("--previous-manifest", type=pathlib.Path,
                        help="manifest.json del release mas nuevo de la base")
    parser.add_argument("--base", help="Version base; default [workspace.package] version")
    parser.add_argument("--source-tree", help="Arbol de codex/src; default `git ls-tree`")
    parser.add_argument("--codex-commit", help="Commit vendeoreado; default ci/source-manifest.json")
    parser.add_argument("--root", type=pathlib.Path,
                        default=pathlib.Path(__file__).resolve().parents[2])
    parser.add_argument("--dry-run", action="store_true")
    default_out = os.environ.get("GITHUB_OUTPUT")
    parser.add_argument("--out-file", type=pathlib.Path,
                        default=pathlib.Path(default_out) if default_out else None)
    args = parser.parse_args()

    tags = read_json(args.tags_file, [])
    base = args.base or base_version(args.root / "codex/src/codex-rs/Cargo.toml")
    tree = args.source_tree or source_tree(args.root)
    commit = args.codex_commit or read_json(
        args.root / "ci/source-manifest.json")["sources"]["codex"]["commit"]
    releases = {r["tag"]: r.get("assets", []) for r in read_json(args.releases_file, [])}

    if args.stage == "newest":
        rungs = ladder(tags, base)
        newest = rungs[max(rungs)] if rungs else ""
        result = {"base": base, "source_tree": tree, "newest_tag": newest,
                  "needs_manifest": "true" if newest and MANIFEST_ASSET in releases.get(newest, []) else "false"}
    else:
        if not args.releases_file:
            raise SystemExit("decide: falta --releases-file")
        result = decide(tags, releases, base, tree, commit,
                        read_json(args.previous_manifest), args.dry_run)

    print(json.dumps(result, indent=2, sort_keys=True))
    if args.out_file:
        with args.out_file.open("a", encoding="utf-8") as handle:
            for key, value in result.items():
                handle.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
