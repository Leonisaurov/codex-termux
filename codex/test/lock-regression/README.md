# Lock regression harness (Android/Termux)

Rust's `std::fs::File::lock`/`try_lock`/`lock_shared`/`try_lock_shared` return
`ErrorKind::Unsupported` ("lock() not supported") on `aarch64-linux-android`:
`library/std/src/sys/fs/unix.rs` only implements flock for a fixed list of
targets that does not include Android — verified for 1.95 through 1.98. Every
unpatched call site therefore fails at runtime with a lock error, which is how
startup (curated plugins sync, app-server control socket), the network-proxy
CA cache, history paging, and the `~/.codex/rules/default.rules` write used by
"don't ask again" used to break on device.

`codex-rs` carries `CODEX-TERMUX-ANDROID-PATCH` markers at every such site.
This harness proves, against a **built binary**, that

1. the app-server/TUI path emits no `lock() not supported`, and
2. answering a command approval with an execpolicy amendment persists an
   `allow` rule to `<CODEX_HOME>/rules/default.rules`,
3. the approved command actually ran.

## Usage

```sh
bash codex/test/lock-regression/run.sh /path/to/codex-android
# or: CODEX_BIN=codex-android bash codex/test/lock-regression/run.sh
```

Exit code 0 means all three checks passed. Everything runs in a throwaway
`$TMPDIR/codex-lock-regression.*` directory with its own `CODEX_HOME`, so the
user's real `~/.codex` is never touched.

## How it works

* `fake-responses-server.py` implements the Responses API SSE subset the agent
  needs: it always answers with a `shell_command` tool call, so a turn always
  reaches an approval prompt without any real model or credentials.
* `appserver-approval-probe.py` drives the real `codex app-server` over stdio
  JSONL (`initialize` → `thread/start` → `turn/start`), then answers
  `item/commandExecution/requestApproval` with
  `acceptWithExecpolicyAmendment`, mirroring what the TUI's "don't ask again"
  and the ntfy relay's "Permitir siempre (regla)" send.
* `run.sh` wires both together and asserts the outcomes above.

When a new upstream lock site appears, this harness is the fastest way to see it:
the failure shows up as a `lock() not supported` line in the app-server output.
