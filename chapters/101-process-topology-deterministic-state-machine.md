# Part XIII — Observability & Operations in Production

# Chapter 101 — Process Topology & The Deterministic State Machine

> **Prerequisites:** Ch. 34 (mmap/shared memory — the inter-process data plane), Ch. 67 (IPC / cross-process ring buffers — how the processes talk), Ch. 46 (seqlock/single-writer publication — the per-segment discipline), Ch. 62 (warming — fast restart isn't fast if it's cold), Ch. 100 (hot reload — config distribution to the processes), Ch. 99 (security — privilege isolation, kill-switch/safe-state).
>
> **Leads into:** Ch. 106 (capture/replay — the deterministic state machine is what you replay *into*), Ch. 116 (case study — the whole topology end-to-end). A Part XIII architecture chapter: structuring the system as cooperating processes so one failure can't take down the session, and isolating a pure, replayable core.

---

## 101.1 Why it matters: contain a crash, keep the venue session up

Everything before this chapter optimized a *process*. This one is about what happens when that process **crashes** — because it will. A null deref in a new strategy, an unhandled malformed packet that slipped past Ch. 99's gate, an assertion, a bad config, an OOM, a hardware glitch — software fails, and a trading system runs real money through it during live sessions. The architectural question is not *whether* a component fails but **what its failure takes down with it.** In a **monolith** — feed handler, strategy, risk, and order gateway all in one process — the answer is *everything*: one strategy's segfault kills the feed handler (you go blind to the market), the order gateway (you drop the venue session and orphan working orders), and risk (you lose the safety check) — all at once, mid-session, with open positions. A single bug becomes a total outage.

The alternative is **process-per-role**: split the system into separate OS processes — feed handler, strategy (often one per strategy/symbol-group), risk, order gateway — each in its own address space, communicating through shared-memory data planes (Ch. 34, 67). Now a crash is **contained**: if a strategy process dies, the feed handler keeps decoding the market, the gateway keeps the venue session alive and can cancel that strategy's working orders (kill-switch, §101.4.4), risk keeps enforcing limits, and a supervisor restarts just the dead process (warm — Ch. 62) while everything else keeps running. The **blast radius** of a failure shrinks from "the whole system" to "one role, one shard." For a system where an outage during market hours is a financial and regulatory event, fault containment is not optional polish — it's the difference between "we restarted one strategy in 200 ms" and "we were down and blind with open positions."

There's a second, equally important reason rooted in the rest of Part XIII: **determinism and replayability.** If the core decision logic is a **pure, side-effect-free state machine** — same inputs always produce the same outputs, no I/O or clocks or randomness inside it — then you can **replay** a captured session (Ch. 106) through it and get *exactly* the same decisions, bit-for-bit. That's the foundation of debugging ("replay the crash"), backtesting ("replay history through the new strategy"), compliance ("prove what we did and why"), and regression testing (Ch. 116). Process topology and the deterministic state machine are two halves of the same idea: **separate the fragile, I/O-bound, non-deterministic edges (network, timers, OS) from a contained, pure, replayable core** — so failures are isolated *and* the system is reproducible. This chapter motivates the topology and the pattern (§101.2), measures restart/failover time and its tail (§101.3), and builds the shared-memory data planes, single-writer discipline, supervision, kill-switch, fast restart, and sharding that make it work (§101.4).

## 101.2 Mental model

### 101.2.1 Process-per-role vs monolith (feed handler / strategy / order gateway / risk)

Decompose the tick-to-trade path into separate processes by **role**, each a fault domain:

```
   wire ─► [ FEED HANDLER ]─shm─►[ STRATEGY(s) ]─shm─►[ RISK ]─shm─►[ ORDER GATEWAY ]─► wire
            decode (Ch.70)        pure decision        pre-trade      venue session,
            normalize, seq        (state machine)      limits/kill    order I/O
                 │                      │                  │                │
            (one process)          (one per strategy/   (one process)   (one process,
                                    symbol shard)                        holds the session)
```

- **Feed handler** — owns the market-data side (Ch. 70–74): decode, normalize, sequence/gap-handle (A/B arbitration and recovery — Ch. 70 §70.4.3, Ch. 72), publish a clean normalized stream into shared memory. If a strategy dies, the feed handler is untouched — the market view stays live.
- **Strategy** — the decision logic; often **one process per strategy or per symbol/venue shard** (§101.4.5) so strategies can't crash each other. Ideally a *pure state machine* (§101.2.2).
- **Risk** — the pre-trade gate (limits, position, kill-switch — built in Ch. 102): a separate process (or a mandatory in-line check) that no strategy can bypass or crash. The last line before an order leaves.
- **Order gateway** — owns the *venue session* (Ch. 68): the TCP/order-entry connection, sequence numbers, working-order state. Keeping this separate means a strategy crash doesn't drop the session or orphan orders — the gateway can cancel-on-disconnect / kill-switch the dead strategy's orders and keep the session up. Order identity, the in-flight order table and post-crash order-state recovery are Ch. 103.

The trade-off is **latency vs containment**: crossing a process boundary costs an IPC hop (a shared-memory ring write/read — Ch. 67; ~50–150 ns one-way *latency* per hop, a cross-core cache-line transfer — Ch. 44, Appendix E's HITM row; Ch. 67 §67.3's ~7–15 ns/msg is amortized throughput cost, not hop latency — *not* a syscall if done right). A monolith saves those hops but couples all failures. The HFT resolution: use **shared-memory data planes** (§101.4.1) so the hops are cheap (no kernel, no copy — Ch. 34/67), and accept a small, bounded latency cost for a large containment win. The hottest, most-coupled stages can be co-located if a benchmark justifies it; the *fault-critical* split (gateway and risk separate from strategy) is rarely worth giving up.

### 101.2.2 The Deterministic State Machine Pattern: pure, side-effect-free core decoupled from I/O for exact replayability

> The arithmetic *inside* this core — per-instrument state, the signals maintained in O(1), floating-point drift and the build flags that keep a replay bit-exact — is Ch. 40; this section is the pattern around it.

The most important pattern in the chapter: **isolate the core decision logic into a pure, deterministic state machine, decoupled from all I/O, timers, and randomness.**

```
   IMPURE EDGE (non-deterministic):              PURE CORE (deterministic):
   ──────────────────────────────               ──────────────────────────
   network RX (Ch.63-81)                         apply(state, input) → (state', outputs)
   timers / clocks (Ch.20)            ─inputs─►   • NO syscalls, NO I/O, NO clock reads
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
using Input = std::variant<MdEvent, Fill, TimerFire, ConfigSwap>;   // one sequenced stream, assigned by the shell (Ch. 106)

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

// Replay harness (Ch. 106 → CI gate, §116.4.1): same inputs ⇒ bit-identical outputs, or the build fails.
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
- **Inputs are an ordered, captured stream.** Every input — each market-data message, each timer fire, each fill, each config change (Ch. 100) — is a record in a single ordered log (Ch. 106) — produced, when more than one consumer must agree on the order, by the sequencer of Ch. 108. The core consumes that stream.
- **The shell is the sequencer.** The impure shell around the core assigns a single global **sequence number** to *every* input — market data, fills, timer fires, config swaps — before the core sees it; that one sequenced stream, not the raw sources, is what Ch. 106 captures and what replay feeds back. Without it, two sources' relative order is a race the replay cannot reproduce.
- **Determinism ⇒ exact replay.** Because outputs depend *only* on (initial state + input sequence), feeding the *same captured input log* through the core reproduces the *same outputs exactly* — the same orders, the same decisions, bit-for-bit. This is the property everything in Ch. 106–116 relies on: replay the captured session to debug a production crash, to backtest a strategy change against real history, to prove compliance, to gate regressions in CI.

This is the same edge/core split as functional-core/imperative-shell, applied to trading for *both* fault isolation (the pure core has fewer ways to fail and no I/O to hang on) and reproducibility. The I/O, timers, and OS interaction — the non-deterministic, failure-prone parts — live in a thin shell around the core; the core itself is pure, testable, and replayable.

### 101.2.3 Fault domains and blast radius

A **fault domain** is a boundary a failure can't cross; the **blast radius** is what one failure actually takes down. Process boundaries (separate address spaces) are the strongest in-box fault domain: a segfault, heap corruption, or assertion in one process cannot corrupt another's memory or take down its thread — the OS contains it. Design so that:

- **Each role is its own fault domain** (§101.2.1) — a strategy crash can't corrupt the feed handler or gateway.
- **Each shard is its own fault domain** (§101.4.5) — one symbol/venue's strategy crash doesn't touch another's.
- **The blast radius of any single failure is bounded and known** — you can state, for each component, exactly what its death takes down and what survives. The gateway and risk survive *every* strategy failure (so orders can be cancelled and the session kept up); a feed handler failure is contained to its feed (the redundant A/B line and the recovery channels, Ch. 70–72, and other feeds survive).
- **Shared memory is a controlled hole in the isolation** (§101.4.1-2): processes share data-plane segments, so a writer corrupting its segment *can* feed bad data to readers — which is why the single-writer-per-segment discipline (§101.4.2) and reader-side validation (Ch. 99) matter. Isolation is the default; sharing is deliberate and disciplined.

### 101.2.4 Crash-only design

Design every process to be **crash-only**: the only way it stops is by crashing (or being killed), and the only way it starts is by recovering from whatever state survived. There is *no* separate "graceful shutdown" path to get wrong — stopping *is* crashing, and starting *is* crash recovery. The benefits: (1) you exercise the recovery path *every* restart, so it's always tested and fast (a rarely-run graceful-shutdown path rots and fails when you need it); (2) recovery is robust to *actual* crashes (power loss, OOM-kill, segfault) because that's the only path; (3) it pairs with fast warm restart (§101.4.4, Ch. 62) — a crash-only process keeps its durable state (in shared memory / journal, Ch. 106) so a restart reloads it warm rather than rebuilding from scratch. Crash-only means: persist what you need to recover (durably, off-process — shared memory, journal), make startup *be* recovery, and make it fast. "Kill -9 and restart" is the normal operation, not the emergency.

## 101.3 Measure it: failover/restart time and its tail

If fault containment is the goal, the metric is **how fast you recover** — and, in this book's spirit (Ch. 1), the *tail* of recovery time, because a slow restart at the wrong moment (market open, a volatility spike) is when it hurts most. Measure **detect → restart → warm → live**: from the instant a process dies to the instant its replacement is *making correct decisions at full speed*.

| Stage | What it costs | What dominates |
|---|---|---|
| **Detect** (supervisor notices death) | heartbeat timeout / `SIGCHLD` (§101.4.3) | the heartbeat interval — too long = slow detect; too short = false positives |
| **Restart** (spawn / re-exec the process) | process spawn + map shared memory (Ch. 34) | spawn cost; mapping pre-existing shm is fast |
| **Recover state** (crash-only, §101.2.4) | reload durable state from shm/journal (Ch. 106): VM-measured, a 512 MiB snapshot loads from page cache in ~110 ms and the journal replays at ~3.3 ns/event (§101.4.7, Ch. 108 §108.3) | journal replay length — bounded by snapshot frequency; the snapshot's cost on the hot thread is the other side of the trade (§101.4.7: a fork/COW snapshot puts 1.3–3.7 µs on the p99–p99.9 of every write while the child lives; a double buffer with a dirty-line bitmap costs +10 ns per write) |
| **Warm** (Ch. 62) | pre-touch pages, prime caches/TLB/predictors, shadow traffic | the warm-up you skip = a cold-start tail on the first real tick |
| **Live** | back to full-speed correct decisions | — |

The harness is the §101.4.3 supervisor itself: it `fork`/`exec`s a role that publishes a heartbeat sequence into a shared-memory slot; loop N times — `kill -9` the role (and, separately, `SIGSTOP` it to simulate a hang), timestamp (`CLOCK_MONOTONIC`) the detect (SIGCHLD vs heartbeat timeout), the re-exec, the shm attach, the snapshot + journal replay (Ch. 106) and the warm-up (Ch. 62), and record each as a histogram (Ch. 3) alongside the *survivors'* hot-path latency during the episode. Representative shape (toolchain and machine: Appendix L.3; figures pending real runs): a crash-only process whose state lives in shared memory and that warms before going live can recover in **~hundreds of ms to low seconds**; the *dangerous* number is the **tail** — a restart that has to rebuild state from a long journal, or that skips warming and takes cold-start stalls on its first ticks (Ch. 62), can be far slower exactly when conditions are worst.

The lessons:

- **Recovery time is detect + restart + recover + warm — measure all four, and the tail of each.** Teams measure "restart time" and forget **warm** (Ch. 62): a process that's "up" but cold is still effectively impaired for its first ticks. The case study (Ch. 116) measures *warm-and-correct*, not *process-alive*.
- **Bound recovery state with snapshots.** If recovery replays a journal (Ch. 106), the journal length bounds recovery time — take periodic state snapshots so replay starts from a recent checkpoint, not session-start (§101.4.7 for how to take one without stalling the hot thread, Ch. 106 §106.4.4 for where it goes).
- **Tune the heartbeat for detect-vs-false-positive.** Too slow and you're impaired longer before noticing; too fast and a transient stall (a page fault, an SMI, a scheduler preemption on a non-isolated core) looks like death and you restart needlessly (§101.4.3). Measure the false-positive rate against the detect latency.
- **The whole point is the *contained* failure has a *bounded, fast, warm* recovery** — the monolith's "recovery" is "restart everything and lose the session," which has no good tail.

## 101.4 Techniques

### 101.4.1 Shared-memory data planes between isolated processes

The processes are isolated but must exchange data on the hot path *without* a syscall per message — so they communicate through **shared-memory ring buffers** (Ch. 34, 67), not pipes/sockets:

- **`mmap`'d shared segments (Ch. 34)** carry the data plane: the feed handler writes the normalized market stream into a shared ring (Ch. 45/67); strategies read it; strategies write orders into a ring the risk/gateway read. The segment is mapped once at startup, pre-faulted and `mlock`'d (Ch. 34) so steady state never faults.
- **Lock-free cross-process rings (Ch. 67).** SPSC/MPSC rings (Ch. 45) laid out in the shared segment — same lock-free producer/consumer discipline as in-process, now across address spaces. A boundary crossing is a ring write + a ring read (tens to low-hundreds of ns — Ch. 67), *no kernel*, the cheap IPC that makes process-per-role affordable (§101.2.1).
- **Offsets, not pointers (ties Ch. 100).** A segment maps at different virtual addresses in different processes; data in it uses *offsets/indices within the segment*, never raw pointers (a raw pointer from process A is meaningless in process B). Same discipline as cross-process config (§100.4.3).
- **Dead readers and segment generations.** A reader process that dies mid-ring leaves a stale consumer cursor: a ring with back-pressure stalls on it (never on the hot path — Ch. 96's rule) and a ring without it overruns it; the supervisor (§101.4.3) must *reap* a dead reader's cursor (mark it detached, or let producers ignore cursors whose heartbeat has lapsed). A *writer* restart needs a **segment generation**: the restarted writer stamps a new generation into the segment header and re-initialises its rings, and readers that see the generation change resynchronise rather than consuming half-written state from the previous life.
- **Control plane separate from data plane.** Slow, rare control (config — Ch. 100, commands, kill-switch) can go over a separate channel; the hot data plane stays a clean, fixed-layout shared ring.

### 101.4.2 Single-writer-per-segment discipline

The rule that keeps shared memory safe and lock-free: **exactly one process writes each segment/ring; all others only read.** This is Ch. 46's single-writer-multiple-reader publication applied across processes:

- **One writer per ring.** The feed handler is the sole writer of the market-data ring; each strategy is the sole writer of its own order ring; the gateway is the sole writer of the fill ring. Single-writer means publication is a release store (Ch. 41/46) with no inter-writer coordination — no cross-process lock (which would be slow and a shared-fate hazard).
- **Readers validate (Ch. 99).** Shared memory is the one place the fault isolation leaks (§101.2.3): a buggy writer can put garbage in its segment. Readers treat segment contents with the same untrusted-input discipline as the wire (Ch. 99) where it matters — bounds/sanity checks on indices and lengths — so a writer's corruption is caught, not propagated into a wrong trade.
- **Single-writer also bounds the blast radius of corruption** — only the segment's one writer can corrupt it; a reader's crash can't corrupt the data for others. It maps cleanly onto the fault-domain model (§101.2.3): the writer owns its segment's integrity.

### 101.4.3 Supervisor/watchdog, heartbeats and liveness

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
    constexpr std::uint64_t kTimeoutNs = 50'000'000;               // tuned per §101.3: detect-latency vs false-positive
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
        // kill-switch first (§101.4.4): tell the gateway to cancel this shard's working orders — then restart
        if (now_ns() - minute_start > 60'000'000'000ull) { restarts_this_minute = 0; minute_start = now_ns(); }
        if (++restarts_this_minute > 5) { std::fprintf(stderr, "crash-loop: safe-state and alert\n"); return 2; }
        struct timespec b{0, long(backoff_ns)}; nanosleep(&b, nullptr); backoff_ns = backoff_ns * 2 > 1'000'000'000 ? 1'000'000'000 : backoff_ns * 2;
        child = spawn(argv + 1); last_seen = now_ns();
        while (hb->seq.load(std::memory_order_acquire) == last_seq) usleep(100);     // first heartbeat = "warm and live" (Ch. 62)
        std::printf("recovered in %.1f ms (detect %s)\n", (now_ns() - t_detect) / 1e6, dead ? "SIGCHLD" : "heartbeat timeout");
    }
}
```


- **Liveness via heartbeats.** Each process periodically writes a heartbeat (a sequence/timestamp in a shared-memory slot — cheap, off the hot path); the supervisor reads them. The natural home for the slot is the telemetry segment of Ch. 98, next to a *progress* counter (last sequence processed) — a loop that is spinning but stuck still heartbeats, and only the progress counter catches it. A missed heartbeat (beyond a tuned timeout, §101.3) means the process is dead *or hung* (a hang is as bad as a crash and a plain `SIGCHLD` won't catch it — the heartbeat does). Combine `SIGCHLD`/`waitpid` (catches exits) with heartbeats (catches hangs).
- **Restart policy.** On death, the supervisor restarts *that* process (warm — §101.4.4) under a policy: backoff to avoid a crash-loop hammering, a cap on restarts-per-interval, and an escalation (if a process won't stay up, kill-switch its orders and alert rather than loop forever). The supervisor itself must be simple and rock-solid (it's a fault domain whose failure is serious) — and often supervised in turn (systemd, a watchdog, or a peer).
- **The watchdog as a safety device.** A hardware or kernel watchdog (and an application watchdog) that triggers the kill-switch/safe-state (§101.4.4) if the *system* goes unresponsive — the last-resort liveness guarantee.

### 101.4.4 Kill-switch / safe-state on failure; fast restart with warm-up

When something fails, the system must reach a **safe state** fast, then recover:

- **The readiness state machine (Appendix M).** Every role publishes one of `cold → warming → syncing → reconciled → armed → trading → disarmed` in the telemetry segment's header (Ch. 98 §98.4.1); the order path reads it with a single load and a predicted branch (`may_send()` — Appendix M.1) and refuses to send in any state but `trading`. Transitions forward are earned by the pre-open gates (host attestation, clock, feed, session, positions, warm-up — Appendix M.2–M.4); *any* failing invariant (a dark feed, a dead session, a host-health delta — Ch. 111/112) or the kill-switch below drops the role to `disarmed`, and the only way back is to re-run the gates from the failed one (a time-boxed re-arm after an intraday restart — Appendix M.5). The supervisor writes `disarmed` into a dead role's slot before restarting it, so nothing ever reads a crashed process as `trading`. The control plane that drives the transitions is the same off-hot-path channel as the kill-switch command: never a signal on the hot thread.
- **Kill-switch / safe-state.** On a detected failure, drive to a *safe state*: stop new orders, cancel the affected working orders, flatten or hold per policy. The **gateway** (separate process, §101.2.1) is what executes this — it owns the session and cancels the dead component's orders *even though that component is gone*; the venue's cancel-on-disconnect only fires if the *gateway's* session drops, which is the opposite of what you want here. This is *the* payoff of separating the gateway: a strategy crash doesn't strand its orders, because the surviving gateway kills them. The triggers (reject-rate, position drift vs drop-copy, drawdown, feed staleness, heartbeat loss, operator), the trigger-to-safe-state sequence and its measured time (a live-order bitmap vs a table walk: 4 µs vs 300 µs for 400 orders) are **Ch. 102 §102.2.4 and §102.4.5–§102.4.6**; the working-order set it enumerates is Ch. 103's in-flight table.
- **When the gateway itself dies.** The gateway is the one role whose failure *is* a session failure: the venue's **cancel-on-disconnect** (enable it — Appendix K) is the safety net that flattens working orders, and recovery is a **hot-standby gateway** that re-logs on and performs sequence-number recovery (replaying the venue's unacknowledged messages — the session protocol's resend request) before the strategies are allowed to resume. Measure that path (§101.3) like any other restart; it is the longest one.
- **Fast restart with warm-up (Ch. 62).** Restart is crash-only recovery (§101.2.4): map the surviving shared state (Ch. 34), replay from the last snapshot to current (Ch. 106), **warm** the process (pre-touch pages, prime caches/TLB/predictors, run shadow traffic — Ch. 62) so its *first real tick is at full speed*, then go live. Skipping the warm step (§101.3) means the freshly restarted process limps through its first ticks — a tail event right after a failure, when the market may be moving.
- **Determinism makes restart correct.** Because the core is a deterministic state machine (§101.2.2) fed from the captured input stream (Ch. 106), a restarted process replays the inputs and arrives at *exactly* the correct current state — no guesswork, no divergence from the process that died. Crash-only + deterministic-replay = provably-correct fast recovery.

### 101.4.5 Symbol/venue sharding (core dumps → Ch. 105)

Two operational techniques for keeping failures small and debuggable — one developed here, one handed to the next chapter:

- **Core dumps that do not stall survivors — Ch. 105.** A crashed role must yield *diagnostics* without taking the survivors' latency with it: the kill-switch handoff from inside the fault handler (so the gateway cancels the dead shard's orders ~20 µs after the fault, not after a 20 s dump), `madvise(MADV_DONTDUMP)` on the hugepage-backed book and data-plane segments every role maps, `coredump_filter`, `RLIMIT_CORE` (not enforced for piped dumps), a bounded `core_pattern` pipe handler on a housekeeping core writing to a separate disk, systemd-coredump's limits, and minidumps. Ch. 105 builds the async-signal-safe handler and measures the dump's cost to the survivors (a 2–4 GiB dump stretched a survivor's worst gap 20–50×; excluding the book shrank it to a sub-millisecond event).
- **Symbol/venue sharding.** Partition strategies by symbol or venue across processes (and cores, Ch. 56) so that (1) one shard's crash contains to that shard's symbols — the rest keep trading (§101.2.3), and (2) load spreads across cores/NUMA nodes (Ch. 18). Sharding is fault-containment *and* scaling: a bug triggered by one symbol's data takes down only that symbol's strategy, and the blast radius is one shard, not the book. Choose the shard key (symbol, venue, asset class) so correlated work and correlated failures stay within a shard.

### 101.4.6 Divergence forensics and determinism auditing

§101.2.2 promised bit-identical replay and §101.5 lists the ways a core stops being deterministic; Ch. 116 §116.4.1 makes "replay matches golden" a CI gate. What none of them gives is a way to *localise* a divergence: a replay of a four-hour session that produces a different order at 11:52:07 has, by then, processed 140 million events, and "the outputs differ" is unactionable. The fix is a **per-event state digest** journaled beside the outputs, a diff of digest streams to find the first diverging event, and a lint of the core for the known causes — measured here on the usual VM (GCC 13.3, `-O2 -march=native`, Linux 6.18; a 4096-level book, ten million level updates):

```
   book update variant                                                    cycles/event      digest size per event
   ───────────────────────────────────────────────────────────────────── ──────────────── ─────────────────────────
   no digest                                                                 6.8              —
   incremental: crc32c of (level index, new qty, new count) chained          6.9  (+0.1)      8 B
   full-state hash (4096 levels) every event                               17,105             8 B
   full-state hash every 4096 events (amortised)                              8.5  (+1.7)      8 B / 4096 events
```

- **Hash what changed, chained.** `digest = crc32c(crc32c(crc32c(digest, idx), qty), n)` after each mutation — three `crc32q` instructions (Ch. 37; 3-cycle latency each, pipelined with the update's own stores) — costs 0.1 cycles per event over the bare update and yields a running digest that depends on *every* mutation in order. It is not a hash of the state (two different states could collide; a mutation that is applied and reverted leaves a trace) and that is exactly what forensics wants: a digest of the *trajectory*, which diverges at the first event whose effect differed. A periodic full-state hash (every 4096 events, +1.7 cycles amortised) is the check that the incremental digest and the state agree — it catches a mutation that bypassed the digesting path (a `memcpy` into the book, a reset).
- **Journal it as a column.** The Ch. 106 §106.4.2 journal record gains an 8-byte `digest` field next to the output it describes; the replay harness writes the same column. The incremental digest is why this is affordable — 8 bytes per event on a path that already writes ~64 — and the gap-free sequencing of Ch. 106 §106.4.4 is what makes the two streams comparable by position.
- **Find the first diverging event.** `cmp` the two digest columns (or the `paste | awk '$1 != $2 {print NR; exit}'` equivalent): in the measurement, a planted nondeterminism — a `clock_gettime` whose nanosecond parity nudged one quantity at event 6,000,001 — was located at exactly #6000001 by a single linear pass over ten million digests. The alternative when the streams are too large to hold is **checkpoint bisection**: snapshot the core's state every *N* events (Ch. 106 §106.4.5's snapshots), replay from the checkpoint before the midpoint, compare the digest at the midpoint, and recurse — 24 replays of a shrinking window for ten million events. Either way the result is an *event number*, and the replay harness's `--stop-at-divergence` mode (replay until the journaled digest and the computed one differ, then `raise(SIGTRAP)`) drops you into gdb or `rr` (Ch. 5 §5.4.2) at the first input the two executions disagreed on, with the state that disagreed in memory.
- **The determinism lint.** The known causes, as things to grep for and to test:

  ```
     cause                                       how it leaks                                             test / fix
     ─────────────────────────────────────────── ──────────────────────────────────────────────────────── ─────────────────────────────────────────────
     clocks: clock_gettime, rdtsc, steady_clock  a timestamp becomes part of a decision or an output      grep the core for them; time is an INPUT event
     randomness: rand, random_device, a seed     path choice differs                                      seed is an input; PRNG state is core state
     pointer-keyed hashing under ASLR            unordered_map<T*> / hash<void*> iteration order and       replay with ASLR on twice: digests differ →
                                                 bucket placement differ run to run (Ch. 32)              key by id/index, or iterate a sorted view
     unordered iteration                         any order-dependent fold over an unordered container     same test; use flat_map/sorted vectors (Ch. 32)
     uninitialised padding in journaled structs  Ch. 11's padding bytes carry stack garbage into the       static_assert(has_unique_object_representations),
                                                 journal and the digest                                   or memset before fill; digest fields, not bytes
     FP reassociation / -ffast-math / FMA        bit-different sums across builds or -march (Ch. 35 §35.4.3) strict FP in the core; fixed-point for prices
     thread scheduling / shared state            a second thread's writes interleave differently          the core is single-threaded; inputs via one ring
     environment: locale, env vars, hostname     to_chars/format differences; config read from the host   configuration is a versioned INPUT (Ch. 100 §100.4.3)
     hidden statics: counters, errno, singletons `apply` depends on more than (state, input) (§101.5)       state is explicit; a reset()-and-replay test
  ```

  The two tests that catch most of it: replay the golden capture **twice with ASLR enabled** (`setarch -R` is the thing you must *not* use here) and require identical digest streams — pointer-keyed hashing and uninitialised padding fail this immediately; and build the core with GCC and Clang at `-march=x86-64-v3` and `-march=native` and require the same — FP reassociation fails this. Both run in the Ch. 116 §116.4.1 gate, which now fails with an event number instead of a diff.

### 101.4.7 Checkpointing the core: consistent snapshots without stalling the hot path

§101.3 bounded recovery by "snapshot frequency" and Ch. 106 §106.4.4 writes the snapshot to storage; neither said how a consistent image of a few hundred MiB of hot state is *taken* while the hot thread keeps applying ticks. A snapshot is a pair **(state, last-applied seq)** captured at a sequence boundary — nothing in it reflects event `seq+1` — so the problem is purely one of copying without pausing. Three mechanisms, measured on the 4-vCPU VM of Ch. 49 §49.3 (GCC 13.3 `-O2`, Linux 6.18; `ckpt.cpp`: a 512 MiB / 2 GiB resident state, a hot loop writing one random cache line every 200 ns with `rdtsc` around each write):

```
   state 512 MiB, THP off                  write p50 / p99 / p99.9 / max              what happened
   ───────────────────────────────────     ─────────────────────────────────────────   ─────────────────────────────────────────────────────────────────────
   baseline, no snapshot                   241 / 464 / 624 ns / 1.5 ms (VM steal)
   fork() — the call itself                parent blocked 8.3 ms                       page-table copy of 512 MiB (28 ms for 2 GiB); the child owns a consistent image
   fork() — the 2 M writes after it        241 / 1 324 / 3 670 ns / 199 µs             17 345 writes > 2 µs: each first write to a page after fork is a COW fault (4 KiB copy + TLB flush)
   …the same with THP (madvise)            250 / 2 050 / 2 887 ns / 247 µs             21 588 writes > 2 µs: a COW fault now copies a 2 MiB page — THP makes fork snapshots WORSE
   …2 GiB state, THP off                   424 / 2 335 / 15 301 ns / 7.1 ms            the fault storm scales with the working set; the max is the VM descheduling a faulting thread
   memcpy to a second buffer, inline       stall 44 ms (12.1 GB/s); 177 ms for 2 GiB   simple and wrong on the hot thread; right from a housekeeping core over a stable double buffer
   hot loop + dirty-line bitmap            252 / 546 / 701 ns                           +10 ns per write; 985 720 of 8.4 M lines dirty after 2 M writes → an incremental snapshot copies 60 MiB, not 512
   restore: load 512 MiB from /dev/shm     113 ms (4.8 GB/s); 503 ms for 2 GiB         + replay of the journal since the snapshot at ~3.3 ns/event (Ch. 108 §108.3): 60 s of 100 k ev/s = 20 ms
```

- **`fork()` is the easy snapshot and the hot thread pays for it twice**: once in the call (the page-table copy blocks the *caller* for ~16 ms/GiB), then in a COW fault on the first write to every page it touches while the child lives — a tail of 1.3–3.7 µs at p99–p99.9 against a 0.5 µs baseline, for as long as the child takes to write its image. Redis-style snapshots work because Redis tolerates that tail; a tick-to-trade core does not. If `fork()` is used at all, call it from a **housekeeping thread** that owns no hot state (the page tables are per process, so the stall still lands on every thread's next fault, but the call's 8 ms does not block the hot loop), keep THP **off** for the state (a 2 MiB COW copy is 500× a 4 KiB one), and schedule it in a quiet window (Appendix M's pre-open, not 09:30:01).
- **Double-buffered state is the steady-state answer.** The core keeps its state behind a seqlock-style version word (Ch. 46 §46.4.2): a housekeeping core copies the live state into the shadow buffer while re-checking the version, retrying the lines that changed under it; the hot thread's only cost is the version increment it already pays. With the **dirty-line bitmap** (one `or` into a bitmap per write — measured at +10 ns, two cache lines touched) the copy is incremental: 60 MiB instead of 512 MiB for two million writes, and the copier walks the bitmap with `tzcnt` (Ch. 37) and clears it under the version. The snapshot's `seq` is read with the version, so it is a boundary by construction.
- **Deterministic cores snapshot for free from a replica** (Ch. 108 §108.2.3): a hot replica that applies the same stream can pause *itself* at any `seq`, write its state, and catch up at 3 ns/event — the primary never notices. This is the preferred design wherever a replica exists; §101.4.7's mechanisms are for the single-copy core.
- **The RTO curve**: `RTO = detect (Ch. 109) + load(snapshot size) + replay(rate × interval since snapshot) + warm-up (Ch. 62)`. From the table: 512 MiB loads in ~110 ms, a 60 s interval at 100 k events/s replays in 20 ms, so snapshotting every minute buys an RTO dominated by load and warm-up, and a 10× larger state costs 10× the load term — which is the argument for keeping the *core's* state small and the research-grade state (Ch. 106 §106.4.7) elsewhere. Add the measured terms as a row to §101.3's table for your system; "bounded by snapshot frequency" is a curve, not a sentence.

### 101.4.8 Segment lifecycle: reattach, epochs and torn-write recovery

Ch. 34 §34.4.1 and Ch. 67 §67.4.2 defer the question of what a process does when it attaches to a shared segment whose writer died mid-store; here is the protocol (`segrecover.cpp`: a writer killed with `SIGKILL` at a random point while writing 64-byte records payload-first/commit-word-last and holding a robust mutex; a reader reattaches):

```
   header (one cache line, written once at creation, checked on every attach)
   ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
   magic              identifies the segment type; mismatch → refuse (it is some other program's segment, or garbage)
   layout_version     bumped on any incompatible change; mismatch → refuse (never "upgrade in place")
   layout_fingerprint hash of sizeof/offsetof of every struct in the segment (Ch. 67): catches a rebuilt binary with a different layout under the same version number
   epoch              incremented by every writer that takes ownership; records carry it, readers discard a dead epoch's tail
   owner_pid + owner_start_ns   liveness of the writer: pid alone is reusable; pid + start time (or a pidfd) is not
   heartbeat_ns       the writer's Ch. 101 §101.4.3 liveness slot, in the segment itself
   seq                the last CLAIMED sequence — may exceed the last COMMITTED one after a crash
   robust_mutex       the rare cross-process lock (config swaps, Ch. 100), PTHREAD_MUTEX_ROBUST so a dead holder's lock is recoverable

   record discipline: write payload … then store commit = seq with release (Ch. 46 §46.4.2's seq-last ordering). A reader accepts a record iff commit == seq.

   the reattach decision tree (measured 86–277 µs on the VM, dominated by the committed-prefix check):
     magic / version / fingerprint wrong?                       → REFUSE to attach; alert. Never patch.
     owner alive (kill(pid,0) && start time matches)?            → attach as a READER only (or refuse, if this process must be the writer: Ch. 109's lease decides)
     owner dead (heartbeat stale AND pid gone)?                  → take ownership: epoch++, owner = me; scan from `seq` backwards to the last record with commit == seq
     torn tail = records claimed but not committed               → discard; every consumer that read past the last committed seq in the dead epoch rolls back to it
     robust mutex: lock() == EOWNERDEAD                          → pthread_mutex_consistent(), re-validate (or rebuild) the state the lock protected, unlock
```

The run that produced those numbers found the writer's last record with **all seven payload words written and the commit word still holding the previous lap's value** — a torn write that a payload-only check would have accepted, and that the commit-word rule rejects in one compare. The committed prefix validated clean (0 bad words in the last 4096 records), ownership moved to epoch 2, and the robust mutex returned `EOWNERDEAD` and was made consistent. Three rules follow. **Never fix up a header**: a wrong magic, version or fingerprint is a deployment error (Ch. 115 §115.4.2's compatibility window exists so a new binary attaches only to layouts it was built for). **Epoch stamps make the tail safe for readers**: a consumer that applied records from the dead writer's final, uncommitted lap sees an epoch it no longer trusts and re-reads from the last committed `seq` — the single-writer discipline (§101.4.2) is what makes "roll back to seq N" meaningful. **Robust mutexes recover the lock, not the data**: after `EOWNERDEAD` the state under the lock is whatever the dead holder left; the recovering process validates it against an invariant or rebuilds it from the journal (Ch. 106) before marking the mutex consistent — and on the hot path there should be no mutex to recover (Ch. 43).

### 101.4.9 The host side of the lifecycle: the trading unit, privileges, boot-to-ready and the readiness gate

The supervisor of §101.4.3 is itself supervised — by `systemd`, which also decides *when* the trading processes may start, *what* they may do, *where* they run and *how fast the box gets there after a reboot*. The unit file is where Appendix C's per-process knobs, Ch. 99 §99.4.6's privilege drop and Appendix M's readiness gate meet, and it is version-controlled with the release (Appendix C.6):

```ini
# /etc/systemd/system/trading@.service — one instance per role/shard (trading@strategy-3.service); the supervisor is a sibling unit.
[Unit]
Description=Trading role %i
After=network-online.target hugepages.service nic-bind.service prefetch-msr.service ptp4l.service phc2sys.service
Requires=hugepages.service nic-bind.service prefetch-msr.service      # fail if the substrate is not there — never start cold
Wants=network-online.target
StartLimitIntervalSec=60                                              # §101.4.3's restart cap, at the unit level
StartLimitBurst=3

[Service]
Type=notify                                                           # READY=1 is sent by the process — after warm-up and the gate, not at exec
NotifyAccess=main
ExecStartPre=/usr/local/sbin/host-attest --release /etc/trading/release.json   # Appendix M.2: refuse to start on a drifted box
ExecStartPre=+/usr/sbin/auditctl -a task,never                        # Ch. 99 §99.4.9: this unit's tasks are not audited (`+` = run as root)
ExecStart=/opt/trading/bin/role --config /etc/trading/%i.toml
TimeoutStartSec=120                                                   # warm-up (Ch. 62) must finish in this; otherwise the start FAILS loudly
WatchdogSec=2                                                         # WATCHDOG=1 from the supervisor loop at ≤ 1 s, or systemd kills and restarts
Restart=on-failure
RestartSec=200ms
# --- placement (Appendix C.4) ---
CPUAffinity=2-7                                                       # the hot cores; the process pins each thread within this set (Ch. 56)
AllowedCPUs=2-7                                                       # the cgroup v2 cpuset, so forks/children cannot escape either
NUMAPolicy=bind
NUMAMask=0
CPUQuota=                                                             # explicitly NONE (Ch. 59 §59.4.4): no cpu.max on this unit
IOSchedulingClass=idle                                                # the only disk I/O is the journal writer's, on a housekeeping thread
# --- limits (Ch. 34, 59, 86) ---
LimitMEMLOCK=infinity                                                 # mlockall + VFIO DMA pins + hugetlb
LimitRTPRIO=99                                                        # SCHED_FIFO from an unprivileged user
LimitNOFILE=65536
LimitCORE=infinity                                                    # Ch. 105: bounded by the piped handler, not by the limit
TasksMax=512
# --- privileges (Ch. 99 §99.4.6) ---
User=trading
Group=trading
AmbientCapabilities=CAP_SYS_NICE CAP_IPC_LOCK CAP_NET_RAW CAP_NET_ADMIN   # RT priority & affinity; mlock; raw/bypass sockets; multicast joins + flow steering
CapabilityBoundingSet=CAP_SYS_NICE CAP_IPC_LOCK CAP_NET_RAW CAP_NET_ADMIN CAP_SYS_PTRACE
NoNewPrivileges=yes                                                   # also required for seccomp filters (Ch. 99 §99.4.6)
ProtectSystem=strict
ReadWritePaths=/var/lib/trading /dev/shm /dev/hugepages /var/log/trading
PrivateTmp=yes
ProtectKernelTunables=yes                                             # the knobs were set by the boot units, not by this one
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_PACKET AF_NETLINK
SystemCallArchitectures=native
# NOT set: MemoryDenyWriteExecute (breaks JIT-ed/`dlopen`ed strategies — Ch. 100), SystemCallFilter (the process installs its own with TSYNC),
# ProtectKernelLogs/ProtectClock (ptp4l needs them — a separate unit), DeviceAllow (VFIO: /dev/vfio/* must be allowed if the role owns a device)

[Install]
WantedBy=trading.target
```

The pieces, and what each buys:

- **`Type=notify` + `READY=1` after the warm-up and the gate.** A `simple` unit is "started" when `exec` returns — long before the segments are mapped, the book is warm (Ch. 62) and Appendix M's checks have passed. With `Type=notify`, `After=`-ordered dependants (the strategy waits for the feed handler; the gateway for the risk gate) start only when the process says so, and `TimeoutStartSec` turns a warm-up that hangs into a failed start with a log line instead of a silent cold process. `sd_notify` is one datagram; the whole protocol fits in twenty lines, no `libsystemd` needed:

```cpp
// sdnotify.cpp — READY=1 only after warm-up and the readiness checks; WATCHDOG=1 from the supervisor loop, no libsystemd.
// Build: g++ -O2 -std=c++20 sdnotify.cpp -o sdnotify
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
static bool sd_notify(const char* state) {                 // one datagram on $NOTIFY_SOCKET; Type=notify units block on READY=1
    const char* path = std::getenv("NOTIFY_SOCKET"); if (!path) return false;
    int fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0); if (fd < 0) return false;
    sockaddr_un a{}; a.sun_family = AF_UNIX; std::strncpy(a.sun_path, path, sizeof a.sun_path - 1);
    if (path[0] == '@') a.sun_path[0] = '\0';               // abstract namespace
    bool ok = sendto(fd, state, std::strlen(state), 0, reinterpret_cast<sockaddr*>(&a), sizeof a) >= 0;
    close(fd); return ok;
}
int main() {
    // ... map and pre-fault segments (Ch. 34), pin and warm hot threads (Ch. 62), run the §C.5 checks, drop capabilities (Ch. 99) ...
    sd_notify("STATUS=warming\n");
    bool warm = true;                                         // the warm-up and the readiness gate (Appendix M) decide this
    if (!warm) { sd_notify("STATUS=warm-up failed\n"); return 1; }
    sd_notify("READY=1\nSTATUS=armed\n");                      // systemd now starts After= dependants and begins WatchdogSec= accounting
    for (int i = 0; i < 3; ++i) { sd_notify("WATCHDOG=1\n"); usleep(100000); }   // from the supervisor loop at < WatchdogSec/2
}
```

  The `WATCHDOG=1` datagram is sent by the **supervisor thread on a housekeeping core** (§101.4.3's loop, which already reads the hot threads' heartbeats from shared memory), never by a hot thread — the hot thread's liveness is proven by its heartbeat word, the process's by the datagram, and `WatchdogSec=2` makes systemd the supervisor of last resort (`SIGABRT`, a core — Ch. 105 — and a restart).
- **Capabilities, named.** `AmbientCapabilities=` grants exactly what setup needs to an *unprivileged* user: `CAP_SYS_NICE` (`SCHED_FIFO`, `sched_setaffinity` onto isolated cores, `mbind`), `CAP_IPC_LOCK` (`mlockall` beyond `LimitMEMLOCK` — kept anyway for VFIO's `MAP_DMA` accounting), `CAP_NET_RAW` (`AF_PACKET`, raw sockets, `SO_BINDTODEVICE`), `CAP_NET_ADMIN` (`ethtool` flow steering — Ch. 76, multicast on a specific interface — Ch. 73). The process then **drops** them after setup (Ch. 99 §99.4.6: `capset` to the empty set before the hot threads are spawned, `PR_CAP_AMBIENT_CLEAR_ALL`), which the bounding set above makes irreversible. `CAP_SYS_PTRACE` is in the bounding set only so a forensics helper spawned by the supervisor can attach (Ch. 105) — never ambient. What is *not* here is as deliberate: no `CAP_SYS_RAWIO` (`resourceN`/`/dev/mem` — Ch. 86 says VFIO instead), no `CAP_SYS_ADMIN` (the knobs are set by the boot units — next bullet), no `CAP_BPF`/`CAP_PERFMON` (the observability sidecar's, Ch. 99 §99.4.6).
- **Unit ordering: the substrate first.** `hugepages.service` (reserve per node — Ch. 17), `nic-bind.service` (`driverctl` the bypass NIC to `vfio-pci`, pin the kernel NIC's vectors — Ch. 86 §86.4.1, Ch. 56), `prefetch-msr.service` (the per-core `wrmsr` — Ch. 12 §12.4.3, signed and early so lockdown allows it), `irq-affinity.service`, `resctrl.service` (Ch. 58), `ptp4l`/`phc2sys` (Ch. 77 — the clock must be *servoed* before `READY`, which the Appendix M.3 gate checks) — each a `oneshot` with `RemainAfterExit=yes`, ordered with `After=`/`Requires=`, so a missing piece fails the trading unit instead of leaving it to discover a cold box at the open. `systemd-analyze critical-chain trading@strategy-3.service` prints the resulting dependency path with timings.
- **Boot-to-ready as a number.** A trading host that reboots intraday (a kernel panic, a forced restart — Ch. 101 §101.4.4's warm restart assumes the *host* stayed up) has a budget from power-on to `armed`: firmware POST (30–90 s on a dual-socket server — the part you cannot shorten except by `kexec`, which skips it entirely: `kexec -l` the same kernel at boot, `systemctl kexec` for a software reboot in ~5 s instead of ~2 min, signed kernel only under lockdown), kernel + initramfs (early microcode — Ch. 6 §6.4.5, the IOMMU, `isolcpus`), the substrate units above (hugepage reservation of 64 GiB ≈ 2 s; NIC bind + FLR ≈ 1 s; PTP servo convergence ≈ 30–60 s — the long pole), then the unit's own warm-up (Ch. 62 — segment pre-fault, book rebuild from the snapshot, Ch. 101 §101.4.7, predictor warming) and Appendix M's gate. `systemd-analyze blame` and `critical-chain` attribute each second; the Appendix M.5 re-arm window says whether the sum fits before the next session. Measure it on a lab box after every release — it is a latency number like any other, with a budget.
- **Live patching: not during the session.** `kpatch`/`livepatch` replace a kernel function by rewriting its entry with `ftrace`'s `text_poke`, which sends an **IPI to every CPU** (the `CAL` row — Ch. 61), and the consistency model stalls tasks at kernel entry until each has transitioned — a few µs on the hot core per patch, and a scheduling hazard on a core that never enters the kernel (the transition never completes until it does; `klp_transition` falls back to signalling the task, Ch. 54 §54.4.3). The same applies to a `systemctl daemon-reexec`, a `modprobe` of anything that patches text, and `perf`'s own `text_poke` for uprobes (Ch. 80). All of it is a maintenance-window event; the in-session rule is that the kernel's text does not change.

The unit, the boot units, the release fingerprint they are checked against and the kernel's version table (Appendix H.7 — which kernel first carried each feature this chapter and the book depend on) are one artefact (Appendix C.6); Appendix M is the gate that runs on top of it every trading day.

## 101.5 Pitfalls & anti-patterns: shared fate, cascading restarts

- **Shared fate through a back door.** The whole point is isolation, but a shared resource silently re-couples the fault domains: a shared lock (cross-process mutex — Ch. 43) one process can hold while dying (leaving it locked), a shared disk a core dump floods (Ch. 105 §105.4.4), a shared core the dead process's restart-storm steals (Ch. 56), or shared memory a writer corrupts (§101.4.2). Audit for *every* shared resource and ensure one process's failure can't stall or corrupt another through it. Isolation you didn't verify is isolation you don't have.
- **Cascading restarts / crash loops.** A process crashes, its restart triggers load that crashes another, or a crash-loop hammers shared resources and drags everyone down. Use restart backoff, restarts-per-interval caps, and escalation-to-safe-state (§101.4.3) so a persistent failure goes to kill-switch, not an infinite loop.
- **A non-deterministic "deterministic" core.** Calling `now()`/`clock_gettime` (Ch. 20), reading a random seed, depending on map iteration order or thread scheduling, or doing I/O *inside* the core — any of these breaks exact replay (§101.2.2), and you discover it when a replay diverges from production. Time and randomness are *inputs*; the core touches nothing else. Test it: replay a captured session and assert bit-identical outputs — and journal the per-event digest (§101.4.6) so the failure names the *event*, not just the fact.
- **Hidden side effects / global state in the core.** A static counter, a cached `errno`, a lazily-initialized singleton — non-obvious state that makes `apply` depend on more than (state, input). Keep the core's state explicit and passed-in.
- **Monolith creep "for latency."** Merging roles back together to save IPC hops (§101.2.1) without measuring — re-coupling fault domains for a few ns that the shared-memory data plane (§101.4.1) mostly eliminates anyway. Keep the fault-critical splits (gateway, risk); co-locate only with a benchmark *and* a fault-containment justification.
- **No kill-switch, or a kill-switch in the dead process.** If the safe-state logic lives in the strategy that just crashed, it can't run. The kill-switch must live in a *surviving* process (the gateway, §101.4.4) that can cancel the dead component's orders. A trading system without a working kill-switch is a regulatory and financial hazard (the market-access / pre-trade-risk mandate — Appendix K §K.4, SEC 15c3-5; the safe-state discipline — Ch. 72 §72.4.3).
- **Restart that's up-but-cold.** Declaring recovery done when the process is *alive* rather than *warm-and-correct* (Ch. 62, §101.3). The first ticks after a cold restart are slow — measure warm-and-live, not process-alive.
- **Heartbeat that only catches exits.** Relying on `SIGCHLD` alone misses *hangs* (a live-but-stuck process). Use heartbeats to catch the hang (§101.4.3).
- **Raw pointers in shared memory.** Segments map at different addresses per process; raw pointers stored in shared memory are garbage in another process (Ch. 34, 100). Use offsets (§101.4.1).
- **A `Type=simple` unit "started" cold, or a hand-crafted restart script.** `exec` returned, so systemd started the dependants and the gate thought the role was up — while the segments were still faulting in and the book was empty. `Type=notify`, `READY=1` after warm-up and the gate, `TimeoutStartSec` as the alarm, `WatchdogSec` as the backstop, and the substrate units as `Requires=` (§101.4.9).
- **Live-patching the kernel (or `perf`-probing it) in the session.** A `kpatch`/`livepatch` apply, a `text_poke` for a uprobe, a module load — each is an IPI to the hot core and a transition that may stall a syscall-free task indefinitely (§101.4.9). Maintenance windows only; the in-session kernel text is immutable.
- **Core dump stalls the survivors.** A synchronous, huge, on-the-hot-disk core dump that freezes the box — and a kill-switch that waits for `SIGCHLD`, i.e. for the dump to finish. Make dumping small, bounded and off-path, and trigger the kill-switch from the fault handler (Ch. 105 §105.4.3–§105.4.4).

## 101.6 Exercises & checklist

**Exercises:**

1. **Contain a crash.** Build a monolith (feed + strategy + gateway in one process) and a process-per-role version with shared-memory data planes (§101.4.1). Inject a segfault into the strategy in both. Show the monolith loses the market view and the venue session; the split keeps both up and the gateway cancels the dead strategy's orders (§101.4.4).
2. **Prove determinism.** Build a state-machine core (§101.2.2), capture an input stream (Ch. 106), and replay it twice — assert bit-identical outputs. Then sneak a `clock_gettime` into the core and watch replay diverge; fix it by making time an input.
3. **Measure recovery, including warm.** Kill a process and measure detect → restart → recover → warm → live (§101.3), with HdrHistogram on the tail. Then *skip* the warm step (Ch. 62) and measure the first-tick latency penalty — quantify why "up" isn't "recovered."
4. **Single-writer enforcement.** Build a shared-memory ring with one writer / many readers (§101.4.2); add reader-side validation (Ch. 99) and show it catches a deliberately corrupting writer without propagating bad data into a decision.
5. **Heartbeat hang detection.** Make a process *hang* (busy-loop, no exit) and show `SIGCHLD` misses it but a heartbeat timeout (§101.4.3) catches it and triggers restart. Tune the timeout for detect-vs-false-positive.
6. **Shard isolation.** Shard strategies by symbol across processes (§101.4.5); crash one shard's strategy on a poison symbol and confirm the other shards keep trading. Measure that the blast radius is one shard.

**Checklist:**

- [ ] The system is **process-per-role** (feed / strategy / risk / gateway), each its own **fault domain**; the **blast radius** of every single failure is bounded and stated (§101.2.1, §101.2.3).
- [ ] The decision core is a **pure, deterministic state machine** — no I/O, no clock, no randomness inside; **time and randomness are inputs**; replay produces **bit-identical** outputs (§101.2.2).
- [ ] Processes communicate via **shared-memory lock-free rings** (Ch. 34, 67) using **offsets not pointers**, pre-faulted/`mlock`'d — cheap IPC, no per-message syscall (§101.4.1).
- [ ] **Single writer per segment** (Ch. 46); readers **validate** shared-memory contents (Ch. 99); corruption is contained to the writer's segment (§101.4.2).
- [ ] A **supervisor** watches liveness via **heartbeats** (catches hangs, not just exits) and restarts under **backoff/cap/escalation** policy (§101.4.3).
- [ ] A **kill-switch / safe-state** lives in a **surviving process** (the gateway) and can cancel a dead component's working orders; the session stays up (§101.4.4).
- [ ] Processes are **crash-only** (§101.2.4) with durable state in shm/journal; restart is **recovery + warm-up (Ch. 62)** measured to **warm-and-correct**, not process-alive (§101.2.4, §101.3, §101.4.4).
- [ ] **Core dumps** are small/bounded/off-path and **don't stall survivors** (Ch. 105); strategies are **sharded** by symbol/venue so one shard's crash contains to that shard (§101.4.5).
- [ ] **No shared-fate back doors** (shared locks/disk/core/segment) re-couple the fault domains — audited and verified (§101.5).

## 101.7 References

- The **crash-only software** paper (Candea & Fox) — crash-only design, recovery-as-the-only-path (§101.2.4).
- The **Erlang/OTP** supervision-tree and "let it crash" literature — process-per-role, supervisors, fault containment as architecture (§101.2.1, §101.4.3).
- The LMAX architecture write-ups (Ch. 48 references) — the deterministic business-logic core decoupled from I/O, fed from an event stream, for replay (§101.2.2).
- Ch. 34 (shared memory), Ch. 67 (cross-process IPC rings), Ch. 46 (single-writer publication), Ch. 62 (warming), Ch. 106 (capture/replay — the input stream the core replays).
- Regulatory guidance on pre-trade risk controls and kill-switches (e.g. SEC Rule 15c3-5 "market access") — why the risk gate and kill-switch are mandatory, not optional (§101.4.4).

## 101.8 Additional Reading

- Talks on trading-system architecture, fault domains, and deterministic replay (LMAX, exchange/matching-engine designs) — the process topology and state-machine pattern in production (§101.2).
- The functional-core / imperative-shell and event-sourcing literature — the pure-core/impure-edge split and replay from an event log (§101.2.2).
- Ch. 106 (*Capture/Persistence/Replay*) — recording the input stream and replaying it through the deterministic core; Ch. 116 (*Case Study*) — the whole topology end-to-end with replay-based regression gates; Ch. 62 (*Warming*) — fast warm restart; Ch. 99 (*Security*) — kill-switch and reader-side validation; Ch. 100 (*Hot Reload*) — config distribution to the processes.
- **Appendix F** — crash-only / state-machine / kill-switch glossary; **Appendix C** — isolation and core-affinity tuning that keeps the processes off each other's cores.

---

*Next: Ch. 102 — The Pre-Trade Risk Gate & Kill-Switch Path: the Risk role of §101.2.1 built — SEC 15c3-5 / MiFID II RTS 6 checks over flat per-symbol and per-account limit records in ~15 ns, checked notional arithmetic, TSC token buckets, limits published lock-free, kill-switch triggers as runtime monitors, and the measured time from trigger to every working order cancelled (a 4 MiB table walk vs a live-order bitmap: 300 µs vs 4 µs).*
