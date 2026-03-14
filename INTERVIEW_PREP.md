# Distributed Trading Engine — Interview Preparation Guide

---

## Table of Contents

- [[#The 30-Second Elevator Pitch]]
- [[#The 2-Minute Technical Overview]]
- [[#Architecture Deep Dive]]
- [[#Component-by-Component Breakdown]]
- [[#Key Design Decisions (What to Emphasize)]]
- [[#Performance Numbers (Memorize These)]]
- [[#Design Patterns Used]]
- [[#What Makes This Project Impressive]]
- [[#Common Interview Questions & Killer Answers]]
- [[#Weakness Acknowledgment (Own These Before They Ask)]]
- [[#Buzzwords to Drop Naturally]]
- [[#System Design Whiteboard Walkthrough]]

---

## The 30-Second Elevator Pitch

> "I built a distributed, low-latency trading engine from scratch — a multi-process system where a feed handler, matching engine, risk gateway, and strategy nodes communicate over ZeroMQ using Protocol Buffers. It processes 50,000+ market data ticks per second, implements a price-time priority limit order book with O(1) lazy cancellation, has a 4-layer pre-trade risk firewall, and supports pluggable strategies including an ML-based signal model. The entire system is brokerless — no Kafka, no RabbitMQ — just raw ZeroMQ for sub-millisecond IPC. I designed it to be horizontally scalable: you can spin up new strategy nodes with zero reconfiguration of other components."

---

## The 2-Minute Technical Overview

Say this when they ask "Tell me about your project":

> "This is a **multi-process distributed trading system** — each component runs as its own OS process, communicating exclusively through ZeroMQ over TCP.
>
> The **Feed Handler** generates synthetic market data using Geometric Brownian Motion at a rate-gated 10,000 ticks/sec — or 50K+ in stress mode. It publishes via ZeroMQ PUB/SUB so any number of consumers can subscribe without the publisher knowing about them.
>
> **Strategy nodes** subscribe to market data, compute trading signals, and submit orders through a **Risk Gateway** that acts as a pre-trade firewall — it checks fat-finger limits, position limits, rate limits via a token bucket, and has a kill switch for emergency shutdown.
>
> Approved orders flow into the **Matching Engine**, which hosts a **limit order book per symbol** using sorted containers with deque-based FIFO queues at each price level. It uses a ring buffer — inspired by the LMAX Disruptor pattern — to decouple network I/O from the matching thread. Execution reports are published back to strategies using topic-filtered PUB/SUB.
>
> All messages are serialized with Protocol Buffers, all prices are integer ticks to avoid float comparison bugs, and every component is independently testable — the LOB has 20+ unit tests, the integration tests spawn real subprocesses with real ZeroMQ sockets, and the end-to-end test runs the full 5-component pipeline."

---

## Architecture Deep Dive

### Process Topology

```
Feed Handler (PUB :5555, PUB :5557 heartbeat)
     |
     | MarketDataTick (protobuf, PUB/SUB)
     v
Strategy Node(s)  ──PUSH──>  Risk Gateway (PULL :5558)
                                   |
                                   | approved NOS -> PUSH
                                   v
                          Matching Engine (PULL :5559, PULL :5562 cancel)
                                   |
                                   | ExecutionReport (PUB :5556, topic=strategy_id)
                                   v
                 Strategy Nodes + Dashboard (SUB, topic-filtered)
```

### Port Map (10 dedicated ports)

| Port | Socket | Purpose |
|------|--------|---------|
| 5555 | PUB/SUB | Market data feed |
| 5556 | PUB/SUB | Execution reports (topic = strategy_id) |
| 5557 | PUB/SUB | Feed heartbeat |
| 5558 | PUSH/PULL | Strategy -> Risk Gateway (order ingress) |
| 5559 | PUSH/PULL | Risk Gateway -> Matching Engine (order egress) |
| 5560 | PUSH/PULL | Dashboard -> Risk Gateway (kill switch) |
| 5561 | PUB/SUB | Risk rejects from RG |
| 5562 | PUSH/PULL | Cancel requests to ME |
| 5563 | PUB/SUB | ME heartbeat |
| 5564 | PUB/SUB | RG heartbeat |

### Why This Topology Impresses

- **Server-side always binds, client-side always connects** — this means adding new strategy nodes requires ZERO reconfiguration of other components. Just `connect()` to the known addresses. This is the "service discovery" pattern without a service registry.
- **Separation of data planes**: market data (PUB/SUB broadcast), order flow (PUSH/PULL pipeline with backpressure), control (kill switch PUSH/PULL).

---

## Component-by-Component Breakdown

### 1. Limit Order Book (`engine/lob.py`) — THE CROWN JEWEL

**This is what interviewers care most about. Know every detail.**

**Data Structures:**
- `_bids`: `SortedDict(lambda x: -x)` — descending sort, so `peekitem(0)` returns best (highest) bid in **O(1)**
- `_asks`: `SortedDict()` — ascending, `peekitem(0)` returns best (lowest) ask in **O(1)**
- Each price level: `deque[Order]` — **O(1) FIFO** (append right, pop left = price-time priority)
- `_order_map`: `dict[order_id, Order]` — **O(1) cancel lookup**

**Why SortedDict not a heap:**
> "I chose `SortedDict` from `sortedcontainers` over `heapq` because heaps don't support O(log N) arbitrary deletion. When you cancel an order in a heap, you'd need to mark it and rebuild — O(N). `SortedDict` gives me O(log N) insert, O(log N) delete, and O(1) best-price peek. It's backed by a B-tree of sorted lists, which is cache-friendly despite being pure Python."

**Order State Machine:**
```
NEW -> PARTIAL -> FILLED
NEW -> CANCELLED
PARTIAL -> CANCELLED
PARTIAL -> FILLED
```
Guarded by `assert` — illegal transitions crash immediately with a clear message. No silent corruption.

**Lazy Cancel — O(1):**
> "When a cancel request comes in, I don't scan the deque to find the order. I just look it up in `_order_map` in O(1) and set its status to `CANCELLED`. The order stays in the deque. During matching, the `_fill_from_level` method checks each resting order's status — if it's `CANCELLED`, it pops it silently and moves on. This is the 'mark as dead' pattern — same philosophy as LMAX Disruptor's sequence barriers."

**Ghost Level Pruning:**
> "After a price level is fully drained (all orders filled or cancelled), I explicitly delete the key from the `SortedDict`. Without this, empty price levels accumulate and corrupt `best_bid()`/`best_ask()` — they'd return a price with no actual liquidity."

**Integer Tick Prices:**
> "All prices are int64 ticks — `$150.00` is stored as `15000`. No float comparison anywhere in the matching logic. This eliminates an entire class of bugs where `150.00 != 149.999999999` due to IEEE 754 representation. Every production exchange works this way."

**VWAP Tracking:**
> "Each order tracks `cum_value_ticks` (sum of `fill_price * fill_qty` across all fills) and `cum_qty`. Average execution price is `cum_value_ticks // cum_qty` — integer division, no float."

---

### 2. Matching Engine (`engine/matching_engine.py`)

**Threading Model (3 threads):**
1. **`recv_loop` thread**: blocking `pull.recv()`, parses `NewOrderSingle` proto, appends to per-symbol ring buffer
2. **`cancel_recv_loop` thread**: blocking `cancel_pull.recv()`, processes `CancelRequest` protos
3. **Main thread**: runs `_match_loop()` — round-robin drains ring buffers, calls `lob.submit()`, publishes `ExecutionReport`

**Ring Buffer — LMAX Disruptor Approximation:**
> "`deque(maxlen=65536)` per symbol. This decouples network I/O latency from matching latency. `deque.append` and `popleft` in CPython are GIL-atomic C operations — thread-safe without locks. The 65K capacity can absorb ~1.3 seconds of burst at 50K orders/sec."

**Say this:** "The ring buffer is my burst absorber. Network latency spikes don't stall the matching thread, and matching slowdowns don't block the receiver. Same principle as the LMAX Disruptor, adapted to Python's GIL guarantees."

**Round-Robin Fair Queue:**
> "The match loop iterates all symbols in a fixed order each cycle. No symbol can starve another. If one symbol has a burst of 10K orders and another has 1, they both get served each cycle."

**Execution Report Routing:**
> "Uses ZeroMQ multipart frames: `[strategy_id.encode(), er.SerializeToString()]`. Strategies subscribe with `SUBSCRIBE = strategy_id`. ZeroMQ's prefix filter drops non-matching frames at the subscriber socket level — before they even reach Python userspace. So with 8 strategies, each only processes 1/8th of the exec reports."

**Orphan Order GC:**
> "Every 5 seconds, the match loop scans open orders. If a strategy hasn't been seen (via heartbeat) for 10 seconds, all its orders are cancelled and CANCEL exec reports published. This prevents a crashed strategy from leaving stale orders in the book."

**Clean Shutdown:**
> "SIGTERM sets a `threading.Event`. Recv threads check it and break on `zmq.ContextTerminated`. `ctx.term()` with `LINGER=100ms` completes in under 500ms — validated by a test that actually sends SIGTERM and asserts wall-clock time."

---

### 3. Risk Gateway (`engine/risk_gateway.py`)

**The pre-trade firewall. Single process, single thread, multiplexed with `zmq.Poller`.**

**Four Risk Checks (in order):**

| Check | Logic | Reject Reason |
|-------|-------|---------------|
| Fat Finger | `price * qty > 100,000` | `FAT_FINGER` |
| Position Limit | `abs(current_position + delta) > 10` | `POSITION_LIMIT` |
| Rate Limiter | Token bucket: 10 tokens/sec, capacity 10 | `RATE_LIMIT` |
| Kill Switch | Boolean flag set via PULL socket | `KILL_SWITCH` |

**Impressive details to mention:**

- **Raw bytes forwarding**: When an order passes all checks, I forward the original serialized bytes — `outbound.send(raw)`. No re-serialization. This saves one protobuf serialize cycle on every approved order.
- **Position reconciliation**: The RG subscribes to the ME's execution reports. Every fill updates a `positions[(strategy_id, symbol)]` ledger. This is a signed integer — positive = long, negative = short.
- **Token bucket implementation**: `tokens = min(capacity, tokens + elapsed_seconds * refill_rate)`. Classic algorithm. If no token available, the order is rejected — not queued.
- **Kill switch cascade**: When kill switch activates, it doesn't just block new orders — it sends `CancelRequest` for every pending order in `pending_orders`. Full emergency stop.

---

### 4. Feed Handler (`engine/feed_handler.py`)

**Three modes:**

| Mode | Behavior | Target TPS |
|------|----------|-----------|
| `gbm` | Geometric Brownian Motion, 5 symbols, rate-gated | 10,000/sec |
| `stress` | No rate gate, spin as fast as possible | 50,000+/sec |
| `replay` | CSV historical data, configurable speed multiplier | Variable |

**Key details to emphasize:**

- **Absolute monotonic timing** (not relative sleep):
  > "I don't do `sleep(interval)` between ticks — that accumulates drift. Instead, I track `next_ns` as an absolute monotonic timestamp and busy-wait until the clock reaches it. Over millions of ticks, there's zero timing drift."

- **Slow-joiner guard**: Sleep after bind, before first publish, to give subscribers time to connect. Without this, ZeroMQ PUB silently drops messages to not-yet-connected subscribers.

- **Tick object reuse**: One `MarketDataTick()` proto object allocated before the loop. Fields overwritten each iteration. Zero per-tick heap allocation.

---

### 5. Strategy Framework (`engine/strategy.py`)

**Template Method / Strategy Pattern:**

> "I designed `BaseStrategy` as an abstract base class. Subclasses implement only `on_tick(tick)` and `on_fill(exec_report)`. All the ZeroMQ wiring, heartbeat monitoring, self-trade prevention, and safe-mode logic lives in `_strategy_target()` — invisible to strategy authors. You can write a new strategy in 20 lines."

**Self-Trade Prevention:**
> "Before sending any order, the submit function checks `open_orders` for resting orders on the same symbol. If a BUY at price P would cross my own resting SELL at rest_price <= P, it's rejected locally — never hits the network. This is critical in a multi-strategy setup."

**Safe Mode (Heartbeat Timeout):**
> "If no feed heartbeat arrives within 5 seconds, the strategy enters safe mode: it cancels all open orders and suppresses `on_tick` dispatch. When the heartbeat resumes, it auto-recovers. This prevents a strategy from acting on stale data."

**Post-Fork ZeroMQ Safety:**
> "All `zmq` imports and `Context` creation happen inside `_strategy_target()` — which runs after `multiprocessing.Process` forks. Creating a ZeroMQ context before fork corrupts the I/O threads in the child because forked file descriptors become invalid. I enforce this rule across every component."

---

### 6. MeanReversionStrategy

> "Rolling z-score mean reversion. Per symbol, I keep a sliding window of mid prices. Once the window is full, I compute `z = (mid - mean) / std`. If z < -2.0, the price is two standard deviations below the mean — I BUY. If z > 2.0, I SELL. Guard against `std == 0.0` to prevent division by zero on static price sequences."

---

### 7. MLSignalStrategy

**Say this — it shows breadth:**
> "I built a gradient-boosted classifier that predicts whether the price will be higher 10 ticks in the future. It uses 4 features: rolling volatility, bid-ask spread ratio, volume imbalance, and momentum. The model outputs a probability, and I scale the order quantity linearly with confidence — higher confidence means larger position."

**Technical details:**
- `HistGradientBoostingClassifier` from scikit-learn (LightGBM-equivalent)
- 70/30 **chronological** split — no shuffle, no data leakage
- Pre-allocated `np.zeros((1, 4), dtype=np.float32)` — zero heap allocation in the inference hot path
- Model loaded via `joblib.load()` in `on_start()` — post-fork

---

## Key Design Decisions (What to Emphasize)

### 1. "Why ZeroMQ instead of Kafka/RabbitMQ?"

> "Brokers add latency and operational complexity. Kafka is great for durable message replay, but I don't need that — market data is ephemeral. ZeroMQ is brokerless, embeds directly in the process, and gives me sub-millisecond IPC. The trade-off is I don't get message persistence or replay, but for a trading system, speed matters more than durability of market data."

### 2. "Why protobuf instead of JSON?"

> "Protobuf is a binary format — smaller wire size, faster serialization, and strict schema enforcement. With JSON, a misspelled field name silently creates a new key. With protobuf, schema version 1 is embedded as field 1 in every message — the cheapest field to encode because fields 1-15 use a 1-byte tag. I also handle the proto3 zero-default hazard: an order with `side=0` could mean 'unset' or 'BUY enum value 0', so I explicitly check for zero-value fields."

### 3. "Why multiprocessing instead of asyncio/threading?"

> "Python's GIL means threads can't truly parallelize CPU-bound work. The matching engine is CPU-bound. `multiprocessing.Process` gives me real OS-level parallelism. Each component gets its own memory space, its own GIL, and can be deployed on separate machines by just changing IPs in the topology config."

### 4. "Why SortedDict instead of a Red-Black Tree?"

> "`sortedcontainers.SortedDict` is a B-tree of sorted lists. It's pure Python but highly optimized — benchmarks show it's faster than C-extension red-black trees for typical order book operations because it's cache-friendly. And I get `peekitem(0)` for O(1) best-price access, which a red-black tree doesn't natively expose."

### 5. "Why integer prices?"

> "Every production exchange uses integer tick prices. IEEE 754 floats can't exactly represent many decimal values — `0.1 + 0.2 != 0.3`. In a matching engine, this means an order at 150.00 might not match a resting order at 150.00 due to floating-point representation differences. Integer ticks eliminate this entire bug class."

### 6. "Why bind/connect topology?"

> "The stable side (servers) always binds. The dynamic side (strategies) always connects. This means I can scale strategy nodes from 1 to N without touching any other component's configuration. It's the same pattern as a web server — you don't reconfigure nginx when a new client connects."

---

## Performance Numbers (Memorize These)

| Metric | Value | Context |
|--------|-------|---------|
| Market data throughput (GBM) | 10,000 ticks/sec | Rate-gated, 5 symbols, 2K/symbol/sec |
| Market data throughput (stress) | 50,000+ ticks/sec | No rate gate, spin loop |
| Ring buffer capacity | 65,536 orders | ~1.3 sec buffer at 50K/sec |
| LOB best price lookup | O(1) | `SortedDict.peekitem(0)` |
| Order cancel | O(1) | Lazy cancel — mark and skip |
| Order insert | O(log N) | `SortedDict` insert |
| ME shutdown time | < 500 ms | SIGTERM → full cleanup, tested |
| Orphan GC interval | 5 seconds | Stale strategy detection |
| Rate limiter | 10 orders/sec/strategy | Token bucket, capacity 10 |
| Position limit | 10 units/side/strategy | Per (strategy, symbol) pair |
| Fat finger threshold | 100,000 notional | price * qty check |
| Heartbeat timeout | 5 seconds | Triggers safe mode in strategies |

---

## Design Patterns Used

| Pattern | Where | What to Say |
|---------|-------|-------------|
| LMAX Disruptor | ME ring buffer | "Decouples I/O from matching — burst absorber" |
| Template Method | BaseStrategy ABC | "Strategy authors only implement on_tick and on_fill" |
| Factory | zmq_factory.py | "Centralized socket creation with enforced LINGER/HWM" |
| Strategy Pattern | MeanReversion, MLSignal | "Pluggable trading logic behind a fixed interface" |
| Token Bucket | Rate limiter in RG | "Classic burst-control — allows short bursts while limiting sustained rate" |
| Pub/Sub | Market data, exec reports | "One-to-many broadcast, O(1) at publisher" |
| Pipeline | Order flow PUSH/PULL | "Point-to-point with backpressure via HWM" |
| Lazy Evaluation | LOB cancel | "O(1) cancel, deferred cleanup during matching" |
| State Machine | Order lifecycle | "NEW -> PARTIAL -> FILLED/CANCELLED, guarded by assertions" |
| Monkey Patching | submit_order injection | "Inject ZeroMQ capability post-fork without changing the ABC" |

---

## What Makes This Project Impressive

**Say these things proactively — don't wait for them to ask:**

1. **"It's not a toy — it handles real exchange semantics"**: Price-time priority, partial fills, VWAP tracking, order state machines, self-trade prevention. These are the same concepts used in NASDAQ's ITCH/OUCH protocol.

2. **"I made deliberate trade-offs and can explain each one"**: ZeroMQ over Kafka (latency vs durability), lazy cancel over eager delete (amortized cost), integer ticks over floats (correctness over convenience), multiprocessing over threads (true parallelism).

3. **"The testing strategy is production-grade"**: Unit tests with zero network dependency, integration tests with real subprocesses and real ZeroMQ, end-to-end tests with the full pipeline on isolated ports. Port isolation per test class prevents socket binding conflicts.

4. **"I understand the failure modes"**: Heartbeat-based failure detection, safe mode on feed loss, orphan order GC for crashed strategies, kill switch for emergency shutdown, clean SIGTERM handling with sub-500ms teardown.

5. **"The architecture scales horizontally by design"**: Adding strategy nodes requires zero reconfiguration. The bind/connect topology, topic-filtered PUB/SUB, and PUSH/PULL fair-queuing all support this.

6. **"I didn't over-engineer"**: No Kafka, no Docker, no Kubernetes, no service mesh. ZeroMQ + protobuf + Python multiprocessing. Minimal dependencies, maximum clarity. I know exactly what each line does.

---

## Common Interview Questions & Killer Answers

### Architecture & Design

**Q: "Walk me through the order lifecycle end-to-end."**

> "A strategy's `on_tick()` fires when it receives a `MarketDataTick` from the feed handler via PUB/SUB. It computes a signal — say, z-score is below -2.0 — and calls `submit_order()`. The submit function first checks for self-trades against `open_orders`. If clean, it builds a `NewOrderSingle` protobuf and PUSHes it to the Risk Gateway on port 5558.
>
> The Risk Gateway's poller fires. It runs four checks in sequence: fat finger (notional <= 100K), position limit (net exposure <= 10), rate limiter (token bucket: 10/sec), and kill switch (boolean flag). If all pass, it forwards the raw bytes — not re-serialized — to the Matching Engine on port 5559.
>
> The ME's recv_loop thread appends the parsed NOS to the ring buffer for that symbol. The main match loop drains it, calls `lob.submit()` which sweeps the opposite side — if there's a resting ASK at a price <= our BUY price, we fill. The LOB generates an `ExecutionReport` for both sides (aggressor and resting order). These are published on PUB port 5556 as multipart frames with topic = strategy_id.
>
> Back in the strategy process, the exec_sub socket receives the report (filtered by its strategy_id subscription). `on_fill()` is called. Meanwhile, the Risk Gateway also subscribes to exec reports and updates its position ledger."

---

**Q: "Why didn't you use a message broker like Kafka?"**

> "Three reasons: latency, complexity, and fit. ZeroMQ is brokerless — messages go directly between processes, no intermediate hop. That's sub-millisecond vs Kafka's typical 2-10ms. Second, I'd need to deploy and manage a Kafka cluster — that's operational overhead for a system that doesn't need message persistence. Market data is ephemeral — if a strategy misses a tick, the next one is 100 microseconds away. Third, ZeroMQ's PUB/SUB and PUSH/PULL patterns map directly to my data flow. I don't need consumer groups, partitions, or offsets."

---

**Q: "How do you handle backpressure?"**

> "Two mechanisms. For the order flow path (PUSH/PULL), ZeroMQ's High Water Mark (HWM) provides backpressure — when the receiver's queue is full, the sender blocks on `send()`. This is set in `zmq_factory.py` via topology config. For the matching engine specifically, the ring buffer (`deque(maxlen=65536)`) acts as a burst absorber — it can hold ~1.3 seconds of orders at 50K/sec. If it overflows, the oldest order is silently dropped (Python deque behavior with maxlen), which I accept as a design trade-off since it means the system is overloaded beyond capacity."

---

**Q: "What happens if the matching engine crashes?"**

> "The strategies detect it indirectly. The feed handler's heartbeat continues, so they stay active. But their orders get no execution reports. After 10 seconds, the ME's orphan GC would have cancelled stale orders — but if the ME itself is down, that logic doesn't run. The strategies would accumulate unanswered orders in their `open_orders` dict.
>
> This is an honest gap. In production, you'd want: (1) a watchdog process that monitors ME heartbeats and triggers restart, (2) order state persistence so the ME can rebuild the book on restart, and (3) strategies that timeout on pending orders and enter safe mode. I implemented the heartbeat infrastructure but not full crash recovery."

---

**Q: "How would you add persistence?"**

> "I'd add a write-ahead log. Before the matching engine processes an order, append the serialized NOS to a sequential file (or a memory-mapped ring buffer backed by a file). On restart, replay the WAL to rebuild the order book. The protobuf serialization I already have makes this straightforward — each log entry is just the raw bytes I already receive over the wire. I'd also persist the Risk Gateway's position ledger so it doesn't reset to zero on restart."

---

### Data Structures & Algorithms

**Q: "Why not use a heap for the order book?"**

> "Heaps give me O(1) peek at the best price, which is good. But they don't support efficient arbitrary deletion. When a cancel comes in, I need to find and remove a specific order. In a heap, that's O(N) to find + O(log N) to re-heapify. With `SortedDict`, I get O(1) peek (via `peekitem(0)`), O(log N) insert, and O(log N) delete. Plus, I need to iterate price levels in order during a market sweep — heaps require full extraction for that."

---

**Q: "Explain your lazy cancel approach."**

> "When a cancel arrives, I look up the order in `_order_map` — O(1) dict lookup — and set `status = CANCELLED`. That's it. The order stays in its deque at its price level. During matching, `_fill_from_level` pops orders from the front of the deque and checks their status. If `CANCELLED`, it skips the order and pops the next one. This amortizes the cost of removal — the order gets cleaned up naturally when it would have been matched.
>
> The alternative is eager deletion: find the order in the deque (O(N) scan), remove it (O(N) shift), and potentially delete the price level if empty. My approach is O(1) for the cancel itself and O(1) amortized for cleanup. The trade-off is slightly more memory usage (cancelled orders linger) and a tiny overhead per match (status check)."

---

**Q: "How does your rate limiter work?"**

> "Token bucket algorithm. Each strategy gets a bucket with capacity 10, refilling at 10 tokens per second. When an order arrives, I compute `elapsed = now - last_refill_time`, add `elapsed * refill_rate` tokens (capped at capacity), then try to consume 1 token. If tokens < 1, the order is rejected.
>
> This allows burst behavior — a strategy can send 10 orders instantly if it hasn't sent any for a second — while capping the sustained rate at 10/sec. It's the same algorithm used by API rate limiters at AWS, Stripe, and most exchanges."

---

### Concurrency & Distributed Systems

**Q: "How do you handle the GIL?"**

> "I don't fight it — I work with it. Each component is a separate `multiprocessing.Process` with its own GIL. Within the matching engine, I use threads for I/O (recv loops) and the main thread for CPU-bound matching. The GIL serializes Python bytecode execution, but `zmq.recv()` releases the GIL while waiting for data — so the recv threads genuinely run in parallel with the match loop when they're blocked on I/O. For the CPU-bound matching code, there's only one thread doing it, so the GIL doesn't matter."

---

**Q: "What's the ZeroMQ context-after-fork rule?"**

> "If you create a `zmq.Context` in the parent process and then fork, the child inherits the same file descriptors that ZeroMQ's I/O threads use. But the child's I/O threads are gone — they didn't survive the fork. So the inherited sockets are broken. The fix is simple: all ZeroMQ initialization happens inside the target function that runs after fork. I enforce this in every component — `zmq` is imported and `Context()` is created inside `_matching_engine_target()`, `_risk_gateway_target()`, etc."

---

**Q: "How do you prevent self-trades?"**

> "Before any order is sent to the network, the strategy's `_submit_order()` function checks its local `open_orders` dict. If I'm submitting a BUY at price P, I check if I have any resting SELL orders at rest_price <= P — because that would mean my BUY would cross my own SELL. If found, I reject the order locally. Same logic in reverse for SELLs. This is a local, in-process check — it never hits the Risk Gateway or Matching Engine."

---

**Q: "How do strategies detect failures?"**

> "The feed handler publishes heartbeats on a dedicated PUB socket (port 5557) every 1000 ticks. Each strategy subscribes and tracks the last heartbeat time. If 5 seconds pass with no heartbeat, the strategy enters 'safe mode': it cancels all open orders and stops processing ticks. When the heartbeat resumes, it auto-recovers.
>
> The matching engine and risk gateway also publish heartbeats (ports 5563, 5564) — currently these are monitored by the dashboard only, not by strategies."

---

### Testing

**Q: "How did you test this?"**

> "Three tiers. **Unit tests**: the LOB, risk checks, and strategy logic are all tested in pure Python — no ZeroMQ, no processes. I instantiate `LimitOrderBook()` directly and call `submit()` with hand-crafted orders. For risk checks, I use `types.SimpleNamespace` to mock protobuf messages. 20+ LOB tests covering FIFO priority, market sweeps, partial fills, lazy cancel, ghost level pruning, VWAP accuracy, and state machine assertions.
>
> **Integration tests**: I spawn real subprocesses — the actual matching engine and feed handler code — with real ZeroMQ sockets on isolated port ranges. The ME integration test sends 1000 orders and verifies throughput. The orphan GC test spawns the ME with a 1-second timeout and verifies stale orders get cancelled. The shutdown test sends SIGTERM and asserts cleanup completes in under 500ms.
>
> **End-to-end test**: I spin up the entire 5-component pipeline on a dedicated port range (18000-18008). Two strategies run — one forced to always buy, one to always sell — so they'll inevitably match against each other. The test waits up to 30 seconds for at least one fill on each strategy.
>
> Each test class uses a different port range (15555, 16000, 18000, 19000) to prevent socket binding conflicts between parallel tests."

---

### ML

**Q: "Tell me about the ML strategy."**

> "It's a gradient-boosted binary classifier — `HistGradientBoostingClassifier` from scikit-learn — that predicts whether the price will be higher 10 ticks in the future. Four features: rolling volatility (50-tick window), bid-ask spread ratio, volume imbalance (20-tick window), and momentum (20-tick window).
>
> I trained it with a chronological 70/30 split — no shuffling, because shuffling time series creates data leakage. The model achieved 53.8% train accuracy, 50.5% test accuracy. I want to be transparent: that's barely above random. The volume_imbalance feature has no real signal because the synthetic feed generates equal bid/ask sizes. I documented this in the model metadata.
>
> What I'm proud of is the engineering: the feature vector is pre-allocated as `np.zeros((1, 4), dtype=np.float32)` — zero heap allocation in the inference hot path. The model is loaded via `joblib.load()` in `on_start()`, which runs post-fork. And order quantity scales linearly with prediction confidence — `qty = max(1, int(BASE_QTY * (proba - 0.5) * 2))`. Higher confidence = larger position."

---

**Q: "How would you improve the ML model?"**

> "Three things. First, train on real market data with real volume — the synthetic feed's equal bid/ask sizes kill the volume_imbalance feature. Second, add features: order book depth, trade imbalance, VWAP deviation, volatility regime indicators. Third, the 10-tick horizon is arbitrary — I'd optimize it via cross-validation on the training set. I might also switch to a temporal convolutional network or transformer for sequence modeling, though the gradient-boosted model is better for low-latency inference."

---

### Scalability & Production

**Q: "How would you scale this to handle more throughput?"**

> "Three axes. First, **symbol partitioning**: right now one ME handles all 5 symbols. I could shard — ME-1 handles BTC/ETH, ME-2 handles XRP/SOL/DOGE. Each ME is independent with its own LOB instances. Second, **strategy parallelism**: adding strategy nodes requires zero reconfiguration — they just connect to the known RG address. Third, **feed handler**: it's already the lightest component. In production, I'd replace synthetic data with a real exchange websocket adapter."

---

**Q: "What would you change for production?"**

> "Five things:
> 1. **Persistence**: Write-ahead log for the order book, persistent position ledger for the Risk Gateway
> 2. **Monitoring**: Prometheus metrics export from each component (already have heartbeats as the foundation)
> 3. **Clock sync**: NTP/PTP for cross-node latency measurement
> 4. **Market data sequence numbers**: Add a `seq` field to `MarketDataTick` so strategies can detect dropped ticks
> 5. **Connection resilience**: Auto-reconnect on socket failure (ZeroMQ has `RECONNECT_IVL` but I'd add application-layer handshake)
> 6. **The `order_map` in the LOB never shrinks** — I'd add periodic cleanup of terminal-state orders"

---

### Behavioral / Soft

**Q: "What was the hardest bug you encountered?"**

> "The ZeroMQ slow-joiner problem. When I first ran the pipeline, the feed handler would start publishing immediately after binding. But PUB/SUB subscription propagation isn't instantaneous — the subscriber needs time to connect and register its subscription filter. So the first few hundred ticks were silently dropped. The fix was a simple `time.sleep()` after bind, before first publish. But it took me a while to diagnose because the data just... vanished. No errors, no exceptions. That's when I learned that ZeroMQ PUB is fire-and-forget — if no subscriber is connected, the message is gone."

---

**Q: "What was the most interesting design decision?"**

> "The lazy cancel in the order book. My first instinct was eager deletion — find the order in the deque and remove it. But that's O(N) per cancel. I realized I could just mark it as cancelled in O(1) and let the matching loop clean it up naturally. It's the same insight behind tombstones in LSM-trees and lazy deletion in B-trees. The trade-off is slightly more work during matching, but cancels are time-critical — a trader wants to know their cancel is acknowledged instantly."

---

**Q: "Why did you choose Python for a trading engine?"**

> "Fair question — C++ or Rust would be faster. I chose Python for three reasons: (1) rapid prototyping — I wanted to focus on the architecture and algorithms, not memory management; (2) the ML integration — scikit-learn, numpy, and joblib are Python-native; (3) ZeroMQ's Python bindings (pyzmq) are excellent and the actual message passing happens in C under the hood.
>
> That said, in production, the matching engine hot path would be in C++ or Rust. But the architecture — the process topology, the protobuf schema, the ZeroMQ patterns — would be identical. That's the value of this project: the design transfers regardless of language."

---

## Weakness Acknowledgment (Own These Before They Ask)

**Say these proactively to show self-awareness:**

1. **"The ML model is barely above random on test data"** — because it's trained on synthetic data where volume_imbalance has no signal. I documented this in the metadata. With real market data, I'd expect meaningful improvement.

2. **"`order_map` in the LOB never shrinks"** — filled/cancelled orders stay in the dict. For long-running systems, this is a memory leak. Fix: periodic sweep of terminal-state orders.

3. **"No clock synchronization across nodes"** — can't measure true tick-to-trade latency without NTP/PTP. I measure intra-node latency only.

4. **"Risk Gateway position ledger is volatile"** — if RG crashes, positions reset to zero. On restart, it could temporarily allow position limit violations until fills reconcile. Fix: persist the ledger.

5. **"No sequence numbers on market data"** — strategies can't detect dropped ticks. The protobuf schema should have a `seq` field.

6. **"Python's GIL limits single-node throughput"** — the matching engine can't multithread the matching logic. For higher throughput, I'd rewrite the hot path in C++ or Rust.

---

## Buzzwords to Drop Naturally

Use these in context — don't force them:

- **LMAX Disruptor** — when explaining the ring buffer
- **Price-time priority** — when explaining the LOB
- **FIX protocol / NewOrderSingle** — the NOS proto is named after the FIX standard
- **VWAP** — volume-weighted average price tracking
- **Fat finger check** — pre-trade risk, same as real exchanges
- **Kill switch / circuit breaker** — emergency shutdown mechanism
- **Token bucket** — rate limiting algorithm
- **Geometric Brownian Motion** — stochastic process for price simulation
- **Protobuf / wire format** — binary serialization
- **Brokerless messaging** — ZeroMQ's key differentiator
- **Backpressure** — HWM in PUSH/PULL
- **Topic filtering** — subscriber-side message routing
- **Lazy deletion / tombstone** — cancel optimization
- **Chronological train/test split** — no data leakage
- **Confidence-scaled sizing** — ML strategy's position sizing

---

## System Design Whiteboard Walkthrough

If asked to whiteboard the system, draw it in this order:

1. **Start with the data flow** — draw the Feed Handler on top, arrow down to Strategy box, arrow right to Risk Gateway, arrow right to Matching Engine, arrow back up to Strategy. Label each arrow with the socket type (PUB/SUB, PUSH/PULL).

2. **Label the separation of concerns** — "market data plane" (PUB/SUB), "order plane" (PUSH/PULL), "control plane" (kill switch, heartbeats).

3. **Zoom into the Matching Engine** — draw the ring buffer between the recv thread and match thread. Draw the LOB with SortedDict + deque. Show the order state machine.

4. **Zoom into the Risk Gateway** — draw the 4 checks as a pipeline/waterfall. Show the position ledger being updated from exec reports.

5. **Discuss scaling** — draw multiple strategy boxes all connecting to the same RG. Draw symbol partitioning across multiple ME boxes.

6. **Discuss failure modes** — heartbeat arrows, safe mode trigger, orphan GC, kill switch.

---

## Quick Reference Card (Review 5 Min Before Interview)

- LOB: SortedDict + deque, O(1) best price, O(1) lazy cancel, int64 ticks
- ME: 3 threads (recv, cancel_recv, match), ring buffer 65K, round-robin drain
- RG: 4 checks (fat finger, position, rate, kill), raw bytes forward, zmq.Poller
- Feed: GBM 10K/sec, stress 50K+, absolute monotonic timing, object reuse
- Strategy: ABC template method, self-trade prevention, safe mode on heartbeat loss
- ZeroMQ: PUB/SUB for broadcast, PUSH/PULL for pipeline, context-after-fork
- Protobuf: 7 message types, int64 prices, schema_version field 1
- Testing: unit (pure Python) + integration (real processes) + e2e (full pipeline)
- ML: HistGBM, 4 features, chronological split, confidence-scaled sizing
- 10 ports, all hardcoded in topology.yaml, env var overrides for FABRIC deployment
