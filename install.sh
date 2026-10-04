#!/bin/sh
set -eu

# Run from a checkout when available; also support `curl .../install.sh | sh`.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd)
if [ -f "$SCRIPT_DIR/ci/scripts/installer.py" ]; then
    exec python3 "$SCRIPT_DIR/ci/scripts/installer.py" "$@"
fi

: "${TMPDIR:=/data/data/com.termux/files/usr/tmp}"
export TMPDIR
mkdir -p "$TMPDIR"
test -d "$TMPDIR" && test -w "$TMPDIR"
INSTALLER=$(mktemp "$TMPDIR/codex-termux-installer.XXXXXX")
trap 'rm -f "$INSTALLER"' EXIT HUP INT TERM
REPO=${CODEX_INSTALL_REPO:-Leonisaurov/codex-termux}
REF=${CODEX_INSTALL_REF:-main}
curl -fsSL "https://raw.githubusercontent.com/$REPO/$REF/ci/scripts/installer.py" -o "$INSTALLER"
exec python3 "$INSTALLER" "$@"
