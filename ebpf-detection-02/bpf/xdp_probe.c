// SPDX-License-Identifier: GPL-2.0
// NimbusNet XDP Probe — Stage 1 kernel bypass detection
// Intercepts packets before the Linux network stack.
// Emits flow_event_t records into a perf_event_array consumed by the Go agent.

#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/tcp.h>
#include <linux/udp.h>
#include <linux/in.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>

// ─── Constants ────────────────────────────────────────────────────────────────

#define MAX_FLOWS       65536
#define FLOW_TIMEOUT_NS 5000000000ULL  // 5 seconds in nanoseconds
#define RTT_ALPHA       85             // EWMA alpha = 0.85 (fixed-point, /100)
#define MAX_RETRIES     10

// ─── Data Structures ──────────────────────────────────────────────────────────

// Key for the per-flow BPF hash map
struct flow_key {
    __u32 src_ip;
    __u32 dst_ip;
    __u16 src_port;
    __u16 dst_port;
    __u8  proto;
    __u8  pad[3];
};

// Per-flow state stored in kernel space
struct flow_state {
    __u64 first_seen_ns;
    __u64 last_seen_ns;
    __u64 bytes_total;
    __u32 pkt_count;
    __u32 retransmit_count;
    __u32 rtt_ewma_us;       // EWMA RTT in microseconds (fixed-point)
    __u16 tcp_flags_seen;    // bitmask of all TCP flags observed in this flow
    __u8  anomaly_score;     // 0-100, computed inline
    __u8  pad;
};

// Event emitted to userspace via perf_event_array
// This is the typed contract between kernel and Go agent.
struct flow_event {
    // Flow identity
    __u32 src_ip;
    __u32 dst_ip;
    __u16 src_port;
    __u16 dst_port;
    __u8  proto;

    // Timing
    __u64 timestamp_ns;
    __u64 flow_duration_ns;

    // Volume
    __u64 bytes_total;
    __u32 pkt_count;

    // TCP signals
    __u32 retransmit_count;
    __u32 rtt_ewma_us;
    __u16 tcp_flags_seen;

    // Derived signal
    __u8  anomaly_score;     // 0=clean, 100=critical
    __u8  event_type;        // 0=periodic, 1=anomaly, 2=flow_end

    __u8  pad[4];
};

// ─── BPF Maps ─────────────────────────────────────────────────────────────────

// Per-flow state table
struct {
    __uint(type, BPF_MAP_TYPE_LRU_HASH);
    __uint(max_entries, MAX_FLOWS);
    __type(key, struct flow_key);
    __type(value, struct flow_state);
} flow_table SEC(".maps");

// Perf event ring — Go agent reads from this
struct {
    __uint(type, BPF_MAP_TYPE_PERF_EVENT_ARRAY);
    __uint(key_size, sizeof(__u32));
    __uint(value_size, sizeof(__u32));
} events SEC(".maps");

// Per-CPU scratch buffer — avoids stack overflow for large structs
struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct flow_event);
} scratch SEC(".maps");

// ─── Anomaly Scoring ──────────────────────────────────────────────────────────

// Inline anomaly score: purely signal-based, no ML at this layer.
// ML models run in Go agent userspace on aggregated metrics.
static __always_inline __u8 compute_anomaly_score(
    struct flow_state *s,
    __u16 tcp_flags,
    __u32 pkt_size
) {
    __u8 score = 0;

    // High retransmit ratio
    if (s->pkt_count > 0) {
        __u32 retry_pct = (s->retransmit_count * 100) / s->pkt_count;
        if (retry_pct > 30) score += 40;
        else if (retry_pct > 10) score += 20;
    }

    // RTT spike: >200ms is suspicious for inter-region
    if (s->rtt_ewma_us > 200000) score += 30;
    else if (s->rtt_ewma_us > 100000) score += 15;

    // SYN flood detection: many SYNs without ACKs
    if ((tcp_flags & 0x02) && !(tcp_flags & 0x10)) score += 20;

    // RST storm
    if (tcp_flags & 0x04) score += 10;

    return score > 100 ? 100 : score;
}

// ─── XDP Main Program ─────────────────────────────────────────────────────────

SEC("xdp")
int xdp_flow_probe(struct xdp_md *ctx) {
    void *data     = (void *)(long)ctx->data;
    void *data_end = (void *)(long)ctx->data_end;

    // ── Parse Ethernet ──
    struct ethhdr *eth = data;
    if ((void *)(eth + 1) > data_end) return XDP_PASS;
    if (bpf_ntohs(eth->h_proto) != ETH_P_IP) return XDP_PASS;

    // ── Parse IPv4 ──
    struct iphdr *ip = (void *)(eth + 1);
    if ((void *)(ip + 1) > data_end) return XDP_PASS;

    struct flow_key key = {};
    key.src_ip = ip->saddr;
    key.dst_ip = ip->daddr;
    key.proto  = ip->protocol;

    __u16 tcp_flags = 0;
    __u32 pkt_size  = data_end - data;

    // ── Parse Transport Layer ──
    if (ip->protocol == IPPROTO_TCP) {
        struct tcphdr *tcp = (void *)(ip + 1);
        if ((void *)(tcp + 1) > data_end) return XDP_PASS;
        key.src_port = bpf_ntohs(tcp->source);
        key.dst_port = bpf_ntohs(tcp->dest);
        // Reconstruct flags from individual bits
        tcp_flags = (tcp->fin)  |
                    (tcp->syn  << 1) |
                    (tcp->rst  << 2) |
                    (tcp->psh  << 3) |
                    (tcp->ack  << 4) |
                    (tcp->urg  << 5);
    } else if (ip->protocol == IPPROTO_UDP) {
        struct udphdr *udp = (void *)(ip + 1);
        if ((void *)(udp + 1) > data_end) return XDP_PASS;
        key.src_port = bpf_ntohs(udp->source);
        key.dst_port = bpf_ntohs(udp->dest);
    } else {
        return XDP_PASS;
    }

    __u64 now_ns = bpf_ktime_get_ns();

    // ── Update Flow State ──
    struct flow_state *s = bpf_map_lookup_elem(&flow_table, &key);
    if (!s) {
        struct flow_state new_state = {
            .first_seen_ns    = now_ns,
            .last_seen_ns     = now_ns,
            .bytes_total      = pkt_size,
            .pkt_count        = 1,
            .retransmit_count = 0,
            .rtt_ewma_us      = 0,
            .tcp_flags_seen   = tcp_flags,
            .anomaly_score    = 0,
        };
        bpf_map_update_elem(&flow_table, &key, &new_state, BPF_NOEXIST);
        return XDP_PASS;
    }

    // Update existing flow
    s->bytes_total    += pkt_size;
    s->pkt_count      += 1;
    s->tcp_flags_seen |= tcp_flags;

    // Detect retransmit: SYN+ACK on established flow
    if ((tcp_flags & 0x12) == 0x12 && s->pkt_count > 3) {
        s->retransmit_count++;
    }

    // EWMA RTT approximation from inter-packet gap
    __u64 gap_us = (now_ns - s->last_seen_ns) / 1000;
    if (s->rtt_ewma_us == 0) {
        s->rtt_ewma_us = (__u32)gap_us;
    } else {
        s->rtt_ewma_us = (RTT_ALPHA * s->rtt_ewma_us + (100 - RTT_ALPHA) * (__u32)gap_us) / 100;
    }
    s->last_seen_ns = now_ns;

    // ── Compute Anomaly Score ──
    s->anomaly_score = compute_anomaly_score(s, tcp_flags, pkt_size);

    // ── Emit Event ──
    // Only emit on anomaly or every 1000 packets (periodic heartbeat)
    __u8 should_emit = (s->anomaly_score > 30) || (s->pkt_count % 1000 == 0);
    if (!should_emit) return XDP_PASS;

    // Use per-CPU scratch to avoid stack limit
    __u32 zero = 0;
    struct flow_event *ev = bpf_map_lookup_elem(&scratch, &zero);
    if (!ev) return XDP_PASS;

    ev->src_ip          = key.src_ip;
    ev->dst_ip          = key.dst_ip;
    ev->src_port        = key.src_port;
    ev->dst_port        = key.dst_port;
    ev->proto           = key.proto;
    ev->timestamp_ns    = now_ns;
    ev->flow_duration_ns= now_ns - s->first_seen_ns;
    ev->bytes_total     = s->bytes_total;
    ev->pkt_count       = s->pkt_count;
    ev->retransmit_count= s->retransmit_count;
    ev->rtt_ewma_us     = s->rtt_ewma_us;
    ev->tcp_flags_seen  = s->tcp_flags_seen;
    ev->anomaly_score   = s->anomaly_score;
    ev->event_type      = s->anomaly_score > 30 ? 1 : 0;

    bpf_perf_event_output(ctx, &events, BPF_F_CURRENT_CPU, ev, sizeof(*ev));

    return XDP_PASS;
}

char _license[] SEC("license") = "GPL";
