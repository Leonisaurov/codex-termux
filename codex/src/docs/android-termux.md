# Android/Termux Port Notes

This checkout is the Codex source tree used by the Android/Termux port. Keep
source changes here, under `codex-rs/`; do not edit generated binaries or
cached copies as a substitute for source changes.

## Target and toolchain

The supported target is `aarch64-linux-android` with Android API 24. The Rust
target configuration is in `codex-rs/.cargo/config.toml` and expects the NDK
tools `aarch64-linux-android-clang` and `llvm-ar` on `PATH`. The code-mode host
also needs the Bionic libc and compiler-rt stubs that
`codex/scripts/build-codex-android.sh` compiles and appends through its linker
wrapper (`android-libc-shims.o` and `libclang_rt.builtins-aarch64-android.a`).

Build and test commands are run from `codex/` unless noted:

```sh
cd codex
just fmt
just test -p codex-cli
```

Use the crate-specific test after changing a crate. Do not run a full
workspace test casually on a device; cross-compilation is resource-intensive
and the Android target cannot execute on an x86_64 build host.

## Vendored revision

`codex-rs` is vendored from upstream `rust-v0.155.1`
(`be2951ea34f0d295ed0becf97079f92fa5f6950e`). The pin lives in
`ci/source-manifest.json` and is mirrored by `ci/scripts/env.sh` and the
`codex_ref` defaults of the build workflows; the workflow fails if they drift.
The Android patch set below is re-applied in-tree on every re-vendor. Files
where upstream already implements the Android/Termux behaviour (the `arboard`
gate in `tui/Cargo.toml`, the Termux CA bundle path in
`network-proxy/src/native_certs.rs`, the clipboard stubs in
`tui/src/clipboard_paste.rs`, `code-mode-protocol/build.rs` aside) are taken
as-is.

## Sandbox

Upstream builds its Linux sandbox from three primitives, and this device has none
of them: Landlock needs a >= 5.13 kernel (this is 5.10-android12 and the syscall
dies under seccomp), `unshare(CLONE_NEWUSER|CLONE_NEWNS)` returns `EINVAL` so
bubblewrap cannot mount any filesystem view, and `bwrap` is not installed. Termux
does ship `proot`, which resolves paths with `ptrace` inside the user's own
process, and that is the only mechanism available here. `codex-rs/android-sandbox`
is therefore a port-owned `codex-linux-sandbox`: it reads the
`--permission-profile` JSON the upstream manager produces and turns it into a
`proot` invocation.

Selection is patched too: `arg0` only injects the sandbox executable under
`cfg!(target_os = "linux")` (`arg0/src/lib.rs:261`), which is why
`sandbox_mode=read-only` and `workspace-write` used to fail every command with
`LandlockSandboxExecutableNotProvided`. An `android` branch now resolves the
wrapper next to the running executable (falling back to `PATH`), so a missing
wrapper still produces the upstream error instead of running unsandboxed.

What the wrapper emits, all measured on the device:

- `-b /:/:ro` makes the whole filesystem unwritable (`openat` with write intent
  fails `EROFS`); a later `-b <root>:<root>` reopens writes under one root, and
  `-b <sub>:<sub>:ro` closes them again inside it. Bindings are ordered by depth
  because the last match wins.
- `/dev` is bound read-only and the device nodes a shell needs (`/dev/null`,
  `/dev/zero`, `/dev/full`, `/dev/random`, `/dev/urandom`, `/dev/tty`) are reopened
  writable; `/dev` is not listable in the Termux runtime and the rest stays closed.
- A carveout whose reads the profile allows and whose path exists (`<ws>/.git`) is
  bound against itself with `:ro`. Measured: that blocks write, unlink and rename
  while the content stays readable, which is what git needs. Masking it with an
  empty node instead would be stricter than the profile asks for.
- A carveout whose reads are denied, and protected metadata that does not exist
  yet (`<ws>/.codex`), is bound to an empty node. Measured: the mask blocks reading
  the content, writing, creating the leaf, unlinking it and renaming it, and it
  works for paths that are not there yet, so the `.git`/`.agents`/`.codex`
  carveouts hold against first-time creation. A bind with a missing source aborts
  `proot`, which is why the empty node is used.
- `--net-policy deny` fails `connect`/`bind` with `EACCES` (loopback included).
- `proot` rejects `--` as a separator, so the wrapped command follows the options
  directly; exit codes and argv arrive unmodified.

The honest limits:

- proot is `ptrace` and the tracee shares the UID with the tracer, so this is not
  a boundary against deliberately malicious code. It is a real boundary against
  accidental writes and network use, which is what `sandbox_mode` can mean on an
  unrooted device.
- A mask hides content, not the directory entry: `ls` and `du` still print the
  masked name, they just cannot descend into it or read it.
- Profiles that restrict *reads* to an allowlist are refused (building that view
  needs `-r` plus a rebuilt `/proc`, `$PREFIX` and the Termux symlinks), as are
  `deny` rules expressed as glob patterns, a `deny` on the filesystem root itself
  (masking `/` would also remove the program being run) and managed network
  (`--allow-network-for-proxy`), which the wrapper cannot express without the proxy
  path. Every one of these fails closed with exit 78 and never runs the command.
- The native `codex-rs/linux-sandbox` (bubblewrap/Landlock/seccomp) is still not a
  working Android sandbox and must not be described as one; it is not built here.

Two harnesses cover this:

```sh
bash codex/test/sandbox-proot/run.sh /path/to/codex-linux-sandbox   # argv + enforcement
SANDBOX_MODE=workspace-write \
    bash codex/test/lock-regression/run.sh /path/to/codex-android   # turno real
```

The first asserts both the argv the wrapper would emit (`--dry-run`) and what the
sandbox really blocks on the device. The second runs a real app-server turn and,
in a restrictive mode, additionally asserts that the turn reached the wrapper
through `CODEX_ANDROID_SANDBOX_LOG` — the argv translation itself is covered by
`cargo test -p codex-android-sandbox`, which CI runs before the long build
("Test Android sandbox wrapper").

## File locks

Rust's `std::fs::File::lock`, `try_lock`, `lock_shared` and `try_lock_shared`
return `ErrorKind::Unsupported` ("lock() not supported") on
`aarch64-linux-android`: std only implements flock for a target list that does
not include Android (checked against 1.95 through 1.98). Any unpatched call site
fails at runtime, which previously broke startup (curated plugins sync,
app-server control socket), the network-proxy CA cache, history paging, and the
`rules/default.rules` write behind "don't ask again".

Every such site carries a `CODEX-TERMUX-ANDROID-PATCH` marker and skips the
advisory flock under `#[cfg(target_os = "android")]`, because the Termux runtime
is single-user. The sites are `execpolicy/src/amend.rs`,
`core/src/installation_id.rs`, `core-plugins/src/startup_sync.rs`,
`message-history/src/{lib,batch}.rs`, `network-proxy/src/certs.rs`,
`app-server-transport/src/transport/unix_socket.rs`,
`rollout/src/{maintenance,writer_lock}.rs` (the thread writer lock moved here
from `thread-store`), `arg0/src/lib.rs` — both the path-entry guard and the
`try_lock_dir` janitor site, which had been missed and printed
`failed to clean up stale arg0 temp dirs: try_lock() not supported` on every
start — `rmcp-client/src/oauth/{refresh_lock,store_lock}.rs` and
`user-verification/src/lifecycle_lock.rs` (new in 0.155.1). A new upstream lock
site will regress silently, so validate a built binary with:

```sh
bash codex/test/lock-regression/run.sh /path/to/codex-android
```

The harness boots the real app-server against a scripted Responses API stand-in
and fails if the binary reports `lock() not supported` or if the turn's command
does not run. The `rules/default.rules` write is asserted only when the
app-server actually asks for an approval; in the default `danger-full-access`
mode nothing denies a command, so that assertion reports `skip` instead of passing
silently. Under `SANDBOX_MODE=workspace-write` the proot wrapper does deny writes
outside the workspace, which is the path where an approval — and the rules write —
become reachable.

## TLS segment alignment

Bionic's arm64 loader refuses an executable whose `PT_TLS` is aligned below 64
bytes and aborts before `main()` with
`executable's TLS segment is underaligned: alignment is 8 (skew 0), needs to be
at least 64 for ARM64 Bionic`. V8 only contributes 8-byte `thread_local`s and
`thread_local!` lowers to emutls on Android, so nothing in the link raises the
segment: `codex-rs/code-mode-host/src/main.rs` emits a 64-byte-aligned `.tdata`
stub through `global_asm!`, and lld takes the segment alignment from the
best-aligned input section.

Only the code-mode host needs it; `codex-cli` loads fine without the stub, which
is why the defect stayed invisible until the host was executed. Re-vendoring
replaces `main.rs` and drops the stub, so the build script verifies `PT_TLS`
alignment on both ELF outputs and fails otherwise.

## Model availability

The backend gates models by their `minimal_client_version` (`gpt-5.6-sol`,
`gpt-5.6-terra` and `gpt-5.6-luna` need 0.144.0, `gpt-6-astra` needs 0.153.0), so
a build pinned to an older upstream release only receives the models its version
satisfies and every newer model fails with "requires a newer version of Codex".

Measured on 2026-09-21 with a 0.134.0-alpha.3 build: the models endpoint honours
a client-supplied version (with `0.155.1` the account catalog returns
`gpt-reserve`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`), but a turn for
`gpt-5.6-luna` still failed with `400 ... requires a newer version of Codex`,
also with `9.9.9`, with the first-party originators `codex_cli_rs` / `codex-tui`
and with a fresh `installation_id`. The inference gate is therefore evaluated
server-side against the authenticated account/session, so raising the reported
version cannot unlock newer models; re-vendoring upstream is the only supported
path. The `CODEX_REPORTED_CLIENT_VERSION` override this port carried for
catalog/diagnostic work was dropped when the pin moved to 0.155.1, which reports
its real version.

## Termux rules

Use `TMPDIR` for temporary files and validate it before builds. In Termux the
canonical fallback is `/data/data/com.termux/files/usr/tmp`; do not introduce
`/tmp` or `/data/local/tmp` into Android scripts. Keep NDK, Cargo, target, and
sccache directories outside the source checkout when the surrounding build
workflow specifies them.
