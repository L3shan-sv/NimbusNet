// Package controlplane — Prometheus metrics for Phase 4.
//
// Metric naming: nimbusnet_controlplane_{subsystem}_{name}_{unit}
//
// These metrics feed the Google multi-window SLO burn rate calculation
// configured in Phase 1's Prometheus rules.

package controlplane

import (
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var (
	// ── FSM State ─────────────────────────────────────────────────────────────

	RegionFSMState = promauto.NewGaugeVec(prometheus.GaugeOpts{
		Name: "nimbusnet_controlplane_region_fsm_state",
		Help: "Current FSM state per region. Values: 0=HEALTHY 1=DEGRADED 2=HEALING 3=RECOVERED 4=FAILED",
	}, []string{"region"})

	RegionFSMTimeInState = promauto.NewGaugeVec(prometheus.GaugeOpts{
		Name: "nimbusnet_controlplane_region_fsm_time_in_state_seconds",
		Help: "Seconds the region FSM has been in its current state.",
	}, []string{"region", "state"})

	RegionFailoverTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "nimbusnet_controlplane_region_failover_total",
		Help: "Total failover events per region (HEALTHY→DEGRADED transitions).",
	}, []string{"region", "severity"}) // severity: soft | hard

	RegionTransitionTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "nimbusnet_controlplane_region_transition_total",
		Help: "Total FSM state transitions.",
	}, []string{"region", "from", "to", "event"})

	// ── Traffic Shaper ────────────────────────────────────────────────────────

	RegionRoutingWeight = promauto.NewGaugeVec(prometheus.GaugeOpts{
		Name: "nimbusnet_controlplane_region_routing_weight",
		Help: "Current normalised routing weight for each region [0, 1].",
	}, []string{"region"})

	RegionRoutingWeightTarget = promauto.NewGaugeVec(prometheus.GaugeOpts{
		Name: "nimbusnet_controlplane_region_routing_weight_target",
		Help: "Target routing weight the shaper is moving towards.",
	}, []string{"region"})

	ShaperTickDuration = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "nimbusnet_controlplane_shaper_tick_duration_seconds",
		Help:    "Duration of each shaper tick (weight update + route table write).",
		Buckets: prometheus.DefBuckets,
	})

	// ── Route Table ───────────────────────────────────────────────────────────

	RouteTableUpdateTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "nimbusnet_controlplane_route_table_update_total",
		Help: "Total VPC route table update operations.",
	}, []string{"region", "operation", "result"}) // operation: failover|restore|weight_update

	RouteTableUpdateDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "nimbusnet_controlplane_route_table_update_duration_seconds",
		Help:    "Duration of route table update operations.",
		Buckets: prometheus.ExponentialBuckets(0.001, 2, 12),
	}, []string{"region", "operation"})

	// ── Route 53 ──────────────────────────────────────────────────────────────

	R53OperationTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "nimbusnet_controlplane_r53_operation_total",
		Help: "Total Route 53 operations.",
	}, []string{"region", "operation", "result"}) // operation: drain|restore

	R53HealthCheckStatus = promauto.NewGaugeVec(prometheus.GaugeOpts{
		Name: "nimbusnet_controlplane_r53_health_check_healthy",
		Help: "1 if the R53 health check for this region is currently healthy.",
	}, []string{"region"})

	DynamoLockAcquireTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "nimbusnet_controlplane_dynamo_lock_acquire_total",
		Help: "Total DynamoDB lock acquisition attempts.",
	}, []string{"operation", "result"}) // result: acquired|contended|error

	// ── SLO Burn Rate ─────────────────────────────────────────────────────────
	// These feed the Google multi-window burn rate rules in Prometheus.
	// The SLO target is 99.9% availability (budget: 43.8 minutes/month).

	SLOBudgetConsumedFraction = promauto.NewGaugeVec(prometheus.GaugeOpts{
		Name: "nimbusnet_controlplane_slo_budget_consumed_fraction",
		Help: "Fraction of monthly error budget consumed [0, 1]. > 1.0 means budget exhausted.",
	}, []string{"region", "window"}) // window: 1h|6h|72h

	SLOAvailability = promauto.NewGaugeVec(prometheus.GaugeOpts{
		Name: "nimbusnet_controlplane_slo_availability",
		Help: "Current availability ratio [0, 1] for the rolling window.",
	}, []string{"region", "window"})

	// ── ML Signal Integration ─────────────────────────────────────────────────

	MLFailoverSignalsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "nimbusnet_controlplane_ml_failover_signals_total",
		Help: "Total ML failover signals received.",
	}, []string{"region", "acted_on"}) // acted_on: true|false

	MLSignalConfidenceHistogram = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "nimbusnet_controlplane_ml_signal_confidence",
		Help:    "Distribution of ML failover signal confidence values.",
		Buckets: []float64{0.5, 0.6, 0.65, 0.70, 0.75, 0.80, 0.90, 1.0},
	}, []string{"region"})

	// ── Control Plane Health ──────────────────────────────────────────────────

	ScoredMetricDropsTotal = promauto.NewCounter(prometheus.CounterOpts{
		Name: "nimbusnet_controlplane_scored_metric_drops_total",
		Help: "Scored metrics dropped because the ingestion channel was full.",
	})

	ProcessingLatencyHistogram = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "nimbusnet_controlplane_metric_processing_latency_seconds",
		Help:    "End-to-end latency from metric receipt to FSM action.",
		Buckets: prometheus.ExponentialBuckets(0.0001, 2, 14),
	})
)
