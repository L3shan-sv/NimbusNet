// Package statemachine implements the NimbusNet healing state machine.
//
// States:
//
//	HEALTHY    → Normal operation. Full traffic. No action.
//	DEGRADED   → Anomaly confirmed. Traffic bleeding to peers. Monitoring heightened.
//	HEALING    → Remediation in progress (runbook executing). Traffic at minimum weight.
//	RECOVERED  → Healing verified. Traffic ramping back. 60s observation window.
//	FAILED     → Healing exhausted TTL without resolution. SRE paged. Human required.
//
// Every transition has:
//   - A guard condition (what must be true for the transition to fire)
//   - A timeout (how long before escalating if the condition doesn't resolve)
//   - A rollback path (what to do if the transition makes things worse)
//
// The state machine is the enforcement layer for the SLO budget.
// It consumes MLScoredMetric from Phase 3 and produces
// healing actions for Route53 and the VPC route table.
//
// Split-brain safety: the consistent hash ring (Phase 2) guarantees
// only one agent owns a given flow. The DynamoDB conditional write
// ensures only one state machine holds the region lock at a time.

package statemachine

import (
	"context"
	"fmt"
	"sync"
	"time"

	"go.uber.org/zap"
)

// ─── States ───────────────────────────────────────────────────────────────────

type State int

const (
	StateHealthy   State = iota // 0 — full traffic, all clear
	StateDegraded               // 1 — anomaly confirmed, traffic bleeding
	StateHealing                // 2 — runbook executing, minimum traffic weight
	StateRecovered              // 3 — healing verified, ramping back
	StateFailed                 // 4 — TTL exhausted, SRE paged
)

func (s State) String() string {
	return [...]string{"HEALTHY", "DEGRADED", "HEALING", "RECOVERED", "FAILED"}[s]
}

// ─── Events ───────────────────────────────────────────────────────────────────

type EventType int

const (
	EventAnomalyDetected    EventType = iota // ML scored: should_trigger_failover=true
	EventSeverityCritical                    // XGBoost: severity=CRITICAL
	EventRunbookStarted                      // runbook executor began
	EventRunbookSucceeded                    // runbook verified healing
	EventRunbookFailed                       // runbook exhausted retries
	EventHealthVerified                      // RTT/retransmit back to baseline for 60s
	EventTTLExpired                          // state timeout exceeded
	EventManualOverride                      // SRE forced state change
	EventRecoveryRollback                    // RECOVERED→DEGRADED: metrics worsened again
)

func (e EventType) String() string {
	return [...]string{
		"ANOMALY_DETECTED", "SEVERITY_CRITICAL", "RUNBOOK_STARTED",
		"RUNBOOK_SUCCEEDED", "RUNBOOK_FAILED", "HEALTH_VERIFIED",
		"TTL_EXPIRED", "MANUAL_OVERRIDE", "RECOVERY_ROLLBACK",
	}[e]
}

type Event struct {
	Type      EventType
	Region    string
	Timestamp time.Time
	Payload   map[string]any
}

// ─── Transition Actions ───────────────────────────────────────────────────────

// Action is a callback fired when a transition completes.
// Phase 4 wires these to Route53, VPC route table, runbook executor, and PagerDuty.
type Action func(ctx context.Context, from, to State, region string, payload map[string]any) error

// ─── State Machine ────────────────────────────────────────────────────────────

// RegionFSM is the healing state machine for a single AWS region.
// One instance per region per agent (3 total in a 3-region deployment).
type RegionFSM struct {
	region string
	log    *zap.Logger
	mu     sync.RWMutex

	current     State
	enteredAt   time.Time
	transitions []TransitionRecord

	// Timeouts per state (how long before escalating)
	timeouts map[State]time.Duration

	// Actions wired in by the control plane
	onEnter map[State][]Action // fired when entering a state
	onExit  map[State][]Action // fired when leaving a state

	// Rollback: if RECOVERED re-detects anomaly within this window, go back to DEGRADED
	recoveryObservationWindow time.Duration

	// Metrics
	failoverCount   int
	lastFailoverAt  time.Time
}

type TransitionRecord struct {
	From      State
	To        State
	Event     EventType
	Timestamp time.Time
	Duration  time.Duration // time spent in the From state
}

// Config for a RegionFSM.
type Config struct {
	Region string

	// How long to stay in each state before escalating
	DegradedTimeout  time.Duration // default: 5m
	HealingTimeout   time.Duration // default: 10m
	RecoveredTimeout time.Duration // default: 60s (observation window before HEALTHY)
	FailedTimeout    time.Duration // default: 30m (before auto-retry from FAILED)

	// How long in RECOVERED before a new anomaly triggers rollback to DEGRADED (not HEALTHY)
	RecoveryObservationWindow time.Duration // default: 60s
}

func DefaultConfig(region string) Config {
	return Config{
		Region:                    region,
		DegradedTimeout:           5 * time.Minute,
		HealingTimeout:            10 * time.Minute,
		RecoveredTimeout:          60 * time.Second,
		FailedTimeout:             30 * time.Minute,
		RecoveryObservationWindow: 60 * time.Second,
	}
}

// New creates a RegionFSM starting in HEALTHY state.
func New(cfg Config, log *zap.Logger) *RegionFSM {
	fsm := &RegionFSM{
		region:    cfg.Region,
		log:       log,
		current:   StateHealthy,
		enteredAt: time.Now(),
		onEnter:   make(map[State][]Action),
		onExit:    make(map[State][]Action),
		timeouts: map[State]time.Duration{
			StateDegraded:  cfg.DegradedTimeout,
			StateHealing:   cfg.HealingTimeout,
			StateRecovered: cfg.RecoveredTimeout,
			StateFailed:    cfg.FailedTimeout,
		},
		recoveryObservationWindow: cfg.RecoveryObservationWindow,
	}
	return fsm
}

// ─── Action Registration ──────────────────────────────────────────────────────

// OnEnter registers an action to fire when entering a state.
func (fsm *RegionFSM) OnEnter(s State, action Action) {
	fsm.onEnter[s] = append(fsm.onEnter[s], action)
}

// OnExit registers an action to fire when leaving a state.
func (fsm *RegionFSM) OnExit(s State, action Action) {
	fsm.onExit[s] = append(fsm.onExit[s], action)
}

// ─── Transition Logic ─────────────────────────────────────────────────────────

// Send processes an event and fires the appropriate transition if valid.
// Returns an error if the event is invalid for the current state.
func (fsm *RegionFSM) Send(ctx context.Context, event Event) error {
	fsm.mu.Lock()
	defer fsm.mu.Unlock()

	next, ok := fsm.nextState(fsm.current, event.Type)
	if !ok {
		fsm.log.Debug("FSM: event ignored in current state",
			zap.String("region", fsm.region),
			zap.String("state", fsm.current.String()),
			zap.String("event", event.Type.String()),
		)
		return nil
	}

	return fsm.transition(ctx, next, event)
}

// CheckTimeouts inspects whether any state TTL has expired and fires TTL escalation.
// Call this on a ticker (e.g. every 10s) from the control plane main loop.
func (fsm *RegionFSM) CheckTimeouts(ctx context.Context) error {
	fsm.mu.Lock()
	defer fsm.mu.Unlock()

	timeout, hasTimeout := fsm.timeouts[fsm.current]
	if !hasTimeout {
		return nil
	}

	if time.Since(fsm.enteredAt) < timeout {
		return nil
	}

	fsm.log.Warn("FSM: state TTL expired — escalating",
		zap.String("region", fsm.region),
		zap.String("state", fsm.current.String()),
		zap.Duration("timeout", timeout),
		zap.Duration("elapsed", time.Since(fsm.enteredAt)),
	)

	ttlEvent := Event{
		Type:      EventTTLExpired,
		Region:    fsm.region,
		Timestamp: time.Now(),
	}

	next, ok := fsm.nextState(fsm.current, EventTTLExpired)
	if !ok {
		return nil
	}
	return fsm.transition(ctx, next, ttlEvent)
}

// Current returns the current state (safe for concurrent reads).
func (fsm *RegionFSM) Current() State {
	fsm.mu.RLock()
	defer fsm.mu.RUnlock()
	return fsm.current
}

// TimeInState returns how long the FSM has been in its current state.
func (fsm *RegionFSM) TimeInState() time.Duration {
	fsm.mu.RLock()
	defer fsm.mu.RUnlock()
	return time.Since(fsm.enteredAt)
}

// TransitionHistory returns a copy of all recorded transitions.
func (fsm *RegionFSM) TransitionHistory() []TransitionRecord {
	fsm.mu.RLock()
	defer fsm.mu.RUnlock()
	result := make([]TransitionRecord, len(fsm.transitions))
	copy(result, fsm.transitions)
	return result
}

// ─── Transition Table ─────────────────────────────────────────────────────────
//
// This is the formal specification of all valid transitions.
// Any (current_state, event) pair not in this table is silently ignored.

func (fsm *RegionFSM) nextState(current State, event EventType) (State, bool) {
	type key struct {
		state State
		event EventType
	}

	table := map[key]State{
		// ── From HEALTHY ──────────────────────────────────────────────────────
		{StateHealthy, EventAnomalyDetected}: StateDegraded,
		{StateHealthy, EventSeverityCritical}: StateDegraded,
		{StateHealthy, EventManualOverride}: StateDegraded,

		// ── From DEGRADED ─────────────────────────────────────────────────────
		{StateDegraded, EventRunbookStarted}: StateHealing,
		{StateDegraded, EventTTLExpired}: StateHealing,     // auto-start runbook on TTL
		{StateDegraded, EventManualOverride}: StateFailed,  // SRE can force-fail

		// ── From HEALING ──────────────────────────────────────────────────────
		{StateHealing, EventRunbookSucceeded}: StateRecovered,
		{StateHealing, EventRunbookFailed}: StateFailed,
		{StateHealing, EventTTLExpired}: StateFailed,      // healing took too long
		{StateHealing, EventManualOverride}: StateFailed,

		// ── From RECOVERED ────────────────────────────────────────────────────
		{StateRecovered, EventHealthVerified}: StateHealthy,    // observation window passed
		{StateRecovered, EventRecoveryRollback}: StateDegraded, // metrics worsened again
		{StateRecovered, EventAnomalyDetected}: StateDegraded,  // new anomaly during observation
		{StateRecovered, EventTTLExpired}: StateHealthy,        // observation window elapsed

		// ── From FAILED ───────────────────────────────────────────────────────
		{StateFailed, EventManualOverride}: StateHealthy,  // SRE marks resolved
		{StateFailed, EventTTLExpired}: StateDegraded,     // auto-retry after 30m
	}

	next, ok := table[key{current, event}]
	return next, ok
}

// ─── Internal Transition ──────────────────────────────────────────────────────

func (fsm *RegionFSM) transition(ctx context.Context, next State, event Event) error {
	from := fsm.current
	duration := time.Since(fsm.enteredAt)

	fsm.log.Info("FSM: transition",
		zap.String("region", fsm.region),
		zap.String("from", from.String()),
		zap.String("to", next.String()),
		zap.String("event", event.Type.String()),
		zap.Duration("time_in_state", duration),
	)

	// Fire exit actions for current state
	for _, action := range fsm.onExit[from] {
		if err := action(ctx, from, next, fsm.region, event.Payload); err != nil {
			fsm.log.Error("FSM: exit action failed",
				zap.String("from", from.String()),
				zap.Error(err),
			)
			// Exit action failures are logged but don't block the transition
		}
	}

	// Record transition
	fsm.transitions = append(fsm.transitions, TransitionRecord{
		From:      from,
		To:        next,
		Event:     event.Type,
		Timestamp: event.Timestamp,
		Duration:  duration,
	})

	// Update state
	fsm.current   = next
	fsm.enteredAt = time.Now()

	if next == StateDegraded || next == StateFailed {
		fsm.failoverCount++
		fsm.lastFailoverAt = time.Now()
	}

	// Fire enter actions for new state
	for _, action := range fsm.onEnter[next] {
		if err := action(ctx, from, next, fsm.region, event.Payload); err != nil {
			fsm.log.Error("FSM: enter action failed",
				zap.String("to", next.String()),
				zap.Error(err),
			)
			return fmt.Errorf("enter action for %s failed: %w", next, err)
		}
	}

	return nil
}

// ─── Metrics ─────────────────────────────────────────────────────────────────

// Status returns a snapshot of the FSM for Prometheus metrics and health checks.
type Status struct {
	Region        string
	State         string
	TimeInState   time.Duration
	FailoverCount int
	LastFailoverAt time.Time
	Transitions   int
}

func (fsm *RegionFSM) Status() Status {
	fsm.mu.RLock()
	defer fsm.mu.RUnlock()
	return Status{
		Region:        fsm.region,
		State:         fsm.current.String(),
		TimeInState:   time.Since(fsm.enteredAt),
		FailoverCount: fsm.failoverCount,
		LastFailoverAt: fsm.lastFailoverAt,
		Transitions:   len(fsm.transitions),
	}
}
