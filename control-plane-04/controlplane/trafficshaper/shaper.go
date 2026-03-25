// Package trafficshaper implements the NimbusNet adaptive traffic shaper.
//
// This is the Maglev-inspired weighted ECMP layer. Traffic bleeds proportionally
// to degradation score — no binary flip. The bandit keeps a minimum exploration
// weight so we never lose signal on a region that might recover.
//
// Two-speed operation:
//   - Soft failover (DEGRADED state): traffic decays linearly over DegradedDecayPeriod
//   - Hard failover (FAILED state):   traffic snaps to MinimumWeight immediately
//
// Recovery:
//   - Traffic ramps back linearly over RecoveryRampPeriod when state → RECOVERED
//   - Full weight restored only when state → HEALTHY after observation window
//
// All weight changes are written to the VPC route table (Phase 4: route_table.go)
// and reported to the bandit (Phase 3) for online learning.

package trafficshaper

import (
	"context"
	"math"
	"sync"
	"time"

	"go.uber.org/zap"
)

// ─── Weight Policy ────────────────────────────────────────────────────────────

const (
	// MinimumWeight is the floor for all routing weights.
	// Even a FAILED region gets 5% of traffic to detect recovery.
	// The control plane can override to 0 for confirmed catastrophic failures.
	MinimumWeight = 0.05

	// FullWeight is the target weight for a fully healthy region.
	// In a 3-region deployment each region carries ~0.33 at steady state.
	// We store weights as absolute values [0,1]; normalisation happens at route table write.
	FullWeight = 1.0
)

// ─── Region Weight State ──────────────────────────────────────────────────────

// RegionWeight tracks the current routing weight and decay trajectory for one region.
type RegionWeight struct {
	Region string

	// Current routing weight [MinimumWeight, FullWeight]
	// Written to the VPC route table on every Tick().
	Current float64

	// Target weight the shaper is moving towards
	Target float64

	// Rate of change per second (positive = ramping up, negative = decaying)
	RatePerSec float64

	// Bandit recommendation (from Phase 3) — advisory, not enforced
	BanditWeight float64

	// Timestamps for rate-of-change calculation
	LastUpdatedAt time.Time

	// Whether this region is in hard-fail mode (immediate snap to minimum)
	HardFail bool
}

// ─── Traffic Shaper ───────────────────────────────────────────────────────────

// Config for the AdaptiveTrafficShaper.
type Config struct {
	// How long to decay traffic to MinimumWeight on soft failover (DEGRADED)
	DegradedDecayPeriod time.Duration // default: 2m

	// How long to ramp back to FullWeight on recovery (RECOVERED→HEALTHY)
	RecoveryRampPeriod time.Duration // default: 5m

	// Tick interval — how often to recalculate weights and write to route table
	TickInterval time.Duration // default: 5s

	// Minimum weight that any region can reach via decay
	// (can be overridden to 0.0 for confirmed total failures)
	MinimumWeight float64 // default: MinimumWeight (0.05)
}

func DefaultConfig() Config {
	return Config{
		DegradedDecayPeriod: 2 * time.Minute,
		RecoveryRampPeriod:  5 * time.Minute,
		TickInterval:        5 * time.Second,
		MinimumWeight:       MinimumWeight,
	}
}

// AdaptiveTrafficShaper manages routing weights for all regions.
// It translates state machine transitions into smooth traffic movements.
type AdaptiveTrafficShaper struct {
	cfg    Config
	log    *zap.Logger
	mu     sync.RWMutex
	regions map[string]*RegionWeight

	// Callback: fired when weights change — used to write to route table
	onWeightChange func(ctx context.Context, weights map[string]float64) error
}

// New creates a shaper with equal weights across all regions.
func New(cfg Config, regions []string, log *zap.Logger) *AdaptiveTrafficShaper {
	s := &AdaptiveTrafficShaper{
		cfg:     cfg,
		log:     log,
		regions: make(map[string]*RegionWeight, len(regions)),
	}

	initialWeight := FullWeight
	for _, r := range regions {
		s.regions[r] = &RegionWeight{
			Region:        r,
			Current:       initialWeight,
			Target:        initialWeight,
			BanditWeight:  1.0 / float64(len(regions)),
			LastUpdatedAt: time.Now(),
		}
	}
	return s
}

// OnWeightChange registers the callback that writes new weights to the route table.
func (s *AdaptiveTrafficShaper) OnWeightChange(fn func(ctx context.Context, weights map[string]float64) error) {
	s.onWeightChange = fn
}

// ─── State Machine Integration ────────────────────────────────────────────────

// OnDegraded begins a soft decay for the given region.
// Called by the FSM enter-DEGRADED action.
func (s *AdaptiveTrafficShaper) OnDegraded(region string) {
	s.mu.Lock()
	defer s.mu.Unlock()

	rw, ok := s.regions[region]
	if !ok {
		return
	}

	// Decay from current weight to MinimumWeight over DegradedDecayPeriod
	decayAmount := rw.Current - s.cfg.MinimumWeight
	rw.Target    = s.cfg.MinimumWeight
	rw.HardFail  = false

	if s.cfg.DegradedDecayPeriod > 0 {
		rw.RatePerSec = -decayAmount / s.cfg.DegradedDecayPeriod.Seconds()
	} else {
		rw.Current   = s.cfg.MinimumWeight
		rw.RatePerSec = 0
	}

	s.log.Info("Shaper: soft decay started",
		zap.String("region", region),
		zap.Float64("from", rw.Current),
		zap.Float64("to", rw.Target),
		zap.Duration("period", s.cfg.DegradedDecayPeriod),
	)
}

// OnFailed snaps a region to minimum weight immediately (hard failover).
// Called by the FSM enter-FAILED action.
func (s *AdaptiveTrafficShaper) OnFailed(region string) {
	s.mu.Lock()
	defer s.mu.Unlock()

	rw, ok := s.regions[region]
	if !ok {
		return
	}

	rw.Current    = s.cfg.MinimumWeight
	rw.Target     = s.cfg.MinimumWeight
	rw.RatePerSec = 0
	rw.HardFail   = true

	s.log.Warn("Shaper: hard failover — region snapped to minimum weight",
		zap.String("region", region),
		zap.Float64("weight", s.cfg.MinimumWeight),
	)
}

// OnRecovering begins ramping traffic back to full weight.
// Called by the FSM enter-RECOVERED action.
func (s *AdaptiveTrafficShaper) OnRecovering(region string) {
	s.mu.Lock()
	defer s.mu.Unlock()

	rw, ok := s.regions[region]
	if !ok {
		return
	}

	rw.Target    = FullWeight
	rw.HardFail  = false

	rampAmount := FullWeight - rw.Current
	if s.cfg.RecoveryRampPeriod > 0 && rampAmount > 0 {
		rw.RatePerSec = rampAmount / s.cfg.RecoveryRampPeriod.Seconds()
	} else {
		rw.Current    = FullWeight
		rw.RatePerSec = 0
	}

	s.log.Info("Shaper: recovery ramp started",
		zap.String("region", region),
		zap.Float64("from", rw.Current),
		zap.Float64("to", rw.Target),
		zap.Duration("period", s.cfg.RecoveryRampPeriod),
	)
}

// OnHealthy restores full weight immediately.
// Called by the FSM enter-HEALTHY action (after observation window).
func (s *AdaptiveTrafficShaper) OnHealthy(region string) {
	s.mu.Lock()
	defer s.mu.Unlock()

	rw, ok := s.regions[region]
	if !ok {
		return
	}

	rw.Current    = FullWeight
	rw.Target     = FullWeight
	rw.RatePerSec = 0
	rw.HardFail   = false

	s.log.Info("Shaper: region restored to full weight",
		zap.String("region", region),
		zap.Float64("weight", FullWeight),
	)
}

// UpdateBanditWeights receives the latest bandit recommendations from Phase 3.
// These are advisory — they influence weight targets but don't override FSM state.
func (s *AdaptiveTrafficShaper) UpdateBanditWeights(weights map[string]float64) {
	s.mu.Lock()
	defer s.mu.Unlock()

	for region, w := range weights {
		if rw, ok := s.regions[region]; ok {
			rw.BanditWeight = w
			// If the region is healthy and the bandit recommends < full weight,
			// smoothly adjust the target (don't override hard failovers)
			if !rw.HardFail && rw.Target == FullWeight && w < 0.8 {
				rw.Target     = math.Max(w, s.cfg.MinimumWeight)
				rw.RatePerSec = (rw.Target - rw.Current) / 30.0 // 30s adjustment
			}
		}
	}
}

// ForceWeight overrides a region's weight directly (SRE manual control).
// Setting to 0.0 completely removes a region from rotation.
func (s *AdaptiveTrafficShaper) ForceWeight(region string, weight float64) {
	s.mu.Lock()
	defer s.mu.Unlock()

	rw, ok := s.regions[region]
	if !ok {
		return
	}

	rw.Current    = math.Max(weight, 0.0)
	rw.Target     = rw.Current
	rw.RatePerSec = 0
	rw.HardFail   = weight <= 0.0

	s.log.Warn("Shaper: manual weight override",
		zap.String("region", region),
		zap.Float64("weight", weight),
	)
}

// ─── Tick Loop ────────────────────────────────────────────────────────────────

// Run starts the tick loop. Advances weights on every tick and writes to route table.
// Blocks until ctx is cancelled.
func (s *AdaptiveTrafficShaper) Run(ctx context.Context) error {
	ticker := time.NewTicker(s.cfg.TickInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			if err := s.tick(ctx); err != nil {
				s.log.Error("Shaper: tick failed", zap.Error(err))
				// Non-fatal — continue ticking. Route table write failure is logged
				// and reported to Prometheus; the next tick will retry.
			}
		}
	}
}

func (s *AdaptiveTrafficShaper) tick(ctx context.Context) error {
	s.mu.Lock()
	changed := false
	elapsed := s.cfg.TickInterval.Seconds()

	for _, rw := range s.regions {
		if rw.RatePerSec == 0 {
			continue
		}

		// Advance weight towards target
		newWeight := rw.Current + rw.RatePerSec*elapsed

		// Clamp to [MinimumWeight, FullWeight] and snap when close to target
		if rw.RatePerSec < 0 {
			// Decaying
			newWeight = math.Max(newWeight, rw.Target)
			if math.Abs(newWeight-rw.Target) < 0.001 {
				newWeight     = rw.Target
				rw.RatePerSec = 0
			}
		} else {
			// Ramping
			newWeight = math.Min(newWeight, rw.Target)
			if math.Abs(newWeight-rw.Target) < 0.001 {
				newWeight     = rw.Target
				rw.RatePerSec = 0
			}
		}

		if math.Abs(newWeight-rw.Current) > 0.001 {
			rw.Current     = newWeight
			rw.LastUpdatedAt = time.Now()
			changed = true
		}
	}
	s.mu.Unlock()

	if !changed || s.onWeightChange == nil {
		return nil
	}

	return s.onWeightChange(ctx, s.NormalisedWeights())
}

// ─── Weight Queries ───────────────────────────────────────────────────────────

// NormalisedWeights returns weights normalised to sum to 1.0.
// These are the values written to the VPC route table as ECMP weights.
func (s *AdaptiveTrafficShaper) NormalisedWeights() map[string]float64 {
	s.mu.RLock()
	defer s.mu.RUnlock()

	total := 0.0
	for _, rw := range s.regions {
		total += rw.Current
	}

	result := make(map[string]float64, len(s.regions))
	for r, rw := range s.regions {
		if total > 0 {
			result[r] = rw.Current / total
		} else {
			result[r] = 1.0 / float64(len(s.regions))
		}
	}
	return result
}

// RawWeights returns the un-normalised weights for diagnostics.
func (s *AdaptiveTrafficShaper) RawWeights() map[string]RegionWeight {
	s.mu.RLock()
	defer s.mu.RUnlock()

	result := make(map[string]RegionWeight, len(s.regions))
	for r, rw := range s.regions {
		result[r] = *rw
	}
	return result
}
