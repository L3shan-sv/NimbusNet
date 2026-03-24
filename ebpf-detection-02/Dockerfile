# ─── Stage 1: BPF Compilation ─────────────────────────────────────────────────
# Compiles the C BPF programs into ELF object files.
# Requires clang/llvm and kernel headers.
FROM ubuntu:22.04 AS bpf-builder

RUN apt-get update && apt-get install -y \
    clang \
    llvm \
    libbpf-dev \
    linux-headers-generic \
    linux-libc-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src/bpf

COPY bpf/ .

# Compile XDP program
RUN clang -O2 -g -Wall -target bpf \
    -D__TARGET_ARCH_x86 \
    -I/usr/include/x86_64-linux-gnu \
    -I/usr/include \
    -c xdp_probe.c \
    -o xdp_probe.o

# Compile TCP kprobe program
RUN clang -O2 -g -Wall -target bpf \
    -D__TARGET_ARCH_x86 \
    -I/usr/include/x86_64-linux-gnu \
    -I/usr/include \
    -c tcp_probe.c \
    -o tcp_probe.o

# ─── Stage 2: Go Build ────────────────────────────────────────────────────────
FROM golang:1.22-bookworm AS go-builder

WORKDIR /src/agent

# Cache dependencies first
COPY agent/go.mod agent/go.sum ./
RUN go mod download

COPY agent/ .

# Build with version injection
ARG VERSION=dev
ARG GIT_COMMIT=unknown

RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build \
    -ldflags="-X main.Version=${VERSION} -X main.GitCommit=${GIT_COMMIT} -w -s" \
    -o /out/nimbusnet-agent \
    ./cmd/agent

# ─── Stage 3: Runtime ─────────────────────────────────────────────────────────
# Minimal runtime image. The agent requires:
#   - CAP_NET_ADMIN   (XDP attach)
#   - CAP_SYS_ADMIN   (BPF syscall)
#   - CAP_BPF         (kernel 5.8+, preferred over SYS_ADMIN)
#   - CAP_PERFMON     (perf_event_open)
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y \
    libbpf1 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user (BPF capabilities are granted explicitly, not via root)
RUN useradd -r -s /bin/false nimbusnet

# BPF object files
COPY --from=bpf-builder /src/bpf/xdp_probe.o /opt/nimbusnet/bpf/xdp_probe.o
COPY --from=bpf-builder /src/bpf/tcp_probe.o  /opt/nimbusnet/bpf/tcp_probe.o

# Agent binary
COPY --from=go-builder /out/nimbusnet-agent /usr/local/bin/nimbusnet-agent

# Default config (overridden by mounted volume or SSM in production)
COPY configs/agent.yaml /etc/nimbusnet/agent.yaml

# Health check — mirrors what Route 53 calls
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/healthz || exit 1

USER nimbusnet

EXPOSE 8080 9090

ENTRYPOINT ["/usr/local/bin/nimbusnet-agent"]
CMD ["--config", "/etc/nimbusnet/agent.yaml"]
