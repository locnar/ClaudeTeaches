# Part I — Foundations & Methodology

# Chapter 2 — Measure First: Profiling & Hardware Performance Counters

> **Prerequisites:** Ch. 1 (*The Latency Mindset*) — you reason about the distribution, not the mean. Solid C++ and Linux command-line fluency. For the hands-on parts you want a Linux box with `perf` installed (`linux-tools-$(uname -r)` on Debian/Ubuntu, `perf` package on RHEL/Fedora) and permission to read counters (`/proc/sys/kernel/perf_event_paranoid` ≤ 2, or run under `sudo`).
>
> **Leads into:** Ch. 3 (*Micro-benchmarking Done Right*) makes the measurement statistically honest; Ch. 4 (*Reading the Machine*) explains the asm that these counters point you at. The top-down method introduced here (§2.2.2) is the lens for all of Part II, and the cache/branch/front-end counters reappear in Ch. 7–14. Live production tracing with eBPF/`bpftrace` is Ch. 61.

---

## 2.1 Why it matters: you can't optimize what you can't see

Chapter 1 left you with a rule — *find and eliminate the sources of the tail* — and a problem: the tail's causes are invisible from the source code. Two functions that look identical in C++ can differ by 10× because one fits in L1 and the other misses to DRAM, or one branch is predictable and the other is a coin flip. The CPU does not tell you this by slowing down a line you can point at; it tells you by stalling, deep inside an out-of-order pipeline, in ways that no amount of staring at the source will reveal.

Human intuition about performance is wrong often enough to be dangerous. The classic failure mode is "optimizing the thing that looks expensive" — rewriting an arithmetic loop that the hardware was already executing four-wide for free, while the real cost was a single cache miss two lines up that you never suspected. Every experienced low-latency engineer has a story about a day spent optimizing code that a five-minute `perf` run would have exonerated.

So the discipline of this book is **measure first**, and it has a specific meaning: before you change anything, attach a tool that reads the CPU's own *hardware performance counters* (the PMU) and let the silicon tell you where the cycles went. Not a profiler that samples wall-clock time and guesses — the actual event counters baked into the chip: cycles, instructions retired, cache misses by level, branch mispredictions, front-end stalls, TLB misses. These are ground truth.

This chapter teaches you to ask the CPU three questions, in order:

1. **Is this code even the bottleneck?** (Don't optimize what isn't hot — Ch. 1's tier discipline.)
2. **What *kind* of bottleneck is it?** Is the pipeline starved at the front end, stalled at the back end waiting on memory, retiring useful work, or throwing work away on mispredicted branches? (Top-down analysis — §2.2.2.)
3. **Which specific resource is the culprit?** Which cache level, which branch, which data structure? (Targeted counters and sampling — §2.4.)

Answer those three and the rest of the book becomes a lookup table: each kind of stall has a chapter that fixes it. Skip them, and you are guessing with money on the line.

---

## 2.2 Mental model

### 2.2.1 The PMU and `perf_events`

Every modern x86-64 core contains a **Performance Monitoring Unit (PMU)**: a small set of hardware counters wired directly into the microarchitecture. There are two kinds:

- **Fixed-function counters** for the always-interesting events — core cycles, instructions retired, reference cycles.
- **General-purpose programmable counters** (typically 4–8 per logical core, fewer when hyperthreading shares them) that you configure to count one event each from a large menu: `L1-dcache-load-misses`, `LLC-load-misses`, `branch-misses`, `dTLB-load-misses`, `cycle_activity.stalls_mem_any`, and hundreds more, many of them model-specific.

The counters increment in hardware at zero cost to your code — they are just registers ticking alongside execution. The cost comes only when software *reads* or *reprograms* them. This is what makes the PMU the right tool for low-latency work: it observes without perturbing the hot path the way an instrumenting profiler would.

On Linux, the kernel exposes the PMU through the **`perf_events`** subsystem and the `perf_event_open(2)` syscall. You almost never call that syscall directly; you use the `perf` userspace tool (and, later, `bpftrace` and VTune, which sit on the same foundation). The mental model:

```
   your process            kernel                     hardware
   ------------       -----------------          ------------------
   hot loop  ──────►  perf_events subsystem ◄────►  PMU counters
                      (perf_event_open)             (fixed + GP)
        ▲                    │
        │                    ▼
   perf stat / record  reads counts, or delivers a
                       sample every N events (overflow IRQ)
```

A crucial constraint follows from the hardware: **there are only so many programmable counters.** Ask `perf` for more events than there are physical counters and it *multiplexes* — time-slicing the counters across your events and scaling the results up to estimate a full run. Multiplexed counts are estimates, not measurements, and on a short or bursty workload they can be badly off (§2.5). Knowing how many counters your core actually has is the difference between trusting a number and being misled by one.

### 2.2.2 Top-down microarchitecture analysis

Raw counters are a haystack. The breakthrough that makes them usable is the **Top-Down Microarchitecture Analysis Method (TMAM)**, developed by Ahmad Yasin at Intel. Instead of staring at fifty events, you classify every *pipeline slot* — an opportunity to issue one micro-op in one cycle — into one of four buckets. A core that can issue 4 µops/cycle ("4-wide") has 4 slots per cycle; over a run, every slot was either usefully used or wasted in one of these ways:

```
                        ┌─────────────────────────┐
                        │   all pipeline slots     │
                        └────────────┬────────────┘
                  did the slot issue a µop?
                ┌────────────┴─────────────┐
              NO  (stalled)             YES (issued)
        ┌───────┴────────┐         ┌───────┴────────┐
   Front-End Bound   Back-End Bound  did it retire?
   (fetch/decode      (execution    ┌──────┴───────┐
    can't feed        resources or  YES           NO
    the back end)     memory stall) Retiring   Bad Speculation
                                    (real work) (wrong-path work
                                                 thrown away)
```

- **Retiring** — slots that produced useful, committed work. You *want* this high. (Even here there's nuance: high retiring on bloated code is still waste — Ch. 18–21.)
- **Bad Speculation** — slots spent on instructions that were later squashed, dominated by **branch mispredictions** and machine clears. The work was done and thrown away. → Ch. 13–14.
- **Front-End Bound** — the back end was ready but the front end couldn't deliver µops fast enough: instruction-cache misses, I-TLB misses, decode bottlenecks, µop-cache (DSB) misses. → Ch. 12.
- **Back-End Bound** — µops were ready but couldn't execute because a resource was unavailable. Splits further into **Memory Bound** (waiting on caches/DRAM — the big one for trading workloads, → Ch. 7–10, 15–16) and **Core Bound** (execution ports / dependency chains saturated — → Ch. 11).

The power of this method is that it is **hierarchical and top-down**: you find the dominant bucket first, then drill *only* into that branch. If you are 60% Back-End Bound → Memory Bound, there is no point reading branch-misprediction counters; the memory subsystem is your problem, and Ch. 7 is your chapter. This is exactly the second question from §2.1, and it is the single most valuable habit in this chapter. On Linux, `perf stat --topdown` (Ice Lake and newer expose the full hierarchy) or Intel's `toplev` (from `pmu-tools`) computes these buckets for you.

### 2.2.3 Sampling vs counting

`perf` operates in two fundamentally different modes, and confusing them is a common beginner error:

- **Counting** (`perf stat`): program the PMU to count events over an entire run and report totals. Answers *"how many cache misses happened?"* and *"what was the IPC?"*. Low overhead, exact (when not multiplexed), but tells you *how much*, not *where*.
- **Sampling** (`perf record`): program a counter to overflow every *N* events (the *period*), and on each overflow the hardware traps and records where the instruction pointer (and optionally the call stack) was. Aggregate thousands of samples and you get a statistical profile: *"73% of L1-miss samples landed in `OrderBook::find_level`."* Answers *where*. Higher overhead, and subject to **skid** — the recorded IP can lag the instruction that actually caused the event because the pipeline is deep and out-of-order (§2.5). Precise event-based sampling (Intel **PEBS**, `perf record -e ...:pp`) reduces skid by having the hardware itself record the precise architectural state.

The workflow is: **`perf stat` to learn *what kind* of problem you have and whether it's even worth chasing; `perf record`/`report` to learn *where* it lives.** Stat narrows the question; record answers it.

---

## 2.3 Measure it: a guided `perf stat` / `perf record` session

Let's make this concrete with a deliberately memory-bound microbenchmark — a pattern you will meet for real in Ch. 7 and again in the order-book case study (Ch. 25). It compares summing an array **sequentially** (cache- and prefetcher-friendly) against summing the same values via a **random pointer-chase** (defeats the hardware prefetcher, misses to DRAM). Same arithmetic, same instruction count; the only difference is the memory access pattern — exactly the kind of cost that is invisible in source and obvious in the PMU.

```cpp
// perf_demo.cpp — same work, two memory access patterns. Profile with perf.
// Build: g++ -O2 -std=c++20 -fno-omit-frame-pointer perf_demo.cpp -o perf_demo
//   (-fno-omit-frame-pointer so perf can unwind stacks for flame graphs; Ch. 21.)
// Run:   ./perf_demo seq      (sequential, cache-friendly)
//        ./perf_demo rand     (random pointer-chase, cache-hostile)
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <numeric>
#include <random>
#include <vector>

int main(int argc, char** argv) {
    const bool random_mode = (argc > 1 && std::strcmp(argv[1], "rand") == 0);

    // ~256 MiB of 64-bit slots: far larger than any cache, so the access
    // pattern decides whether we hit cache or pay for DRAM.
    constexpr std::size_t N = 32u * 1024 * 1024;  // 32M elements * 8B = 256 MiB
    std::vector<std::uint64_t> next(N);

    // Build a traversal order: identity (sequential) or a random permutation
    // forming one big cycle (each step a dependent, unpredictable load).
    std::vector<std::uint64_t> order(N);
    std::iota(order.begin(), order.end(), 0);
    if (random_mode) {
        std::mt19937_64 rng(1);
        std::shuffle(order.begin(), order.end(), rng);
    }
    // next[order[i]] = order[i+1]: chase the permutation as a linked list.
    for (std::size_t i = 0; i < N; ++i)
        next[order[i]] = order[(i + 1) % N];

    // The measured loop: N dependent loads. Identical instruction stream in
    // both modes; only the addresses (and thus cache behaviour) differ.
    std::uint64_t idx = 0, checksum = 0;
    constexpr int PASSES = 4;
    for (int p = 0; p < PASSES; ++p)
        for (std::size_t i = 0; i < N; ++i) {
            idx = next[idx];        // dependent load: address of the next ⇐ this load
            checksum += idx;
        }

    std::printf("mode=%s checksum=%llu\n", random_mode ? "rand" : "seq",
                static_cast<unsigned long long>(checksum));
    return 0;
}
```

**Step 1 — count, to learn what kind of problem it is.** Run both under `perf stat` with a hand-picked event list (IPC, cache misses, branch misses):

```
$ perf stat -e cycles,instructions,L1-dcache-load-misses,LLC-load-misses,branch-misses \
      ./perf_demo seq
$ perf stat -e cycles,instructions,L1-dcache-load-misses,LLC-load-misses,branch-misses \
      ./perf_demo rand
```

Representative results on the reference machine — single-socket **Intel Xeon Gold 6326** (Ice Lake-SP, 2.9 GHz base), Linux 6.x, GCC 13 `-O2` (illustrative; reproduce on your own box):

```
                         seq (sequential)      rand (pointer-chase)
cycles                    1,180,000,000         18,900,000,000
instructions              1,074,000,000          1,074,000,000     <- identical work
IPC (insn/cycle)                   0.91                   0.057    <- ~16x worse
L1-dcache-load-misses        9,200,000            135,400,000
LLC-load-misses                 41,000            131,900,000      <- ~3200x more
branch-misses                  138,000                141,000      <- basically equal
```

Read it through the Ch. 1 + §2.2 lens, and the story writes itself:

- **Same instructions, ~16× the cycles.** The work is identical (instruction counts match to the percent); the *only* variable is the access pattern. So this is a memory problem, full stop.
- **IPC collapses from 0.91 to 0.057.** The random-mode core is issuing almost nothing per cycle — it is stalled, not computing. Low IPC + high cache misses is the textbook signature of **Back-End Bound → Memory Bound**.
- **LLC misses explode ~3200×; branch misses don't move.** This tells you *which* branch of the top-down tree to take: memory, not speculation. Don't waste a second on branchless tricks here — Ch. 7's prefetching and layout are the fix.

Confirm it with the top-down summary directly (Ice Lake+):

```
$ perf stat --topdown -- ./perf_demo rand
# ... Retiring  Bad-Spec  Front-End  Back-End
#        3.1%      0.4%       1.2%      95.3%   <- overwhelmingly Back-End (Memory) Bound
```

**Step 2 — sample, to learn where it lives.** In a one-function toy the answer is obvious, but the workflow is what matters. Sample on the LLC-miss event with precise (PEBS) sampling to minimize skid, then view the attribution:

```
$ perf record -e LLC-load-misses:pp -g -- ./perf_demo rand
$ perf report --stdio | head
# Overhead  Command    Symbol
#   98.7%   perf_demo  [.] main            <- the chasing loop, as expected
```

That two-step — **stat to characterize, record to localize** — is the core loop of performance work. Every later chapter assumes you can run it.

---

## 2.4 Techniques

### 2.4.1 IPC, cache-miss, and branch-mispredict counters

A handful of derived metrics carry most of the diagnostic weight. Learn to read them as a panel:

- **IPC (instructions per cycle) = `instructions / cycles`.** The headline health number. Modern cores can sustain ~3–4 IPC on dense compute; a hot path grinding at **IPC < 1 is stalled**, and the other counters tell you on what. (Caveat: IPC can be *misleadingly high* on code executing lots of cheap, useless instructions — pair it with top-down, never read it alone.)
- **Cache miss rates per level.** `L1-dcache-load-misses`, `LLC-load-misses` (last-level/L3), and the all-important **LLC misses ⇒ DRAM access** (~50–100 ns each; Ch. 7, Appendix E). Normalize per instruction or per operation, not as a raw count, so the number is comparable across runs. *Misses per kilo-instruction (MPKI)* is the standard unit.
- **Branch misprediction rate = `branch-misses / branches`.** Each mispredict costs ~15–20 cycles of squashed work (Ch. 13). A few percent is normal; double digits on a hot path is a red flag pointing at a data-dependent branch.
- **Stall-cycle breakdowns**: `cycle_activity.stalls_mem_any`, `...stalls_l3_miss`, etc., attribute *cycles* (not events) to specific stall reasons — the bridge between "many misses" and "many *cycles lost* to misses," which is what actually costs you latency.
- **TLB misses**: `dTLB-load-misses`, `iTLB-load-misses` — the signature that huge pages (Ch. 15) may help.

The discipline: **always normalize, always compare against a baseline.** "135M LLC misses" means nothing in isolation; "131 LLC-MPKI vs 0.04 in the sequential baseline" is a diagnosis.

### 2.4.2 Flame graphs for hot-path attribution

`perf report`'s flat list answers "which symbol," but real hot paths are deep call trees, and the cost you care about is often *inclusive* (a cheap function called a million times). **Flame graphs** (Brendan Gregg) visualize sampled stacks: width = share of samples, stacked by call depth. One glance shows you the widest plateau — the code where the samples actually piled up — and the path that got there.

```
$ perf record -F 999 -g -- ./your_hot_binary       # 999 Hz, with call stacks
$ perf script | stackcollapse-perf.pl | flamegraph.pl > flame.svg
```

Two notes that matter for low-latency work specifically:

- Build with **`-fno-omit-frame-pointer`** (or rely on DWARF/LBR unwinding) or the stacks will be truncated and the graph useless. This is a profiling build concern — Ch. 22.
- You can flame-graph **any** event, not just cycles. An *LLC-miss* flame graph shows exactly which call paths are paying for DRAM — often more actionable on a trading hot path than a time-based one, because it points straight at the data-structure choices of Part IV.

For the *off-CPU* side of the tail — time spent **not** running because you blocked on a lock, a syscall, or the scheduler (Ch. 31–46) — cycles-based sampling is blind by construction (a sleeping thread burns no cycles). That is the domain of **off-CPU flame graphs** and `bpftrace`/eBPF scheduler tracing (Ch. 61). Remember the boundary: on-CPU profiling finds where you burn cycles; off-CPU tracing finds where you *wait*. The tail of Ch. 1 lives in both.

### 2.4.3 VTune for microarchitectural drill-down

`perf` is the workhorse, but **Intel VTune Profiler** is worth knowing for deep microarchitectural work. It runs the full top-down hierarchy with a GUI that lets you drill from "Back-End Bound" down through "Memory Bound → L3 Bound → DRAM Bound" to the exact source line and the exact assembly, with the relevant counters attached at each level. It also automates Memory Access analysis (bandwidth, NUMA traffic — Ch. 16) and Microarchitecture Exploration so you don't hand-pick event lists or worry about counter multiplexing.

When to reach for it: when `perf stat --topdown` has told you the *category* but you need the GUI's guided drill-down to find the precise line, or when you want the NUMA/bandwidth analyses that are tedious to assemble by hand. On AMD, the analogous tool is **AMD uProf**; both sit on the same PMU foundation as `perf`, so the §2.2 mental model transfers unchanged. Don't let the GUI lull you — it is reading the same counters, with the same multiplexing and skid caveats (§2.5).

---

## 2.5 Pitfalls & anti-patterns: skid, multiplexed counters, profiling the wrong build

- **Profiling the wrong build.** The single most common and most embarrassing mistake: profiling a debug (`-O0`) or unoptimized build, or one without LTO/`-march`, then optimizing code the release compiler would have deleted or vectorized. **Always profile the build you ship**, with its real flags (Ch. 22). A flame graph of a `-O0` binary is fiction.
- **Counter multiplexing.** Ask for more events than the core has programmable counters and `perf` time-slices them and scales up — the output looks complete but is estimated. `perf stat` reports the `[ ... ]` enabled-percentage; if it's below 100%, your counts are extrapolations. Fix: request fewer events per run (group only what fits), or split across multiple runs. On a short or bursty hot path, multiplexing error can be large.
- **Skid.** In sampling mode the recorded instruction pointer can lag the instruction that truly caused the event by several instructions, because the pipeline is deep and out-of-order — so the "hot line" in `perf report` may sit a few lines past the real culprit. Use **precise (PEBS) events** (`:pp`/`:ppp` suffix) to pin the architectural state to the right instruction. Trusting non-precise skid blindly sends you optimizing the wrong line.
- **Frequency scaling and warm-up contamination.** Turbo, C-states, and DVFS (Ch. 6) mean the same code runs at different clocks depending on thermal/idle history, so `cycles` and wall-time wobble run-to-run. Pin frequency, pin the thread (Ch. 42), warm the path (Ch. 46), and prefer counting *instructions/misses* (frequency-independent) over raw time when you can. This is Ch. 3's whole subject — don't trust a single noisy run.
- **The observer effect.** Sampling at very high frequency, heavy `-g` DWARF unwinding, or attaching tracers perturbs the very latency you're measuring. Keep sampling rates sane (≤ a few kHz), prefer LBR/frame-pointer unwinding over DWARF on the hot path, and remember that the lowest-overhead observation is a hardware counter *count*, not a stack sample.
- **Optimizing without re-measuring.** A change that improves IPC can still *regress the p99.9* (Ch. 1) — e.g. by adding a branch that's usually free but occasionally mispredicts under real data. Close the loop: measure, change, **re-measure the distribution**, keep or revert. Never declare victory on a single mean.
- **`perf_event_paranoid` and permissions.** If counters read as zero or `perf` refuses to run, you likely need `kernel.perf_event_paranoid` lowered (`sysctl`) or appropriate capabilities; in containers you may need `--cap-add=PERFMON`/`SYS_ADMIN`. A silent zero is a permissions problem, not a fast program.

---

## 2.6 Exercises & checklist

**Exercises**

1. **Reproduce the gap.** Build `perf_demo.cpp` and run `perf stat -e cycles,instructions,L1-dcache-load-misses,LLC-load-misses,branch-misses` on both `seq` and `rand`. Compute IPC and LLC-MPKI for each. By what factor does each metric change? Which one *doesn't* move, and why does that rule out a whole category of fix?
2. **Top-down it.** Run `perf stat --topdown` (or `toplev -l1` from `pmu-tools`) on both modes. Which bucket dominates `rand`? Drill one level deeper (`-l2`) into that bucket. Does it say Memory Bound? Does it agree with your counter reading from exercise 1?
3. **Localize it.** `perf record -e LLC-load-misses:pp -g` on `rand`, then `perf report`. Now rerun *without* the `:pp` (non-precise) — does the attributed line move? That shift is skid.
4. **Multiplexing in action.** Run `perf stat` requesting ~12 events at once and check the enabled-percentage in brackets. Are any below 100%? Split them into two runs of fewer events and compare the counts. How much did multiplexing distort them?
5. **Flame the misses.** Generate both a cycles flame graph and an `LLC-load-misses` flame graph of `rand`. How do they differ in what they emphasize? (Builds intuition for Ch. 7.)

**Checklist — before you optimize anything**

- [ ] I profiled the **release build** with shipping flags, not a debug build.
- [ ] I ran `perf stat` (counting) **first** to confirm this code is the bottleneck and to learn *what kind* of bottleneck it is.
- [ ] I used **top-down** to find the dominant bucket and only drilled into that branch.
- [ ] My counts aren't **multiplexed** (enabled % = 100), or I split the events across runs.
- [ ] I normalized counters (IPC, MPKI, miss-rate), not raw totals, against a **baseline**.
- [ ] I used **precise (PEBS)** events for sampling so skid didn't mislocate the hot line.
- [ ] I controlled frequency/affinity/warm-up so cycles and time are trustworthy (→ Ch. 3, 6, 42, 46).
- [ ] After the change I **re-measured the distribution** (p99/p99.9), not just the mean or IPC.

---

## 2.7 References

- A. Yasin, *"A Top-Down Method for Performance Analysis and Counters Architecture,"* ISPASS 2014. The foundational paper behind §2.2.2 and `perf --topdown`/`toplev`.
- Intel, *64 and IA-32 Architectures Optimization Reference Manual* — PMU events, PEBS, and the top-down event hierarchy (the authoritative source for Intel counters).
- Intel, *64 and IA-32 Architectures Software Developer's Manual (SDM)*, Vol. 3B, "Performance Monitoring" — the architectural definition of the PMU and counters.
- B. Gregg, *Systems Performance: Enterprise and the Cloud*, 2nd ed., Pearson, 2020 — `perf`, sampling vs counting, and flame graphs, end to end.
- The Linux kernel `perf` documentation and `man perf-stat`, `man perf-record`, `man perf_event_open(2)` — the tool and syscall reference.

## 2.8 Additional Reading

- B. Gregg, *"Flame Graphs"* (brendangregg.com) and the `FlameGraph` toolkit — the canonical write-up and scripts used in §2.4.2; also his off-CPU flame graph posts.
- Intel `pmu-tools` (Andi Kleen) — `toplev`, `ocperf`, and friends: top-down analysis and human-readable event names on top of `perf`.
- Intel VTune Profiler and AMD uProf product documentation — the GUI drill-down tools of §2.4.3, including Memory Access and Microarchitecture Exploration analyses.
- D. Terpstra et al., *PAPI (Performance API)* — a portable library interface to the same hardware counters, useful for programmatic, in-process measurement.
- Appendix E (*Latency Numbers Every Trading Developer Should Know*) — the ns/cycle costs that turn raw miss counts into a latency budget.

---

*Next: Ch. 3 — Micro-benchmarking Done Right, where we make these measurements statistically honest: defeating dead-code elimination, taming frequency scaling, and capturing the tail without coordinated omission.*
