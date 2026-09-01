# Part XII — Observability & Operations in Production

# Chapter 74 — Process Topology & The Deterministic State Machine

> **Prerequisites:** Ch. 26 (mmap/shared memory — the inter-process data plane), Ch. 50 (IPC / cross-process ring buffers — how the processes talk), Ch. 35 (seqlock/single-writer publication — the per-segment discipline), Ch. 46 (warming — fast restart isn't fast if it's cold), Ch. 73 (hot reload — config distribution to the processes), Ch. 72 (security — privilege isolation, kill-switch/safe-state).
>
> **Leads into:** Ch. 75 (capture/replay — the deterministic state machine is what you replay *into*), Ch. 76 (case study — the whole topology end-to-end). A Part XII architecture chapter: structuring the system as cooperating processes so one failure can't take down the session, and isolating a pure, replayable core.

---

## 74.1 Why it matters: contain a crash, keep the venue session up

Everything before this chapter optimized a *process*. This one is about what happens when that process **crashes** — because it will. A null deref in a new strategy, an unhandled malformed packet that slipped past Ch. 72's gate, an assertion, a bad config, an OOM, a hardware glitch — software fails, and a trading system runs real money through it during live sessions. The architectural question is not *whether* a component fails but **what its failure takes down with it.** In a **monolith** — feed handler, strategy, risk, and order gateway all in one process — the answer is *everything*: one strategy's segfault kills the feed handler (you go blind to the market), the order gateway (you drop the venue session and orphan working orders), and risk (you lose the safety check) — all at once, mid-session, with open positions. A single bug becomes a total outage.

The alternative is **process-per-role**: split the system into separate OS processes — feed handler, strategy (often one per strategy/symbol-group), risk, order gateway — each in its own address space, communicating through shared-memory data planes (Ch. 26, 50). Now a crash is **contained**: if a strategy process dies, the feed handler keeps decoding the market, the gateway keeps the venue session alive and can cancel that strategy's working orders (kill-switch, §74.4.4), risk keeps enforcing limits, and a supervisor restarts just the dead process (warm — Ch. 46) while everything else keeps running. The **blast radius** of a failure shrinks from "the whole system" to "one role, one shard." For a system where an outage during market hours is a financial and regulatory event, fault containment is not optional polish — it's the difference between "we restarted one strategy in 200 ms" and "we were down and blind with open positions."

There's a second, equally important reason rooted in the rest of Part XII: **determinism and replayability.** If the core decision logic is a **pure, side-effect-free state machine** — same inputs always produce the same outputs, no I/O or clocks or randomness inside it — then you can **replay** a captured session (Ch. 75) through it and get *exactly* the same decisions, bit-for-bit. That's the foundation of debugging ("replay the crash"), backtesting ("replay history through the new strategy"), compliance ("prove what we did and why"), and regression testing (Ch. 76). Process topology and the deterministic state machine are two halves of the same idea: **separate the fragile, I/O-bound, non-deterministic edges (network, timers, OS) from a contained, pure, replayable core** — so failures are isolated *and* the system is reproducible. This chapter motivates the topology and the pattern (§74.2), measures restart/failover time and its tail (§74.3), and builds the shared-memory data planes, single-writer discipline, supervision, kill-switch, fast restart, and sharding that make it work (§74.4).

## 74.2 Mental model

### 74.2.1 Process-per-role vs monolith (feed handler / strategy / order gateway / risk)

Decompose the tick-to-trade path into separate processes by **role**, each a fault domain:

```
   wire ─► [ FEED HANDLER ]─shm─►[ STRATEGY(s) ]─shm─►[ RISK ]─shm─►[ ORDER GATEWAY ]─► wire
            decode (Ch.48)        pure decision        pre-trade      venue session,
            normalize, seq        (state machine)      limits/kill    order I/O
                 │                      │                  │                │
            (one process)          (one per strategy/   (one process)   (one process,
                                    symbol shard)                        holds the session)
```

- **Feed handler** — owns the market-data side (Ch. 53, 55): decode, normalize, sequence/gap-handle, publish a clean normalized stream into shared memory. If a strategy dies, the feed handler is untouched — the market view stays live.
- **Strategy** — the decision logic; often **one process per strategy or per symbol/venue shard** (§74.4.5) so strategies can't crash each other. Ideally a *pure state machine* (§74.2.2).
- **Risk** — the pre-trade gate (limits, position, kill-switch, Ch. 72): a separate process (or a mandatory in-line check) that no strategy can bypass or crash. The last line before an order leaves.
- **Order gateway** — owns the *venue session* (Ch. 51): the TCP/order-entry connection, sequence numbers, working-order state. Keeping this separate means a strategy crash doesn't drop the session or orphan orders — the gateway can cancel-on-disconnect / kill-switch the dead strategy's orders and keep the session up.

The trade-off is **latency vs containment**: crossing a process boundary costs an IPC hop (a shared-memory ring write/read — Ch. 50, tens to low-hundreds of ns, *not* a syscall if done right). A monolith saves those hops but couples all failures. The HFT resolution: use **shared-memory data planes** (§74.4.1) so the hops are cheap (no kernel, no copy — Ch. 26/50), and accept a small, bounded latency cost for a large containment win. The hottest, most-coupled stages can be co-located if a benchmark justifies it; the *fault-critical* split (gateway and risk separate from strategy) is rarely worth giving up.

### 74.2.2 The Deterministic State Machine Pattern: pure, side-effect-free core decoupled from I/O for exact replayability

The most important pattern in the chapter: **isolate the core decision logic into a pure, deterministic state machine, decoupled from all I/O, timers, and randomness.**

```
   IMPURE EDGE (non-deterministic):              PURE CORE (deterministic):
   ──────────────────────────────               ──────────────────────────
   network RX (Ch.44-52)                         apply(state, input) → (state', outputs)
   timers / clocks (Ch.16)            ─inputs─►   • NO syscalls, NO I/O, NO clock reads
   RNG, OS scheduling                             • NO wall-clock now() inside — time is an INPUT
   network TX                         ◄─outputs─  • same (state, input sequence) ⇒ same outputs, ALWAYS
```

The core is a function `apply(state, input) → (new_state, outputs)` — a **fold over the input stream**. Crucially:

- **No side effects inside.** It reads no clock, makes no syscall, does no network I/O, touches no global mutable state, uses no randomness. Anything non-deterministic — the current time, a random seed, a sequence number — is passed *in as part of the input*, never read from the environment inside the core. (Time becomes a *timestamped input event*, so replay reproduces the exact timeline.)
- **Inputs are an ordered, captured stream.** Every input — each market-data message, each timer fire, each fill, each config change (Ch. 73) — is a record in a single ordered log (Ch. 75). The core consumes that stream.
- **Determinism ⇒ exact replay.** Because outputs depend *only* on (initial state + input sequence), feeding the *same captured input log* through the core reproduces the *same outputs exactly* — the same orders, the same decisions, bit-for-bit. This is the property everything in Ch. 75–76 relies on: replay the captured session to debug a production crash, to backtest a strategy change against real history, to prove compliance, to gate regressions in CI.

This is the same edge/core split as functional-core/imperative-shell, applied to trading for *both* fault isolation (the pure core has fewer ways to fail and no I/O to hang on) and reproducibility. The I/O, timers, and OS interaction — the non-deterministic, failure-prone parts — live in a thin shell around the core; the core itself is pure, testable, and replayable.

### 74.2.3 Fault domains and blast radius

A **fault domain** is a boundary a failure can't cross; the **blast radius** is what one failure actually takes down. Process boundaries (separate address spaces) are the strongest in-box fault domain: a segfault, heap corruption, or assertion in one process cannot corrupt another's memory or take down its thread — the OS contains it. Design so that:

- **Each role is its own fault domain** (§74.2.1) — a strategy crash can't corrupt the feed handler or gateway.
- **Each shard is its own fault domain** (§74.4.5) — one symbol/venue's strategy crash doesn't touch another's.
- **The blast radius of any single failure is bounded and known** — you can state, for each component, exactly what its death takes down and what survives. The gateway and risk survive *every* strategy failure (so orders can be cancelled and the session kept up); a feed handler failure is contained to its feed (the redundant A/B line, Ch. 53, and other feeds survive).
- **Shared memory is a controlled hole in the isolation** (§74.4.1-2): processes share data-plane segments, so a writer corrupting its segment *can* feed bad data to readers — which is why the single-writer-per-segment discipline (§74.4.2) and reader-side validation (Ch. 72) matter. Isolation is the default; sharing is deliberate and disciplined.

### 74.2.4 Crash-only design

Design every process to be **crash-only**: the only way it stops is by crashing (or being killed), and the only way it starts is by recovering from whatever state survived. There is *no* separate "graceful shutdown" path to get wrong — stopping *is* crashing, and starting *is* crash recovery. The benefits: (1) you exercise the recovery path *every* restart, so it's always tested and fast (a rarely-run graceful-shutdown path rots and fails when you need it); (2) recovery is robust to *actual* crashes (power loss, OOM-kill, segfault) because that's the only path; (3) it pairs with fast warm restart (§74.4.4, Ch. 46) — a crash-only process keeps its durable state (in shared memory / journal, Ch. 75) so a restart reloads it warm rather than rebuilding from scratch. Crash-only means: persist what you need to recover (durably, off-process — shared memory, journal), make startup *be* recovery, and make it fast. "Kill -9 and restart" is the normal operation, not the emergency.

## 74.3 Measure it: failover/restart time and its tail

If fault containment is the goal, the metric is **how fast you recover** — and, in this book's spirit (Ch. 1), the *tail* of recovery time, because a slow restart at the wrong moment (market open, a volatility spike) is when it hurts most. Measure **detect → restart → warm → live**: from the instant a process dies to the instant its replacement is *making correct decisions at full speed*.

| Stage | What it costs | What dominates |
|---|---|---|
| **Detect** (supervisor notices death) | heartbeat timeout / `SIGCHLD` (§74.4.3) | the heartbeat interval — too long = slow detect; too short = false positives |
| **Restart** (spawn / re-exec the process) | process spawn + map shared memory (Ch. 26) | spawn cost; mapping pre-existing shm is fast |
| **Recover state** (crash-only, §74.2.4) | reload durable state from shm/journal (Ch. 75) | journal replay length — bounded by snapshot frequency |
| **Warm** (Ch. 46) | pre-touch pages, prime caches/TLB/predictors, shadow traffic | the warm-up you skip = a cold-start tail on the first real tick |
| **Live** | back to full-speed correct decisions | — |

Representative shape (figures pending real runs): a crash-only process whose state lives in shared memory and that warms before going live can recover in **~hundreds of ms to low seconds**; the *dangerous* number is the **tail** — a restart that has to rebuild state from a long journal, or that skips warming and takes cold-start stalls on its first ticks (Ch. 46), can be far slower exactly when conditions are worst.

The lessons:

- **Recovery time is detect + restart + recover + warm — measure all four, and the tail of each.** Teams measure "restart time" and forget **warm** (Ch. 46): a process that's "up" but cold is still effectively impaired for its first ticks. The case study (Ch. 76) measures *warm-and-correct*, not *process-alive*.
- **Bound recovery state with snapshots.** If recovery replays a journal (Ch. 75), the journal length bounds recovery time — take periodic state snapshots so replay starts from a recent checkpoint, not session-start (§74.4, Ch. 75).
- **Tune the heartbeat for detect-vs-false-positive.** Too slow and you're impaired longer before noticing; too fast and a GC-less hiccup looks like death and you restart needlessly (§74.4.3). Measure the false-positive rate against the detect latency.
- **The whole point is the *contained* failure has a *bounded, fast, warm* recovery** — the monolith's "recovery" is "restart everything and lose the session," which has no good tail.

## 74.4 Techniques

### 74.4.1 Shared-memory data planes between isolated processes

The processes are isolated but must exchange data on the hot path *without* a syscall per message — so they communicate through **shared-memory ring buffers** (Ch. 26, 50), not pipes/sockets:

- **`mmap`'d shared segments (Ch. 26)** carry the data plane: the feed handler writes the normalized market stream into a shared ring (Ch. 34/50); strategies read it; strategies write orders into a ring the risk/gateway read. The segment is mapped once at startup, pre-faulted and `mlock`'d (Ch. 26) so steady state never faults.
- **Lock-free cross-process rings (Ch. 50).** SPSC/MPSC rings (Ch. 34) laid out in the shared segment — same lock-free producer/consumer discipline as in-process, now across address spaces. A boundary crossing is a ring write + a ring read (tens to low-hundreds of ns — Ch. 50), *no kernel*, the cheap IPC that makes process-per-role affordable (§74.2.1).
- **Offsets, not pointers (ties Ch. 73).** A segment maps at different virtual addresses in different processes; data in it uses *offsets/indices within the segment*, never raw pointers (a raw pointer from process A is meaningless in process B). Same discipline as cross-process config (Ch. 73.4.3).
- **Control plane separate from data plane.** Slow, rare control (config — Ch. 73, commands, kill-switch) can go over a separate channel; the hot data plane stays a clean, fixed-layout shared ring.

### 74.4.2 Single-writer-per-segment discipline

The rule that keeps shared memory safe and lock-free: **exactly one process writes each segment/ring; all others only read.** This is Ch. 35's single-writer-multiple-reader publication applied across processes:

- **One writer per ring.** The feed handler is the sole writer of the market-data ring; each strategy is the sole writer of its own order ring; the gateway is the sole writer of the fill ring. Single-writer means publication is a release store (Ch. 30/35) with no inter-writer coordination — no cross-process lock (which would be slow and a shared-fate hazard).
- **Readers validate (Ch. 72).** Shared memory is the one place the fault isolation leaks (§74.2.3): a buggy writer can put garbage in its segment. Readers treat segment contents with the same untrusted-input discipline as the wire (Ch. 72) where it matters — bounds/sanity checks on indices and lengths — so a writer's corruption is caught, not propagated into a wrong trade.
- **Single-writer also bounds the blast radius of corruption** — only the segment's one writer can corrupt it; a reader's crash can't corrupt the data for others. It maps cleanly onto the fault-domain model (§74.2.3): the writer owns its segment's integrity.

### 74.4.3 Supervisor/watchdog, heartbeats and liveness

A **supervisor** process owns the lifecycle: start the roles, watch them, restart the dead, enforce policy. It is the thing that makes failures *contained-and-recovered* rather than just contained:

- **Liveness via heartbeats.** Each process periodically writes a heartbeat (a sequence/timestamp in a shared-memory slot — cheap, off the hot path); the supervisor reads them. A missed heartbeat (beyond a tuned timeout, §74.3) means the process is dead *or hung* (a hang is as bad as a crash and a plain `SIGCHLD` won't catch it — the heartbeat does). Combine `SIGCHLD`/`waitpid` (catches exits) with heartbeats (catches hangs).
- **Restart policy.** On death, the supervisor restarts *that* process (warm — §74.4.4) under a policy: backoff to avoid a crash-loop hammering, a cap on restarts-per-interval, and an escalation (if a process won't stay up, kill-switch its orders and alert rather than loop forever). The supervisor itself must be simple and rock-solid (it's a fault domain whose failure is serious) — and often supervised in turn (systemd, a watchdog, or a peer).
- **The watchdog as a safety device.** A hardware or kernel watchdog (and an application watchdog) that triggers the kill-switch/safe-state (§74.4.4) if the *system* goes unresponsive — the last-resort liveness guarantee.

### 74.4.4 Kill-switch / safe-state on failure; fast restart with warm-up

When something fails, the system must reach a **safe state** fast, then recover:

- **Kill-switch / safe-state.** On a detected failure (a dead strategy, a risk breach, a stuck process, an operator command), drive to a *safe state*: cancel the affected strategy's working orders, stop sending new orders, flatten or hold per policy. The **gateway** (separate process, §74.2.1) is what executes this — it owns the session and can cancel-on-disconnect / mass-cancel the dead component's orders *even though that component is gone*. This is *the* payoff of separating the gateway: a strategy crash doesn't strand its orders, because the surviving gateway kills them. (Ties Ch. 72 — the kill-switch is a safety/security control.)
- **Fast restart with warm-up (Ch. 46).** Restart is crash-only recovery (§74.2.4): map the surviving shared state (Ch. 26), replay from the last snapshot to current (Ch. 75), **warm** the process (pre-touch pages, prime caches/TLB/predictors, run shadow traffic — Ch. 46) so its *first real tick is at full speed*, then go live. Skipping the warm step (§74.3) means the freshly restarted process limps through its first ticks — a tail event right after a failure, when the market may be moving.
- **Determinism makes restart correct.** Because the core is a deterministic state machine (§74.2.2) fed from the captured input stream (Ch. 75), a restarted process replays the inputs and arrives at *exactly* the correct current state — no guesswork, no divergence from the process that died. Crash-only + deterministic-replay = provably-correct fast recovery.

### 74.4.5 Core dumps without stalling survivors; symbol/venue sharding

Two operational techniques for keeping failures small and debuggable:

- **Capture core dumps without stalling survivors.** A core dump of a crashed process can be large and slow to write — and if it stalls the box (I/O storm, lock, freezing the writing thread) it can hurt the *surviving* processes (a shared-fate hazard, §74.5). Mitigate: write cores to a separate disk/path off the hot cores (Ch. 42), bound/compress them, use `core_pattern` piping to an async handler that doesn't block, or capture a minimal dump. The dead process must yield *diagnostics* without taking the survivors' latency with it.
- **Symbol/venue sharding.** Partition strategies by symbol or venue across processes (and cores, Ch. 42) so that (1) one shard's crash contains to that shard's symbols — the rest keep trading (§74.2.3), and (2) load spreads across cores/NUMA nodes (Ch. 16). Sharding is fault-containment *and* scaling: a bug triggered by one symbol's data takes down only that symbol's strategy, and the blast radius is one shard, not the book. Choose the shard key (symbol, venue, asset class) so correlated work and correlated failures stay within a shard.

## 74.5 Pitfalls & anti-patterns: shared fate, cascading restarts

- **Shared fate through a back door.** The whole point is isolation, but a shared resource silently re-couples the fault domains: a shared lock (cross-process mutex — Ch. 32) one process can hold while dying (leaving it locked), a shared disk a core dump floods (§74.4.5), a shared core the dead process's restart-storm steals (Ch. 42), or shared memory a writer corrupts (§74.4.2). Audit for *every* shared resource and ensure one process's failure can't stall or corrupt another through it. Isolation you didn't verify is isolation you don't have.
- **Cascading restarts / crash loops.** A process crashes, its restart triggers load that crashes another, or a crash-loop hammers shared resources and drags everyone down. Use restart backoff, restarts-per-interval caps, and escalation-to-safe-state (§74.4.3) so a persistent failure goes to kill-switch, not an infinite loop.
- **A non-deterministic "deterministic" core.** Calling `now()`/`clock_gettime` (Ch. 17), reading a random seed, depending on map iteration order or thread scheduling, or doing I/O *inside* the core — any of these breaks exact replay (§74.2.2), and you discover it when a replay diverges from production. Time and randomness are *inputs*; the core touches nothing else. Test it: replay a captured session and assert bit-identical outputs.
- **Hidden side effects / global state in the core.** A static counter, a cached `errno`, a lazily-initialized singleton — non-obvious state that makes `apply` depend on more than (state, input). Keep the core's state explicit and passed-in.
- **Monolith creep "for latency."** Merging roles back together to save IPC hops (§74.2.1) without measuring — re-coupling fault domains for a few ns that the shared-memory data plane (§74.4.1) mostly eliminates anyway. Keep the fault-critical splits (gateway, risk); co-locate only with a benchmark *and* a fault-containment justification.
- **No kill-switch, or a kill-switch in the dead process.** If the safe-state logic lives in the strategy that just crashed, it can't run. The kill-switch must live in a *surviving* process (the gateway, §74.4.4) that can cancel the dead component's orders. A trading system without a working kill-switch is a regulatory and financial hazard (Ch. 72).
- **Restart that's up-but-cold.** Declaring recovery done when the process is *alive* rather than *warm-and-correct* (Ch. 46, §74.3). The first ticks after a cold restart are slow — measure warm-and-live, not process-alive.
- **Heartbeat that only catches exits.** Relying on `SIGCHLD` alone misses *hangs* (a live-but-stuck process). Use heartbeats to catch the hang (§74.4.3).
- **Raw pointers in shared memory.** Segments map at different addresses per process; raw pointers stored in shared memory are garbage in another process (Ch. 26, 73). Use offsets (§74.4.1).
- **Core dump stalls the survivors.** A synchronous, huge, on-the-hot-disk core dump that freezes the box (§74.4.5). Make dumping async/bounded/off-path.

## 74.6 Exercises & checklist

**Exercises:**

1. **Contain a crash.** Build a monolith (feed + strategy + gateway in one process) and a process-per-role version with shared-memory data planes (§74.4.1). Inject a segfault into the strategy in both. Show the monolith loses the market view and the venue session; the split keeps both up and the gateway cancels the dead strategy's orders (§74.4.4).
2. **Prove determinism.** Build a state-machine core (§74.2.2), capture an input stream (Ch. 75), and replay it twice — assert bit-identical outputs. Then sneak a `clock_gettime` into the core and watch replay diverge; fix it by making time an input.
3. **Measure recovery, including warm.** Kill a process and measure detect → restart → recover → warm → live (§74.3), with HdrHistogram on the tail. Then *skip* the warm step (Ch. 46) and measure the first-tick latency penalty — quantify why "up" isn't "recovered."
4. **Single-writer enforcement.** Build a shared-memory ring with one writer / many readers (§74.4.2); add reader-side validation (Ch. 72) and show it catches a deliberately corrupting writer without propagating bad data into a decision.
5. **Heartbeat hang detection.** Make a process *hang* (busy-loop, no exit) and show `SIGCHLD` misses it but a heartbeat timeout (§74.4.3) catches it and triggers restart. Tune the timeout for detect-vs-false-positive.
6. **Shard isolation.** Shard strategies by symbol across processes (§74.4.5); crash one shard's strategy on a poison symbol and confirm the other shards keep trading. Measure that the blast radius is one shard.

**Checklist:**

- [ ] The system is **process-per-role** (feed / strategy / risk / gateway), each its own **fault domain**; the **blast radius** of every single failure is bounded and stated (§74.2.1, §74.2.3).
- [ ] The decision core is a **pure, deterministic state machine** — no I/O, no clock, no randomness inside; **time and randomness are inputs**; replay produces **bit-identical** outputs (§74.2.2).
- [ ] Processes communicate via **shared-memory lock-free rings** (Ch. 26, 50) using **offsets not pointers**, pre-faulted/`mlock`'d — cheap IPC, no per-message syscall (§74.4.1).
- [ ] **Single writer per segment** (Ch. 35); readers **validate** shared-memory contents (Ch. 72); corruption is contained to the writer's segment (§74.4.2).
- [ ] A **supervisor** watches liveness via **heartbeats** (catches hangs, not just exits) and restarts under **backoff/cap/escalation** policy (§74.4.3).
- [ ] A **kill-switch / safe-state** lives in a **surviving process** (the gateway) and can cancel a dead component's working orders; the session stays up (§74.4.4).
- [ ] Processes are **crash-only** (§74.2.4) with durable state in shm/journal; restart is **recovery + warm-up (Ch. 46)** measured to **warm-and-correct**, not process-alive (§74.2.4, §74.3, §74.4.4).
- [ ] **Core dumps** are async/bounded/off-path and **don't stall survivors**; strategies are **sharded** by symbol/venue so one shard's crash contains to that shard (§74.4.5).
- [ ] **No shared-fate back doors** (shared locks/disk/core/segment) re-couple the fault domains — audited and verified (§74.5).

## 74.7 References

- The **crash-only software** paper (Candea & Fox) — crash-only design, recovery-as-the-only-path (§74.2.4).
- The **Erlang/OTP** supervision-tree and "let it crash" literature — process-per-role, supervisors, fault containment as architecture (§74.2.1, §74.4.3).
- The LMAX architecture write-ups (Ch. 37 references) — the deterministic business-logic core decoupled from I/O, fed from an event stream, for replay (§74.2.2).
- Ch. 26 (shared memory), Ch. 50 (cross-process IPC rings), Ch. 35 (single-writer publication), Ch. 46 (warming), Ch. 75 (capture/replay — the input stream the core replays).
- Regulatory guidance on pre-trade risk controls and kill-switches (e.g. SEC Rule 15c3-5 "market access") — why the risk gate and kill-switch are mandatory, not optional (§74.4.4).

## 74.8 Additional Reading

- Talks on trading-system architecture, fault domains, and deterministic replay (LMAX, exchange/matching-engine designs) — the process topology and state-machine pattern in production (§74.2).
- The functional-core / imperative-shell and event-sourcing literature — the pure-core/impure-edge split and replay from an event log (§74.2.2).
- Ch. 75 (*Capture/Persistence/Replay*) — recording the input stream and replaying it through the deterministic core; Ch. 76 (*Case Study*) — the whole topology end-to-end with replay-based regression gates; Ch. 46 (*Warming*) — fast warm restart; Ch. 72 (*Security*) — kill-switch and reader-side validation; Ch. 73 (*Hot Reload*) — config distribution to the processes.
- **Appendix F** — fault-domain / crash-only / state-machine / kill-switch glossary; **Appendix C** — isolation and core-affinity tuning that keeps the processes off each other's cores.

---

*Next: Ch. 75 — Capture, Persistence & Replay Storage, recording every inbound tick and outbound order for deterministic replay, research, and compliance: lossless line capture with nanosecond timestamps (Ch. 55, 58), append-only/write-ahead journals and compact binary formats (Ch. 71), getting bytes to disk off the hot path (io_uring/`O_DIRECT`/NVMe — Ch. 48), and feeding captures into the deterministic state machine (Ch. 74) for replay and backtests.*
