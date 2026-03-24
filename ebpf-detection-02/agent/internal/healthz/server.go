// Package healthz implements the /healthz HTTP endpoint.
//
// This is the DNS poisoning mechanism described in the NimbusNet architecture:
// when the control plane initiates a region drain, it calls SetDraining(true),
// which causes /healthz to return 503. Route 53 health checks poll this endpoint
// every 10 seconds and stop routing traffic to the region once it sees 503.
//
// The VPC route table is updated immediately (data-plane fast path).
// Route 53 convergence takes up to 30 seconds (DNS TTL + health check interval).
// The drain delay bridges this gap — the agent stays alive until R53 has caught up.
//
// Endpoint contract:
//
//	GET /healthz      → 200 {"status":"ok","draining":false}
//	                  → 503 {"status":"draining","draining":true}
//	GET /healthz/live → 200 always (Kubernetes liveness, not R53)
//	GET /healthz/ready→ same as /healthz (R53 + k8s readiness)

package healthz

import (
	"context"
	"encoding/json"
	"net/http"
	"sync/atomic"
	"time"

	"go.uber.org/zap"
)

// Config for the health check server.
type Config struct {
	ListenAddr  string
	DrainDelay  time.Duration // how long to wait after SetDraining before shutdown
}

// Server serves /healthz and exposes a drain control.
type Server struct {
	cfg      Config
	log      *zap.Logger
	draining atomic.Bool
}

// NewServer creates a health check server.
func NewServer(cfg Config, log *zap.Logger) *Server {
	if cfg.ListenAddr == "" {
		cfg.ListenAddr = ":8080"
	}
	if cfg.DrainDelay == 0 {
		cfg.DrainDelay = 35 * time.Second
	}
	return &Server{cfg: cfg, log: log}
}

// SetDraining puts the server into draining mode.
// /healthz will return 503 after this call.
// Route 53 will stop sending traffic within one health check interval (~30s).
func (s *Server) SetDraining(d bool) {
	s.draining.Store(d)
	if d {
		s.log.Info("healthz: entering drain mode — /healthz will return 503",
			zap.Duration("drain_delay", s.cfg.DrainDelay),
		)
	}
}

// Serve starts the HTTP server. Blocks until ctx is cancelled.
func (s *Server) Serve(ctx context.Context) {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.handleHealthz)
	mux.HandleFunc("/healthz/live", s.handleLiveness)
	mux.HandleFunc("/healthz/ready", s.handleHealthz)

	srv := &http.Server{
		Addr:         s.cfg.ListenAddr,
		Handler:      mux,
		ReadTimeout:  5 * time.Second,
		WriteTimeout: 5 * time.Second,
	}

	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		srv.Shutdown(shutdownCtx)
	}()

	s.log.Info("healthz server listening", zap.String("addr", s.cfg.ListenAddr))
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		s.log.Error("healthz server error", zap.Error(err))
	}
}

type healthResponse struct {
	Status   string `json:"status"`
	Draining bool   `json:"draining"`
	// Timestamps help Route 53 log correlation
	Timestamp string `json:"timestamp"`
}

func (s *Server) handleHealthz(w http.ResponseWriter, r *http.Request) {
	draining := s.draining.Load()
	resp := healthResponse{
		Draining:  draining,
		Timestamp: time.Now().UTC().Format(time.RFC3339),
	}

	if draining {
		resp.Status = "draining"
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusServiceUnavailable) // 503
	} else {
		resp.Status = "ok"
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK) // 200
	}

	json.NewEncoder(w).Encode(resp)
}

// handleLiveness always returns 200 — even during drain the process is alive.
// Kubernetes should not restart a draining pod.
func (s *Server) handleLiveness(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(map[string]string{
		"status":    "alive",
		"timestamp": time.Now().UTC().Format(time.RFC3339),
	})
}
