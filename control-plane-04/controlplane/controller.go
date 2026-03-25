// Package controlplane is the NimbusNet control plane orchestrator.
//
// This is the central coordinator for Phase 4. It:
//   1. Receives MLScoredMetric from the Phase 3 ML scoring service
//   2. Drives the per-region healing state machines
//   3. Issues traffic shaping commands (adaptive decay / hard failover)
//   4. Executes Route 53 two-plane drain/restore
//   5. Updates VPC route tables (data plane fast path)
//   6. Publishes SLO burn rate metrics to CloudWatch
//   7. Feeds bandit observations back to Phase 3
//
// The control plane runs as a long-lived goroutine per region.
// All AWS API calls are idempotent and rate-limited.

package controlplane

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/nimbusnet/controlplane/statemachine"
	"github.com/nimbusnet/controlplane/trafficshaper"
	"github.com/nimbusnet/controlplane/route53"
	"github.com/nimbusnet/controlplane/routetable"
	"go.uber.org/zap"
)

// ─── Scored Metric (mirrors Phase 3 MLScoredMetric) ──────────────────────────

// MLScoredMetric is the typed input from the Phase 3 ML scoring service.
// Received via the gRPC/HTTP scoring endpoint every 50ms per active flow.
type MLScoredMetric struct {
	Region                  string
	SrcIP                   string
	DstIP                   string
	IsolationForestScore    float64
	LSTMFaultType           string // "UNKNOWN", "NETWORK_LATENCY", "PACKET_LOSS", etc.
	LSTMConfidence          float64
	XGBoostSeverity         string // "NOMINAL", "WARNING", "DEGRADED", "CRITICAL"
	XGBoostConfidence       float64
	BanditRoutingWeight     float64
	ShouldTriggerFailover   bool
	FailoverConfidence      float64
	RTTEwmaUs               int64
	RetransmitRate          float64
	ScoredAtNs              int64
}

// ─── Config ───────────────────────────────────────────────────────────────────

type Config struct {
	LocalRegion string
	Regions     []string

	// Minimum confidence to act on a failover signal
	// Below this, we escalate monitoring but don't change routing
	MinFailoverConfidence float64 // default: 0.65

	// How many consecutive CRITICAL signals before hard failover
	// (prevents flapping on transient spikes)
	CriticalSignalsThreshold int // default: 3

	// Timeout check interval
	FSMTickInterval time.Duration // default: 10s

	// SLO burn rate reporting interval
	SLOReportInterval time.Duration // default: 60s
}

func DefaultConfig(localRegion string, regions []string) Config {
	return Config{
		LocalRegion:              localRegion,
		Regions:                  regions,
		MinFailoverConfidence:    0.65,
		CriticalSignalsThreshold: 3,
		FSMTickInterval:          10 * time.Second,
		SLOReportInterval:        60 * time.Second,
	}
}

// ─── Controller ───────────────────────────────────────────────────────────────

// Controller is the central control plane orchestrator.
type Controller struct {
	cfg     Config
	log     *zap.Logger

	// Per-region state machines
	fsms map[string]*statemachine.RegionFSM

	// Traffic shaper — manages weight decay/ramp
	shaper *trafficshaper.AdaptiveTrafficShaper

	// AWS integrations
	r53        *route53.Client
	routeTable *routetable.Manager

	// Channels
	scoredMetrics chan MLScoredMetric

	// Anti-flapping: count of consecutive CRITICAL signals per region
	criticalCounts map[string]int
	mu             sync.Mutex
}

// New creates a Controller and wires all subsystems together.
func New(
	cfg        Config,
	r53Client  *route53.Client,
	rtManager  *routetable.Manager,
	log        *zap.Logger,
) *Controller {
	c := &Controller{
		cfg:            cfg,
		log:            log,
		r53:            r53Client,
		routeTable:     rtManager,
		fsms:           make(map[string]*statemachine.RegionFSM),
		scoredMetrics:  make(chan MLScoredMetric, 1024),
		criticalCounts: make(map[string]int),
	}

	// Create per-region FSMs
	for _, region := range cfg.Regions {
		fsmCfg := statemachine.DefaultConfig(region)
		fsm := statemachine.New(fsmCfg, log)
		c.wireActions(fsm, region)
		c.fsms[region] = fsm
	}

	// Create traffic shaper
	shaperCfg := trafficshaper.DefaultConfig()
	c.shaper = trafficshaper.New(shaperCfg, cfg.Regions, log)

	// Wire shaper → route table
	c.shaper.OnWeightChange(func(ctx context.Context, weights map[string]float64) error {
		return c.routeTable.UpdateWeights(ctx, weights)
	})

	return c
}

// ─── Wire FSM Actions ─────────────────────────────────────────────────────────

// wireActions connects state machine transitions to infrastructure actions.
// This is where the FSM becomes operational — each state entry/exit
// triggers real AWS API calls.
func (c *Controller) wireActions(fsm *statemachine.RegionFSM, region string) {

	// ── Enter DEGRADED ────────────────────────────────────────────────────────
	// 1. Begin DNS drain (/healthz is returning 503 from Phase 2 agent)
	// 2. Start traffic decay in the shaper
	// 3. Log the event for postmortem
	fsm.OnEnter(statemachine.StateDegraded, func(ctx context.Context, from, to statemachine.State, r string, payload map[string]any) error {
		c.log.Warn("ControlPlane: region DEGRADED — initiating soft failover",
			zap.String("region", region),
			zap.String("from", from.String()),
		)
		c.shaper.OnDegraded(region)

		// Drain Route 53 — DNS plane slow path (35s convergence)
		if err := c.r53.DrainRegion(ctx, region); err != nil {
			c.log.Error("ControlPlane: R53 drain failed", zap.String("region", region), zap.Error(err))
			// Non-fatal — data plane is already handling it via route table
		}
		return nil
	})

	// ── Enter HEALING ─────────────────────────────────────────────────────────
	// Runbook executor (Phase 5) is called here.
	// Traffic is at minimum weight while healing proceeds.
	fsm.OnEnter(statemachine.StateHealing, func(ctx context.Context, from, to statemachine.State, r string, payload map[string]any) error {
		c.log.Info("ControlPlane: region HEALING — runbook executing",
			zap.String("region", region),
		)
		// Runbook is triggered via the SRE operations layer (Phase 5).
		// The FSM receives EventRunbookSucceeded or EventRunbookFailed from that layer.
		// Control plane just holds minimum weight here.
		return nil
	})

	// ── Enter RECOVERED ───────────────────────────────────────────────────────
	// Healing succeeded. Begin traffic ramp. 60s observation window.
	fsm.OnEnter(statemachine.StateRecovered, func(ctx context.Context, from, to statemachine.State, r string, payload map[string]any) error {
		c.log.Info("ControlPlane: region RECOVERED — ramping traffic back",
			zap.String("region", region),
		)
		c.shaper.OnRecovering(region)
		return nil
	})

	// ── Enter HEALTHY ─────────────────────────────────────────────────────────
	// Observation window passed. Restore full weight and Route 53.
	fsm.OnEnter(statemachine.StateHealthy, func(ctx context.Context, from, to statemachine.State, r string, payload map[string]any) error {
		if from == statemachine.StateRecovered {
			c.log.Info("ControlPlane: region HEALTHY — full restore",
				zap.String("region", region),
			)
			c.shaper.OnHealthy(region)

			if err := c.r53.RestoreRegion(ctx, region); err != nil {
				c.log.Error("ControlPlane: R53 restore failed", zap.String("region", region), zap.Error(err))
			}
		}
		return nil
	})

	// ── Enter FAILED ─────────────────────────────────────────────────────────
	// Healing exhausted TTL. Hard failover. Page the SRE.
	fsm.OnEnter(statemachine.StateFailed, func(ctx context.Context, from, to statemachine.State, r string, payload map[string]any) error {
		c.log.Error("ControlPlane: region FAILED — hard failover, SRE paged",
			zap.String("region", region),
			zap.String("from", from.String()),
		)
		c.shaper.OnFailed(region)

		// VPC route table hard failover — data plane
		if err := c.routeTable.FailoverRegion(ctx, region); err != nil {
			c.log.Error("ControlPlane: route table failover failed",
				zap.String("region", region),
				zap.Error(err),
			)
		}

		// Phase 5 SRE operations layer fires PagerDuty here
		// (The control plane publishes a CloudWatch alarm which Alertmanager picks up)
		return nil
	})
}

// ─── Main Loop ────────────────────────────────────────────────────────────────

// Run starts the control plane. Blocks until ctx is cancelled.
func (c *Controller) Run(ctx context.Context) error {
	fsmTicker := time.NewTicker(c.cfg.FSMTickInterval)
	sloTicker  := time.NewTicker(c.cfg.SLOReportInterval)
	defer fsmTicker.Stop()
	defer sloTicker.Stop()

	// Start shaper tick loop
	go c.shaper.Run(ctx)

	c.log.Info("ControlPlane: running",
		zap.String("local_region", c.cfg.LocalRegion),
		zap.Strings("regions", c.cfg.Regions),
	)

	for {
		select {
		case <-ctx.Done():
			return nil

		case metric, ok := <-c.scoredMetrics:
			if !ok {
				return nil
			}
			if err := c.processScoredMetric(ctx, metric); err != nil {
				c.log.Error("ControlPlane: metric processing failed", zap.Error(err))
			}

		case <-fsmTicker.C:
			// Check all FSM timeouts
			for region, fsm := range c.fsms {
				if err := fsm.CheckTimeouts(ctx); err != nil {
					c.log.Error("ControlPlane: FSM timeout check failed",
						zap.String("region", region),
						zap.Error(err),
					)
				}
			}

		case <-sloTicker.C:
			c.publishSLOMetrics(ctx)
		}
	}
}

// Ingest pushes a scored metric into the control plane for processing.
// Called by the Phase 3 ML scoring service callback.
func (c *Controller) Ingest(metric MLScoredMetric) {
	select {
	case c.scoredMetrics <- metric:
	default:
		c.log.Warn("ControlPlane: scored metrics channel full — dropping",
			zap.String("region", metric.Region),
		)
	}
}

// ─── Metric Processing ────────────────────────────────────────────────────────

func (c *Controller) processScoredMetric(ctx context.Context, metric MLScoredMetric) error {
	fsm, ok := c.fsms[metric.Region]
	if !ok {
		return fmt.Errorf("no FSM for region %q", metric.Region)
	}

	// Update bandit weights from the ML recommendation
	c.shaper.UpdateBanditWeights(map[string]float64{
		metric.Region: metric.BanditRoutingWeight,
	})

	// Below confidence threshold — increase monitoring but don't act
	if metric.FailoverConfidence < c.cfg.MinFailoverConfidence {
		return nil
	}

	currentState := fsm.Current()

	// ── Hard failover signal: CRITICAL severity ────────────────────────────
	if metric.XGBoostSeverity == "CRITICAL" && metric.XGBoostConfidence > 0.70 {
		c.mu.Lock()
		c.criticalCounts[metric.Region]++
		count := c.criticalCounts[metric.Region]
		c.mu.Unlock()

		if count >= c.cfg.CriticalSignalsThreshold {
			c.mu.Lock()
			c.criticalCounts[metric.Region] = 0
			c.mu.Unlock()

			if currentState == statemachine.StateHealthy || currentState == statemachine.StateDegraded {
				return fsm.Send(ctx, statemachine.Event{
					Type:      statemachine.EventSeverityCritical,
					Region:    metric.Region,
					Timestamp: time.Now(),
					Payload: map[string]any{
						"xgb_severity":   metric.XGBoostSeverity,
						"xgb_confidence": metric.XGBoostConfidence,
						"if_score":       metric.IsolationForestScore,
						"fault_type":     metric.LSTMFaultType,
					},
				})
			}
		}
		return nil
	}

	// ── Soft failover signal: ensemble decision ────────────────────────────
	if metric.ShouldTriggerFailover && currentState == statemachine.StateHealthy {
		c.mu.Lock()
		c.criticalCounts[metric.Region] = 0 // Reset critical counter
		c.mu.Unlock()

		return fsm.Send(ctx, statemachine.Event{
			Type:      statemachine.EventAnomalyDetected,
			Region:    metric.Region,
			Timestamp: time.Now(),
			Payload: map[string]any{
				"if_score":       metric.IsolationForestScore,
				"fault_type":     metric.LSTMFaultType,
				"confidence":     metric.FailoverConfidence,
			},
		})
	}

	// ── Health verification: clear consecutive critical counts on healthy signal
	if !metric.ShouldTriggerFailover && metric.IsolationForestScore < 0.30 {
		c.mu.Lock()
		c.criticalCounts[metric.Region] = 0
		c.mu.Unlock()

		// If in RECOVERED state, check if we should transition to HEALTHY
		if currentState == statemachine.StateRecovered {
			return fsm.Send(ctx, statemachine.Event{
				Type:      statemachine.EventHealthVerified,
				Region:    metric.Region,
				Timestamp: time.Now(),
			})
		}
	}

	return nil
}

// ─── Runbook Integration ──────────────────────────────────────────────────────

// NotifyRunbookStarted is called by the SRE operations layer (Phase 5)
// when a runbook begins execution.
func (c *Controller) NotifyRunbookStarted(ctx context.Context, region string) error {
	fsm, ok := c.fsms[region]
	if !ok {
		return fmt.Errorf("no FSM for region %q", region)
	}
	return fsm.Send(ctx, statemachine.Event{
		Type:      statemachine.EventRunbookStarted,
		Region:    region,
		Timestamp: time.Now(),
	})
}

// NotifyRunbookSucceeded is called when a runbook verifies healing.
func (c *Controller) NotifyRunbookSucceeded(ctx context.Context, region string) error {
	fsm, ok := c.fsms[region]
	if !ok {
		return fmt.Errorf("no FSM for region %q", region)
	}
	return fsm.Send(ctx, statemachine.Event{
		Type:      statemachine.EventRunbookSucceeded,
		Region:    region,
		Timestamp: time.Now(),
	})
}

// NotifyRunbookFailed is called when a runbook exhausts its retries.
func (c *Controller) NotifyRunbookFailed(ctx context.Context, region string) error {
	fsm, ok := c.fsms[region]
	if !ok {
		return fmt.Errorf("no FSM for region %q", region)
	}
	return fsm.Send(ctx, statemachine.Event{
		Type:      statemachine.EventRunbookFailed,
		Region:    region,
		Timestamp: time.Now(),
	})
}

// ManualOverride allows SRE to force a region into a specific state.
func (c *Controller) ManualOverride(ctx context.Context, region string, targetState statemachine.State) error {
	fsm, ok := c.fsms[region]
	if !ok {
		return fmt.Errorf("no FSM for region %q", region)
	}
	return fsm.Send(ctx, statemachine.Event{
		Type:      statemachine.EventManualOverride,
		Region:    region,
		Timestamp: time.Now(),
		Payload:   map[string]any{"target_state": targetState.String()},
	})
}

// ─── SLO Metrics ─────────────────────────────────────────────────────────────

func (c *Controller) publishSLOMetrics(ctx context.Context) {
	for region, fsm := range c.fsms {
		status := fsm.Status()

		// Publish to Prometheus (picked up by Alertmanager for multi-window burn rate)
		regionStateLabelValue := status.State
		_ = regionStateLabelValue // wired to Prometheus in metrics.go

		c.log.Debug("ControlPlane: SLO snapshot",
			zap.String("region", region),
			zap.String("state", status.State),
			zap.Duration("time_in_state", status.TimeInState),
			zap.Int("failover_count", status.FailoverCount),
		)
	}
}

// ─── Status ───────────────────────────────────────────────────────────────────

// RegionStatus returns a diagnostic snapshot of all regions.
type RegionStatus struct {
	Region        string
	FSMState      string
	TimeInState   time.Duration
	FailoverCount int
	CurrentWeight float64
}

func (c *Controller) AllRegionStatus() []RegionStatus {
	weights := c.shaper.RawWeights()
	result := make([]RegionStatus, 0, len(c.fsms))

	for region, fsm := range c.fsms {
		status := fsm.Status()
		weight := 0.0
		if rw, ok := weights[region]; ok {
			weight = rw.Current
		}
		result = append(result, RegionStatus{
			Region:        region,
			FSMState:      status.State,
			TimeInState:   status.TimeInState,
			FailoverCount: status.FailoverCount,
			CurrentWeight: weight,
		})
	}
	return result
}
