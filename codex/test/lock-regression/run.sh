#!/usr/bin/env bash
# Regression harness for the Android/bionic file-lock patches in codex-rs.
#
# Usage:
#   bash codex/test/lock-regression/run.sh /path/to/codex-android
#
# It boots the real `codex app-server` against a scripted Responses API stand-in,
# runs a turn whose tool call executes a command, and asserts:
#   1. nothing reports `lock() not supported` / `try_lock() not supported` (the
#      failure mode that used to break startup, history, the CA cache and the
#      rules-file write);
#   2. if the app-server asked for a command approval, the "don't ask again"
#      answer really persisted `<CODEX_HOME>/rules/default.rules`;
#   3. the command actually ran.
#
# Assertion 2 is conditional on purpose: in the default mode the app-server never
# denies a command, so no escalation prompt appears and the rules write stays out of
# reach. Set SANDBOX_MODE=workspace-write (or read-only) to run the turn through the
# proot wrapper instead; that is the mode in which the sandbox produces denials, and
# it also enables the extra assertion below that proves the real turn went through
# codex-linux-sandbox rather than merely through a binary that exists.
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

SANDBOX_MODE="${SANDBOX_MODE:-danger-full-access}"
export SANDBOX_MODE
# El wrapper anota cada invocación en este archivo: es la evidencia de que el turno
# real pasó por proot, no de que el binario del sandbox exista en el árbol instalado.
export CODEX_ANDROID_SANDBOX_LOG="$WORK/sandbox-calls.jsonl"

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

if [ "$SANDBOX_MODE" = "danger-full-access" ]; then
    echo "skip: modo $SANDBOX_MODE; SANDBOX_MODE=workspace-write ejercita el wrapper de proot"
else
    if grep -q "was required but not provided" "$WORK/probe.log"; then
        fail "el modo $SANDBOX_MODE no resolvió el exe de sandbox (arg0 no lo inyectó)"
    elif [ -s "$CODEX_ANDROID_SANDBOX_LOG" ] && grep -q '"proot_argv"' "$CODEX_ANDROID_SANDBOX_LOG"; then
        echo "ok: el turno pasó por el wrapper de proot ($(wc -l < "$CODEX_ANDROID_SANDBOX_LOG") invocaciones)"
    else
        fail "modo $SANDBOX_MODE: no hay registro de que el wrapper se invocó"
        sed -n '1,60p' "$WORK/probe.log" >&2 || true
    fi
fi

RULES="$CODEX_HOME/rules/default.rules"
if grep -q "requestApproval" "$WORK/probe.log"; then
    if [ -s "$RULES" ] && grep -q 'decision="allow"' "$RULES"; then
        echo "ok: persisted rule -> $(cat "$RULES")"
    else
        fail "se pidió aprobación pero no se escribió una regla allow en $RULES"
        sed -n '1,80p' "$WORK/probe.log" >&2 || true
    fi
else
    # Sin denegación no hay nada que escalar, así que el app-server nunca pide
    # aprobación y la escritura de reglas queda fuera de alcance en este modo.
    echo "skip: rules/default.rules no alcanzable (nada pidió aprobación en modo $SANDBOX_MODE)"
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
