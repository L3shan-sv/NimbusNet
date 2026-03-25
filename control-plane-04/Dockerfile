# NimbusNet Control Plane Dockerfile
# Phase 4

FROM golang:1.22-bookworm AS builder

WORKDIR /src

COPY controlplane/go.mod controlplane/go.sum ./
RUN go mod download

COPY controlplane/ .

ARG VERSION=dev
ARG GIT_COMMIT=unknown

RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build \
    -ldflags="-X main.Version=${VERSION} -X main.GitCommit=${GIT_COMMIT} -w -s" \
    -o /out/nimbusnet-controlplane \
    ./cmd

# ─── Runtime ──────────────────────────────────────────────────────────────────
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

RUN useradd -r -s /bin/false nimbusnet

COPY --from=builder /out/nimbusnet-controlplane /usr/local/bin/nimbusnet-controlplane
COPY configs/controlplane.yaml /etc/nimbusnet/controlplane.yaml

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:9091/metrics || exit 1

USER nimbusnet

EXPOSE 9091

ENTRYPOINT ["/usr/local/bin/nimbusnet-controlplane"]
CMD ["--config", "/etc/nimbusnet/controlplane.yaml"]
