# NimbusNet

> Autonomous global cloud network engineering platform — self-healing, ML-driven, chaos-validated.

[![Phase](https://img.shields.io/badge/current%20phase-1%20of%206-blue)](#phases)
[![AWS](https://img.shields.io/badge/AWS-multi--region-orange)](https://aws.amazon.com)
[![Terraform](https://img.shields.io/badge/Terraform-1.7%2B-purple)](https://terraform.io)
[![Go](https://img.shields.io/badge/Go-1.22%2B-00ADD8)](https://golang.org)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)

---

## What is NimbusNet?

NimbusNet is a production-grade autonomous network platform spanning four AWS regions. It detects
congestion at kernel level using eBPF/XDP probes, heals routing in sub-second time via a formally
specified Go state machine, predicts failures before they happen using four ML models, validates
every reliability claim with a chaos engineering suite that blocks CI merges on regressions, and
operates autonomously — paging an SRE only when the system itself has exhausted its remediation budget.

Every architectural decision is justified. Every component is production-grade.

---

## Architecture at a glance

```
┌────────────────────────────────────────────────────────────────────┐
│                     Global control plane                           │
│         Route 53 · consistent-hash ring · SLO burn-rate engine     │
└────────────┬───────────────────────────────────┬───────────────────┘
             │                                   │
  ┌──────────▼──────────┐             ┌──────────▼──────────┐
  │     US-East-1        │◄──────────►│     EU-West-1        │  ← Active-active
  │  XDP · Go agent      │            │  XDP · Go agent      │
  │  ML runtime (ONNX)   │            │  ML runtime (ONNX)   │
  └─────────────────────┘             └─────────────────────┘
             │                                   │
  ┌──────────▼──────────┐             ┌──────────▼──────────┐
  │    AP-Southeast-1    │             │      SA-East-1       │  ← Cold standby
  │  Terraform on-demand │             │  Terraform on-demand │
  └─────────────────────┘             └─────────────────────┘

Observability:  Prometheus · Loki · Tempo · Grafana · Alertmanager
SRE loop:       Auto-runbook → postmortem → ML retrain → escalate only on TTL breach
```

---

## Phases

| Phase | Name | Status |
|-------|------|--------|
| 1 | Foundation — multi-region Terraform, Route 53, VPC, monitoring skeleton | ✅ Current |
| 2 | Detection layer — eBPF/XDP probes, Go agent, telemetry bus | Upcoming |
| 3 | Intelligence layer — Isolation Forest, LSTM, XGBoost, bandit, ONNX | Upcoming |
| 4 | Healing engine — state machine, route arbiter, consistent-hash ring | Upcoming |
| 5 | Chaos + SLO — chaos CI gate, GameDay suite, multi-window burn rate | Upcoming |
| 6 | SRE operations — runbooks, postmortem engine, escalation pipeline | Upcoming |

---

## Repository layout

```
nimbusnet/
├── README.md                     ← You are here
├── DOCUMENTATION.md              ← Full system architecture reference
├── docker-compose.yml            ← Local observability stack
├── docs/
│   ├── architecture/
│   ├── runbooks/
│   ├── postmortems/
│   └── slo/
├── phase-01-foundation/
├── phase-02-detection/           ← Added in Phase 2
├── phase-03-intelligence/        ← Added in Phase 3
├── phase-04-healing/             ← Added in Phase 4
├── phase-05-chaos-slo/           ← Added in Phase 5
└── phase-06-sre-operations/      ← Added in Phase 6
```

---

## Key design decisions

**Active-active US + EU, cold standby AP + SA** — Active-active gives zero-RPO failover between
primary regions. AP and SA stay cold because XGBoost pre-warm prediction fires a Terraform run
~15 minutes before they are needed, converting cold standby into effective warm standby without
the continuous cost.

**eBPF/XDP over CloudWatch for detection** — CloudWatch polls every 10–30 seconds from outside
the region. XDP hooks see packets before the kernel processes them — detection is in milliseconds.
The two are complementary: XDP catches micro-congestion instantly, CloudWatch feeds Route 53 DNS
failover as the external-visibility layer.

**Consistent hashing on the control plane** — Two active agents can see the same telemetry.
A Dynamo-style hash ring assigns each flow key to exactly one agent — competing AWS API calls
and split-brain are impossible by construction, not by coordination.

**Google multi-window burn rate** — Single-threshold alerting misses slow burns and floods on
transient spikes. The 14.4×/6×/1× window pair catches fast burns, slow burns, and silent leaks —
three failure modes requiring three different responses.

**Autonomous runbooks before SRE escalation** — Auto-remediation handles the 80% of incidents
matching a known pattern. The SRE is brought in only when the system is genuinely exhausted,
handed a full incident context and postmortem skeleton rather than a raw alert at 3am.

---

## SLOs

| SLI | Target | Window |
|-----|--------|--------|
| Availability | 99.95% | 28-day rolling |
| Healing latency p99 | < 2s | Per incident |
| Fast burn rate | < 14.4× | 1h long / 5m short |
| Slow burn rate | < 6× | 6h long / 30m short |
| Split-brain events | 0 | All time |

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| Infrastructure | Terraform 1.7 · Terragrunt · AWS |
| Detection | eBPF/XDP · Go 1.22 |
| Intelligence | Python 3.11 · scikit-learn · PyTorch · ONNX · MLflow |
| Healing agent | Go 1.22 · AWS SDK v2 |
| Observability | Prometheus · Loki · Tempo · Grafana · Alertmanager |
| Chaos | tc qdisc · Go chaos runner · GitHub Actions |
| SRE ops | PagerDuty · Slack · auto-postmortem (Go) |

---

## Prerequisites

- AWS CLI with appropriate IAM permissions
- Terraform >= 1.7.0 and Terragrunt >= 0.55.0
- Go >= 1.22.0
- Python >= 3.11.0
- Docker + Docker Compose
- Linux kernel >= 5.15 (eBPF/XDP — required from Phase 2)

---

## Quick start

```bash
git clone https://github.com/yourorg/nimbusnet.git
cd nimbusnet

# Start local observability stack
docker compose up -d

# Bootstrap Phase 1 — US-East-1 first
cd phase-01-foundation
./scripts/bootstrap.sh us-east-1

# Verify
./scripts/verify.sh
```
