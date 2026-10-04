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
