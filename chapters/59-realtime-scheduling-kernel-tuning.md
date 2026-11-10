# Part VII — OS, Scheduling & Isolation

# Chapter 59 — Real-Time Scheduling & Kernel Tuning

> **Prerequisites:** Ch. 54 (context switching — preemption/interrupts are the jitter this eliminates), Ch. 56 (pinning — isolation makes the pin effective), Ch. 57 (SMT — isolate the sibling too), Ch. 58 (cache partitioning — the shared-L3 half of isolation, which this chapter's kernel knobs complete), Ch. 6 (BIOS/firmware tuning — the layer below the kernel), Ch. 1 (jitter/tail — the metric).
>
> **Leads into:** Ch. 62 (warming — once the core is quiet, keep it warm), Ch. 75–77, 81, 82 (NIC / DDIO / flow steering / bypass — the I/O on the isolated cores), Appendix C (the consolidated checklist this chapter feeds). This is the kernel configuration that delivers the "quiet core" Ch. 56–58 assumed.

---

## 59.1 Why it matters: eliminating scheduler-induced jitter

Chapters 56–57 pinned the hot thread to a core and handled its SMT sibling — but a pinned thread on a stock-configured core is *still* not quiet. The Linux kernel, by default, does a steady stream of per-core housekeeping that interrupts *any* thread on *any* core: the **periodic timer tick** (the scheduler's heartbeat, historically 100-1000 Hz — an interrupt every 1-10 ms on every core), **RCU callbacks** (Ch. 47) processed per-CPU, **kernel worker threads** (`kworker`), scheduler load-balancing, and the **fair-class scheduler** itself (CFS, replaced by EEVDF in Linux 6.6 — the `SCHED_OTHER` policy either way) which will happily preempt your thread to run something else (or nothing else, but still tick). Each of these is a context switch or interrupt (Ch. 54) — a cache-trashing latency spike that lands in the tail (Ch. 1). Pinning stops *migration*; it does not stop the kernel from doing its periodic work *on the pinned core*. To get a genuinely **jitter-free** core you must tell the kernel to *leave that core alone* — and that's kernel tuning: `isolcpus`, `nohz_full`, `rcu_nocbs`, RT scheduling, and cgroups.

This is the layer that turns "pinned" into "quiet," and it's mostly **boot-time kernel configuration** (the kernel command line) plus runtime knobs — the most impactful and most often half-done part of building a low-latency box. `isolcpus` removes cores from the scheduler's general pool so nothing is load-balanced onto them. **`nohz_full`** makes cores **tickless** — when only one thread runs on the core (your pinned hot thread), the kernel *stops the periodic timer interrupt* entirely, so the hot loop runs with *no* per-millisecond tick stealing cycles and polluting caches. **`rcu_nocbs`** offloads RCU callback processing off the isolated cores onto housekeeping cores. **RT scheduling** (`SCHED_FIFO`/`SCHED_RR` with high priority) ensures that if anything *does* become runnable on the core, your thread wins and isn't preempted by lower-priority work. Together these eliminate the scheduler- and timer-induced jitter that pinning alone leaves behind — taking the per-core interrupt rate from ~1000/sec down to ~0 during steady-state trading.

The discipline, and the reason this chapter exists as its own thing, is that **jitter is measured, not assumed** — and the standard instrument is **`cyclictest`** (from `rt-tests`), which measures the *latency between when a timer should fire and when a thread actually wakes*, giving you the scheduling-jitter distribution (max latency is the number that matters — Ch. 1's tail). A well-tuned isolated core shows `cyclictest` max latencies in the *single-digit microseconds* or below; a stock core shows *hundreds of microseconds to milliseconds*. You tune, you `cyclictest`, you find the remaining jitter source (a stray IRQ — Ch. 56, an un-offloaded kernel thread, a missed `nohz_full`), you fix it, you re-measure — until the tail is flat. This chapter covers the `isolcpus`/`nohz_full`/RT/cgroup mental model (§59.2), the `cyclictest` before/after measurement (§59.3), the isolation-and-RT techniques (§59.4), and the pitfalls — RT throttling (which can *stall* a busy-polling RT thread), priority inversion, and the over-tuning that breaks the system (§59.5). It's the capstone of the OS-isolation arc (Ch. 54–58) and the bulk of Appendix C.

## 59.2 Mental model: `isolcpus`, `nohz_full`, RT priorities, cgroups

**The default kernel is busy on every core.** Even with nothing of yours scheduled, a stock core takes the **timer tick** (the scheduler interrupt, `CONFIG_HZ` times/sec), runs **RCU callbacks** (Ch. 47), hosts **kernel threads** and **`kworker`** items, and is subject to **scheduler load-balancing** (the fair-class scheduler — CFS/EEVDF — moving tasks around). The tuning goal: make the hot cores do *none* of this.

**The kernel command-line isolation knobs (boot-time — `/etc/default/grub` → reboot):**

```
   isolcpus=managed_irq,domain,2-7
                       remove cores 2-7 from the general scheduler's load-balancing pool (the `domain` flag,
                       the default) → nothing is auto-scheduled there; only explicitly-pinned tasks run (Ch. 56)
                       `managed_irq`: keep driver-MANAGED interrupts (multi-queue NIC/NVMe vectors, which ignore
                       irqaffinity= and refuse /proc/irq writes — Ch. 56) off these cores (Linux 5.6+)
                       `nohz`: equivalent to nohz_full= for the same cores (then also affine the residual-tick
                       workqueues: /sys/devices/virtual/workqueue/cpumask)
   nohz_full=2-7       make cores 2-7 TICKLESS when ≤1 task runs on them
                       → NO periodic timer interrupt → the hot loop runs uninterrupted
   rcu_nocbs=2-7       offload RCU callback processing off cores 2-7 onto housekeeping cores
                       → no RCU softirq work on the hot cores
   irqaffinity=0,1     default IRQ affinity = housekeeping cores only (ties Ch. 56)
   (+ Ch.6: mitigations=, intel_pstate=, nosmt or HT handling — Ch.57, hugepages, etc.)
```

- **`isolcpus`** — the foundation: the scheduler won't *place* tasks on these cores (so no surprise threads), and won't *load-balance* onto them. You then explicitly pin your hot threads there (Ch. 56). Its flag list matters on a trading box: `domain` (the scheduler isolation above), `nohz` (tickless, like `nohz_full`), and `managed_irq` — the only way to keep *driver-managed* NIC/NVMe interrupt vectors off the isolated cores, since those ignore `irqaffinity=` and reject `/proc/irq/*/smp_affinity` writes (Ch. 56). (Newer kernels prefer the `cpuset`/`cgroup` v2 isolation interface — `cpuset.cpus.partition=isolated` — and `isolcpus` is somewhat deprecated, but the concept is identical.)
- **`nohz_full` (the big one for jitter)** — "full dynticks": a core with `nohz_full` and **only one runnable task** stops its periodic scheduler tick entirely — *no* timer interrupt every millisecond. This is what lets a busy-polling hot thread (Ch. 54) run for milliseconds *with zero kernel interruptions*. (It requires `≤1` task on the core — a *second* task brings the tick back, because the scheduler needs the tick to arbitrate between them; hence one hot thread per isolated core, Ch. 42, 54.) `nohz_full=` *implies* RCU-callback offload for the same CPUs (as if `rcu_nocbs=` had been given); listing `rcu_nocbs=` explicitly is harmless, documents intent, and lets you offload more CPUs than you make tickless. Both need `CONFIG_NO_HZ_FULL=y` (and `CONFIG_RCU_NOCB_CPU=y`) in the kernel build — the real prerequisite.
- **`rcu_nocbs`** — moves RCU's per-CPU callback processing off the isolated cores, removing another source of per-core softirq work; pairs with `nohz_full`.

**RT scheduling classes (runtime — `sched_setscheduler`/`chrt`):**

- **`SCHED_OTHER` (the fair class — EEVDF since Linux 6.6, CFS before; the default)** — the fair scheduler; *will* preempt your thread for fairness. Not for the hot path on a contended core.
- **`SCHED_FIFO` / `SCHED_RR` (real-time)** — RT priority classes that run *ahead of* all `SCHED_OTHER` tasks; a `SCHED_FIFO` thread runs until *it* yields/blocks (FIFO) or its priority is exceeded — it is **not** preempted by normal tasks or timeslices. This guarantees the hot thread isn't preempted if anything else becomes runnable on its core. `chrt -f 80 ./app` or `sched_setscheduler(SCHED_FIFO, prio)`. On an *isolated* core there's little to preempt it anyway, but RT priority is belt-and-suspenders (and essential on shared cores and for ordering multiple threads).
- **The deadline class (`SCHED_DEADLINE`)** — EDF scheduling with explicit runtime/deadline/period; powerful for periodic RT tasks, less common for busy-polling HFT (which wants to run *continuously*, not periodically). §59.2.2 is the depth: admission control, the CBS budget and GRUB reclaiming.
- **PREEMPT_RT (mainline since Linux 6.12, `CONFIG_PREEMPT_RT`).** The fully-preemptible kernel turns IRQ handlers into threads, makes most kernel spinlocks sleeping locks, and runs `ksoftirqd`/`rcuc` at RT priority — bounding the *worst-case* latency of anything that does enter the kernel. For a trading box the trade is a slightly higher mean cost of every kernel crossing against a tighter bound; an isolated, syscall-free busy-polling core gains little from it (there is nothing to preempt), while the housekeeping and I/O cores — and any design that still takes a NIC IRQ — can. Measure with `cyclictest` on both configurations before choosing.

**cgroups — confining everything else.** cgroups (v2) `cpuset` controllers confine *non*-hot tasks to housekeeping cores (the inverse of pinning — move everything-not-hot *off* the isolated cores), and `cpu` controllers bound their resource use. This enforces the housekeeping/hot partition (Ch. 56) system-wide, including system services and anything you forgot to pin.

The model: **a stock kernel interrupts every core (tick, RCU, kworkers, fair-class (EEVDF) preemption); to get a quiet core, isolate it from the scheduler (`isolcpus`/cpuset), make it tickless (`nohz_full` + `rcu_nocbs`, with ≤1 task on it), route IRQs away (Ch. 56), and run the hot thread RT (`SCHED_FIFO`) so nothing preempts it — confining everything else to housekeeping cores with cgroups. Then verify the jitter with `cyclictest`.**

### 59.2.1 Inside the fair class: EEVDF lag, virtual deadlines and slice requests

Since Linux 6.6 the `SCHED_OTHER` class is **EEVDF** (Earliest Eligible Virtual Deadline First), and three things changed for a housekeeping core that shares work. CFS picked the runnable task with the smallest *vruntime* — the one furthest behind its fair share — and tuned the result with `sched_min_granularity_ns`/`sched_wakeup_granularity_ns`. EEVDF keeps the fair share as **lag** (how far a task is behind or ahead of its entitlement), calls a task **eligible** when its lag is ≥ 0, and among eligible tasks picks the one with the earliest **virtual deadline** = its eligible time + its **slice** ÷ weight. The slice is the new, task-settable knob: `sched_setattr` with `sched_runtime` on a `SCHED_OTHER` task *requests* a slice (clamped to `[100 µs, …]`; the default is `base_slice_ns`, 3 ms × (1 + log₂ cpus) scaled, read from `/sys/kernel/debug/sched/base_slice_ns`), and **a short slice means an early virtual deadline — a latency-sensitive task gets picked sooner and runs for less** before it is deadline-preempted; a long slice (a batch job) means fewer, longer runs. Wake-up preemption follows the same rule: a waking task with positive lag and an earlier deadline preempts the running one (`RUN_TO_PARITY`/`PREEMPT_SHORT` features in 6.11+ refine when). The old sysctls are gone (`/proc/sys/kernel/sched_min_granularity_ns` etc. no longer exist); what remains is under `/sys/kernel/debug/sched/` (`base_slice_ns`, `features`, `debug`) and per-task via `sched_setattr`.

For the *isolated* hot core none of this applies — there is one task, it is `SCHED_FIFO`, EEVDF never runs. For the **housekeeping** cores it is the tool for the telemetry sampler (Ch. 113), the logger consumer (Ch. 98) and the control plane sharing a core: give the latency-sensitive one a 100–500 µs slice request (it is picked first and never runs long) and the capture writer the default or a long one. Measured on the guest (`sched_probe.cpp`, a 1 kHz periodic thread sharing a vCPU with a spinner): wake latency p50 was 60–62 µs for default, 100 µs and 20 ms slice requests alike — the guest's timer wake-up floor is 75 µs with *no* spinner at all (Appendix S §S.2), so the experiment cannot resolve EEVDF's sub-100 µs behaviour there; on bare metal the Ch. 55 §55.3 timer-wake harness with a spinner neighbour is the measurement, and the expected effect is in the p99 (how long the spinner's remaining slice can hold the waker off: up to the *spinner's* slice minus its lag, i.e. up to ~3 ms with defaults, ~100 µs with the waker's deadline earlier).

### 59.2.2 `SCHED_DEADLINE` in depth: admission, CBS and GRUB

`SCHED_DEADLINE` is the one class with a **contract**: a task declares `sched_runtime` (CPU time it needs) per `sched_period`, due by `sched_deadline` (≤ period), and the kernel (a) refuses the task at `sched_setattr` time if the sum of runtime/period over the **root domain** would exceed `sched_rt_runtime_us / sched_rt_period_us` (95 % by default) — **admission control**, `EBUSY`; (b) runs the admitted tasks earliest-deadline-first, above every `SCHED_FIFO` priority; (c) **throttles** a task that has used its runtime until its next period — the **Constant Bandwidth Server** (CBS) — so one misbehaving task cannot take more than it declared; and (d) with `SCHED_FLAG_RECLAIM` lets a throttled task use the CPU's *unused* deadline bandwidth — **GRUB** (Greedy Reclamation of Unused Bandwidth). Four facts that bite:

- **Admission is per root domain, not per CPU.** On a box with no cpuset partitions the root domain is all CPUs, so the 95 % is of *all* of them: two tasks asking 200 µs/1 ms and 900 µs/1 ms are both admitted on a 4-CPU domain (measured below — 110 % of one CPU is 27 % of four) and will then *migrate* to meet their deadlines. To get per-core admission, make the hot core an **isolated cpuset partition** (`cpuset.cpus.partition=isolated` — §59.4.1) — it becomes its own root domain.
- **A DEADLINE task may not be pinned narrower than its root domain** — `sched_setattr` returns `EPERM` if the affinity mask is a strict subset — again unless the CPU is a partition. `EPERM` from `sched_setattr`, after `taskset`, is this, not a capability problem.
- **A DEADLINE task may not `fork`/`clone`** (`EAGAIN`): its bandwidth is not inheritable. Create helper threads *before* switching class, or from another thread.
- **CBS enforcement is as fine as the tick unless `HRTICK_DL` is on.** The runtime is charged at scheduler ticks (`CONFIG_HZ`: 4 ms at 250 Hz, 1 ms at 1000 Hz) and at context switches; a spinning task overruns its 200 µs budget by up to a tick and is then throttled for correspondingly longer. `echo HRTICK_DL > /sys/kernel/debug/sched/features` (`CONFIG_SCHED_HRTICK=y`) arms an hrtimer at the budget boundary instead.

## 59.3 Measure it: `cyclictest` before/after

The standard scheduling-jitter instrument is **`cyclictest`** (from `rt-tests`): it sleeps a thread for a fixed interval, then measures the difference between the *intended* wake time and the *actual* wake time — the **scheduling latency**, i.e. the jitter the kernel/hardware injects. The **max** latency is the number that matters (the tail — Ch. 1). Run it before and after tuning, on a stock core vs a fully-isolated core.

```
   # one command per row of the table below; "stress-ng --cpu 16" runs in the background for every row
   # row 1: stock core, SCHED_OTHER, unpinned                      (no -p, no -a)
   $ cyclictest -m -i 200 -h 400 -t 1 -D 60
   # row 2: pinned to core 5 (Ch. 56), still SCHED_OTHER             (+ -a 5)
   $ cyclictest -m -i 200 -h 400 -a 5 -t 1 -D 60
   # rows 3-5: + SCHED_FIFO 80 (+ -p 80), then the kernel-cmdline and BIOS layers change underneath it:
   #   row 3: isolcpus=5                 row 4: + nohz_full=5 rcu_nocbs=5, IRQs routed away (Ch. 56), HT sibling idle (Ch. 57)
   #   row 5: + mitigations=off, C-states capped at C1 (Ch. 6)
   $ cyclictest -m -p 80 -i 200 -h 400 -a 5 -t 1 -D 60
```

Ch. 6 §6.3 already showed the two endpoints of this experiment on the same box (untuned ~3.2 ms max → tuned ~4 µs); what this table adds is the *decomposition* — which knob buys which part — and one more layer (the firmware/spec-exec row, which is why its final max is lower than Ch. 6's). Representative results (toolchain and machine: Appendix L.3) — reference machine **Xeon Gold 6326** (Ice Lake-SP), Linux 6.18, `CONFIG_HZ=1000`, `PREEMPT` (non-RT), rt-tests 2.x; illustrative (`cyclictest` reports min/avg/max in µs; the policy is stated per row):

```
   configuration                                    min    avg    max        what's in the max
   stock core, busy system (SCHED_OTHER, unpinned)  2 µs   15 µs  ~3,000 µs   fair-class (EEVDF) preemption, tick, IRQs, kworkers
   pinned only (Ch.56), SCHED_OTHER, default kernel 2 µs    3 µs   ~250 µs    no migration, but tick + IRQs + RCU remain
   pinned + isolcpus + SCHED_FIFO 80                1 µs    2 µs    ~40 µs    no preemption, but tick still fires
   + nohz_full + rcu_nocbs + IRQs away (Ch.56)      1 µs   1.2 µs   ~5 µs     tickless, quiet — tail nearly GONE
   + mitigations=off, C-states limited (Ch.6)       1 µs   1.0 µs   ~2 µs     firmware/spec-exec noise removed too
```

Read it the Ch. 1 way: **the min/avg barely move; the MAX — the tail — drops from ~3 ms to ~2 µs**, three orders of magnitude, as you add each isolation layer. Pinning alone (Ch. 56) removes migration but leaves the **timer tick, IRQs, and RCU** (max ~250 µs). Adding `isolcpus` + RT priority stops **preemption** (~40 µs) but the **tick still fires** every ms. Only **`nohz_full` + `rcu_nocbs`** (tickless) + **IRQs routed away** (Ch. 56) get the core genuinely quiet (~5 µs), and the last bit comes from the **firmware/spec-exec layer** (Ch. 6 — C-states waking the core, mitigations). This is the canonical low-latency-tuning result and the verification loop: **`cyclictest` max is your jitter; each isolation knob removes one source; tune-measure-repeat until the max is flat.** The remaining spikes after full tuning point at the *last* noise sources (a stray IPI, a SMI from firmware — Ch. 6, an un-offloaded kernel thread) — chase them with `cyclictest -b` (break-trace) / `ftrace` and `/proc/interrupts`, or, properly, with the `rtla osnoise`/`timerlat` attribution workflow and the noise catalog of Ch. 61. A production latency box is signed off when `cyclictest` (and the real app's tail) shows a flat distribution under realistic load.

## 59.4 Techniques

### 59.4.1 CPU isolation and tickless cores

Building the quiet core — the boot-time and runtime isolation (the bulk of Appendix C):

- **`isolcpus` (or cpuset isolation).** Add `isolcpus=<hot-cores>` to the kernel cmdline (or use cgroup v2 `cpuset.cpus`/`cpuset.cpus.partition=isolated`) so the scheduler never auto-places or load-balances tasks onto the hot cores. Then pin your hot threads there explicitly (Ch. 56). This is the floor — without it, the kernel uses your "hot" cores for general work.
- **`nohz_full` + `rcu_nocbs` (tickless — the jitter killer).** Add `nohz_full=<hot-cores> rcu_nocbs=<hot-cores>` so the hot cores stop the periodic tick (when ≤1 task runs) and offload RCU callbacks. This removes the per-millisecond timer interrupt — the single biggest remaining jitter source after pinning (§59.3). **Keep exactly one runnable task per `nohz_full` core** (Ch. 42, 54) — a second task brings the tick back.
- **Route IRQs and kernel work off (Ch. 56).** `irqaffinity=<housekeeping>` on the cmdline + per-IRQ `smp_affinity` (disable `irqbalance`), and cgroups to confine system services/`kworker`s to housekeeping cores. Isolation from the *scheduler* (`isolcpus`) plus isolation from *interrupts* (Ch. 56) plus *tickless* (`nohz_full`) together make the core quiet — all three are needed.
- **Firmware/BIOS layer (Ch. 6).** Below the kernel: disable deep **C-states** (a core entering C6 and being woken adds latency — keep it in C1/C0), fix **P-states/turbo** (frequency changes are jitter), disable **SMIs** where possible (System Management Interrupts are invisible to the OS and a notorious jitter source — Ch. 6), handle **HT** (Ch. 57). Kernel tuning sits *on top of* a properly-tuned BIOS (Ch. 6); a stray SMI defeats perfect kernel isolation.
- **`tuned` / profiles.** Distros ship low-latency profiles (`tuned-adm profile latency-performance`/`realtime`, RHEL's `realtime` package) that bundle many of these settings; useful as a starting point, but verify each knob (and `cyclictest`) rather than trusting the profile blindly.
- **Verify the core is tickless/quiet.** Check `/proc/interrupts` (hot core columns ~flat — including `LOC` local-timer count not incrementing under `nohz_full`), `cat /sys/devices/system/cpu/cpuN/...`, and `cyclictest` (§59.3). A `nohz_full` core that *still* ticks (because a second task is on it, or a dependency isn't met) isn't quiet.

### 59.4.2 RT scheduling classes and priorities

Ensuring the hot thread is never preempted — and doing it *safely*:

- **Run the hot thread `SCHED_FIFO` at high priority.** `sched_setscheduler(0, SCHED_FIFO, &param)` (or `chrt -f 80`) so the hot thread runs ahead of all normal tasks and isn't preempted by timeslices or `SCHED_OTHER` work. On an isolated core there's little to preempt it, but RT priority guarantees correctness if *anything* (a kernel thread, a stray task) becomes runnable, and orders *multiple* RT threads deterministically. Set it in thread startup (with affinity — Ch. 56, FTZ/DAZ — Ch. 35, warming — Ch. 62).
- **Choose priorities deliberately.** Higher RT priority = runs first. Order your RT threads by latency-criticality (the tick-to-trade thread above the journaling thread, etc.), and know what a 100%-busy `SCHED_FIFO` thread *can* starve on its CPU: the `SCHED_OTHER` per-CPU kthreads (`ksoftirqd/N`, `kworker/N:*`), lower-priority RT kthreads such as `watchdog/N`, and on PREEMPT_RT the threaded `irq/*` handlers and `rcuc/N` — which is why those belong on housekeeping cores and why the throttle exists (§59.5). (`migration/N` runs in the *stop* class, above every RT priority, and cannot be starved.) Document the priority map.
- **`SCHED_FIFO` vs `SCHED_RR`.** FIFO runs until the thread yields/blocks/is preempted by higher priority (no timeslicing among equal priority — right for a single dominant hot thread). RR adds round-robin timeslicing among equal-priority threads (use if you have several equal RT threads that must share). For a busy-polling hot thread that runs continuously, **FIFO** is the norm.
- **Beware RT throttling (§59.5).** On kernels before 6.12 (or with `CONFIG_RT_GROUP_SCHED=y`) the RT throttle (`sched_rt_runtime_us`/`sched_rt_period_us`, default 950,000 of every 1,000,000 µs) *caps* RT-thread CPU to prevent a runaway RT thread from locking up the system — but a **busy-polling `SCHED_FIFO` thread runs 100%**, so the throttle *forcibly deschedules it* for the remaining ~50 ms of every second (a periodic, tens-of-milliseconds latency spike!). On a dedicated isolated core you typically **disable RT throttling** (`sched_rt_runtime_us = -1`) — *carefully*, because that removes the safety net (a buggy RT thread can then hang the box). From Linux 6.12 the throttle is replaced by the **fair deadline server** (`/sys/kernel/debug/sched/fair_server/cpuN/{runtime,period,defer}`), which preempts the RT thread only when fair-class tasks are actually runnable on that CPU — so a lone RT thread on a properly isolated core is no longer stalled at all, and the knob to check is that nothing `SCHED_OTHER` lands there. This is the key RT-tuning subtlety for busy-polling HFT.
- **Priority inheritance for shared locks (Ch. 43).** If an RT hot thread shares a lock with a lower-priority thread (it shouldn't on the hot path — Ch. 42, but control-plane interactions happen), use **priority-inheritance mutexes** (PI-futex / `PTHREAD_PRIO_INHERIT`) so the lock holder is temporarily boosted — preventing **priority inversion** (§59.5) where the RT thread blocks forever on a lock held by a thread that never gets to run.
- **`mlockall` + RT (Ch. 30, 34).** An RT thread that page-faults (Ch. 30) defeats its determinism (the fault is a syscall + possible I/O). `mlockall(MCL_CURRENT|MCL_FUTURE)` and pre-faulting (Ch. 34) are part of RT setup — RT priority without locked memory still spikes on a fault.

### 59.4.3 `SCHED_DEADLINE` and the RT kernel: when a trading host wants them

*Toolchain and machine: Appendix L.3; the DEADLINE runs are on the 4-vCPU KVM guest (kernel 6.18, `CONFIG_HZ=250`, `HRTICK_DL` off, no cpuset partitions — a root domain of four vCPUs).*

**When `SCHED_DEADLINE` earns its place.** Not for the busy-polling hot thread — it wants 100 % of a core forever, which no admission control will grant (and `SCHED_FIFO` on an isolated partition already gives it). It is for the **periodic housekeeping with a budget**: the 1 kHz telemetry sampler that must run 50 µs every millisecond and must *never* run longer; the PTP servo (Ch. 78); the heartbeat writer (Ch. 103 §103.4.3). Declared as `runtime/deadline/period`, each gets its slot ahead of every FIFO priority, cannot starve its neighbours, and fails loudly at startup if the core is oversubscribed — a property no priority scheme has. Measured, `sched_probe.cpp` on the guest — the DEADLINE half of it:

```cpp
// sched_probe.cpp (excerpt) — SCHED_DEADLINE: a 200 us / 1 ms contract, a spinner that wants more, admission, GRUB.
// Build: g++ -O2 -std=c++20 -pthread sched_probe.cpp -o sched_probe      Run (root): ./sched_probe <cpu>
#include <linux/sched.h>
#include <linux/sched/types.h>
#include <sys/syscall.h>
static int setattr(const sched_attr& a) { return (int)syscall(SYS_sched_setattr, 0, &a, 0); }
static std::uint64_t now_ns() { timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return std::uint64_t(t.tv_sec) * 1000000000ull + t.tv_nsec; }
// A DEADLINE task may not be pinned narrower than its root domain (EPERM) unless the cpu is an isolated cpuset partition,
// and may not clone (EAGAIN): the thread is NOT pinned here, and helpers are spawned from main.
auto cbs_run = [&](const sched_attr& a, const char* label) {
    std::thread t([&] {
        if (setattr(a)) { std::perror("   sched_setattr(DEADLINE)"); return; }
        std::uint64_t t0 = now_ns(), last = t0; std::vector<std::uint64_t> gaps;
        while (now_ns() - t0 < 1000000000ull) { std::uint64_t n = now_ns(); if (n - last > 50000) gaps.push_back(n - last); last = n; }
        std::sort(gaps.begin(), gaps.end());
        std::printf("%s throttled %zu times in 1 s, typical gap %.0f us\n", label, gaps.size(), gaps.empty() ? 0.0 : gaps[gaps.size() / 2] / 1e3);
    });
    t.join();
};
sched_attr a{}; a.size = sizeof a; a.sched_policy = SCHED_DEADLINE; a.sched_runtime = 200000; a.sched_deadline = 1000000; a.sched_period = 1000000;
cbs_run(a, "   runtime 200us / period 1ms, a spinner that wants 100%:");
sched_attr b = a; b.sched_runtime = 900000;        /* admitted alongside `a` on the same root domain? */
sched_attr c = a; c.sched_flags = SCHED_FLAG_RECLAIM; cbs_run(c, "   same task with SCHED_FLAG_RECLAIM (GRUB):");
```


```
   runtime 200us / period 1ms, a spinner that wants 100%:  throttled 83 times in 1 s, typical gap 12456 us
      <- CBS enforcing the budget; the 12 ms gaps (not 800 us) are CONFIG_HZ=250 tick-granular accounting with HRTICK_DL off:
         the spinner overruns its 200 us by up to 4 ms per charge and is throttled proportionally longer (§59.2.2)
   admission: 200us/1ms task admitted; a second asking 900us/1ms on the same root domain: admitted
      <- 110 % of one CPU is 27 % of a four-CPU root domain: admission is domain-wide without a cpuset partition
   same task with SCHED_FLAG_RECLAIM (GRUB):               throttled 104 times in 1 s, typical gap 103 us
      <- the idle bandwidth of the domain is reclaimed; the task ran ~90 % of the second
   sched_setattr(SCHED_DEADLINE) from a thread pinned to one vCPU: EPERM (affinity narrower than the root domain)
```

The recipe: the housekeeping core(s) as a cpuset partition (so admission is per core and pinning is legal), `sched_setattr` from an unpinned helper or after the partition is in place, `HRTICK_DL` on, no `RECLAIM` for the sampler (it must stay bounded), `RECLAIM` for the best-effort periodic work.

**PREEMPT_RT, mainline since 6.12 — should the box run it?** `CONFIG_PREEMPT_RT` makes almost all of the kernel preemptible: spinlocks become sleeping (PI-aware) mutexes, interrupt handlers run as threads (`irq/N-name`, as if `threadirqs` were given), softirqs run in `ksoftirqd`/the raising thread, and RCU callbacks in `rcuc`. The *worst case* of anything that enters the kernel becomes bounded (tens of µs instead of ms); the *mean* cost of every kernel crossing rises (a lock that used to spin now may sleep; an IRQ that used to run in 2 µs now costs a wake-up). `PREEMPT_DYNAMIC` (`preempt=none|voluntary|full` on the cmdline, `/sys/kernel/debug/sched/preempt` at runtime, 5.12+) switches between the non-RT models without a rebuild; RT itself is a build option (or `preempt=lazy`/`PREEMPT_LAZY` in 6.13+, the halfway house). Representative `cyclictest` on the reference machine, `-m -p 95 -i 200 -h 400 -D 10m` under `stress-ng --cpu 8 --io 4` (the §59.3 method):

```
   kernel model / core                         min   avg    max       what the max is
   PREEMPT (non-RT), stock core                 2     9     ~3,000    fair-class preemption, long non-preemptible kernel sections (softirqs, locks)
   PREEMPT_RT, stock core                       2    11       ~65    everything preemptible; the max is a threaded IRQ + a PI hand-off
   PREEMPT (non-RT), isolated core (§59.4.1)    1     1.2      ~5    nothing enters the kernel on this core — RT has nothing to bound
   PREEMPT_RT, isolated core                    1     1.4      ~5    identical tail; ~15 % higher avg because syscalls/wake-ups cost more
```

The verdict for a **busy-poll isolated box** (the tick-to-trade path): **no** — the hot core never enters the kernel, so RT bounds nothing there and taxes the housekeeping syscalls; keep `PREEMPT` (or `PREEMPT_DYNAMIC` at `full`) and the §59.4.1 isolation. For a **shared gateway/risk box** that still takes NIC IRQs on the cores doing the work, or any design that *blocks* on the hot path (Ch. 54 §54.4.2), **yes, measured** — the 3 ms → 65 µs worst case is what RT is for. Either way, `threadirqs` alone (non-RT kernels, since 2.6.39) buys the threaded-IRQ half: each handler becomes a `SCHED_FIFO 50` thread you can pin (`irq/N-eth0-TxRx-0` → housekeeping cores, with `isolcpus=managed_irq` for the managed vectors — Ch. 56) and prioritize *below* your hot FIFO threads, which a hard IRQ cannot be.

**`stalld`: the alternative to the RT throttle.** Disabling `sched_rt_runtime_us` (§59.4.2) removes the safety net; `stalld` (from the `rt-tests` family, Red Hat) puts a *selective* one back: a daemon on a housekeeping core that watches `/sys/kernel/debug/sched/debug` (or the `sched_switch` tracepoint) for a runnable task starved on a CPU for longer than a threshold and boosts it to `SCHED_DEADLINE` for a tiny budget (e.g. 20 µs per 1 s) so it makes progress — a `kworker` or `ksoftirqd` that a FIFO spinner would otherwise starve into a softlockup. On a properly isolated core there is nothing to starve and `stalld` never fires; on a shared or half-isolated core it is the difference between a stalled `kworker` and a 20 µs blip you can see in Ch. 61's trace. Run it with `-t 60 -r 20 -d 1000` (threshold 60 s, 20 µs boost per 1 s) and alarm on its log — a boost is a configuration error to find, not a feature to rely on.

### 59.4.4 Containers on the trading host: cpusets, `cpu.max` and the Kubernetes question

Adjacent work — a risk service, a monitoring agent, a gateway that owns an SR-IOV VF (Appendix S) — increasingly arrives as a container. A container is a cgroup plus namespaces; the latency question is *which* cgroup controllers it runs under:

- **`cpuset` partitions are the right confinement** (§59.4.1, Appendix C.4): a container whose `cpuset.cpus` is the housekeeping set cannot land on the hot cores, and one whose cpuset *is* an isolated partition owns its core like any pinned process. Docker: `--cpuset-cpus=0-1`; systemd-nspawn/`systemd-run`: `AllowedCPUs=`; Kubernetes: the **static CPU manager policy** (`--cpu-manager-policy=static`, a Guaranteed pod with integer CPU requests gets exclusive CPUs from the shared pool) plus the **topology manager** (`--topology-manager-policy=single-numa-node`) so the exclusive CPUs, the VF and the memory are on one node.
- **`cpu.max` (cgroup v2; `cpu.cfs_quota_us` in v1) is the wrong confinement — it is a throttle.** A quota of *q* per period *p* lets the group run *q* µs, then **stops every task in it until the period ends**, whatever CPU it is on and whatever it was doing. Docker's `--cpus=1.5`, Kubernetes' `resources.limits.cpu`, systemd's `CPUQuota=` all set it. Measured on the guest (a spinner recording every gap > 1 ms in its own execution, placed in a cgroup with a 50 % quota at three periods):

```cpp
// gapdetect.cpp — a spinner that records every gap > T us in its own execution (the Ch. 61 idea); here it is put in a
// cgroup with a quota:  mkdir /sys/fs/cgroup/q && echo "50000 100000" > /sys/fs/cgroup/q/cpu.max &&
//   sh -c 'echo $$ > /sys/fs/cgroup/q/cgroup.procs; exec taskset -c 2 ./gapdetect 3 1000'      (v1: cpu.cfs_quota_us / cfs_period_us)
// Build: g++ -O2 -std=c++20 gapdetect.cpp -o gapdetect
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <time.h>
#include <vector>
static std::uint64_t now_ns() { timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return std::uint64_t(t.tv_sec) * 1000000000ull + t.tv_nsec; }
int main(int argc, char** argv) {
    double secs = argc > 1 ? std::atof(argv[1]) : 3.0; std::uint64_t thr = argc > 2 ? std::atoll(argv[2]) * 1000 : 1000000;
    std::vector<std::uint64_t> gaps; std::uint64_t t0 = now_ns(), last = t0;
    while (now_ns() - t0 < secs * 1e9) { std::uint64_t n = now_ns(); if (n - last > thr) gaps.push_back(n - last); last = n; }
    std::sort(gaps.begin(), gaps.end());
    std::printf("gaps > %llu us in %.0f s: %zu; p50 %.2f ms, p90 %.2f ms, max %.2f ms\n", (unsigned long long)(thr / 1000), secs, gaps.size(),
                gaps.empty() ? 0.0 : gaps[gaps.size() / 2] / 1e6, gaps.empty() ? 0.0 : gaps[gaps.size() * 9 / 10] / 1e6, gaps.empty() ? 0.0 : gaps.back() / 1e6);
}
```


```
   quota / period                          gaps > 1 ms in 3 s   p50 gap     p90 gap     max gap     cpu.stat
   none                                            3            1.56 ms     2.18 ms     2.18 ms     (the guest's own steal)
   50 ms / 100 ms  (Docker --cpus=0.5)            34           49.00 ms    49.06 ms    53.05 ms     nr_throttled 207
   10 ms /  20 ms                                147            8.98 ms    12.98 ms    56.99 ms     nr_throttled 355
   500 ms / 1 s                                    3          496.98 ms   501.26 ms   501.26 ms     nr_throttled 358
   200 ms / 100 ms (two CPUs' worth, one spinner)  4            3.63 ms   777.03 ms   777.03 ms     <- a multi-CPU quota still burst-throttles
```

  A 50 % quota is a **49 ms stall every 100 ms**, exactly as the arithmetic says — a risk check or a gateway inside such a container is *dead* for half of every period, on a schedule unrelated to the market. Even a quota larger than the group's use throttles in bursts (the last row: slices are handed out in `cpu.cfs_bandwidth_slice_us` = 5 ms chunks per CPU and reclaimed; a burst can exhaust the period's distributed slices). The rule: **no `cpu.max`/`CPUQuota=`/`limits.cpu` on anything latency-sensitive** — confine with cpusets, prioritize with `cpu.weight` (proportional, never a stall), and verify with `cat /sys/fs/cgroup/<path>/cpu.max` = `max` and `nr_throttled` = 0 in `cpu.stat` at the gate (Appendix M). Kubernetes' Guaranteed QoS sets requests = limits, which *sets a quota*; the `cpu-manager-policy=static` exclusive-CPU path is the one case where the kubelet's quota is harmless (the pod has whole CPUs and the quota equals them), and `--cpu-cfs-quota=false` on the kubelet removes it entirely.
- **The Kubernetes question.** A hot-path pod is almost never the right answer: the kubelet's own agents, the CNI's veth/bridge path (Ch. 64 §64.2.4, §64.4.5 — the namespaces hop), the liveness probes and the scheduler's willingness to evict are each a Ch. 61 noise source, and the static CPU manager + topology manager + SR-IOV device plugin + hugepages + `isolcpus` on the node reproduces, with more moving parts, what one systemd unit with `AllowedCPUs=` does (Ch. 103 §103.4.9). Use it for the **adjacent tier** — risk, analytics, monitoring — on nodes whose kubelet is itself confined to housekeeping cores; keep the tick-to-trade processes as units on the bare-metal hot box.

## 59.5 Pitfalls & anti-patterns: RT throttling, priority inversion, forced IRQ threads, `cpu.max`

- **RT throttling descheduling a busy-poll thread (the classic RT-HFT trap).** A `SCHED_FIFO` busy-polling thread runs 100% CPU; on kernels before 6.12 (or with `CONFIG_RT_GROUP_SCHED=y`) the RT throttle (default 950 ms of every 1 s) will **forcibly stop it** for ~50 ms once per second — a periodic, tens-of-milliseconds stall that looks mysterious until you find it. On a dedicated isolated core, **disable RT throttling** (`sched_rt_runtime_us=-1`) or tune it — *knowing* you've removed the runaway-RT safety net (test thoroughly; a bug can now hang the box). From Linux 6.12 the fair deadline server replaces the throttle and fires only when `SCHED_OTHER` work is runnable on that CPU (§59.4.2) — so on a current kernel the fix is to keep fair-class tasks off the hot core, and the symptom to look for changes from a 1 Hz stall to "something else landed on my core".
- **`NET_RX` softirq / `ksoftirqd` landing on an isolated core (Ch. 64 §64.2.2).** A NIC queue whose managed vector still includes the hot core, an RPS mask that names it, or the hot thread itself sending on a kernel socket (loopback runs the whole receive path in the sender's softirq context) puts network stack work on the core that `isolcpus`/`nohz_full` were supposed to empty — visible as `NET_RX` moving in `/proc/softirqs` for that CPU and `sched_switch → ksoftirqd/N` in Ch. 61's trace. `isolcpus=managed_irq`, `rps_cpus=0`, and no kernel sockets on the hot thread (Ch. 64 §64.4.2).
- **Priority inversion.** An RT hot thread blocks on a lock (or resource) held by a *lower*-priority thread that — because it's lower priority and the RT thread is hogging the CPU — *never runs to release it*, so the RT thread waits forever (or a long time). Avoid shared locks on the RT/hot path (Ch. 42–43); where unavoidable, use **priority-inheritance** mutexes (§59.4.2).
- **`nohz_full` core with >1 task (tick comes back).** `nohz_full` only stops the tick when **one** task is runnable on the core; a second runnable task (a stray thread, a `kworker` you didn't offload) **re-enables the tick**, silently undoing the tickless benefit. (The SMT sibling is a *different* logical CPU to the scheduler: load there does not bring this CPU's tick back — it degrades the hot thread through shared core resources instead, Ch. 57.) Keep exactly one hot thread per isolated core (Ch. 42, 54) and offload kernel work (`rcu_nocbs`, cgroups).
- **Pinning/isolating without the firmware layer (Ch. 6).** Perfect kernel isolation is defeated by BIOS-level jitter: deep **C-states** waking the core, **turbo/P-state** frequency changes, and especially **SMIs** (invisible to the OS, can be hundreds of µs). Tune the BIOS (Ch. 6) *under* the kernel tuning; `cyclictest` spikes that survive kernel tuning often point at firmware.
- **Forgetting `mlockall` on RT threads (Ch. 30, 34).** RT priority guarantees scheduling, not memory residency — a page fault (Ch. 30) still spikes. `mlockall` + pre-fault (Ch. 34) is mandatory RT setup.
- **Over-tuning / breaking the system.** Isolating *too many* cores (leaving too few for housekeeping), disabling RT throttling without understanding it, setting a 100%-busy RT thread above the per-CPU kthreads it can starve (`ksoftirqd/N`, `kworker/N:*`, `watchdog/N`, threaded `irq/*` on PREEMPT_RT → softlockups and stalled I/O), or applying a `tuned` profile blindly — all can make the box *unstable* or *slower*. Tune deliberately, measure each change (`cyclictest`), and keep enough housekeeping capacity.
- **Forced IRQ threads on the wrong core.** With `threadirqs` or `PREEMPT_RT`, every handler is a `SCHED_FIFO 50` thread — created with the *default* affinity, i.e. free to run on the hot core until pinned, and able to preempt a FIFO hot thread of priority < 50. Pin `irq/*` threads to housekeeping cores (`isolcpus=managed_irq` for the managed vectors, `/proc/irq/N/smp_affinity` plus the thread's affinity for the rest) and set the hot thread's FIFO priority above 50 (§59.4.3).
- **`SCHED_DEADLINE` admission that "failed silently" — or succeeded for the wrong reason.** `EPERM` from `sched_setattr` after pinning (affinity narrower than the root domain), `EAGAIN` from `pthread_create` in a DEADLINE thread, and two tasks oversubscribing one core but admitted because the root domain is the whole box (§59.2.2, §59.4.3). Make the core a cpuset partition first; check `nr_throttled`-style evidence (`/proc/<pid>/sched`'s `dl.runtime`) rather than trusting the return code.
- **`cpu.max` on a latency-sensitive container.** A 50 % quota is a 49 ms stall every 100 ms (§59.4.4) — the single most common "periodic multi-ms gap in the risk service" finding. `cpu.max = max`, cpusets for confinement, `cpu.weight` for priority; `nr_throttled` must read 0 at the gate.
- **`isolcpus` deprecation / wrong interface.** `isolcpus` works but is semi-deprecated in favor of cgroup v2 `cpuset` isolated partitions on newer kernels; using the wrong/old interface (or both inconsistently) can give surprising behavior. Know your kernel's preferred isolation mechanism.
- **Assuming tuned once = tuned forever.** Kernel/distro updates, BIOS updates, and config drift can silently re-enable `irqbalance`, change `CONFIG_HZ`, reset RT throttling, or re-route IRQs. **Re-run `cyclictest` after any change** and monitor the production tail — and the isolation itself — continuously (Ch. 113, 118).
- **RT priority on a shared/general core without isolation.** Giving a thread `SCHED_FIFO` on a *non*-isolated core can *starve* the rest of the system (it preempts everything, including things the box needs) — RT priority belongs with isolation. Don't RT-prioritize on a general-purpose core casually.

## 59.6 Exercises & checklist

**Exercises**

1. **`cyclictest` the layers.** On a test box, run `cyclictest -p 80 -a <core> -D 60` (a) stock + busy, (b) pinned only (Ch. 56), (c) + `isolcpus` + RT, (d) + `nohz_full`/`rcu_nocbs` + IRQs away, (e) + BIOS C-states/mitigations (Ch. 6). Record max latency at each step. Reproduce the §59.3 progression — which knob removed the most?
2. **Catch RT throttling.** On a pre-6.12 kernel, run a `SCHED_FIFO` busy-polling thread (100% CPU) on an isolated core with RT throttling at default; measure its latency and look for the ~50 ms stall once per second (the throttle). On 6.12+, instead park a `SCHED_OTHER` spinner on the same core and watch the fair deadline server carve its slice out of your RT thread. Disable throttling (`sysctl kernel.sched_rt_runtime_us=-1`); confirm the spikes vanish. Understand what safety you gave up (§59.5).
3. **Tickless verification.** With `nohz_full=<core>`, run one task on the core and watch `/proc/interrupts` LOC (local timer) for that core — confirm it stops incrementing. Then add a *second* runnable task to the core and watch the tick come back (§59.5). 
4. **Priority inversion.** Construct an RT thread blocking on a mutex held by a low-priority thread that can't run (RT thread busy on the same core). Observe the stall. Switch to a `PTHREAD_PRIO_INHERIT` mutex and confirm the holder is boosted and the stall resolves (§59.4.2).
5. **Find the last spike.** After full tuning, if `cyclictest` still shows occasional spikes, use `cyclictest -b <us>` (break-trace) + ftrace, and `/proc/interrupts`, to identify the source (an IPI, an SMI — Ch. 6, an un-offloaded kthread). Eliminate it; re-measure. Then do it again the Ch. 61 way — `rtla osnoise -s <us> -t` — and compare how much faster the stopped trace names the culprit.

**Checklist — RT scheduling & kernel tuning**

- [ ] Hot cores are **isolated** (`isolcpus`/cpuset), **tickless** (`nohz_full` + `rcu_nocbs`), with **IRQs routed away** (`irqaffinity` + Ch. 56) and **exactly one hot thread per core** (so `nohz_full` actually stops the tick — Ch. 42, 54).
- [ ] Hot threads run **`SCHED_FIFO`** at deliberate, documented priorities (below critical kernel RT threads), set in thread startup with **affinity** (Ch. 56) and **`mlockall`** + pre-fault (Ch. 30, 34).
- [ ] **RT throttling is handled** for busy-polling threads (`sched_rt_runtime_us=-1` or tuned) — knowingly, with the runaway-RT risk understood and tested (§59.5).
- [ ] **Priority inversion** is avoided (no shared locks on the hot path — Ch. 42–43; **priority-inheritance** mutexes where unavoidable).
- [ ] The **BIOS/firmware layer** (Ch. 6) is tuned *under* the kernel: deep **C-states off**, **P-states/turbo** fixed, **SMIs** minimized, **HT** handled (Ch. 57).
- [ ] I **measured jitter with `cyclictest`** before/after each change (max latency = the tail — Ch. 1), drove the max down to single-digit µs under realistic load, and chased remaining spikes to their source.
- [ ] Everything-not-hot is **confined to housekeeping cores** (cgroups/`cpuset`), with **enough housekeeping capacity**; I didn't over-isolate or over-tune into instability.
- [ ] Tuning is **re-verified after kernel/BIOS/config changes** (no drift — `irqbalance` re-enabled, `CONFIG_HZ` changed, throttling reset) and the production tail — and the core's quietness — is **monitored continuously** (Ch. 113, 118); residual spikes are attributed with Ch. 61's catalog and tools, not tolerated.

## 59.7 References

- The Linux kernel documentation — `Documentation/admin-guide/kernel-parameters.txt` (`isolcpus`, `nohz_full`, `rcu_nocbs`, `irqaffinity`), `Documentation/timers/no_hz.rst`, and the RT-scheduling (`sched(7)`, `SCHED_FIFO`/`SCHED_DEADLINE`) docs — the mechanisms of §59.2, §59.4.
- The `rt-tests` / `cyclictest` documentation and the OSADL latency-plot methodology — measuring scheduling jitter (§59.3).
- The PREEMPT_RT documentation (mainline since Linux 6.12, `CONFIG_PREEMPT_RT`) and the kernel `Documentation/scheduler/sched-rt-group.rst` (RT throttling; superseded by the fair deadline server from 6.12) — RT scheduling and the throttling subtlety (§59.4.2, §59.5).
- Red Hat *Low Latency / Real-Time Tuning Guide* and the `tuned`/`tuned-adm` (`realtime`, `latency-performance`) documentation — the consolidated isolation/RT recipe (ties Appendix C).
- Carl Cook / HFT latency talks — isolation, RT, and `cyclictest`-driven tuning of the trading box.

## 59.8 Additional Reading

- The OSADL real-time wiki and `cyclictest` latency plots — extensive measured jitter data and methodology.
- Jon Masters' real-time Linux talks and the `nohz_full` design papers (Frederic Weisbecker) — full dynticks internals.
- Ch. 54 (*Context Switching*) — the preemption/interrupt jitter this eliminates; Ch. 56 (*Pinning*) — affinity/IRQ routing that pairs with isolation; Ch. 57 (*SMT*) — isolating the sibling; Ch. 6 (*System Setup*) — the BIOS layer below; Ch. 62 (*Warming*) — keeping the now-quiet core warm; Ch. 30/34 (*Memory/mmap*) — `mlockall` for RT.
- Ch. 58 (*Cache Allocation Technology & Intel RDT*) — the next step after pinning: partitioning the shared L3 so a neighbor can't evict the hot working set even when the core is isolated.
- **Appendix C** (System Tuning Checklist) — the full `isolcpus`/`nohz_full`/RT/IRQ/BIOS recipe consolidated; **Appendix E** — the context-switch cost this tuning avoids (the `cyclictest` figures themselves are §59.3's).

---

*Next: Ch. 60 — Power Management & Frequency Determinism, the clock half of a quiet core: the per-generation C-state exit-latency table and the ~120 µs median wake-up a quiet market inflicts when C6 is allowed, PM QoS from the trading process (what `tuned` does and how its hold silently vanishes), HWP/EPP and making the frequency window a point, PL1/PL2/RAPL throttles that hit the hot core from other cores, the uncore clock nobody pins, and a 1 Hz proof — `APERF/MPERF`, `cycles/ref-cycles` or an `imul` chain — that the clock stayed flat all day.*
