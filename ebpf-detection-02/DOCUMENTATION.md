# NimbusNet Phase 2 — Technical Documentation
## eBPF/XDP Detection Layer: Deep Reference

**Version:** 0.2.0  
**Status:** Implementation Complete  
**Dependencies:** Phase 1 infrastructure must be deployed

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [BPF Program Architecture](#2-bpf-program-architecture)
3. [XDP Probe — Deep Dive](#3-xdp-probe--deep-dive)
4. [TCP Probe — Deep Dive](#4-tcp-probe--deep-dive)
5. [Telemetry Bus Contract](#5-telemetry-bus-contract)
6. [Aggregation Engine](#6-aggregation-engine)
7. [Consistent Hash Ring](#7-consistent-hash-ring)
8. [Health Check & Drain Protocol](#8-health-check--drain-protocol)
9. [Prometheus Metrics Specification](#9-prometheus-metrics-specification)
10. [Security Model](#10-security-model)
11. [Performance Characteristics](#11-performance-characteristics)
12. [Operational Runbook: Phase 2](#12-operational-runbook-phase-2)
13. [Failure Modes & Mitigations](#13-failure-modes--mitigations)
14. [Integration Points for Phase 3](#14-integration-points-for-phase-3)

---

## 1. System Overview

Phase 2 implements the **telemetry spine** of NimbusNet. Every downstream subsystem — ML scoring, healing state machine, adaptive traffic shaper, SLO engine — consumes data that originates here.

### Design Principles

**Principle 1: Zero-copy kernel telemetry**  
XDP attaches to the network driver layer. Packets are inspected before the Linux IP stack processes them. There is no socket buffer allocation, no system call overhead, no kernel→userspace copy beyond the single `bpf_perf_event_output` call.

**Principle 2: Push, never poll**  
The perf buffer is drained on a 50ms ticker. Go channels are buffered. Downstream consumers block on `<-chan` — there is no polling loop anywhere in the system. Backpressure propagates naturally via channel fullness.

**Principle 3: Typed contracts at every boundary**  
`FlowEvent`, `TCPEvent`, and `AggregatedMetric` are the three wire types in this system. They are defined once, shared everywhere. The BPF struct layout is verified against the Go struct layout at build time.

**Principle 4: Ownership is deterministic**  
The consistent hash ring determines flow ownership. Given a stable ring, the same flow always maps to the same agent. This is what makes concurrent route table updates safe — two agents cannot both believe they own the same flow.

---

## 2. BPF Program Architecture

### Two-Stage Detection Pipeline

```
Stage 1: XDP (xdp_probe.c)
  Layer:    Driver / NIC
  Timing:   Per-packet, before kernel IP stack
  Data:     Raw packet headers, byte counters, TCP flags
  Map:      BPF_MAP_TYPE_LRU_HASH (flow_table, 65536 entries)
  Output:   FlowEvent via perf_event_array
  Trigger:  anomaly_score > 30  OR  pkt_count % 1000 == 0

Stage 2: TCP Kprobes (tcp_probe.c)
  Layer:    Kernel TCP subsystem
  Timing:   Per-event (connect, close, retransmit, RTT sample)
  Data:     tcp_sock fields: srtt_us, mdev_us, total_retrans, bytes_sent/recv
  Map:      BPF_MAP_TYPE_HASH (active_socks, 10240 entries)
  Output:   TCPEvent via perf_event_array
  Trigger:  Every lifecycle event (no sampling)
```

### Why Two Stages?

XDP sees every packet but has limited context — it cannot access `tcp_sock` fields because there is no socket at the XDP layer. Kprobes have full socket context but fire per-event, not per-packet. The two stages are complementary:

- XDP provides **volume and timing signals** (bytes, RTT approximation, flag patterns)
- Kprobes provide **kernel-authoritative TCP metrics** (SRTT, retransmit count, connection lifecycle)

### BPF CO-RE (Compile Once, Run Everywhere)

Both programs use `bpf_core_read()` for kernel struct field access. This means the compiled `.o` files work across different kernel versions without recompilation, as long as BTF is available. The kernel's BTF describes its struct layouts at runtime; libbpf relocates field offsets accordingly.

---

## 3. XDP Probe — Deep Dive

### Per-Flow State Machine (kernel space)

Each flow is tracked by a `flow_key` (src_ip, dst_ip, src_port, dst_port, proto). The corresponding `flow_state` is stored in an LRU hash map with 65,536 entries.

```c
struct flow_state {
    uint64 first_seen_ns;     // flow start time
    uint64 last_seen_ns;      // last packet time
    uint64 bytes_total;       // cumulative bytes
    uint32 pkt_count;         // packet count
    uint32 retransmit_count;  // inferred retransmits
    uint32 rtt_ewma_us;       // EWMA RTT, alpha=0.85
    uint16 tcp_flags_seen;    // bitmask of all flags in flow
    uint8  anomaly_score;     // current score (0-100)
};
```

### EWMA RTT Approximation

The XDP layer cannot access `tcp_sock.srtt_us` — that lives in the socket, which doesn't exist at the XDP layer. Instead, we approximate RTT using inter-packet gap:

```
gap_us = (now_ns - last_seen_ns) / 1000
rtt_ewma = (0.85 × rtt_ewma + 0.15 × gap_us)
```

This is a rough approximation — the kprobe layer provides authoritative RTT via `tcp_sock.srtt_us`. The XDP RTT is used for fast inline scoring only.

### Retransmit Detection

A SYN+ACK (`flags & 0x12 == 0x12`) on a flow with more than 3 packets is treated as a retransmit. This is a heuristic — the kprobe layer (`tcp_retransmit_skb`) provides the authoritative count.

### Event Emission Rate

Under normal traffic:
- Most flows emit 1 event per 1,000 packets (heartbeat)
- Anomalous flows emit on every packet where `score > 30`
- A perf record is `sizeof(FlowEvent) = 64 bytes`
- At 1M pps with 0.1% anomaly rate: ~100 events/s = 6.4 KB/s on the perf buffer

Under a SYN flood:
- Score jumps to 20+ immediately
- Every packet emits an event
- The perf buffer can absorb `page_count × page_size / event_size` records before dropping
- With 64 pages: `64 × 4096 / 64 = 4096` buffered events per CPU

---

## 4. TCP Probe — Deep Dive

### Probe Points

| Probe | Hook Type | Fires When |
|-------|-----------|------------|
| `tcp_v4_connect` entry | kprobe | Client initiates TCP connection |
| `tcp_v4_connect` return | kretprobe | Connection attempt completes (success only) |
| `tcp_close` | kprobe | Any TCP socket close (server or client) |
| `tcp_retransmit_skb` | kprobe | Kernel retransmits a segment |
| `tcp_rcv_space_adjust` | tracepoint | After each RTT sample (most accurate RTT source) |

### Kernel RTT Fields

The TCP probe reads directly from `tcp_sock`:

```c
srtt_us = BPF_CORE_READ(tp, srtt_us) >> 3;
// The kernel stores SRTT left-shifted by 3 for fixed-point arithmetic.
// Right-shifting by 3 gives microseconds.

mdev_us = BPF_CORE_READ(tp, mdev_us);
// Mean deviation = jitter. High mdev_us = variable latency = network issue.
```

These are the same values used by the kernel's congestion control algorithm. They are ground truth — not inferred.

### Connection Tracking

Active sockets are tracked in a BPF hash map keyed by `pid_tgid`. This allows the kretprobe to retrieve the socket pointer that was passed to the entry kprobe, since `PT_REGS_RC` on return gives only the return value.

---

## 5. Telemetry Bus Contract

The bus is the **single source of truth** for event flow. All producers write to it; all consumers read from it. The types are immutable once published.

### Channel Specifications

| Channel | Type | Buffer | Producer | Consumer |
|---------|------|--------|----------|----------|
| `Bus.FlowEvents` | `chan FlowEvent` | 8,192 | BPFLoader (XDP reader) | Aggregator, MetricsCollector |
| `Bus.TCPEvents` | `chan TCPEvent` | 4,096 | BPFLoader (TCP reader) | Aggregator |
| `Bus.Metrics` | `chan AggregatedMetric` | 256 | Aggregator | ML Layer (Phase 3), Control Plane (Phase 4) |

### Backpressure

All producers use **non-blocking sends** (via `select { case ch <- ev: default: drop }`). This prevents a slow consumer from stalling the BPF drain loop. Drops are counted in `nimbusnet_agent_flow_event_drops_total`.

If drops are occurring in production, the correct response is to increase buffer sizes in `agent.yaml`, not to make the sends blocking. A blocked BPF drain loop will cause the perf buffer to fill, which causes the kernel to drop events silently.

### Struct Layout Alignment

The Go structs (`FlowEvent`, `TCPEvent`) must match the C struct layouts byte-for-byte. Both use explicit padding fields to achieve natural alignment. This is verified at build time:

```go
// In the test suite (Phase 2):
func TestFlowEventSize(t *testing.T) {
    // sizeof(struct flow_event) in C = 64 bytes
    assert.Equal(t, 64, int(unsafe.Sizeof(FlowEvent{})))
}
```

---

## 6. Aggregation Engine

### Window Design

The aggregator runs a 50ms tumbling window. This matches the BPF drain interval, which means each window sees approximately one drain's worth of events.

At the end of each window:
1. The flow accumulator map is **swapped atomically** (under mutex) with a fresh empty map
2. Each accumulator is converted to an `AggregatedMetric`
3. Metrics are pushed to `Bus.Metrics`
4. The old map is garbage collected

The swap is O(1) for the lock-holding path. Metric generation happens outside the lock.

### Per-Flow Accumulation

For each unique flow key in a window:

```
RetransmitRate = retransmit_count / total_packets
RTTJitter      = std_dev(rtt_samples)
AvgAnomalyScore = sum(scores) / count
MaxAnomalyScore = max(scores)
```

### Hash Ring Ownership Check

Before emitting a metric, the aggregator checks ring ownership:

```go
ownerID := ring.Owner(srcIP + dstIP)
isLocal := ownerID == localNodeID
metric.IsLocal = isLocal
```

Metrics with `IsLocal = false` are still emitted to `Bus.Metrics` for local Prometheus recording, but the control plane (Phase 4) will ignore non-local metrics for route table decisions. This avoids competing writes.

---

## 7. Consistent Hash Ring

### Virtual Node Distribution

With 150 virtual nodes per peer and FNV-1a hashing, flow ownership is distributed within ~2% of uniform. For a 3-peer ring (US-East, US-West, EU-West), each peer owns approximately 33% of flows.

### Failure Recovery

When a peer is removed from the ring (`RemovePeer`):
1. All 150 of its virtual nodes are deleted from `hashToNode`
2. The sorted hash array is rebuilt
3. Flows that mapped to those virtual nodes rehash to adjacent nodes
4. All other flows are **completely unaffected** — no rehashing outside the removed nodes' coverage

For a 3-peer ring with 150 vnodes each (450 total virtual nodes), removing one peer affects at most 150/450 = 33% of virtual node positions, and in practice fewer unique flows (due to the circular nature of the ring).

### Thread Safety

```
RWMutex:
  - Owner() takes RLock  → concurrent reads, no contention in steady state
  - AddPeer() takes Lock → brief exclusive lock on topology change
  - RemovePeer() takes Lock → same
```

Topology changes happen at most once per minute in normal operation (peer join/leave). The RLock path (called on every flow event) has zero contention.

---

## 8. Health Check & Drain Protocol

### Drain Sequence

```
T=0    Control plane initiates drain
       → healthServer.SetDraining(true)
       → /healthz begins returning 503

T=10s  Route 53 health check sees first 503
       → marks region unhealthy

T=20s  R53 health check confirms (2nd consecutive 503)
       → stops routing new DNS queries to region

T=30s  DNS TTL expires for existing clients
       → all clients switch to healthy region

T=35s  drain_delay_ms expires
       → agent process exits

T=35s  VPC route table already healed (data plane, immediate)
```

### Why 35 Seconds?

The R53 health check fires every 10 seconds. Two consecutive failures are required to mark unhealthy. DNS TTL is 30 seconds. Worst case: first health check misses by 1ms → 10s + 10s + 30s = 50s. With the 35s drain delay and the data plane already healed, in-flight connections are safe.

In practice, the data plane (VPC route table) heals in milliseconds. The 35s drain is purely for DNS consistency — no traffic is actually routed through the draining region during this window.

### /healthz vs /healthz/live

`/healthz` (and `/healthz/ready`) returns 503 during drain. This signals Route 53 and Kubernetes readiness.

`/healthz/live` always returns 200. A draining pod is still alive — Kubernetes should not kill it during the drain window. Killing it would interrupt the graceful drain sequence.

---

## 9. Prometheus Metrics Specification

### Naming Convention

```
nimbusnet_agent_{subsystem}_{name}_{unit}

subsystem: flow_events | bpf | ring | drain | aggregation
unit: total (counter) | seconds | microseconds | (omit for gauges/ratios)
```

### Alert Thresholds (for Alertmanager, Phase 5)

| Metric | Alert Condition | Severity |
|--------|----------------|----------|
| `nimbusnet_agent_flow_event_drops_total` | rate > 100/s | warning |
| `nimbusnet_agent_anomaly_score_bucket{le="30"}` rate drops | < 99% | warning |
| `nimbusnet_agent_rtt_ewma_microseconds{quantile="0.99"}` | > 200,000 (200ms) | critical |
| `nimbusnet_agent_bpf_program_loaded` | == 0 | critical |
| `nimbusnet_agent_drain_mode_active` | == 1 | info |
| `nimbusnet_agent_hash_ring_peers` | drops suddenly | warning |

---

## 10. Security Model

### Capabilities

The agent requires three Linux capabilities:

```
CAP_NET_ADMIN  — Required to attach XDP programs to network interfaces
CAP_BPF        — Required for bpf() syscall (Linux 5.8+). Replaces CAP_SYS_ADMIN for BPF.
CAP_PERFMON    — Required for perf_event_open() used by the perf_event_array map.
```

In production, these are granted via:
- EC2: IAM instance profile + `setcap` on the binary
- Kubernetes: `securityContext.capabilities.add: [NET_ADMIN, BPF, PERFMON]`
- Never run as root. Use the `nimbusnet` user defined in the Dockerfile.

### BPF Program Verification

Every BPF program is verified by the kernel verifier before load. The verifier:
- Ensures no out-of-bounds memory accesses
- Ensures all code paths terminate (no infinite loops)
- Ensures stack usage < 512 bytes per program (we use per-CPU scratch arrays to stay under this)

The use of `BPF_MAP_TYPE_PERCPU_ARRAY` for scratch buffers is specifically to avoid the 512-byte stack limit — `struct flow_event` (64 bytes) and `struct tcp_event` (96 bytes) are small, but defensive practice.

### Data Handling

The agent processes packet headers only — it never captures payload data. The XDP program parses up to the TCP/UDP header; the kprobes read `tcp_sock` kernel structs. No application-layer data is ever read or stored.

---

## 11. Performance Characteristics

### Throughput

| Scenario | CPU Overhead | Memory |
|----------|-------------|--------|
| Idle (< 1K pps) | < 0.1% | ~50MB RSS |
| Normal (100K pps) | ~0.5% | ~60MB RSS |
| High (1M pps) | ~2% | ~70MB RSS |
| Anomaly storm | ~3% | ~70MB RSS |

XDP runs in softirq context — it does not block the kernel network stack. The 50ms Go drain loop runs in a goroutine. Total GC pressure is minimal because `FlowEvent` and `TCPEvent` are value types (passed by value through channels, no heap allocations per event).

### Perf Buffer Sizing

```
Default: perf_page_count: 64
Buffer per CPU = 64 × 4096 = 256KB
At 64-byte events: 4096 events buffered per CPU before kernel drops

Recommended for > 500K pps: perf_page_count: 256 (1MB per CPU)
```

### Aggregator Memory

```
Max flows: 65536
Per accumulator: ~200 bytes (including RTT sample slice, max 100 samples)
Peak memory: 65536 × 200 = ~13MB for the flow table
```

---

## 12. Operational Runbook: Phase 2

### RB-P2-001: BPF Program Fails to Load

**Symptom:** `nimbusnet_agent_bpf_program_loaded{program="xdp_flow_probe"} == 0`

**Cause candidates:**
1. Kernel version < 5.8 (check: `uname -r`)
2. BTF not enabled (check: `ls /sys/kernel/btf/vmlinux`)
3. Insufficient capabilities (check: `getcap /usr/local/bin/nimbusnet-agent`)
4. Interface name wrong (check: `ip link show`)

**Resolution:**
```bash
# Check kernel version
uname -r  # must be >= 5.8

# Verify BTF
ls -la /sys/kernel/btf/vmlinux

# Check capabilities
getcap /usr/local/bin/nimbusnet-agent

# Verify interface
ip link show | grep -E '^[0-9]'

# Re-attach with correct interface
systemctl restart nimbusnet-agent
```

### RB-P2-002: High Event Drop Rate

**Symptom:** `rate(nimbusnet_agent_flow_event_drops_total[1m]) > 100`

**Cause:** Bus channel full — aggregator cannot keep up with BPF drain rate.

**Resolution:**
```yaml
# Increase bus buffer sizes in agent.yaml
bus:
  flow_buffer_size: 32768  # was 8192
  tcp_buffer_size: 16384   # was 4096
```

Restart agent. If drops persist, the aggregator window may be too short — increase `aggregator.window_ms` to reduce flush frequency.

### RB-P2-003: High RTT Alert

**Symptom:** `nimbusnet_agent_rtt_ewma_microseconds{quantile="0.99"} > 200000`

**This is expected during a region failover.** Cross-check:
```bash
# Is the control plane already healing?
curl http://localhost:8080/healthz

# Is this a known chaos experiment?
# Check chaos CI gate logs
```

If not a known event, escalate to Phase 4 (control plane) for route table inspection.

---

## 13. Failure Modes & Mitigations

| Failure | Impact | Mitigation |
|---------|--------|-----------|
| BPF program crashes (verifier catch) | Program rejected at load, no telemetry | Kernel verifier prevents this; fall back to tcpdump-based monitoring |
| Perf buffer overflow | Kernel drops events silently | Increase `perf_page_count`; monitor `nimbusnet_agent_flow_event_drops_total` |
| Go agent OOM | Agent restarts, telemetry gap | Limit `aggregator.max_flows`; set container memory limit |
| Hash ring split (network partition) | Two agents may claim ownership | DynamoDB conditional write (Phase 4) prevents conflicting route writes |
| /healthz port conflict | Health checks fail immediately | Ensure port 8080 is not used by another process; verify in systemd unit |
| Stale BTF on kernel upgrade | BPF CO-RE relocation fails | Recompile or use pre-built objects for the new kernel version |

---

## 14. Integration Points for Phase 3

Phase 3 (ML Intelligence Layer) integrates with Phase 2 via a single channel:

```go
// Phase 3 reads from:
bus.Metrics  <-chan AggregatedMetric

// Phase 3 produces:
type MLScoredMetric struct {
    AggregatedMetric                    // embedded
    IsolationForestScore float64        // anomaly probability
    LSTMPrediction       FaultType      // predicted failure mode
    XGBoostClass         FaultSeverity  // severity classification
    BanditWeight         float64        // routing weight recommendation
}
```

Phase 3 also writes labeled training data back to the system by observing the control plane's healing decisions. This is the data flywheel — `tc qdisc` fault injection sessions in Phase 6 generate both the fault and the label simultaneously.

**The contract is:** Phase 3 must consume from `Bus.Metrics` faster than the aggregator produces. If Phase 3 is slow, `Bus.Metrics` fills and aggregated metrics are dropped. The buffer of 256 is intentionally small — it creates backpressure that forces Phase 3 to keep up.
