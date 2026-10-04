#!/usr/bin/env bash
# Device smoke test for the Codex code-mode host.
#
# Usage:
#   bash codex/test/host-smoke/run.sh /path/to/codex-code-mode-host
#
# No build-time check can prove the host runs: a misaligned TLS segment, a missing
# Bionic symbol, or an unusable V8 archive all fail in the dynamic loader on the
# device, before main(). This script asserts the binary actually gets past the
# linker and stays there long enough to be exercised.
set -euo pipefail

HOST="${1:-${HOST_BIN:-}}"
if [ -z "$HOST" ] || [ ! -x "$HOST" ]; then
    echo "usage: bash $0 /path/to/codex-code-mode-host (or set HOST_BIN)" >&2
    exit 2
fi
HOST="$(cd "$(dirname "$HOST")" && pwd)/$(basename "$HOST")"

TMPDIR="${TMPDIR:-/data/data/com.termux/files/usr/tmp}"
WORK="$(mktemp -d "$TMPDIR/codex-host-smoke.XXXXXX")"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

STATUS=0
fail() { echo "FAIL: $*" >&2; STATUS=1; }

# Signatures of a loader rejection: bionic prints the executable name and dies
# without ever running Rust code.
loader_rejected() {
    grep -Eqi 'CANNOT LINK EXECUTABLE|TLS segment is underaligned|needs library|bad CPU type|cannot execute' <<<"$1"
}

usage_out="$("$HOST" --help 2>&1 || true)"
if loader_rejected "$usage_out"; then
    fail "--help no llegó a ejecutarse: el loader rechazó el binario"
    printf '%s\n' "$usage_out" >&2
elif ! grep -q '^Usage: codex-code-mode-host' <<<"$usage_out"; then
    fail "--help no devolvió el uso esperado"
    printf '%s\n' "$usage_out" >&2
else
    echo "ok: --help responde desde clap (el binario carga)"
fi

# Real start: stdio transport with an immediate EOF. Whatever the exit code, a
# clean loader means the process reached main(); anything else is a regression.
set +e
timeout -k 2 20 "$HOST" --listen stdio:// </dev/null >"$WORK/stdout" 2>"$WORK/stderr"
RC=$?
set -e
combined="$(cat "$WORK/stdout" "$WORK/stderr")"
if loader_rejected "$combined"; then
    fail "arranque real abortado por el loader (rc=$RC)"
    printf '%s\n' "$combined" >&2
elif [ "$RC" -eq 124 ] || [ "$RC" -eq 137 ]; then
    fail "el host no terminó solo ante EOF stdin (rc=$RC, timeout)"
else
    echo "ok: arranque real con stdio:// sin intervención del loader (rc=$RC)"
fi

if [ "$STATUS" -eq 0 ]; then
    echo "== codex-host-smoke: OK ($HOST)"
else
    echo "== codex-host-smoke: FALLOS ($HOST)" >&2
fi
exit "$STATUS"
