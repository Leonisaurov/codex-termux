#!/bin/bash
# setup-runner.sh — Install all build dependencies on a GitHub Actions runner.
# Must be sourced (source ci/scripts/setup-runner.sh) or run as script.
# Expects env vars: ANDROID_NDK_HOME, WORK_DIR, ZIG_VERSION

set -euo pipefail

# Some product workflows do not need Zig but still source this shared setup.
# Keep the default here so set -u cannot abort those jobs before their build.
: "${ZIG_VERSION:=0.15.2}"
export ZIG_VERSION
: "${BUN_VERSION:=1.2.13}"
export BUN_VERSION
: "${HOST_BUN_VERSION:=1.3.2}"
export HOST_BUN_VERSION

if [ -n "${GITHUB_ACTIONS:-}" ] && [ -z "${TMPDIR:-}" ] && [ -n "${RUNNER_TEMP:-}" ]; then
  TMPDIR="$RUNNER_TEMP"
fi
: "${TMPDIR:=/data/data/com.termux/files/usr/tmp}"
export TMPDIR
mkdir -p "$TMPDIR"
test -d "$TMPDIR" && test -w "$TMPDIR"
RUNNER_TMP_ROOT="$(mktemp -d "$TMPDIR/opencode-termux-runner.XXXXXX")"

# ── Free disk space ────────────────────────────────────────────
echo "=== Freeing disk space ==="
sudo rm -rf /usr/share/dotnet /opt/ghc /usr/local/share/boost /opt/hostedtoolcache 2>/dev/null || true
df -h .

# ── System packages ─────────────────────────────────────────────
echo "=== Installing system packages ==="
sudo apt-get update -qq
sudo apt-get install -y -qq ninja-build python3 ruby perl git curl wget unzip \
  xz-utils zip build-essential ccache pkg-config libxml2-dev libxslt1-dev \
  golang-go autoconf automake libtool util-linux kmod gpg zstd 2>&1 | tail -5

# ── CMake 3.28+ ─────────────────────────────────────────────────
if ! cmake --version 2>/dev/null | grep -q "3\.2[89]\|3\.[3-9]"; then
  echo "=== Installing CMake 3.28.6 ==="
  CMAKE_VER="3.28.6"
  cd "$RUNNER_TMP_ROOT"
  wget -q "https://github.com/Kitware/CMake/releases/download/v${CMAKE_VER}/cmake-${CMAKE_VER}-linux-x86_64.tar.gz" -O cmake.tar.gz
  sudo tar xzf cmake.tar.gz -C /opt/
  sudo ln -sf "/opt/cmake-${CMAKE_VER}-linux-x86_64/bin/cmake" /usr/local/bin/cmake
  sudo ln -sf "/opt/cmake-${CMAKE_VER}-linux-x86_64/bin/ctest" /usr/local/bin/ctest
  sudo ln -sf "/opt/cmake-${CMAKE_VER}-linux-x86_64/bin/cpack" /usr/local/bin/cpack
  rm cmake.tar.gz
fi
echo "CMake: $(cmake --version | head -1)"

# ── Android NDK ─────────────────────────────────────────────────
if [ ! -d "${ANDROID_NDK_HOME}/toolchains/llvm/prebuilt/linux-x86_64" ]; then
  echo "=== Installing Android NDK r28b ==="
  cd "$RUNNER_TMP_ROOT"
  wget -q "https://dl.google.com/android/repository/android-ndk-r28b-linux.zip" -O ndk.zip
  mkdir -p "$ANDROID_NDK_HOME"
  unzip -q ndk.zip -d "$RUNNER_TMP_ROOT/ndk-extract"
  mv "$RUNNER_TMP_ROOT/ndk-extract/android-ndk-r28b"/* "$ANDROID_NDK_HOME/"
  rm -rf "$RUNNER_TMP_ROOT/ndk-extract" "$RUNNER_TMP_ROOT/ndk.zip"

fi

# ── Bionic stubs (dl, pthread, rt, util están en libc.so en Android) ──
echo "=== Creating Bionic library stubs ==="
SYSROOT_LIB="${ANDROID_NDK_HOME}/toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/lib"
for lib in libdl.so libpthread.so librt.so libutil.so; do
  if [ ! -f "${SYSROOT_LIB}/${lib}" ]; then
    echo 'INPUT(-lc)' > "${SYSROOT_LIB}/${lib}"
    echo "  Created stub: ${SYSROOT_LIB}/${lib}"
  fi
done
for lib in libdl.a libpthread.a librt.a libutil.a; do
  if [ ! -f "${SYSROOT_LIB}/${lib}" ]; then
    echo 'INPUT(-lc)' > "${SYSROOT_LIB}/${lib}"
    echo "  Created stub: ${SYSROOT_LIB}/${lib}"
  fi
done
echo "$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/linux-x86_64/bin" >> "$GITHUB_PATH"
echo "NDK: $(ls "$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android"*clang 2>/dev/null | head -1)"
echo "ANDROID_NDK_ROOT=${ANDROID_NDK_HOME}" >> "$GITHUB_ENV"

# Zig 0.15 does not infer an Android libc from the NDK path. Keep this
# configuration in the shared runner setup so Kilo and any future Zig Android
# product use the same Bionic headers and API-24 CRT directory.
NDK_SYSROOT="${ANDROID_NDK_HOME}/toolchains/llvm/prebuilt/linux-x86_64/sysroot"
ZIG_LIBC_FILE="${WORK_DIR}/android-libc.txt"
cat > "$ZIG_LIBC_FILE" <<EOF
# Android NDK bionic libc specification for Zig
# The directory that contains stdlib.h
include_dir=${NDK_SYSROOT}/usr/include
# The system-specific include directory (sys/errno.h etc)
sys_include_dir=${NDK_SYSROOT}/usr/include/aarch64-linux-android
# Android uses crtbegin_static.o and crtend_android.o instead of crt1.o
crt_dir=${NDK_SYSROOT}/usr/lib/aarch64-linux-android/${ANDROID_API:-24}
# Windows-only paths (not needed)
msvc_lib_dir=
kernel32_lib_dir=
gcc_dir=
EOF
test -d "${NDK_SYSROOT}/usr/include"
test -d "${NDK_SYSROOT}/usr/lib/aarch64-linux-android/${ANDROID_API:-24}"
echo "ZIG_LIBC_FILE=${ZIG_LIBC_FILE}" >> "$GITHUB_ENV"

# ── Rust + Android target ───────────────────────────────────────
if ! command -v rustup &>/dev/null; then
  echo "=== Installing Rust ==="
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
  echo "$HOME/.cargo/bin" >> "$GITHUB_PATH"
  export PATH="$HOME/.cargo/bin:$PATH"
fi
rustup target add aarch64-linux-android
echo "Rust: $(rustc --version)"

# ── Zig ─────────────────────────────────────────────────────────
if [ ! -f "${GITHUB_WORKSPACE}/.ci/zig-${ZIG_VERSION}/zig" ]; then
  echo "=== Installing Zig ${ZIG_VERSION} ==="
  mkdir -p "${GITHUB_WORKSPACE}/.ci"
  cd "$RUNNER_TMP_ROOT"
  wget -q "https://ziglang.org/download/${ZIG_VERSION}/zig-x86_64-linux-${ZIG_VERSION}.tar.xz" -O zig.tar.xz
  mkdir -p "${GITHUB_WORKSPACE}/.ci/zig-${ZIG_VERSION}"
  tar xf zig.tar.xz -C "${GITHUB_WORKSPACE}/.ci/zig-${ZIG_VERSION}" --strip-components=1
  rm zig.tar.xz
fi
echo "ZIG_BIN=${GITHUB_WORKSPACE}/.ci/zig-${ZIG_VERSION}/zig" >> "$GITHUB_ENV"
echo "${GITHUB_WORKSPACE}/.ci/zig-${ZIG_VERSION}" >> "$GITHUB_PATH"
echo "Zig: $(${GITHUB_WORKSPACE}/.ci/zig-${ZIG_VERSION}/zig version 2>/dev/null)"

# ── Bun host ────────────────────────────────────────────────────
if [ ! -x "$HOME/.bun/bin/bun" ] || [ "$("$HOME/.bun/bin/bun" --version 2>/dev/null || true)" != "$HOST_BUN_VERSION" ]; then
  echo "=== Installing Bun host v${HOST_BUN_VERSION} ==="
  curl -fsSL https://bun.sh/install | bash -s "bun-v${HOST_BUN_VERSION}"
fi
echo "$HOME/.bun/bin" >> "$GITHUB_PATH"
export PATH="$HOME/.bun/bin:$PATH"
test "$(bun --version 2>/dev/null || true)" = "$HOST_BUN_VERSION"
echo "Bun host: $(bun --version 2>/dev/null || echo 'not found')"

echo "=== Setup complete ==="
df -h .
rm -rf "$RUNNER_TMP_ROOT"
