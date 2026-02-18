# Distributed Low-Latency Trading Engine

## What This Is

A distributed, low-latency trading engine deployed across FABRIC testbed nodes for CS451 (Spring 2026). The system ingests real-time and historically replayed market data, executes multiple trading strategies in parallel across distributed nodes, routes orders through a pre-trade risk gateway, and matches them in a full Limit Order Book (LOB) with price-time priority. Designed as both an academic deliverable and a portfolio artifact targeting quant SWE / quant dev / distributed systems roles.

## Core Value

A working, benchmarked, production-pattern trading engine that proves scaling — tick-to-trade latency (p50/p95/p99) and throughput (orders/sec) measured rigorously as strategy nodes scale 1 → 8 on FABRIC.

## Requirements

### Validated

(None yet — ship to validate)

### Active

- [ ] Market data feed handler: ZeroMQ PUB/SUB, protobuf ticks, 10K+ ticks/sec, 5 assets (BTC, ETH, AAPL, MSFT, SPY)
- [ ] Historical replay engine: Binance (ccxt) + Yahoo Finance data, configurable 1x/10x/100x speeds
- [ ] Mean-reversion strategy: rolling z-score, buy z<-2 / sell z>+2, configurable lookback
- [ ] ML signal strategy: gradient-boosted classifier (scikit-learn), 4 engineered features, confidence-scaled sizing
- [ ] Pluggable strategy interface: abstract BaseStrategy class, no infra changes needed to add new strategies
- [ ] Risk gateway: fat-finger filter ($100K notional), position limits, rate limiter, kill switch
- [ ] Matching engine: price-time priority LOB (SortedDict), Limit/Market/Cancel order types, ring buffer ingestion
- [ ] Execution reports: FIX-lite (NEW/FILL/PARTIAL/CANCEL/REJECT) published back to strategies + dashboard
- [ ] Heartbeat / node health: ZeroMQ channel, cpu_pct, mem_pct, orders_processed per node
- [ ] Jupyter monitoring dashboard: ipywidgets, real-time P&L, latency histograms, order book depth, kill switch button
- [ ] Scaling benchmarks: throughput + latency vs. node count (1/2/4/8), intra-site vs. cross-site comparison
- [ ] Fault injection test: kill one node at t=30s, verify system continues + dashboard detects dropout
- [ ] 3 test scenarios: synthetic bull run (GBM), BTC crash May 2021 historical replay, stress test 50K ticks/sec
- [ ] FABRIC deployment scripts: VM provisioning, dependency install, process orchestration
- [ ] Unit + integration tests: matching engine correctness, risk gateway filters, end-to-end order flow
- [ ] Local-first dev: full system runnable locally with mock multi-process ZeroMQ nodes before FABRIC deployment

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
- **Bonus target:** 5% bonus for Jupyter ipywidgets dashboard

## Constraints

- **Language:** Python 3.11+ — fixed (FABRIC + Jupyter integration, solo timeline)
- **Messaging:** ZeroMQ (pyzmq) — fixed (course architecture requirement)
- **Serialization:** Protocol Buffers — fixed (FIX-lite over ZeroMQ)
- **Timeline:** 8 weeks solo — scope must be completable by one person
- **No per-tick heap allocations:** Hot path (LOB, ring buffer) must use pre-allocated structures (NumPy, deque(maxlen=N))
- **FABRIC access:** Required for final benchmark runs; local dev must not depend on it

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Python over C++ | Solo timeline + Jupyter native; hot path compensated with C++-style discipline | — Pending |
| ZeroMQ broker-less | Low latency, no broker bottleneck, maps to production HFT patterns | — Pending |
| protobuf over JSON | Compact binary encoding, schema enforcement, minimal serialization overhead | — Pending |
| sortedcontainers SortedDict for LOB | O(log N) price-level ops, pure Python, well-tested | — Pending |
| Ring buffer (deque maxlen) for ingestion | Decouples network I/O from matching logic (LMAX Disruptor pattern) | — Pending |
| Local-first development | Avoids FABRIC dependency during build; enables fast iteration | — Pending |
| GBM synthetic + Binance historical + stress test | Covers trending/crash/high-load regimes; satisfies CS451 3-input requirement | — Pending |

---
*Last updated: 2026-02-17 after initialization*
