# Repository Guidelines

## Structure and routing

This repository is the Android/Termux port of the Codex CLI, split out of the
`opencode-termux` stack workspace. Everything Codex-related lives here:

- `codex/src/`: vendored `openai/codex` monorepo plus the Android port patches.
- `codex/scripts/`: the CI build entry point (`build-codex-android.sh`).
- `codex/src/codex-rs/android-sandbox/`: port-owned crate that ships as
  `codex-linux-sandbox`; it translates a Codex permission profile into a `proot`
  invocation and must fail closed rather than run a command unsandboxed.
- `codex/test/`, `codex/more/`: device-side tests and the ntfy approval relay.
- `codex/build/`, `codex/artifacts/`: CI-generated state (gitignored).
- `ci/scripts/`: shared environment, resumable build-state engine, cache
  contracts, packaging and the installer.
- `ci/actions/`: composite actions used by the workflows.
- `.github/workflows/`: `build.yml` orchestrates `rusty-v8` → `codex` → `publish`.

For work inside the vendored tree, read `codex/src/AGENTS.md` and
`codex/src/docs/android-termux.md` before changing `codex-rs`.

## Version pins

The upstream commit is declared once, in `ci/source-manifest.json`, and CI
refuses a checkout that does not match it (`build.yml` → `build-codex.yml`
validates the pin, the absence of nested `.git`, and a clean tree). Source
changes and their pin update land in the same commit. Do not raise the pin from
an inference or from another workflow's default.

Rusty V8 is pinned to `v8 150.4.0` with the `ptrcomp_sandbox` feature; the
archive it publishes is what `codex-code-mode-host` links against.

## Development and validation

This workspace is not a local build runner: no `cargo`, NDK or V8 compilation on
the device. Use GitHub Actions for builds and validate statically here:

```sh
for t in test-workflow-cache-contracts test-release-decision \
         test-vendored-android-patches test-build-state test-ci-summary test-installer; do
  python3 "ci/scripts/$t.py"
done
bash -n ci/scripts/*.sh codex/scripts/*.sh codex/test/lock-regression/run.sh
```

Never claim a build passed without reading its workflow log and artifact list.

## CI operations

Use `gh` to dispatch, inspect and monitor runs. Publishing is not a manual step:
the `publish` job of `build.yml` compares the vendored tree
(`git ls-tree HEAD codex/src`, the same expression the product cache contract
hashes) against the identity recorded in the newest `codex-v<base>` release's
manifest, and publishes the next ladder tag only when they differ. Runs from a
branch never write releases, and `dry_run=true` on `workflow_dispatch` reports
the decision without packaging or publishing. Cache keys are per repository, so
the first run after any change to an `ENGINE_PATHS` file legitimately misses.

## Conventions

Shell uses Bash, `set -euo pipefail`, quoted paths, and the canonical Termux
`$TMPDIR`; never `/tmp` or `/data/local/tmp`. Preserve the vendored checkout's
Rust conventions. Keep commits concise, for example
`fix(codex): shim Bionic API 24 for aligned_alloc` or
`vendor(codex): rebase to rust-v0.156.0`.
