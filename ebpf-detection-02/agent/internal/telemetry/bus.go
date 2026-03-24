// Package telemetry defines the typed event bus that connects the BPF perf
// buffer readers to all downstream consumers (aggregator, ML scorer, control plane).
//
// Architecture:
//
//	BPF perf_event_array
//	      │
//	      ▼  (every 50ms drain)
//	  BPFLoader.Run()
//	      │
//	      ├──► Bus.FlowEvents   chan FlowEvent       (XDP events)
//	      └──► Bus.TCPEvents    chan TCPEvent         (kprobe events)
//	                │
//	         ┌──────┴──────┐
//	         ▼             ▼
//	    Aggregator    MetricsCollector
//	         │
//	         ▼
//	  AggregatedMetric  (per-window summaries pushed to ML + control plane)

package telemetry

import (
	"context"
	"encoding/binary"
	"fmt"
	"net"
	"time"
	"unsafe"

	"github.com/cilium/ebpf"
	"github.com/cilium/ebpf/link"
	"github.com/cilium/ebpf/perf"
	"go.uber.org/zap"
)

// ─── Wire Types (must mirror BPF structs byte-for-byte) ───────────────────────

// FlowEvent mirrors struct flow_event in xdp_probe.c
// Field layout must exactly match the C struct (no padding surprises).
type FlowEvent struct {
	SrcIP           uint32
	DstIP           uint32
	SrcPort         uint16
	DstPort         uint16
	Proto           uint8
	_               [3]byte // padding
	TimestampNs     uint64
	FlowDurationNs  uint64
	BytesTotal      uint64
	PktCount        uint32
	RetransmitCount uint32
	RTTEwmaUs       uint32
	TCPFlagsSeen    uint16
	AnomalyScore    uint8
	EventType       uint8
	_               [4]byte // pad
}

// TCPEvent mirrors struct tcp_event in tcp_probe.c
type TCPEvent struct {
	TimestampNs  uint64
	PID          uint32
	TID          uint32
	SrcIP        uint32
	DstIP        uint32
	SrcPort      uint16
	DstPort      uint16
	EventType    uint8
	TCPState     uint8
	_            [2]byte
	SRTTUs       uint32
	MdevUs       uint32
	RetransCount uint32
	_            [4]byte
	BytesSent    uint64
	BytesRecv    uint64
	Comm         [16]byte
}

// SrcIPString returns the source IP as a dotted-decimal string.
func (e *FlowEvent) SrcIPString() string {
	return ipToString(e.SrcIP)
}

// DstIPString returns the destination IP as a dotted-decimal string.
func (e *FlowEvent) DstIPString() string {
	return ipToString(e.DstIP)
}

func ipToString(ip uint32) string {
	b := make([]byte, 4)
	binary.BigEndian.PutUint32(b, ip)
	return net.IP(b).String()
}

// AggregatedMetric is the per-window summary pushed to the ML layer and control plane.
// One metric per unique flow key per aggregation window.
type AggregatedMetric struct {
	// Window
	WindowStart time.Time
	WindowEnd   time.Time

	// Flow identity
	SrcIP   string
	DstIP   string
	Region  string // inferred from IP range / ASN

	// Volume
	TotalBytes   uint64
	TotalPackets uint64

	// Reliability signals
	RetransmitRate float64 // retransmits / total packets
	RTTEwmaUs      uint32
	RTTJitterUs    uint32

	// Anomaly
	MaxAnomalyScore uint8
	AvgAnomalyScore float64

	// TCP lifecycle
	ConnectsObserved uint32
	ClosesObserved   uint32
	RetransObserved  uint32

	// Control plane metadata
	OwnerNodeID string // which agent owns this flow (from hash ring)
	IsLocal     bool   // does this agent own it?
}

// ─── Bus ──────────────────────────────────────────────────────────────────────

// BusConfig controls channel buffer sizes.
type BusConfig struct {
	FlowBufferSize int
	TCPBufferSize  int
}

// Bus is the central typed event bus. BPF readers push; consumers pull.
// All channels are non-blocking on the producer side — drops are counted.
type Bus struct {
	FlowEvents chan FlowEvent
	TCPEvents  chan TCPEvent
	Metrics    chan AggregatedMetric

	dropCount uint64
}

// NewBus constructs a Bus with buffered channels.
func NewBus(cfg BusConfig) *Bus {
	if cfg.FlowBufferSize == 0 {
		cfg.FlowBufferSize = 8192
	}
	if cfg.TCPBufferSize == 0 {
		cfg.TCPBufferSize = 4096
	}
	return &Bus{
		FlowEvents: make(chan FlowEvent, cfg.FlowBufferSize),
		TCPEvents:  make(chan TCPEvent, cfg.TCPBufferSize),
		Metrics:    make(chan AggregatedMetric, 256),
	}
}

// TryPushFlow pushes a FlowEvent non-blocking. Increments drop counter on full.
func (b *Bus) TryPushFlow(ev FlowEvent) bool {
	select {
	case b.FlowEvents <- ev:
		return true
	default:
		b.dropCount++
		return false
	}
}

// TryPushTCP pushes a TCPEvent non-blocking.
func (b *Bus) TryPushTCP(ev TCPEvent) bool {
	select {
	case b.TCPEvents <- ev:
		return true
	default:
		b.dropCount++
		return false
	}
}

// ─── BPF Loader ───────────────────────────────────────────────────────────────

// BPFConfig holds paths and tuning for the BPF programs.
type BPFConfig struct {
	Interface       string
	XDPProgramPath  string
	TCPProgramPath  string
	PerfPageCount   int
	DrainIntervalMs int
}

// BPFLoader loads, attaches, and runs the BPF programs.
// It owns the perf buffer readers and pushes events into the Bus.
type BPFLoader struct {
	cfg    BPFConfig
	bus    *Bus
	log    *zap.Logger

	xdpProg  *ebpf.Program
	tcpProg  *ebpf.Program
	xdpLink  link.Link

	flowReader *perf.Reader
	tcpReader  *perf.Reader
}

// NewBPFLoader creates a BPFLoader. Call Load() before Run().
func NewBPFLoader(cfg BPFConfig, bus *Bus, log *zap.Logger) (*BPFLoader, error) {
	if cfg.PerfPageCount == 0 {
		cfg.PerfPageCount = 64
	}
	if cfg.DrainIntervalMs == 0 {
		cfg.DrainIntervalMs = 50
	}
	return &BPFLoader{cfg: cfg, bus: bus, log: log}, nil
}

// Load compiles (if needed) and attaches the BPF programs.
func (l *BPFLoader) Load() error {
	// In production this uses cilium/ebpf's generated loader from bpf2go.
	// Here we demonstrate the manual approach for clarity.

	xdpSpec, err := ebpf.LoadCollectionSpec(l.cfg.XDPProgramPath)
	if err != nil {
		return fmt.Errorf("load XDP spec: %w", err)
	}

	xdpColl, err := ebpf.NewCollection(xdpSpec)
	if err != nil {
		return fmt.Errorf("create XDP collection: %w", err)
	}

	l.xdpProg = xdpColl.Programs["xdp_flow_probe"]
	if l.xdpProg == nil {
		return fmt.Errorf("xdp_flow_probe program not found in object")
	}

	// Attach XDP to the network interface
	iface, err := net.InterfaceByName(l.cfg.Interface)
	if err != nil {
		return fmt.Errorf("interface %q not found: %w", l.cfg.Interface, err)
	}

	l.xdpLink, err = link.AttachXDP(link.XDPOptions{
		Program:   l.xdpProg,
		Interface: iface.Index,
		Flags:     link.XDPGenericMode, // use XDPDriverMode in production
	})
	if err != nil {
		return fmt.Errorf("attach XDP: %w", err)
	}

	// Open perf reader for flow events
	eventsMap := xdpColl.Maps["events"]
	if eventsMap == nil {
		return fmt.Errorf("events map not found")
	}
	l.flowReader, err = perf.NewReader(eventsMap, l.cfg.PerfPageCount*os.Getpagesize())
	if err != nil {
		return fmt.Errorf("open flow perf reader: %w", err)
	}

	l.log.Info("XDP program attached",
		zap.String("interface", l.cfg.Interface),
		zap.Int("index", iface.Index),
	)
	return nil
}

// Run starts the perf buffer drain loop. Blocks until ctx is cancelled.
func (l *BPFLoader) Run(ctx context.Context) error {
	ticker := time.NewTicker(time.Duration(l.cfg.DrainIntervalMs) * time.Millisecond)
	defer ticker.Stop()

	eventSize := unsafe.Sizeof(FlowEvent{})

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			// Drain all available records from the perf ring
			for {
				record, err := l.flowReader.Read()
				if err != nil {
					if err == perf.ErrClosed {
						return nil
					}
					// No more records available — break inner loop
					break
				}

				if uint64(len(record.RawSample)) < uint64(eventSize) {
					l.log.Warn("short perf record", zap.Int("size", len(record.RawSample)))
					continue
				}

				// Zero-copy cast from raw bytes to FlowEvent
				ev := *(*FlowEvent)(unsafe.Pointer(&record.RawSample[0]))
				if !l.bus.TryPushFlow(ev) {
					l.log.Warn("flow event bus full — dropping event",
						zap.Uint8("anomaly_score", ev.AnomalyScore),
					)
				}
			}
		}
	}
}

// Close detaches BPF programs and closes readers.
func (l *BPFLoader) Close() error {
	if l.flowReader != nil {
		l.flowReader.Close()
	}
	if l.xdpLink != nil {
		l.xdpLink.Close()
	}
	if l.xdpProg != nil {
		l.xdpProg.Close()
	}
	return nil
}
