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
codex/scripts        build-codex-android.sh, stub codex-linux-sandbox
codex/test           pruebas de dispositivo (regresión de locks, relay ntfy)
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
for t in test-workflow-cache-contracts test-build-state test-ci-summary test-installer; do
  python3 "ci/scripts/$t.py"
done
```

Cambios en `ci/scripts/cache-contract.py`, `build-state.py`,
`validate-source-tree.py`, `validate-android-bundle.py` o
`ci/actions/incremental-cache/action.yml` invalidan todas las claves de cache:
son `ENGINE_PATHS` del contrato.

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
