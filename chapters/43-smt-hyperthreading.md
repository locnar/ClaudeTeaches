# Part VII — OS, Scheduling & Isolation

# Chapter 43 — SMT / Hyperthreading

> **Prerequisites:** Ch. 42 (pinning — you pin to a *logical* CPU; this chapter is its sibling), Ch. 11–12 (the execution ports / front-end the siblings share), Ch. 7 (the L1/L2 they share), Ch. 6 (BIOS — enabling/disabling HT), Ch. 41 (the hot-path-owns-the-core principle), Ch. 29 (AVX downclocking shares the core too), Ch. 1 (latency vs throughput).
>
> **Leads into:** Ch. 45 (RT scheduling / isolation — isolate the sibling too), Ch. 6/Appendix C (BIOS HT toggle). This resolves the "what about the sibling?" question Ch. 42 raised and feeds the per-core decision in the tuning checklist.

---

## 43.1 Why it matters: a sibling thread steals your resources

When you pin a hot thread to a logical CPU (Ch. 42), you've only handled *half* a physical core. **Simultaneous Multithreading (SMT)** — Intel calls it Hyper-Threading (HT) — makes one physical core present as **two logical CPUs** that *share the core's execution resources*: the execution ports (Ch. 11), the front-end (Ch. 12), the L1 and L2 caches (Ch. 7), the TLB (Ch. 15), the store buffer, the branch predictors (Ch. 13–14). The two logical CPUs run two threads *simultaneously*, interleaving their instructions through the *same* physical machinery. So pinning your hot thread to logical CPU 2 doesn't give it a whole core — it gives it *half* of one, and the **sibling** logical CPU (the other half of the same physical core) is running *something*, and whatever that something is **competes for and steals** the resources your hot thread needs.

This is the crux for low latency: SMT is a **throughput** optimization that is often a **latency** *pessimization*. The idea behind HT is that when one thread stalls (a cache miss — Ch. 7), the other can use the idle execution units — improving aggregate *throughput* by keeping the core busy. But your latency-critical hot thread doesn't *want* to share: when its sibling is busy, the sibling halves its share of execution ports, evicts its data from the shared L1/L2, pollutes its branch predictors and TLB, and competes for the front-end — so the hot thread runs *slower* and *more variably* (jitter — Ch. 1) than it would on a core it owned outright. The whole point of Part VII is for the hot thread to *own* its core (Ch. 41–42); SMT, left enabled with a busy sibling, gives away half of it. That's why HFT hot cores almost always run with the **sibling idle** or with **HT disabled entirely**.

But "disable HT" is not a universal answer — it's a *per-core* (or per-box) decision with a real trade-off, and that nuance is the chapter. Disabling HT (or leaving the sibling idle) gives the hot thread the *whole* physical core — lower, more deterministic latency — at the cost of *halving the logical CPU count*, which hurts the *throughput*-oriented work (risk, backtesting, housekeeping) that benefits from more hardware threads. So the right configuration is often **mixed**: HT *disabled* (or siblings *idle*) on the latency-critical hot cores, HT *enabled* on the housekeeping/throughput cores where the extra threads help. Making that decision requires *detecting which logical CPUs are siblings* (so you pin correctly — Ch. 42), *measuring* how much the sibling actually interferes with *your* workload (§43.3 — it varies a lot by what the thread does), and deciding HT on/off per core accordingly. This chapter explains the shared-resource mental model (§43.2), measures sibling interference (§43.3), covers sibling detection, idle-vs-housekeeping-sibling, and the per-core HT decision (§43.4), and warns about the cardinal sin — co-scheduling two hot threads on one physical core (§43.5).

## 43.2 Mental model: partitioned vs competitively-shared resources; throughput-vs-latency

**What SMT shares — and how.** Two logical CPUs (sibling threads) on one physical core share its microarchitectural resources, but *how* they share differs by resource — and that determines the interference:

```
   one PHYSICAL core, two LOGICAL CPUs (siblings):
   ┌──────────────────────────────────────────────────────────────┐
   │ front-end (fetch/decode/µop-cache — Ch.12)   shared/alternated │
   │ execution ports (ALU/LD/ST/FP/vector — Ch.11) COMPETITIVELY    │  ← the big one for latency
   │ L1i / L1d / L2 cache (Ch.7)                    COMPETITIVELY    │  ← sibling evicts your data
   │ TLB (Ch.15)                                    shared          │
   │ branch predictors (Ch.13-14)                   shared/polluted │
   │ store buffer, load buffer                      PARTITIONED      │  ← split in half while BOTH threads are active
   │ ROB / reservation stations                     partitioned/shared│
   └──────────────────────────────────────────────────────────────┘
```

- **Competitively-shared resources** (execution ports, L1/L2, branch predictors) are used by whichever thread demands them — so a *busy* sibling can take most of them, starving your thread. This is where the latency damage comes from: your thread gets fewer execution slots, its cache lines get evicted by the sibling's data, its predictors get polluted by the sibling's branches.
- **Partitioned resources** (store buffer, load buffer, parts of the ROB, the small-page ITLB) are *statically split in half* while **both** logical processors are active. They are recombined when the sibling is *halted* — executing `HLT`/`MWAIT`, i.e. parked in C1 or deeper (Intel SDM Vol. 3A §8.10.1: resources used by a halted logical processor become available to the active one). The subtlety is what "idle" means: a sibling that is idle but *not halted* — spinning in a busy-wait, running a polling housekeeping thread, or idling under `idle=poll` (Ch. 6) — keeps the core in two-thread mode, and your thread then gets only *half* the store buffer/ROB it would have with SMT off. That is one reason **disabling HT** (or truly halting the sibling) can beat **leaving the sibling idle** for the most latency-sensitive threads.

**The throughput-vs-latency trade-off (Ch. 1).** SMT's bargain:

- **Throughput win:** two threads share a core, so when one stalls (cache miss), the other uses the idle units — the core does more *aggregate* work than one thread could. Great for *throughput*-bound, parallel, latency-insensitive work (batch risk, backtesting, compilation, housekeeping). This is what HT was designed for.
- **Latency loss:** a latency-critical thread sharing a core with a busy sibling gets fewer resources and more variability — *higher and jitterier* latency than owning the core. Bad for the tick-to-trade path.
- **The implication:** SMT helps when you have *more threads than cores and care about throughput*; it hurts when you have a *thread that must be fast and deterministic*. A trading box has both kinds of work, so the answer is *per-core*: latency cores → sibling idle / HT off; throughput cores → HT on.

**Why "sibling idle" isn't the same as "HT off."** Two configurations give the hot thread "the core to itself":

- **HT off** (BIOS — Ch. 6): the physical core presents as *one* logical CPU, which gets *all* resources including the *full* store buffer/ROB (never partitioned, and no sibling for the OS to schedule onto). Cleanest, lowest-latency.
- **HT on, sibling left idle** (pin nothing to the sibling — Ch. 42): the hot thread runs alone on the core and — *as long as the sibling is actually halted* (`HLT`/`MWAIT` in C1+, not spinning, not `idle=poll`) — the partitioned resources recombine and it gets essentially the full core; but every wake-up of the sibling (a timer tick, an IRQ, a kernel thread) re-splits them for its duration, and the OS *could* schedule something on the idle sibling if you're not careful (isolate it — Ch. 45).

For the most latency-critical cores, **HT off** is often preferred (full resources, no risk of the OS using the sibling); **sibling idle** is the alternative when you want HT *on* elsewhere on the same CPU and can't toggle it per-core (HT is usually a whole-CPU BIOS setting — §43.4.3).

The model: **SMT runs two threads on one physical core, sharing execution ports / caches / predictors (competitively — a busy sibling steals them) and partitioning the store buffer/ROB (halved while both threads are active; recombined only when the sibling is *halted*, not merely idle-spinning). It trades latency for throughput: great for throughput work, bad for a latency-critical thread that wants to own its core. So latency cores run with the sibling idle or HT off; throughput cores keep HT on — a per-core decision.**

## 43.3 Measure it: per-thread interference with the sibling busy

The decisive measurement: run your latency-sensitive thread on a logical CPU and measure its latency/throughput with the **sibling idle** vs the **sibling busy** (running a resource-hungry load) — the gap is the SMT interference *for your specific workload*, which varies enormously by what the thread does.

```cpp
// smt.cpp — measure a thread's performance with its SMT sibling idle vs busy.
// Build: g++ -O2 -std=c++20 -march=native smt.cpp -o smt -pthread
// Find sibling of CPU 2:  cat /sys/devices/system/cpu/cpu2/topology/thread_siblings_list  (e.g. "2,34")
// Run:  taskset -c 2 ./smt            (sibling 34 idle)
//       (in another shell) taskset -c 34 ./smt_load    (sibling busy — a port/cache hog)
//       then re-run  taskset -c 2 ./smt   and compare
#include <cstdio>
#include <cstdint>
#include <vector>
#include <algorithm>
#include <chrono>

int main() {
    constexpr int N = 8192;                       // ~L1/L2 working set
    constexpr long REPS = 2'000'000;
    std::vector<std::uint64_t> a(N, 1);
    // a mixed workload: dependent + independent ops + loads (uses ports + L1/L2 — Ch.7,11)
    std::vector<std::uint32_t> samples; samples.reserve(REPS);
    for (long r = 0; r < REPS; ++r) {
        std::uint64_t t0 = __builtin_ia32_rdtsc();
        std::uint64_t s = 0;
        for (int i = 0; i < N; ++i) s += a[i] * 3 + (s >> 1);   // ports + loads + a dependency
        a[r & (N-1)] = s;                                       // a store
        std::uint64_t t1 = __builtin_ia32_rdtsc();
        samples.push_back(std::uint32_t(t1 - t0));
    }
    std::sort(samples.begin(), samples.end());
    auto pct=[&](double p){return samples[std::size_t(p*(REPS-1))];};
    std::printf("per-iter cycles: p50=%u p99=%u p99.9=%u  (run with sibling idle vs busy)\n",
                pct(0.5), pct(0.99), pct(0.999));
    return 0;
}
// smt_load: a sibling hog — tight loop hammering execution ports + cache (compile similarly).
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP, HT enabled), pinned, turbo off (illustrative; *interference varies hugely by workload*):

```
   hot thread on CPU 2:                p50 cycles   p99    p99.9    note
   sibling (CPU 34) IDLE (halted)      ~10,000      ~10,400 ~11,000  near full-core resources
   sibling BUSY (port/cache hog)       ~17,000      ~22,000 ~35,000  ~1.7-2x slower + jittier — SMT steals!
   HT OFF (whole core, BIOS)           ~9,200       ~9,400  ~9,600   fastest + flattest (no sibling wake-ups)

   the gap (idle→busy) is the SMT interference; the small (idle→HT-off) gap is sibling wake-ups re-splitting the core.
```

Read it: with the **sibling busy**, the hot thread runs **~1.7-2× slower and markedly jitterier** — the busy sibling stole execution ports (Ch. 11), evicted the working set from the shared L1/L2 (Ch. 7), and polluted the predictors (Ch. 13). That's the latency tax of SMT with a contended sibling, and it lands in the tail (Ch. 1) — *exactly* what a tick-to-trade path can't afford. With the **sibling idle**, the thread runs near full speed — *but* note that **HT off** is *still slightly faster and flatter* than sibling-idle: a halted sibling does recombine the partitioned store buffer/ROB (§43.2), yet every time it wakes (timer ticks, IRQs, kernel threads — unless fully isolated, Ch. 45) the core re-splits for the duration, and that lands in the tail. The magnitude of all three gaps **depends on the workload**: a thread bound on a resource the sibling also hammers (ports, L1) suffers most; a thread bound on something else (a long dependency chain — Ch. 11, or memory-bandwidth-bound where the sibling helps overlap) suffers less, occasionally even benefits. **So you must measure *your* workload** — the §43.3 idle-vs-busy gap *for the actual hot thread* — not assume. The conclusion the numbers point to for latency cores: **don't let a busy sibling share the hot core** — leave it idle or turn HT off (§43.4), and prefer HT-off for the most sensitive threads.

## 43.4 Techniques

### 43.4.1 Detecting sibling topology

You can't manage siblings without knowing *which* logical CPUs are siblings — pinning (Ch. 42) requires it:

- **Read the sibling map.** `/sys/devices/system/cpu/cpuN/topology/thread_siblings_list` gives the logical CPUs sharing CPU N's physical core (e.g. `2,34` means logical 2 and 34 are siblings). `lscpu -e` (CORE column) and `lstopo` (hwloc) show the full logical→physical→core→socket mapping. `/proc/cpuinfo` (`core id` + `physical id`) is the raw data.
- **The typical layout.** On a dual-socket / N-physical-core box with HT, logical CPUs are often numbered so that `0..N-1` are the first thread of each core and `N..2N-1` are the siblings (so CPU 2's sibling is `2+N`) — but **don't assume the numbering**; read `thread_siblings_list` (vendors/BIOS differ, and it's bitten many people who hard-coded `+N`).
- **Pin with siblings in mind (Ch. 42).** When you pin the hot thread to logical CPU 2, you now *know* its sibling is (e.g.) 34 — so you can leave 34 idle, or put only light housekeeping there, or ensure the OS/IRQs don't use it. Pinning correctly *requires* the sibling map.
- **Discover at startup, don't hard-code.** A robust app reads the topology at startup (parse `thread_siblings_list`, or use hwloc) and pins accordingly — so it works across machines with different layouts. Hard-coded CPU numbers break when you change hardware or BIOS settings.

### 43.4.2 Leaving the sibling idle vs housekeeping-only

When HT is *on* (whole-CPU setting you can't or don't want to disable), manage the sibling of each hot core:

- **Leave the sibling fully idle.** Pin nothing to it, and **isolate** it (`isolcpus`/`cpuset` — Ch. 45) so the OS scheduler can't place anything there either. The hot thread then runs ~alone on the physical core (close to the §43.3 "sibling idle" numbers). The cost: that logical CPU is "wasted" (no work runs there) — you trade a hardware thread for the hot thread's determinism. And the sibling must actually be *halted* for the partitioned resources to recombine (§43.2): leave C1 (`HLT`/`MWAIT`) available on it rather than booting with `idle=poll` (Ch. 6), which spins the "idle" sibling and keeps the core in two-thread mode.
- **Put only *light* housekeeping on the sibling.** If you can't afford to idle it, run only *non-bursty, resource-light, latency-insensitive* work on the sibling (a slow poller, a counter aggregator) — something that rarely demands the shared ports/cache, so its interference with the hot thread is minimal. **Never** put a *busy* or *latency-critical* thread on a hot core's sibling (§43.5). Measure the actual interference (§43.3) — even "light" work can hurt a sensitive thread.
- **Route IRQs off the sibling too (Ch. 42).** The sibling shares the core, so an IRQ handled on the sibling pollutes the hot thread's caches just like one on the hot logical CPU. Route IRQs off *both* logical CPUs of a hot physical core.
- **Sibling-idle vs HT-off (§43.2).** Leaving the sibling idle is the *per-core* approximation of HT-off when HT is a whole-CPU BIOS setting and you want it *on* for other cores. It's *almost* as good when the sibling is halted and isolated (the partitioned resources recombine — §43.2), but every sibling wake-up re-splits the core; for the most sensitive cores, HT-off (or offlining the sibling) is better (§43.4.3).

### 43.4.3 Deciding HT on/off per core

The strategic decision — and it's genuinely a *decision*, measured per workload:

- **Latency cores: HT off (or sibling idle).** The hot tick-to-trade cores want to own their physical core fully — **HT off** gives them the whole core with no sibling to ever re-partition it (§43.2), the lowest and flattest latency (§43.3). This is the default for the latency-critical cores in HFT.
- **Throughput cores: HT on.** Cores running *throughput*-bound, parallel, latency-insensitive work — risk calc, Monte Carlo, backtesting, compilation, general housekeeping — benefit from HT's extra hardware threads (more aggregate work). Keep HT *on* there.
- **The wrinkle: HT is usually a whole-CPU/BIOS setting.** You typically can't enable HT on some cores and disable it on others within one CPU — it's a BIOS toggle for the whole socket (Ch. 6). So the real choices are: **(a)** HT *off* for the whole box (simplest, best for latency, sacrifices throughput-core hardware threads); **(b)** HT *on* for the whole box, with the latency cores' **siblings left idle + isolated** (§43.4.2) — getting most of the latency benefit on hot cores while keeping HT for throughput cores. Many shops pick (b) on a mixed box, or (a) on a pure latency box. (Linux *can* offline individual siblings at runtime — `echo 0 > /sys/devices/system/cpu/cpuN/online` — a per-sibling "soft HT-off" without a reboot, a useful middle path.)
- **Decide by measuring (§43.3) — and security (Ch. 6, 72).** Measure the idle-vs-busy-sibling gap for *your* hot workload to quantify what HT costs you. Also weigh security: SMT has been the vector for several side-channel attacks (L1TF, MDS, the cross-sibling leaks), so some environments disable HT for **security** regardless of latency (Ch. 6, 72) — a reason that compounds with the latency case on a hot box.
- **Offlining siblings at runtime.** `echo 0 > /sys/devices/system/cpu/cpuN/online` takes a logical CPU offline without a reboot — a flexible way to "disable HT" on just the hot cores' siblings (an offlined sibling sits halted, so the partitioned resources recombine — §43.2). Handy for testing the HT-on/off trade live and for per-core control when BIOS is whole-socket.

## 43.5 Pitfalls & anti-patterns: co-scheduling two hot threads on one core

- **Two hot threads on sibling logical CPUs (the cardinal SMT sin).** Pinning two latency-critical threads to logical CPUs that are *siblings* (same physical core) makes them **fight** for the same ports/cache/predictors — each runs ~half-speed and jittery (§43.3), the worst of both worlds. **Always pin hot threads to separate *physical* cores** (check `thread_siblings_list` — §43.4.1); two hot threads on one core is a self-inflicted ~2× latency hit.
- **Pinning the hot thread but ignoring the sibling.** A pinned hot thread whose sibling runs *anything* busy (another app, a kernel thread, an IRQ, a housekeeping task the OS placed there) suffers the §43.3 interference. Pinning the hot logical CPU is only half — **manage the sibling** (idle + isolated, or light-only — §43.4.2).
- **Assuming the sibling numbering (`+N`).** Hard-coding "CPU 2's sibling is CPU 34" without reading `thread_siblings_list` breaks on different hardware/BIOS layouts → you "idle" the wrong CPU and leave the real sibling busy. **Read the topology** (§43.4.1).
- **Leaving HT on for a pure latency box.** If the box does *only* latency-critical work (no throughput jobs that benefit from HT), leaving HT on just risks the OS (or an IRQ) waking a sibling and re-partitioning the core (§43.2) — **turn HT off** for the cleanest latency. HT's benefit requires throughput work to exploit it.
- **IRQs on the sibling (Ch. 42).** Routing IRQs off the hot logical CPU but *not* its sibling still pollutes the shared core's caches. Route IRQs off **both** logical CPUs of every hot physical core.
- **Forgetting AVX downclocking is core-shared (Ch. 29).** Heavy AVX-512 on the sibling can *downclock the whole physical core* (Ch. 29), slowing the hot thread even if the sibling isn't otherwise contending. Another reason to keep the sibling quiet (or AVX-free) on a hot core.
- **Believing "sibling idle" == "HT off."** Sibling-idle recombines the partitioned store buffer/ROB only while the sibling is *halted* (§43.2) — a spinning sibling (`idle=poll`, a polling thread) keeps it halved — and it risks the OS scheduling something there if not isolated (Ch. 45). For the most sensitive threads, prefer **HT off / sibling offlined**; if leaving it idle, **isolate** it.
- **Ignoring SMT security implications (Ch. 6, 72).** SMT side-channels (MDS/L1TF) can leak across siblings; in a multi-tenant or security-sensitive context, disabling HT is a *security* requirement that also happens to help latency. Don't enable HT on sensitive cores without considering this.
- **Not measuring per workload.** SMT interference varies *hugely* by what the hot thread does (port-bound suffers most; some memory-bound work is neutral or helped). Concluding "HT is bad/fine" without measuring *your* hot thread (§43.3) gets the trade wrong. Measure the idle-vs-busy-sibling gap.

## 43.6 Exercises & checklist

**Exercises**

1. **Find the siblings.** On your box, read `/sys/devices/system/cpu/cpu*/topology/thread_siblings_list` and `lscpu -e`; map logical CPUs → physical cores → siblings. Is the numbering `+N` or something else? (§43.4.1) Confirm before pinning.
2. **Measure interference.** Build `smt.cpp` + a sibling hog; measure the hot thread's per-iter cycles (p50/p99.9) with the sibling **idle** vs **busy** (pin the hog to the sibling). Quantify the SMT tax for this workload (§43.3). Then try a *different* hot workload (a long dependency chain — Ch. 11, vs a memory-streaming loop) — does the interference differ?
3. **Sibling-idle vs HT-off.** Measure the same hot thread (a) sibling idle (HT on), (b) sibling *offlined* (`echo 0 > .../cpuN/online`), (c) HT off in BIOS. Compare p50 and the tail. Quantify the residual idle→off gap (§43.2) — then repeat (a) booted with `idle=poll` to see the partition cost of a sibling that spins instead of halting.
4. **The cardinal sin.** Pin two copies of the hot workload to *sibling* logical CPUs (same physical core) vs two *separate* physical cores. Measure each thread's latency. Confirm the ~2× hit from co-scheduling on one core (§43.5).
5. **AVX-512 sibling downclock.** Run a heavy AVX-512 loop (Ch. 29) on the sibling and a scalar latency task on the hot logical CPU; measure the scalar task's latency/frequency. Confirm the sibling's AVX downclocks the whole core (§43.5, Ch. 29).

**Checklist — SMT / hyperthreading**

- [ ] I **detected sibling topology** (`thread_siblings_list`/`lscpu -e`/hwloc) at startup — **not assumed `+N`** — and pin hot threads to separate **physical** cores (Ch. 42).
- [ ] **No two hot/latency-critical threads share a physical core** (the cardinal sin — §43.5); hot threads each own a physical core.
- [ ] Each hot core's **sibling is managed**: left **idle + isolated** (Ch. 45), or **offlined**, or HT **disabled** — and runs **no busy/latency-critical work, no IRQs** (Ch. 42), and ideally no heavy AVX (Ch. 29).
- [ ] The **HT on/off decision is made per workload type**: **off / sibling-halted** for latency cores (the whole core, nothing to re-partition it — lowest/flattest latency), **on** for throughput cores (extra hardware threads) — measured (§43.3), not assumed.
- [ ] For the most sensitive threads I prefer **HT off / sibling offlined** over sibling-idle (no sibling wake-ups to re-partition the store buffer/ROB — §43.2).
- [ ] On a **pure latency box**, HT is **off** (no throughput work to exploit it); on a mixed box, HT on with hot siblings idle/offlined.
- [ ] **Security** implications of SMT (MDS/L1TF side-channels — Ch. 6, 72) are considered in the HT decision.
- [ ] I **measured the idle-vs-busy-sibling gap** for the actual hot workload (§43.3) — the SMT tax varies hugely by what the thread does.

## 43.7 References

- Intel *SDM* / *Optimization Reference Manual* (Hyper-Threading: shared vs partitioned resources) and Agner Fog's microarchitecture guide — exactly which resources siblings share vs partition (§43.2).
- The Linux `Documentation/admin-guide/` (CPU topology, `thread_siblings_list`, CPU hotplug/online) and `lscpu(1)`/`hwloc`/`lstopo` docs — sibling detection and offlining (§43.4.1, §43.4.3).
- Intel's L1TF/MDS advisories and the Linux SMT-mitigation documentation (`/sys/devices/system/cpu/smt/`) — the security case for disabling HT (§43.4.3, ties Ch. 6, 72).
- Red Hat / kernel low-latency tuning guides — SMT and the latency-vs-throughput trade-off on isolated cores (consolidated in Appendix C).
- Carl Cook / HFT latency talks — why hot cores run with siblings idle or HT disabled.

## 43.8 Additional Reading

- D. Bakhvalov, *Performance Analysis and Tuning on Modern CPUs* — measuring SMT interference and the shared-resource model.
- The `cyclictest`/rt-tests and `turbostat` documentation — measuring jitter and frequency effects (incl. the AVX-downclock sibling interaction — Ch. 29).
- Ch. 42 (*Pinning*) — pinning to the right logical CPU and handling the sibling; Ch. 45 (*RT Scheduling*) — isolating the sibling; Ch. 11–12 (*Pipelines / Front-End*) — the ports/front-end siblings share; Ch. 7 (*Caches*) — the shared L1/L2; Ch. 29 (*SIMD*) — AVX downclocking is core-shared; Ch. 6 (*System Setup*) — BIOS HT toggle; Ch. 72 (*Security*) — SMT side-channels.
- **Appendix A** — SMT differences on ARM (big.LITTLE / heterogeneous cores, fewer SMT designs); **Appendix C** — the HT/SMT tuning decision in the checklist.

---

*Next: Ch. 44 — Cache Allocation Technology & Intel RDT, the isolation step beyond pinning: partitioning the shared last-level cache so a noisy neighbor can't evict the hot working set even when the core is dedicated — the cache half of a quiet core.*
