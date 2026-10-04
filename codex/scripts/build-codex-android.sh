#!/usr/bin/env bash
# Build the Codex Android CLI and code-mode host through the shared state graph.
# The Codex checkout is intentionally kept isolated under ./codex.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../../ci/scripts/env.sh"

CODEX_SRC="${CODEX_SRC:-$REPO_ROOT/codex/src}"
CODEX_TARGET_DIR="${CODEX_TARGET_DIR:-$WORK_DIR/codex-target}"
CODEX_ARTIFACT_DIR="${CODEX_ARTIFACT_DIR:-$REPO_ROOT/codex/artifacts}"
CODEX_OUT="${CODEX_OUT:-$CODEX_ARTIFACT_DIR/codex-android}"
CODEX_HOST_OUT="${CODEX_HOST_OUT:-$CODEX_ARTIFACT_DIR/codex-code-mode-host}"
CODEX_SANDBOX_OUT="${CODEX_SANDBOX_OUT:-$CODEX_ARTIFACT_DIR/codex-linux-sandbox}"
mkdir -p "$CODEX_ARTIFACT_DIR"

incremental_exec codex \
    --input "$SCRIPT_DIR/build-codex-android.sh" --input "$REPO_ROOT/ci/scripts/env.sh" \
    --input "$CODEX_SRC/codex-rs" \
    --value "ANDROID_API=$ANDROID_API" \
    --value "ANDROID_NDK_VERSION=$ANDROID_NDK_VERSION" \
    --value "CODEX_TARGET_DIR=$CODEX_TARGET_DIR" \
    --value "RUSTY_V8_ARCHIVE=${RUSTY_V8_ARCHIVE:-}" \
    --value "RUSTY_V8_SRC_BINDING_PATH=${RUSTY_V8_SRC_BINDING_PATH:-}" \
    --output "$CODEX_OUT" --output "$CODEX_HOST_OUT" --output "$CODEX_SANDBOX_OUT"

if [ ! -f "$CODEX_SRC/codex-rs/Cargo.toml" ]; then
    echo "ERROR: Codex checkout not found at $CODEX_SRC"
    echo "       The vendored codex/src tree is incomplete."
    exit 1
fi
validate_source_checkout "$CODEX_SRC" "$CODEX_SOURCE_COMMIT" "Codex"

if [ ! -x "$ANDROID_CC" ]; then
    echo "ERROR: Android compiler not found: $ANDROID_CC"
    echo "       Set ANDROID_NDK_HOME to the installed NDK."
    exit 1
fi

# The code-mode host embeds Rusty V8. Requiring its pinned CI artifact here
# prevents Cargo from silently downloading/building a second, incompatible V8.
if [ -z "${RUSTY_V8_ARCHIVE:-}" ] || [ -z "${RUSTY_V8_SRC_BINDING_PATH:-}" ]; then
    echo "ERROR: Codex code-mode host requires RUSTY_V8_ARCHIVE and RUSTY_V8_SRC_BINDING_PATH."
    echo "       Produce the pinned Rusty V8 artifact first (see .github/workflows/build-rusty-v8-android.yml)."
    exit 1
fi

export CARGO_TARGET_DIR="$CODEX_TARGET_DIR"
export CARGO_BUILD_TARGET="$ANDROID_TRIPLE"
export CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER="$ANDROID_CC"
export CC_aarch64_linux_android="$ANDROID_CC"
export CXX_aarch64_linux_android="$ANDROID_CXX"
export AR_aarch64_linux_android="$ANDROID_AR"
export RANLIB_aarch64_linux_android="$ANDROID_RANLIB"
export RUSTY_V8_ARCHIVE RUSTY_V8_SRC_BINDING_PATH

# V8's Android arm64 CPU code calls compiler-rt's __clear_cache. The NDK
# linker does not add that archive when Cargo invokes the target clang, so
# append it through a linker wrapper. Using target-specific RUSTFLAGS would
# replace the source checkout's flags; the wrapper preserves them and appends
# the archive after all objects and libraries.
CLANG_RT_BUILTINS="$(find "${ANDROID_NDK_HOME}/toolchains/llvm/prebuilt/linux-x86_64/lib/clang" \
    -type f -name 'libclang_rt.builtins-aarch64-android.a' -print -quit)"
if [ -z "$CLANG_RT_BUILTINS" ] || [ ! -s "$CLANG_RT_BUILTINS" ]; then
    echo "ERROR: Android compiler runtime not found under ${ANDROID_NDK_HOME}" >&2
    exit 1
fi

# Bionic API 24 lacks three libc entry points that the libc++/libc++abi bundled
# inside the pinned Rusty V8 static archive references unconditionally:
# aligned_alloc (Bionic API 28+) and strtof_l/strtod_l (Bionic API 26+). Because
# V8 is consumed as a prebuilt archive, the final link must supply ABI-compatible
# definitions. Compile a small shim with the Android target compiler and append
# it to every target link through the linker wrapper.
ANDROID_LIBC_SHIMS_C="$CODEX_TARGET_DIR/android-libc-shims.c"
ANDROID_LIBC_SHIMS_O="$CODEX_TARGET_DIR/android-libc-shims.o"
mkdir -p "$CODEX_TARGET_DIR"
cat > "$ANDROID_LIBC_SHIMS_C" <<'CEOF'
#include <stddef.h>

/* Declare only what the shims need so the NDK headers cannot clash with these
   definitions regardless of their availability guards. */
extern int posix_memalign(void **memptr, size_t alignment, size_t size);
extern float strtof(const char *nptr, char **endptr);
extern double strtod(const char *nptr, char **endptr);

/* Bionic exports posix_memalign from API 16 but C11 aligned_alloc only from API 28. */
void *aligned_alloc(size_t alignment, size_t size) {
    void *ptr = NULL;
    if (posix_memalign(&ptr, alignment, size) != 0) {
        return NULL;
    }
    return ptr;
}

/* Bionic exports strtof_l/strtod_l only from API 26. The terminal runs in the C
   locale, so delegating to the non-_l variants preserves behaviour; the locale
   pointer is forwarded as an opaque ABI-compatible argument. */
float strtof_l(const char *nptr, char **endptr, void *locale) {
    (void)locale;
    return strtof(nptr, endptr);
}

double strtod_l(const char *nptr, char **endptr, void *locale) {
    (void)locale;
    return strtod(nptr, endptr);
}
CEOF
"$ANDROID_CC" -c -O2 -fPIC -o "$ANDROID_LIBC_SHIMS_O" "$ANDROID_LIBC_SHIMS_C"

LINKER_WRAPPER="$CODEX_TARGET_DIR/android-linker"
cat > "$LINKER_WRAPPER" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec "$ANDROID_CC" "\$@" "$ANDROID_LIBC_SHIMS_O" "$CLANG_RT_BUILTINS"
EOF
chmod 0755 "$LINKER_WRAPPER"
export CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER="$LINKER_WRAPPER"
echo "Android compiler runtime: $CLANG_RT_BUILTINS"
echo "Android libc shims: $ANDROID_LIBC_SHIMS_O"

cd "$CODEX_SRC/codex-rs"
CORE_MANIFEST="$CODEX_SRC/codex-rs/core/Cargo.toml"
grep -qF '[target.aarch64-linux-android.dependencies]' "$CORE_MANIFEST" || {
    echo "ERROR: Codex source commit is missing its Android dependency configuration" >&2
    exit 1
}

cargo build --locked --release --target "$ANDROID_TRIPLE" \
    --package codex-cli --package codex-code-mode-host

install -m 0755 "$CODEX_TARGET_DIR/$ANDROID_TRIPLE/release/codex" "$CODEX_OUT"
install -m 0755 "$CODEX_TARGET_DIR/$ANDROID_TRIPLE/release/codex-code-mode-host" "$CODEX_HOST_OUT"
install -m 0755 "$SCRIPT_DIR/codex-linux-sandbox" "$CODEX_SANDBOX_OUT"
echo "Codex outputs: $CODEX_OUT and $CODEX_HOST_OUT"
