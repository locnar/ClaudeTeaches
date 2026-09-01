# Part VII — OS, Scheduling & Isolation

# Chapter 42 — Thread & Interrupt Pinning

> **Prerequisites:** Ch. 41 (context switching — migration and interrupts are the switches pinning eliminates), Ch. 7/12/15 (caches/I-cache/TLB — what stays warm when the thread stays put), Ch. 16 (NUMA — pin to the right node), Ch. 6 (system setup — topology discovery), Ch. 1 (jitter/tail).
>
> **Leads into:** Ch. 43 (SMT — which logical CPU you pin to, and the sibling), Ch. 45 (RT scheduling / `isolcpus`/`nohz_full` — the isolation that makes pinning effective), Ch. 55 (NIC IRQs and RSS — the interrupts to route). Consolidated in Appendix C. The affinity here is what Ch. 16/31/41 assumed.

---

## 42.1 Why it matters: own the core, evict the noise

Chapter 41 established *what* to eliminate — migrations, preemptions, interrupts — and this chapter is *how*: **pinning**. CPU affinity binds a thread to a specific core so the scheduler can't migrate it (keeping its working set warm — Ch. 7, 12, 15, and NUMA-local — Ch. 16); IRQ affinity routes hardware interrupts *away* from that core so nothing steals its cycles or pollutes its caches. Together they implement the core idea of Part VII: **the hot thread *owns* its core, and everything else — other threads, the timer tick, NIC interrupts, kernel work — is evicted from it.** A pinned thread on a cleaned core runs continuously, warm, and uninterrupted; an unpinned thread on a noisy core gets migrated, preempted, and interrupted — each event a cache-trashing latency spike (Ch. 41) that lands in the tail (Ch. 1).

Pinning is the *single highest-leverage* OS-level intervention for latency, and it's two-sided in a way people often half-do. The thread side (`taskset`/`sched_setaffinity`) is well-known: bind the feed handler to core 2, the strategy to core 3. But the **interrupt side** is the one that's frequently missed and just as important: by default, the OS routes hardware interrupts (especially the NIC's — Ch. 55) across all cores, and `irqbalance` actively shuffles them around. If the NIC IRQ that fires on *every* incoming packet lands on your hot core, then every market-data packet *interrupts your hot thread* — handling the IRQ steals cycles and evicts the thread's caches (Ch. 41) at the worst possible moment. Pinning the thread but leaving interrupts on its core is like soundproofing a room but leaving the door open. The full discipline is **both**: pin the hot thread to a core, *and* route all IRQs (and `irqbalance`, and kernel threads, and everything else) *off* that core onto dedicated **housekeeping** cores.

This chapter is the concrete mechanics — `taskset`/`sched_setaffinity`/`pthread_setaffinity_np` for threads, `/proc/irq/*/smp_affinity` for interrupts, disabling `irqbalance`, and the topology discovery (Ch. 6) you need to pin *correctly* (the right physical core, the right NUMA node — Ch. 16, the right SMT sibling — Ch. 43). It measures the jitter difference pinning makes (§42.3), gives the pin-threads and route-IRQs techniques (§42.4), and warns about the most common failure — **leftover IRQs and kernel threads on the "isolated" hot core** that silently reintroduce the jitter you pinned to remove (§42.5). Pinning is necessary but not *sufficient* on its own — it pairs with core isolation (`isolcpus`/`nohz_full` — Ch. 45) to actually deliver a quiet core — but it's the foundation, and the rest of Part VII builds on getting it right.

## 42.2 Mental model: CPU affinity and IRQ routing

**CPU affinity — binding a thread to a core.** The OS scheduler, by default, runs a thread on whatever core is convenient and **migrates** it freely (for load-balancing, when a core frees up, etc.). **Affinity** constrains the set of cores a thread may run on; pin it to *one* core and it can never migrate:

```
   without affinity:  thread bounces across cores → cold every time (Ch. 39), NUMA-random (Ch. 15)
   with affinity (pinned to core 2):  thread ALWAYS runs on core 2 → warm caches/TLB, NUMA-stable

   the mechanisms:
     taskset -c 2 ./app                       (launch pinned, command-line)
     sched_setaffinity(tid, &cpuset)          (syscall — pin self/another thread)
     pthread_setaffinity_np(thr, ...)         (per-pthread)
     cpuset cgroups                           (confine a whole group of threads to a core set)
```

Pinning the thread is necessary but, alone, only stops *migration* — the core can still be **preempted** (other threads scheduled there) and **interrupted** (IRQs). To get a *quiet* core you also need isolation (Ch. 45) and IRQ routing (below).

**Topology — pin to the *right* logical CPU.** "Core 2" is ambiguous: modern CPUs have **logical CPUs** (the things you pin to) that map onto **physical cores** via SMT (Ch. 43 — two logical CPUs per physical core) and onto **NUMA nodes** (Ch. 16). You must discover the topology (`lscpu`, `/proc/cpuinfo`, `lstopo`/hwloc, `/sys/devices/system/cpu/`) and pin deliberately:

- to a logical CPU on the **right NUMA node** (the node holding the thread's data and its NIC — Ch. 16, 55),
- accounting for the **SMT sibling** (Ch. 43 — pinning to logical CPU 2 means its sibling, sharing the physical core, must be left idle or used only for housekeeping, or HT disabled),
- avoiding **core 0** (the OS/boot core, where lots of kernel housekeeping lands by default).

Pinning to the wrong logical CPU (the SMT sibling of a busy core, the wrong NUMA node) gives you contention or remote-memory penalties you thought you avoided.

**IRQ routing — moving interrupts off the hot core.** A hardware interrupt (NIC packet arrival — Ch. 55, timer, disk, IPI) is handled by a CPU, stealing its cycles and polluting its caches (Ch. 41). Each IRQ has an **affinity mask** (`/proc/irq/N/smp_affinity`) controlling which CPUs may handle it. By default IRQs spread across all cores and **`irqbalance`** (a daemon) actively migrates them for "balance" — exactly wrong for a latency box. The discipline:

```
   - DISABLE irqbalance (systemctl stop/disable irqbalance) — stop it shuffling IRQs onto hot cores
   - set every IRQ's smp_affinity to HOUSEKEEPING cores only (never the hot cores)
   - especially the NIC IRQs (Ch. 49): route them to housekeeping cores, or use RSS/affinity so the
     queue feeding the hot path is steered appropriately (Ch. 49)
   - the hot core handles NO device IRQs; combined with nohz_full (Ch. 42) it takes no timer IRQ either
```

**Housekeeping vs hot cores — the partition.** The clean design splits the CPU into **housekeeping cores** (run the OS, kernel threads, IRQs, `irqbalance`-equivalent work, control/admin/logging threads — Ch. 41's blocking threads) and **hot/isolated cores** (run *only* the pinned busy-polling trading threads, no IRQs, no kernel work, no timer tick). Everything noisy goes on housekeeping; the hot cores are pristine. This partition is the heart of a low-latency box (Ch. 6, 45, Appendix C).

The model: **affinity pins a thread to a logical CPU (no migration → warm, NUMA-stable); but a quiet core also needs IRQs routed away (disable `irqbalance`, set `smp_affinity` to housekeeping cores) and isolation (Ch. 45). Pin the hot thread to the right logical CPU (correct NUMA node, SMT sibling handled, not core 0), and evict every interrupt and kernel task onto dedicated housekeeping cores.**

## 42.3 Measure it: jitter with/without pinning

Pinning's payoff is **jitter reduction** — the tail (Ch. 1), not the mean. Measure the latency *distribution* of a tight loop (or a `cyclictest`-style timer-wakeup, Ch. 45) under three conditions: unpinned on a noisy system, pinned to a normal core, pinned to an isolated+IRQ-cleaned core.

```cpp
// jitter.cpp — measure loop-iteration jitter; pin via taskset and observe the tail shrink.
// Build: g++ -O2 -std=c++20 -march=native jitter.cpp -o jitter
// Run:   ./jitter                        (unpinned — scheduler may migrate/preempt)
//        taskset -c 2 ./jitter           (pinned to core 2)
//        taskset -c 2 ./jitter           (with core 2 isolated: isolcpus/nohz_full + IRQs routed away — Ch.42)
#include <cstdio>
#include <cstdint>
#include <vector>
#include <algorithm>
#include <chrono>
#include <x86intrin.h>

int main() {
    constexpr long N = 50'000'000;
    std::vector<std::uint32_t> samples; samples.reserve(N);
    std::uint64_t prev = __rdtsc();
    for (long i = 0; i < N; ++i) {
        std::uint64_t now = __rdtsc();
        samples.push_back(std::uint32_t(now - prev));   // cycles for this iteration
        prev = now;
        // a tiny fixed amount of work would go here; the iteration time SHOULD be constant.
    }
    std::sort(samples.begin(), samples.end());
    auto pct = [&](double p){ return samples[std::size_t(p * (N - 1))]; };
    std::printf("p50=%u  p99=%u  p99.9=%u  p99.99=%u  max=%u cycles\n",
                pct(0.5), pct(0.99), pct(0.999), pct(0.9999), samples.back());
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP), turbo off (illustrative; the *tail* is the point — convert cycles to ns at ~2.9 GHz):

```
                                   p50    p99    p99.9    p99.99    max        what's in the tail
   unpinned (noisy system)        ~20    ~80    ~2,500   ~30,000   ~120,000   migrations, preemption, IRQs (Ch.39)
   pinned to a normal core        ~20    ~30    ~400     ~8,000    ~40,000    fewer migrations, but timer tick + IRQs remain
   pinned + isolated + IRQs away  ~20    ~22    ~28      ~60       ~300       quiet core: tail nearly GONE
```

Read it the Ch. 1 way: **the median (p50) barely changes — pinning isn't about the mean — but the *tail* collapses by orders of magnitude.** Unpinned, the p99.99/max are *huge* (tens of thousands of cycles) because the loop is occasionally **migrated** (cold restart — Ch. 41), **preempted** (another thread ran), or **interrupted** (a timer tick or IRQ stole the core). Pinning removes migrations and shrinks the tail; but a *normal* pinned core still takes the **timer tick** and **device IRQs**, so spikes remain. Only when the core is **isolated** (`isolcpus`/`nohz_full` — Ch. 45) *and* **IRQs are routed away** (this chapter) does the tail nearly vanish — the loop iteration time becomes the constant it should be. This is the headline result of Part VII: **a quiet core has a flat latency distribution; a noisy core has a long, ugly tail — and pinning + IRQ routing + isolation is what makes the core quiet.** Verify with `perf stat -e cpu-migrations,context-switches` (→ 0) and by checking `/proc/interrupts` shows ~no interrupts accumulating on the hot core during a run.

## 42.4 Techniques

### 42.4.1 `taskset`/`sched_setaffinity`

Pinning threads — the mechanisms and the discipline:

- **`taskset` (launch-time / external).** `taskset -c 2 ./app` launches pinned to CPU 2; `taskset -c 2-3 ./app` to a set; `taskset -p MASK PID` repins a running process. Quick for whole-process pinning, scripts, and testing. For per-*thread* control within a multithreaded process, use the API.
- **`sched_setaffinity(2)` / `pthread_setaffinity_np` (programmatic, per-thread).** The trading app pins *each* hot thread to its *specific* core from inside the program (during thread setup): build a `cpu_set_t`, `CPU_SET(core, &set)`, call `pthread_setaffinity_np(thread, ...)`. This is the production approach — the app *knows* its topology and pins the feed handler to core 2, strategy to core 3, etc. deterministically. Do it in the thread's startup (alongside FTZ/DAZ — Ch. 27, warming — Ch. 46).
- **Pin to the *right* logical CPU (topology — §42.2).** Discover topology first (`lscpu -e`, `lstopo`, `/sys/devices/system/cpu/cpu*/topology/`): pin to a logical CPU on the **NUMA node** holding the thread's data and NIC (Ch. 16, 55), handle the **SMT sibling** (Ch. 43 — leave it idle or housekeeping-only), and **avoid core 0**. Hard-coding "core 2" without checking topology can land you on the wrong node or a busy sibling.
- **`cpuset` cgroups (confinement).** A `cpuset` cgroup confines a *group* of threads/processes to a core set (and can move *all other* tasks off the hot cores — the inverse pin). Useful to corral everything-not-hot onto housekeeping cores, and to enforce the partition system-wide. Pairs with `isolcpus` (Ch. 45).
- **Verify it stuck.** Check `/proc/<pid>/task/<tid>/status` (`Cpus_allowed`), `taskset -p`, or `perf stat -e cpu-migrations` (→ 0). A pin that didn't apply (wrong tid, overridden by cgroup) silently leaves the thread migratable.

### 42.4.2 IRQ affinity and isolating the hot core

Routing interrupts away — the half people miss (§42.1, §42.2):

- **Disable `irqbalance`.** `systemctl stop irqbalance && systemctl disable irqbalance` — stop the daemon that actively shuffles IRQs onto (potentially hot) cores. On a latency box, IRQ placement is *manual and fixed*, not balanced.
- **Set each IRQ's `smp_affinity` to housekeeping cores.** Write a housekeeping-core mask to `/proc/irq/<N>/smp_affinity` (or `smp_affinity_list`) for every IRQ — so no device interrupt is ever handled on a hot core. Script it at boot (the IRQ numbers come from `/proc/interrupts`). The default (all cores) lets IRQs land on hot cores.
- **NIC IRQs specifically (Ch. 55).** The NIC fires an IRQ per packet-batch (the highest-rate, most hot-path-relevant interrupt). Route NIC IRQs to housekeeping cores; with **RSS** (multiple RX queues — Ch. 55) steer the queue feeding the strategy and pin *its* IRQ appropriately, or (with busy-polling/kernel-bypass — Ch. 55, 62) take the NIC out of the interrupt path entirely on the hot core.
- **Move kernel threads and RCU callbacks off (Ch. 45).** Beyond IRQs, kernel work (`kworker`, RCU callbacks, the timer tick) lands on cores; `isolcpus`/`nohz_full`/`rcu_nocbs` (Ch. 45) and `cpuset` keep the hot core free of these. Pinning the *user* thread without isolating the core leaves kernel noise (§42.5).
- **The housekeeping/hot partition (§42.2).** Concretely: reserve cores 0-1 (and the SMT siblings — Ch. 43) as housekeeping (OS, IRQs, `irqbalance`-disabled, control threads); isolate cores 2-N as hot (pinned trading threads, no IRQs, `nohz_full`). This partition, applied at boot (kernel cmdline — Ch. 45, Appendix C) plus runtime (IRQ affinity, `cpuset`), is the standard low-latency configuration.
- **Verify the core is quiet.** Run the workload and check `/proc/interrupts` (the hot core's columns should barely increment), `perf stat -e cpu-migrations,context-switches` (→ 0 on the hot thread), and the jitter distribution (§42.3). If interrupts still hit the hot core, find and reroute them (a missed IRQ, a re-enabled `irqbalance`, an MSI-X vector you didn't catch).

## 42.5 Pitfalls & anti-patterns: leftover IRQs on the hot core

- **Pinning the thread but leaving IRQs on the core (the half-done job).** The most common mistake: `taskset` the hot thread but never touch IRQ affinity → the NIC and timer interrupts still fire on the hot core, stealing cycles and trashing caches on *every packet* (§42.1, §42.3). **Route IRQs away** (disable `irqbalance`, set `smp_affinity`) — pinning is two-sided.
- **`irqbalance` re-shuffling IRQs back.** Leaving `irqbalance` running undoes your manual IRQ affinity (it migrates IRQs for "balance," possibly onto hot cores). **Disable it** on a latency box (§42.4.2); manual placement only.
- **Pinning to the wrong logical CPU (topology blindness).** Hard-coding a CPU number without checking topology → landing on the wrong **NUMA node** (remote memory — Ch. 16), the **SMT sibling** of a busy core (resource contention — Ch. 43), or **core 0** (OS noise). Discover topology (`lscpu`/`lstopo`) and pin deliberately (§42.4.1).
- **Pinning without isolating the core (Ch. 45).** Affinity stops *migration* but not *preemption* or *kernel noise* — other threads can still be scheduled on a pinned-but-not-isolated core, and kernel threads/timer ticks still run there. Pair pinning with **`isolcpus`/`nohz_full`** (Ch. 45); pinning alone leaves a noisy core (§42.3 "normal core" row).
- **Forgetting the SMT sibling (Ch. 43).** Pinning the hot thread to logical CPU 2 while *another* (housekeeping or hot) thread runs on its sibling (logical CPU 2's pair, sharing the physical core) means the sibling steals execution resources (Ch. 43). Leave the sibling idle, put only light housekeeping on it, or disable SMT — a deliberate choice (Ch. 43).
- **Oversubscribing housekeeping cores.** Cramming all non-hot work (OS, IRQs, control, logging, *and* the SMT siblings' housekeeping) onto too few housekeeping cores makes *them* the bottleneck (and they can still IPI the hot cores). Budget enough housekeeping capacity.
- **Pin not actually applied.** A `sched_setaffinity` on the wrong tid, overridden by a `cpuset` cgroup, or a thread created *after* the pin call — the pin silently doesn't take. **Verify** (`Cpus_allowed`, `cpu-migrations == 0` — §42.4.1).
- **Stray IPIs and unmovable interrupts.** Some interrupts/IPIs (TLB shootdowns, function-call IPIs, certain per-CPU timers) are hard to fully eliminate from a core. `nohz_full`/`isolcpus` (Ch. 45) and minimizing cross-core kernel activity reduce them; a perfectly silent core takes effort — measure `/proc/interrupts` to find the stragglers.
- **Migrating the pinned thread by accident.** A child thread/process inheriting affinity you didn't intend, or a library spawning helper threads on hot cores. Audit *all* threads' affinities (not just the main hot one); confine everything-not-hot to housekeeping via `cpuset`.

## 42.6 Exercises & checklist

**Exercises**

1. **Jitter, three ways.** Build `jitter.cpp`; run unpinned, `taskset -c 2`, and on an isolated+IRQ-cleaned core 2 (Ch. 45). Compare p99.9/p99.99/max (the tail). Confirm the median barely moves but the tail collapses (§42.3). Convert cycles to ns.
2. **Catch the IRQ.** With the hot thread pinned to core 2 but IRQs *not* routed, watch `/proc/interrupts` (core 2's column) and the jitter while generating network traffic (the NIC IRQ — Ch. 55). Then route NIC IRQs to a housekeeping core and disable `irqbalance`; re-measure. Quantify the IRQ-on-hot-core cost (§42.4.2).
3. **Topology pinning.** Use `lscpu -e`/`lstopo` to map logical CPUs → physical cores → NUMA nodes → SMT siblings. Pin a memory-bound thread to a logical CPU on the *wrong* NUMA node vs the right one (Ch. 16) and measure. Pin two threads to SMT siblings vs separate cores (Ch. 43) and measure interference.
4. **Verify the pin.** Pin a thread, then check `/proc/<pid>/task/<tid>/status` `Cpus_allowed` and `perf stat -e cpu-migrations`. Now create a thread *before* pinning self and show its affinity differs. Confirm a `cpuset` cgroup can override your `sched_setaffinity` (§42.5).
5. **Build the partition.** Configure a box: housekeeping cores 0-1, hot cores 2-3; `cpuset` everything-not-hot onto 0-1, route all IRQs to 0-1, disable `irqbalance`, pin trading threads to 2-3. Verify `/proc/interrupts` and `cpu-migrations` show the hot cores are quiet.

**Checklist — thread & interrupt pinning**

- [ ] Every hot thread is **pinned** (`sched_setaffinity`/`pthread_setaffinity_np`, in thread startup) to a **specific logical CPU** chosen by **topology** (right NUMA node — Ch. 16, SMT sibling handled — Ch. 43, not core 0).
- [ ] **`irqbalance` is disabled**, and **every IRQ's `smp_affinity`** (especially the **NIC IRQs** — Ch. 55) is set to **housekeeping cores** — no device interrupt hits a hot core (§42.4.2).
- [ ] Pinning is paired with **core isolation** (`isolcpus`/`nohz_full`/`rcu_nocbs` — Ch. 45) and a **`cpuset`** that confines everything-not-hot to housekeeping cores — a quiet core, not just a non-migrating thread.
- [ ] The system is partitioned into **housekeeping** (OS, IRQs, control/logging/blocking threads — Ch. 41) and **hot/isolated** cores (only pinned busy-polling trading threads).
- [ ] I **verified** the pin took (`Cpus_allowed`, `cpu-migrations == 0`) and the core is **quiet** (`/proc/interrupts` barely increments on hot cores; jitter tail collapsed — §42.3) — not assumed.
- [ ] The **SMT sibling** of each hot core is left idle / housekeeping-only / SMT disabled (a deliberate Ch. 43 choice) — no sibling stealing resources.
- [ ] **All** threads (including library/child threads) have audited affinities — none accidentally land on hot cores; housekeeping cores aren't oversubscribed.
- [ ] Stray IPIs/unmovable interrupts are minimized (`nohz_full` — Ch. 45) and **measured** (`/proc/interrupts`); the config is applied at **boot + runtime** (Appendix C).

## 42.7 References

- The Linux man pages — `sched_setaffinity(2)`, `pthread_setaffinity_np(3)`, `taskset(1)`, `cpuset(7)`, and `Documentation/IRQ-affinity.txt` / `/proc/irq/*/smp_affinity` — the mechanisms of §42.4.
- The `hwloc`/`lstopo`, `lscpu`, and `/sys/devices/system/cpu/` documentation — topology discovery for correct pinning (§42.2, §42.4.1; ties Ch. 6, 16, 43).
- Red Hat *Low Latency Performance Tuning* and the kernel low-latency/RT documentation — the housekeeping/hot partition, IRQ routing, and `irqbalance` guidance (§42.4, consolidated in Appendix C).
- The NIC vendor (Mellanox/Intel/Solarflare) tuning guides — NIC IRQ affinity, RSS, and steering for low latency (§42.4.2, ties Ch. 55).
- Carl Cook, *"When a Microsecond Is an Eternity"* — pinning and core isolation as the foundation of the HFT hot path.

## 42.8 Additional Reading

- The `cyclictest`/rt-tests and `tuned`/`tuned-adm` (low-latency profiles) documentation — measuring jitter and applying pinning/IRQ/isolation profiles (ties Ch. 45).
- Jon Masters / kernel low-latency talks and the `nohz_full` documentation — eliminating per-CPU kernel noise.
- Ch. 41 (*Context Switching*) — the migrations/preemptions/interrupts pinning eliminates; Ch. 43 (*SMT*) — the sibling of the pinned core; Ch. 45 (*RT Scheduling*) — `isolcpus`/`nohz_full`/RT priority/`cyclictest` that make pinning effective; Ch. 16 (*NUMA*) — pin to the right node; Ch. 55 (*NIC*) — NIC IRQs/RSS to route; Ch. 6 (*System Setup*) — topology and BIOS.
- **Appendix C** (System Tuning Checklist) — the consolidated pinning/IRQ/isolation recipe; **Appendix E** — the jitter/interrupt-cost numbers.

---

*Next: Ch. 43 — SMT / Hyperthreading, which confronts the question pinning raised: when you pin to a logical CPU, what about its sibling? — how two hardware threads share one physical core's resources, the throughput-vs-latency trade-off, and why HFT hot cores usually run with the sibling idle or HT disabled.*
