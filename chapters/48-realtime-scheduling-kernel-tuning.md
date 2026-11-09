# Part VII — OS, Scheduling & Isolation

# Chapter 48 — Real-Time Scheduling & Kernel Tuning

> **Prerequisites:** Ch. 43 (context switching — preemption/interrupts are the jitter this eliminates), Ch. 45 (pinning — isolation makes the pin effective), Ch. 46 (SMT — isolate the sibling too), Ch. 47 (cache partitioning — the shared-L3 half of isolation, which this chapter's kernel knobs complete), Ch. 6 (BIOS/firmware tuning — the layer below the kernel), Ch. 1 (jitter/tail — the metric).
>
> **Leads into:** Ch. 50 (warming — once the core is quiet, keep it warm), Ch. 60–62, 66, 67 (NIC / DDIO / flow steering / bypass — the I/O on the isolated cores), Appendix C (the consolidated checklist this chapter feeds). This is the kernel configuration that delivers the "quiet core" Ch. 45–47 assumed.

---

## 48.1 Why it matters: eliminating scheduler-induced jitter

Chapters 45–46 pinned the hot thread to a core and handled its SMT sibling — but a pinned thread on a stock-configured core is *still* not quiet. The Linux kernel, by default, does a steady stream of per-core housekeeping that interrupts *any* thread on *any* core: the **periodic timer tick** (the scheduler's heartbeat, historically 100-1000 Hz — an interrupt every 1-10 ms on every core), **RCU callbacks** (Ch. 36) processed per-CPU, **kernel worker threads** (`kworker`), scheduler load-balancing, and the **fair-class scheduler** itself (CFS, replaced by EEVDF in Linux 6.6 — the `SCHED_OTHER` policy either way) which will happily preempt your thread to run something else (or nothing else, but still tick). Each of these is a context switch or interrupt (Ch. 43) — a cache-trashing latency spike that lands in the tail (Ch. 1). Pinning stops *migration*; it does not stop the kernel from doing its periodic work *on the pinned core*. To get a genuinely **jitter-free** core you must tell the kernel to *leave that core alone* — and that's kernel tuning: `isolcpus`, `nohz_full`, `rcu_nocbs`, RT scheduling, and cgroups.

This is the layer that turns "pinned" into "quiet," and it's mostly **boot-time kernel configuration** (the kernel command line) plus runtime knobs — the most impactful and most often half-done part of building a low-latency box. `isolcpus` removes cores from the scheduler's general pool so nothing is load-balanced onto them. **`nohz_full`** makes cores **tickless** — when only one thread runs on the core (your pinned hot thread), the kernel *stops the periodic timer interrupt* entirely, so the hot loop runs with *no* per-millisecond tick stealing cycles and polluting caches. **`rcu_nocbs`** offloads RCU callback processing off the isolated cores onto housekeeping cores. **RT scheduling** (`SCHED_FIFO`/`SCHED_RR` with high priority) ensures that if anything *does* become runnable on the core, your thread wins and isn't preempted by lower-priority work. Together these eliminate the scheduler- and timer-induced jitter that pinning alone leaves behind — taking the per-core interrupt rate from ~1000/sec down to ~0 during steady-state trading.

The discipline, and the reason this chapter exists as its own thing, is that **jitter is measured, not assumed** — and the standard instrument is **`cyclictest`** (from `rt-tests`), which measures the *latency between when a timer should fire and when a thread actually wakes*, giving you the scheduling-jitter distribution (max latency is the number that matters — Ch. 1's tail). A well-tuned isolated core shows `cyclictest` max latencies in the *single-digit microseconds* or below; a stock core shows *hundreds of microseconds to milliseconds*. You tune, you `cyclictest`, you find the remaining jitter source (a stray IRQ — Ch. 45, an un-offloaded kernel thread, a missed `nohz_full`), you fix it, you re-measure — until the tail is flat. This chapter covers the `isolcpus`/`nohz_full`/RT/cgroup mental model (§48.2), the `cyclictest` before/after measurement (§48.3), the isolation-and-RT techniques (§48.4), and the pitfalls — RT throttling (which can *stall* a busy-polling RT thread), priority inversion, and the over-tuning that breaks the system (§48.5). It's the capstone of the OS-isolation arc (Ch. 43–47) and the bulk of Appendix C.

## 48.2 Mental model: `isolcpus`, `nohz_full`, RT priorities, cgroups

**The default kernel is busy on every core.** Even with nothing of yours scheduled, a stock core takes the **timer tick** (the scheduler interrupt, `CONFIG_HZ` times/sec), runs **RCU callbacks** (Ch. 36), hosts **kernel threads** and **`kworker`** items, and is subject to **scheduler load-balancing** (the fair-class scheduler — CFS/EEVDF — moving tasks around). The tuning goal: make the hot cores do *none* of this.

**The kernel command-line isolation knobs (boot-time — `/etc/default/grub` → reboot):**

```
   isolcpus=managed_irq,domain,2-7
                       remove cores 2-7 from the general scheduler's load-balancing pool (the `domain` flag,
                       the default) → nothing is auto-scheduled there; only explicitly-pinned tasks run (Ch. 45)
                       `managed_irq`: keep driver-MANAGED interrupts (multi-queue NIC/NVMe vectors, which ignore
                       irqaffinity= and refuse /proc/irq writes — Ch. 45) off these cores (Linux 5.6+)
                       `nohz`: equivalent to nohz_full= for the same cores (then also affine the residual-tick
                       workqueues: /sys/devices/virtual/workqueue/cpumask)
   nohz_full=2-7       make cores 2-7 TICKLESS when ≤1 task runs on them
                       → NO periodic timer interrupt → the hot loop runs uninterrupted
   rcu_nocbs=2-7       offload RCU callback processing off cores 2-7 onto housekeeping cores
                       → no RCU softirq work on the hot cores
   irqaffinity=0,1     default IRQ affinity = housekeeping cores only (ties Ch. 45)
   (+ Ch.6: mitigations=, intel_pstate=, nosmt or HT handling — Ch.46, hugepages, etc.)
```

- **`isolcpus`** — the foundation: the scheduler won't *place* tasks on these cores (so no surprise threads), and won't *load-balance* onto them. You then explicitly pin your hot threads there (Ch. 45). Its flag list matters on a trading box: `domain` (the scheduler isolation above), `nohz` (tickless, like `nohz_full`), and `managed_irq` — the only way to keep *driver-managed* NIC/NVMe interrupt vectors off the isolated cores, since those ignore `irqaffinity=` and reject `/proc/irq/*/smp_affinity` writes (Ch. 45). (Newer kernels prefer the `cpuset`/`cgroup` v2 isolation interface — `cpuset.cpus.partition=isolated` — and `isolcpus` is somewhat deprecated, but the concept is identical.)
- **`nohz_full` (the big one for jitter)** — "full dynticks": a core with `nohz_full` and **only one runnable task** stops its periodic scheduler tick entirely — *no* timer interrupt every millisecond. This is what lets a busy-polling hot thread (Ch. 43) run for milliseconds *with zero kernel interruptions*. (It requires `≤1` task on the core — a *second* task brings the tick back, because the scheduler needs the tick to arbitrate between them; hence one hot thread per isolated core, Ch. 31, 43.) `nohz_full=` *implies* RCU-callback offload for the same CPUs (as if `rcu_nocbs=` had been given); listing `rcu_nocbs=` explicitly is harmless, documents intent, and lets you offload more CPUs than you make tickless. Both need `CONFIG_NO_HZ_FULL=y` (and `CONFIG_RCU_NOCB_CPU=y`) in the kernel build — the real prerequisite.
- **`rcu_nocbs`** — moves RCU's per-CPU callback processing off the isolated cores, removing another source of per-core softirq work; pairs with `nohz_full`.

**RT scheduling classes (runtime — `sched_setscheduler`/`chrt`):**

- **`SCHED_OTHER` (CFS, default)** — the fair scheduler; *will* preempt your thread for fairness. Not for the hot path on a contended core.
- **`SCHED_FIFO` / `SCHED_RR` (real-time)** — RT priority classes that run *ahead of* all `SCHED_OTHER` tasks; a `SCHED_FIFO` thread runs until *it* yields/blocks (FIFO) or its priority is exceeded — it is **not** preempted by normal tasks or timeslices. This guarantees the hot thread isn't preempted if anything else becomes runnable on its core. `chrt -f 80 ./app` or `sched_setscheduler(SCHED_FIFO, prio)`. On an *isolated* core there's little to preempt it anyway, but RT priority is belt-and-suspenders (and essential on shared cores and for ordering multiple threads).
- **The deadline class (`SCHED_DEADLINE`)** — EDF scheduling with explicit runtime/deadline/period; powerful for periodic RT tasks, less common for busy-polling HFT (which wants to run *continuously*, not periodically).
- **PREEMPT_RT (mainline since Linux 6.12, `CONFIG_PREEMPT_RT`).** The fully-preemptible kernel turns IRQ handlers into threads, makes most kernel spinlocks sleeping locks, and runs `ksoftirqd`/`rcuc` at RT priority — bounding the *worst-case* latency of anything that does enter the kernel. For a trading box the trade is a slightly higher mean cost of every kernel crossing against a tighter bound; an isolated, syscall-free busy-polling core gains little from it (there is nothing to preempt), while the housekeeping and I/O cores — and any design that still takes a NIC IRQ — can. Measure with `cyclictest` on both configurations before choosing.

**cgroups — confining everything else.** cgroups (v2) `cpuset` controllers confine *non*-hot tasks to housekeeping cores (the inverse of pinning — move everything-not-hot *off* the isolated cores), and `cpu` controllers bound their resource use. This enforces the housekeeping/hot partition (Ch. 45) system-wide, including system services and anything you forgot to pin.

The model: **a stock kernel interrupts every core (tick, RCU, kworkers, CFS preemption); to get a quiet core, isolate it from the scheduler (`isolcpus`/cpuset), make it tickless (`nohz_full` + `rcu_nocbs`, with ≤1 task on it), route IRQs away (Ch. 45), and run the hot thread RT (`SCHED_FIFO`) so nothing preempts it — confining everything else to housekeeping cores with cgroups. Then verify the jitter with `cyclictest`.**

## 48.3 Measure it: `cyclictest` before/after

The standard scheduling-jitter instrument is **`cyclictest`** (from `rt-tests`): it sleeps a thread for a fixed interval, then measures the difference between the *intended* wake time and the *actual* wake time — the **scheduling latency**, i.e. the jitter the kernel/hardware injects. The **max** latency is the number that matters (the tail — Ch. 1). Run it before and after tuning, on a stock core vs a fully-isolated core.

```
   # one command per row of the table below; "stress-ng --cpu 16" runs in the background for every row
   # row 1: stock core, SCHED_OTHER, unpinned                      (no -p, no -a)
   $ cyclictest -m -i 200 -h 400 -t 1 -D 60
   # row 2: pinned to core 5 (Ch. 45), still SCHED_OTHER             (+ -a 5)
   $ cyclictest -m -i 200 -h 400 -a 5 -t 1 -D 60
   # rows 3-5: + SCHED_FIFO 80 (+ -p 80), then the kernel-cmdline and BIOS layers change underneath it:
   #   row 3: isolcpus=5                 row 4: + nohz_full=5 rcu_nocbs=5, IRQs routed away (Ch. 45), HT sibling idle (Ch. 46)
   #   row 5: + mitigations=off, C-states capped at C1 (Ch. 6)
   $ cyclictest -m -p 80 -i 200 -h 400 -a 5 -t 1 -D 60
```

Ch. 6 §6.3 already showed the two endpoints of this experiment on the same box (untuned ~3.2 ms max → tuned ~4 µs); what this table adds is the *decomposition* — which knob buys which part — and one more layer (the firmware/spec-exec row, which is why its final max is lower than Ch. 6's). Representative results (toolchain and machine: Appendix L.3) — reference machine **Xeon Gold 6326** (Ice Lake-SP), Linux 6.18, `CONFIG_HZ=1000`, `PREEMPT` (non-RT), rt-tests 2.x; illustrative (`cyclictest` reports min/avg/max in µs; the policy is stated per row):

```
   configuration                                    min    avg    max        what's in the max
   stock core, busy system (SCHED_OTHER, unpinned)  2 µs   15 µs  ~3,000 µs   fair-class (EEVDF) preemption, tick, IRQs, kworkers
   pinned only (Ch.45), SCHED_OTHER, default kernel 2 µs    3 µs   ~250 µs    no migration, but tick + IRQs + RCU remain
   pinned + isolcpus + SCHED_FIFO 80                1 µs    2 µs    ~40 µs    no preemption, but tick still fires
   + nohz_full + rcu_nocbs + IRQs away (Ch.45)      1 µs   1.2 µs   ~5 µs     tickless, quiet — tail nearly GONE
   + mitigations=off, C-states limited (Ch.6)       1 µs   1.0 µs   ~2 µs     firmware/spec-exec noise removed too
```

Read it the Ch. 1 way: **the min/avg barely move; the MAX — the tail — drops from ~3 ms to ~2 µs**, three orders of magnitude, as you add each isolation layer. Pinning alone (Ch. 45) removes migration but leaves the **timer tick, IRQs, and RCU** (max ~250 µs). Adding `isolcpus` + RT priority stops **preemption** (~40 µs) but the **tick still fires** every ms. Only **`nohz_full` + `rcu_nocbs`** (tickless) + **IRQs routed away** (Ch. 45) get the core genuinely quiet (~5 µs), and the last bit comes from the **firmware/spec-exec layer** (Ch. 6 — C-states waking the core, mitigations). This is the canonical low-latency-tuning result and the verification loop: **`cyclictest` max is your jitter; each isolation knob removes one source; tune-measure-repeat until the max is flat.** The remaining spikes after full tuning point at the *last* noise sources (a stray IPI, a SMI from firmware — Ch. 6, an un-offloaded kernel thread) — chase them with `cyclictest -b` (break-trace) / `ftrace` and `/proc/interrupts`, or, properly, with the `rtla osnoise`/`timerlat` attribution workflow and the noise catalog of Ch. 49. A production latency box is signed off when `cyclictest` (and the real app's tail) shows a flat distribution under realistic load.

## 48.4 Techniques

### 48.4.1 CPU isolation and tickless cores

Building the quiet core — the boot-time and runtime isolation (the bulk of Appendix C):

- **`isolcpus` (or cpuset isolation).** Add `isolcpus=<hot-cores>` to the kernel cmdline (or use cgroup v2 `cpuset.cpus`/`cpuset.cpus.partition=isolated`) so the scheduler never auto-places or load-balances tasks onto the hot cores. Then pin your hot threads there explicitly (Ch. 45). This is the floor — without it, the kernel uses your "hot" cores for general work.
- **`nohz_full` + `rcu_nocbs` (tickless — the jitter killer).** Add `nohz_full=<hot-cores> rcu_nocbs=<hot-cores>` so the hot cores stop the periodic tick (when ≤1 task runs) and offload RCU callbacks. This removes the per-millisecond timer interrupt — the single biggest remaining jitter source after pinning (§48.3). **Keep exactly one runnable task per `nohz_full` core** (Ch. 31, 43) — a second task brings the tick back.
- **Route IRQs and kernel work off (Ch. 45).** `irqaffinity=<housekeeping>` on the cmdline + per-IRQ `smp_affinity` (disable `irqbalance`), and cgroups to confine system services/`kworker`s to housekeeping cores. Isolation from the *scheduler* (`isolcpus`) plus isolation from *interrupts* (Ch. 45) plus *tickless* (`nohz_full`) together make the core quiet — all three are needed.
- **Firmware/BIOS layer (Ch. 6).** Below the kernel: disable deep **C-states** (a core entering C6 and being woken adds latency — keep it in C1/C0), fix **P-states/turbo** (frequency changes are jitter), disable **SMIs** where possible (System Management Interrupts are invisible to the OS and a notorious jitter source — Ch. 6), handle **HT** (Ch. 46). Kernel tuning sits *on top of* a properly-tuned BIOS (Ch. 6); a stray SMI defeats perfect kernel isolation.
- **`tuned` / profiles.** Distros ship low-latency profiles (`tuned-adm profile latency-performance`/`realtime`, RHEL's `realtime` package) that bundle many of these settings; useful as a starting point, but verify each knob (and `cyclictest`) rather than trusting the profile blindly.
- **Verify the core is tickless/quiet.** Check `/proc/interrupts` (hot core columns ~flat — including `LOC` local-timer count not incrementing under `nohz_full`), `cat /sys/devices/system/cpu/cpuN/...`, and `cyclictest` (§48.3). A `nohz_full` core that *still* ticks (because a second task is on it, or a dependency isn't met) isn't quiet.

### 48.4.2 RT scheduling classes and priorities

Ensuring the hot thread is never preempted — and doing it *safely*:

- **Run the hot thread `SCHED_FIFO` at high priority.** `sched_setscheduler(0, SCHED_FIFO, &param)` (or `chrt -f 80`) so the hot thread runs ahead of all normal tasks and isn't preempted by timeslices or `SCHED_OTHER` work. On an isolated core there's little to preempt it, but RT priority guarantees correctness if *anything* (a kernel thread, a stray task) becomes runnable, and orders *multiple* RT threads deterministically. Set it in thread startup (with affinity — Ch. 45, FTZ/DAZ — Ch. 27, warming — Ch. 50).
- **Choose priorities deliberately.** Higher RT priority = runs first. Order your RT threads by latency-criticality (the tick-to-trade thread above the journaling thread, etc.), and know what a 100%-busy `SCHED_FIFO` thread *can* starve on its CPU: the `SCHED_OTHER` per-CPU kthreads (`ksoftirqd/N`, `kworker/N:*`), lower-priority RT kthreads such as `watchdog/N`, and on PREEMPT_RT the threaded `irq/*` handlers and `rcuc/N` — which is why those belong on housekeeping cores and why the throttle exists (§48.5). (`migration/N` runs in the *stop* class, above every RT priority, and cannot be starved.) Document the priority map.
- **`SCHED_FIFO` vs `SCHED_RR`.** FIFO runs until the thread yields/blocks/is preempted by higher priority (no timeslicing among equal priority — right for a single dominant hot thread). RR adds round-robin timeslicing among equal-priority threads (use if you have several equal RT threads that must share). For a busy-polling hot thread that runs continuously, **FIFO** is the norm.
- **Beware RT throttling (§48.5).** On kernels before 6.12 (or with `CONFIG_RT_GROUP_SCHED=y`) the RT throttle (`sched_rt_runtime_us`/`sched_rt_period_us`, default 950,000 of every 1,000,000 µs) *caps* RT-thread CPU to prevent a runaway RT thread from locking up the system — but a **busy-polling `SCHED_FIFO` thread runs 100%**, so the throttle *forcibly deschedules it* for the remaining ~50 ms of every second (a periodic, tens-of-milliseconds latency spike!). On a dedicated isolated core you typically **disable RT throttling** (`sched_rt_runtime_us = -1`) — *carefully*, because that removes the safety net (a buggy RT thread can then hang the box). From Linux 6.12 the throttle is replaced by the **fair deadline server** (`/sys/kernel/debug/sched/fair_server/cpuN/{runtime,period,defer}`), which preempts the RT thread only when fair-class tasks are actually runnable on that CPU — so a lone RT thread on a properly isolated core is no longer stalled at all, and the knob to check is that nothing `SCHED_OTHER` lands there. This is the key RT-tuning subtlety for busy-polling HFT.
- **Priority inheritance for shared locks (Ch. 32).** If an RT hot thread shares a lock with a lower-priority thread (it shouldn't on the hot path — Ch. 31, but control-plane interactions happen), use **priority-inheritance mutexes** (PI-futex / `PTHREAD_PRIO_INHERIT`) so the lock holder is temporarily boosted — preventing **priority inversion** (§48.5) where the RT thread blocks forever on a lock held by a thread that never gets to run.
- **`mlockall` + RT (Ch. 23, 26).** An RT thread that page-faults (Ch. 23) defeats its determinism (the fault is a syscall + possible I/O). `mlockall(MCL_CURRENT|MCL_FUTURE)` and pre-faulting (Ch. 26) are part of RT setup — RT priority without locked memory still spikes on a fault.

## 48.5 Pitfalls & anti-patterns: RT throttling, priority inversion

- **RT throttling descheduling a busy-poll thread (the classic RT-HFT trap).** A `SCHED_FIFO` busy-polling thread runs 100% CPU; on kernels before 6.12 (or with `CONFIG_RT_GROUP_SCHED=y`) the RT throttle (default 950 ms of every 1 s) will **forcibly stop it** for ~50 ms once per second — a periodic, tens-of-milliseconds stall that looks mysterious until you find it. On a dedicated isolated core, **disable RT throttling** (`sched_rt_runtime_us=-1`) or tune it — *knowing* you've removed the runaway-RT safety net (test thoroughly; a bug can now hang the box). From Linux 6.12 the fair deadline server replaces the throttle and fires only when `SCHED_OTHER` work is runnable on that CPU (§48.4.2) — so on a current kernel the fix is to keep fair-class tasks off the hot core, and the symptom to look for changes from a 1 Hz stall to "something else landed on my core".
- **Priority inversion.** An RT hot thread blocks on a lock (or resource) held by a *lower*-priority thread that — because it's lower priority and the RT thread is hogging the CPU — *never runs to release it*, so the RT thread waits forever (or a long time). Avoid shared locks on the RT/hot path (Ch. 31–32); where unavoidable, use **priority-inheritance** mutexes (§48.4.2).
- **`nohz_full` core with >1 task (tick comes back).** `nohz_full` only stops the tick when **one** task is runnable on the core; a second runnable task (a stray thread, a `kworker` you didn't offload) **re-enables the tick**, silently undoing the tickless benefit. (The SMT sibling is a *different* logical CPU to the scheduler: load there does not bring this CPU's tick back — it degrades the hot thread through shared core resources instead, Ch. 46.) Keep exactly one hot thread per isolated core (Ch. 31, 43) and offload kernel work (`rcu_nocbs`, cgroups).
- **Pinning/isolating without the firmware layer (Ch. 6).** Perfect kernel isolation is defeated by BIOS-level jitter: deep **C-states** waking the core, **turbo/P-state** frequency changes, and especially **SMIs** (invisible to the OS, can be hundreds of µs). Tune the BIOS (Ch. 6) *under* the kernel tuning; `cyclictest` spikes that survive kernel tuning often point at firmware.
- **Forgetting `mlockall` on RT threads (Ch. 23, 26).** RT priority guarantees scheduling, not memory residency — a page fault (Ch. 23) still spikes. `mlockall` + pre-fault (Ch. 26) is mandatory RT setup.
- **Over-tuning / breaking the system.** Isolating *too many* cores (leaving too few for housekeeping), disabling RT throttling without understanding it, setting a 100%-busy RT thread above the per-CPU kthreads it can starve (`ksoftirqd/N`, `kworker/N:*`, `watchdog/N`, threaded `irq/*` on PREEMPT_RT → softlockups and stalled I/O), or applying a `tuned` profile blindly — all can make the box *unstable* or *slower*. Tune deliberately, measure each change (`cyclictest`), and keep enough housekeeping capacity.
- **`isolcpus` deprecation / wrong interface.** `isolcpus` works but is semi-deprecated in favor of cgroup v2 `cpuset` isolated partitions on newer kernels; using the wrong/old interface (or both inconsistently) can give surprising behavior. Know your kernel's preferred isolation mechanism.
- **Assuming tuned once = tuned forever.** Kernel/distro updates, BIOS updates, and config drift can silently re-enable `irqbalance`, change `CONFIG_HZ`, reset RT throttling, or re-route IRQs. **Re-run `cyclictest` after any change** and monitor the production tail — and the isolation itself — continuously (Ch. 93, 95).
- **RT priority on a shared/general core without isolation.** Giving a thread `SCHED_FIFO` on a *non*-isolated core can *starve* the rest of the system (it preempts everything, including things the box needs) — RT priority belongs with isolation. Don't RT-prioritize on a general-purpose core casually.

## 48.6 Exercises & checklist

**Exercises**

1. **`cyclictest` the layers.** On a test box, run `cyclictest -p 80 -a <core> -D 60` (a) stock + busy, (b) pinned only (Ch. 45), (c) + `isolcpus` + RT, (d) + `nohz_full`/`rcu_nocbs` + IRQs away, (e) + BIOS C-states/mitigations (Ch. 6). Record max latency at each step. Reproduce the §48.3 progression — which knob removed the most?
2. **Catch RT throttling.** On a pre-6.12 kernel, run a `SCHED_FIFO` busy-polling thread (100% CPU) on an isolated core with RT throttling at default; measure its latency and look for the ~50 ms stall once per second (the throttle). On 6.12+, instead park a `SCHED_OTHER` spinner on the same core and watch the fair deadline server carve its slice out of your RT thread. Disable throttling (`sysctl kernel.sched_rt_runtime_us=-1`); confirm the spikes vanish. Understand what safety you gave up (§48.5).
3. **Tickless verification.** With `nohz_full=<core>`, run one task on the core and watch `/proc/interrupts` LOC (local timer) for that core — confirm it stops incrementing. Then add a *second* runnable task to the core and watch the tick come back (§48.5). 
4. **Priority inversion.** Construct an RT thread blocking on a mutex held by a low-priority thread that can't run (RT thread busy on the same core). Observe the stall. Switch to a `PTHREAD_PRIO_INHERIT` mutex and confirm the holder is boosted and the stall resolves (§48.4.2).
5. **Find the last spike.** After full tuning, if `cyclictest` still shows occasional spikes, use `cyclictest -b <us>` (break-trace) + ftrace, and `/proc/interrupts`, to identify the source (an IPI, an SMI — Ch. 6, an un-offloaded kthread). Eliminate it; re-measure. Then do it again the Ch. 49 way — `rtla osnoise -s <us> -t` — and compare how much faster the stopped trace names the culprit.

**Checklist — RT scheduling & kernel tuning**

- [ ] Hot cores are **isolated** (`isolcpus`/cpuset), **tickless** (`nohz_full` + `rcu_nocbs`), with **IRQs routed away** (`irqaffinity` + Ch. 45) and **exactly one hot thread per core** (so `nohz_full` actually stops the tick — Ch. 31, 43).
- [ ] Hot threads run **`SCHED_FIFO`** at deliberate, documented priorities (below critical kernel RT threads), set in thread startup with **affinity** (Ch. 45) and **`mlockall`** + pre-fault (Ch. 23, 26).
- [ ] **RT throttling is handled** for busy-polling threads (`sched_rt_runtime_us=-1` or tuned) — knowingly, with the runaway-RT risk understood and tested (§48.5).
- [ ] **Priority inversion** is avoided (no shared locks on the hot path — Ch. 31–32; **priority-inheritance** mutexes where unavoidable).
- [ ] The **BIOS/firmware layer** (Ch. 6) is tuned *under* the kernel: deep **C-states off**, **P-states/turbo** fixed, **SMIs** minimized, **HT** handled (Ch. 46).
- [ ] I **measured jitter with `cyclictest`** before/after each change (max latency = the tail — Ch. 1), drove the max down to single-digit µs under realistic load, and chased remaining spikes to their source.
- [ ] Everything-not-hot is **confined to housekeeping cores** (cgroups/`cpuset`), with **enough housekeeping capacity**; I didn't over-isolate or over-tune into instability.
- [ ] Tuning is **re-verified after kernel/BIOS/config changes** (no drift — `irqbalance` re-enabled, `CONFIG_HZ` changed, throttling reset) and the production tail — and the core's quietness — is **monitored continuously** (Ch. 93, 95); residual spikes are attributed with Ch. 49's catalog and tools, not tolerated.

## 48.7 References

- The Linux kernel documentation — `Documentation/admin-guide/kernel-parameters.txt` (`isolcpus`, `nohz_full`, `rcu_nocbs`, `irqaffinity`), `Documentation/timers/no_hz.rst`, and the RT-scheduling (`sched(7)`, `SCHED_FIFO`/`SCHED_DEADLINE`) docs — the mechanisms of §48.2, §48.4.
- The `rt-tests` / `cyclictest` documentation and the OSADL latency-plot methodology — measuring scheduling jitter (§48.3).
- The PREEMPT_RT documentation (mainline since Linux 6.12, `CONFIG_PREEMPT_RT`) and the kernel `Documentation/scheduler/sched-rt-group.rst` (RT throttling; superseded by the fair deadline server from 6.12) — RT scheduling and the throttling subtlety (§48.4.2, §48.5).
- Red Hat *Low Latency / Real-Time Tuning Guide* and the `tuned`/`tuned-adm` (`realtime`, `latency-performance`) documentation — the consolidated isolation/RT recipe (ties Appendix C).
- Carl Cook / HFT latency talks — isolation, RT, and `cyclictest`-driven tuning of the trading box.

## 48.8 Additional Reading

- The OSADL real-time wiki and `cyclictest` latency plots — extensive measured jitter data and methodology.
- Jon Masters' real-time Linux talks and the `nohz_full` design papers (Frederic Weisbecker) — full dynticks internals.
- Ch. 43 (*Context Switching*) — the preemption/interrupt jitter this eliminates; Ch. 45 (*Pinning*) — affinity/IRQ routing that pairs with isolation; Ch. 46 (*SMT*) — isolating the sibling; Ch. 6 (*System Setup*) — the BIOS layer below; Ch. 50 (*Warming*) — keeping the now-quiet core warm; Ch. 23/26 (*Memory/mmap*) — `mlockall` for RT.
- Ch. 47 (*Cache Allocation Technology & Intel RDT*) — the next step after pinning: partitioning the shared L3 so a neighbor can't evict the hot working set even when the core is isolated.
- **Appendix C** (System Tuning Checklist) — the full `isolcpus`/`nohz_full`/RT/IRQ/BIOS recipe consolidated; **Appendix E** — the context-switch cost this tuning avoids (the `cyclictest` figures themselves are §48.3's).

---

*Next: Ch. 49 — Hunting Residual Noise on Isolated Cores, the continuation of this chapter's `cyclictest` loop for when the max is *still* not flat: the catalog of what can still run on an `isolcpus`/`nohz_full` core (per-CPU kthreads, workqueues, RCU, IPIs, managed vectors, SMIs) with the fingerprint and knob for each, and the attribution toolkit — `rtla osnoise`/`timerlat`, `hwlat`, stop-on-threshold ftrace, `trace-cmd`, `perf sched` — that names the source of the last spike instead of guessing at it.*
