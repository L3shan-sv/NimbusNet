#!/usr/bin/env bash
# NimbusNet Phase 2 — Build Script
# Compiles BPF programs and the Go agent binary.
# Usage: ./scripts/build.sh [--docker] [--version v1.0.0]

set -euo pipefail

# ── Defaults ─────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
BPF_DIR="$ROOT_DIR/bpf"
AGENT_DIR="$ROOT_DIR/agent"
OUTPUT_DIR="$ROOT_DIR/dist"

VERSION="${VERSION:-dev}"
GIT_COMMIT="${GIT_COMMIT:-$(git rev-parse --short HEAD 2>/dev/null || echo 'unknown')}"
USE_DOCKER=false

# ── Parse Args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --docker) USE_DOCKER=true; shift ;;
        --version) VERSION="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

mkdir -p "$OUTPUT_DIR/bpf"

# ─────────────────────────────────────────────────────────────────────────────
# Docker build (recommended for CI — reproducible kernel headers)
# ─────────────────────────────────────────────────────────────────────────────
if $USE_DOCKER; then
    echo "▶ Building via Docker (reproducible)..."
    docker build \
        --build-arg VERSION="$VERSION" \
        --build-arg GIT_COMMIT="$GIT_COMMIT" \
        --target go-builder \
        -t nimbusnet-agent-builder:latest \
        -f "$ROOT_DIR/Dockerfile" \
        "$ROOT_DIR"

    # Extract binaries from builder stage
    docker create --name nimbusnet-extract nimbusnet-agent-builder:latest
    docker cp nimbusnet-extract:/out/nimbusnet-agent "$OUTPUT_DIR/nimbusnet-agent"
    docker rm nimbusnet-extract

    echo "✓ Binary: $OUTPUT_DIR/nimbusnet-agent"
    exit 0
fi

# ─────────────────────────────────────────────────────────────────────────────
# Local build
# ─────────────────────────────────────────────────────────────────────────────

echo "▶ Checking build dependencies..."

# Check clang
if ! command -v clang &>/dev/null; then
    echo "✗ clang not found. Install: apt-get install clang llvm libbpf-dev"
    exit 1
fi

CLANG_VERSION=$(clang --version | head -1 | awk '{print $3}' | cut -d. -f1)
if [[ "$CLANG_VERSION" -lt 12 ]]; then
    echo "✗ clang >= 12 required (found $CLANG_VERSION)"
    exit 1
fi

# Check Go
if ! command -v go &>/dev/null; then
    echo "✗ go not found"
    exit 1
fi

echo "  clang: $(clang --version | head -1)"
echo "  go:    $(go version)"

# ─────────────────────────────────────────────────────────────────────────────
# Compile BPF programs
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "▶ Compiling BPF programs..."

ARCH=$(uname -m)
case $ARCH in
    x86_64)  TARGET_ARCH="x86" ;;
    aarch64) TARGET_ARCH="arm64" ;;
    *)       echo "✗ Unsupported arch: $ARCH"; exit 1 ;;
esac

# Locate kernel headers
KERNEL_VERSION=$(uname -r)
KERNEL_HEADERS="/usr/src/linux-headers-$KERNEL_VERSION"
if [[ ! -d "$KERNEL_HEADERS" ]]; then
    KERNEL_HEADERS="/usr/include"
fi

BPF_FLAGS=(
    -O2
    -g
    -Wall
    -Werror
    -target bpf
    "-D__TARGET_ARCH_$TARGET_ARCH"
    "-I$KERNEL_HEADERS/include"
    "-I/usr/include/$(gcc -dumpmachine 2>/dev/null || echo x86_64-linux-gnu)"
    -I/usr/include
)

for prog in xdp_probe tcp_probe; do
    echo "  Compiling $prog.c..."
    clang "${BPF_FLAGS[@]}" \
        -c "$BPF_DIR/$prog.c" \
        -o "$OUTPUT_DIR/bpf/$prog.o"
    echo "  ✓ $OUTPUT_DIR/bpf/$prog.o ($(du -h "$OUTPUT_DIR/bpf/$prog.o" | cut -f1))"
done

# ─────────────────────────────────────────────────────────────────────────────
# Build Go agent
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "▶ Building Go agent..."

cd "$AGENT_DIR"
go mod tidy

CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build \
    -ldflags="-X main.Version=$VERSION -X main.GitCommit=$GIT_COMMIT -w -s" \
    -o "$OUTPUT_DIR/nimbusnet-agent" \
    ./cmd/agent

echo "  ✓ $OUTPUT_DIR/nimbusnet-agent ($(du -h "$OUTPUT_DIR/nimbusnet-agent" | cut -f1))"

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════"
echo "  NimbusNet Phase 2 Build Complete"
echo "  Version:    $VERSION"
echo "  Git commit: $GIT_COMMIT"
echo ""
echo "  Artifacts:"
ls -lh "$OUTPUT_DIR"/ "$OUTPUT_DIR/bpf/" 2>/dev/null | grep -v '^total' || true
echo "═══════════════════════════════════════════════════════"
