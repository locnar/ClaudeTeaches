# Part I — Foundations & Methodology

# Chapter 2 — Measure First: Profiling & Hardware Performance Counters

> **Prerequisites:** Ch. 1 (*The Latency Mindset*) — you reason about the distribution, not the mean. Solid C++ and Linux command-line fluency. For the hands-on parts you want a Linux box with `perf` installed (`linux-tools-$(uname -r)` on Ubuntu, `linux-perf` on Debian, the `perf` package on RHEL/Fedora) and permission to read counters (`/proc/sys/kernel/perf_event_paranoid` ≤ 2, or run under `sudo`).
>
> **Leads into:** Ch. 3 (*Micro-benchmarking Done Right*) makes the measurement statistically honest; Ch. 4 (*Reading the Machine*) explains the asm that these counters point you at. The top-down method introduced here (§2.2.2) is the lens for all of Part II, and the cache/branch/front-end counters reappear in Ch. 8–16. Live production tracing with eBPF/`bpftrace` is Ch. 81.

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

Raw counters are a haystack. The breakthrough that makes them usable is the **Top-Down Microarchitecture Analysis Method (TMAM)**, developed by Ahmad Yasin at Intel. Instead of staring at fifty events, you classify every *pipeline slot* — an opportunity to issue one micro-op in one cycle — into one of four buckets. A core that can issue 4 µops/cycle ("4-wide", Skylake) has 4 slots per cycle — the 5-wide Ice Lake-SP reference machine has 5 (`TOPDOWN.SLOTS` = 5 × cycles), Golden Cove 6; over a run, every slot was either usefully used or wasted in one of these ways:

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

- **Retiring** — slots that produced useful, committed work. You *want* this high. (Even here there's nuance: high retiring on bloated code is still waste — Ch. 21–26.)
- **Bad Speculation** — slots spent on instructions that were later squashed, dominated by **branch mispredictions** and machine clears. The work was done and thrown away. → Ch. 15–16.
- **Front-End Bound** — the back end was ready but the front end couldn't deliver µops fast enough: instruction-cache misses, I-TLB misses, decode bottlenecks, µop-cache (DSB) misses. → Ch. 14.
- **Back-End Bound** — µops were ready but couldn't execute because a resource was unavailable. Splits further into **Memory Bound** (waiting on caches/DRAM — the big one for trading workloads, → Ch. 8–12, 17–18) and **Core Bound** (execution ports / dependency chains saturated — → Ch. 13).

The power of this method is that it is **hierarchical and top-down**: you find the dominant bucket first, then drill *only* into that branch. If you are 60% Back-End Bound → Memory Bound, there is no point reading branch-misprediction counters; the memory subsystem is your problem, and Ch. 8 is your chapter. This is exactly the second question from §2.1, and it is the single most valuable habit in this chapter. On Linux, `perf stat --topdown` computes the Level-1 buckets for you (Ice Lake and newer expose them in hardware via `TOPDOWN.SLOTS`/`PERF_METRICS`; Sapphire Rapids adds Level 2 with `--td-level 2`); for the deeper levels use Intel's `toplev` (from `pmu-tools`), which derives them from ordinary counters on any generation.

### 2.2.3 Sampling vs counting

`perf` operates in two fundamentally different modes, and confusing them is a common beginner error:

- **Counting** (`perf stat`): program the PMU to count events over an entire run and report totals. Answers *"how many cache misses happened?"* and *"what was the IPC?"*. Low overhead, exact (when not multiplexed), but tells you *how much*, not *where*.
- **Sampling** (`perf record`): program a counter to overflow every *N* events (the *period*), and on each overflow the hardware traps and records where the instruction pointer (and optionally the call stack) was. Aggregate thousands of samples and you get a statistical profile: *"73% of L1-miss samples landed in `OrderBook::find_level`."* Answers *where*. Higher overhead, and subject to **skid** — the recorded IP can lag the instruction that actually caused the event because the pipeline is deep and out-of-order (§2.5). Precise event-based sampling (Intel **PEBS**, `perf record -e ...:pp`) reduces skid by having the hardware itself record the precise architectural state.

The workflow is: **`perf stat` to learn *what kind* of problem you have and whether it's even worth chasing; `perf record`/`report` to learn *where* it lives.** Stat narrows the question; record answers it.

---

## 2.3 Measure it: a guided `perf stat` / `perf record` session

Let's make this concrete with a deliberately memory-bound microbenchmark — a pattern you will meet for real in Ch. 8 and again in the order-book case study (Ch. 32). It compares summing an array **sequentially** (cache- and prefetcher-friendly) against summing the same values via a **random pointer-chase** (defeats the hardware prefetcher, misses to DRAM). Same arithmetic, same instruction count; the only difference is the memory access pattern — exactly the kind of cost that is invisible in source and obvious in the PMU. One discipline up front: `perf stat` counts the **whole process** by default, and here the setup (a 32 Mi-element `std::shuffle` in `rand` mode, ~2 G instructions and 32 M random misses that `seq` never executes) would swamp the loop we care about. So the listing lets `perf stat --control` start and stop the counters around the measured loop only — the steady-state-vs-setup split this book insists on everywhere.

```cpp
// perf_demo.cpp — same work, two memory access patterns. Profile with perf.
// Build: g++ -O2 -std=c++20 -fno-omit-frame-pointer perf_demo.cpp -o perf_demo
//   (-fno-omit-frame-pointer so perf can unwind stacks for flame graphs; Ch. 27.)
// Run:   ./perf_demo seq      (sequential, cache-friendly)
//        ./perf_demo rand     (random pointer-chase, cache-hostile)
// Count ONLY the measured loop (perf >= 5.10): create two FIFOs and let perf wait for
// our enable/disable commands instead of counting the ~2 G instructions of setup:
//        mkfifo perf.ctl perf.ack
//        perf stat -D -1 --control fifo:perf.ctl,perf.ack -e ... ./perf_demo rand
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <numeric>
#include <random>
#include <vector>
#include <fcntl.h>
#include <unistd.h>

// Tell `perf stat --control` to start/stop counting. A no-op when the FIFOs are absent
// (plain `./perf_demo` or `perf stat` without --control: then the WHOLE process is counted).
static void perf_ctl(const char* cmd) {
    static const int ctl = ::open("perf.ctl", O_WRONLY | O_NONBLOCK);
    static const int ack = ctl < 0 ? -1 : ::open("perf.ack", O_RDONLY);
    if (ctl < 0) return;
    char buf[8];
    if (::write(ctl, cmd, std::strlen(cmd)) > 0) (void)!::read(ack, buf, sizeof buf);
}

int main(int argc, char** argv) {
    const bool random_mode = (argc > 1 && std::strcmp(argv[1], "rand") == 0);

    // ~256 MiB of 64-bit slots: far larger than any cache, so the access
    // pattern decides whether we hit cache or pay for DRAM.
    constexpr std::size_t N = 32u * 1024 * 1024;  // 32M elements * 8B = 256 MiB
    std::vector<std::uint64_t> next(N);

    // Setup (NOT counted): a traversal order — identity (sequential) or a random
    // permutation forming one big cycle (each step a dependent, unpredictable load).
    std::vector<std::uint64_t> order(N);
    std::iota(order.begin(), order.end(), 0);
    if (random_mode) {
        std::mt19937_64 rng(1);
        std::shuffle(order.begin(), order.end(), rng);
    }
    // next[order[i]] = order[i+1]: chase the permutation as a linked list.
    for (std::size_t i = 0; i < N; ++i)
        next[order[i]] = order[(i + 1) % N];

    // The measured loop: N dependent loads per pass. Identical instruction stream
    // in both modes (mov/add/sub/jne — 4 instructions); only the addresses differ.
    std::uint64_t idx = 0, checksum = 0;
    constexpr int PASSES = 4;
    perf_ctl("enable\n");
    for (int p = 0; p < PASSES; ++p)
        for (std::size_t i = 0; i < N; ++i) {
            idx = next[idx];        // dependent load: address of the next ⇐ this load
            checksum += idx;
        }
    perf_ctl("disable\n");

    std::printf("mode=%s checksum=%llu\n", random_mode ? "rand" : "seq",
                static_cast<unsigned long long>(checksum));
    return 0;
}
```

**Step 1 — count, to learn what kind of problem it is.** Run both under `perf stat` with a hand-picked event list (IPC, cache misses, branch misses):

```
$ mkfifo perf.ctl perf.ack        # once; -D -1 = don't count until we say "enable"
$ perf stat -D -1 --control fifo:perf.ctl,perf.ack \
      -e cycles,instructions,L1-dcache-load-misses,LLC-load-misses,branch-misses ./perf_demo seq
$ perf stat -D -1 --control fifo:perf.ctl,perf.ack \
      -e cycles,instructions,L1-dcache-load-misses,LLC-load-misses,branch-misses ./perf_demo rand
```

Representative results for the **measured loop only** (4 × 32 Mi = 134 M dependent loads) on the reference machine — single-socket **Intel Xeon Gold 6326** (Ice Lake-SP, 2.9 GHz base, turbo off), Linux 6.18, GCC 13 `-O2` (illustrative; reproduce on your own box):

```
                         seq (sequential)      rand (pointer-chase)
cycles                      805,000,000         42,800,000,000     <- ~53x
instructions                537,000,000            537,000,000     <- identical work: 4 insns x 134M
IPC (insn/cycle)                   0.67                  0.0125    <- ~53x worse
L1-dcache-load-misses        16,900,000            134,100,000     <- 1 GiB/64 B vs one per load
LLC-load-misses                 270,000            122,300,000     <- ~450x more
branch-misses                     2,100                  2,400     <- basically equal
```

Read it through the Ch. 1 + §2.2 lens, and the story writes itself:

- **Same instructions, ~53× the cycles.** The work is identical (the loop is `mov`/`add`/`sub`/`jne`, 4 instructions per load, and the counts match to the percent); the *only* variable is the access pattern. So this is a memory problem, full stop. Even the *sequential* chain is latency-bound: each `idx = next[idx]` waits ~5 cycles for the L1 hit before the next address exists (Ch. 8), plus the DRAM fetch of the first line of every 4 KiB page that the prefetchers won't cross — ~6 cycles per load, IPC 0.67. The random chain waits a full DRAM round-trip *and* a page walk per load (~110 ns ≈ 320 cycles; Appendix E, Ch. 17): IPC 0.0125.
- **IPC collapses from 0.67 to 0.0125.** The random-mode core is issuing almost nothing per cycle — it is stalled, not computing. Low IPC + high cache misses is the textbook signature of **Back-End Bound → Memory Bound**.
- **LLC misses explode ~450×; branch misses don't move.** In `seq` the hardware prefetchers fetch the lines before the demand load arrives, so only the page-boundary lines (1 GiB / 4 KiB ≈ 260 k) show as demand misses; in `rand` ~91% of 134 M loads miss the 24 MB L3 (the table is 256 MiB). This tells you *which* branch of the top-down tree to take: memory, not speculation. Don't waste a second on branchless tricks here — Ch. 8's prefetching and layout are the fix.

(Run the same commands without the FIFOs and `-D -1` and you count the whole process: ~2.6 G instructions in both modes, the shuffle's 32 M random misses in the `seq`-vs-`rand` *difference*, and a far less legible table. Always ask what region a counter covered.)

Confirm it with the top-down summary directly (Ice Lake+):

```
$ perf stat -D -1 --control fifo:perf.ctl,perf.ack --topdown -- ./perf_demo rand
# ... Retiring  Bad-Spec  Front-End  Back-End
#        0.3%      0.0%       0.2%      99.5%   <- overwhelmingly Back-End (Memory) Bound
```

**Step 2 — sample, to learn where it lives.** In a one-function toy the answer is obvious, but the workflow is what matters. Sample on an L3-miss event that supports precise (PEBS) sampling to minimize skid — `mem_load_retired.l3_miss`, not the generic `LLC-load-misses`, which maps to an offcore-response *counting* event and is not precise even where the kernel accepts `:pp` on it — then view the attribution:

```
$ perf record -e mem_load_retired.l3_miss:pp -g -- ./perf_demo rand
$ perf report --stdio | head
# Overhead  Command    Symbol
#   98.7%   perf_demo  [.] main            <- the chasing loop, as expected
$ perf annotate --stdio -s main | head -40       # which *instruction* the samples landed on
```

`perf annotate` is the bridge from "which function" to "which load": it attributes samples to individual instructions (and source lines, with `-g`), which is exactly the view Ch. 4 teaches you to read. Group events that must be counted over the same interval — `-e '{cycles,instructions,LLC-load-misses}'` — so a ratio is not computed from two multiplexed, differently-scaled counts.

That two-step — **stat to characterize, record to localize** — is the core loop of performance work. Every later chapter assumes you can run it.

---

## 2.4 Techniques

### 2.4.1 IPC, cache-miss, and branch-mispredict counters

A handful of derived metrics carry most of the diagnostic weight. Learn to read them as a panel:

- **IPC (instructions per cycle) = `instructions / cycles`.** The headline health number. Modern cores can sustain ~3–4 IPC on dense compute; a hot path grinding at **IPC < 1 is stalled**, and the other counters tell you on what. (Caveat: IPC can be *misleadingly high* on code executing lots of cheap, useless instructions — pair it with top-down, never read it alone.)
- **Cache miss rates per level.** `L1-dcache-load-misses`, `LLC-load-misses` (last-level/L3), and the all-important **LLC misses ⇒ DRAM access** (~50–100 ns each; Ch. 8, Appendix E). Normalize per instruction or per operation, not as a raw count, so the number is comparable across runs. *Misses per kilo-instruction (MPKI)* is the standard unit.
- **Branch misprediction rate = `branch-misses / branches`.** Each mispredict costs ~15–20 cycles of squashed work (Ch. 15). A few percent is normal; double digits on a hot path is a red flag pointing at a data-dependent branch.
- **Stall-cycle breakdowns**: `cycle_activity.stalls_mem_any`, `...stalls_l3_miss`, etc., attribute *cycles* (not events) to specific stall reasons — the bridge between "many misses" and "many *cycles lost* to misses," which is what actually costs you latency.
- **TLB misses**: `dTLB-load-misses`, `iTLB-load-misses` — the signature that huge pages (Ch. 17) may help.

The discipline: **always normalize, always compare against a baseline.** "122M LLC misses" means nothing in isolation; "≈230 LLC-MPKI vs 0.5 in the sequential baseline" (122.3 M and 0.27 M misses over 537 M instructions) is a diagnosis.

### 2.4.2 Flame graphs for hot-path attribution

`perf report`'s flat list answers "which symbol," but real hot paths are deep call trees, and the cost you care about is often *inclusive* (a cheap function called a million times). **Flame graphs** (Brendan Gregg) visualize sampled stacks: width = share of samples, stacked by call depth. One glance shows you the widest plateau — the code where the samples actually piled up — and the path that got there.

```
$ perf record -F 999 -g -- ./your_hot_binary       # 999 Hz, with call stacks
$ perf script | stackcollapse-perf.pl | flamegraph.pl > flame.svg
```

Two notes that matter for low-latency work specifically:

- Build with **`-fno-omit-frame-pointer`** (or rely on DWARF/LBR unwinding) or the stacks will be truncated and the graph useless. This is a profiling build concern — Ch. 27.
- You can flame-graph **any** event, not just cycles. An *LLC-miss* flame graph shows exactly which call paths are paying for DRAM — often more actionable on a trading hot path than a time-based one, because it points straight at the data-structure choices of Part IV.

For the *off-CPU* side of the tail — time spent **not** running because you blocked on a lock, a syscall, or the scheduler (Ch. 42–62) — cycles-based sampling is blind by construction (a sleeping thread burns no cycles). That is the domain of **off-CPU flame graphs** and `bpftrace`/eBPF scheduler tracing (Ch. 81). Remember the boundary: on-CPU profiling finds where you burn cycles; off-CPU tracing finds where you *wait*. The tail of Ch. 1 lives in both.

### 2.4.3 VTune for microarchitectural drill-down

`perf` is the workhorse, but **Intel VTune Profiler** is worth knowing for deep microarchitectural work. It runs the full top-down hierarchy with a GUI that lets you drill from "Back-End Bound" down through "Memory Bound → L3 Bound → DRAM Bound" to the exact source line and the exact assembly, with the relevant counters attached at each level. It also automates Memory Access analysis (bandwidth, NUMA traffic — Ch. 18) and Microarchitecture Exploration so you don't hand-pick event lists or worry about counter multiplexing.

When to reach for it: when `perf stat --topdown` has told you the *category* but you need the GUI's guided drill-down to find the precise line, or when you want the NUMA/bandwidth analyses that are tedious to assemble by hand. On AMD, the analogous tool is **AMD uProf**; both sit on the same PMU foundation as `perf`, so the §2.2 mental model transfers unchanged. Don't let the GUI lull you — it is reading the same counters, with the same multiplexing and skid caveats (§2.5).

### 2.4.4 Beyond the core PMU: uncore, IBS, RAPL, SMI and Intel PT

Everything above counts events *inside* a core. The tail events the rest of the book hunts — DRAM bandwidth saturation, a neighbour evicting the LLC, cross-socket traffic, a NIC's DMA missing DDIO, a frequency-license drop, a firmware interrupt — happen *outside* it, and the core PMU sees only their consequences (a stall cycle, a miss). Four more counter families, and one trace, cover the rest. *The recipes are `perf` one-liners; the VM the other measurements in this chapter's revision ran on exposes no PMU at all (`/sys/bus/event_source/devices/` has only `breakpoint`, `msr`, `software`, `tracepoint`, `uprobe`), so the output shown is representative for the reference Xeon Gold 6326 and labelled; Appendix I.2 collects the commands.*

- **Uncore PMUs (Intel): IMC, CHA, UPI, IIO, M2M.** Each uncore *box* has its own counters, enumerated under `/sys/bus/event_source/devices/uncore_*` and `perf list uncore`. They are **socket-scoped and counting-only** (most boxes cannot sample): `perf` programs them through one CPU of the package (the PMU's `cpumask` in sysfs — CPU 0 and the first CPU of socket 1 by default), so arming and reading them **never touches an isolated core**, and they count everything on the socket, so attribution is by *difference* — idle baseline vs trading, or with one process running — not by thread.

  ```
     perf stat -a -I 1000 -e 'uncore_imc/cas_count_read/,uncore_imc/cas_count_write/'        # IMC: CAS × 64 B = DRAM bandwidth per channel (Ch. 8, 18)
     perf stat -a -I 1000 -e 'uncore_upi/txl_flits.all_data/'                                  # UPI: cross-socket data flits — the Ch. 18 remote-traffic counter
     perf stat -a -e 'uncore_cha/event=0x35,umask=0x21,config1=0x40433/'                        # CHA TOR_INSERTS: LLC lookups by source — I/O vs core (Ch. 76 DDIO)
     perf stat -a -I 1000 -e 'uncore_iio/data_req_of_cpu.mem_write.part0/,uncore_iio/data_req_of_cpu.mem_write.part1/'   # IIO: PCIe inbound writes = NIC DMA volume (Ch. 87)
     ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────── representative, 2× 6326, market open:
     1.000  uncore_imc/cas_count_read/   48.1 M  (× 64 B = 3.1 GB/s)     uncore_imc/cas_count_write/  12.6 M  (0.8 GB/s)   — far below the 8-channel ~200 GB/s peak:
     1.000  uncore_upi/txl_flits.all_data/  0.21 M                                                                          the hot socket is not bandwidth-bound; a
     1.000  uncore_iio/...mem_write.part0/  4.0 M  (× 4 B granule reporting — see the event's unit; ≈ 16 MB/s of NIC DMA)  neighbour saturating IMC would show here first
  ```

  The IMC row is the bandwidth number Ch. 18 and Ch. 58 §58.4.2's MBM approximate per RMID; the UPI row is the cross-socket leak a mis-pinned allocation produces (Ch. 18 §18.3); the CHA `TOR_INSERTS` filters by *opcode and source* and is how you see I/O-sourced LLC traffic — DDIO writes landing (or missing) in the LLC's I/O ways (Ch. 76); the IIO row is the PCIe inbound byte volume per stack (Ch. 87), i.e. what the NIC wrote. Uncore event encodings are per-generation (`config1` carries the opcode filter on Ice Lake-SP; the JSON event lists in `perf`'s `pmu-events/arch/x86/icelakex/uncore-*.json` are the reference), so copy them from `perf list --details`, not from memory.
- **AMD IBS (Instruction-Based Sampling).** Zen has no PEBS; its precise sampling is **IBS**: `ibs_op//` tags one µop every *N* and records, when it retires, its address, data address, cache level and latency, TLB behaviour and branch outcome — with zero skid, because the tag is attached before execution. `perf mem record` and `perf c2c record` (Ch. 8, 44) use IBS automatically on AMD; `perf record -e ibs_op/cnt_ctl=1/ -c 100000 -- ./app` samples by µop count, `ibs_fetch//` samples the front end (Ch. 14). Top-down on Zen is not Intel's TMA: Zen 4's `perf` ships *Pipeline Utilization* metric groups (`perf stat -M PipelineL1 …`; `perf list metricgroup` shows the exact names for your kernel) that decompose dispatch slots along AMD's own hierarchy; AMD uProf (§2.4.3) renders the same counters. The §2.2 model transfers; the event names do not — keep the AMD recipes in the runbook beside the Intel ones, because colo fleets mix them.
- **RAPL and frequency-license events.** `power/energy-pkg/`, `power/energy-ram/`, `power/energy-cores/` (`perf stat -a -I 1000 -e …`) read the RAPL energy MSRs in joules at ~1 ms granularity — the number that says whether the socket is at its power limit (Ch. 6 §6.4.1's BIOS knob, now as a counter) and that Ch. 114 samples beside thermal. On Ice Lake-SP the core-level `core_power.lvl0_turbo_license` / `lvl1` / `lvl2` events count cycles spent in each AVX frequency license — the direct measure of Ch. 39's downclocking caveat: a hot loop that is 3 % AVX-512 and 40 % `lvl2` cycles is paying the license for the whole stretch. `msr/aperf/` and `msr/mperf/` give the effective frequency ratio the same way (Ch. 114 §114.4.2).
- **SMI counting.** The `msr` PMU exposes `msr/smi/` — MSR `0x34`, `SMI_COUNT`: `perf stat -a -I 1000 -e msr/smi/` prints the number of System Management Interrupts per second per CPU, which should be **zero** on a tuned box (Ch. 6 §6.4.4, 46, 82). A non-zero delta aligned with a gap that has *nothing* in the ftrace ring is the firmware fingerprint of Ch. 113 §113.2.3. `turbostat --show SMI` and `rdmsr -p N 0x34` are the same number; read it from a housekeeping core (Ch. 114 §114.2.4 — an MSR read of the hot core IPIs it).
- **Intel Processor Trace.** Not a counter but the exact control flow: `perf record -e intel_pt//u -C 5 --snapshot` records every taken branch of CPU 5 into a per-CPU buffer at 2–15 % slowdown and is frozen on a trigger; `perf script --itrace` decodes it offline to the instruction. It answers "which instructions ran during the 41 µs" when no counter can, and it is Ch. 113 §113.4.3's deepest tool; this chapter only names it.

What this does to the method: the top-down hierarchy of §2.2.2 ends at *Memory Bound → DRAM Bound* or *Core Bound* — the uncore counters start there, and say whether the DRAM-bound stall is *your* bandwidth or a neighbour's (IMC), *your* socket or the other one (UPI), *your* loads or the NIC's DMA (IIO/CHA); IBS makes the same hierarchy precise on AMD; RAPL/license and SMI counters are the first thing to check when every stage stretches at once (Ch. 113 §113.4.4, rows 5–7). Arming any of them on a live box has a cost of its own — Ch. 119 §119.4.4 measures what a `perf` sample and a `-C` attach cost an isolated core, and gives `rdpmc` as the syscall-free way to read the *core* counters from the hot thread itself.

---

## 2.5 Pitfalls & anti-patterns: skid, multiplexed counters, profiling the wrong build

- **Profiling the wrong build.** The single most common and most embarrassing mistake: profiling a debug (`-O0`) or unoptimized build, or one without LTO/`-march`, then optimizing code the release compiler would have deleted or vectorized. **Always profile the build you ship**, with its real flags (Ch. 27). A flame graph of a `-O0` binary is fiction.
- **Counter multiplexing.** Ask for more events than the core has programmable counters and `perf` time-slices them and scales up — the output looks complete but is estimated. `perf stat` prints a trailing `(NN.NN%)` enabled-percentage on a multiplexed counter's line; if it's below 100%, your counts are extrapolations. Fix: request fewer events per run (group only what fits), or split across multiple runs. On a short or bursty hot path, multiplexing error can be large.
- **Skid.** In sampling mode the recorded instruction pointer can lag the instruction that truly caused the event by several instructions, because the pipeline is deep and out-of-order — so the "hot line" in `perf report` may sit a few lines past the real culprit. Use **precise (PEBS) events** (`:pp`/`:ppp` suffix) to pin the architectural state to the right instruction. Trusting non-precise skid blindly sends you optimizing the wrong line.
- **Frequency scaling and warm-up contamination.** Turbo, C-states, and DVFS (Ch. 6) mean the same code runs at different clocks depending on thermal/idle history, so `cycles` and wall-time wobble run-to-run. Pin frequency, pin the thread (Ch. 56), warm the path (Ch. 62), and prefer counting *instructions/misses* (frequency-independent) over raw time when you can. This is Ch. 3's whole subject — don't trust a single noisy run.
- **The observer effect.** Sampling at very high frequency, heavy `-g` DWARF unwinding, or attaching tracers perturbs the very latency you're measuring. Keep sampling rates sane (≤ a few kHz), prefer LBR/frame-pointer unwinding over DWARF on the hot path, and remember that the lowest-overhead observation is a hardware counter *count*, not a stack sample. The numbers behind the rule — what one sample costs an isolated core, the IPIs a `perf -C` sends it, and the `rdpmc` path that reads a counter from the hot thread with no syscall at all — are Ch. 119 §119.4.4.
- **Optimizing without re-measuring.** A change that improves IPC can still *regress the p99.9* (Ch. 1) — e.g. by adding a branch that's usually free but occasionally mispredicts under real data. Close the loop: measure, change, **re-measure the distribution**, keep or revert. Never declare victory on a single mean.
- **`perf_event_paranoid` and permissions.** If counters read as zero or `perf` refuses to run, you likely need `kernel.perf_event_paranoid` lowered (`sysctl`) or appropriate capabilities; in containers you may need `--cap-add=PERFMON`/`SYS_ADMIN`. A silent zero is a permissions problem, not a fast program.

---

## 2.6 Exercises & checklist

**Exercises**

1. **Reproduce the gap.** Build `perf_demo.cpp` and run `perf stat -D -1 --control fifo:perf.ctl,perf.ack -e cycles,instructions,L1-dcache-load-misses,LLC-load-misses,branch-misses` on both `seq` and `rand`; then once more without `-D -1 --control`, so the setup is counted too. Compute IPC and LLC-MPKI for each. By what factor does each metric change? Which one *doesn't* move, and why does that rule out a whole category of fix?
2. **Top-down it.** Run `perf stat --topdown` (or `toplev -l1` from `pmu-tools`) on both modes. Which bucket dominates `rand`? Drill one level deeper (`-l2`) into that bucket. Does it say Memory Bound? Does it agree with your counter reading from exercise 1?
3. **Localize it.** `perf record -e mem_load_retired.l3_miss:pp -g` on `rand`, then `perf report`. Now rerun *without* the `:pp` (non-precise) — does the attributed line move? That shift is skid.
4. **Multiplexing in action.** Run `perf stat` requesting ~12 events at once and check the trailing enabled-percentage in parentheses. Are any below 100%? Split them into two runs of fewer events and compare the counts. How much did multiplexing distort them?
5. **Flame the misses.** Generate both a cycles flame graph and an `LLC-load-misses` flame graph of `rand`. How do they differ in what they emphasize? (Builds intuition for Ch. 8.)

**Checklist — before you optimize anything**

- [ ] I profiled the **release build** with shipping flags, not a debug build.
- [ ] I ran `perf stat` (counting) **first** to confirm this code is the bottleneck and to learn *what kind* of bottleneck it is.
- [ ] I used **top-down** to find the dominant bucket and only drilled into that branch.
- [ ] My counts aren't **multiplexed** (enabled % = 100), or I split the events across runs.
- [ ] I normalized counters (IPC, MPKI, miss-rate), not raw totals, against a **baseline**.
- [ ] I used **precise (PEBS)** events for sampling so skid didn't mislocate the hot line.
- [ ] I controlled frequency/affinity/warm-up so cycles and time are trustworthy (→ Ch. 3, 6, 56, 62).
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
