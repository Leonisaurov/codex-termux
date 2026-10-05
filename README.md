# codex-termux

Puerto Android/Termux de la CLI Codex (`codex-rs`), separado del stack
`opencode-termux` para que su pipeline y sus pins evolucionen solos.

- **Fuente**: monorepo `openai/codex` vendeoreado en `codex/src`, pin
  `be2951ea34f0d295ed0becf97079f92fa5f6950e` (`rust-v0.155.1`), declarado en
  `ci/source-manifest.json`. Los parches Android viven dentro de ese árbol
  vendeoreado, como commits; no se aplican en el build.
- **Target**: `aarch64-linux-android`, API 24, NDK `28.1.13356709`.
- **Salidas**: `codex-android`, `codex-code-mode-host`, `codex-linux-sandbox`.
- **Dependencia nativa**: archivo estático Rusty V8 (`v8 150.4.0`,
  `ptrcomp_sandbox`) construido por `.github/workflows/build-rusty-v8-android.yml`,
  que restaura desde el release `rusty-v8-v<ver>` antes de recompilar V8.

## Estructura

```
codex/src            fuente upstream + parches del port (vendeoreada, pin en ci/source-manifest.json)
codex/scripts        build-codex-android.sh
codex/test           harnesses de dispositivo (lock-regression, host-smoke, sandbox-proot)
codex/more           relay ntfy de aprobaciones (TypeScript, corre con Bun)
codex/build          estado generado por CI (ignorado)
codex/artifacts      binarios producidos por CI (ignorados)
ci/scripts           entorno compartido, motor de estado, contratos, instalador
ci/actions           acciones compuestas incremental-cache y zram
.github/workflows    build.yml (orquestador), build-codex.yml, build-rusty-v8-android.yml
releases             ejemplo de manifest
```

## CI

Todo build pesado corre en GitHub Actions; este repositorio no es un runner local.

```sh
gh workflow run build.yml -R Leonisaurov/codex-termux --ref main \
  -f release=0.155.1 \
  -f codex_ref=be2951ea34f0d295ed0becf97079f92fa5f6950e \
  -f v8_version=150.4.0
gh run watch <RUN_ID> -R Leonisaurov/codex-termux --exit-status
```

`build.yml` encadena `contracts` (chequeos estáticos) → `rusty-v8` → `codex` →
`publish`, que publica la release `codex-v<release>` con `manifest.json` y el
`.tar.gz` de los tres binarios. Un push a `main` valida y construye pero no
publica.

Chequeos estáticos, sin runner:

```sh
for t in test-workflow-cache-contracts test-vendored-android-patches test-build-state test-ci-summary test-installer; do
  python3 "ci/scripts/$t.py"
done
```

Cambios en `ci/scripts/cache-contract.py`, `build-state.py`,
`validate-source-tree.py`, `validate-android-bundle.py` o
`ci/actions/incremental-cache/action.yml` invalidan todas las claves de cache:
son `ENGINE_PATHS` del contrato.

El contrato del producto hashea el árbol `codex/src` completo —no solo
`codex-rs`—, así que hasta un comentario en `codex/src/docs` cambia la clave y
fuerza una recompilación desde cero. Para probar un cambio de docs sin pagar
ese coste, hacerlo en `README.md` o en `ci/`.

## Pruebas en dispositivo

```sh
bash codex/test/lock-regression/run.sh "$PREFIX/bin/codex-android"
bash codex/test/host-smoke/run.sh "$PREFIX/bin/codex-code-mode-host"
bash codex/test/sandbox-proot/run.sh "$PREFIX/bin/codex-linux-sandbox"
```

La primera arranca el `app-server` real contra un Responses API de juguete y
verifica los parches de `File::lock` sobre un turno que ejecuta un comando; la
persistencia de `rules/default.rules` solo se asevera si el server pidió una
aprobación, lo que en el modo por defecto no ocurre. Corriéndola con
`SANDBOX_MODE=workspace-write` el turno pasa además por el wrapper de sandbox.
La segunda es lo que ningún chequeo de build puede probar: que el host sobrevive al loader
dinámico de Bionic y llega a `main()` (un `PT_TLS` mal alineado, un símbolo
ausente o un archivo V8 incompatible matan el binario en el dispositivo, no en
el runner). La tercera mide qué bloquea de verdad el sandbox en el dispositivo.

## Sandbox

`sandbox_mode=read-only` y `workspace-write` sí ejecutan comandos aquí: el puerto
aporta `codex-rs/android-sandbox`, un `codex-linux-sandbox` propio que traduce el
`--permission-profile` del manager de Codex a un arranque de `proot` de Termux
(`-b /:/:ro` más reapertura de las raíces escribibles y `--net-policy deny`), y
parchea `arg0` para que lo resuelva en Android. Falla cerrado: sin `proot`, con un
perfil que restrinja lecturas, con negaciones por glob o con red gestionada, sale
con 78 y no ejecuta nada.

No es una frontera contra código deliberadamente malicioso — proot es `ptrace` y
el trazado comparte UID con el trazador —; es una frontera real contra escrituras
y usos accidentales de la red. El `linux-sandbox` nativo (bubblewrap/Landlock)
sigue sin ser un sandbox de Android y no se compila aquí.
`codex/src/docs/android-termux.md` detalla primitivas, límites y harnesses.

## Instalación en Termux

```sh
curl -fsSL https://raw.githubusercontent.com/Leonisaurov/codex-termux/main/install.sh | sh -s -- --yes --smoke-test
```

Instala los tres ejecutables en `$PREFIX/bin` verificando checksum y arquitectura
desde el manifest de la release. `--dry-run` valida sin tocar el prefijo.

## Trabajo en el port

Para cambios en `codex/src`, leer `codex/src/docs/android-termux.md` y
`codex/src/AGENTS.md` antes de tocar `codex-rs`. La fuente se modifica en commits
de este repositorio y el pin de `ci/source-manifest.json` se actualiza junto con
ellos; CI rechaza un checkout sucio o con `.git` anidado.
