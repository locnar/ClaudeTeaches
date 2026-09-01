# Part VII — OS, Scheduling & Isolation

# Chapter 41 — Context Switching & Its Mitigation

> **Prerequisites:** Ch. 7 (caches — a switch evicts the working set), Ch. 12/15 (I-cache, TLB — also trashed), Ch. 46-preview (warming — a switch makes the hot path cold), Ch. 32 (spin vs block — the busy-poll decision), Ch. 1 (tail latency — a switch is a tail event), Ch. 6 (system setup — the OS knobs).
>
> **Leads into:** Ch. 42 (pinning — keep the thread on its core), Ch. 43 (SMT — the sibling), Ch. 45 (RT scheduling / `isolcpus`/`nohz_full` — eliminate preemption), Ch. 47 (syscalls/`epoll` — syscall cost). Opens **Part VII**, the OS-isolation foundation under the busy-polling hot path of the rest of the book.

---

## 41.1 Why it matters: a switch trashes the caches and TLB

A context switch — the OS taking the CPU away from your thread to run another — looks cheap in isolation (the *direct* cost, saving/restoring registers and running the scheduler, is ~1-5 µs) but is *catastrophically* expensive in its *indirect* cost: when your thread is switched out and later switched back in, it returns to a **cold** core. The other thread that ran in the meantime evicted your working set from L1/L2 (Ch. 7), your code from the L1i (Ch. 12), your translations from the TLB (Ch. 15), and your history from the branch predictors (Ch. 13–14). So the first hundreds of memory accesses after you resume are cache misses, TLB misses, and mispredicts — *thousands* of cycles of cold-start penalty (Ch. 46) on top of the few-microsecond direct cost. A context switch on the hot path doesn't cost you 2 µs; it costs you 2 µs *plus* a cold restart that can be tens of microseconds of degraded execution. For a tick-to-trade path budgeted in hundreds of nanoseconds, **a single context switch is a complete latency blowout**, and it lands in the tail (Ch. 1) unpredictably.

This reframes the entire OS-scheduling question for low latency. The general-purpose OS is *designed* to context-switch constantly — that's how it shares a few cores among hundreds of threads fairly. But a latency-critical trading thread does *not* want to be a fair citizen sharing a core; it wants to **own a core completely and never be switched off it.** Every involuntary preemption (the scheduler deciding another thread should run), every migration (the scheduler moving your thread to a different core, instantly cold), every syscall that blocks (putting you to sleep and requiring a wake-up switch — Ch. 32), every interrupt handled on your core (Ch. 42) — each is a context switch or a cache-trashing event that the hot path must eliminate. This is the through-line of Part VII: **take a core away from the OS's general scheduling, pin the hot thread to it (Ch. 42), eliminate everything else that could run or interrupt there (Ch. 42, 45), and keep the thread running continuously — busy-polling rather than blocking (§41.4.2) — so it's never switched out and never goes cold.**

The companion cost is **syscalls**. A system call crosses from user space into the kernel — a privilege transition that's gotten more expensive with speculative-execution mitigations (Ch. 6, the KPTI page-table switch), costing ~hundreds of ns to over a microsecond *each*, and a *blocking* syscall (a blocking `read`, a `futex` wait — Ch. 32) additionally puts the thread to sleep, forcing a context switch. So the hot path must be **syscall-free in steady state** (Ch. 23's no-malloc, no-I/O-on-the-hot-path discipline restated at the OS level): all syscalls happen at setup; the steady-state loop does no `read`/`write`/`malloc`/lock-that-blocks — it busy-polls user-space queues (Ch. 34, 37) and memory-mapped NIC rings (Ch. 55, 62). This chapter measures the direct and indirect switch costs and the syscall cost (§41.3), gives the keep-on-core and busy-poll-vs-block techniques (§41.4), and warns about involuntary preemption and hidden syscalls (§41.5) — establishing the OS-isolation foundation that pinning (Ch. 42), SMT control (Ch. 43), and RT/`isolcpus` tuning (Ch. 45) build on.

## 41.2 Mental model: direct vs indirect costs; syscall overhead

**A context switch has two costs, and the indirect one dominates:**

```
   DIRECT cost (~1-5 µs): the switch mechanism itself
     - save outgoing thread's registers, run the scheduler, load incoming thread's registers
     - (with KPTI / spec-exec mitigations — Ch. 5 — a page-table switch on kernel entry adds more)

   INDIRECT cost (often FAR larger): the cold restart when YOUR thread resumes
     - L1/L2 data cache evicted by the other thread       → hundreds of misses (Ch. 6)
     - L1i / µop cache evicted                            → front-end stalls (Ch. 11)
     - TLB flushed (or polluted)                          → page-walk storms (Ch. 14)
     - branch predictors polluted                         → mispredicts (Ch. 12-13)
     → the resumed thread runs COLD for thousands of cycles (Ch. 43) — the real cost
```

The mental correction: **never think of a context switch as "a few microseconds." Think of it as "a few microseconds *plus* a cold-start that degrades the next ~10-100 µs of execution."** This is why even *infrequent* switches wreck the tail, and why eliminating them (not just minimizing them) is the goal.

**Kinds of switches — what causes them:**

- **Voluntary** — your thread *gives up* the CPU: a blocking syscall (blocking `read`, `futex` wait when a lock is contended — Ch. 32, `sleep`, blocking I/O), or `yield`. The hot path avoids these by busy-polling (§41.4.2).
- **Involuntary (preemption)** — the *scheduler* takes the CPU: your timeslice expired, a higher-priority thread became runnable, or the scheduler load-balances. Eliminated by isolating the core (`isolcpus`/`nohz_full` — Ch. 45) so nothing else is scheduled there, and by RT priority (Ch. 45).
- **Migration** — the scheduler moves your thread to a *different* core (instantly cold — none of your working set is there). Eliminated by **pinning** (affinity — Ch. 42).
- **Interrupt-driven** — a hardware interrupt (timer tick, NIC IRQ, IPI) handled on your core steals cycles and pollutes caches, and can trigger a reschedule. Eliminated by IRQ affinity (route IRQs away — Ch. 42) and `nohz_full` (stop the timer tick — Ch. 45).

**Syscall overhead.** A syscall is a user→kernel privilege transition:

- **The transition itself** (~100s of ns, more with KPTI/mitigations — Ch. 6): switch to kernel stack, save state, (KPTI) switch page tables, run the handler, return. Even a *non-blocking* "null" syscall (`getpid`) costs this.
- **A *blocking* syscall additionally context-switches** (sleeps the thread, schedules another, wakes later) — the worst case, combining syscall + switch + cold restart.
- **The hot-path rule:** steady-state hot loops make **zero syscalls** — no I/O, no `malloc` (which can `mmap`/`brk` — Ch. 23), no blocking lock (Ch. 32). Syscalls are for setup/teardown. The hot path talks to other threads/processes via user-space lock-free queues (Ch. 34, 37) and to the NIC via kernel-bypass/mapped rings (Ch. 55, 62) — *no kernel crossing per message*.

The model: **a context switch costs a small direct fee plus a large cold-restart penalty (the dominant cost); the hot path eliminates switches (pin to avoid migration, isolate to avoid preemption, route IRQs away, busy-poll to avoid blocking) and eliminates syscalls (setup-only; user-space queues + kernel bypass in steady state) so the thread runs continuously on a warm, owned core.**

## 41.3 Measure it: context-switch and null-syscall cost

Measure the three costs: a **null syscall** (the kernel-crossing floor), a **context switch** (direct + indirect), and — the killer — the **cold-restart penalty** after a switch. Use the classic ping-pong (two threads bouncing a token via a pipe/futex forces switches) and a syscall micro-loop.

```cpp
// ctxsw.cpp — null syscall cost, and context-switch cost via a futex ping-pong.
// Build: g++ -O2 -std=c++20 ctxsw.cpp -o ctxsw -pthread
// Run pinned:  taskset -c 2,3 ./ctxsw
#include <cstdio>
#include <cstdint>
#include <atomic>
#include <thread>
#include <chrono>
#include <unistd.h>
#include <sys/syscall.h>
#include <linux/futex.h>

static long futex(std::atomic<int>* a, int op, int val) {
    return syscall(SYS_futex, (int*)a, op, val, nullptr, nullptr, 0);
}

int main() {
    constexpr long N = 2'000'000;

    // (1) null syscall cost (getpid is a trivial kernel crossing)
    auto t0 = std::chrono::steady_clock::now();
    for (long i = 0; i < N; ++i) syscall(SYS_getpid);
    auto t1 = std::chrono::steady_clock::now();
    double sys_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count() / (double)N;

    // (2) context switch: two threads ping-pong via futex (each exchange = 2 switches)
    std::atomic<int> turn{0};
    auto t2 = std::chrono::steady_clock::now();
    std::thread other([&]{ for (long i = 0; i < N; ++i) {
        while (turn.load() != 1) futex(&turn, FUTEX_WAIT, turn.load());  // sleep until our turn
        turn.store(0); futex(&turn, FUTEX_WAKE, 1); } });                // wake the other (switch)
    for (long i = 0; i < N; ++i) {
        turn.store(1); futex(&turn, FUTEX_WAKE, 1);
        while (turn.load() != 0) futex(&turn, FUTEX_WAIT, turn.load());
    }
    other.join();
    auto t3 = std::chrono::steady_clock::now();
    double cs_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t3-t2).count() / (double)(2*N);

    std::printf("null syscall: %.0f ns   context switch (round-trip/2): ~%.0f ns\n", sys_ns, cs_ns);
    std::printf("(perf stat -e context-switches,cache-misses ./ctxsw to see the indirect cost)\n");
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP), pinned, turbo off (illustrative; mitigations on/off changes the syscall number a lot — Ch. 6):

```
   null syscall (getpid)              ~80-400 ns      kernel crossing (higher with KPTI/mitigations — Ch. 5)
   context switch (direct, measured)  ~1-3 µs         the futex ping-pong round-trip / 2
   + cold-restart (indirect)          +5-50 µs        the resumed thread's working set is GONE (Ch. 6,11,14,43)
                                                       (measure via cache-misses/cycles after resume)

   the hot path's budget is ~hundreds of ns → ANY of these is a blowout.
```

Read it: a **null syscall** alone (~80-400 ns, and *much* worse with spec-exec mitigations — Ch. 6) already rivals a whole hot-path budget — which is why steady-state must be syscall-free. A **context switch** is ~1-3 µs of *direct* cost in this micro-measurement, but the micro-measurement *understates* it: in the ping-pong both threads stay roughly warm (tiny working set), so it hides the **indirect** cost — in a real system the resumed thread returns to a core whose caches/TLB/predictors were trashed by whatever ran in between, adding *tens of microseconds* of cold-start degradation (Ch. 46) that you see as a spike in `cache-misses`/`cycles` after resume, not in the switch count. The lesson the numbers force: **on a path budgeted in hundreds of nanoseconds, a syscall, a switch, or a migration is a 10-100× blowout — so the hot path must have *zero* of them in steady state.** Confirm it with `perf stat -e context-switches,migrations` on the hot thread: the target is **zero involuntary switches and zero migrations** during trading (Ch. 42, 45 deliver that). If you see non-zero, hunt down the cause (a hidden syscall, a stray IRQ, a missing pin).

## 41.4 Techniques

### 41.4.1 Keeping threads on-core

The first half of eliminating switches: make the hot thread *own* its core and never leave it (the detail is Ch. 42, 45; the principle here):

- **Pin the thread (affinity — Ch. 42).** `sched_setaffinity`/`pthread_setaffinity_np` (or `taskset`) bind the hot thread to a specific core so the scheduler can't **migrate** it (migration = instant cold). Affinity is the floor; without it the scheduler moves threads around for load-balancing, trashing the working set.
- **Isolate the core from the scheduler (`isolcpus`/`nohz_full`/`rcu_nocbs` — Ch. 45).** Remove the core from the general scheduler's pool (`isolcpus`) so *nothing else* is scheduled there → no **involuntary preemption**; stop the periodic timer tick on it (`nohz_full`) → no timer-interrupt switch; offload RCU callbacks (`rcu_nocbs`) → no RCU work on the core. The core becomes effectively private to your thread.
- **Route interrupts away (IRQ affinity — Ch. 42).** Pin device/timer IRQs to *housekeeping* cores, not the hot core, so interrupt handling doesn't steal cycles or pollute the hot core's caches (Ch. 42).
- **RT priority where needed (Ch. 45).** `SCHED_FIFO`/`SCHED_RR` with high priority ensures that if anything *does* contend for the core, your thread wins — and prevents lower-priority work from preempting it. On a properly isolated core there's nothing to contend, but RT priority is belt-and-suspenders (and essential on shared cores).
- **One hot thread per core.** The shared-nothing pipeline (Ch. 31, 37) maps one pinned thread per isolated core; **no oversubscription** (more busy threads than cores forces switching — §41.5, Ch. 31). The core budget bounds the number of hot threads.

### 41.4.2 Busy-poll vs block

The second half: eliminate *voluntary* switches by **busy-polling instead of blocking** — the defining choice for a dedicated-core hot path (ties Ch. 32, 37):

- **Busy-poll: spin checking for work, never sleep.** The hot thread loops checking its input (a user-space lock-free queue — Ch. 34, the NIC's mapped RX ring — Ch. 55/62, the Disruptor sequence — Ch. 37) and processes work the instant it appears — **never** calling a blocking syscall, never sleeping, never yielding. So it's **never switched out** (no voluntary switch) and stays warm (Ch. 46) and ready. This is the right model on a *dedicated, isolated* core (Ch. 45): the core is yours, there's no other work to yield to, so spinning is not waste — it's the lowest-latency way to wait (Ch. 32's spin-vs-block logic at the scheduling level).
- **The cost: 100% CPU, always.** A busy-polling thread pegs its core at 100% forever (and burns power, generates heat — and on SMT affects the sibling — Ch. 43). That's an *accepted* cost for a dedicated hot core (you bought the core to do exactly this), but it means you can't busy-poll on a shared/general-purpose core without starving everything else.
- **Block on cold/background paths.** Threads that are *not* latency-critical — control plane, admin, logging flush (Ch. 71), housekeeping — should **block** (`epoll_wait` — Ch. 47, condition variables), yielding their core to other work, exactly as a general server would. Busy-poll is *only* for the hot path; everything else blocks. Match the strategy to the thread's role (the hot/cold split of Ch. 1 at the scheduling level).
- **`epoll`/io_uring with busy-poll modes (Ch. 47–48, 55).** Where you do use the kernel I/O stack on a latency path, Linux offers **busy-poll** socket options (`SO_BUSY_POLL`, `napi` busy-polling) and io_uring polled modes (Ch. 48) that *poll* for completions instead of sleeping+interrupt — a middle ground that avoids the block/wake switch while still using the kernel. Kernel bypass (Ch. 62) removes even that.
- **Hybrid wait strategies (Ch. 37).** When a core *isn't* fully dedicated, a hybrid — spin for a short while, then block — caps the wasted spinning while keeping low latency for the common (work-arrives-soon) case. The Disruptor's wait strategies (Ch. 37.4.2) are exactly this knob.

## 41.5 Pitfalls & anti-patterns: involuntary preemption, syscalls on the hot path

- **Involuntary preemption on a non-isolated core.** Running the hot thread on a core the general scheduler manages → it gets preempted by other threads/timeslices, going cold each time (§41.2). **Isolate the core** (`isolcpus`/`nohz_full` — Ch. 45) and pin (Ch. 42); verify **zero involuntary context-switches** (`perf stat`) during trading.
- **Thread migration (no pinning).** Without affinity, the scheduler migrates the hot thread to a different core — instantly cold (none of its working set is there) and unpredictable. **Pin every hot thread** (Ch. 42); check **migrations == 0**.
- **Syscalls on the hot path.** A blocking `read`/`write`, a `malloc` that hits the kernel (`mmap`/`brk` — Ch. 23), a contended lock that `futex`-waits (Ch. 32), a `clock_gettime` that's *not* vDSO (Ch. 17), a log call that writes (Ch. 71) — each is a kernel crossing (~hundreds of ns+) and possibly a blocking switch. **Steady-state hot loops make zero syscalls**; everything I/O is user-space queues (Ch. 34) + kernel bypass (Ch. 55, 62), and timestamps are vDSO/`rdtsc` (Ch. 17).
- **Oversubscription (Ch. 31).** More busy/runnable threads than cores forces the scheduler to time-slice them → constant switching and cold restarts. **One hot thread per dedicated core, no more.** Don't run a fat thread pool on the hot cores.
- **Busy-polling on a shared core.** Busy-poll pegs a core at 100%; doing it on a *non-dedicated* core starves other work and may itself get preempted (defeating the point). Busy-poll **only** on isolated/dedicated cores; block on shared ones (§41.4.2).
- **Blocking on the hot path "to be nice."** Yielding/sleeping/blocking on the dedicated hot core to "free the CPU" is backwards — there's no other work to free it for, and the block/wake costs a switch + cold restart. On a dedicated core, **busy-poll** (Ch. 32's spin-vs-block, restated).
- **Hidden interrupts on the hot core (Ch. 42).** Even isolated, a core can still take the timer tick (without `nohz_full`), NIC IRQs (without IRQ affinity), or IPIs — each steals cycles and pollutes caches. Route IRQs away, `nohz_full` the core (Ch. 42, 45).
- **`SCHED_OTHER` (default) for the hot thread on a contended system.** The default fair scheduler will preempt your thread; on a shared system use RT priority (`SCHED_FIFO` — Ch. 45). (On a fully isolated core it matters less, but it's cheap insurance.)
- **Mitigation cost ignored (Ch. 6).** Spectre/Meltdown mitigations (KPTI, retpolines) make *every* syscall and switch more expensive; on an isolated, syscall-free hot core you may disable some (`mitigations=off` on a trusted box — Ch. 6) to cut the cost — a deliberate, measured security/latency trade.

## 41.6 Exercises & checklist

**Exercises**

1. **Measure the costs.** Build `ctxsw.cpp` (pin to two cores); record null-syscall and context-switch costs. Then build with mitigations on vs off (`mitigations=off` boot param — Ch. 6, test box) and re-measure the syscall — how much do mitigations add (§41.3)?
2. **See the indirect cost.** Pin a thread that streams a large array (working set > L2). Periodically force it off-core (run a cache-thrashing thread on the same core). Measure the latency *spike* right after each preemption (cold restart) with `perf` (cache-misses) — confirm the indirect cost dwarfs the direct (§41.2).
3. **Zero switches.** Pin a busy-polling thread to a core; run `perf stat -e context-switches,cpu-migrations` for 10s. Now isolate the core (`isolcpus`/`nohz_full` — Ch. 45) and repeat. Get the involuntary switches and migrations to **zero** (§41.4.1).
4. **Busy-poll vs block latency.** Build a producer→consumer where the consumer (a) `futex`-blocks waiting for work vs (b) busy-polls. Measure wake-to-process latency for each. Quantify the block/wake switch cost; confirm busy-poll wins on a dedicated core (§41.4.2, Ch. 32).
5. **Hunt a hidden syscall.** Take a "syscall-free" hot loop and trace it with `strace -f -c` (or a bpftrace syscall counter — Ch. 61). Find a sneaky syscall (a log line, a `malloc`, a non-vDSO clock). Eliminate it; confirm `strace` shows none on the steady-state loop.

**Checklist — context switching & its mitigation**

- [ ] Hot threads are **pinned** (no migration — Ch. 42) to **isolated cores** (`isolcpus`/`nohz_full`/`rcu_nocbs` — Ch. 45); **`perf stat` shows zero involuntary context-switches and zero migrations** during trading.
- [ ] The steady-state hot loop makes **zero syscalls** — no blocking I/O, no `malloc`-to-kernel (Ch. 23), no `futex`-blocking lock (Ch. 32), no non-vDSO clock (Ch. 17), no logging-that-writes (Ch. 71).
- [ ] The hot path **busy-polls** (user-space queues — Ch. 34, mapped NIC rings — Ch. 55/62, Disruptor sequences — Ch. 37) on a **dedicated** core — it never blocks/sleeps/yields.
- [ ] **Cold/background** threads (control, admin, logging flush) **block** (`epoll`/cond-var) and live on **housekeeping** cores — busy-poll is hot-path only.
- [ ] **No oversubscription** — one hot thread per dedicated core (Ch. 31); IRQs are **routed away** from hot cores (Ch. 42) and the **timer tick** is off (`nohz_full` — Ch. 45).
- [ ] I treated a switch as **direct + cold-restart** cost (Ch. 46) — eliminating switches, not just minimizing — and measured the **indirect** penalty (post-resume cache misses), not just the count.
- [ ] RT priority (`SCHED_FIFO` — Ch. 45) is set where the core could be contended; **spec-exec mitigation cost** (Ch. 6) is a deliberate, measured choice on the isolated box.
- [ ] Where the kernel I/O path is on a latency thread, **busy-poll modes** (`SO_BUSY_POLL`/io_uring polled — Ch. 47–48) or **kernel bypass** (Ch. 62) avoid the block/wake switch.

## 41.7 References

- The Linux kernel scheduler documentation and `sched(7)`/`sched_setaffinity(2)`/`SCHED_FIFO` man pages — context switches, preemption, affinity (the mechanisms of §41.2, §41.4).
- U. Drepper, *What Every Programmer Should Know About Memory* — the cache/TLB cost of losing the core (the indirect cost of §41.2).
- Intel *SDM* / *Optimization Reference Manual* and the KPTI / spec-exec mitigation writeups — syscall/transition cost and how mitigations inflate it (§41.3, ties Ch. 6).
- The `perf` documentation — `context-switches`, `cpu-migrations`, and measuring the indirect (cache-miss) cost of a switch (§41.3).
- Carl Cook, *"When a Microsecond Is an Eternity"* (CppCon) — the busy-poll, syscall-free, pinned hot-path discipline in HFT (the whole chapter's ethos).

## 41.8 Additional Reading

- The `cyclictest`/rt-tests documentation (ties Ch. 45) — measuring scheduling latency and jitter from preemption/interrupts.
- Red Hat / kernel low-latency tuning guides — isolating cores, `nohz_full`, and eliminating switches (consolidated in Appendix C).
- Ch. 42 (*Thread & Interrupt Pinning*) — the affinity/IRQ mechanisms; Ch. 43 (*SMT*) — the sibling's effect; Ch. 45 (*RT Scheduling*) — `isolcpus`/`nohz_full`/RT priority/`cyclictest`; Ch. 46 (*Warming*) — the cold-restart this chapter's switches cause; Ch. 47–48 (*epoll/io_uring*) — syscall/busy-poll I/O; Ch. 62 (*Kernel Bypass*) — removing the kernel from the hot path entirely.
- **Appendix C** (System Tuning Checklist) — the `isolcpus`/`nohz_full`/IRQ/governor settings; **Appendix E** — context-switch and null-syscall latency numbers.

---

*Next: Ch. 42 — Thread & Interrupt Pinning, the concrete mechanics behind "keep the thread on-core": CPU affinity (`taskset`/`sched_setaffinity`), IRQ affinity, and isolating the hot core so nothing — no thread, no interrupt — shares it.*
