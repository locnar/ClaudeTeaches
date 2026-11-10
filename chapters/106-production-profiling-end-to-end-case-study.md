# Part XIII — Observability & Operations in Production

# Chapter 106 — Production Profiling & End-to-End Case Study

> **Prerequisites:** Ch. 103–105 (the test suite, the CI gate and its A/A calibration, and the release procedure — this chapter's gate is the principle, theirs the mechanics), Ch. 88, 101, 102 (telemetry, host health, network health — the three continuous signal sources this chapter's monitoring half consumes), all of it — this is the capstone. Most directly: Ch. 2–3 (profiling / micro-benchmarking), Ch. 1 (tail-latency thinking), Ch. 19/68 (timestamps), Ch. 71 (eBPF production tracing), Ch. 91–96 (deterministic state machine + capture/replay — the basis of the regression gate), Ch. 62, 65, 68, 71, 72 (the wire path the walkthrough traces).
>
> **Leads into:** the Appendices (A: ARM/Graviton; C: tuning checklist; E: latency numbers; G: bibliography; I: tooling command cookbook; J: market-structure & protocol primer; L: reproducible benchmark harness). The book's final chapter: continuous performance monitoring, regression detection, and a full tick-to-trade walkthrough that touches every Part.

---

## 106.1 Why it matters: tying the whole book together

Seventy-five chapters built a toolkit — caches, branches, NUMA, atomics, lock-free queues, kernel bypass, FPGAs, capture/replay. This chapter assembles them into one **tick-to-trade path** and asks the two questions production actually cares about: *where do the nanoseconds go, end to end?* and *how do you keep them from coming back?* Everything before this was a technique in isolation, measured on a microbenchmark; here the techniques compose, interact, and contend for the same caches, cores, and PCIe lanes — and the only way to understand the *whole* is to measure the *whole*, in production, continuously.

Two things make this more than a summary. First, **the budget is a sum of distributions, not means** (Ch. 1). A tick-to-trade path is wire → NIC → decode → strategy → risk → order → wire, and each stage has a latency *distribution*. The end-to-end p99.9 is dominated by whichever stage has the fattest tail at the moment that matters (the open, a volatility spike, a microburst — Ch. 62), and that stage is rarely the one with the worst *mean*. You cannot reason about tick-to-trade by adding average stage costs; you must measure the end-to-end distribution and decompose *its tail* into stage contributions. This is why the book has insisted, since Ch. 1, on p99/p99.9 and on measuring the distribution — the capstone is where that discipline pays off or fails.

Second, **performance is not a state you reach but one you defend.** A path tuned to 800 ns today regresses to 1.2 µs next week because someone added an indirect call (Ch. 15), changed a struct's layout (Ch. 10), introduced an allocation (Ch. 28), or a compiler/kernel update changed codegen. Without **continuous monitoring** (you can't see a regression you don't measure) and **automated regression gates** (you can't prevent one you don't block), the latency you spent the book earning silently bleeds away. The deterministic state machine (Ch. 91) and capture/replay (Ch. 96) exist precisely so you *can* build a gate: replay a golden capture through the build in CI and assert the outputs are bit-identical *and* the per-stage latency hasn't regressed. This chapter covers the production-monitoring mental model (§106.2), walks a full tick-to-trade decomposition (§106.3), builds the CI regression gates / replay / simulation techniques (§106.4), and warns about the gap between what you test and what production actually does (§106.5). It's where the book stops being a list of techniques and becomes a *system*.

## 106.2 Mental model: three tiers — always-on, targeted-live, offline-on-replay

Low-latency systems must be observed **continuously, in production, at near-zero overhead** — because the behaviour that matters (tail latency under real market conditions) only happens in production, and the lab cannot reproduce the open. Earlier drafts of this chapter drew that as two halves: *production = cheap counters*, *offline = heavy tools on a replay*. Part XIII has since filled in the middle, and the honest model has **three tiers**, distinguished by what each costs the hot thread and who is allowed to arm it:

```
   ALWAYS-ON (≈ free; on every hot thread, all session)      TARGETED-LIVE (bounded; armed per investigation,    OFFLINE / REPLAY (deep; never on the trading host)
                                                              by a housekeeping core, for a session)
   ───────────────────────────────────────────────────────── ─────────────────────────────────────────────────── ──────────────────────────────────────────────────
   • trace header: rdtscp stage stamps per message (Ch. 87)  • perf record --overwrite, low rate, ONE core,      • perf / VTune / top-down on the replay (Ch. 2)
   • per-stage + end-to-end HdrHistograms in the shm           fp or LBR unwinding, frozen on trigger (§106.4.4,  • flame graphs, uncore / IBS attribution (Ch. 2 §2.4.4)
     telemetry segment, scraped off-core (Ch. 88)              Ch. 100 §100.4.3)                                   • deterministic replay + golden diff + per-event
   • binary log (Ch. 86) and the per-thread flight recorder  • LBR snapshot / Intel PT snapshot of the hot core    digest → first diverging event (Ch. 91 §91.4.6)
     frozen on an over-budget message (Ch. 100 §100.4.2)        (2–15 % while armed; decode elsewhere)              • latency-regression gate with CIs (§106.4.1)
   • rdpmc per-stage counter reads from the thread itself    • hardware data breakpoint armed from a sidecar        • simulation harness (§106.4.3)
     (§106.4.4) — cycles, LLC misses per stage, no syscall      (0 cost until the write — Ch. 5 §5.4.5)             • rr / gdb on the replay harness (Ch. 5 §5.4.2)
   • ftrace ring in x86-tsc on isolated cores (Ch. 54, 100)   • gdb non-stop on a HOUSEKEEPING thread; process_    • post-mortem of a core + crash record (Ch. 95 §95.4.5)
   • host / network samplers from housekeeping cores           vm_readv of versioned state (Ch. 5 §5.4.4)
     (Ch. 101, 102); uncore / RAPL / SMI counters (Ch. 2 §2.4.4) • bpftrace on boundaries and slow paths (Ch. 71)
   • capture of every tick and order (Ch. 96)
        │                                                          │                                                        ▲
        └──── triggers (Ch. 88 §88.4.4) freeze the rings ─────────┘──── capture (Ch. 96) + bundle (Appendix N) feed ──────┘
```

The three tiers and the rules between them:

- **Always-on must be ~free, and its evidence must survive the event.** Ch. 1's thesis applies to *observing* too: you cannot run a 2× profiler (Ch. 89's ASan point) on the trading path. So this tier is stores, not syscalls: TSC stamps at stage boundaries carried *in the message* (Ch. 87 — ~10–17 ns per stage, measured), histogram records into the shm segment (Ch. 88 — a few stores), binary log and flight-recorder records (Ch. 86, 100 — ~10 ns each with `rdtsc`), `rdpmc` reads of the core counters (§106.4.4 — ~20–40 cycles, no kernel), and the capture stream (Ch. 96) as the richest source. The one thing the hot thread *evaluates* is the per-message budget compare (Ch. 88 §88.4.4), and on breach it **freezes** its ring and the kernel's (Ch. 100 §100.2.4) so the evidence is not overwritten a millisecond later. Everything in this column is read by somebody else's core.
- **Targeted-live is bounded, and a housekeeping core arms it.** Between "free" and "offline" there is a sanctioned middle: `perf record` on *one* isolated core at a few hundred Hz with frame-pointer or LBR unwinding costs that core a ~1–2 µs NMI per sample and nothing else (§106.4.4 has the numbers and the `-C` IPI caveat); an Intel PT or LBR snapshot costs a few percent *while armed* and is frozen on the same trigger; a hardware data breakpoint costs nothing until the corrupting write (Ch. 5 §5.4.5); a non-stop attach to a housekeeping thread or an out-of-process read of versioned state costs the hot thread nothing (Ch. 5 §5.4.4); the uncore counters never touch the hot core at all (Ch. 2 §2.4.4). The rules: one investigation at a time, armed and disarmed from a housekeeping core, never a `perf -C` left running all day on an isolated core, never an all-stop attach, never a decode on the trading host.
- **Deep analysis happens offline, on replays — and it starts from the live tiers' evidence.** The expensive tools — `perf`, VTune, top-down, uncore attribution, flame graphs (Ch. 2) — run *off* the production path on a **deterministic replay** (Ch. 91, 96) of the captured session, bit-identical to production (Ch. 91 §91.2.2), so you profile the *exact* production behaviour with heavy tooling without touching the box. When the replay disagrees with production, the per-event digest (Ch. 91 §91.4.6) says at which event; when the question is one unrepeatable spike, the frozen rings and the incident bundle (Ch. 100, Appendix N) are the input, because a replay runs the message in 180 ns and cannot show the 41 µs the kernel took.
- **The tiers close a loop:** always-on detects and freezes → targeted-live and the bundle attribute → offline replay reproduces, the fix is made, the CI gate (§106.4.1) defends → deploy → the always-on tier watches the trend. A low-latency system that lacks any of the three is one deploy — or one 09:31:42 — away from a silent regression it cannot explain.

## 106.3 Measure it: a full tick-to-trade walkthrough

*Toolchain and machine: Appendix L.3.*

The capstone measurement: decompose the **end-to-end tick-to-trade latency distribution** into stage contributions, naming the chapter that governs each stage. Measured wire-to-wire with PTP hardware timestamps (Ch. 68) at the boundaries, as a distribution (HdrHistogram, Ch. 3), under a market-open microburst (Ch. 62). Representative budget for a *software* (kernel-bypass) path on a tuned box (Xeon Gold 6326, Linux 6.18 `isolcpus`/`nohz_full`, GCC 13 `-O2 -march=icelake-server`, Solarflare X2522 via ef_vi — explicit bypass, Ch. 72; NIC hardware timestamps at wire-in/wire-out, fenced `rdtscp` at the internal stage boundaries mapped to the PHC per Ch. 19/68; N ≈ 10⁷ ticks over a replayed open — Ch. 96; a **single-process build** — feed handler, book, strategy, risk and gateway on one isolated core; toolchain and machine: Appendix L.3; figures pending real runs):

| Stage | What happens | Governing chapters | ~p50 | ~p99.9 (the tail that matters) |
|---|---|---|---:|---:|
| **Wire → NIC → userspace** | packet arrives, kernel-bypass RX (poll-mode) | Ch. 65, 72 | ~700 ns | ~1 µs |
| **Decode / normalize** | parse ITCH/SBE off the wire, zero-copy | Ch. 62, 33 | ~80 ns | ~200 ns |
| **Book update / strategy** | update order book, run decision (state machine) | Ch. 30, 91, 9–14 | ~150 ns | ~500 ns |
| **Pre-trade risk** | limit/position/kill-switch check | Ch. 92 (built there: ~11–15 ns measured), 84, 86 | ~40 ns | ~120 ns |
| **Order encode → TX** | build order message, kernel-bypass TX | Ch. 62, 72 | ~500 ns | ~700 ns |
| **End-to-end tick-to-trade** | wire-in to wire-out (measured, not summed) | (all) | **~1.5 µs** | **~2.0 µs** |

The end-to-end p99.9 is *less* than the sum of the stage p99.9s (~2.5 µs): the stages' tails rarely coincide, so the measured end-to-end tail is roughly the sum of p50s plus the dominant stage's excess — which is why the end-to-end row is measured at the wire, never summed. The Ch. 91 process-per-role topology would add ~3 shared-memory ring hops at ~50–150 ns each (Ch. 59; Ch. 43 §43.3 measures the hop and the topologies) — the containment-vs-latency trade of §91.2.1, paid deliberately or co-located after a benchmark like this one; a sequencer in front of those roles (Ch. 98) replaces the point-to-point rings with one stamped stream for the price of one such hop, and makes the gateway, risk and journal replicas of one fold.

Read it the way the whole book taught:

- **The p99.9 is ~1.3× the p50, and the tail is not spread evenly** — the strategy/book stage contributes most of the tail (cache misses on a cold book entry — Ch. 8/30, a branch mispredict on a rare path — Ch. 14, a TLB miss on a rarely-touched page — Ch. 16; on a dual-socket box add the occasional NUMA-remote access — Ch. 17). The *median* (and any mean built from it) would point you at the NIC stage (the largest p50); the *tail* points you at the strategy stage. **Optimize the tail's dominant stage, not the mean's** (Ch. 1) — this single reframing is the book's thesis, made concrete.
- **Each stage's improvement traces to a Part.** The NIC stage is ~700 ns because of kernel bypass (Ch. 72) instead of ~5-15 µs kernel stack (Ch. 56) — Parts VIII/X. The decode is ~80 ns because it's zero-copy and branch-light (Ch. 62, 33) — Parts V/IX. The book update's *tail* is controlled by cache-aware layout (Ch. 9/30) — Part II/IV. The whole budget is the book, composed.
- **The next order of magnitude is hardware** (Ch. 79): an FPGA inline path collapses this ~1.5 µs software budget to ~tens-to-low-hundreds of ns wire-to-wire *with a flat tail* (p99 ≈ p50) — Part XI's latency tier. The decomposition tells you *which* stages to move to fabric (decode + risk + encode — the deterministic, simple, hot ones) and which to keep in software (the complex, evolving strategy — §79.4.3).
- **Decompose continuously, not once.** The per-stage histograms run in production (§106.2) so the decomposition is *live* — you see the tail shift stage-to-stage as conditions change, and you catch a regression as a stage's p99.9 creeping up *before* it becomes an incident.

This table *is* the book: every stage is a Part, every number is a technique, and the discipline of decomposing the *distribution* into stages and attacking the *tail's* dominant stage is everything Chapter 1 promised.

## 106.4 Techniques

### 106.4.1 Regression detection and latency-regression gates in CI

The defense against silent regression: **make latency a CI gate, the same way correctness is.** The principles, with the mechanics in Ch. 103–104:

- **Golden capture + golden output.** Keep a representative captured session (Ch. 96–97 — including a microburst, Ch. 62) and the golden outputs the deterministic core (Ch. 91) produces from it. This is the regression fixture; Ch. 103 §103.4.4 cuts the small per-build masters from it.
- **Correctness gate (determinism).** Replay the golden capture through the build and assert outputs are **bit-identical** to golden (§91.2.2), with the per-event digest column of §91.4.6 naming the first diverging event; run it under ASLR and across two compilers. Beside it, the logic suite of Ch. 103 — table-driven, property-based and deterministic-simulation tests — because a golden master catches *change*, not *wrongness*.
- **Parity gate (research vs production).** Ch. 96 §96.4.6's Python module, built from the same `kernel.hpp`, must produce the same digest over the golden capture as the engine; Ch. 85 §85.4.6 extends the digest across every language the firm runs.
- **Latency-regression gate.** Replay the capture, measure per-stage and end-to-end latency (the §106.3 decomposition) and compare the *distribution* against a stored baseline — but only after **calibrating what the gate can see**: Ch. 104 §104.3 ran the same binary twenty times and found a 22 % run-to-run p99 spread and a 196 % p99.9 spread on a shared runner, so the per-PR gate there checks the **median** and p99 with a tolerance derived from that A/A spread, uses a bootstrap CI whose lower bound must clear the tolerance *and* a rank test, and leaves p99.9 to the **nightly** run on the tuned box (Appendix C) with the replayed open (Ch. 97). Deterministic proxies (`instructions:u`, cachegrind, an asm hash of the hot functions) stand in where the timing gate is blind. Ch. 104 §104.4.1–§104.4.4 is the procedure; Appendix L.6 the skeleton.
- **Track the trend and bisect by replay.** Per-stage distributions stored per revision, a change-point test on the trend for the drift no single gate catches, and a nightly failure that starts a bisection over the same capture on the same box (Ch. 104 §104.4.6, §106.4.2).

### 106.4.2 Deterministic replay of captured market data

The engine under both diagnosis and the gate (Ch. 91–96, applied):

- **Replay = production fidelity, off the production box.** Feed the captured input stream (Ch. 96) into the deterministic core (Ch. 91) and reproduce the session exactly — same inputs, same outputs (§91.2.2). Now run the *heavy* tools on it: `perf`/VTune/top-down (Ch. 2), flame graphs, PMU counters (cache misses, branch mispredicts, front-end stalls — Ch. 2/8/13/14) — analyzing the *exact* production behavior without perturbing production. This is why determinism was worth the architectural cost.
- **Replay at two altitudes (§96.4.5).** Wire-level replay exercises the whole stack (decode → decision — find a decode-stage regression); application-level replay exercises just the core (find a strategy-stage regression). Use the altitude that isolates the stage you're diagnosing (§106.3).
- **Bisect regressions with replay.** When the trend dashboard (§106.4.1) shows a regression, replay the golden capture across builds to *bisect* which change caused it — deterministic replay makes performance bisection as reliable as `git bisect` for correctness.
- **Debug production incidents by replay.** A bad fill at 09:31:42 → replay that captured window through the core in a debugger (§96.4.5; the determinism property — §91.2.2; record/replay and reverse-debugging tooling — Ch. 5 §5.4.2), deterministically, to see exactly what happened and why — the operational payoff of capture+determinism.

### 106.4.3 Simulation harnesses

Replay reproduces what *did* happen; **simulation** explores what *would* happen when the strategy's own actions change the world:

- **Model the venue's responses.** A naive backtest/replay (§106.4.2) assumes your orders don't affect the market — false for anything that takes liquidity or signals. A simulation harness models the exchange: queue position, fills, partial fills, rejects, latency of the venue's responses — so you test the strategy's *behavior in a reactive market*, not just recompute its outputs against a fixed tape.
- **Closed-loop testing.** The simulator feeds market data → the strategy decides → the simulator models how that order changes the book and what comes back → the strategy reacts. This closed loop (vs replay's open loop) is where you validate a strategy change's *P&L and risk*, not just its latency.
- **Inject the rare and the hostile.** Simulation is also where you test the tail conditions production rarely shows on demand: the microburst (Ch. 62), the gap/recovery (Ch. 62–63), the malformed packet (Ch. 89), the failover (Ch. 91) — drive them deterministically and assert the system stays correct, safe, and within latency budget.
- **Simulation complements, doesn't replace, replay.** Replay gives *fidelity* (exactly what happened); simulation gives *counterfactual* exploration (what if). The book's testing strategy uses both: replay for regression/debugging/compliance, simulation for strategy research and tail-condition validation.

### 106.4.4 Profiling the live system safely

The targeted-live tier has one hard question — *what does it cost the hot thread?* — and this section answers it mechanism by mechanism. *Toolchain and machine: Appendix L.3. The VM the revision's measurements ran on exposes no core PMU (so `perf record`, LBR and `rdpmc` could not be timed there); the per-sample costs below are derived from the measured components — the frame-pointer walk and signal-frame costs of Ch. 26 §26.4.4 and Ch. 95 §95.3 — and the published mechanics, and are labelled representative for the reference Xeon Gold 6326.*

**1. What one `perf record` sample costs the sampled core.** A sample is an NMI: the counter overflows, the core takes the interrupt (no `XSAVE` — the NMI path saves integer state only), `perf` records the IP and, if asked, the user stack, writes the record into the per-CPU ring and returns.

```
   --call-graph        what the NMI path does                                                   per sample (representative)    at 99 Hz    999 Hz      9999 Hz
   ─────────────────── ──────────────────────────────────────────────────────────────────────── ────────────────────────────── ─────────── ─────────── ───────────
   (none, IP only)     NMI entry/exit + one 32-B record                                            ~0.5–1 µs                    0.01 %      0.1 %       1 %
   fp                  + frame-pointer walk of the user stack, ≤ 127 frames (Ch. 26 §26.4.4:       ~1–1.5 µs                    0.015 %     0.15 %      1.5 %
                         152 cycles at 37 frames) + 8 B per frame
   lbr                 + read the 32 LBR MSRs (~50–100 cycles each) — the CPU recorded them free   ~1–2 µs                      0.02 %      0.2 %       2 %
   dwarf               + copy 8 KiB (default) of user stack and the registers into the ring;       ~3–8 µs + 8 MB/s of ring     0.05 %      0.5 %       5 %
                         unwind OFFLINE in perf report                                             traffic at 999 Hz
```

The *percentage* is never the problem — even `dwarf` at 999 Hz is half a percent of the core. The **gap** is: every sample is a 1–8 µs hole in the hot thread's timeline, once per 1 / *f*, and at 9999 Hz that is a hole every 100 µs on a thread whose tick-to-trade budget is 2 µs — the observer *is* the tail. Hence the rule: on an isolated core, `-c <period>` (a fixed count, not `-F`'s adaptive frequency) tuned to a few hundred samples per second, `fp` or `lbr` (never `dwarf` — the copy is the cost, and the stacks are the same), `--overwrite` so the ring holds the last few seconds, and a trigger (Ch. 100 §100.4.3) rather than a session-long recording.

**2. What `perf -C <isolated core>` does to that core besides sampling.** `perf_event_open` for a *specific CPU* installs the event with a cross-CPU function call — **one IPI to the target core** at arm and one at disarm (`irq_vectors:call_function_single_entry` in the Ch. 54 ring; the `CAL` column of `/proc/interrupts` moves by one). A *counting* `perf stat -C 5 -I 1000` is worse: each interval **read of an active event on another CPU is itself an IPI** (`perf_event_read` → `smp_call_function_single`), so `-I 1000` IPIs the hot core once per second per event group, and asking for more events than the core has counters adds **multiplexing** — a 1–4 ms hrtimer *on the target core* that rotates the counters and, on a `nohz_full` core, brings the tick back (`timer:tick_stop success=0 dependency=PERF_EVENTS` — Ch. 54 §54.4.2). The consequence for the always-on tier: **the hot thread should read its own counters** (below); for the targeted tier: arm once, disarm once, and never multiplex on an isolated core (`perf stat -e` with at most the programmable-counter count — 8 on Ice Lake-SP with SMT off, 4 with it on — and no `-I` against a hot core).

**3. `rdpmc`: syscall-free, IPI-free counter reads from the hot thread.** Open the event *from the thread itself* (`pid = 0, cpu = -1`: it follows the thread), `mmap` the event's first page, and read the counter in user space with `rdpmc` using the index the page publishes — the kernel keeps the page's `lock`/`index`/`offset` consistent across context switches and counter reprogramming, so the read is a seqlock (Ch. 40):

```cpp
// rdpmc.cpp — syscall-free per-stage counter reads: perf_event_open once, mmap the event page, then rdpmc in user space.
// Build: g++ -O2 -std=c++20 rdpmc.cpp -o rdpmc    (GCC 13.3 / Clang 18.1 both build it; running needs a core PMU and
//        /sys/bus/event_source/devices/cpu/rdpmc = 1 (default: allowed for mmap'd events) or 2)
#include <cstdint>
#include <cstdio>
#include <linux/perf_event.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <x86intrin.h>

struct UserCounter {                               // one hardware counter, readable with rdpmc by the thread that opened it
    perf_event_mmap_page* pg = nullptr; int fd = -1;
    bool open(std::uint32_t type, std::uint64_t config) {
        perf_event_attr a{}; a.type = type; a.size = sizeof a; a.config = config; a.exclude_kernel = 1; a.exclude_hv = 1;
        fd = (int)syscall(SYS_perf_event_open, &a, 0, -1, -1, 0); if (fd < 0) return false;      // pid 0 = this thread, cpu -1 = follow it
        pg = (perf_event_mmap_page*)mmap(nullptr, 4096, PROT_READ, MAP_SHARED, fd, 0);
        return pg != MAP_FAILED && pg->cap_user_rdpmc;                                            // the kernel allows user-space reads of this event
    }
    inline std::uint64_t read() const {            // ~20–40 cycles (representative), no syscall; seqlock'd against reprogramming
        std::uint32_t seq; std::uint64_t v;
        do { seq = pg->lock; asm volatile("" ::: "memory");
             std::uint32_t idx = pg->index; v = pg->offset;                                        // index 0 = not currently on a counter
             if (idx) { std::uint64_t pmc = _rdpmc(idx - 1); pmc <<= 64 - pg->pmc_width; pmc >>= 64 - pg->pmc_width; v += pmc; }
             asm volatile("" ::: "memory"); } while (pg->lock != seq);
        return v;
    }
};
int main() {
    UserCounter cyc, llc;
    if (!cyc.open(PERF_TYPE_HARDWARE, PERF_COUNT_HW_CPU_CYCLES) || !llc.open(PERF_TYPE_HARDWARE, PERF_COUNT_HW_CACHE_MISSES)) { perror("perf_event_open/rdpmc"); return 1; }
    volatile std::uint64_t sink = 0; static std::uint64_t big[1 << 22];
    std::uint64_t c0 = cyc.read(), m0 = llc.read();                                               // stage start: two user-space reads
    for (std::uint64_t i = 0; i < (1u << 22); i += 64) sink += big[(i * 7919) & ((1u << 22) - 1)];   // the "stage"
    std::uint64_t c1 = cyc.read(), m1 = llc.read();                                               // stage end
    std::uint64_t t0 = __rdtsc(); for (int i = 0; i < 1000000; ++i) sink += cyc.read(); std::uint64_t t1 = __rdtsc();
    std::printf("stage: %llu cycles, %llu LLC misses; rdpmc read cost %.1f cycles\n", (unsigned long long)(c1 - c0), (unsigned long long)(m1 - m0), (t1 - t0) / 1e6);
}
```

Two reads per stage boundary — `cycles` and `LLC-load-misses`, or `instructions` for an IPC-per-stage — at ~20–40 cycles each (representative; `rdpmc` is a ~25-cycle instruction plus the two page loads) ride beside the Ch. 87 TSC stamp and give the always-on tier the one thing the TSC cannot: *why* the stage was slow (misses up with cycles up = memory; cycles up with misses flat = frequency or a stall elsewhere — Ch. 100 §100.4.4 rows 6–7). The event is opened once per thread at startup (a syscall, allowed there), never multiplexed (open at most the core's counter count), and `exclude_kernel` keeps `perf_event_paranoid = 2` happy. Log the deltas into the trace record or the flight recorder, not a histogram, unless the stage's histogram is the question.

**4. LBR snapshots and Intel PT windows on a tail event.** The last-branch record is the cheapest *history* the hardware keeps: 32 taken branches, recorded for free, read at a sample. `perf record -C 5 -e cycles:u -c 2000000 --call-graph lbr --overwrite --switch-output=signal` (Ch. 100 §100.4.3) keeps a ring of them and dumps the last few seconds on the trigger — the sample nearest the over-budget message shows the 32 branches before it. Intel PT (Ch. 2 §2.4.4) is the complete version at 2–15 % while armed: `perf record -e intel_pt//u -C 5 --snapshot`, frozen on the same `SIGUSR2`, decoded on another box. Both are *targeted-live*: armed for a market open under investigation, from a housekeeping core, disarmed after.

**5. Continuous profilers.** Parca, Pyroscope, Elastic Universal Profiling and their relatives attach an eBPF-driven `perf_event` sampler to **every CPU** at ~19–99 Hz and unwind in the kernel (frame pointers or their own DWARF/`.eh_frame` interpretation) — a fleet-wide 0.1–1 % that is excellent for the housekeeping half of the box and exactly the wrong thing for an isolated core: each sample is an NMI on the hot thread, the attach is an IPI, and the per-CPU event is a `nohz_full` tick dependency (Ch. 54). Run them with the isolated cores excluded (their CPU filter, or a `cpuset` that never includes the hot cores — Appendix C.4), and let the hot cores' "continuous profile" be what it already is: the per-stage histograms (Ch. 88) and the `rdpmc` deltas above, which cost no NMI at all.

The observer-effect budget, in one line: **the hot thread pays for stores and `rdpmc`; a housekeeping core pays for everything else; and anything that IPIs or NMIs the hot core is armed for a session, bounded, and frozen on a trigger.**

### 106.4.5 Closing the loop: from the latency ledger to fills, markouts and race outcomes

Every number in this book is justified by one sentence in Appendix K.7 — "the marginal nanosecond still changes fill rates" — that the book has never *checked*. The check is a **post-trade join**, off the hot path, over three journals: the wire capture (Ch. 97: every inbound tick with its hardware timestamp), the order journal (Ch. 93–94: every send with the triggering tick's sequence number, the decision record from the in-band ledger — Ch. 87 — and the NIC's hardware TX stamp), and the venue's acknowledgements and fills with the **venue's own timestamps** (Ch. 62 §62.4.2; Appendix J.5 — the matching engine's receive and match times, to the microsecond or better where RTS 25 applies). Keyed by `ClOrdID` and tick sequence, the join yields per order:

```
   reaction latency    t_tx_hw(order) − t_rx_hw(triggering tick)             the firm's tick-to-trade for THIS order (the §106.3 budget, realised)
   venue transit       t_venue_recv(order) − t_tx_hw(order)                   the wire + venue gateway; constant per path, so its variance is the venue's
   time to ack         t_rx_hw(ack) − t_tx_hw(order)                          the round trip the risk gate's ageing timer (Ch. 49 §49.4.2) is tuned against
   outcome             filled / partially filled / rejected / cancelled        from the lifecycle (Appendix J.3)
   queue position      venue timestamp of our order vs venue timestamps of the resting orders ahead of it (from the L3 feed, Ch. 62) at entry
   race outcome        for a liquidity-taking order: did ours reach the venue before the competing order that took the same liquidity? by how many ns?
   markout             mid-price change at +1 / +10 / +100 ms after our fill, signed by side — adverse selection: did the market move against the fill?
```

From per-order rows come the curves that decide where the next engineering month goes: **fill rate vs reaction latency** (bin orders by their own reaction latency; a knee marks where being faster stops buying fills because the competition is already slower), **markout vs reaction latency** (fills won *slowly* are the adversely selected ones — the race was lost and the fill is a consolation), **race margin histograms** per venue (by how many ns we win or lose, from the venue's timestamps — the only clock both sides share), and **time-to-ack by venue gateway** (which session is slow today). The analysis is a batch job over binary journals — the Ch. 96 §96.4.6 decoder bound into Python, Arrow tables, a sort-merge join on `(venue, ClOrdID)` and `(venue, seq)`; minutes for a day — and a strategy question only in its last step. Its first use is operational: Ch. 105 §105.4.4's canary compares a shard's fill rate and markout with its siblings'; a release that is 50 ns faster and fills *less* has changed what it decides, not how fast.

An illustration of the first curve's shape, from a **synthetic** race model (not a measured session — the method is the deliverable, the data must be your own captures): our order against one competitor whose reaction time is normally distributed around 1 200 ns (σ 300 ns); fill rate is the probability of arriving first:

```
   our reaction (ns)          fill rate
     600 ns reaction             96.9 %
     800 ns reaction             89.3 %
    1000 ns reaction             73.3 %
    1200 ns reaction             50.0 %
    1400 ns reaction             26.8 %
    1600 ns reaction             10.8 %
    2000 ns reaction              0.7 %
   the knee sits where our latency crosses the competition's distribution: from 1 200 to 1 000 ns buys ~+23 points; from 800 to 600 ns buys ~+8; below that, nothing the market is paying for.
```

On a real session the curve is the *measured* value of a nanosecond for each strategy and venue, and the §106.3 decomposition says which stage's hundred nanoseconds are the cheapest to buy. Two cautions: venue timestamps have their own semantics and resolution (gateway receive vs match time; µs vs ns — Appendix J.5's table), so the race margin is only as fine as the venue's clock; and markouts need the *consolidated* mid (Ch. 62 §62.2.3), not the venue's own book, or the adverse selection measured is the venue's, not the market's.

## 106.5 Pitfalls & anti-patterns: drift between test and production

- **Optimizing the mean, shipping the tail (the book's cardinal sin, one last time).** Tuning the stage with the worst *average* while the p99.9 is dominated by a different stage (§106.3). Always decompose the *distribution* and attack the *tail's* dominant stage (Ch. 1).
- **Drift between test and production.** The lab box isn't tuned like production (different BIOS/C-states/isolation — Ch. 6, Appendix C), the test data lacks the open's microburst (Ch. 62), the replay omits an input the production core consumes (a timer, a config change — Ch. 90/91), or the build flags differ (Ch. 26). The result: green in CI, regressed in production. Make test fidelity a first-class concern — same flags, same tuning, capture-based data, *all* inputs replayed.
- **Profiling overhead distorting the measurement.** Running a heavy profiler (or shipped sanitizers — Ch. 89) on the production path changes the thing you're measuring (and wrecks latency). Production observation is *cheap* (stamps, histograms, `rdpmc`, frozen rings — §106.2's always-on tier); the live tools that *are* sanctioned are bounded and armed per investigation from a housekeeping core (§106.4.4: a `perf` sample is a 1–8 µs NMI hole, a `perf -C` attach and every interval read is an IPI, `dwarf` unwinding copies 8 KiB per sample); deep profiling runs *offline on replay* (§106.4.2).
- **A regression gate too noisy to trust.** Run on an untuned/shared box, too few iterations, comparing single runs or means → the gate flaps, people disable it, regressions flow. Stabilize it (Ch. 3, Appendix C) and compare distributions (§106.4.1).
- **No continuous production monitoring.** Measuring latency only in the lab and assuming production matches. The tail that matters happens at the open under real conditions — instrument production continuously (§106.2) or you're blind to your actual p99.9.
- **Non-deterministic core silently breaking replay/gates.** If the core isn't truly deterministic (§91.5), replay diverges and both the correctness and latency gates become meaningless. The determinism gate (§106.4.1) must itself be enforced — replay must match golden bit-for-bit, always — and when it fails, §91.4.6's digest diff and checkpoint bisection localise the event instead of leaving a four-hour replay to be eyeballed.
- **Naive backtest mistaken for reality.** Open-loop replay (your orders don't move the market) trusted for a liquidity-taking strategy — the P&L is fiction. Use a simulation harness (§106.4.3) where the strategy's actions matter.
- **Measuring kernel/component latency, not wire-to-wire.** Timing inside the app and missing the NIC/PCIe/queuing (Ch. 68, 76) — the §106.3 walkthrough must be wire-to-wire (PTP), or it omits exactly the parts the customer (the market) sees.
- **Tuning once and walking away.** Performance is defended continuously (§106.1) — a compiler bump (Ch. 26), a kernel update (Ch. 6), a dependency change (Ch. 89) can regress codegen or jitter at any time. The gate and the monitoring are forever, not a one-time project — and the monitoring includes the *platform*: Ch. 101's isolation/thermal/SMI/clocksource invariants and Ch. 102's drop counters, sampled beside the application histograms, are what turn "the tail moved" into "this knob drifted".

## 106.6 Exercises & checklist

**Exercises:**

1. **Decompose your tick-to-trade.** Instrument every stage boundary with TSC/PTP timestamps (Ch. 19/68) and build per-stage HdrHistograms (Ch. 3). Under a microburst (Ch. 62), produce the §106.3 table for your system and identify which stage dominates the *p99.9* (vs which dominates the *mean*) — then optimize the former.
2. **Build a latency-regression gate.** Create a golden capture + golden output (Ch. 91–96); in CI, replay through the build and fail on (a) any output divergence and (b) any per-stage p99.9 regression beyond tolerance (§106.4.1). Introduce a deliberate regression (add a virtual call — Ch. 15) and confirm the gate catches it.
3. **Replay-based deep profiling.** Capture a production-like session, replay it (Ch. 91–96), and run `perf` top-down + flame graphs (Ch. 2) on the replay. Find the top tail contributor (a cache miss in the book — Ch. 30, a mispredict — Ch. 14) and fix it; confirm the fix on a re-replay without ever touching production.
4. **Bisect a regression.** Plant a regression several commits back; use deterministic replay (§106.4.2) to bisect which commit caused the per-stage latency to jump.
5. **Simulation vs replay.** Backtest a liquidity-taking strategy with open-loop replay and with a closed-loop simulation harness (§106.4.3) that models queue position and fills; quantify how much the open-loop backtest *overstates* the P&L by ignoring market impact.
6. **Test/prod drift hunt.** Deliberately diverge the test box from production (different governor/C-states, Ch. 6) and show the regression gate gives a different answer than production — then align them (Appendix C) and show it converges. Internalize that the gate is only as good as its fidelity.

**Checklist:**

- [ ] Tick-to-trade is measured **wire-to-wire (PTP, Ch. 68)** as a **distribution** (HdrHistogram, Ch. 3), **decomposed per stage** (§106.3), under a **microburst** (Ch. 62).
- [ ] Optimization targets the **stage dominating the p99.9**, not the mean (Ch. 1, §106.3).
- [ ] Production is monitored **continuously** at **~zero overhead** — TSC/PTP timestamps, per-stage histograms, eBPF/bpftrace (Ch. 71), capture (Ch. 96); **deep profiling runs offline on replay**, never on the production path (§106.2, §106.4.2, §106.5).
- [ ] A **CI correctness gate** asserts replay outputs are **bit-identical** to golden (Ch. 91) — also enforcing the core stays deterministic (§106.4.1).
- [ ] A **CI latency-regression gate** compares per-stage **distributions** against baseline on a **tuned, stable** box (Ch. 6, Appendix C), failing on regression beyond tolerance; a **trend dashboard** catches slow drift (§106.4.1, §106.5).
- [ ] **Deterministic replay** (Ch. 91–96) is used for production-fidelity deep profiling, regression **bisection**, and **incident debugging** (§106.4.2).
- [ ] A **simulation harness** models venue responses for closed-loop strategy/P&L testing and **rare/hostile tail-condition** validation (microburst, gap, malformed, failover) (§106.4.3).
- [ ] **Test fidelity** is enforced — same build flags (Ch. 26), same tuning (Appendix C), capture-based data, **all** core inputs replayed — so CI and production agree (§106.5).
- [ ] Performance is **defended continuously** (gate + monitoring forever), not tuned once (§106.1, §106.5).

## 106.7 References

- Brendan Gregg, *Systems Performance* and the flame-graph / USE-method work — continuous production performance methodology (§106.2; Ch. 2 references).
- The `perf`, VTune, and top-down microarchitecture analysis documentation (Ch. 2 references) — the offline-on-replay deep-analysis tools (§106.4.2).
- HdrHistogram and the coordinated-omission literature (Gil Tene; Ch. 3 references) — measuring and comparing latency *distributions*, the basis of the regression gate (§106.3, §106.4.1).
- The LMAX / deterministic-replay and event-sourcing literature (Ch. 42, 91, 96 references) — replay-based testing and regression detection (§106.4.1-2).
- Carl Cook, "When a Microsecond Is an Eternity" (CppCon) — the end-to-end HFT latency mindset this chapter consummates (§106.1, §106.3).
- Every prior chapter — this capstone composes them; see each Part's references for the technique behind each stage of §106.3.

## 106.8 Additional Reading

- CppCon and Mechanical Sympathy talks on production low-latency systems, tick-to-trade measurement, and latency-regression testing (§106.1, §106.4).
- The continuous-profiling literature and tools (eBPF-based production profilers, Ch. 71) — always-on, low-overhead production observation (§106.2).
- **Appendix E** (*Latency Numbers Every Trading Developer Should Know*) — the per-stage costs behind the §106.3 budget; **Appendix C** (*System Tuning Checklist*) — the quiet box the gate and production both need; **Appendix G** (*Annotated Bibliography*) — the consolidated reading behind the whole book; **Appendix A** (*ARM/Graviton*) — re-running this walkthrough on a non-x86 target.
- Ch. 1 (*The Latency Mindset*) — re-read it now: the capstone is Chapter 1's thesis (measure the distribution, attack the tail) realized end-to-end.

---

*This completes the book's chapters (Ch. 1–106). From the latency mindset (Ch. 1) through the deterministic, end-to-end tick-to-trade case study of this chapter, the twelve Parts build the full low-latency toolkit — measurement and methodology, CPU microarchitecture, language mechanics, memory, numerics, concurrency, OS isolation, networking from kernel sockets through the NIC and fabric to transport, hardware acceleration, and production operations. The Appendices (A–L) follow as the standing references: the ARM port, the alternative-language survey, the system-tuning checklist, the compiler-flag reference, the latency-numbers table, the glossary, the annotated bibliography, the C++23/26 feature-availability matrix, the tooling command cookbook, the HFT market-structure & protocol primer, the exchange-connectivity & physical-layer survey, and the reproducible benchmark & measurement harness.*

*Next: Appendix A — ARM / Graviton & Non-x86 Targets, the first of the standing references: what changes in this toolkit when the box is AArch64.*
