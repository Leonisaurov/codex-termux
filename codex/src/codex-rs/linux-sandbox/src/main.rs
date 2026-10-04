/// Note that the cwd, env, and command args are preserved in the ultimate call
/// to `execv`, so the caller is responsible for ensuring those values are
/// correct.
// CODEX-TERMUX-ANDROID-PATCH: en Android el sandbox Linux no aplica; el
// binario se mantiene compilable pero aborta con un mensaje claro.
#[cfg(any(target_os = "linux", target_os = "android"))]
fn main() {
    #[cfg(target_os = "linux")]
    codex_linux_sandbox::run_main();
    #[cfg(target_os = "android")]
    {
        eprintln!("codex-linux-sandbox no soportado en Android");
        std::process::exit(1);
    }
}
