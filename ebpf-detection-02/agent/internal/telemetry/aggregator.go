// Package telemetry — Aggregator
//
// Reads raw FlowEvent and TCPEvent from the Bus every window duration,
// computes per-flow AggregatedMetrics, and pushes them to Bus.Metrics.
//
// Ownership: the consistent hash ring determines whether this agent
// is the authoritative owner of a given flow. Non-owned flows are
// still tracked locally but not forwarded to the control plane.

package telemetry

import (
	"context"
	"math"
	"sync"
	"time"

	"github.com/nimbusnet/agent/internal/ring"
	"go.uber.org/zap"
)

// AggregatorConfig tuning parameters.
type AggregatorConfig struct {
	WindowSize  time.Duration
	MaxFlows    int
	HashRing    *ring.Ring
	LocalNodeID string
}

// flowAccumulator holds running totals for a single flow within a window.
type flowAccumulator struct {
	srcIP   string
	dstIP   string
	region  string

	totalBytes   uint64
	totalPackets uint64

	retransmitSum uint64
	rttSamples    []uint32 // kept for jitter calculation
	rttEwma       uint32

	maxAnomalyScore uint8
	anomalyScoreSum float64
	anomalyCount    uint64

	connects uint32
	closes   uint32
	retrans  uint32
}

func (a *flowAccumulator) toMetric(start, end time.Time, ownerID string, isLocal bool) AggregatedMetric {
	retransmitRate := float64(0)
	if a.totalPackets > 0 {
		retransmitRate = float64(a.retransmitSum) / float64(a.totalPackets)
	}

	avgAnomalyScore := float64(0)
	if a.anomalyCount > 0 {
		avgAnomalyScore = a.anomalyScoreSum / float64(a.anomalyCount)
	}

	// Jitter = std deviation of RTT samples
	jitter := uint32(0)
	if len(a.rttSamples) > 1 {
		mean := float64(a.rttEwma)
		variance := float64(0)
		for _, s := range a.rttSamples {
			d := float64(s) - mean
			variance += d * d
		}
		variance /= float64(len(a.rttSamples))
		jitter = uint32(math.Sqrt(variance))
	}

	return AggregatedMetric{
		WindowStart:      start,
		WindowEnd:        end,
		SrcIP:            a.srcIP,
		DstIP:            a.dstIP,
		Region:           a.region,
		TotalBytes:       a.totalBytes,
		TotalPackets:     a.totalPackets,
		RetransmitRate:   retransmitRate,
		RTTEwmaUs:        a.rttEwma,
		RTTJitterUs:      jitter,
		MaxAnomalyScore:  a.maxAnomalyScore,
		AvgAnomalyScore:  avgAnomalyScore,
		ConnectsObserved: a.connects,
		ClosesObserved:   a.closes,
		RetransObserved:  a.retrans,
		OwnerNodeID:      ownerID,
		IsLocal:          isLocal,
	}
}

// Aggregator reads from the bus and produces windowed metrics.
type Aggregator struct {
	cfg AggregatorConfig
	bus *Bus
	log *zap.Logger

	mu    sync.Mutex
	flows map[string]*flowAccumulator // key = "srcIP:srcPort-dstIP:dstPort"
}

// NewAggregator constructs an Aggregator.
func NewAggregator(cfg AggregatorConfig, bus *Bus, log *zap.Logger) *Aggregator {
	if cfg.WindowSize == 0 {
		cfg.WindowSize = 50 * time.Millisecond
	}
	if cfg.MaxFlows == 0 {
		cfg.MaxFlows = 65536
	}
	return &Aggregator{
		cfg:   cfg,
		bus:   bus,
		log:   log,
		flows: make(map[string]*flowAccumulator, 1024),
	}
}

// Run starts the aggregation loop. Blocks until ctx is cancelled.
func (a *Aggregator) Run(ctx context.Context) error {
	ticker := time.NewTicker(a.cfg.WindowSize)
	defer ticker.Stop()

	windowStart := time.Now()

	for {
		select {
		case <-ctx.Done():
			return nil

		case ev, ok := <-a.bus.FlowEvents:
			if !ok {
				return nil
			}
			a.ingestFlow(ev)

		case ev, ok := <-a.bus.TCPEvents:
			if !ok {
				return nil
			}
			a.ingestTCP(ev)

		case <-ticker.C:
			windowEnd := time.Now()
			a.flush(windowStart, windowEnd)
			windowStart = windowEnd
		}
	}
}

// ingestFlow updates the accumulator for a raw XDP FlowEvent.
func (a *Aggregator) ingestFlow(ev FlowEvent) {
	key := flowKey(ev.SrcIPString(), ev.SrcPort, ev.DstIPString(), ev.DstPort)

	a.mu.Lock()
	defer a.mu.Unlock()

	acc, exists := a.flows[key]
	if !exists {
		if len(a.flows) >= a.cfg.MaxFlows {
			// Evict oldest — simple strategy, LRU in production
			a.log.Warn("flow table full — evicting", zap.Int("size", len(a.flows)))
			for k := range a.flows {
				delete(a.flows, k)
				break
			}
		}
		acc = &flowAccumulator{
			srcIP:  ev.SrcIPString(),
			dstIP:  ev.DstIPString(),
			region: inferRegion(ev.DstIPString()),
		}
		a.flows[key] = acc
	}

	acc.totalBytes += ev.BytesTotal
	acc.totalPackets += uint64(ev.PktCount)
	acc.retransmitSum += uint64(ev.RetransmitCount)

	if ev.RTTEwmaUs > 0 {
		acc.rttSamples = append(acc.rttSamples, ev.RTTEwmaUs)
		acc.rttEwma = ev.RTTEwmaUs // last value as proxy; aggregator owns EWMA
	}

	if ev.AnomalyScore > acc.maxAnomalyScore {
		acc.maxAnomalyScore = ev.AnomalyScore
	}
	acc.anomalyScoreSum += float64(ev.AnomalyScore)
	acc.anomalyCount++
}

// ingestTCP updates lifecycle counters from TCP kprobe events.
func (a *Aggregator) ingestTCP(ev TCPEvent) {
	srcIP := ipToString(ev.SrcIP)
	dstIP := ipToString(ev.DstIP)
	key := flowKey(srcIP, ev.SrcPort, dstIP, ev.DstPort)

	a.mu.Lock()
	defer a.mu.Unlock()

	acc, exists := a.flows[key]
	if !exists {
		acc = &flowAccumulator{srcIP: srcIP, dstIP: dstIP, region: inferRegion(dstIP)}
		a.flows[key] = acc
	}

	const (
		tcpEventConnect    = 0
		tcpEventClose      = 1
		tcpEventRetransmit = 2
		tcpEventRTTSample  = 3
	)

	switch ev.EventType {
	case tcpEventConnect:
		acc.connects++
	case tcpEventClose:
		acc.closes++
	case tcpEventRetransmit:
		acc.retrans++
		acc.retransmitSum++
	case tcpEventRTTSample:
		if ev.SRTTUs > 0 {
			acc.rttSamples = append(acc.rttSamples, ev.SRTTUs)
			acc.rttEwma = ev.SRTTUs
		}
	}
}

// flush drains all accumulators into AggregatedMetric and resets state.
func (a *Aggregator) flush(start, end time.Time) {
	a.mu.Lock()
	snapshot := a.flows
	a.flows = make(map[string]*flowAccumulator, len(snapshot))
	a.mu.Unlock()

	emitted := 0
	for _, acc := range snapshot {
		ownerID := a.cfg.HashRing.Owner(acc.srcIP + acc.dstIP)
		isLocal := ownerID == a.cfg.LocalNodeID

		metric := acc.toMetric(start, end, ownerID, isLocal)

		select {
		case a.bus.Metrics <- metric:
			emitted++
		default:
			a.log.Warn("metrics channel full — dropping aggregate",
				zap.String("src", acc.srcIP),
				zap.String("dst", acc.dstIP),
			)
		}
	}

	if emitted > 0 {
		a.log.Debug("aggregation window flushed",
			zap.Int("flows", emitted),
			zap.Duration("window", end.Sub(start)),
		)
	}
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

func flowKey(srcIP string, srcPort uint16, dstIP string, dstPort uint16) string {
	return fmt.Sprintf("%s:%d-%s:%d", srcIP, srcPort, dstIP, dstPort)
}

// inferRegion maps destination IPs to AWS regions based on CIDR ranges.
// In production this is populated from the Terraform outputs via SSM.
func inferRegion(dstIP string) string {
	// Placeholder — replaced at runtime by CIDR table from SSM Parameter Store
	return "unknown"
}

// Ensure fmt is imported (used in flowKey)
import "fmt"
