# Progreso actual — 2026-10-04

Repositorio creado al extraer Codex del stack `opencode-termux`. El árbol
`codex/src` es byte-idéntico al del commit `12d2c97` de ese repositorio, y su
pipeline (`build-codex.yml`, `build-rusty-v8-android.yml`) y sus helpers de `ci/`
viajaron sin editar, por lo que el contrato de cache conserva los mismos digests.

## Estado

- Fuente vendeoreada: `be2951ea34f0d295ed0becf97079f92fa5f6950e` (`rust-v0.155.1`).
- Rusty V8 `150.4.0`: el release `rusty-v8-v150.4.0` se espejó desde
  `Leonisaurov/opencode-termux` para que el primer build restaure el archivo
  estático en vez de recompilar V8 (~110 min).
- Las caches de Actions son por repositorio: la primera corrida de cada clave es
  `miss` legítimo, no una regresión.

Nada de lo anterior se declara funcional hasta leer el log de la run y listar los
artifacts y releases publicados.
