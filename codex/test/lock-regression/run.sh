#!/usr/bin/env bash
# Regression harness for the Android/bionic file-lock patches in codex-rs and for
# the execpolicy "always allow" persistence path.
#
# Usage:
#   bash codex/test/lock-regression/run.sh /path/to/codex-android
#
# It boots the real `codex app-server` against a scripted Responses API stand-in,
# answers a command approval with an execpolicy amendment, and asserts:
#   1. the app-server/TUI path never reports `lock() not supported` (the failure
#      mode that used to break startup and the rules-file write);
#   2. `<CODEX_HOME>/rules/default.rules` is created with the allow rule, i.e.
#      "don't ask again" really persists on Android;
#   3. the approved command actually ran.
set -euo pipefail

CODEX_BIN="${1:-${CODEX_BIN:-}}"
if [ -z "$CODEX_BIN" ] || [ ! -x "$CODEX_BIN" ]; then
    echo "usage: bash $0 /path/to/codex-android (or set CODEX_BIN)" >&2
    exit 2
fi
CODEX_BIN="$(cd "$(dirname "$CODEX_BIN")" && pwd)/$(basename "$CODEX_BIN")"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMPDIR="${TMPDIR:-/data/data/com.termux/files/usr/tmp}"
WORK="$(mktemp -d "$TMPDIR/codex-lock-regression.XXXXXX")"
CODEX_HOME="$WORK/codex-home"
mkdir -p "$CODEX_HOME"

SERVER_PID=""
cleanup() {
    if [ -n "$SERVER_PID" ]; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    rm -rf "$WORK"
}
trap cleanup EXIT

PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
COMMAND="echo hola > $WORK/approved.txt"

python3 "$HERE/fake-responses-server.py" "$PORT" "$COMMAND" "$WORK/requests.log" >"$WORK/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 50); do
    if grep -q "listening" "$WORK/server.log" 2>/dev/null; then break; fi
    sleep 0.2
done

echo "== codex-lock-regression: $CODEX_BIN"
python3 "$HERE/appserver-approval-probe.py" "$CODEX_BIN" "$CODEX_HOME" "$PORT" '["echo"]' \
    >"$WORK/probe.log" 2>&1 || true

STATUS=0
fail() { echo "FAIL: $*" >&2; STATUS=1; }

if grep -q "lock() not supported" "$WORK/probe.log"; then
    fail "app-server reported 'lock() not supported' (unpatched File::lock on Android)"
    grep -n "lock() not supported" "$WORK/probe.log" >&2 || true
else
    echo "ok: no 'lock() not supported' in the app-server run"
fi

RULES="$CODEX_HOME/rules/default.rules"
if [ -s "$RULES" ] && grep -q 'decision="allow"' "$RULES"; then
    echo "ok: persisted rule -> $(cat "$RULES")"
else
    fail "no allow rule written to $RULES"
    sed -n '1,80p' "$WORK/probe.log" >&2 || true
fi

if [ -s "$WORK/approved.txt" ]; then
    echo "ok: approved command executed -> $(cat "$WORK/approved.txt")"
else
    fail "the approved command did not run"
fi

if [ "$STATUS" -eq 0 ]; then
    echo "== codex-lock-regression: PASS"
else
    echo "== codex-lock-regression: FAIL"
fi
exit "$STATUS"
