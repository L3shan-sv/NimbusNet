// SPDX-License-Identifier: GPL-2.0
// NimbusNet eBPF Stage 2 — Flow-level TCP state machine probe
// Attaches as a kprobe/tracepoint to track TCP socket lifecycle events.
// Emits richer per-connection events: connect, close, retransmit, RTT samples.

#include <linux/bpf.h>
#include <linux/ptrace.h>
#include <linux/tcp.h>
#include <linux/in.h>
#include <net/sock.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>

// ─── TCP Lifecycle Event ──────────────────────────────────────────────────────

#define TCP_EVENT_CONNECT     0
#define TCP_EVENT_CLOSE       1
#define TCP_EVENT_RETRANSMIT  2
#define TCP_EVENT_RTT_SAMPLE  3
#define TCP_EVENT_STATE_CHANGE 4

struct tcp_event {
    __u64 timestamp_ns;
    __u32 pid;
    __u32 tid;
    __u32 src_ip;
    __u32 dst_ip;
    __u16 src_port;
    __u16 dst_port;
    __u8  event_type;
    __u8  tcp_state;    // enum tcp_state value
    __u16 pad;

    // RTT fields (valid on TCP_EVENT_RTT_SAMPLE)
    __u32 srtt_us;      // Smoothed RTT from kernel tcp_sock
    __u32 mdev_us;      // Mean deviation (jitter)

    // Retransmit fields (valid on TCP_EVENT_RETRANSMIT)
    __u32 retrans_count;

    // Lifecycle fields
    __u64 bytes_sent;
    __u64 bytes_recv;

    char  comm[16];     // Process name
};

// ─── Maps ─────────────────────────────────────────────────────────────────────

struct {
    __uint(type, BPF_MAP_TYPE_PERF_EVENT_ARRAY);
    __uint(key_size, sizeof(__u32));
    __uint(value_size, sizeof(__u32));
} tcp_events SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct tcp_event);
} tcp_scratch SEC(".maps");

// Track active sockets by PID for correlation
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 10240);
    __type(key, __u64);         // pid_tgid
    __type(value, struct sock*);
} active_socks SEC(".maps");

// ─── Helper: Populate event from sock ─────────────────────────────────────────

static __always_inline void fill_tcp_event(
    struct tcp_event *ev,
    struct sock *sk,
    __u8 event_type
) {
    ev->timestamp_ns = bpf_ktime_get_ns();
    ev->event_type   = event_type;

    // Read addresses safely via BPF CO-RE
    ev->src_ip   = BPF_CORE_READ(sk, __sk_common.skc_rcv_saddr);
    ev->dst_ip   = BPF_CORE_READ(sk, __sk_common.skc_daddr);
    ev->src_port = BPF_CORE_READ(sk, __sk_common.skc_num);
    ev->dst_port = bpf_ntohs(BPF_CORE_READ(sk, __sk_common.skc_dport));
    ev->tcp_state= BPF_CORE_READ(sk, __sk_common.skc_state);

    // TCP-specific metrics
    struct tcp_sock *tp = (struct tcp_sock *)sk;
    ev->srtt_us      = BPF_CORE_READ(tp, srtt_us) >> 3; // kernel stores srtt << 3
    ev->mdev_us      = BPF_CORE_READ(tp, mdev_us);
    ev->retrans_count= BPF_CORE_READ(tp, total_retrans);
    ev->bytes_sent   = BPF_CORE_READ(tp, bytes_sent);
    ev->bytes_recv   = BPF_CORE_READ(tp, bytes_received);

    // Process context
    ev->pid = bpf_get_current_pid_tgid() >> 32;
    ev->tid = bpf_get_current_pid_tgid() & 0xFFFFFFFF;
    bpf_get_current_comm(&ev->comm, sizeof(ev->comm));
}

// ─── Probes ───────────────────────────────────────────────────────────────────

// Fires on tcp_v4_connect entry — record the socket pointer
SEC("kprobe/tcp_v4_connect")
int kprobe_tcp_connect(struct pt_regs *ctx) {
    struct sock *sk = (struct sock *)PT_REGS_PARM1(ctx);
    __u64 pid_tgid  = bpf_get_current_pid_tgid();
    bpf_map_update_elem(&active_socks, &pid_tgid, &sk, BPF_ANY);
    return 0;
}

// Fires on tcp_v4_connect return — emit CONNECT event with result
SEC("kretprobe/tcp_v4_connect")
int kretprobe_tcp_connect(struct pt_regs *ctx) {
    int ret = PT_REGS_RC(ctx);
    if (ret != 0) return 0; // Ignore failed connects

    __u64 pid_tgid = bpf_get_current_pid_tgid();
    struct sock **skp = bpf_map_lookup_elem(&active_socks, &pid_tgid);
    if (!skp) return 0;

    __u32 zero = 0;
    struct tcp_event *ev = bpf_map_lookup_elem(&tcp_scratch, &zero);
    if (!ev) return 0;

    fill_tcp_event(ev, *skp, TCP_EVENT_CONNECT);
    bpf_perf_event_output(ctx, &tcp_events, BPF_F_CURRENT_CPU, ev, sizeof(*ev));
    bpf_map_delete_elem(&active_socks, &pid_tgid);
    return 0;
}

// Fires when a TCP connection is closed
SEC("kprobe/tcp_close")
int kprobe_tcp_close(struct pt_regs *ctx) {
    struct sock *sk = (struct sock *)PT_REGS_PARM1(ctx);
    if (BPF_CORE_READ(sk, __sk_common.skc_family) != AF_INET) return 0;

    __u32 zero = 0;
    struct tcp_event *ev = bpf_map_lookup_elem(&tcp_scratch, &zero);
    if (!ev) return 0;

    fill_tcp_event(ev, sk, TCP_EVENT_CLOSE);
    bpf_perf_event_output(ctx, &tcp_events, BPF_F_CURRENT_CPU, ev, sizeof(*ev));
    return 0;
}

// Fires on retransmit — highest signal for NimbusNet anomaly detection
SEC("kprobe/tcp_retransmit_skb")
int kprobe_tcp_retransmit(struct pt_regs *ctx) {
    struct sock *sk = (struct sock *)PT_REGS_PARM1(ctx);
    if (BPF_CORE_READ(sk, __sk_common.skc_family) != AF_INET) return 0;

    __u32 zero = 0;
    struct tcp_event *ev = bpf_map_lookup_elem(&tcp_scratch, &zero);
    if (!ev) return 0;

    fill_tcp_event(ev, sk, TCP_EVENT_RETRANSMIT);
    bpf_perf_event_output(ctx, &tcp_events, BPF_F_CURRENT_CPU, ev, sizeof(*ev));
    return 0;
}

// Fires after RTT sample — provides ground truth RTT without inference
SEC("tracepoint/tcp/tcp_rcv_space_adjust")
int tracepoint_rtt_sample(struct pt_regs *ctx) {
    struct sock *sk = (struct sock *)PT_REGS_PARM1(ctx);
    if (!sk) return 0;
    if (BPF_CORE_READ(sk, __sk_common.skc_family) != AF_INET) return 0;

    __u32 zero = 0;
    struct tcp_event *ev = bpf_map_lookup_elem(&tcp_scratch, &zero);
    if (!ev) return 0;

    fill_tcp_event(ev, sk, TCP_EVENT_RTT_SAMPLE);
    bpf_perf_event_output(ctx, &tcp_events, BPF_F_CURRENT_CPU, ev, sizeof(*ev));
    return 0;
}

char _license[] SEC("license") = "GPL";
