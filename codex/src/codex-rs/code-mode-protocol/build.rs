use std::path::PathBuf;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    println!("cargo:rustc-check-cfg=cfg(codex_bazel)");
    println!("cargo:rerun-if-changed=src/grpc");

    let mut config = tonic_prost_build::Config::new();
    // CODEX-TERMUX-ANDROID-PATCH: protoc-bin-vendored 3.2.0 only ships
    // linux/macos/windows binaries and the linux-aarch64 one is glibc (cannot
    // run on Termux/bionic). On Android, skip protoc_executable entirely so
    // prost-build falls back to protoc_from_env(): $PROTOC or `protoc` on PATH
    // (native Termux package `protobuf`). Other platforms keep the vendored
    // protoc so CI (linux) is unaffected.
    #[cfg(not(target_os = "android"))]
    config.protoc_executable(protoc_bin_vendored::protoc_bin_path()?);
    // CODEX-TERMUX-ANDROID-PATCH-END
    let proto_files = glob::glob("src/grpc/*.proto")?.collect::<Result<Vec<_>, _>>()?;

    tonic_prost_build::configure()
        .build_client(/*enable*/ true)
        .build_server(/*enable*/ true)
        .compile_with_config(config, &proto_files, &[PathBuf::from("src/grpc")])?;

    Ok(())
}
