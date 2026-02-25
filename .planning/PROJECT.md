# Distributed Low-Latency Trading Engine

## What This Is

A distributed, low-latency trading engine deployed across FABRIC testbed nodes for CS451 (Spring 2026). The system ingests real-time and historically replayed market data, executes multiple trading strategies in parallel across distributed nodes, routes orders through a pre-trade risk gateway, and matches them in a full Limit Order Book (LOB) with price-time priority. As of v1.0, the core engine (feed → risk → LOB → strategies → dashboard) runs end-to-end on loopback. Next: integration test suite + FABRIC deployment + scaling benchmarks.

## Core Value

A working, benchmarked, production-pattern trading engine that proves scaling — tick-to-trade latency (p50/p95/p99) and throughput (orders/sec) measured rigorously as strategy nodes scale 1 → 8 on FABRIC.

## Current State (v1.0 shipped 2026-02-24)

- **Phases shipped:** 8 phases, 22 plans, ~4,681 LOC Python
- **What works:** Full local pipeline — feed handler (10K+ TPS), LOB matching (price-time priority), risk gateway (4 filters + kill switch), mean-reversion + ML signal strategies, Jupyter monitoring dashboard (real-time P&L/latency/depth + kill switch)
- **What's next:** Phase 9 (local integration + test suite) → Phase 10 (FABRIC benchmarks)

## Requirements

### Validated (v1.0)

- ✓ Market data feed handler: ZeroMQ PUB/SUB, protobuf ticks, 10K+ ticks/sec, 5 assets — v1.0
- ✓ Historical replay engine: Binance (ccxt) + Yahoo Finance data, configurable 1x/10x/100x speeds — v1.0
- ✓ Mean-reversion strategy: rolling z-score, buy z<-2 / sell z>+2, configurable lookback — v1.0
- ✓ ML signal strategy: gradient-boosted classifier (scikit-learn), 4 engineered features, confidence-scaled sizing — v1.0
- ✓ Pluggable strategy interface: abstract BaseStrategy class, no infra changes needed to add new strategies — v1.0
- ✓ Risk gateway: fat-finger filter ($100K notional), position limits, rate limiter, kill switch — v1.0
- ✓ Matching engine: price-time priority LOB (SortedDict), Limit/Market/Cancel order types, ring buffer ingestion — v1.0
- ✓ Execution reports: FIX-lite (NEW/FILL/PARTIAL/CANCEL/REJECT) published back to strategies + dashboard — v1.0
- ✓ Heartbeat / node health: ZeroMQ channel, cpu_pct, mem_pct, orders_processed per node — v1.0
- ✓ Jupyter monitoring dashboard: ipywidgets, real-time P&L, latency histograms, order book depth, kill switch button — v1.0
- ✓ 3 test scenarios: synthetic bull run (GBM), BTC crash May 2021 historical replay, stress test 50K ticks/sec — v1.0

### Active (v1.1 target)

- [ ] Scaling benchmarks: throughput + latency vs. node count (1/2/4/8), intra-site vs. cross-site comparison
- [ ] Fault injection test: kill one node at t=30s, verify system continues + dashboard detects dropout
- [ ] FABRIC deployment scripts: VM provisioning, dependency install, process orchestration
- [ ] Unit + integration tests: matching engine correctness, risk gateway filters, end-to-end order flow (full suite)
- [ ] Local-first dev: full system runnable locally with single `make run-local` command (orchestration script)

### Out of Scope

- C++ implementation — Python chosen deliberately for solo timeline; hot path designed with C++-style discipline
- Real money / live exchange connectivity — simulated/historical data only
- Mobile or web frontend — Jupyter dashboard is the only UI
- OAuth/auth layer for dashboard — academic project, no multi-user auth needed
- Persistent storage / database — in-memory state only (intentional for latency)

## Context

- **Platform:** FABRIC Testbed + JupyterHub (IIT Chicago, A20518537)
- **Course requirement:** CS451 requires scaling story (1→8 nodes), 3 I/O examples, 5-page doc, 2+ min recorded demo
- **Portfolio goal:** Demonstrate Jane Street / Citadel / Two Sigma-level systems thinking (LOB, FIX, latency discipline, risk-by-design, ML+rules hybrid) across quant SWE, quant dev, and distributed systems roles
- **Local-first dev:** Build and test locally using multi-process ZeroMQ on loopback; FABRIC used only for final benchmarks and recorded demo
- **Timeline:** 8 weeks (Weeks 1-2 foundation, 3-4 core engine, 5-6 scale+ML, 7-8 benchmark+polish)
- **Bonus target:** 5% bonus for Jupyter ipywidgets dashboard (shipped in v1.0)
- **Codebase:** ~4,681 LOC Python, 82 files (as of v1.0)

## Constraints

- **Language:** Python 3.11+ — fixed (FABRIC + Jupyter integration, solo timeline)
- **Messaging:** ZeroMQ (pyzmq) — fixed (course architecture requirement)
- **Serialization:** Protocol Buffers — fixed (FIX-lite over ZeroMQ)
- **Timeline:** 8 weeks solo — scope must be completable by one person
- **No per-tick heap allocations:** Hot path (LOB, ring buffer) must use pre-allocated structures (NumPy, deque(maxlen=N))
- **FABRIC access:** Required for final benchmark runs; local dev must not depend on it
- **FABRIC Python:** NOT pre-installed on FABRIC nodes — deployment scripts must install Python 3.11 via deadsnakes PPA or pyenv
- **Latency measurement:** Intra-node segment breakdown (recv→parse→signal→risk→match→report) is sufficient; no cross-node clock sync required
- **Data caching format:** CSV acceptable (no pyarrow/parquet dependency needed)
- **protobuf 5.x:** upb C extension wheel available on FABRIC CPU arch — use protobuf>=5.27.0

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Python over C++ | Solo timeline + Jupyter native; hot path compensated with C++-style discipline | ✓ Good — clean impl, fast iteration |
| ZeroMQ broker-less | Low latency, no broker bottleneck, maps to production HFT patterns | ✓ Good — zero broker overhead |
| protobuf over JSON | Compact binary encoding, schema enforcement, minimal serialization overhead | ✓ Good — C extension backend (upb) |
| sortedcontainers SortedDict for LOB | O(log N) price-level ops, pure Python, well-tested | ✓ Good — correct and fast enough |
| Ring buffer (deque maxlen) for ingestion | Decouples network I/O from matching logic (LMAX Disruptor pattern) | ✓ Good — no coordinated omission |
| Local-first development | Avoids FABRIC dependency during build; enables fast iteration | ✓ Good — entire v1.0 built on loopback |
| GBM synthetic + Binance historical + stress test | Covers trending/crash/high-load regimes; satisfies CS451 3-input requirement | ✓ Good — 3 datasets live |
| One multiprocessing.Process per logical node | Avoids GIL in ZeroMQ hot path; Context-after-fork invariant enforced | ✓ Good — clean process isolation |
| zmq_factory as sole socket creator | LINGER=100ms + HWM baked in; raw ctx.socket() banned | ✓ Good — no socket leaks |
| LOB built in isolation before ZeroMQ wiring | Correctness bugs separated from ZMQ bugs | ✓ Good — Phase 3 tests green immediately |
| CancelRequest routed via order_push (side=0 sentinel) | Avoids extra socket; RG routes based on sentinel | ✓ Good — minimal socket count |
| Kill switch uses separate _ks_ctx ZMQ context | Isolates push socket from poll context | ✓ Good — no context teardown race |
| models/ committed (not gitignored) | Grader convenience; re-run train_ml_model.py for real data | ⚠ Revisit — large artifact in repo |

---
*Last updated: 2026-02-24 after v1.0 milestone*
