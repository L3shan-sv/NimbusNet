// NimbusNet Control Plane — main entrypoint
// Phase 4: Healing state machine + adaptive traffic shaper + Route53 + VPC route table

package main

import (
	"context"
	"flag"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	awsroute53 "github.com/aws/aws-sdk-go-v2/service/route53"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	controlplane "github.com/nimbusnet/controlplane"
	"github.com/nimbusnet/controlplane/route53"
	"github.com/nimbusnet/controlplane/routetable"
	"go.uber.org/zap"
	"gopkg.in/yaml.v3"
)

func main() {
	configPath := flag.String("config", "/etc/nimbusnet/controlplane.yaml", "Config path")
	flag.Parse()

	// ── Logger ──────────────────────────────────────────────────────────────
	log, _ := zap.NewProduction()
	defer log.Sync()

	// ── Config ──────────────────────────────────────────────────────────────
	cfg, err := loadConfig(*configPath)
	if err != nil {
		log.Fatal("failed to load config", zap.Error(err))
	}

	log.Info("NimbusNet control plane starting",
		zap.String("region", cfg.Region),
		zap.Strings("regions", cfg.Regions),
	)

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	// ── AWS SDK ─────────────────────────────────────────────────────────────
	awsCfg, err := config.LoadDefaultConfig(ctx, config.WithRegion(cfg.Region))
	if err != nil {
		log.Fatal("failed to load AWS config", zap.Error(err))
	}

	r53APIClient  := awsroute53.NewFromConfig(awsCfg)
	ddbClient     := dynamodb.NewFromConfig(awsCfg)
	ec2Client     := ec2.NewFromConfig(awsCfg)

	// ── Route 53 Client ─────────────────────────────────────────────────────
	r53Config := route53.Config{
		HostedZoneID:   cfg.Route53.HostedZoneID,
		RecordName:     cfg.Route53.RecordName,
		RecordTTL:      int64(cfg.Route53.RecordTTLSeconds),
		HealthCheckIDs: cfg.Route53.HealthCheckIDs,
		LockTableName:  cfg.DynamoDB.LockTableName,
		LockTTLSeconds: cfg.DynamoDB.LockTTLSeconds,
	}
	r53Client := route53.New(r53Config, r53APIClient, ddbClient, cfg.Region, log)

	// ── Route Table Manager ─────────────────────────────────────────────────
	rtConfig := routetable.Config{
		LocalRegion: cfg.Region,
		Regions:     buildRegionRouteConfigs(cfg),
	}
	rtManager := routetable.New(rtConfig, ec2Client, log)

	// ── Control Plane ───────────────────────────────────────────────────────
	cpConfig := controlplane.DefaultConfig(cfg.Region, cfg.Regions)
	cpConfig.MinFailoverConfidence    = cfg.ControlPlane.MinFailoverConfidence
	cpConfig.CriticalSignalsThreshold = cfg.ControlPlane.CriticalSignalsThreshold
	cpConfig.FSMTickInterval          = time.Duration(cfg.ControlPlane.FSMTickIntervalMs) * time.Millisecond
	cpConfig.SLOReportInterval        = time.Duration(cfg.ControlPlane.SLOReportIntervalS) * time.Second

	controller := controlplane.New(cpConfig, r53Client, rtManager, log)

	// ── Prometheus Metrics Server ────────────────────────────────────────────
	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.Handler())
	mux.HandleFunc("/status", func(w http.ResponseWriter, r *http.Request) {
		// Quick JSON status for health checks
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		// Status is rendered from controller.AllRegionStatus()
	})

	metricsServer := &http.Server{
		Addr:    cfg.MetricsAddr,
		Handler: mux,
	}
	go func() {
		log.Info("Metrics server listening", zap.String("addr", cfg.MetricsAddr))
		if err := metricsServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Error("Metrics server error", zap.Error(err))
		}
	}()

	// ── Run ─────────────────────────────────────────────────────────────────
	log.Info("Control plane running")
	if err := controller.Run(ctx); err != nil {
		log.Error("Control plane exited with error", zap.Error(err))
		os.Exit(1)
	}

	// Graceful shutdown
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()
	metricsServer.Shutdown(shutdownCtx)

	log.Info("Control plane shut down cleanly")
}

// ─── Config ───────────────────────────────────────────────────────────────────

type AppConfig struct {
	Region      string   `yaml:"region"`
	Regions     []string `yaml:"regions"`
	MetricsAddr string   `yaml:"metrics_addr"`

	Route53 struct {
		HostedZoneID     string            `yaml:"hosted_zone_id"`
		RecordName       string            `yaml:"record_name"`
		RecordTTLSeconds int               `yaml:"record_ttl_seconds"`
		HealthCheckIDs   map[string]string `yaml:"health_check_ids"`
	} `yaml:"route53"`

	DynamoDB struct {
		LockTableName  string `yaml:"lock_table_name"`
		LockTTLSeconds int    `yaml:"lock_ttl_seconds"`
	} `yaml:"dynamodb"`

	RouteTable struct {
		Regions map[string]struct {
			TGWAttachmentID string   `yaml:"tgw_attachment_id"`
			TargetCIDRs     []string `yaml:"target_cidrs"`
			SubnetIDs       []string `yaml:"subnet_ids"`
			RouteTableIDs   []string `yaml:"route_table_ids"`
		} `yaml:"regions"`
	} `yaml:"route_table"`

	ControlPlane struct {
		MinFailoverConfidence    float64 `yaml:"min_failover_confidence"`
		CriticalSignalsThreshold int     `yaml:"critical_signals_threshold"`
		FSMTickIntervalMs        int     `yaml:"fsm_tick_interval_ms"`
		SLOReportIntervalS       int     `yaml:"slo_report_interval_s"`
	} `yaml:"control_plane"`
}

func loadConfig(path string) (*AppConfig, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	cfg := &AppConfig{}
	return cfg, yaml.NewDecoder(f).Decode(cfg)
}

func buildRegionRouteConfigs(cfg *AppConfig) map[string]routetable.RegionRouteConfig {
	result := make(map[string]routetable.RegionRouteConfig, len(cfg.RouteTable.Regions))
	for region, rc := range cfg.RouteTable.Regions {
		result[region] = routetable.RegionRouteConfig{
			Region:          region,
			TGWAttachmentID: rc.TGWAttachmentID,
			TargetCIDRs:     rc.TargetCIDRs,
			SubnetIDs:       rc.SubnetIDs,
			RouteTableIDs:   rc.RouteTableIDs,
		}
	}
	return result
}

// Version injected at build time
var Version = "dev"
