#!/usr/bin/env bash
# Harness en dispositivo del wrapper de sandbox de Android/Termux.
#
# Usage:
#   bash codex/test/sandbox-proot/run.sh /path/to/codex-linux-sandbox
#
# El binario bajo prueba es el crate `codex-android-sandbox`, que traduce el
# `--permission-profile` que produce el manager de sandbox de Codex a un arranque de
# `proot`. Este script ejercita las dos mitades del contrato:
#
#   1. argv: con `--dry-run` se verifica qué binds se emitirían para cada perfil;
#   2. enforcement: corridas reales, midiendo qué escrituras, lecturas y conexiones
#      efectivamente fallan dentro del sandbox.
#
# (2) es la parte que importa: un proot que acepte `-b ...:ro` sin aplicarlo dejaría
# el sandbox como decorado, y eso solo se ve ejecutando. Los perfiles JSON que se
# arman abajo copian la forma exacta que serializa `PermissionProfile`; el pin de esa
# forma vive en el test `the_wire_shape_of_the_profile_is_pinned` (se corre en CI,
# `Test Android sandbox wrapper`) y si upstream la cambia, ese test falla antes de
# que este harness sirva de algo.
set -euo pipefail

SANDBOX_BIN="${1:-${CODEX_SANDBOX_BIN:-}}"
if [ -z "$SANDBOX_BIN" ] || [ ! -x "$SANDBOX_BIN" ]; then
    echo "usage: bash $0 /path/to/codex-linux-sandbox (o CODEX_SANDBOX_BIN)" >&2
    exit 2
fi
SANDBOX_BIN="$(cd "$(dirname "$SANDBOX_BIN")" && pwd)/$(basename "$SANDBOX_BIN")"

TMPDIR="${TMPDIR:-/data/data/com.termux/files/usr/tmp}"
WORK="$(mktemp -d "$TMPDIR/codex-sandbox-proot.XXXXXX")"
export TMPDIR="$WORK/tmp"
WS="$WORK/ws"
mkdir -p "$WS/.git" "$TMPDIR"
printf '[core]\n\tbare = false\n' > "$WS/.git/config"
printf 'semilla\n' > "$WS/archivo.txt"
SHELL_BIN="${SHELL_BIN:-/system/bin/sh}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

STATUS=0
fail() { echo "FAIL: $*" >&2; STATUS=1; }
ok() { echo "ok: $*"; }

# El harness también necesita verificar el bloqueo de red contra un puerto que
# realmente escuche, para que un fallo no pueda atribuirse a que no había nada.
LISTEN_PORT="$("$PYTHON_BIN" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
"$PYTHON_BIN" - "$LISTEN_PORT" > "$WORK/listener.log" 2>&1 <<'PY' &
import socket, sys
port = int(sys.argv[1])
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen(8)
while True:
    conn, _ = server.accept()
    conn.sendall(b"pidao\n")
    conn.close()
PY
LISTENER_PID=$!
trap 'kill "$LISTENER_PID" 2>/dev/null || true; cleanup' EXIT

for _ in $(seq 1 25); do
    # `$$` se expande aquí, cuando la línea todavía es shell: el programa python ve un
    # número literal y el control puede repetirse sin reescribir el archivo.
    if "$PYTHON_BIN" -c "
import socket, sys
try:
    socket.create_connection(('127.0.0.1', $LISTEN_PORT), 1).close()
except OSError:
    sys.exit(1)
" 2>/dev/null; then break; fi
    sleep 0.2
done

profile() {
    # profile <kind> [<ruta>] -> JSON PermissionProfile en stdout
    "$PYTHON_BIN" - "$1" "$WS" <<'PY'
import json, sys

kind, ws = sys.argv[1], sys.argv[2]


def entry(value, access):
    return {"path": {"type": "special", "value": value}, "access": access}

def path_entry(path, access):
    return {"path": {"type": "path", "path": path}, "access": access}


def managed(entries, network):
    return {
        "type": "managed",
        "file_system": {"type": "restricted", "entries": entries},
        "network": network,
    }


if kind == "read-only":
    profile_value = managed([entry({"kind": "root"}, "read")], "restricted")
elif kind == "workspace-write":
    profile_value = managed(
        [
            entry({"kind": "root"}, "read"),
            entry({"kind": "project_roots"}, "write"),
            entry({"kind": "slash_tmp"}, "write"),
            entry({"kind": "tmpdir"}, "write"),
            entry({"kind": "project_roots", "subpath": ".git"}, "read"),
            entry({"kind": "project_roots", "subpath": ".agents"}, "read"),
            entry({"kind": "project_roots", "subpath": ".codex"}, "read"),
        ],
        "restricted",
    )
elif kind == "workspace-write-network":
    profile_value = managed(
        [entry({"kind": "root"}, "read"), entry({"kind": "project_roots"}, "write")],
        "enabled",
    )
elif kind == "danger-full-access":
    profile_value = {"type": "disabled"}
elif kind == "read-allowlist":
    profile_value = managed([path_entry(ws, "write")], "enabled")
elif kind == "glob-deny":
    profile_value = managed(
        [
            entry({"kind": "root"}, "read"),
            entry({"kind": "project_roots"}, "write"),
            {"path": {"type": "glob_pattern", "pattern": "**/*.pem"}, "access": "deny"},
        ],
        "enabled",
    )
else:
    raise SystemExit(f"perfil desconocido: {kind}")

print(json.dumps(profile_value, separators=(",", ":")))
PY
}

run_sandbox() {
    # run_sandbox <kind> [--flag adicional] -- <comando...>
    local kind="$1"
    shift
    local extra=()
    if [ "${1:-}" != "--" ]; then
        extra=("$1")
        shift
    fi
    # Se descarta el `--` del llamador: el wrapper lo recibe justo antes del comando.
    shift
    local json
    json="$(profile "$kind")"
    "$SANDBOX_BIN" \
        --sandbox-policy-cwd "$WS" \
        --command-cwd "$WS" \
        "${extra[@]}" \
        --permission-profile "$json" \
        -- "$@"
}

exits_zero() { "$@" >/dev/null 2>&1; }
exits_nonzero() { ! "$@" >/dev/null 2>&1; }

echo "== codex-sandbox-proot: $SANDBOX_BIN"
if exits_nonzero "$SHELL_BIN" -c 'exit 0'; then
    echo "AVISO: \$SHELL_BIN ($SHELL_BIN) no corre; el harness no puede concluir nada" >&2
    exit 2
fi

# ---------------------------------------------------------------- 1. argv (dry-run)
dry="$(run_sandbox workspace-write --dry-run -- "$SHELL_BIN" -c 'exit 0')"
printf '%s\n' "$dry" > "$WORK/dry-run.txt"

grep -qx -- '--net-policy' "$WORK/dry-run.txt" && grep -qx -- 'deny' "$WORK/dry-run.txt" \
    && ok "workspace-write bloquea la red" \
    || fail "workspace-write no emite --net-policy deny"
grep -qxF -- '/:/:ro' "$WORK/dry-run.txt" \
    && ok "el filesystem se monta read-only desde /" \
    || fail "falta el bind /:/:ro"
grep -qxF -- "$WS:$WS" "$WORK/dry-run.txt" \
    && ok "el workspace se reabre en escritura" \
    || fail "falta el bind escribible del workspace"
grep -qxF -- "$WS/.git:$WS/.git:ro" "$WORK/dry-run.txt" \
    && ok ".git queda read-only" \
    || fail "falta el carveout de .git"
grep -qxF -- "$TMPDIR:$TMPDIR" "$WORK/dry-run.txt" \
    && ok "TMPDIR queda escribible" \
    || fail "falta el bind escribible de TMPDIR"
grep -q ":$WS/.codex:ro" "$WORK/dry-run.txt" \
    && ok ".codex se enmascara aunque no exista" \
    || fail "no se enmascara .codex (se podría crear sin aprobación)"
grep -qxF -- '/dev/null:/dev/null' "$WORK/dry-run.txt" \
    && ok "/dev/null queda utilizable bajo /dev read-only" \
    || fail "falta el bind de /dev/null"
if grep -qxF -- '--' "$WORK/dry-run.txt"; then
    fail "proot rechaza '--' como separador; el argv emitido lo incluye"
else
    ok "el argv no usa '--' (proot lo rechazaría)"
fi
tail -n 3 "$WORK/dry-run.txt" | grep -qxF -- 'exit 0' \
    && ok "el comando va al final del argv" \
    || fail "el comando no termina el argv"

dry_readonly="$(run_sandbox read-only --dry-run -- "$SHELL_BIN" -c 'exit 0')"
printf '%s\n' "$dry_readonly" > "$WORK/dry-run-read-only.txt"
if grep -qxF -- "$WS:$WS" "$WORK/dry-run-read-only.txt"; then
    fail "read-only abre escritura en el workspace"
else
    ok "read-only no abre ninguna escritura en el workspace"
fi
if grep -qxF -- '--net-policy' "$WORK/dry-run-read-only.txt"; then
    ok "read-only bloquea la red"
else
    fail "read-only no bloquea la red"
fi
dry_danger="$(run_sandbox danger-full-access --dry-run -- "$SHELL_BIN" -c 'exit 0')"
if [ "$(printf '%s' "$dry_danger" | tr '\n' ' ')" = "$SHELL_BIN -c exit 0" ]; then
    ok "danger-full-access se ejecuta sin wrapper"
else
    fail "danger-full-access no degeneró al comando desnudo: $(printf '%s' "$dry_danger" | tr '\n' ' ')"
fi

# ------------------------------------------------------------ 2. enforcement real
if exits_zero run_sandbox workspace-write -- "$SHELL_BIN" -c "echo adentro > '$WS/dentro.txt'"; then
    ok "se puede escribir dentro del workspace"
else
    fail "no se pudo escribir dentro del workspace"
fi
if exits_nonzero run_sandbox workspace-write -- "$SHELL_BIN" -c "echo afuera > '$WORK/afuera.txt'"; then
    ok "una escritura fuera del workspace falla"
else
    fail "SE PUDO ESCRIBIR FUERA DEL WORKSPACE: el sandbox no aísla"
fi
[ ! -e "$WORK/afuera.txt" ] || fail "la escritura de control dejó rastro afuera"
if exits_nonzero run_sandbox workspace-write -- "$SHELL_BIN" -c "echo x > '$WS/.git/config'"; then
    ok ".git sigue intachable"
else
    fail "se pudo escribir en .git"
fi
if exits_zero run_sandbox workspace-write -- "$SHELL_BIN" -c "cat '$WS/.git/config' > /dev/null"; then
    ok ".git se sigue pudiendo leer (git lo necesita)"
else
    fail "no se pudo leer .git"
fi
if exits_nonzero run_sandbox workspace-write -- "$SHELL_BIN" -c "mkdir -p '$WS/.codex' && echo x > '$WS/.codex/config.toml'"; then
    ok "no se puede crear la metadata .codex del workspace"
else
    fail "se pudo crear .codex sin aprobación"
fi
if exits_zero run_sandbox workspace-write -- "$SHELL_BIN" -c "echo t > '$TMPDIR/en-tmp.txt'"; then
    ok "TMPDIR queda escribible"
else
    fail "no se pudo escribir en TMPDIR"
fi
if exits_zero run_sandbox workspace-write -- "$SHELL_BIN" -c "echo silencio > /dev/null"; then
    ok "/dev/null funciona"
else
    fail "/dev/null no funciona dentro del sandbox (rompe git y compañía)"
fi
if exits_nonzero run_sandbox workspace-write -- "$SHELL_BIN" -c "echo veneno > /dev/kmsg"; then
    ok "/dev sigue cerrado salvo los nodos reabiertos"
else
    fail "se pudo escribir en /dev/kmsg"
fi

# La red: primero el control positivo sin sandbox, luego dentro. El programa python va
# en una sola línea a propósito: es el argumento de un comando envuelto, y cualquier
# intermediario que reempaque el argv línea a línea partiría un programa multilínea.
NET_PROBE="import socket; socket.create_connection(('127.0.0.1', $LISTEN_PORT), 3).close()"
if exits_zero run_sandbox workspace-write -- "$PYTHON_BIN" -c "print(1)"; then
    ok "python corre dentro del sandbox (control para las pruebas de red)"
else
    fail "python no corre dentro del sandbox; las pruebas de red no valdrían nada"
fi
if exits_zero "$PYTHON_BIN" -c "$NET_PROBE"; then
    if exits_nonzero run_sandbox workspace-write -- "$PYTHON_BIN" -c "$NET_PROBE"; then
        ok "connect() está bloqueado con la red restringida"
    else
        fail "HUBO RED DENTRO DEL SANDBOX CON network=restricted: el sandbox no aísla"
    fi
else
    fail "el listener de control no responde; no se puede concluir nada de la red"
fi
if exits_zero run_sandbox workspace-write-network -- "$PYTHON_BIN" -c "$NET_PROBE"; then
    ok "con network=enabled la red sigue disponible"
else
    fail "network=enabled salió bloqueado igualmente"
fi

rc=0
run_sandbox workspace-write -- "$SHELL_BIN" -c 'exit 42' >/dev/null 2>&1 || rc=$?
if [ "$rc" = 42 ]; then
    ok "el código de salida del comando llega intacto"
else
    fail "código de salida propagado como $rc, se esperaba 42"
fi
if run_sandbox workspace-write -- "$SHELL_BIN" -c "printf '%s|%s\n' 'con espacio' 'otro arg'" \
    | grep -q 'con espacio|otro arg'; then
    ok "los argumentos con espacios llegan sin partirse"
else
    fail "el argv del comando se mutiló"
fi
if exits_zero run_sandbox read-only -- "$SHELL_BIN" -c "cat '$WS/dentro.txt' > /dev/null"; then
    ok "read-only conserva las lecturas"
else
    fail "read-only no dejó leer un archivo del workspace"
fi
if exits_nonzero run_sandbox read-only -- "$SHELL_BIN" -c "echo x > '$WS/otro.txt'"; then
    ok "read-only bloquea toda escritura"
else
    fail "read-only permitió escribir"
fi
if exits_zero run_sandbox danger-full-access -- "$SHELL_BIN" -c "echo x > '$WORK/afuera-danger.txt'"; then
    ok "danger-full-access escribe en cualquier lado (por diseño)"
else
    fail "danger-full-access no se degeneró a ejecución directa"
fi

# ------------------------------------------------------------- 3. fail-closed
check_refuses() {
    # check_refuses <etiqueta> <kind> <flag-extra> <canario>
    local label="$1" kind="$2" extra="$3" canario="$WORK/canario-$4"
    rm -f "$canario"
    local rc=0
    if [ "$extra" = "none" ]; then
        run_sandbox "$kind" -- "$SHELL_BIN" -c "touch '$canario'" >/dev/null 2>&1 || rc=$?
    else
        run_sandbox "$kind" "$extra" -- "$SHELL_BIN" -c "touch '$canario'" >/dev/null 2>&1 || rc=$?
    fi
    if [ -e "$canario" ]; then
        fail "$label: el comando se ejecutó igualmente (rc=$rc)"
    elif [ "$rc" != 78 ]; then
        fail "$label: salida $rc, se esperaba 78 (EX_CONFIG)"
    else
        ok "$label: falla cerrado sin ejecutar el comando"
    fi
    rm -f "$canario"
}

check_refuses "perfil con allowlist de lecturas" read-allowlist none allowlist
check_refuses "deny con glob" glob-deny none glob

rc=0
json="$(profile workspace-write)"
"$SANDBOX_BIN" --sandbox-policy-cwd "$WS" --permission-profile "$json" \
    --nuevo-knob-del-que-no-se-nada -- "$SHELL_BIN" -c true >/dev/null 2>&1 || rc=$?
if [ "$rc" = 78 ]; then
    ok "un flag de sandbox desconocido aborta en vez de ignorarse"
else
    fail "flag desconocido devolvió $rc, se esperaba 78"
fi

rc=0
CODEX_ANDROID_PROOT="$WORK/no-existe-proot" \
    run_sandbox workspace-write -- "$SHELL_BIN" -c "touch '$WORK/canario-sin-proot'" \
    >/dev/null 2>&1 || rc=$?
if [ "$rc" = 78 ] && [ ! -e "$WORK/canario-sin-proot" ]; then
    ok "sin proot disponible no se ejecuta nada"
else
    fail "sin proot se ejecutó el comando o salió $rc (se esperaba 78)"
fi

if [ "$STATUS" -eq 0 ]; then
    echo "== codex-sandbox-proot: PASS"
else
    echo "== codex-sandbox-proot: FAIL"
fi
exit "$STATUS"
