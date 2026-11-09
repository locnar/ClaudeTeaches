# Part XIII — Observability & Operations in Production

# Chapter 89 — Process Topology & The Deterministic State Machine

> **Prerequisites:** Ch. 29 (mmap/shared memory — the inter-process data plane), Ch. 57 (IPC / cross-process ring buffers — how the processes talk), Ch. 38 (seqlock/single-writer publication — the per-segment discipline), Ch. 53 (warming — fast restart isn't fast if it's cold), Ch. 88 (hot reload — config distribution to the processes), Ch. 87 (security — privilege isolation, kill-switch/safe-state).
>
> **Leads into:** Ch. 94 (capture/replay — the deterministic state machine is what you replay *into*), Ch. 101 (case study — the whole topology end-to-end). A Part XIII architecture chapter: structuring the system as cooperating processes so one failure can't take down the session, and isolating a pure, replayable core.

---

## 89.1 Why it matters: contain a crash, keep the venue session up

Everything before this chapter optimized a *process*. This one is about what happens when that process **crashes** — because it will. A null deref in a new strategy, an unhandled malformed packet that slipped past Ch. 87's gate, an assertion, a bad config, an OOM, a hardware glitch — software fails, and a trading system runs real money through it during live sessions. The architectural question is not *whether* a component fails but **what its failure takes down with it.** In a **monolith** — feed handler, strategy, risk, and order gateway all in one process — the answer is *everything*: one strategy's segfault kills the feed handler (you go blind to the market), the order gateway (you drop the venue session and orphan working orders), and risk (you lose the safety check) — all at once, mid-session, with open positions. A single bug becomes a total outage.

The alternative is **process-per-role**: split the system into separate OS processes — feed handler, strategy (often one per strategy/symbol-group), risk, order gateway — each in its own address space, communicating through shared-memory data planes (Ch. 29, 57). Now a crash is **contained**: if a strategy process dies, the feed handler keeps decoding the market, the gateway keeps the venue session alive and can cancel that strategy's working orders (kill-switch, §89.4.4), risk keeps enforcing limits, and a supervisor restarts just the dead process (warm — Ch. 53) while everything else keeps running. The **blast radius** of a failure shrinks from "the whole system" to "one role, one shard." For a system where an outage during market hours is a financial and regulatory event, fault containment is not optional polish — it's the difference between "we restarted one strategy in 200 ms" and "we were down and blind with open positions."

There's a second, equally important reason rooted in the rest of Part XIII: **determinism and replayability.** If the core decision logic is a **pure, side-effect-free state machine** — same inputs always produce the same outputs, no I/O or clocks or randomness inside it — then you can **replay** a captured session (Ch. 94) through it and get *exactly* the same decisions, bit-for-bit. That's the foundation of debugging ("replay the crash"), backtesting ("replay history through the new strategy"), compliance ("prove what we did and why"), and regression testing (Ch. 101). Process topology and the deterministic state machine are two halves of the same idea: **separate the fragile, I/O-bound, non-deterministic edges (network, timers, OS) from a contained, pure, replayable core** — so failures are isolated *and* the system is reproducible. This chapter motivates the topology and the pattern (§89.2), measures restart/failover time and its tail (§89.3), and builds the shared-memory data planes, single-writer discipline, supervision, kill-switch, fast restart, and sharding that make it work (§89.4).

## 89.2 Mental model

### 89.2.1 Process-per-role vs monolith (feed handler / strategy / order gateway / risk)

Decompose the tick-to-trade path into separate processes by **role**, each a fault domain:

```
   wire ─► [ FEED HANDLER ]─shm─►[ STRATEGY(s) ]─shm─►[ RISK ]─shm─►[ ORDER GATEWAY ]─► wire
            decode (Ch.60)        pure decision        pre-trade      venue session,
            normalize, seq        (state machine)      limits/kill    order I/O
                 │                      │                  │                │
            (one process)          (one per strategy/   (one process)   (one process,
                                    symbol shard)                        holds the session)
```

- **Feed handler** — owns the market-data side (Ch. 60–63): decode, normalize, sequence/gap-handle (A/B arbitration and recovery — Ch. 60 §60.4.3, Ch. 61), publish a clean normalized stream into shared memory. If a strategy dies, the feed handler is untouched — the market view stays live.
- **Strategy** — the decision logic; often **one process per strategy or per symbol/venue shard** (§89.4.5) so strategies can't crash each other. Ideally a *pure state machine* (§89.2.2).
- **Risk** — the pre-trade gate (limits, position, kill-switch — built in Ch. 90): a separate process (or a mandatory in-line check) that no strategy can bypass or crash. The last line before an order leaves.
- **Order gateway** — owns the *venue session* (Ch. 58): the TCP/order-entry connection, sequence numbers, working-order state. Keeping this separate means a strategy crash doesn't drop the session or orphan orders — the gateway can cancel-on-disconnect / kill-switch the dead strategy's orders and keep the session up. Order identity, the in-flight order table and post-crash order-state recovery are Ch. 91.

The trade-off is **latency vs containment**: crossing a process boundary costs an IPC hop (a shared-memory ring write/read — Ch. 57; ~50–150 ns one-way *latency* per hop, a cross-core cache-line transfer — Ch. 36, Appendix E's HITM row; Ch. 57 §57.3's ~7–15 ns/msg is amortized throughput cost, not hop latency — *not* a syscall if done right). A monolith saves those hops but couples all failures. The HFT resolution: use **shared-memory data planes** (§89.4.1) so the hops are cheap (no kernel, no copy — Ch. 29/57), and accept a small, bounded latency cost for a large containment win. The hottest, most-coupled stages can be co-located if a benchmark justifies it; the *fault-critical* split (gateway and risk separate from strategy) is rarely worth giving up.

### 89.2.2 The Deterministic State Machine Pattern: pure, side-effect-free core decoupled from I/O for exact replayability

The most important pattern in the chapter: **isolate the core decision logic into a pure, deterministic state machine, decoupled from all I/O, timers, and randomness.**

```
   IMPURE EDGE (non-deterministic):              PURE CORE (deterministic):
   ──────────────────────────────               ──────────────────────────
   network RX (Ch.54-70)                         apply(state, input) → (state', outputs)
   timers / clocks (Ch.17)            ─inputs─►   • NO syscalls, NO I/O, NO clock reads
   RNG, OS scheduling                             • NO wall-clock now() inside — time is an INPUT
   network TX                         ◄─outputs─  • same (state, input sequence) ⇒ same outputs, ALWAYS
```

The core is a function `apply(state, input) → (new_state, outputs)` — a **fold over the input stream**. As a type, with the replay loop that proves the property:

```cpp
// dsm.cpp — the deterministic core as a type, and the replay loop that proves it. Build: g++ -O2 -std=c++20 dsm.cpp -o dsm
#include <cstdint>
#include <cstdio>
#include <span>
#include <variant>
#include <vector>

struct MdEvent    { std::uint64_t seq, ts_ns; std::uint32_t sym; std::int64_t px; std::int32_t qty; };
struct Fill       { std::uint64_t seq, ts_ns, order_id; std::int64_t px; std::int32_t qty; };
struct TimerFire  { std::uint64_t seq, ts_ns; };                 // time is an INPUT, never read inside
struct ConfigSwap { std::uint64_t seq, ts_ns, version; };
using Input = std::variant<MdEvent, Fill, TimerFire, ConfigSwap>;   // one sequenced stream, assigned by the shell (Ch. 94)

struct Output { enum Kind : std::uint8_t { NewOrder, Cancel, Log } kind; std::uint64_t order_id; std::int64_t px; std::int32_t qty; };

struct State { std::int64_t best_bid = 0, best_ask = 0; std::int32_t position = 0; std::uint64_t next_order = 1, config_version = 0; };

// apply(): pure. No clock, no syscall, no I/O, no globals, no randomness — the fold over the input stream.
std::size_t apply(State& s, const Input& in, std::span<Output> out) noexcept {
    std::size_t n = 0;
    std::visit([&](const auto& e) {
        using T = std::decay_t<decltype(e)>;
        if constexpr (std::is_same_v<T, MdEvent>) {
            if (e.qty > 0) s.best_bid = e.px; else s.best_ask = e.px;
            if (s.best_ask && s.best_bid >= s.best_ask - 1 && s.position < 100 && n < out.size())
                out[n++] = {Output::NewOrder, s.next_order++, s.best_ask, 10};
        } else if constexpr (std::is_same_v<T, Fill>)       { s.position += e.qty; }
        else if constexpr (std::is_same_v<T, ConfigSwap>)   { s.config_version = e.version; }
        // TimerFire: housekeeping decided from e.ts_ns, not from any clock
    }, in);
    return n;
}

// Replay harness (Ch. 94 → CI gate, §101.4.1): same inputs ⇒ bit-identical outputs, or the build fails.
bool replay_matches(std::span<const Input> log, std::span<const Output> golden) {
    State s; std::vector<Output> produced; Output buf[8];
    for (const Input& in : log) { std::size_t n = apply(s, in, buf); produced.insert(produced.end(), buf, buf + n); }
    if (produced.size() != golden.size()) return false;
    for (std::size_t i = 0; i < golden.size(); ++i)
        if (produced[i].kind != golden[i].kind || produced[i].order_id != golden[i].order_id ||
            produced[i].px != golden[i].px || produced[i].qty != golden[i].qty) return false;
    return true;
}

int main() {
    std::vector<Input> log{ MdEvent{1, 100, 7, 10000, 5}, MdEvent{2, 101, 7, 10001, -5}, Fill{3, 150, 1, 10001, 10}, TimerFire{4, 200} };
    std::vector<Output> golden; { State s; Output buf[8]; for (auto& in : log) { auto n = apply(s, in, buf); golden.insert(golden.end(), buf, buf + n); } }
    std::printf("replay %s (%zu outputs)\n", replay_matches(log, golden) ? "bit-identical" : "DIVERGED", golden.size());
}
```

Crucially:

- **No side effects inside.** It reads no clock, makes no syscall, does no network I/O, touches no global mutable state, uses no randomness. Anything non-deterministic — the current time, a random seed, a sequence number — is passed *in as part of the input*, never read from the environment inside the core. (Time becomes a *timestamped input event*, so replay reproduces the exact timeline.)
- **Inputs are an ordered, captured stream.** Every input — each market-data message, each timer fire, each fill, each config change (Ch. 88) — is a record in a single ordered log (Ch. 94) — produced, when more than one consumer must agree on the order, by the sequencer of Ch. 96. The core consumes that stream.
- **The shell is the sequencer.** The impure shell around the core assigns a single global **sequence number** to *every* input — market data, fills, timer fires, config swaps — before the core sees it; that one sequenced stream, not the raw sources, is what Ch. 94 captures and what replay feeds back. Without it, two sources' relative order is a race the replay cannot reproduce.
- **Determinism ⇒ exact replay.** Because outputs depend *only* on (initial state + input sequence), feeding the *same captured input log* through the core reproduces the *same outputs exactly* — the same orders, the same decisions, bit-for-bit. This is the property everything in Ch. 94–101 relies on: replay the captured session to debug a production crash, to backtest a strategy change against real history, to prove compliance, to gate regressions in CI.

This is the same edge/core split as functional-core/imperative-shell, applied to trading for *both* fault isolation (the pure core has fewer ways to fail and no I/O to hang on) and reproducibility. The I/O, timers, and OS interaction — the non-deterministic, failure-prone parts — live in a thin shell around the core; the core itself is pure, testable, and replayable.

### 89.2.3 Fault domains and blast radius

A **fault domain** is a boundary a failure can't cross; the **blast radius** is what one failure actually takes down. Process boundaries (separate address spaces) are the strongest in-box fault domain: a segfault, heap corruption, or assertion in one process cannot corrupt another's memory or take down its thread — the OS contains it. Design so that:

- **Each role is its own fault domain** (§89.2.1) — a strategy crash can't corrupt the feed handler or gateway.
- **Each shard is its own fault domain** (§89.4.5) — one symbol/venue's strategy crash doesn't touch another's.
- **The blast radius of any single failure is bounded and known** — you can state, for each component, exactly what its death takes down and what survives. The gateway and risk survive *every* strategy failure (so orders can be cancelled and the session kept up); a feed handler failure is contained to its feed (the redundant A/B line and the recovery channels, Ch. 60–61, and other feeds survive).
- **Shared memory is a controlled hole in the isolation** (§89.4.1-2): processes share data-plane segments, so a writer corrupting its segment *can* feed bad data to readers — which is why the single-writer-per-segment discipline (§89.4.2) and reader-side validation (Ch. 87) matter. Isolation is the default; sharing is deliberate and disciplined.

### 89.2.4 Crash-only design

Design every process to be **crash-only**: the only way it stops is by crashing (or being killed), and the only way it starts is by recovering from whatever state survived. There is *no* separate "graceful shutdown" path to get wrong — stopping *is* crashing, and starting *is* crash recovery. The benefits: (1) you exercise the recovery path *every* restart, so it's always tested and fast (a rarely-run graceful-shutdown path rots and fails when you need it); (2) recovery is robust to *actual* crashes (power loss, OOM-kill, segfault) because that's the only path; (3) it pairs with fast warm restart (§89.4.4, Ch. 53) — a crash-only process keeps its durable state (in shared memory / journal, Ch. 94) so a restart reloads it warm rather than rebuilding from scratch. Crash-only means: persist what you need to recover (durably, off-process — shared memory, journal), make startup *be* recovery, and make it fast. "Kill -9 and restart" is the normal operation, not the emergency.

## 89.3 Measure it: failover/restart time and its tail

If fault containment is the goal, the metric is **how fast you recover** — and, in this book's spirit (Ch. 1), the *tail* of recovery time, because a slow restart at the wrong moment (market open, a volatility spike) is when it hurts most. Measure **detect → restart → warm → live**: from the instant a process dies to the instant its replacement is *making correct decisions at full speed*.

| Stage | What it costs | What dominates |
|---|---|---|
| **Detect** (supervisor notices death) | heartbeat timeout / `SIGCHLD` (§89.4.3) | the heartbeat interval — too long = slow detect; too short = false positives |
| **Restart** (spawn / re-exec the process) | process spawn + map shared memory (Ch. 29) | spawn cost; mapping pre-existing shm is fast |
| **Recover state** (crash-only, §89.2.4) | reload durable state from shm/journal (Ch. 94) | journal replay length — bounded by snapshot frequency |
| **Warm** (Ch. 53) | pre-touch pages, prime caches/TLB/predictors, shadow traffic | the warm-up you skip = a cold-start tail on the first real tick |
| **Live** | back to full-speed correct decisions | — |

The harness is the §89.4.3 supervisor itself: it `fork`/`exec`s a role that publishes a heartbeat sequence into a shared-memory slot; loop N times — `kill -9` the role (and, separately, `SIGSTOP` it to simulate a hang), timestamp (`CLOCK_MONOTONIC`) the detect (SIGCHLD vs heartbeat timeout), the re-exec, the shm attach, the snapshot + journal replay (Ch. 94) and the warm-up (Ch. 53), and record each as a histogram (Ch. 3) alongside the *survivors'* hot-path latency during the episode. Representative shape (toolchain and machine: Appendix L.3; figures pending real runs): a crash-only process whose state lives in shared memory and that warms before going live can recover in **~hundreds of ms to low seconds**; the *dangerous* number is the **tail** — a restart that has to rebuild state from a long journal, or that skips warming and takes cold-start stalls on its first ticks (Ch. 53), can be far slower exactly when conditions are worst.

The lessons:

- **Recovery time is detect + restart + recover + warm — measure all four, and the tail of each.** Teams measure "restart time" and forget **warm** (Ch. 53): a process that's "up" but cold is still effectively impaired for its first ticks. The case study (Ch. 101) measures *warm-and-correct*, not *process-alive*.
- **Bound recovery state with snapshots.** If recovery replays a journal (Ch. 94), the journal length bounds recovery time — take periodic state snapshots so replay starts from a recent checkpoint, not session-start (§89.4, Ch. 94).
- **Tune the heartbeat for detect-vs-false-positive.** Too slow and you're impaired longer before noticing; too fast and a transient stall (a page fault, an SMI, a scheduler preemption on a non-isolated core) looks like death and you restart needlessly (§89.4.3). Measure the false-positive rate against the detect latency.
- **The whole point is the *contained* failure has a *bounded, fast, warm* recovery** — the monolith's "recovery" is "restart everything and lose the session," which has no good tail.

## 89.4 Techniques

### 89.4.1 Shared-memory data planes between isolated processes

The processes are isolated but must exchange data on the hot path *without* a syscall per message — so they communicate through **shared-memory ring buffers** (Ch. 29, 57), not pipes/sockets:

- **`mmap`'d shared segments (Ch. 29)** carry the data plane: the feed handler writes the normalized market stream into a shared ring (Ch. 37/57); strategies read it; strategies write orders into a ring the risk/gateway read. The segment is mapped once at startup, pre-faulted and `mlock`'d (Ch. 29) so steady state never faults.
- **Lock-free cross-process rings (Ch. 57).** SPSC/MPSC rings (Ch. 37) laid out in the shared segment — same lock-free producer/consumer discipline as in-process, now across address spaces. A boundary crossing is a ring write + a ring read (tens to low-hundreds of ns — Ch. 57), *no kernel*, the cheap IPC that makes process-per-role affordable (§89.2.1).
- **Offsets, not pointers (ties Ch. 88).** A segment maps at different virtual addresses in different processes; data in it uses *offsets/indices within the segment*, never raw pointers (a raw pointer from process A is meaningless in process B). Same discipline as cross-process config (§88.4.3).
- **Dead readers and segment generations.** A reader process that dies mid-ring leaves a stale consumer cursor: a ring with back-pressure stalls on it (never on the hot path — Ch. 84's rule) and a ring without it overruns it; the supervisor (§89.4.3) must *reap* a dead reader's cursor (mark it detached, or let producers ignore cursors whose heartbeat has lapsed). A *writer* restart needs a **segment generation**: the restarted writer stamps a new generation into the segment header and re-initialises its rings, and readers that see the generation change resynchronise rather than consuming half-written state from the previous life.
- **Control plane separate from data plane.** Slow, rare control (config — Ch. 88, commands, kill-switch) can go over a separate channel; the hot data plane stays a clean, fixed-layout shared ring.

### 89.4.2 Single-writer-per-segment discipline

The rule that keeps shared memory safe and lock-free: **exactly one process writes each segment/ring; all others only read.** This is Ch. 38's single-writer-multiple-reader publication applied across processes:

- **One writer per ring.** The feed handler is the sole writer of the market-data ring; each strategy is the sole writer of its own order ring; the gateway is the sole writer of the fill ring. Single-writer means publication is a release store (Ch. 33/38) with no inter-writer coordination — no cross-process lock (which would be slow and a shared-fate hazard).
- **Readers validate (Ch. 87).** Shared memory is the one place the fault isolation leaks (§89.2.3): a buggy writer can put garbage in its segment. Readers treat segment contents with the same untrusted-input discipline as the wire (Ch. 87) where it matters — bounds/sanity checks on indices and lengths — so a writer's corruption is caught, not propagated into a wrong trade.
- **Single-writer also bounds the blast radius of corruption** — only the segment's one writer can corrupt it; a reader's crash can't corrupt the data for others. It maps cleanly onto the fault-domain model (§89.2.3): the writer owns its segment's integrity.

### 89.4.3 Supervisor/watchdog, heartbeats and liveness

A **supervisor** process owns the lifecycle: start the roles, watch them, restart the dead, enforce policy. It is the thing that makes failures *contained-and-recovered* rather than just contained. Its whole loop is short enough to show:

```cpp
// supervisor.cpp — the role supervisor: start, watch (SIGCHLD + heartbeat), restart with backoff, measure recovery.
// Build: g++ -O2 -std=c++20 supervisor.cpp -o supervisor      Run: ./supervisor ./strategy --shard 3
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <ctime>
#include <fcntl.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>

struct Heartbeat { std::atomic<std::uint64_t> seq; std::atomic<std::uint64_t> ts_ns; };   // one slot per role, in shm

static std::uint64_t now_ns() { timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return std::uint64_t(t.tv_sec) * 1'000'000'000ull + t.tv_nsec; }

static pid_t spawn(char** argv) {
    pid_t p = fork();
    if (p == 0) { execv(argv[0], argv); _exit(127); }               // child: the role binary maps the same shm by name
    return p;
}

int main(int argc, char** argv) {
    if (argc < 2) return 1;
    int fd = shm_open("/hb_strategy", O_CREAT | O_RDWR, 0600); (void)!ftruncate(fd, sizeof(Heartbeat));
    auto* hb = static_cast<Heartbeat*>(mmap(nullptr, sizeof(Heartbeat), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0));
    constexpr std::uint64_t kTimeoutNs = 50'000'000;               // tuned per §89.3: detect-latency vs false-positive
    unsigned restarts_this_minute = 0; std::uint64_t backoff_ns = 10'000'000, minute_start = now_ns();
    pid_t child = spawn(argv + 1); std::uint64_t last_seq = 0, last_seen = now_ns();
    for (;;) {
        usleep(1000);
        int status; bool dead = waitpid(child, &status, WNOHANG) == child;           // catches exits
        std::uint64_t seq = hb->seq.load(std::memory_order_acquire);
        if (seq != last_seq) { last_seq = seq; last_seen = now_ns(); }
        bool hung = now_ns() - last_seen > kTimeoutNs;                                // catches hangs — SIGCHLD cannot
        if (!dead && !hung) continue;
        const std::uint64_t t_detect = now_ns();
        if (hung) { kill(child, SIGKILL); waitpid(child, &status, 0); }                // a hang is a crash we must cause
        // kill-switch first (§89.4.4): tell the gateway to cancel this shard's working orders — then restart
        if (now_ns() - minute_start > 60'000'000'000ull) { restarts_this_minute = 0; minute_start = now_ns(); }
        if (++restarts_this_minute > 5) { std::fprintf(stderr, "crash-loop: safe-state and alert\n"); return 2; }
        struct timespec b{0, long(backoff_ns)}; nanosleep(&b, nullptr); backoff_ns = backoff_ns * 2 > 1'000'000'000 ? 1'000'000'000 : backoff_ns * 2;
        child = spawn(argv + 1); last_seen = now_ns();
        while (hb->seq.load(std::memory_order_acquire) == last_seq) usleep(100);     // first heartbeat = "warm and live" (Ch. 53)
        std::printf("recovered in %.1f ms (detect %s)\n", (now_ns() - t_detect) / 1e6, dead ? "SIGCHLD" : "heartbeat timeout");
    }
}
```


- **Liveness via heartbeats.** Each process periodically writes a heartbeat (a sequence/timestamp in a shared-memory slot — cheap, off the hot path); the supervisor reads them. The natural home for the slot is the telemetry segment of Ch. 86, next to a *progress* counter (last sequence processed) — a loop that is spinning but stuck still heartbeats, and only the progress counter catches it. A missed heartbeat (beyond a tuned timeout, §89.3) means the process is dead *or hung* (a hang is as bad as a crash and a plain `SIGCHLD` won't catch it — the heartbeat does). Combine `SIGCHLD`/`waitpid` (catches exits) with heartbeats (catches hangs).
- **Restart policy.** On death, the supervisor restarts *that* process (warm — §89.4.4) under a policy: backoff to avoid a crash-loop hammering, a cap on restarts-per-interval, and an escalation (if a process won't stay up, kill-switch its orders and alert rather than loop forever). The supervisor itself must be simple and rock-solid (it's a fault domain whose failure is serious) — and often supervised in turn (systemd, a watchdog, or a peer).
- **The watchdog as a safety device.** A hardware or kernel watchdog (and an application watchdog) that triggers the kill-switch/safe-state (§89.4.4) if the *system* goes unresponsive — the last-resort liveness guarantee.

### 89.4.4 Kill-switch / safe-state on failure; fast restart with warm-up

When something fails, the system must reach a **safe state** fast, then recover:

- **The readiness state machine (Appendix M).** Every role publishes one of `cold → warming → syncing → reconciled → armed → trading → disarmed` in the telemetry segment's header (Ch. 86 §86.4.1); the order path reads it with a single load and a predicted branch (`may_send()` — Appendix M.1) and refuses to send in any state but `trading`. Transitions forward are earned by the pre-open gates (host attestation, clock, feed, session, positions, warm-up — Appendix M.2–M.4); *any* failing invariant (a dark feed, a dead session, a host-health delta — Ch. 99/100) or the kill-switch below drops the role to `disarmed`, and the only way back is to re-run the gates from the failed one (a time-boxed re-arm after an intraday restart — Appendix M.5). The supervisor writes `disarmed` into a dead role's slot before restarting it, so nothing ever reads a crashed process as `trading`. The control plane that drives the transitions is the same off-hot-path channel as the kill-switch command: never a signal on the hot thread.
- **Kill-switch / safe-state.** On a detected failure, drive to a *safe state*: stop new orders, cancel the affected working orders, flatten or hold per policy. The **gateway** (separate process, §89.2.1) is what executes this — it owns the session and cancels the dead component's orders *even though that component is gone*; the venue's cancel-on-disconnect only fires if the *gateway's* session drops, which is the opposite of what you want here. This is *the* payoff of separating the gateway: a strategy crash doesn't strand its orders, because the surviving gateway kills them. The triggers (reject-rate, position drift vs drop-copy, drawdown, feed staleness, heartbeat loss, operator), the trigger-to-safe-state sequence and its measured time (a live-order bitmap vs a table walk: 4 µs vs 300 µs for 400 orders) are **Ch. 90 §90.2.4 and §90.4.5–§90.4.6**; the working-order set it enumerates is Ch. 91's in-flight table.
- **When the gateway itself dies.** The gateway is the one role whose failure *is* a session failure: the venue's **cancel-on-disconnect** (enable it — Appendix K) is the safety net that flattens working orders, and recovery is a **hot-standby gateway** that re-logs on and performs sequence-number recovery (replaying the venue's unacknowledged messages — the session protocol's resend request) before the strategies are allowed to resume. Measure that path (§89.3) like any other restart; it is the longest one.
- **Fast restart with warm-up (Ch. 53).** Restart is crash-only recovery (§89.2.4): map the surviving shared state (Ch. 29), replay from the last snapshot to current (Ch. 94), **warm** the process (pre-touch pages, prime caches/TLB/predictors, run shadow traffic — Ch. 53) so its *first real tick is at full speed*, then go live. Skipping the warm step (§89.3) means the freshly restarted process limps through its first ticks — a tail event right after a failure, when the market may be moving.
- **Determinism makes restart correct.** Because the core is a deterministic state machine (§89.2.2) fed from the captured input stream (Ch. 94), a restarted process replays the inputs and arrives at *exactly* the correct current state — no guesswork, no divergence from the process that died. Crash-only + deterministic-replay = provably-correct fast recovery.

### 89.4.5 Symbol/venue sharding (core dumps → Ch. 93)

Two operational techniques for keeping failures small and debuggable — one developed here, one handed to the next chapter:

- **Core dumps that do not stall survivors — Ch. 93.** A crashed role must yield *diagnostics* without taking the survivors' latency with it: the kill-switch handoff from inside the fault handler (so the gateway cancels the dead shard's orders ~20 µs after the fault, not after a 20 s dump), `madvise(MADV_DONTDUMP)` on the hugepage-backed book and data-plane segments every role maps, `coredump_filter`, `RLIMIT_CORE` (not enforced for piped dumps), a bounded `core_pattern` pipe handler on a housekeeping core writing to a separate disk, systemd-coredump's limits, and minidumps. Ch. 93 builds the async-signal-safe handler and measures the dump's cost to the survivors (a 2–4 GiB dump stretched a survivor's worst gap 20–50×; excluding the book shrank it to a sub-millisecond event).
- **Symbol/venue sharding.** Partition strategies by symbol or venue across processes (and cores, Ch. 48) so that (1) one shard's crash contains to that shard's symbols — the rest keep trading (§89.2.3), and (2) load spreads across cores/NUMA nodes (Ch. 16). Sharding is fault-containment *and* scaling: a bug triggered by one symbol's data takes down only that symbol's strategy, and the blast radius is one shard, not the book. Choose the shard key (symbol, venue, asset class) so correlated work and correlated failures stay within a shard.

### 89.4.6 Divergence forensics and determinism auditing

§89.2.2 promised bit-identical replay and §89.5 lists the ways a core stops being deterministic; Ch. 101 §101.4.1 makes "replay matches golden" a CI gate. What none of them gives is a way to *localise* a divergence: a replay of a four-hour session that produces a different order at 11:52:07 has, by then, processed 140 million events, and "the outputs differ" is unactionable. The fix is a **per-event state digest** journaled beside the outputs, a diff of digest streams to find the first diverging event, and a lint of the core for the known causes — measured here on the usual VM (GCC 13.3, `-O2 -march=native`, Linux 6.18; a 4096-level book, ten million level updates):

```
   book update variant                                                    cycles/event      digest size per event
   ───────────────────────────────────────────────────────────────────── ──────────────── ─────────────────────────
   no digest                                                                 6.8              —
   incremental: crc32c of (level index, new qty, new count) chained          6.9  (+0.1)      8 B
   full-state hash (4096 levels) every event                               17,105             8 B
   full-state hash every 4096 events (amortised)                              8.5  (+1.7)      8 B / 4096 events
```

- **Hash what changed, chained.** `digest = crc32c(crc32c(crc32c(digest, idx), qty), n)` after each mutation — three `crc32q` instructions (Ch. 31; 3-cycle latency each, pipelined with the update's own stores) — costs 0.1 cycles per event over the bare update and yields a running digest that depends on *every* mutation in order. It is not a hash of the state (two different states could collide; a mutation that is applied and reverted leaves a trace) and that is exactly what forensics wants: a digest of the *trajectory*, which diverges at the first event whose effect differed. A periodic full-state hash (every 4096 events, +1.7 cycles amortised) is the check that the incremental digest and the state agree — it catches a mutation that bypassed the digesting path (a `memcpy` into the book, a reset).
- **Journal it as a column.** The Ch. 94 §94.4.2 journal record gains an 8-byte `digest` field next to the output it describes; the replay harness writes the same column. The incremental digest is why this is affordable — 8 bytes per event on a path that already writes ~64 — and the gap-free sequencing of Ch. 94 §94.4.4 is what makes the two streams comparable by position.
- **Find the first diverging event.** `cmp` the two digest columns (or the `paste | awk '$1 != $2 {print NR; exit}'` equivalent): in the measurement, a planted nondeterminism — a `clock_gettime` whose nanosecond parity nudged one quantity at event 6,000,001 — was located at exactly #6000001 by a single linear pass over ten million digests. The alternative when the streams are too large to hold is **checkpoint bisection**: snapshot the core's state every *N* events (Ch. 94 §94.4.5's snapshots), replay from the checkpoint before the midpoint, compare the digest at the midpoint, and recurse — 24 replays of a shrinking window for ten million events. Either way the result is an *event number*, and the replay harness's `--stop-at-divergence` mode (replay until the journaled digest and the computed one differ, then `raise(SIGTRAP)`) drops you into gdb or `rr` (Ch. 5 §5.4.2) at the first input the two executions disagreed on, with the state that disagreed in memory.
- **The determinism lint.** The known causes, as things to grep for and to test:

  ```
     cause                                       how it leaks                                             test / fix
     ─────────────────────────────────────────── ──────────────────────────────────────────────────────── ─────────────────────────────────────────────
     clocks: clock_gettime, rdtsc, steady_clock  a timestamp becomes part of a decision or an output      grep the core for them; time is an INPUT event
     randomness: rand, random_device, a seed     path choice differs                                      seed is an input; PRNG state is core state
     pointer-keyed hashing under ASLR            unordered_map<T*> / hash<void*> iteration order and       replay with ASLR on twice: digests differ →
                                                 bucket placement differ run to run (Ch. 28)              key by id/index, or iterate a sorted view
     unordered iteration                         any order-dependent fold over an unordered container     same test; use flat_map/sorted vectors (Ch. 28)
     uninitialised padding in journaled structs  Ch. 9's padding bytes carry stack garbage into the       static_assert(has_unique_object_representations),
                                                 journal and the digest                                   or memset before fill; digest fields, not bytes
     FP reassociation / -ffast-math / FMA        bit-different sums across builds or -march (Ch. 30 §30.4.3) strict FP in the core; fixed-point for prices
     thread scheduling / shared state            a second thread's writes interleave differently          the core is single-threaded; inputs via one ring
     environment: locale, env vars, hostname     to_chars/format differences; config read from the host   configuration is a versioned INPUT (Ch. 88 §88.4.3)
     hidden statics: counters, errno, singletons `apply` depends on more than (state, input) (§89.5)       state is explicit; a reset()-and-replay test
  ```

  The two tests that catch most of it: replay the golden capture **twice with ASLR enabled** (`setarch -R` is the thing you must *not* use here) and require identical digest streams — pointer-keyed hashing and uninitialised padding fail this immediately; and build the core with GCC and Clang at `-march=x86-64-v3` and `-march=native` and require the same — FP reassociation fails this. Both run in the Ch. 101 §101.4.1 gate, which now fails with an event number instead of a diff.

## 89.5 Pitfalls & anti-patterns: shared fate, cascading restarts

- **Shared fate through a back door.** The whole point is isolation, but a shared resource silently re-couples the fault domains: a shared lock (cross-process mutex — Ch. 35) one process can hold while dying (leaving it locked), a shared disk a core dump floods (Ch. 93 §93.4.4), a shared core the dead process's restart-storm steals (Ch. 48), or shared memory a writer corrupts (§89.4.2). Audit for *every* shared resource and ensure one process's failure can't stall or corrupt another through it. Isolation you didn't verify is isolation you don't have.
- **Cascading restarts / crash loops.** A process crashes, its restart triggers load that crashes another, or a crash-loop hammers shared resources and drags everyone down. Use restart backoff, restarts-per-interval caps, and escalation-to-safe-state (§89.4.3) so a persistent failure goes to kill-switch, not an infinite loop.
- **A non-deterministic "deterministic" core.** Calling `now()`/`clock_gettime` (Ch. 17), reading a random seed, depending on map iteration order or thread scheduling, or doing I/O *inside* the core — any of these breaks exact replay (§89.2.2), and you discover it when a replay diverges from production. Time and randomness are *inputs*; the core touches nothing else. Test it: replay a captured session and assert bit-identical outputs — and journal the per-event digest (§89.4.6) so the failure names the *event*, not just the fact.
- **Hidden side effects / global state in the core.** A static counter, a cached `errno`, a lazily-initialized singleton — non-obvious state that makes `apply` depend on more than (state, input). Keep the core's state explicit and passed-in.
- **Monolith creep "for latency."** Merging roles back together to save IPC hops (§89.2.1) without measuring — re-coupling fault domains for a few ns that the shared-memory data plane (§89.4.1) mostly eliminates anyway. Keep the fault-critical splits (gateway, risk); co-locate only with a benchmark *and* a fault-containment justification.
- **No kill-switch, or a kill-switch in the dead process.** If the safe-state logic lives in the strategy that just crashed, it can't run. The kill-switch must live in a *surviving* process (the gateway, §89.4.4) that can cancel the dead component's orders. A trading system without a working kill-switch is a regulatory and financial hazard (the market-access / pre-trade-risk mandate — Appendix K §K.4, SEC 15c3-5; the safe-state discipline — Ch. 61 §61.4.3).
- **Restart that's up-but-cold.** Declaring recovery done when the process is *alive* rather than *warm-and-correct* (Ch. 53, §89.3). The first ticks after a cold restart are slow — measure warm-and-live, not process-alive.
- **Heartbeat that only catches exits.** Relying on `SIGCHLD` alone misses *hangs* (a live-but-stuck process). Use heartbeats to catch the hang (§89.4.3).
- **Raw pointers in shared memory.** Segments map at different addresses per process; raw pointers stored in shared memory are garbage in another process (Ch. 29, 88). Use offsets (§89.4.1).
- **Core dump stalls the survivors.** A synchronous, huge, on-the-hot-disk core dump that freezes the box — and a kill-switch that waits for `SIGCHLD`, i.e. for the dump to finish. Make dumping small, bounded and off-path, and trigger the kill-switch from the fault handler (Ch. 93 §93.4.3–§93.4.4).

## 89.6 Exercises & checklist

**Exercises:**

1. **Contain a crash.** Build a monolith (feed + strategy + gateway in one process) and a process-per-role version with shared-memory data planes (§89.4.1). Inject a segfault into the strategy in both. Show the monolith loses the market view and the venue session; the split keeps both up and the gateway cancels the dead strategy's orders (§89.4.4).
2. **Prove determinism.** Build a state-machine core (§89.2.2), capture an input stream (Ch. 94), and replay it twice — assert bit-identical outputs. Then sneak a `clock_gettime` into the core and watch replay diverge; fix it by making time an input.
3. **Measure recovery, including warm.** Kill a process and measure detect → restart → recover → warm → live (§89.3), with HdrHistogram on the tail. Then *skip* the warm step (Ch. 53) and measure the first-tick latency penalty — quantify why "up" isn't "recovered."
4. **Single-writer enforcement.** Build a shared-memory ring with one writer / many readers (§89.4.2); add reader-side validation (Ch. 87) and show it catches a deliberately corrupting writer without propagating bad data into a decision.
5. **Heartbeat hang detection.** Make a process *hang* (busy-loop, no exit) and show `SIGCHLD` misses it but a heartbeat timeout (§89.4.3) catches it and triggers restart. Tune the timeout for detect-vs-false-positive.
6. **Shard isolation.** Shard strategies by symbol across processes (§89.4.5); crash one shard's strategy on a poison symbol and confirm the other shards keep trading. Measure that the blast radius is one shard.

**Checklist:**

- [ ] The system is **process-per-role** (feed / strategy / risk / gateway), each its own **fault domain**; the **blast radius** of every single failure is bounded and stated (§89.2.1, §89.2.3).
- [ ] The decision core is a **pure, deterministic state machine** — no I/O, no clock, no randomness inside; **time and randomness are inputs**; replay produces **bit-identical** outputs (§89.2.2).
- [ ] Processes communicate via **shared-memory lock-free rings** (Ch. 29, 57) using **offsets not pointers**, pre-faulted/`mlock`'d — cheap IPC, no per-message syscall (§89.4.1).
- [ ] **Single writer per segment** (Ch. 38); readers **validate** shared-memory contents (Ch. 87); corruption is contained to the writer's segment (§89.4.2).
- [ ] A **supervisor** watches liveness via **heartbeats** (catches hangs, not just exits) and restarts under **backoff/cap/escalation** policy (§89.4.3).
- [ ] A **kill-switch / safe-state** lives in a **surviving process** (the gateway) and can cancel a dead component's working orders; the session stays up (§89.4.4).
- [ ] Processes are **crash-only** (§89.2.4) with durable state in shm/journal; restart is **recovery + warm-up (Ch. 53)** measured to **warm-and-correct**, not process-alive (§89.2.4, §89.3, §89.4.4).
- [ ] **Core dumps** are small/bounded/off-path and **don't stall survivors** (Ch. 93); strategies are **sharded** by symbol/venue so one shard's crash contains to that shard (§89.4.5).
- [ ] **No shared-fate back doors** (shared locks/disk/core/segment) re-couple the fault domains — audited and verified (§89.5).

## 89.7 References

- The **crash-only software** paper (Candea & Fox) — crash-only design, recovery-as-the-only-path (§89.2.4).
- The **Erlang/OTP** supervision-tree and "let it crash" literature — process-per-role, supervisors, fault containment as architecture (§89.2.1, §89.4.3).
- The LMAX architecture write-ups (Ch. 40 references) — the deterministic business-logic core decoupled from I/O, fed from an event stream, for replay (§89.2.2).
- Ch. 29 (shared memory), Ch. 57 (cross-process IPC rings), Ch. 38 (single-writer publication), Ch. 53 (warming), Ch. 94 (capture/replay — the input stream the core replays).
- Regulatory guidance on pre-trade risk controls and kill-switches (e.g. SEC Rule 15c3-5 "market access") — why the risk gate and kill-switch are mandatory, not optional (§89.4.4).

## 89.8 Additional Reading

- Talks on trading-system architecture, fault domains, and deterministic replay (LMAX, exchange/matching-engine designs) — the process topology and state-machine pattern in production (§89.2).
- The functional-core / imperative-shell and event-sourcing literature — the pure-core/impure-edge split and replay from an event log (§89.2.2).
- Ch. 94 (*Capture/Persistence/Replay*) — recording the input stream and replaying it through the deterministic core; Ch. 101 (*Case Study*) — the whole topology end-to-end with replay-based regression gates; Ch. 53 (*Warming*) — fast warm restart; Ch. 87 (*Security*) — kill-switch and reader-side validation; Ch. 88 (*Hot Reload*) — config distribution to the processes.
- **Appendix F** — crash-only / state-machine / kill-switch glossary; **Appendix C** — isolation and core-affinity tuning that keeps the processes off each other's cores.

---

*Next: Ch. 90 — The Pre-Trade Risk Gate & Kill-Switch Path: the Risk role of §89.2.1 built — SEC 15c3-5 / MiFID II RTS 6 checks over flat per-symbol and per-account limit records in ~15 ns, checked notional arithmetic, TSC token buckets, limits published lock-free, kill-switch triggers as runtime monitors, and the measured time from trigger to every working order cancelled (a 4 MiB table walk vs a live-order bitmap: 300 µs vs 4 µs).*
