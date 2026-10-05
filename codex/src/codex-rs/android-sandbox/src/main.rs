// CODEX-TERMUX-ANDROID-PATCH: en Android este binario es el wrapper de sandbox sobre
// proot; upstream lo mantiene burbuja+landlock y el puerto necesita otro mecanismo.
fn main() {
    codex_android_sandbox::run_main();
}
