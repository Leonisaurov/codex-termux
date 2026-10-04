// Bionic stubs link args for the code-mode host (V8 embebido).
// El crate v8 (use_custom_libcxx) referencia símbolos que bionic API 24 no
// exporta: __clear_cache (compiler-rt), aligned_alloc, strtof_l, strtod_l.
// Las rutas se inyectan desde los build scripts del port vía env vars para
// no depender de RUSTFLAGS (que reemplazaría el .cargo/config.toml).
fn main() {
    for var in ["CODEX_BIONIC_STUBS_O", "CODEX_CLANG_RT_BUILTINS"] {
        if let Ok(path) = std::env::var(var) {
            if !path.is_empty() {
                println!("cargo:rustc-link-arg={}", path);
            }
        }
        println!("cargo:rerun-if-env-changed={}", var);
    }
}
