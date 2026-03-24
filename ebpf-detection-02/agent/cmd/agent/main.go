// NimbusNet Agent — main entrypoint
// Loads and pins BPF programs, starts perf buffer readers,
// fans out typed events to downstream consumers via channels.

package main

import (
	"context"
	"flag"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/nimbusnet/agent/internal/telemetry"
	"github.com/nimbusnet/agent/internal/healthz"
	"github.com/nimbusnet/agent/internal/ring"
	"github.com/nimbusnet/agent/internal/metrics"
	"go.uber.org/zap"
	"gopkg.in/yaml.v3"
)

func main() {
	configPath := flag.String("config", "/etc/nimbusnet/agent.yaml", "Path to agent config")
	flag.Parse()

	// ── Logger ──────────────────────────────────────────────────────────────
	log, err := zap.NewProduction()
	if err != nil {
		panic(err)
	}
	defer log.Sync()

	// ── Config ──────────────────────────────────────────────────────────────
	cfg, err := loadConfig(*configPath)
	if err != nil {
		log.Fatal("failed to load config", zap.Error(err))
	}
	log.Info("NimbusNet agent starting",
		zap.String("region", cfg.Region),
		zap.String("iface", cfg.Interface),
		zap.String("version", Version),
	)

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	// ── Consistent Hash Ring ─────────────────────────────────────────────────
	hashRing := ring.New(cfg.Ring.VirtualNodes, cfg.Ring.Peers)
	log.Info("hash ring initialised",
		zap.Int("virtual_nodes", cfg.Ring.VirtualNodes),
		zap.Int("peers", len(cfg.Ring.Peers)),
	)

	// ── Metrics Server ───────────────────────────────────────────────────────
	metricsServer := metrics.NewServer(cfg.Metrics.ListenAddr, log)
	go metricsServer.Serve(ctx)

	// ── Telemetry Bus ────────────────────────────────────────────────────────
	// Central typed channels — all BPF readers push here, all consumers pull.
	bus := telemetry.NewBus(telemetry.BusConfig{
		FlowBufferSize: cfg.Bus.FlowBufferSize,
		TCPBufferSize:  cfg.Bus.TCPBufferSize,
	})

	// ── BPF Loader ───────────────────────────────────────────────────────────
	loader, err := telemetry.NewBPFLoader(telemetry.BPFConfig{
		Interface:       cfg.Interface,
		XDPProgramPath:  cfg.BPF.XDPObjectPath,
		TCPProgramPath:  cfg.BPF.TCPObjectPath,
		PerfPageCount:   cfg.BPF.PerfPageCount,
		DrainIntervalMs: cfg.BPF.DrainIntervalMs,
	}, bus, log)
	if err != nil {
		log.Fatal("failed to initialise BPF loader", zap.Error(err))
	}
	defer loader.Close()

	if err := loader.Load(); err != nil {
		log.Fatal("failed to load BPF programs", zap.Error(err))
	}
	log.Info("BPF programs loaded and attached",
		zap.String("interface", cfg.Interface),
	)

	// ── Aggregator ───────────────────────────────────────────────────────────
	// Reads from bus, computes per-flow aggregates every drain interval,
	// pushes AggregatedMetric to downstream (ML layer, control plane, Route53).
	agg := telemetry.NewAggregator(telemetry.AggregatorConfig{
		WindowSize:   time.Duration(cfg.Aggregator.WindowMs) * time.Millisecond,
		MaxFlows:     cfg.Aggregator.MaxFlows,
		HashRing:     hashRing,
		LocalNodeID:  cfg.NodeID,
	}, bus, log)

	// ── Health Check Endpoint ─────────────────────────────────────────────────
	// /healthz returns 200 normally.
	// Returns 503 when the control plane sets the drain flag (Route53 awareness).
	healthServer := healthz.NewServer(healthz.Config{
		ListenAddr:  cfg.Healthz.ListenAddr,
		DrainDelay:  time.Duration(cfg.Healthz.DrainDelayMs) * time.Millisecond,
	}, log)
	go healthServer.Serve(ctx)

	// ── Start Everything ─────────────────────────────────────────────────────
	errCh := make(chan error, 4)

	go func() { errCh <- loader.Run(ctx) }()
	go func() { errCh <- agg.Run(ctx) }()

	log.Info("NimbusNet agent running")

	select {
	case <-ctx.Done():
		log.Info("shutdown signal received — draining")
		healthServer.SetDraining(true)
		// Give Route53 time to drain before we stop processing
		time.Sleep(time.Duration(cfg.Healthz.DrainDelayMs) * time.Millisecond)
		log.Info("drain complete — exiting")
	case err := <-errCh:
		log.Error("fatal error from subsystem", zap.Error(err))
		cancel()
	}
}

// ─── Config ───────────────────────────────────────────────────────────────────

type Config struct {
	Region    string `yaml:"region"`
	NodeID    string `yaml:"node_id"`
	Interface string `yaml:"interface"`

	BPF struct {
		XDPObjectPath   string `yaml:"xdp_object_path"`
		TCPObjectPath   string `yaml:"tcp_object_path"`
		PerfPageCount   int    `yaml:"perf_page_count"`
		DrainIntervalMs int    `yaml:"drain_interval_ms"`
	} `yaml:"bpf"`

	Bus struct {
		FlowBufferSize int `yaml:"flow_buffer_size"`
		TCPBufferSize  int `yaml:"tcp_buffer_size"`
	} `yaml:"bus"`

	Aggregator struct {
		WindowMs int `yaml:"window_ms"`
		MaxFlows int `yaml:"max_flows"`
	} `yaml:"aggregator"`

	Ring struct {
		VirtualNodes int      `yaml:"virtual_nodes"`
		Peers        []string `yaml:"peers"`
	} `yaml:"ring"`

	Metrics struct {
		ListenAddr string `yaml:"listen_addr"`
	} `yaml:"metrics"`

	Healthz struct {
		ListenAddr  string `yaml:"listen_addr"`
		DrainDelayMs int   `yaml:"drain_delay_ms"`
	} `yaml:"healthz"`
}

func loadConfig(path string) (*Config, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	cfg := &Config{}
	if err := yaml.NewDecoder(f).Decode(cfg); err != nil {
		return nil, err
	}
	return cfg, nil
}

// Version is injected at build time via -ldflags
var Version = "dev"
