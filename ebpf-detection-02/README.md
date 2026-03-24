# NimbusNet — Phase 2: eBPF/XDP Detection Layer

> **The nervous system.** Every byte of telemetry in NimbusNet flows through this layer. XDP intercepts packets before the Linux kernel stack touches them. The Go agent drains the perf buffer every 50ms and fans out typed events to all downstream consumers via Go channels. Nothing polls. Everything is push.

---

## What This Phase Delivers

| Component | Description |
|-----------|-------------|
| `bpf/xdp_probe.c` | XDP program — intercepts all IPv4 TCP/UDP packets at the driver layer, maintains a per-flow hash table in kernel space, computes inline anomaly scores, emits `FlowEvent` records via `perf_event_array` |
| `bpf/tcp_probe.c` | kprobe/tracepoint program — hooks `tcp_v4_connect`, `tcp_close`, `tcp_retransmit_skb`, and RTT sample tracepoints; emits `TCPEvent` records with kernel-authoritative RTT (no inference) |
| `agent/internal/telemetry/bus.go` | Typed Go channel bus — the contract between BPF readers and all consumers. `FlowEvent`, `TCPEvent`, `AggregatedMetric` types mirror BPF structs byte-for-byte |
| `agent/internal/telemetry/aggregator.go` | Windowed aggregator — consumes raw events, computes per-flow `AggregatedMetric` every 50ms, pushes to `Bus.Metrics` for the ML layer and control plane |
| `agent/internal/ring/ring.go` | Consistent hash ring — 150 virtual nodes, FNV-1a, thread-safe. Determines flow ownership. Split-brain impossible by construction |
| `agent/internal/healthz/server.go` | `/healthz` HTTP server — returns 200 normally, 503 when draining. This is the DNS poisoning mechanism: Route 53 stops routing when it sees 503 |
| `agent/internal/metrics/server.go` | Prometheus metrics server on `:9090` — full coverage of BPF drops, RTT distribution, anomaly score histogram, ring state, drain mode |
| `agent/cmd/agent/main.go` | Agent entrypoint — loads config, wires all subsystems, handles graceful drain on SIGTERM |
| `configs/agent.yaml` | Annotated configuration reference |
| `Dockerfile` | Three-stage build: BPF compilation → Go build → minimal runtime |
| `scripts/build.sh` | Local and Docker build script |

---

## Architecture: The Telemetry Pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│                        KERNEL SPACE                             │
│                                                                 │
│  NIC Driver → XDP Hook (xdp_probe.c)                           │
│               ├── Per-flow hash table (BPF_MAP_TYPE_LRU_HASH)  │
│               ├── Inline anomaly score (0-100)                  │
│               └── perf_event_array ──────────────────────┐      │
│                                                          │      │
│  tcp_v4_connect / tcp_close / tcp_retransmit_skb         │      │
│  tracepoint/tcp/tcp_rcv_space_adjust                     │      │
│               └── perf_event_array ──────────────────────┤      │
│                                                          │      │
└──────────────────────────────────────────────────────────┼──────┘
                                                           │ 50ms drain
┌──────────────────────────────────────────────────────────┼──────┐
│                       USER SPACE (Go Agent)              │      │
│                                                          ▼      │
│  BPFLoader.Run()  ──►  Bus.FlowEvents  (chan FlowEvent)         │
│                   ──►  Bus.TCPEvents   (chan TCPEvent)           │
│                                                                 │
│  Aggregator.Run() ◄──  Bus.FlowEvents                           │
│                   ◄──  Bus.TCPEvents                            │
│                   ──►  Bus.Metrics     (chan AggregatedMetric)  │
│                                          │                      │
│                              ┌───────────┴────────────┐        │
│                              ▼                        ▼         │
│                         ML Layer              Control Plane     │
│                       (Phase 3)               (Phase 4)        │
│                                                                 │
│  /healthz  ──► 200 OK  │  503 Draining  (Route 53 signal)      │
│  /metrics  ──► Prometheus scrape                                │
└─────────────────────────────────────────────────────────────────┘
```

---

## The Two-Plane Design

NimbusNet uses two independent healing planes that operate at different speeds:

**Data plane (fast) — VPC route tables**
Updated immediately by the control plane (Phase 4). New packets are rerouted within milliseconds. This is the actual traffic fix.

**DNS plane (slow) — Route 53**
The Go agent's `/healthz` returns **503** when draining. Route 53 health checks poll every 10s and stop routing to the region after seeing 503. DNS TTL is 30s. Total convergence: ~35 seconds.

The drain delay in `agent.yaml` (`drain_delay_ms: 35000`) ensures the agent stays alive long enough for R53 to catch up before the process exits.

---

## XDP Anomaly Scoring

The BPF program computes a lightweight anomaly score (0–100) **in kernel space**, before userspace is involved. This is not the ML model — it's a fast first-pass filter that decides whether to emit an event at all.

| Signal | Score Contribution |
|--------|-------------------|
| Retransmit ratio > 30% | +40 |
| Retransmit ratio 10-30% | +20 |
| RTT EWMA > 200ms | +30 |
| RTT EWMA 100-200ms | +15 |
| SYN without ACK (flood pattern) | +20 |
| RST flag observed | +10 |

Events are only emitted when `score > 30` OR every 1,000 packets (heartbeat). This keeps the perf buffer lean under normal conditions.

The **full ML scoring** (Isolation Forest, LSTM, XGBoost, multi-armed bandit) runs in Phase 3 on `AggregatedMetric` structs from the aggregator.

---

## Consistent Hash Ring

The ring determines which agent is the authoritative owner of each flow. This is what makes split-brain impossible.

```
Flow ownership = ring.Owner(srcIP + dstIP)

When US-East-1 fails:
  - 150 of its virtual nodes rehash to US-West-2 or EU-West-1
  - The remaining 300 virtual nodes are UNTOUCHED
  - Only 37% of flows experience an ownership change
  - No competing route table writes possible
```

The ring is backed by FNV-1a (fast, good distribution, no crypto overhead). Thread-safe via RWMutex — concurrent reads, brief write lock on peer join/leave.

---

## Prerequisites

### Kernel Requirements
- Linux kernel **5.8+** (for `CAP_BPF`, BTF, and ring buffer support)
- BTF (BPF Type Format) enabled: `CONFIG_DEBUG_INFO_BTF=y`
- XDP support on the network interface (generic mode works for dev/test; driver mode required for production throughput)

### Build Requirements
| Tool | Version | Purpose |
|------|---------|---------|
| `clang` | ≥ 12 | BPF compilation |
| `llvm` | ≥ 12 | BPF backend |
| `libbpf` | ≥ 1.0 | BPF CO-RE (Compile Once, Run Everywhere) |
| `go` | ≥ 1.22 | Agent build |
| `docker` | any | Reproducible BPF builds (recommended) |

### Runtime Capabilities
```
CAP_NET_ADMIN   — XDP program attachment
CAP_BPF         — BPF syscall (kernel 5.8+)
CAP_PERFMON     — perf_event_open for perf buffer
```

---

## Quick Start

### 1. Build

```bash
# Docker build (recommended — reproducible kernel headers)
./scripts/build.sh --docker --version v0.2.0

# Local build (requires clang/llvm/libbpf installed)
./scripts/build.sh --version v0.2.0

# Artifacts land in ./dist/
# dist/nimbusnet-agent
# dist/bpf/xdp_probe.o
# dist/bpf/tcp_probe.o
```

### 2. Configure

```bash
cp configs/agent.yaml /etc/nimbusnet/agent.yaml
# Edit: region, node_id, interface, ring.peers
```

### 3. Run

```bash
# Must run with BPF capabilities
sudo setcap cap_net_admin,cap_bpf,cap_perfmon+eip ./dist/nimbusnet-agent

./dist/nimbusnet-agent --config /etc/nimbusnet/agent.yaml
```

### 4. Verify

```bash
# Health check
curl http://localhost:8080/healthz
# {"status":"ok","draining":false,"timestamp":"..."}

# Metrics
curl http://localhost:9090/metrics | grep nimbusnet_agent

# Key metrics to watch:
# nimbusnet_agent_flow_events_total{event_type="anomaly"}
# nimbusnet_agent_anomaly_score_bucket
# nimbusnet_agent_rtt_ewma_microseconds_bucket
# nimbusnet_agent_bpf_program_loaded{program="xdp_flow_probe"}
```

---

## Key Metrics Reference

| Metric | Type | Description |
|--------|------|-------------|
| `nimbusnet_agent_flow_events_total` | Counter | Events from XDP perf buffer, by type |
| `nimbusnet_agent_flow_event_drops_total` | Counter | Events dropped — bus full (increase `flow_buffer_size`) |
| `nimbusnet_agent_anomaly_score` | Histogram | Distribution of BPF inline scores |
| `nimbusnet_agent_rtt_ewma_microseconds` | Histogram | RTT distribution per region |
| `nimbusnet_agent_retransmit_rate` | Histogram | Retransmit fraction per window |
| `nimbusnet_agent_active_flows` | Gauge | Flows in current aggregation window |
| `nimbusnet_agent_hash_ring_virtual_nodes` | Gauge | Virtual nodes in ring (expect: peers × 150) |
| `nimbusnet_agent_locally_owned_flows` | Gauge | Flows this agent owns |
| `nimbusnet_agent_drain_mode_active` | Gauge | 1 = returning 503 on /healthz |
| `nimbusnet_agent_bpf_program_loaded` | Gauge | 1 = program attached and running |

---

## What's Next — Phase 3

Phase 3 is the ML Intelligence Layer. It consumes `AggregatedMetric` from `Bus.Metrics` and runs:

- **Isolation Forest** — unsupervised anomaly detection on feature vectors
- **LSTM** — sequence model for temporal failure patterns
- **XGBoost** — fault type classification
- **Multi-armed bandit** — online routing weight optimisation (updates on every decision)

The data flywheel: `tc qdisc` fault injection in dev generates labeled training data simultaneously with testing the healing system. One operation, two outputs.

---

## File Tree

```
phase-02-ebpf-detection/
├── README.md                          ← you are here
├── DOCUMENTATION.md                   ← deep technical reference
├── Dockerfile                         ← 3-stage: BPF → Go → runtime
├── bpf/
│   ├── xdp_probe.c                   ← XDP packet interceptor
│   └── tcp_probe.c                   ← TCP kprobe/tracepoint suite
├── agent/
│   ├── go.mod
│   ├── cmd/agent/
│   │   └── main.go                   ← entrypoint, config, wiring
│   └── internal/
│       ├── telemetry/
│       │   ├── bus.go                ← typed event bus + BPFLoader
│       │   └── aggregator.go         ← windowed metric aggregation
│       ├── ring/
│       │   └── ring.go               ← consistent hash ring
│       ├── healthz/
│       │   └── server.go             ← /healthz with drain support
│       └── metrics/
│           └── server.go             ← Prometheus metrics
├── configs/
│   └── agent.yaml                    ← annotated config reference
└── scripts/
    └── build.sh                      ← BPF + Go build script
```
