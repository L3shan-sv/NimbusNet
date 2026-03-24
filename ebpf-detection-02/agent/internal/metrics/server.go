// Package metrics exposes Prometheus metrics for the NimbusNet agent.
//
// Metric naming follows Prometheus conventions:
//   nimbusnet_agent_{subsystem}_{name}_{unit}
//
// All metrics are registered globally and exposed on /metrics.

package metrics

import (
	"context"
	"net/http"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"go.uber.org/zap"
)

// ─── Metric Definitions ───────────────────────────────────────────────────────

var (
	// XDP perf buffer metrics
	FlowEventsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "nimbusnet_agent_flow_events_total",
		Help: "Total flow events received from XDP perf buffer.",
	}, []string{"event_type"}) // event_type: periodic | anomaly | flow_end

	FlowEventDropsTotal = promauto.NewCounter(prometheus.CounterOpts{
		Name: "nimbusnet_agent_flow_event_drops_total",
		Help: "Flow events dropped due to full bus channel.",
	})

	// Anomaly scoring
	AnomalyScoreHistogram = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "nimbusnet_agent_anomaly_score",
		Help:    "Distribution of inline BPF anomaly scores (0-100).",
		Buckets: []float64{10, 20, 30, 50, 70, 90, 100},
	})

	// RTT metrics
	RTTEwmaHistogram = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "nimbusnet_agent_rtt_ewma_microseconds",
		Help:    "EWMA RTT per flow in microseconds.",
		Buckets: prometheus.ExponentialBuckets(1000, 2, 12), // 1ms → 4s
	}, []string{"region"})

	// Retransmit rate
	RetransmitRateHistogram = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "nimbusnet_agent_retransmit_rate",
		Help:    "Fraction of packets that are retransmits, per aggregation window.",
		Buckets: []float64{0.01, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0},
	}, []string{"region"})

	// Aggregation
	AggregationWindowDuration = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "nimbusnet_agent_aggregation_window_duration_seconds",
		Help:    "Time taken to flush one aggregation window.",
		Buckets: prometheus.DefBuckets,
	})

	ActiveFlows = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "nimbusnet_agent_active_flows",
		Help: "Number of flows tracked in the current aggregation window.",
	})

	// Hash ring
	HashRingVirtualNodes = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "nimbusnet_agent_hash_ring_virtual_nodes",
		Help: "Number of virtual nodes in the consistent hash ring.",
	})

	HashRingPeers = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "nimbusnet_agent_hash_ring_peers",
		Help: "Number of peer agents in the consistent hash ring.",
	})

	LocallyOwnedFlows = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "nimbusnet_agent_locally_owned_flows",
		Help: "Flows for which this agent is the hash ring owner.",
	})

	// Health / drain
	DrainModeActive = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "nimbusnet_agent_drain_mode_active",
		Help: "1 if the agent is currently draining (returning 503 on /healthz), 0 otherwise.",
	})

	// BPF program load
	BPFProgramLoaded = promauto.NewGaugeVec(prometheus.GaugeOpts{
		Name: "nimbusnet_agent_bpf_program_loaded",
		Help: "1 if the named BPF program is attached and running.",
	}, []string{"program"}) // program: xdp_flow_probe | tcp_probe
)

// ─── Server ───────────────────────────────────────────────────────────────────

// Server serves Prometheus metrics on /metrics.
type Server struct {
	listenAddr string
	log        *zap.Logger
}

// NewServer creates a metrics server.
func NewServer(listenAddr string, log *zap.Logger) *Server {
	if listenAddr == "" {
		listenAddr = ":9090"
	}
	return &Server{listenAddr: listenAddr, log: log}
}

// Serve starts the metrics HTTP server. Blocks until ctx is cancelled.
func (s *Server) Serve(ctx context.Context) {
	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.Handler())
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "/metrics", http.StatusMovedPermanently)
	})

	srv := &http.Server{
		Addr:         s.listenAddr,
		Handler:      mux,
		ReadTimeout:  5 * time.Second,
		WriteTimeout: 10 * time.Second,
	}

	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		srv.Shutdown(shutdownCtx)
	}()

	s.log.Info("metrics server listening", zap.String("addr", s.listenAddr))
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		s.log.Error("metrics server error", zap.Error(err))
	}
}
