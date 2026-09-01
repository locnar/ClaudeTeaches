# Part VI — Concurrency

# Chapter 32 — Spinlocks, Backoff & Contention Control

> **Prerequisites:** Ch. 30 (atomics & memory ordering — a spinlock is an atomic acquire/release protocol), Ch. 31 (shared-nothing — a spinlock is for the sharing you *couldn't* eliminate), Ch. 7 & 32-preview (cache coherence / false sharing — the lock line bounces), Ch. 41 (context switching — what blocking costs), Ch. 6/42 (isolated/pinned cores — where spinning is viable).
>
> **Leads into:** Ch. 33 (false sharing — padding the lock), Ch. 34 (lock-free — the alternative when even a good spinlock contends), Ch. 37 (Disruptor — busy-spin waiting), Ch. 41, 42, 43, 45 (scheduling — why spinning needs an isolated core). Futex internals tie to Ch. 47 (syscalls).

---

## 32.1 Why it matters: spin vs block on a hot core

When two threads genuinely must coordinate on shared state (the sharing you couldn't design away in Ch. 31), you need a lock — and the first, most consequential decision is **spin or block**. A blocking lock (`std::mutex`) that's contended puts the waiting thread to *sleep* via a kernel syscall (a futex wait), and wakes it later with another syscall — and that sleep/wake cycle costs **microseconds** (the syscall, the context switch — Ch. 41, the scheduler latency, the cold cache on wake-up — Ch. 12/46). On a path budgeted in nanoseconds, blocking is a catastrophe if the lock is held only briefly: you pay microseconds of OS machinery to avoid spinning for what would have been tens of nanoseconds. A **spinlock** instead *busy-waits* — the thread loops checking the lock until it's free, never entering the kernel — so a short critical section costs just the spin (tens of ns), no syscall, no context switch, no jitter.

This makes spinning the right choice for HFT's specific situation: **short critical sections on dedicated, isolated cores** (Ch. 6, 42, 45). If the hot thread owns its core (nothing else is scheduled there — `isolcpus`/pinning) and the critical section is microscopic, spinning is pure win — the lock is almost always free, and on the rare contention the spin is shorter than a syscall would be. The blocking-lock logic ("don't waste CPU spinning, let other work run") is *backwards* on an isolated core: there *is* no other work to run, the core is dedicated to this thread, so spinning isn't waste — it's the lowest-latency way to wait. This is why low-latency code is full of spinlocks and busy-polling (Ch. 37, 41) where general-purpose code would block.

But spinning is dangerous if misused, and this chapter is as much about *contention control* as about the lock itself. A naive spinlock (every waiter hammering the lock's cache line) creates exactly the coherence-traffic catastrophe of Ch. 31 — the line ping-pongs among all spinners, starving the holder. The cures are a layered toolkit: **test-and-test-and-set** (spin on a *read* until the lock looks free, only then attempt the atomic — Ch. 7/33), the **`pause` instruction** (tell the CPU you're spinning so it doesn't waste power/pipeline and doesn't mis-speculate), **exponential backoff** (back off when contended to reduce the hammering), and **queue locks** (MCS/CLH — each waiter spins on its *own* cache line, eliminating the bounce) for higher contention. And — the crucial caveat (§32.5) — spinning is only safe when the holder can't be *preempted*: spinning while waiting for a thread the scheduler descheduled (on a shared core) wastes a whole timeslice (the "spinning across the scheduler" disaster). So the chapter's discipline: **spin for short sections on isolated cores with proper backoff/`pause`; block (or, better, go lock-free — Ch. 34) otherwise** — and know exactly which situation you're in.

## 32.2 Mental model: test-and-test-and-set; `pause`/`tpause`; futex internals

**Spin vs block — the cost model.** A lock acquisition that finds the lock free is cheap either way (one atomic). The difference is what happens when it's *held*:

```
   spinlock (contended):   loop { check lock }  until free   — busy-wait, NO kernel, ~tens of ns
                           good IF: section is short AND the core is dedicated (holder runs)
   blocking mutex (contended): futex_wait syscall → SLEEP → (holder unlocks) → futex_wake → wake
                           ~microseconds: syscall + context switch (Ch. 39) + cold cache on wake
                           good IF: section is long OR the core is shared (let other work run)
```

The decision: **spin when the expected wait < the cost of a context switch (~1-3 µs) AND the core isn't needed for other work; block when the wait is long or the core is shared.**

**The naive spinlock and its problem.** A test-and-set (TAS) spinlock loops doing an atomic exchange:

```cpp
while (lock.exchange(1, acquire)) { /* spin */ }   // every iteration WRITES the lock line
```

Every spinner's `exchange` is a *write* (an atomic RMW), so every iteration takes the cache line *exclusive* — and with N spinners all writing, the line ping-pongs furiously among them (Ch. 7's HITM), starving the holder (who needs the line to *release*) and wasting bandwidth. This is the contention catastrophe of Ch. 31 in lock form.

**Test-and-test-and-set (TTAS) — spin on a read.** The fix: spin on a *read* (which keeps the line in *shared* state across all spinners — no bouncing) and only attempt the atomic write when the lock *looks* free:

```cpp
while (true) {
    while (lock.load(relaxed)) pause();          // spin on READ — line stays shared, no bounce
    if (!lock.exchange(1, acquire)) break;       // only WRITE when it looks free (then retry if lost)
}
```

Now spinners share the line read-only; only the actual handoff causes a coherence transfer. This is the baseline correct spinlock (§32.4.1).

**The `pause` instruction (and `tpause`).** Inside a spin loop, `pause` (`_mm_pause()`) tells the CPU "I'm spin-waiting": it (1) hints the pipeline to not aggressively speculate the loop (avoiding a costly memory-order-violation flush when the lock changes — Ch. 11), (2) reduces power, and (3) on SMT (Ch. 43) yields pipeline resources to the sibling thread. It's essentially free and **mandatory** in a spin loop — omitting it both wastes power and *slows* the spin. `tpause`/`umwait`/`umonitor` (newer ISA) let a core wait on a memory location with a timeout in a low-power state — a more efficient "wait until this address changes" for spin-waiting.

**Futex internals (how blocking locks work).** A `std::mutex` is built on the Linux **futex** (fast userspace mutex): the *uncontended* case is a single atomic in userspace (no syscall — fast); only on *contention* does it call `futex(FUTEX_WAIT)` to sleep and `futex(FUTEX_WAKE)` to wake a waiter. So a `std::mutex` is cheap when uncontended (just an atomic) and expensive only when it actually has to block — meaning the spin-vs-block question is really "how often, and how long, is it contended?" Many mutexes also **spin briefly before blocking** (an adaptive mutex) to get the best of both. Understanding the futex shows why "mutexes are slow" is only true *under contention*.

The model: **a lock is an atomic acquire/release protocol (Ch. 30); spinning busy-waits (cheap for short sections on dedicated cores) while blocking sleeps via futex (cheap uncontended, microseconds when it blocks). A correct spinlock spins on a *read* (TTAS) with `pause`, and adds backoff/queueing as contention rises — choosing spin vs block by section length and whether the core is dedicated.**

## 32.3 Measure it: spin vs mutex under contention

Measure the two crossovers: (1) spin vs `std::mutex` as a function of **critical-section length** and **contention**, and (2) the effect of TTAS + `pause` + backoff vs a naive TAS spinlock. The headline: spin wins for short sections, loses (or wastes CPU) for long ones; naive spin collapses under contention while TTAS+backoff holds.

```cpp
// spin.cpp — naive TAS vs TTAS+pause spinlock vs std::mutex, under contention.
// Build: g++ -O2 -std=c++20 -march=native spin.cpp -o spin -pthread
// Run:  ./spin tas 8 | ./spin ttas 8 | ./spin mutex 8   (threads pinned to distinct cores)
#include <atomic>
#include <mutex>
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <thread>
#include <vector>
#include <chrono>
#include <immintrin.h>   // _mm_pause

struct TAS  { std::atomic<int> f{0};
    void lock(){ while (f.exchange(1, std::memory_order_acquire)) {} }       // naive: spin on WRITE
    void unlock(){ f.store(0, std::memory_order_release); } };
struct TTAS { std::atomic<int> f{0};
    void lock(){ for(;;){ while (f.load(std::memory_order_relaxed)) _mm_pause();  // spin on READ + pause
                          if(!f.exchange(1, std::memory_order_acquire)) return; } }
    void unlock(){ f.store(0, std::memory_order_release); } };

std::uint64_t shared_counter = 0;                 // the "critical section" protects this

template <class Lock>
double run(int M, Lock& lk) {
    constexpr long TOTAL = 20'000'000; long per = TOTAL / M;
    auto t0 = std::chrono::steady_clock::now();
    std::vector<std::thread> ts;
    for (int t=0;t<M;++t) ts.emplace_back([&]{
        for (long i=0;i<per;++i){ lk.lock(); shared_counter++; /* tiny CS */ lk.unlock(); }
    });
    for (auto& th:ts) th.join();
    auto t1 = std::chrono::steady_clock::now();
    return std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count() / (double)TOTAL;
}

int main(int argc, char** argv){
    const char* mode = argc>1?argv[1]:"ttas"; int M = argc>2?std::atoi(argv[2]):8;
    double ns;
    if(!std::strcmp(mode,"tas")){ TAS l; ns=run(M,l); }
    else if(!std::strcmp(mode,"mutex")){ struct MX{std::mutex m; void lock(){m.lock();} void unlock(){m.unlock();}} l; ns=run(M,l); }
    else { TTAS l; ns=run(M,l); }
    std::printf("%-6s M=%d  %.1f ns/critical-section (counter=%llu)\n",
                mode, M, ns, (unsigned long long)shared_counter);
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP, single socket), threads pinned to distinct cores, turbo off, *tiny* critical section (illustrative):

```
   M=8 cores, tiny critical section:
   TAS (naive spin-on-write)   ~900 ns/CS    line ping-pongs among all 8 writers — catastrophic
   TTAS (+pause)               ~250 ns/CS    spin on read; only handoff bounces — much better
   std::mutex                  ~600 ns/CS    futex: brief adaptive spin then syscall under contention

   crossover (single holder, varying CS length, low contention):
   short CS (~50 ns)   spin wins (no syscall)
   long  CS (~5 µs)    block wins (spinning wastes the core; let it sleep)
```

Read it: under contention with a tiny critical section, **naive TAS is a disaster** (~900 ns — every spinner writes the line, the holder can barely get it back to release), **TTAS+`pause` is ~3.6× better** (spinners share the read-only line; only the handoff transfers ownership), and `std::mutex` sits in between (its adaptive spin + futex). The crossover row is the spin-vs-block rule: for a *short* section spinning beats the mutex's syscall; for a *long* section the mutex's sleep beats burning a core spinning. The lessons: (1) **never use naive TAS** — always TTAS + `pause` (§32.4.1); (2) **spin for short sections on dedicated cores, block for long ones**; and (3) even TTAS degrades with more contenders — beyond a few cores, add **backoff** (§32.4.2) or a **queue lock** (§32.4.3), or reconsider whether you should be locking at all (lock-free — Ch. 34, or shared-nothing — Ch. 31). The fundamental cost is the lock line's coherence traffic; everything here is about reducing how much it bounces.

## 32.4 Techniques

### 32.4.1 TTAS spinlocks

The correct baseline spinlock (§32.2, §32.3):

- **Spin on a read, write only to acquire.** The inner loop reads the lock (`load(relaxed)`) until it *looks* free — keeping the line in shared state across all spinners, no coherence bounce — then attempts the atomic `exchange`/`compare_exchange` to actually take it (retrying if another spinner won the race). Never spin on the atomic write (the naive TAS disaster).
- **`pause` in the spin loop — mandatory.** `_mm_pause()` every spin iteration: avoids the memory-order-violation pipeline flush when the lock changes (Ch. 11), saves power, and yields to the SMT sibling (Ch. 43). Omitting it both wastes energy and *slows* the spin (the flush penalty).
- **Acquire/release ordering (Ch. 30).** Acquire on the lock (so the critical section's reads can't hoist above it), release on the unlock (so the critical section's writes are published before the lock frees). This is the publication pattern of Ch. 30; `seq_cst` is unnecessary.
- **Pad the lock to a cache line (Ch. 33).** The lock word must not share a cache line with the data it protects (or other locks) — false sharing makes every data access bounce the lock line. `alignas(hardware_destructive_interference_size)`.
- **When TTAS is enough.** Short critical sections, low-to-moderate contention, dedicated cores — TTAS+`pause` is simple, fast, and correct. Add backoff/queueing only when measurement (§32.3) shows TTAS degrading under your contention.

### 32.4.2 Exponential backoff

When contention is high enough that even TTAS spinners contend on the handoff, **back off** — wait progressively longer between attempts to reduce hammering:

- **The idea.** On each failed acquire, spin-wait a *growing* number of `pause` iterations (1, 2, 4, 8, … up to a cap) before re-checking — so contenders spread out their attempts, cutting the coherence traffic and giving the holder room to release and the next waiter a clean shot. Like network backoff (Ch. 51), applied to the lock.
- **Bounded and randomized.** Cap the backoff (so latency doesn't blow up) and optionally add randomization (jitter) to de-synchronize contenders that would otherwise back off in lockstep. The cap trades fairness/latency against contention reduction — tune it to your contention level (§32.3).
- **Backoff vs queue locks.** Backoff is simple and stateless (good for moderate contention) but *unfair* (a thread can be unlucky repeatedly — starvation/latency tail) and still has all contenders touching the lock line. For *high* contention or fairness needs, a queue lock (§32.4.3) is better. Backoff is the easy upgrade from plain TTAS; queue locks are the heavier-duty answer.
- **`tpause`/`umwait` for power-aware backoff.** On newer ISAs, `tpause` waits in a low-power state for a bounded time — a more efficient backoff than spinning `pause` loops, and `umonitor`/`umwait` can wake on the lock line changing.

### 32.4.3 MCS/CLH queue locks

For high contention (or when fairness/latency-tail matter), **queue locks** eliminate the line-bouncing entirely by having each waiter spin on its *own* cache line:

- **The idea.** Waiters form a queue (a linked list of per-thread nodes); each thread spins on a flag in *its own* node, and the lock holder, on release, sets the flag of the *next* node in the queue. So no two threads spin on the same line — *zero* contention on the lock word during the wait, and the handoff touches exactly one line (the successor's). This converts the O(N) coherence traffic of a contended TTAS into O(1) per handoff.
- **MCS lock** — each thread enqueues its own node (FIFO, so **fair** — no starvation), spins on its node's flag; the unlocker hands off to its successor. The standard high-contention queue lock; needs a per-thread node (passed in or thread-local).
- **CLH lock** — similar, but a thread spins on its *predecessor's* node; slightly different memory traffic, also fair. Both give FIFO fairness and per-line spinning.
- **When to use.** High contention where TTAS+backoff degrades (the line bounces among many waiters) and/or where **fairness/bounded latency** matters (backoff can starve; queue locks are FIFO-fair — a better *tail*, Ch. 1). The cost is complexity and the per-thread node bookkeeping. For most low-latency code, the better answer at this contention level is to **remove the sharing** (shared-nothing — Ch. 31) or go **lock-free** (Ch. 34); queue locks are for when you genuinely must have a fair, scalable lock.
- **The honest framing.** If you're reaching for an MCS lock, the contention is high enough that you should first ask whether the *design* is wrong (Ch. 31) — a well-partitioned system rarely needs a heavily-contended lock. Queue locks are the right tool when the sharing is genuinely irreducible.

## 32.5 Pitfalls & anti-patterns: spinning across the scheduler; lock convoy

- **Spinning while the holder is preempted (the cardinal spinlock sin).** On a *shared* (non-isolated) core, if the lock holder gets descheduled by the OS (Ch. 41) mid-critical-section, every spinner burns its *entire timeslice* spinning for a lock that *can't* be released until the holder is rescheduled — a massive waste and latency spike. **Spinlocks are only safe when the holder can't be preempted** — i.e. on dedicated/isolated cores (Ch. 6, 42, 45) with the holder pinned and not contended for the CPU. On a general-purpose core, use a blocking (or adaptive) mutex.
- **Naive test-and-set (spin on write).** Every spinner writing the lock line ping-pongs it catastrophically (§32.3 — ~3.6× worse than TTAS). Always **TTAS + `pause`** (§32.4.1).
- **Forgetting `pause`.** A spin loop without `pause` wastes power, starves the SMT sibling (Ch. 43), and eats a pipeline-flush penalty when the lock changes (Ch. 11) — *slower* than spinning with `pause`. Always `pause` in the spin.
- **Spinning for a long critical section.** Spinning burns the core; for a long-held lock, blocking (let the core do other work / sleep) is better. Spin only for *short* sections (< ~context-switch cost — §32.3).
- **Lock convoy.** Under sustained contention, threads pile up behind a lock and proceed in lockstep, each blocking/waking the next — throughput collapses and latency spikes (a convoy). Reduce critical-section length, shard the lock (Ch. 31), use a queue lock (fair, no convoy), or go lock-free (Ch. 34).
- **False sharing of the lock and its data (Ch. 33).** The lock word on the same cache line as the protected data means every data access bounces the lock line. Pad the lock (`alignas(hardware_destructive_interference_size)`).
- **Priority inversion (Ch. 45).** A low-priority thread holding a lock a high-priority thread spins/blocks on — under RT scheduling (Ch. 45) this can deadlock or stall. Use priority-inheritance mutexes (PI-futex) where RT priorities and shared locks mix, or avoid the shared lock.
- **Holding a spinlock across anything that can block/syscall/fault.** A spinlock holder that page-faults (Ch. 23), makes a syscall, or otherwise stalls makes *all* spinners wait for that stall — and risks the preemption disaster. Critical sections under a spinlock must be tiny, non-blocking, fault-free (pre-faulted — Ch. 26), and syscall-free.
- **Reaching for a lock when shared-nothing/lock-free would do.** A heavily-contended lock is often a *design* smell (Ch. 31). Before tuning the lock, ask whether the sharing can be eliminated (partition, single-writer) or replaced with a lock-free structure (Ch. 34). The best lock is no lock.

## 32.6 Exercises & checklist

**Exercises**

1. **TAS vs TTAS vs mutex.** Build `spin.cpp` (pin threads to distinct cores); run `tas`/`ttas`/`mutex` at M=2,4,8,16 with a tiny critical section. Confirm TAS collapses, TTAS+`pause` is best, mutex is in between. Add `perf stat` cache-coherence events (HITM) — which bounces the lock line most?
2. **Spin-vs-block crossover.** Vary the critical-section length (add work inside the lock) for a single-holder/low-contention case; find the section length where `std::mutex` beats the spinlock. Relate to the context-switch cost (Ch. 41).
3. **`pause` matters.** Remove `_mm_pause()` from the TTAS loop; measure. Quantify the slowdown (pipeline flush + power) and, on an SMT-enabled core, the effect on a task running on the sibling (Ch. 43).
4. **Backoff.** Add bounded exponential backoff to the TTAS lock; measure at high contention (M=16). Does it help? Add randomization. Where does backoff beat plain TTAS, and where would a queue lock be better (§32.4.2-3)?
5. **Preemption disaster.** Run the spinlock on a *shared* core (don't pin; oversubscribe) and force the holder to be preempted. Observe spinners burning timeslices (latency spikes). Then move to an isolated/pinned core and confirm the problem vanishes (§32.5).

**Checklist — spinlocks & contention control**

- [ ] I chose **spin vs block** by critical-section length and core dedication: **spin** for short sections on **dedicated/isolated cores** (Ch. 6, 42, 45); **block** (adaptive mutex) for long sections or shared cores.
- [ ] Spinlocks are **TTAS + `pause`** (spin on read, write only to acquire) — **never naive TAS**; acquire/release ordering (Ch. 30), not `seq_cst`.
- [ ] The lock holder **cannot be preempted** (pinned, isolated core) — no spinning across the scheduler (§32.5); critical sections are tiny, **non-blocking, fault-free** (pre-faulted — Ch. 26), syscall-free.
- [ ] The lock word is **cache-line padded** (no false sharing with the data it protects — Ch. 33).
- [ ] Under higher contention I added **bounded (randomized) backoff** or a **fair queue lock (MCS/CLH)** — and considered whether the sharing should be **eliminated (Ch. 31) or made lock-free (Ch. 34)** instead.
- [ ] I measured **under realistic contention and core count** (§32.3) — and watched the **latency tail** (Ch. 1), not just throughput; no lock convoy.
- [ ] Where RT priorities mix with shared locks, **priority inheritance** is used (Ch. 45) to avoid inversion.
- [ ] I confirmed a heavily-contended lock isn't a **design smell** — the best lock is **no lock** (shared-nothing — Ch. 31).

## 32.7 References

- T. Anderson, *"The Performance of Spin Lock Alternatives for Shared-Memory Multiprocessors"* — the classic comparison of TAS/TTAS/backoff/queue locks (the basis of §32.3-§32.4).
- J. Mellor-Crummey & M. Scott, *"Algorithms for Scalable Synchronization on Shared-Memory Multiprocessors"* — the MCS lock and scalable queue locks (§32.4.3).
- U. Drepper, *"Futexes Are Tricky"* and the Linux `futex(2)` man page — futex internals and how blocking mutexes work (§32.2).
- Intel *SDM* / *Optimization Reference Manual* — the `pause`/`tpause`/`umwait`/`umonitor` instructions and spin-wait-loop guidance (§32.2, §32.4).
- A. Williams, *C++ Concurrency in Action* (2e) — spinlocks, `std::mutex`, and the spin-vs-block trade-offs in practice.

## 32.8 Additional Reading

- P. McKenney, *Is Parallel Programming Hard?* — locking, contention, and when to avoid locks (ties Ch. 31, 34).
- The "Mechanical Sympathy" community and Carl Cook's *"When a Microsecond Is an Eternity"* — busy-spin/wait strategies on dedicated cores in HFT.
- Ch. 30 (*Atomics*) — the acquire/release a lock is built on; Ch. 31 (*Foundations*) — eliminating the sharing; Ch. 33 (*False Sharing*) — padding the lock; Ch. 34 (*Lock-Free*) — the alternative to locking; Ch. 37 (*Disruptor*) — busy-spin waiting strategies; Ch. 41, 42, 43, 45 (*Scheduling*) — why spinning needs an isolated core; Ch. 47 (*Syscalls*) — futex cost.
- **Appendix E** — context-switch and uncontended/contended lock latency numbers (the spin-vs-block crossover); **Appendix F** — TTAS/MCS/futex/convoy glossary.

---

*Next: Ch. 33 — False Sharing & Thread-Safety Anomalies, the invisible cache-line ping-pong that has shadowed every measurement in this Part: how logically-independent data on one cache line silently contends, how to detect it, and `hardware_destructive_interference_size`/padding to cure it.*
