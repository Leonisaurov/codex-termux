#!/usr/bin/env python3
"""Package the built Codex CI artifacts and emit the installer's manifest."""
import argparse, hashlib, json, pathlib, re, subprocess, tarfile

FILES = ["codex-android", "codex-code-mode-host", "codex-linux-sandbox"]
PREFIX = "codex-android-aarch64-"

def verify(path: pathlib.Path, name: str) -> None:
    # codex-linux-sandbox dejó de ser un script: es el ELF del wrapper de proot.
    info = subprocess.check_output(["file", str(path)], text=True)
    if "ELF" not in info or not re.search(r"aarch64|ARM aarch64", info):
        raise SystemExit(f"{name}: no es un ELF aarch64 ({info.strip()})")

def main():
    p = argparse.ArgumentParser(); p.add_argument("--root", type=pathlib.Path, required=True); p.add_argument("--out", type=pathlib.Path, required=True); p.add_argument("--release", required=True); p.add_argument("--codex", required=True, help="Commit vendeoreado de openai/codex"); p.add_argument("--source-tree", required=True, help="git ls-tree HEAD codex/src: la identidad que decide si hay que publicar"); a = p.parse_args(); a.out.mkdir(parents=True, exist_ok=True)
    if not re.fullmatch(r"[0-9a-f]{40}", a.source_tree): raise SystemExit(f"--source-tree no es un digest git: {a.source_tree}")
    artifact_dirs = [d for d in a.root.iterdir() if d.is_dir() and d.name.startswith(PREFIX)]
    if len(artifact_dirs) != 1: raise SystemExit("codex: artifact ambiguo o ausente")
    source = artifact_dirs[0]
    if source.name != PREFIX + a.codex: raise SystemExit(f"codex: el artifact {source.name} no corresponde a --codex {a.codex}")
    archive = a.out / f"codex-{a.codex}-android-aarch64.tar.gz"
    with tarfile.open(archive, "w:gz") as t:
        for f in FILES:
            matches = list(source.rglob(f))
            if len(matches) != 1 or not matches[0].is_file(): raise SystemExit(f"codex: artifact ambiguo o ausente: {f}")
            verify(matches[0], f)
            t.add(matches[0], arcname=f)
    comps = {"codex": {"version": a.codex, "tag": a.codex, "asset": archive.name, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "size": archive.stat().st_size, "archive": "tar.gz", "depends": [], "files": FILES, "source": {"commit": a.codex, "tree": a.source_tree}}}
    (a.out / "manifest.json").write_text(json.dumps({"schema": "codex-termux/v1", "release": a.release, "stability": "stable", "android": {"arch": "aarch64", "abi": "arm64-v8a", "api": 24}, "components": comps}, indent=2) + "\n")

if __name__ == "__main__":
    main()
