# Part XII — Observability & Operations in Production

# Chapter 77 — Process Topology & The Deterministic State Machine

> **Prerequisites:** Ch. 26 (mmap/shared memory — the inter-process data plane), Ch. 51 (IPC / cross-process ring buffers — how the processes talk), Ch. 35 (seqlock/single-writer publication — the per-segment discipline), Ch. 47 (warming — fast restart isn't fast if it's cold), Ch. 76 (hot reload — config distribution to the processes), Ch. 75 (security — privilege isolation, kill-switch/safe-state).
>
> **Leads into:** Ch. 78 (capture/replay — the deterministic state machine is what you replay *into*), Ch. 81 (case study — the whole topology end-to-end). A Part XII architecture chapter: structuring the system as cooperating processes so one failure can't take down the session, and isolating a pure, replayable core.

---

## 77.1 Why it matters: contain a crash, keep the venue session up

Everything before this chapter optimized a *process*. This one is about what happens when that process **crashes** — because it will. A null deref in a new strategy, an unhandled malformed packet that slipped past Ch. 75's gate, an assertion, a bad config, an OOM, a hardware glitch — software fails, and a trading system runs real money through it during live sessions. The architectural question is not *whether* a component fails but **what its failure takes down with it.** In a **monolith** — feed handler, strategy, risk, and order gateway all in one process — the answer is *everything*: one strategy's segfault kills the feed handler (you go blind to the market), the order gateway (you drop the venue session and orphan working orders), and risk (you lose the safety check) — all at once, mid-session, with open positions. A single bug becomes a total outage.

The alternative is **process-per-role**: split the system into separate OS processes — feed handler, strategy (often one per strategy/symbol-group), risk, order gateway — each in its own address space, communicating through shared-memory data planes (Ch. 26, 51). Now a crash is **contained**: if a strategy process dies, the feed handler keeps decoding the market, the gateway keeps the venue session alive and can cancel that strategy's working orders (kill-switch, §77.4.4), risk keeps enforcing limits, and a supervisor restarts just the dead process (warm — Ch. 47) while everything else keeps running. The **blast radius** of a failure shrinks from "the whole system" to "one role, one shard." For a system where an outage during market hours is a financial and regulatory event, fault containment is not optional polish — it's the difference between "we restarted one strategy in 200 ms" and "we were down and blind with open positions."

There's a second, equally important reason rooted in the rest of Part XII: **determinism and replayability.** If the core decision logic is a **pure, side-effect-free state machine** — same inputs always produce the same outputs, no I/O or clocks or randomness inside it — then you can **replay** a captured session (Ch. 78) through it and get *exactly* the same decisions, bit-for-bit. That's the foundation of debugging ("replay the crash"), backtesting ("replay history through the new strategy"), compliance ("prove what we did and why"), and regression testing (Ch. 81). Process topology and the deterministic state machine are two halves of the same idea: **separate the fragile, I/O-bound, non-deterministic edges (network, timers, OS) from a contained, pure, replayable core** — so failures are isolated *and* the system is reproducible. This chapter motivates the topology and the pattern (§77.2), measures restart/failover time and its tail (§77.3), and builds the shared-memory data planes, single-writer discipline, supervision, kill-switch, fast restart, and sharding that make it work (§77.4).

## 77.2 Mental model

### 77.2.1 Process-per-role vs monolith (feed handler / strategy / order gateway / risk)

Decompose the tick-to-trade path into separate processes by **role**, each a fault domain:

```
   wire ─► [ FEED HANDLER ]─shm─►[ STRATEGY(s) ]─shm─►[ RISK ]─shm─►[ ORDER GATEWAY ]─► wire
            decode (Ch.54)        pure decision        pre-trade      venue session,
            normalize, seq        (state machine)      limits/kill    order I/O
                 │                      │                  │                │
            (one process)          (one per strategy/   (one process)   (one process,
                                    symbol shard)                        holds the session)
```

- **Feed handler** — owns the market-data side (Ch. 54–57): decode, normalize, sequence/gap-handle (A/B arbitration and recovery — Ch. 54 §54.4.3, Ch. 55), publish a clean normalized stream into shared memory. If a strategy dies, the feed handler is untouched — the market view stays live.
- **Strategy** — the decision logic; often **one process per strategy or per symbol/venue shard** (§77.4.5) so strategies can't crash each other. Ideally a *pure state machine* (§77.2.2).
- **Risk** — the pre-trade gate (limits, position, kill-switch, Ch. 75): a separate process (or a mandatory in-line check) that no strategy can bypass or crash. The last line before an order leaves.
- **Order gateway** — owns the *venue session* (Ch. 52): the TCP/order-entry connection, sequence numbers, working-order state. Keeping this separate means a strategy crash doesn't drop the session or orphan orders — the gateway can cancel-on-disconnect / kill-switch the dead strategy's orders and keep the session up.

The trade-off is **latency vs containment**: crossing a process boundary costs an IPC hop (a shared-memory ring write/read — Ch. 51; ~50–150 ns one-way *latency* per hop, a cross-core cache-line transfer — Ch. 33, Appendix E's HITM row; Ch. 51 §51.3's ~7–15 ns/msg is amortized throughput cost, not hop latency — *not* a syscall if done right). A monolith saves those hops but couples all failures. The HFT resolution: use **shared-memory data planes** (§77.4.1) so the hops are cheap (no kernel, no copy — Ch. 26/51), and accept a small, bounded latency cost for a large containment win. The hottest, most-coupled stages can be co-located if a benchmark justifies it; the *fault-critical* split (gateway and risk separate from strategy) is rarely worth giving up.

### 77.2.2 The Deterministic State Machine Pattern: pure, side-effect-free core decoupled from I/O for exact replayability

The most important pattern in the chapter: **isolate the core decision logic into a pure, deterministic state machine, decoupled from all I/O, timers, and randomness.**

```
   IMPURE EDGE (non-deterministic):              PURE CORE (deterministic):
   ──────────────────────────────               ──────────────────────────
   network RX (Ch.48-64)                         apply(state, input) → (state', outputs)
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
using Input = std::variant<MdEvent, Fill, TimerFire, ConfigSwap>;   // one sequenced stream, assigned by the shell (Ch. 78)

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

// Replay harness (Ch. 78 → CI gate, §81.4.1): same inputs ⇒ bit-identical outputs, or the build fails.
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
- **Inputs are an ordered, captured stream.** Every input — each market-data message, each timer fire, each fill, each config change (Ch. 76) — is a record in a single ordered log (Ch. 78). The core consumes that stream.
- **The shell is the sequencer.** The impure shell around the core assigns a single global **sequence number** to *every* input — market data, fills, timer fires, config swaps — before the core sees it; that one sequenced stream, not the raw sources, is what Ch. 78 captures and what replay feeds back. Without it, two sources' relative order is a race the replay cannot reproduce.
- **Determinism ⇒ exact replay.** Because outputs depend *only* on (initial state + input sequence), feeding the *same captured input log* through the core reproduces the *same outputs exactly* — the same orders, the same decisions, bit-for-bit. This is the property everything in Ch. 78–81 relies on: replay the captured session to debug a production crash, to backtest a strategy change against real history, to prove compliance, to gate regressions in CI.

This is the same edge/core split as functional-core/imperative-shell, applied to trading for *both* fault isolation (the pure core has fewer ways to fail and no I/O to hang on) and reproducibility. The I/O, timers, and OS interaction — the non-deterministic, failure-prone parts — live in a thin shell around the core; the core itself is pure, testable, and replayable.

### 77.2.3 Fault domains and blast radius

A **fault domain** is a boundary a failure can't cross; the **blast radius** is what one failure actually takes down. Process boundaries (separate address spaces) are the strongest in-box fault domain: a segfault, heap corruption, or assertion in one process cannot corrupt another's memory or take down its thread — the OS contains it. Design so that:

- **Each role is its own fault domain** (§77.2.1) — a strategy crash can't corrupt the feed handler or gateway.
- **Each shard is its own fault domain** (§77.4.5) — one symbol/venue's strategy crash doesn't touch another's.
- **The blast radius of any single failure is bounded and known** — you can state, for each component, exactly what its death takes down and what survives. The gateway and risk survive *every* strategy failure (so orders can be cancelled and the session kept up); a feed handler failure is contained to its feed (the redundant A/B line and the recovery channels, Ch. 54–55, and other feeds survive).
- **Shared memory is a controlled hole in the isolation** (§77.4.1-2): processes share data-plane segments, so a writer corrupting its segment *can* feed bad data to readers — which is why the single-writer-per-segment discipline (§77.4.2) and reader-side validation (Ch. 75) matter. Isolation is the default; sharing is deliberate and disciplined.

### 77.2.4 Crash-only design

Design every process to be **crash-only**: the only way it stops is by crashing (or being killed), and the only way it starts is by recovering from whatever state survived. There is *no* separate "graceful shutdown" path to get wrong — stopping *is* crashing, and starting *is* crash recovery. The benefits: (1) you exercise the recovery path *every* restart, so it's always tested and fast (a rarely-run graceful-shutdown path rots and fails when you need it); (2) recovery is robust to *actual* crashes (power loss, OOM-kill, segfault) because that's the only path; (3) it pairs with fast warm restart (§77.4.4, Ch. 47) — a crash-only process keeps its durable state (in shared memory / journal, Ch. 78) so a restart reloads it warm rather than rebuilding from scratch. Crash-only means: persist what you need to recover (durably, off-process — shared memory, journal), make startup *be* recovery, and make it fast. "Kill -9 and restart" is the normal operation, not the emergency.

## 77.3 Measure it: failover/restart time and its tail

If fault containment is the goal, the metric is **how fast you recover** — and, in this book's spirit (Ch. 1), the *tail* of recovery time, because a slow restart at the wrong moment (market open, a volatility spike) is when it hurts most. Measure **detect → restart → warm → live**: from the instant a process dies to the instant its replacement is *making correct decisions at full speed*.

| Stage | What it costs | What dominates |
|---|---|---|
| **Detect** (supervisor notices death) | heartbeat timeout / `SIGCHLD` (§77.4.3) | the heartbeat interval — too long = slow detect; too short = false positives |
| **Restart** (spawn / re-exec the process) | process spawn + map shared memory (Ch. 26) | spawn cost; mapping pre-existing shm is fast |
| **Recover state** (crash-only, §77.2.4) | reload durable state from shm/journal (Ch. 78) | journal replay length — bounded by snapshot frequency |
| **Warm** (Ch. 47) | pre-touch pages, prime caches/TLB/predictors, shadow traffic | the warm-up you skip = a cold-start tail on the first real tick |
| **Live** | back to full-speed correct decisions | — |

The harness is the §77.4.3 supervisor itself: it `fork`/`exec`s a role that publishes a heartbeat sequence into a shared-memory slot; loop N times — `kill -9` the role (and, separately, `SIGSTOP` it to simulate a hang), timestamp (`CLOCK_MONOTONIC`) the detect (SIGCHLD vs heartbeat timeout), the re-exec, the shm attach, the snapshot + journal replay (Ch. 78) and the warm-up (Ch. 47), and record each as a histogram (Ch. 3) alongside the *survivors'* hot-path latency during the episode. Representative shape (toolchain and machine: Appendix L.3; figures pending real runs): a crash-only process whose state lives in shared memory and that warms before going live can recover in **~hundreds of ms to low seconds**; the *dangerous* number is the **tail** — a restart that has to rebuild state from a long journal, or that skips warming and takes cold-start stalls on its first ticks (Ch. 47), can be far slower exactly when conditions are worst.

The lessons:

- **Recovery time is detect + restart + recover + warm — measure all four, and the tail of each.** Teams measure "restart time" and forget **warm** (Ch. 47): a process that's "up" but cold is still effectively impaired for its first ticks. The case study (Ch. 81) measures *warm-and-correct*, not *process-alive*.
- **Bound recovery state with snapshots.** If recovery replays a journal (Ch. 78), the journal length bounds recovery time — take periodic state snapshots so replay starts from a recent checkpoint, not session-start (§77.4, Ch. 78).
- **Tune the heartbeat for detect-vs-false-positive.** Too slow and you're impaired longer before noticing; too fast and a transient stall (a page fault, an SMI, a scheduler preemption on a non-isolated core) looks like death and you restart needlessly (§77.4.3). Measure the false-positive rate against the detect latency.
- **The whole point is the *contained* failure has a *bounded, fast, warm* recovery** — the monolith's "recovery" is "restart everything and lose the session," which has no good tail.

## 77.4 Techniques

### 77.4.1 Shared-memory data planes between isolated processes

The processes are isolated but must exchange data on the hot path *without* a syscall per message — so they communicate through **shared-memory ring buffers** (Ch. 26, 51), not pipes/sockets:

- **`mmap`'d shared segments (Ch. 26)** carry the data plane: the feed handler writes the normalized market stream into a shared ring (Ch. 34/51); strategies read it; strategies write orders into a ring the risk/gateway read. The segment is mapped once at startup, pre-faulted and `mlock`'d (Ch. 26) so steady state never faults.
- **Lock-free cross-process rings (Ch. 51).** SPSC/MPSC rings (Ch. 34) laid out in the shared segment — same lock-free producer/consumer discipline as in-process, now across address spaces. A boundary crossing is a ring write + a ring read (tens to low-hundreds of ns — Ch. 51), *no kernel*, the cheap IPC that makes process-per-role affordable (§77.2.1).
- **Offsets, not pointers (ties Ch. 76).** A segment maps at different virtual addresses in different processes; data in it uses *offsets/indices within the segment*, never raw pointers (a raw pointer from process A is meaningless in process B). Same discipline as cross-process config (§76.4.3).
- **Dead readers and segment generations.** A reader process that dies mid-ring leaves a stale consumer cursor: a ring with back-pressure stalls on it (never on the hot path — Ch. 73's rule) and a ring without it overruns it; the supervisor (§77.4.3) must *reap* a dead reader's cursor (mark it detached, or let producers ignore cursors whose heartbeat has lapsed). A *writer* restart needs a **segment generation**: the restarted writer stamps a new generation into the segment header and re-initialises its rings, and readers that see the generation change resynchronise rather than consuming half-written state from the previous life.
- **Control plane separate from data plane.** Slow, rare control (config — Ch. 76, commands, kill-switch) can go over a separate channel; the hot data plane stays a clean, fixed-layout shared ring.

### 77.4.2 Single-writer-per-segment discipline

The rule that keeps shared memory safe and lock-free: **exactly one process writes each segment/ring; all others only read.** This is Ch. 35's single-writer-multiple-reader publication applied across processes:

- **One writer per ring.** The feed handler is the sole writer of the market-data ring; each strategy is the sole writer of its own order ring; the gateway is the sole writer of the fill ring. Single-writer means publication is a release store (Ch. 30/35) with no inter-writer coordination — no cross-process lock (which would be slow and a shared-fate hazard).
- **Readers validate (Ch. 75).** Shared memory is the one place the fault isolation leaks (§77.2.3): a buggy writer can put garbage in its segment. Readers treat segment contents with the same untrusted-input discipline as the wire (Ch. 75) where it matters — bounds/sanity checks on indices and lengths — so a writer's corruption is caught, not propagated into a wrong trade.
- **Single-writer also bounds the blast radius of corruption** — only the segment's one writer can corrupt it; a reader's crash can't corrupt the data for others. It maps cleanly onto the fault-domain model (§77.2.3): the writer owns its segment's integrity.

### 77.4.3 Supervisor/watchdog, heartbeats and liveness

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
    constexpr std::uint64_t kTimeoutNs = 50'000'000;               // tuned per §77.3: detect-latency vs false-positive
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
        // kill-switch first (§77.4.4): tell the gateway to cancel this shard's working orders — then restart
        if (now_ns() - minute_start > 60'000'000'000ull) { restarts_this_minute = 0; minute_start = now_ns(); }
        if (++restarts_this_minute > 5) { std::fprintf(stderr, "crash-loop: safe-state and alert\n"); return 2; }
        struct timespec b{0, long(backoff_ns)}; nanosleep(&b, nullptr); backoff_ns = backoff_ns * 2 > 1'000'000'000 ? 1'000'000'000 : backoff_ns * 2;
        child = spawn(argv + 1); last_seen = now_ns();
        while (hb->seq.load(std::memory_order_acquire) == last_seq) usleep(100);     // first heartbeat = "warm and live" (Ch. 47)
        std::printf("recovered in %.1f ms (detect %s)\n", (now_ns() - t_detect) / 1e6, dead ? "SIGCHLD" : "heartbeat timeout");
    }
}
```


- **Liveness via heartbeats.** Each process periodically writes a heartbeat (a sequence/timestamp in a shared-memory slot — cheap, off the hot path); the supervisor reads them. A missed heartbeat (beyond a tuned timeout, §77.3) means the process is dead *or hung* (a hang is as bad as a crash and a plain `SIGCHLD` won't catch it — the heartbeat does). Combine `SIGCHLD`/`waitpid` (catches exits) with heartbeats (catches hangs).
- **Restart policy.** On death, the supervisor restarts *that* process (warm — §77.4.4) under a policy: backoff to avoid a crash-loop hammering, a cap on restarts-per-interval, and an escalation (if a process won't stay up, kill-switch its orders and alert rather than loop forever). The supervisor itself must be simple and rock-solid (it's a fault domain whose failure is serious) — and often supervised in turn (systemd, a watchdog, or a peer).
- **The watchdog as a safety device.** A hardware or kernel watchdog (and an application watchdog) that triggers the kill-switch/safe-state (§77.4.4) if the *system* goes unresponsive — the last-resort liveness guarantee.

### 77.4.4 Kill-switch / safe-state on failure; fast restart with warm-up

When something fails, the system must reach a **safe state** fast, then recover:

- **Kill-switch / safe-state.** On a detected failure (a dead strategy, a risk breach, a stuck process, an operator command), drive to a *safe state*: cancel the affected strategy's working orders, stop sending new orders, flatten or hold per policy. The **gateway** (separate process, §77.2.1) is what executes this — it owns the session and mass-cancels the dead component's orders (per-strategy order tagging → venue mass-cancel or per-order cancels — the gateway's own cancel-on-*client*-disconnect policy) *even though that component is gone*; the venue's cancel-on-disconnect only fires if the *gateway's* session drops, which is the opposite of what you want here. This is *the* payoff of separating the gateway: a strategy crash doesn't strand its orders, because the surviving gateway kills them. (Ties Ch. 75 — the kill-switch is a safety/security control.)
- **When the gateway itself dies.** The gateway is the one role whose failure *is* a session failure: the venue's **cancel-on-disconnect** (enable it — Appendix K) is the safety net that flattens working orders, and recovery is a **hot-standby gateway** that re-logs on and performs sequence-number recovery (replaying the venue's unacknowledged messages — the session protocol's resend request) before the strategies are allowed to resume. Measure that path (§77.3) like any other restart; it is the longest one.
- **Fast restart with warm-up (Ch. 47).** Restart is crash-only recovery (§77.2.4): map the surviving shared state (Ch. 26), replay from the last snapshot to current (Ch. 78), **warm** the process (pre-touch pages, prime caches/TLB/predictors, run shadow traffic — Ch. 47) so its *first real tick is at full speed*, then go live. Skipping the warm step (§77.3) means the freshly restarted process limps through its first ticks — a tail event right after a failure, when the market may be moving.
- **Determinism makes restart correct.** Because the core is a deterministic state machine (§77.2.2) fed from the captured input stream (Ch. 78), a restarted process replays the inputs and arrives at *exactly* the correct current state — no guesswork, no divergence from the process that died. Crash-only + deterministic-replay = provably-correct fast recovery.

### 77.4.5 Core dumps without stalling survivors; symbol/venue sharding

Two operational techniques for keeping failures small and debuggable:

- **Capture core dumps without stalling survivors.** A core dump of a crashed process can be large and slow to write — and if it stalls the box (I/O storm, lock, freezing the writing thread) it can hurt the *surviving* processes (a shared-fate hazard, §77.5). Mitigate: shrink the dump first — `madvise(MADV_DONTDUMP)` on the data-plane segments every role maps (or clear the matching `coredump_filter` bits), `RLIMIT_CORE` for file dumps (note it is *not* enforced for piped dumps) — then pipe `core_pattern` to a handler on a housekeeping core (Ch. 42) that writes to a separate disk/path and compresses asynchronously, or capture a minimal dump. The dead process must yield *diagnostics* without taking the survivors' latency with it.
- **Symbol/venue sharding.** Partition strategies by symbol or venue across processes (and cores, Ch. 42) so that (1) one shard's crash contains to that shard's symbols — the rest keep trading (§77.2.3), and (2) load spreads across cores/NUMA nodes (Ch. 16). Sharding is fault-containment *and* scaling: a bug triggered by one symbol's data takes down only that symbol's strategy, and the blast radius is one shard, not the book. Choose the shard key (symbol, venue, asset class) so correlated work and correlated failures stay within a shard.

## 77.5 Pitfalls & anti-patterns: shared fate, cascading restarts

- **Shared fate through a back door.** The whole point is isolation, but a shared resource silently re-couples the fault domains: a shared lock (cross-process mutex — Ch. 32) one process can hold while dying (leaving it locked), a shared disk a core dump floods (§77.4.5), a shared core the dead process's restart-storm steals (Ch. 42), or shared memory a writer corrupts (§77.4.2). Audit for *every* shared resource and ensure one process's failure can't stall or corrupt another through it. Isolation you didn't verify is isolation you don't have.
- **Cascading restarts / crash loops.** A process crashes, its restart triggers load that crashes another, or a crash-loop hammers shared resources and drags everyone down. Use restart backoff, restarts-per-interval caps, and escalation-to-safe-state (§77.4.3) so a persistent failure goes to kill-switch, not an infinite loop.
- **A non-deterministic "deterministic" core.** Calling `now()`/`clock_gettime` (Ch. 17), reading a random seed, depending on map iteration order or thread scheduling, or doing I/O *inside* the core — any of these breaks exact replay (§77.2.2), and you discover it when a replay diverges from production. Time and randomness are *inputs*; the core touches nothing else. Test it: replay a captured session and assert bit-identical outputs.
- **Hidden side effects / global state in the core.** A static counter, a cached `errno`, a lazily-initialized singleton — non-obvious state that makes `apply` depend on more than (state, input). Keep the core's state explicit and passed-in.
- **Monolith creep "for latency."** Merging roles back together to save IPC hops (§77.2.1) without measuring — re-coupling fault domains for a few ns that the shared-memory data plane (§77.4.1) mostly eliminates anyway. Keep the fault-critical splits (gateway, risk); co-locate only with a benchmark *and* a fault-containment justification.
- **No kill-switch, or a kill-switch in the dead process.** If the safe-state logic lives in the strategy that just crashed, it can't run. The kill-switch must live in a *surviving* process (the gateway, §77.4.4) that can cancel the dead component's orders. A trading system without a working kill-switch is a regulatory and financial hazard (the market-access / pre-trade-risk mandate — Appendix K §K.4, SEC 15c3-5; the safe-state discipline — Ch. 55 §55.4.3).
- **Restart that's up-but-cold.** Declaring recovery done when the process is *alive* rather than *warm-and-correct* (Ch. 47, §77.3). The first ticks after a cold restart are slow — measure warm-and-live, not process-alive.
- **Heartbeat that only catches exits.** Relying on `SIGCHLD` alone misses *hangs* (a live-but-stuck process). Use heartbeats to catch the hang (§77.4.3).
- **Raw pointers in shared memory.** Segments map at different addresses per process; raw pointers stored in shared memory are garbage in another process (Ch. 26, 76). Use offsets (§77.4.1).
- **Core dump stalls the survivors.** A synchronous, huge, on-the-hot-disk core dump that freezes the box (§77.4.5). Make dumping async/bounded/off-path.

## 77.6 Exercises & checklist

**Exercises:**

1. **Contain a crash.** Build a monolith (feed + strategy + gateway in one process) and a process-per-role version with shared-memory data planes (§77.4.1). Inject a segfault into the strategy in both. Show the monolith loses the market view and the venue session; the split keeps both up and the gateway cancels the dead strategy's orders (§77.4.4).
2. **Prove determinism.** Build a state-machine core (§77.2.2), capture an input stream (Ch. 78), and replay it twice — assert bit-identical outputs. Then sneak a `clock_gettime` into the core and watch replay diverge; fix it by making time an input.
3. **Measure recovery, including warm.** Kill a process and measure detect → restart → recover → warm → live (§77.3), with HdrHistogram on the tail. Then *skip* the warm step (Ch. 47) and measure the first-tick latency penalty — quantify why "up" isn't "recovered."
4. **Single-writer enforcement.** Build a shared-memory ring with one writer / many readers (§77.4.2); add reader-side validation (Ch. 75) and show it catches a deliberately corrupting writer without propagating bad data into a decision.
5. **Heartbeat hang detection.** Make a process *hang* (busy-loop, no exit) and show `SIGCHLD` misses it but a heartbeat timeout (§77.4.3) catches it and triggers restart. Tune the timeout for detect-vs-false-positive.
6. **Shard isolation.** Shard strategies by symbol across processes (§77.4.5); crash one shard's strategy on a poison symbol and confirm the other shards keep trading. Measure that the blast radius is one shard.

**Checklist:**

- [ ] The system is **process-per-role** (feed / strategy / risk / gateway), each its own **fault domain**; the **blast radius** of every single failure is bounded and stated (§77.2.1, §77.2.3).
- [ ] The decision core is a **pure, deterministic state machine** — no I/O, no clock, no randomness inside; **time and randomness are inputs**; replay produces **bit-identical** outputs (§77.2.2).
- [ ] Processes communicate via **shared-memory lock-free rings** (Ch. 26, 51) using **offsets not pointers**, pre-faulted/`mlock`'d — cheap IPC, no per-message syscall (§77.4.1).
- [ ] **Single writer per segment** (Ch. 35); readers **validate** shared-memory contents (Ch. 75); corruption is contained to the writer's segment (§77.4.2).
- [ ] A **supervisor** watches liveness via **heartbeats** (catches hangs, not just exits) and restarts under **backoff/cap/escalation** policy (§77.4.3).
- [ ] A **kill-switch / safe-state** lives in a **surviving process** (the gateway) and can cancel a dead component's working orders; the session stays up (§77.4.4).
- [ ] Processes are **crash-only** (§77.2.4) with durable state in shm/journal; restart is **recovery + warm-up (Ch. 47)** measured to **warm-and-correct**, not process-alive (§77.2.4, §77.3, §77.4.4).
- [ ] **Core dumps** are async/bounded/off-path and **don't stall survivors**; strategies are **sharded** by symbol/venue so one shard's crash contains to that shard (§77.4.5).
- [ ] **No shared-fate back doors** (shared locks/disk/core/segment) re-couple the fault domains — audited and verified (§77.5).

## 77.7 References

- The **crash-only software** paper (Candea & Fox) — crash-only design, recovery-as-the-only-path (§77.2.4).
- The **Erlang/OTP** supervision-tree and "let it crash" literature — process-per-role, supervisors, fault containment as architecture (§77.2.1, §77.4.3).
- The LMAX architecture write-ups (Ch. 37 references) — the deterministic business-logic core decoupled from I/O, fed from an event stream, for replay (§77.2.2).
- Ch. 26 (shared memory), Ch. 51 (cross-process IPC rings), Ch. 35 (single-writer publication), Ch. 47 (warming), Ch. 78 (capture/replay — the input stream the core replays).
- Regulatory guidance on pre-trade risk controls and kill-switches (e.g. SEC Rule 15c3-5 "market access") — why the risk gate and kill-switch are mandatory, not optional (§77.4.4).

## 77.8 Additional Reading

- Talks on trading-system architecture, fault domains, and deterministic replay (LMAX, exchange/matching-engine designs) — the process topology and state-machine pattern in production (§77.2).
- The functional-core / imperative-shell and event-sourcing literature — the pure-core/impure-edge split and replay from an event log (§77.2.2).
- Ch. 78 (*Capture/Persistence/Replay*) — recording the input stream and replaying it through the deterministic core; Ch. 81 (*Case Study*) — the whole topology end-to-end with replay-based regression gates; Ch. 47 (*Warming*) — fast warm restart; Ch. 75 (*Security*) — kill-switch and reader-side validation; Ch. 76 (*Hot Reload*) — config distribution to the processes.
- **Appendix F** — crash-only / state-machine / kill-switch glossary; **Appendix C** — isolation and core-affinity tuning that keeps the processes off each other's cores.

---

*Next: Ch. 78 — Capture, Persistence & Replay Storage, recording every inbound tick and outbound order for deterministic replay, research, and compliance: lossless line capture with nanosecond timestamps (Ch. 57, 60), append-only/write-ahead journals and compact binary formats (Ch. 73), getting bytes to disk off the hot path (io_uring/`O_DIRECT`/NVMe — Ch. 49), and feeding captures into the deterministic state machine (Ch. 77) for replay and backtests.*
