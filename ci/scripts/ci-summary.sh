#!/usr/bin/env bash
# Common cache/build summary. Source this file from a GitHub Actions job.
set -euo pipefail

: "${CACHE_DEPENDENCIES:=miss}"
: "${CACHE_INTERMEDIATES:=miss}"
: "${BUILD_STATE:=miss}"
: "${COMPILER_CACHE_HITS:=0}"
: "${COMPILER_CACHE_MISSES:=0}"
: "${BUILD_START_SECONDS:=$(date +%s)}"
: "${BUILD_SCENARIO:=unspecified}"
: "${OUTPUT_VALIDATION:=not-run}"

ci_cache_result() {
    local output_name="$1" cache_hit="${2:-false}"
    if [ "$cache_hit" = "true" ]; then
        printf -v "$output_name" '%s' hit
    elif [ "$cache_hit" = "" ]; then
        printf -v "$output_name" '%s' miss
    else
        printf -v "$output_name" '%s' partial
    fi
    export "$output_name"
}

ci_summary() {
    local now duration
    if command -v ccache >/dev/null 2>&1; then
        COMPILER_CACHE_HITS="$(ccache --show-stats 2>/dev/null | awk '/Cache hit.*[0-9]+/ {print $NF; exit}' || true)"
        COMPILER_CACHE_MISSES="$(ccache --show-stats 2>/dev/null | awk '/Cache miss.*[0-9]+/ {print $NF; exit}' || true)"
        : "${COMPILER_CACHE_HITS:=0}"
        : "${COMPILER_CACHE_MISSES:=0}"
    elif command -v sccache >/dev/null 2>&1; then
        COMPILER_CACHE_HITS="$(sccache --show-stats 2>/dev/null | awk '/Cache hits/ {print $NF; exit}' || true)"
        COMPILER_CACHE_MISSES="$(sccache --show-stats 2>/dev/null | awk '/Cache misses/ {print $NF; exit}' || true)"
        : "${COMPILER_CACHE_HITS:=0}"
        : "${COMPILER_CACHE_MISSES:=0}"
    fi
    now="$(date +%s)"
    duration=$((now - BUILD_START_SECONDS))
    export BUILD_DURATION_SECONDS="$duration"
    {
        echo "BUILD_SCENARIO=$BUILD_SCENARIO"
        echo "RUNNER_OS=${RUNNER_OS:-unknown}"
        echo "RUNNER_ARCH=${RUNNER_ARCH:-unknown}"
        echo "CACHE_DEPENDENCIES=$CACHE_DEPENDENCIES"
        echo "CACHE_INTERMEDIATES=$CACHE_INTERMEDIATES"
        echo "BUILD_STATE=$BUILD_STATE"
        echo "OUTPUT_VALIDATION=$OUTPUT_VALIDATION"
        echo "COMPILER_CACHE_HITS=$COMPILER_CACHE_HITS"
        echo "COMPILER_CACHE_MISSES=$COMPILER_CACHE_MISSES"
        echo "BUILD_DURATION_SECONDS=$BUILD_DURATION_SECONDS"
    } | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
}
