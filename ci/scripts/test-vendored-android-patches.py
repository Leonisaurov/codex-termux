#!/usr/bin/env python3
"""Guard the Android port patches a re-vendor of the vendored tree can erase."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
RS = ROOT / "codex" / "src" / "codex-rs"
MARKER = "CODEX-TERMUX-ANDROID-PATCH"

# Un re-vendor que reemplace cualquiera de estos archivos se lleva el parche Android
# sin dejar rastro, y el fallo aparece en tiempo de ejecución (o en linker64).
REQUIRED = (
    "code-mode-host/src/main.rs",            # stub TLS alineado: sin él el host aborta
    "arg0/src/lib.rs",                        # flock no soportado (guard y janitor)
    "execpolicy/src/amend.rs",
    "core/src/installation_id.rs",
    "core-plugins/src/startup_sync.rs",
    "message-history/src/lib.rs",
    "message-history/src/batch.rs",
    "network-proxy/src/certs.rs",
    "app-server-transport/src/transport/unix_socket.rs",
    "rollout/src/writer_lock.rs",
    "rollout/src/maintenance.rs",
    "rmcp-client/src/oauth/refresh_lock.rs",
    "rmcp-client/src/oauth/store_lock.rs",
    "user-verification/src/lifecycle_lock.rs",
)
MIN_MARKERS = 29


def main() -> None:
    host = (RS / "code-mode-host" / "src" / "main.rs").read_text(encoding="utf-8")
    # Bionic's arm64 loader aborts before main() unless PT_TLS is aligned to 64, and
    # V8 only supplies 8-byte thread_locals, so the link needs this stub. The re-vendor
    # to rust-v0.155.1 replaced the file and shipped a host that could not start.
    for needle in ("core::arch::global_asm!", ".p2align 6", "tls_align_stub:"):
        assert needle in host, f"falta el stub TLS alineado ({needle!r}) en code-mode-host/src/main.rs"

    for rel in REQUIRED:
        path = RS / rel
        assert path.is_file(), f"ausente en el árbol vendeoreado: {rel}"
        assert MARKER in path.read_text(encoding="utf-8"), f"{rel}: falta el parche Android ({MARKER})"

    total = sum(
        path.read_text(encoding="utf-8", errors="ignore").count(MARKER)
        for path in RS.rglob("*.rs")
        # codex-rs/target es el árbol de build (1.9 G) y viaja ignorado: nunca escanearlo.
        if "target" not in path.relative_to(RS).parts
    )
    assert total >= MIN_MARKERS, f"solo {total} marcadores {MARKER}; se perdieron parches del puerto"
    print(f"OK: {len(REQUIRED)} sitios críticos conservan el parche Android ({total} marcadores)")


if __name__ == "__main__":
    main()
