# Progreso actual — 2026-10-04

Repositorio creado al extraer Codex del stack `opencode-termux`. El árbol
`codex/src` es byte-idéntico al del commit `12d2c97` de ese repositorio, y su
pipeline (`build-codex.yml`, `build-rusty-v8-android.yml`) y sus helpers de `ci/`
viajaron sin editar, por lo que el contrato de cache conserva los mismos digests.

## Estado

- Fuente vendorizada: `be2951ea34f0d295ed0becf97079f92fa5f6950e` (`rust-v0.155.1`).
- Rusty V8 `150.4.0`: el release `rusty-v8-v150.4.0` se espejó desde
  `Leonisaurov/opencode-termux` para que el primer build restaure el archivo
  estático en vez de recompilar V8 (~110 min).
- Las caches de Actions son por repositorio: la primera corrida de cada clave es
  `miss` legítimo, no una regresión.
- La caché local de `cargo` (`codex/src/codex-rs/target`, 1.9 G) y `.vscode`
  se trasladaron con `mv` (rename en el mismo dispositivo) desde el repo del
  stack, así que los builds en dispositivo siguen donde vive el producto. Sus
  fingerprints de cargo se invalidan al cambiar de ruta; el valor del traslado es
  liberar el repo viejo, no reutilizar objetos.

Nada de lo anterior se declara funcional hasta leer el log de la run y listar los
artifacts y releases publicados.

## Verificación de paridad con el repositorio de origen (2026-10-04)

Comparación `git ls-files -s codex` entre `opencode-termux@12d2c97` y este repo:

- 7625 rutas trackeadas en ambos lados, sin diferencias de modo ni de symlink.
- Un único blob distinto: `codex/test/lock-regression/fake-responses-server.py`,
  donde el valor por defecto hardcoded `/data/data/com.termux/.../approved.txt`
  pasó a derivar de `tempfile.gettempdir()` (`run.sh` lo sobreescribe siempre).
- Tree de la fuente: `ceabd0d75e1cd0851e4746d8c576ef6cd021810d` en ambos lados,
  que es exactamente el input del contrato de cache de `build-codex.yml`.
- El mismo tree está confirmado en el **remoto**: `main` = `4fb96c403c8ede2a70b390d359b99c20df03c151`
  y `codex/src` = `ceabd0d7…` leído con `gh api …/git/trees/…`, así que la
  identidad no depende del worktree local.

## Gates de CI leídas en log

- Run `37189092590` (push a `main`): `contracts` y `rusty-v8 / build-android` en
  verde. El log del job imprime `Restored Rusty V8 from release rusty-v8-v150.4.0`,
  así que el release espejado evita el build de V8 desde fuente.
- Cierre de esa misma run en verde: `Validate Codex outputs for durable cache`,
  `Save Codex durable artifacts` y `Upload Codex binaries` en `success`, con el
  artifact `codex-android-aarch64-be2951ea34f0d295ed0becf97079f92fa5f6950e` de
  391842279 bytes (ID 11298803340).
- Run `37191620271` (segunda corrida): `Cache hit for:
  ci-cache-v2-codex-eb4660af53126ae739d98b2f0786b867f666d9199a5644c29c1c444f73610cb1-artifact`
  (337 MB restaurados) y `Cache restored from key: ci-cache-v2-toolchain-Linux-X64-
  ndk-28.1.13356709-api-24` (650 MB). El paso de compilación quedó `skipped` porque
  los artifacts durables ya estaban en cache: la incrementalidad funciona dentro del
  repo nuevo.
- El digest del contrato es **el mismo que calculaba el stack**: la corrida vieja
  `35683166526` de `build-codex.yml` usaba `ci-cache-v2-codex-eb4660af53126ae739d98
  b2f0786b867f666d919…`. La extracción no alteró la identidad del contrato, solo el
  espacio de nombres de cache (por repositorio).

## Ensayo local del tramo publish → install (2026-10-04)

Sobre un árbol sintético de artifacts ELF `aarch64` generado en `$TMPDIR` (no toca
CI ni releases):

- `package-release.py` produjo `codex-<ref>-android-aarch64.tar.gz` +
  `manifest.json` con asset plano, `sha256` y `size` coherentes.
- `installer.py --manifest <manifiesto> --all --yes` instaló los tres nombres bajo
  `bin/` con modo `0755` y validó checksum y arquitectura.
- Rechazo cruzado de esquemas: el instalador de este repo rechaza un manifiesto
  `opencode-termux/stack/v1` y el del stack rechaza uno `codex-termux/v1`, ambos con
  `schema de manifiesto no soportado` y código 1.

## Release publicado y validado en dispositivo (2026-10-04)

- Run `37191642794` (`workflow_dispatch`, `release=0.155.1`): `success`. Release
  `codex-v0.155.1` con `manifest.json` (schema `codex-termux/v1`) y
  `codex-be2951ea…-android-aarch64.tar.gz` de 383301367 bytes; el `sha256` del asset
  descargado coincide con el del manifiesto (`43fa15e974bc6fa7…`).
- Instalación real con `--prefix` bajo `$TMPDIR`: `codex-cli 0.155.1`, código 0,
  `ELF 64-bit LSB pie executable, ARM aarch64`.
- El instalador copiaba cada fichero extraído a `payload/` antes de moverlo, así que
  pedía ~3.7 GB libres para un producto que ocupa 1.65 GB (`codex-android` son
  1.43 GB sin comprimir) y abortaba con `ENOSPC` en este dispositivo. Ahora mueve
  dentro del staging (`os.replace`, con `copy2` de reserva si hay `EXDEV`): el pico
  baja a ~2.0 GB y la instalación termina.

## Causa raíz: `codex-code-mode-host` no arrancaba en Bionic (2026-10-04)

- `codex-code-mode-host --help` abortaba con `executable's TLS segment is underaligned:
  alignment is 8 (skew 0), needs to be at least 64 for ARM64 Bionic`.
- No lo causó la extracción: el artifact `codex-android-aarch64-be2951ea…` de la
  corrida vieja `35683166526` del repositorio del stack falla **idénticamente**, y el
  `codex-code-mode-host` publicado en `stack-v1.18.11` (commit `fee9a8d5…`) sí arranca
  y devuelve su `Usage:`. La rotura entró con el rebase a `rust-v0.155.1`, antes de
  separar los repositorios; `codex-android` no la padece.
- Comparando los dos ELF (`readelf -lW` / `-sW`, mismo binario descargado de cada
  release):

  | build | `.tdata` | `.tbss` | `PT_TLS p_align` | símbolo |
  |---|---|---|---|---|
  | `fee9a8d5` (funciona) | 0x44, algn **64** | 0xd8, algn 8 | **0x40** | `tls_align_stub` presente |
  | `be2951ea` (roto) | 0x04, algn 4 | 0xd8, algn 8 | **0x8** | sin `tls_align_stub` |

  El puerto llevaba en `codex-rs/code-mode-host/src/main.rs` un `global_asm!` que emite
  64 bytes en `.tdata` con `.p2align 6`; lld toma la alineación del segmento de la
  sección mejor alineada, así que ese stub es lo que producía `p_align` 0x40. El
  re-vendor sobreescribió `main.rs` con el de upstream y **barrió el parche sin dejar
  marcador**: el inventario de `CODEX-TERMUX-ANDROID-PATCH` no lo cubría porque el stub
  no lo usaba. El `commit 12d2c97` re-aplicó las cfgs de sandbox pero no este bloque.
- Verificado en el dispositivo antes de tocar nada: un `.tdata` con `.p2align 6` hace
  que lld emita `PT_TLS` con `p_align` 0x40 (binario C mínimo, ejecutado con éxito);
  sin segmento TLS también carga, así que el gate solo aplica cuando existe `PT_TLS`.
- Fix (`3937e52`): se re-aplica el stub **verbatim** sobre el `main.rs` de 0.155.1,
  ahora con marcador `CODEX-TERMUX-ANDROID-PATCH`, y `codex/scripts/build-codex-android.sh`
  valida `PT_TLS >= 64` en las dos salidas ELF — el tripwire rechaza el binario roto y
  acepta el bueno, comprobado localmente contra ambos. Documentado en
  `codex/src/docs/android-termux.md` (§ TLS segment alignment).
- Pendiente de cerrar con la corrida en curso: build verde, release publicada con el
  fix, e instalación real con `codex-code-mode-host --help` devolviendo su `Usage:`.


## Huella de cache tras la extracción (2026-10-04)

Medida con `gh api repos/<repo>/actions/caches` (solo lectura):

- `Leonisaurov/opencode-termux`: `total_count=0`. GitHub ya podó por LRU de 7 días
  todas las claves del stack y de Codex (la última corrida ahí fue el 2026-09-22), así
  que la extracción no dejó cache huérfana que servir un artifact roto ni cuota
  ocupada; no hay nada que podar a mano.
- `Leonisaurov/codex-termux`: 7 entradas, ~2.77 GiB de los 10 GiB. `toolchain`
  (650 MiB), `rusty-v8` (35 + 29 + 29 MiB), el final del contrato viejo (337 MiB) y
  el de compilador (1.69 GiB, tocado durante la corrida del fix).
- El `ci-cache-v2-codex-compiler-…` renace con prefijo de la contract-key anterior
  (`restore-prefix`), por lo que un cambio en `codex/src` no es una compilación fría:
  restaura objetos y solo recompila lo alcanzado por el parche.

## Aislamiento entre los dos instaladores (2026-10-04)

Invariante del plan: un manifiesto del stack nunca puede alimentarse al instalador
de Codex ni al revés. Probada con los manifiestos **publicados** de ambos repos
(`stack-v1.18.11` → `opencode-termux.stack/v1` con 5 componentes; `codex-v0.155.1`
→ `codex-termux/v1` con 1), cruzados con `--manifest … --all --dry-run`:

- instalador de Codex + manifiesto del stack ⇒ `rc=1`, `schema de manifiesto no soportado`;
- instalador del stack + manifiesto de Codex ⇒ `rc=1`, el mismo rechazo.

Además `installer.py --just codex` en el repo del stack falla con
`invalid choice: 'codex' (choose from 'bun', 'opentui', 'opencode', 'kilo')`, que es
la señal de retirada esperada. Se comprobó también que `download()` es código
idéntico en los dos repos: el `FileNotFoundError` que produce un `--manifest` local
cuyo `.tar.gz` no está junto al manifiesto es comportamiento heredado compartido
(la validación descarga y desenvasa de verdad), no un defecto de la extracción.
Nota: `stack-v1.18.11` conserva su componente `codex` — es historia publicada, no
un re-cableado.

## Release verificado en dispositivo y dos defectos que aparecieron al verificarlo (2026-10-04)

**`codex-v0.155.1-1` publicado y bueno donde estaba roto.** Run `37211704073` (push del
fix TLS) verde: el build imprimió `TLS: sin segmento PT_TLS (codex-android)` y
`TLS: PT_TLS alineado a 64 (codex-code-mode-host)`. Run de publicación `37213433702`
verde con los 5 jobs y release `codex-v0.155.1-1` (383 MB + manifest
`codex-termux/v1`, 1 componente). Instalación real con `install.sh` contra ese release:
`rc=0` con checksum y ELF validados, y en el árbol instalado
`PT_TLS codex-android = ninguno`, `PT_TLS codex-code-mode-host = 0x40`.
`codex/test/host-smoke/run.sh` ⇒ **OK**: `--help` responde desde clap y el arranque
real por `stdio://` llega a `main()` sin intervención del loader. Ese era el defecto
que había que cerrar.

**Sitio de lock que faltaba (`arg0::try_lock_dir`).** `codex-android --version`
imprimía `WARNING: failed to clean up stale arg0 temp dirs: try_lock() not supported`
en cada arranque: el parche de `arg0/src/lib.rs` cubría el guard de path-entry pero
no el janitor. No es inocuo — en `~/.codex/tmp/arg0` hay 6 `codex-arg0*` de sesiones
pasadas que nunca se limpian. Se corrigió devolviendo `Ok(None)` bajo
`target_os = "android"` (el janitor solo borra cuando el lock le prueba que nadie usa
el directorio; tratarlo como adquirido borraría sesiones vivas). Costo aceptado: los
directorios de sesiones pasadas se acumulan, y son symlinks + un `.lock` (~4 KB por
sesión, medido), no copias del binario de 1.43 GB.

**El sandbox restrictivo no funciona en este puerto (hallazgo, no introducido aquí).**
Con `sandbox_mode=read-only` cada comando aborta con
`LandlockSandboxExecutableNotProvided`: la ruta al ejecutable de sandbox la provee
`arg0` solo bajo `cfg!(target_os = "linux")` (`arg0/src/lib.rs:261`) y la clave
`codex_linux_sandbox_exe` no es configurable desde TOML, así que en Android queda
`None`. El `codex-linux-sandbox` empaquetado es un stub diagnóstico (imprime y
`exit 78`) — nunca invoca `proot`, aunque Termux lo tiene instalado
(`$PREFIX/bin/proot`, sin `bwrap`). Historia: el stub es idéntico desde `f47bb02` del
repo viejo, o sea que es una limitación del puerto, no una regresión de la extracción
ni del rebase. Consecuencia práctica: en Termux `codex` solo ejecuta comandos sin
sandbox. Cerrarlo de verdad significa escribir el wrapper sobre `proot` (o extender
`arg0` a `any(linux, android)` y seleccionar el helper), y eso cambia la postura de
aislamiento: queda como decisión del usuario, documentada en
`codex/src/docs/android-termux.md`.

**El harness de lock-regression estaba podado contra este pin** y por eso el fallo
inicial se leyó como regresión de producto. Dos cosas: `approval_policy="untrusted"`
lo rechaza upstream 0.155.1 y mata el app-server (`validate_config`), y el tool call
de juguete usaba `shell_command`/`command`, nombre y esquema viejos — hoy es
`exec_command` con `cmd` (`core/src/tools/handlers/shell_spec.rs:96`). Con eso
arreglado el app-server arranca y el comando del turno se ejecuta. La aserción de
`rules/default.rules` ahora es condicional: solo falla si el app-server pidió
aprobación; sin sandbox no hay denegación que escalar, así que informa `skip` en vez
de pasar fingiendo cobertura. Resultado con el binario instalado: **PASS**
(`ok: no 'lock() not supported'`, `skip: rules/default.rules …`, `ok: approved command
executed -> hola`, `rc=0`).

Refuerzo: `ci/scripts/test-vendored-android-patches.py` pasa de revisar solo el stub
TLS a exigir el marcador en 14 archivos críticos (incluido `arg0/src/lib.rs`) y un
piso de 29 marcadores en el árbol, corre en 1.2 s y está cableado en `contracts`.

## Release `0.155.1-2`: los dos fixes confirmados en dispositivo (2026-10-04)

El release anterior (`codex-v0.155.1-1`) lleva el stub TLS pero **no** el parche del
janitor de `arg0`, así que se publicó `codex-v0.155.1-2` desde `main` con
`7aee792`/`9356ab1` adentro.

- Run de push `37216217817` verde (`contracts`, `rusty-v8`, `build-codex`):
  `TLS: PT_TLS alineado a 64 (…/codex-code-mode-host)` y restore del cache de
  compilador con 1 699 MB calientes.
- Run de publicación `37218638462` verde y **rápido** (8 min):
  `Cache hit for: ci-cache-v2-codex-7ad39a051…-artifact`, o sea que el artefacto
  publicado es el mismo binario que el run de push validó — la identidad del contrato
  se mantiene dentro del repo nuevo (primera corrida: miss; segunda: hit).
- Release `codex-v0.155.1-2`: `manifest.json` con `"schema": "codex-termux/v1"`,
  `release: 0.155.1-2`, 1 componente, asset de 383 321 670 B y sha256
  `a898c96b…`.
- Verificación real en dispositivo contra ese release (`install.sh` + los dos
  harness): `instalador rc=0`, `PT_TLS codex-android = ninguno`,
  `PT_TLS codex-code-mode-host = 0x40`, `arg0-janitor OK (sin avisos de lock)` —
  `codex-android --version` ya no imprime `try_lock() not supported` —,
  `host-smoke rc=0` y `lock-regression PASS rc=0`.

Queda abierto, no resuelto aquí: la decisión de construir el wrapper real de sandbox
sobre `proot`.

**Limpieza de releases defectuosos (decisión del usuario, 2026-10-04).** Se borraron
`codex-v0.155.1` (host sin `PT_TLS` alineado) y `codex-v0.155.1-1` (aviso del lock de
`arg0`), con sus assets. Los tags siguen apuntando a `fc9f366` y `3937e52`, así que
cada release es recreable si hiciera falta. Publicados quedan solo
`codex-v0.155.1-2` (latest) y el espejo `rusty-v8-v150.4.0`.

## Wrapper de sandbox sobre `proot` (2026-10-04)

Se cierra la brecha que `PROGRESS.md` dejaba abierta a propósito. Antes de escribir
código se midió en el dispositivo qué primitives hay: Landlock no (kernel 5.10 y la
syscall 444 muere por seccomp), `unshare(CLONE_NEWUSER|CLONE_NEWNS)` → `EINVAL`, sin
`bwrap`; lo único que existe es `proot` de Termux (`5.1.107.96-0`). Cada afirmación del
wrapper sale de esa medición, no de la documentación de proot:

- `-b /:/:ro` produce `EROFS` en cualquier `openat` con escritura; un `-b <raíz>:<raíz>`
  posterior reabre la escritura y un `-b <sub>:<sub>:ro` la vuelve a cerrar.
- `-b <vacío>:<objetivo>:ro` oculta contenido **e impide crear** el objetivo aunque el
  objetivo todavía no exista — medido, porque es lo que hace falta para que `.git` /
  `.codex` / `.agents` no se puedan fabricar.
- `--net-policy deny` falla `connect` con `EACCES` también en loopback; no afecta a
  `getaddrinfo` ni a `bind` (no hace falta aquí, pero acota qué se está prometiendo).
- proot **rechaza `--`** como separador, así que el comando va suelto al final; códigos
  de salida y argv con espacios llegan intactos.
- `/dev` no es listable en el runtime de Termux: se monta `:ro` y se reabren solo
  `null`, `zero`, `full`, `random`, `urandom`, `tty`. Un `/` read-only sin esa reapertura
  rompe `git` (`could not open '/dev/null'`), que fue el fallo de la primera versión.

Lo que se cableó:

- `codex-rs/android-sandbox`, crate propio que se publica como binario
  `codex-linux-sandbox`. Parsea el contrato real del manager
  (`--sandbox-policy-cwd`, `--command-cwd`, `--permission-profile` JSON,
  `--use-legacy-landlock`, `--allow-network-for-proxy`) con **flag desconocido = error**,
  y traduce el `PermissionProfile` tipado (no JSON a mano) a los binds de arriba.
- `arg0/src/lib.rs`: rama `cfg!(target_os = "android")` que resuelve el wrapper hermana
  al ejecutable y, si no la encuentra, deja `None` para que siga el error de upstream en
  vez de correr sin sandbox.
- Todo lo no expresable falla cerrado con 78 y no ejecuta nada: sin `proot`, perfil con
  allowlist de lecturas, negaciones por glob, red gestionada. Y hay sonda previa: si el
  `proot` del sistema acepta `:ro` pero no lo aplica, o no virtualiza máscaras sobre
  rutas inexistentes, el wrapper aborta en vez de servir un sandbox decorativo.
- Se borró el stub diagnóstico `codex/scripts/codex-linux-sandbox`; `build-codex-android.sh`
  compila `--package codex-android-sandbox`, instala su ELF y le pasa
  `verify_tls_alignment`; `installer.py` y `package-release.py` lo validan como ELF (era
  `bash -n`).
- `ci/scripts/test-vendored-android-patches.py` sube a 16 archivos y 33 marcadores y
  añade la guarda de la cadena de selección (miembro del workspace, rama de `arg0`,
  `--package` en el build, stub ausente).
- `build-codex.yml` corre `cargo test -p codex-android-sandbox` **antes** del build largo,
  bajo la misma condición que el build: tocar `android-sandbox` mueve
  `CODEX_SOURCE_TREE`, así que un cache-hit implica que esos tests ya pasaron con ese
  árbol.

Todavía no verificado, y es el gate para dar esto por bueno: el ELF del crate no corrió
en el dispositivo. El harness nuevo (`codex/test/sandbox-proot/run.sh`, 32 aserciones de
argv, enforcement y fail-closed) pasó entero **contra un shim de shell** que emite el
mismo argv — eso prueba las primitivas de proot y la plomería del harness, no la
traducción perfil→binds, que es justo lo que validan los unit tests en CI. Pasos
pendientes: build verde en CI, `bash codex/test/sandbox-proot/run.sh
$PREFIX/bin/codex-linux-sandbox` con el ELF real, y `SANDBOX_MODE=workspace-write
codex/test/lock-regression/run.sh` para confirmar que un turno real de `codex` pasa por
el wrapper (ese modo además hace alcanzable la escritura de `rules/default.rules`, que
hasta ahora siempre informaba `skip`).

Postura documentada en `README.md` y `codex/src/docs/android-termux.md`: proot es
`ptrace` con UID compartido, así que esto **no** es frontera contra código
deliberadamente malicioso; es frontera real contra escrituras y red accidentales, que
es lo que `sandbox_mode` puede significar en un dispositivo sin root.
