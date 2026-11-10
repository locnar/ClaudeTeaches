# Part VI — Concurrency

# Chapter 39 — The C++ Memory Model & Atomics

> **Prerequisites:** Ch. 8 (cache coherence/MESI — atomics are coherence operations; the cost is cache-line traffic), Ch. 13 (out-of-order execution — the hardware reordering atomics constrain), Ch. 26 (the compiler reordering atomics also constrain), Ch. 4 (reading asm — §39.5 reads the emitted barriers), Ch. 42-preview (false sharing — the cost of contended atomics).
>
> **Leads into:** the rest of Part VI — Ch. 40 (threading), 32 (spinlocks), 33 (false sharing — the cost of atomics), 34 (lock-free structures built on these orderings), 35 (seqlocks), 36 (reclamation). This chapter is the *foundation* every lock-free pattern in Part VI rests on. ARM's weaker model is Appendix A.

---

## 39.1 Why it matters: correctness and cost of sharing

The moment two threads touch the same memory, you leave the comfortable world where code runs in the order you wrote it. Both the **compiler** (Ch. 4, 26 — it reorders, caches values in registers, eliminates "redundant" loads) and the **hardware** (Ch. 8, 13 — out-of-order execution, store buffers, cache coherence) reorder memory operations, and that reordering is *invisible and correct* for single-threaded code but *catastrophic* for sharing: thread B can observe thread A's writes in a different order than A issued them, see a half-updated structure, or never see an update at all (the value sits in A's store buffer or B's register). The C++ memory model (since C++11) is the contract that makes concurrent code *definable*: `std::atomic` and `memory_order` are how you tell the compiler and CPU which reorderings you can tolerate, turning "undefined behavior" (a data race) into "well-defined, with these visibility guarantees."

This is the **foundation of all of Part VI**. Every lock-free queue (Ch. 43), seqlock (Ch. 44), spinlock (Ch. 41), and the Disruptor (Ch. 46) is built out of atomic operations with carefully chosen memory orderings — and getting the ordering wrong produces bugs that are *rare, non-deterministic, hardware-dependent, and nearly impossible to reproduce* (they manifest under specific timing on specific CPUs, often never on x86 but readily — though still non-deterministically — on ARM; Appendix A). For HFT, where shared-memory IPC (Ch. 34, 65) and lock-free pipelines are the fast path between feed handler, strategy, and gateway, the memory model isn't academic: it's the difference between a correct nanosecond-latency data plane and a heisenbug that costs money once a month.

And it's a *cost* axis, not just correctness. Atomic operations are **cache-coherence operations** (Ch. 8): an atomic store must make the value visible to other cores, which means the cache line moves (a coherence transfer — ~tens of ns when contended, the HITM of Ch. 8/42), and stronger memory orderings emit **barriers** that prevent the CPU from hiding latency by reordering. `seq_cst` (the default, and the safe one) is the *most expensive* ordering; `relaxed` is nearly free but gives almost no guarantees; acquire/release sit in between. So the discipline has two halves: **correctness** (choose an ordering strong enough that your algorithm is right) and **cost** (choose the *weakest* ordering that's still correct, so you don't pay for guarantees you don't need). This chapter builds the mental model of `memory_order` (§39.2), measures the cost of each (§39.3), gives the rules for choosing (§39.4), and verifies in the asm what barriers each ordering actually emits on x86 vs ARM (§39.5) — because the gap between "x86 made my sloppy ordering work" and "ARM exposed the bug" is exactly what §39.5 reveals.

## 39.2 Mental model: `memory_order`; acquire/release/seq_cst; fences; reordering

**The problem: two kinds of reordering.** Memory operations get reordered by (1) the **compiler** (Ch. 4/26 — it hoists loads into registers, sinks stores, reorders independent accesses) and (2) the **hardware** (Ch. 8/13 — store buffers delay stores becoming visible; out-of-order execution and the cache-coherence protocol let other cores observe writes in a different order). For single-threaded code the result is *as-if* sequential; across threads, without synchronization, thread B can see thread A's operations in *any* order consistent with each thread's own program order — and a **data race** (two threads access the same location, at least one writing, with no synchronization) is flat **undefined behavior**.

**`std::atomic` + `memory_order`: the contract.** An atomic operation is indivisible *and* carries a `memory_order` that constrains reordering of the *surrounding non-atomic* operations relative to it. The orderings, weakest to strongest:

- **`relaxed`** — atomicity only, **no ordering/visibility guarantees** beyond the single location's own modification order. Other operations can reorder freely around it. Nearly free (no barrier). Use for counters/statistics where you only need the value to be atomic, not ordered relative to other data (e.g. a monotonic event counter you read later).
- **`acquire` (on a load) / `release` (on a store)** — the **workhorse pair** for publishing data. A `release` store *publishes*: everything the thread wrote *before* the release is visible to any thread that later does an `acquire` load *seeing that store*. An `acquire` load *consumes*: nothing after it can be reordered before it. Together they form a **happens-before** edge: "write data, then `release`-store a flag; `acquire`-load the flag, then read the data" guarantees the data is there. This is how you publish a message/pointer/snapshot — the basis of SPSC queues (Ch. 43), seqlocks (Ch. 44), the Disruptor (Ch. 46). Cheap on x86 (often *no* extra barrier — §39.5), real cost on ARM.
- **`acq_rel`** — for read-modify-write operations (`fetch_add`, `compare_exchange`) that both consume and publish: acquire on the read part, release on the write part.
- **`seq_cst` (sequentially consistent)** — the **default** and **strongest**: acquire/release *plus* a single **total order** of all `seq_cst` operations that all threads agree on. The easiest to reason about (everything looks interleaved-but-consistent) and the **most expensive** (a full barrier on x86 — `xchg`, or `mov`+`mfence`, for the store; on AArch64 the seq_cst store is still `stlr` and the load `ldar`, the extra strength coming from how `ldar`/`stlr` interact — only a standalone `atomic_thread_fence(seq_cst)` is `dmb ish`). The default precisely *because* it's the safe choice when you're unsure — but it's overkill for most acquire/release patterns.

```
   release/acquire publication (the workhorse):
     thread A:  data = 42;                 // (1) plain write
                flag.store(1, release);    // (2) publishes (1): "everything before is visible"
     thread B:  while(!flag.load(acquire)) {}   // (3) sees (2) ...
                use(data);                  // (4) ... guaranteed to see data==42  (happens-before)
```

**Fences (`std::atomic_thread_fence`).** Standalone barriers (`fence(acquire)`, `fence(release)`, `fence(seq_cst)`) that order surrounding atomic operations *without* being attached to a specific load/store. Useful when you want one barrier to cover several operations, or to separate the atomic access from the ordering. Mostly equivalent to ordered atomics; ordered atomics are usually clearer (§39.4.2).

**The rest of the atomic toolkit.** *Compare-and-swap:* `compare_exchange_weak` may fail *spuriously* (on ARM it maps to a load-linked/store-conditional pair) and belongs inside a retry loop; `compare_exchange_strong` fails only on a genuine mismatch (on x86 both are one `lock cmpxchg`). Both take separate *success* and *failure* orders — the failure order is a load order, so never `release`/`acq_rel`. The CAS loop is what every structure in Ch. 43–45 is built on. `std::atomic<T>::is_always_lock_free` tells you at compile time whether `T` is lock-free: a 16-byte pointer+counter pair (the ABA fix of Ch. 43) is lock-free on x86-64 only via `cmpxchg16b` (`-mcx16`, implied by `-march=native`), and libstdc++ otherwise routes it through libatomic — check before putting one on the hot path. *C++20 additions:* `std::atomic_ref<T>` (libstdc++ 10+, libc++ 19+) applies atomic operations to an *existing* non-atomic object — the tool for fields of a shared-memory layout you don't own (Ch. 34, 65); `wait()`/`notify_one()`/`notify_all()` block on a value change, futex-backed on Linux, for the *non*-hot threads (Ch. 41 covers spin-then-wait). `memory_order_consume` is still in the standard but every compiler promotes it to `acquire`; don't write it.

**The architecture matters enormously (§39.5, Appendix A).** **x86 is strongly ordered (TSO):** loads aren't reordered with loads, stores aren't reordered with stores, and the only reordering is store→load (a store buffered before a later load). So on x86, `acquire`/`release` often need **no extra instructions** (plain `mov` suffices) — only `seq_cst` stores need a barrier. **ARM/POWER are weakly ordered:** they reorder almost everything, so acquire/release emit real barriers (`ldar`/`stlr`/`dmb`). This is why **x86 forgives wrong orderings that ARM punishes** — sloppy code "works" on your Intel dev box and fails on Graviton (Appendix A). Reason about correctness from the *model*, not from what x86 happens to do.

The mental model: **shared memory without synchronization is reorder-anything UB; `std::atomic` + `memory_order` is the contract that constrains reordering. `relaxed` = atomic only; `acquire`/`release` = the publication workhorse (happens-before); `seq_cst` = strongest + total order + most expensive (the safe default). Choose by what your algorithm needs — and reason from the model, because x86's strong ordering hides bugs ARM exposes.**

## 39.3 Measure it: cost per memory order

The cost of an atomic depends on (1) the **memory order** (barriers emitted) and (2) **contention** (whether the cache line is bouncing between cores — Ch. 8/42). Measure both: the per-operation cost of each ordering uncontended, and the collapse under contention.

```cpp
// atomics.cpp — cost of relaxed/release/seq_cst stores, uncontended; and contended fetch_add.
// Build: g++ -O2 -std=c++20 -march=native atomics.cpp -o atomics -pthread
// Run pinned:  taskset -c 2 ./atomics relaxed | release | seqcst
//   contention:  ./atomics contend <nthreads>   (each thread pins itself to core t)
#include <atomic>
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <cstdlib>    // std::atoi
#include <thread>
#include <vector>
#include <chrono>
#include <pthread.h>
#include <sched.h>

std::atomic<std::uint64_t> g{0};

static void pin_to(int core) {                           // Ch. 54: each contender on its own core
    cpu_set_t s; CPU_ZERO(&s); CPU_SET(core, &s);
    if (pthread_setaffinity_np(pthread_self(), sizeof s, &s) != 0) std::perror("pthread_setaffinity_np");
}

// The memory order MUST be a compile-time constant. GCC's __atomic builtins (which std::atomic
// inlines to) map any run-time memory_order to seq_cst, so `g.store(i, order)` with a variable
// `order` emits one xchg for all three modes and measures nothing (verified: GCC 13 -O2).
template <std::memory_order O>
double store_loop(long N) {
    auto t0 = std::chrono::steady_clock::now();
    for (long i = 0; i < N; ++i) g.store(i, O);            // uncontended store, this thread only
    auto t1 = std::chrono::steady_clock::now();
    return std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count() / (double)N;
}

int main(int argc, char** argv) {
    const char* mode = argc > 1 ? argv[1] : "seqcst";
    constexpr long N = 200'000'000;

    if (!std::strcmp(mode, "contend")) {                  // contended fetch_add from M cores
        int M = argc > 2 ? std::atoi(argv[2]) : 4;
        auto t0 = std::chrono::steady_clock::now();
        std::vector<std::thread> ts;
        for (int t = 0; t < M; ++t) ts.emplace_back([&, t]{
            pin_to(t);
            for (long i = 0; i < N / M; ++i) g.fetch_add(1, std::memory_order_relaxed);
        });
        for (auto& th : ts) th.join();
        auto t1 = std::chrono::steady_clock::now();
        double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
        std::printf("contend M=%d  %.2f ns/op (g=%llu)\n", M, ns/N, (unsigned long long)g.load());
        return 0;
    }

    double ns = !std::strcmp(mode,"relaxed") ? store_loop<std::memory_order_relaxed>(N)   // dispatch ONCE,
              : !std::strcmp(mode,"release") ? store_loop<std::memory_order_release>(N)   // outside the loop
              :                                store_loop<std::memory_order_seq_cst>(N);
    std::printf("%-8s %.3f ns/store\n", mode, ns);
    return 0;
}
```

Representative results (toolchain and machine: Appendix L.3) — reference machine **Xeon Gold 6326** (Ice Lake-SP, x86-TSO), `-O2`, pinned, turbo off (illustrative):

```
   UNCONTENDED store (single thread):
   relaxed   ~0.3 ns/store     plain mov — no barrier
   release   ~0.3 ns/store     plain mov on x86! (TSO gives release free — §39.5)
   seq_cst   ~5-7 ns/store     xchg/mfence — full barrier, ~20x the relaxed store

   CONTENDED fetch_add (M cores hammering one line):
   M=1       ~6 ns/op          uncontended RMW (the atomic itself, line stays local)
   M=2       ~20 ns/op         line ping-pongs between 2 cores (HITM — Ch. 8/42): 2 cores, LESS than half the throughput of 1
   M=4       ~33 ns/op         ... worse; cache line bounces among 4 cores
   M=8       ~50 ns/op         contention dominates; retrograde scaling (the same curve as Ch. 40 §40.3)
```

Two distinct lessons. (1) **Ordering cost (uncontended):** on x86, `relaxed` and `release` stores are *the same* plain `mov` — release is **free** under TSO (§39.5) — while `seq_cst` costs ~20× because it emits a full barrier. So defaulting to `seq_cst` when acquire/release would do is pure waste on the store side. (2) **Contention cost:** the *atomic operation itself* is cheap (~6 ns uncontended), but when multiple cores hammer the *same cache line*, the line ping-pongs (the HITM coherence transfer of Ch. 8 — and across sockets, the NUMA cliff of Ch. 18), and throughput **collapses** — ~8× worse per op at 8 cores than uncontended, so eight cores together do *less* than one did alone (retrograde scaling; Ch. 40 §40.3 plots the same curve). This is the dominant cost in real concurrent code, and the lesson that drives Part VI: **the expensive thing isn't the atomic, it's the *contention*** — the cure is not a faster atomic but *not sharing the line* (per-thread state, padding — Ch. 42, sharding — Ch. 40). The memory-order choice (§39.4) is the cheaper, second-order optimization on top.

**Wide atomics, `atomic_ref` and waiting** (§39.4.3; VM-measured — the 4-vCPU Xeon VM of Appendix L.2, GCC 13.3 / Clang 18.1, `-O2 -std=c++20 -latomic`):

```
16-byte CAS on alignas(16) struct { uint64_t ptr, tag; }, uncontended, 50 M iterations:
   g++               call __atomic_compare_exchange_16 (libatomic)   9.8 ns/CAS   is_always_lock_free = 0   is_lock_free() = 0
   g++ -mcx16        call __atomic_compare_exchange_16 (libatomic)   9.8 ns/CAS   is_always_lock_free = 0   is_lock_free() = 0   ← GCC still calls out
   clang++           call __atomic_compare_exchange     (generic)    8.5 ns/CAS   is_always_lock_free = 0   is_lock_free() = 0
   clang++ -mcx16    inline  lock cmpxchg16b                        10.1 ns/CAS   is_always_lock_free = 1   is_lock_free() = 0   ← the runtime query disagrees
thread wake-up, two threads ping-pong (one-way ≈ round trip / 2):
   busy-poll + pause                              602 ns round trip      ≈ 300 ns one-way (VM; a line transfer + hypervisor noise)
   std::atomic<int>::wait / notify_one          6,782 ns round trip      ≈ 3.4 µs one-way  — a futex wake and a schedule, 11×
std::atomic_ref<int64_t>(bbo.bid).load(relaxed) vs plain read:   same single mov (§39.5)
```

Three facts to carry. **GCC never inlines `cmpxchg16b`** — with or without `-mcx16` the 16-byte CAS is a call into libatomic, which *does* use `cmpxchg16b` when the CPU has it (every x86-64 since 2006), so the operation is lock-free in practice but **`is_always_lock_free` is `false`** and a `static_assert` on it fails; the 9.8 ns is the instruction plus a call, not a lock. **Clang inlines it only with `-mcx16`** (`-march=x86-64-v2` and up imply it); without the flag the generic libatomic entry point is used, which falls back to a *real lock* on a CPU without `cx16` — and even with the flag Clang 18's runtime `is_lock_free()` consults libatomic and reports `false` while the compile-time constant says `true`: trust the compile-time constant for the flags you ship, and `static_assert` it. **`wait`/`notify` is a futex**: 3.4 µs to wake a parked thread against 300 ns to notice a store while spinning — the right tool for housekeeping threads and for parking a hot thread *between sessions* (Ch. 41 §41.4 on when to block at all), never inside the tick-to-trade path.

## 39.4 Techniques

### 39.4.1 Choosing the weakest correct ordering

The rule: **use the weakest memory order that makes your algorithm correct** — strong enough for correctness, no stronger (you pay for guarantees you buy). A decision guide:

- **`relaxed` — atomicity without ordering.** Use when you only need the operation to be atomic and don't need it ordered relative to *other* memory: monotonic counters/statistics read later, a flag whose ordering relative to other data doesn't matter, generating unique IDs (`fetch_add`). *Don't* use it to publish data (no happens-before — a classic bug).
- **`acquire`/`release` — publication (the default workhorse).** Use whenever one thread *produces* data and another *consumes* it: write the data, `release`-store the flag/index/pointer; `acquire`-load it, then read the data. This covers SPSC queues (Ch. 43), seqlock publication (Ch. 44), the Disruptor's sequence barriers (Ch. 46), "is this ready?" flags. **This is the right default for lock-free producer/consumer** — and on x86 it's nearly free (§39.3, §39.5).
- **`acq_rel` — read-modify-write that both consumes and publishes.** `fetch_add`/`compare_exchange` on a shared index/pointer where you both observe the prior state and publish a new one (a lock-free push/pop). Acquire on the load half, release on the store half.
- **`seq_cst` — only when you genuinely need a total order.** Some algorithms (e.g. certain mutual-exclusion or multi-location protocols, Dekker-style, where independent atomics must have a single agreed order) need the total order `seq_cst` provides; acquire/release isn't enough. Use it *there*, knowingly. It's also fine as a *correct-by-default* starting point — but optimize down to acquire/release once the algorithm is proven (§39.6).
- **Reason from the model, prove it, then test on ARM.** Choose ordering by the *happens-before edges your algorithm requires*, write down why each is correct, and **test on a weakly-ordered machine** (ARM/Graviton — Appendix A) or with a model checker (Ch. 51) — because x86 will pass incorrect-but-too-weak code that ARM breaks. Weakening ordering is a correctness-critical optimization: do it deliberately, not by guessing.

### 39.4.2 Fences vs ordered atomics

Two ways to express ordering — attached to an atomic op, or as a standalone fence:

- **Ordered atomics (preferred default).** `x.store(v, release)` / `x.load(acquire)` attach the ordering to the operation. Clearer (the ordering is right where the access is), and the compiler can often emit the tightest code (e.g. x86 plain `mov` for release). Prefer these for almost all code.
- **Standalone fences (`atomic_thread_fence`).** A `release` fence before a `relaxed` store (or an `acquire` fence after a `relaxed` load) achieves similar ordering but as a separate barrier — useful when *one* barrier must order *several* atomic operations (amortize one fence over a batch), or when separating the access from the ordering aids clarity in a complex protocol. A `seq_cst` fence is the heaviest. Fences are slightly more general but easier to get subtly wrong (the fence-to-atomic pairing rules are fiddlier than ordered-atomic pairing).
- **When fences pay.** Batching: if you do several `relaxed` writes then want to publish them all, one `release` fence + a `relaxed` flag store can be cheaper/clearer than making each write ordered. Some seqlock and reclamation patterns (Ch. 44–45) use fences deliberately. But for the common producer/consumer publication, **ordered atomics are clearer and equally fast** — reach for fences only when you have a specific reason.
- **Don't hand-roll with `volatile` or compiler barriers.** `volatile` is **not** an atomic/ordering primitive in C++ (it prevents *compiler* optimization of the access but provides *no* inter-thread ordering or atomicity — a classic mistake from older code). Inline-asm memory clobbers (`asm volatile("":::"memory")`) are compiler-only barriers, not hardware ones. Use `std::atomic` + `memory_order`; that's what the model defines.

### 39.4.3 Beyond `std::atomic<T>`: `atomic_ref`, `wait`/`notify`, and wide atomics

Three C++20 additions close gaps the book's own designs walk into:

- **`std::atomic_ref<T>` (C++20; libstdc++ 10, libc++ 19; `__cpp_lib_atomic_ref`) — atomic access to a plain object.** A wire struct in a receive buffer, a `Bbo` in a shared-memory segment (Ch. 65), a seqlock payload (Ch. 44) are *plain* `int64_t`s that another thread or process may write while you read: formally a data race, UB, however benign on x86. `std::atomic_ref<std::int64_t>(bbo.bid).load(std::memory_order_relaxed)` makes the access atomic *for that access* without changing the struct's type or layout — the object stays a plain `Bbo` the DMA engine or the other process can write — and compiles to the same `mov` (§39.5). Requirements: the object must be suitably aligned (`atomic_ref<T>::required_alignment`, 8 for `int64_t`, 16 for a 16-byte type) and *every* access during the `atomic_ref`'s lifetime must go through an `atomic_ref` (mixing plain and atomic access is UB again). Ch. 44 §44.4.2's seqlock reads its payload this way; use it for every field of a shm struct that a foreign writer can touch.
- **`std::atomic<T>::wait()` / `notify_one()` / `notify_all()` (C++20; libstdc++ 11, libc++ 11; `__cpp_lib_atomic_wait`) — a futex with a type.** `flag.wait(old)` blocks until the value differs from `old` (spurious wakes allowed — loop on the predicate); `notify_one()` wakes a waiter. It replaces the condition-variable-plus-mutex idiom for one-word state, and on Linux it *is* `futex(2)` after a short spin: §39.3 measured a 3.4 µs one-way wake against 300 ns for a busy-poll. Use it where a thread should *not* burn a core — the scraper (Ch. 96), the journal writer (Ch. 104), a hot thread parked overnight — and keep the hot path on `load`/`pause` (Ch. 41). `std::atomic_flag::test()` (C++20) finally makes the flag readable without a `test_and_set`.
- **Wide (16-byte) atomics and the libatomic trap.** A tagged pointer or a `{seq, value}` pair in one `alignas(16)` struct is the DWCAS the lock-free chapters assume (Ch. 43 §43.4). The toolchain facts from §39.3: GCC compiles it to a libatomic call whatever you pass (lock-free at runtime via `cmpxchg16b`, but `is_always_lock_free == false`); Clang inlines `lock cmpxchg16b` only with `-mcx16`/`-march=x86-64-v2+`, and otherwise calls the generic entry point that may take a lock. The discipline, for every 16-byte atomic and every atomic placed in shared memory: build with `-mcx16` (or an `-march` that implies it — Ch. 27, Ch. 28 §28.4.5 for keeping that flag uniform), `alignas(16)` the object (a 16-byte atomic at offset 8 in a shm struct is *misaligned*, and libatomic takes the lock path), and `static_assert(std::atomic<Tagged>::is_always_lock_free)` — accepting that on GCC this assertion fails and the fallback is a startup check of `is_lock_free()` on the actual object plus a documented decision. A locked 16-byte atomic in shared memory is a mutex shared with another process (Ch. 65 §65.4.2) — the one thing the design was built to avoid.

## 39.5 Verify the codegen: barriers emitted per ordering (x86 vs ARM)

The single most clarifying thing about the memory model is *seeing what each ordering compiles to* — and how wildly it differs by architecture. Snippets are **verified** Clang output.

**x86-64 (TSO — strongly ordered):**

```asm
; x.store(v, relaxed)  AND  x.store(v, release):    IDENTICAL — plain store (TSO gives release free)
        mov     qword ptr [rdi], rsi

; x.store(v, seq_cst):    needs a full barrier — xchg (implicitly locked) or mov+mfence
        xchg    qword ptr [rdi], rsi        ; the seq_cst store is ~20x the relaxed store (§39.3)

; x.load(acquire)  AND  x.load(seq_cst)  AND  x.load(relaxed):  IDENTICAL — plain load (TSO)
        mov     rax, qword ptr [rdi]
```

On x86, **acquire loads and release stores are plain `mov`** — *no barrier* — because TSO already forbids the reorderings they prevent. Only the **`seq_cst` store** costs a real barrier. This is exactly why x86 "forgives" using too-weak ordering: the hardware is stronger than your code asked for.

**AArch64 (weakly ordered — Appendix A):**

```asm
; x.store(v, relaxed):    plain store
        str     x1, [x0]
; x.store(v, release):    STLR — release store (a real barrier instruction)
        stlr    x1, [x0]
; x.load(acquire):        LDAR — acquire load (a real barrier instruction)
        ldar    x0, [x0]
; x.store(v, seq_cst):    stlr    x1, [x0]   — the SAME instruction as release: AArch64's seq_cst premium is in
;                         the ldar/stlr pairing rules, not an extra barrier (only atomic_thread_fence(seq_cst) is dmb ish)
```

On ARM, **acquire/release emit real instructions** (`ldar`/`stlr`) and `relaxed` does not — so the ordering you choose has *visible cost and visible correctness effect*. Code that omits the needed acquire/release compiles to plain `str`/`ldr` on ARM and **races** — the bug x86 hid.

The verification habits: **(1) read the asm for your atomics on *both* x86 and ARM** (Godbolt cross-compile) — confirm `seq_cst` stores emit the barrier you're paying for, and that on ARM your acquire/release became `ldar`/`stlr` (if they're plain `ldr`/`str`, your ordering is too weak and you have a latent race). **(2) Notice when `seq_cst` is costing you** a barrier (x86 `xchg`/`mfence`; on ARM a `dmb ish` appears only for a standalone `seq_cst` fence) that acquire/release would avoid — the §39.4.1 optimization, visible in the asm. **(3) Don't trust x86 testing for correctness** — the asm shows x86 emits nothing for acquire/release, so x86 *cannot* catch a too-weak-ordering bug; only the model, a weakly-ordered machine, or a model checker (Ch. 51) can.

**`atomic_ref` and the 16-byte CAS (§39.4.3).** `clang++ -O2 -std=c++20`:

```asm
; int64 read_bid_plain(const Bbo&)     ; int64 read_bid_ref(const Bbo&) — atomic_ref<int64_t>(b.bid).load(relaxed)
        mov     rax, qword ptr [rdi]   ;         mov     rax, qword ptr [rdi]        ; IDENTICAL: defined, same mov
        ret                            ;         ret
; write_bid_ref — atomic_ref(b.bid).store(v, release):  mov qword ptr [rdi], rsi ; ret   (TSO: release is a plain store)
; cas16(atomic<Tagged>&, Tagged& expected, Tagged desired):
;   clang++ -mcx16 :   mov rax,[expected]; mov rdx,[expected+8]; mov rbx,desired.lo; mov rcx,desired.hi
;                      lock cmpxchg16b xmmword ptr [rdi]            ; INLINE — lock-free, provably
;   clang++ (no flag): call __atomic_compare_exchange@PLT           ; generic libatomic — may LOCK
;   g++ (either)   :   call __atomic_compare_exchange_16@PLT        ; libatomic; cmpxchg16b inside at runtime, is_always_lock_free == false
```

The check: `objdump -d hot.o | grep -c 'cmpxchg16b\|__atomic_compare_exchange'` — the first should be non-zero and the second zero on the hot path (Appendix I.12).

## 39.6 Pitfalls & anti-patterns: seq_cst by default; data races

- **`seq_cst` everywhere (the over-strong default).** The default ordering is `seq_cst` — *correct* but the *most expensive* (a full barrier; ~20× a relaxed store on the store side — §39.3, §39.5). Leaving every atomic at the default pays for a total order you rarely need. Profile, then weaken to acquire/release where the algorithm allows (§39.4.1) — *after* proving correctness.
- **`relaxed` where you needed publication (the under-strong bug).** Using `relaxed` to publish data (write data, `relaxed`-store a flag) gives **no happens-before** — the consumer can see the flag set but stale/absent data. The most common lock-free correctness bug. Publication needs `release`/`acquire`. (And it'll *work on x86* and fail on ARM — §39.5.)
- **Data race = UB, not "probably fine."** Two threads accessing the same non-atomic location with at least one write, unsynchronized, is **undefined behavior** — the compiler may assume it can't happen and miscompile (Ch. 26). Not "you read a stale value"; *anything* can happen. Make shared mutable access atomic or synchronized. (TSan — Ch. 51 — finds these.)
- **`volatile` as an atomic/ordering tool.** `volatile` prevents *compiler* optimization of an access but provides **no atomicity and no inter-thread ordering** — it is *not* a concurrency primitive in C++ (unlike Java/C#). Using it for thread communication is a classic, real bug. Use `std::atomic`.
- **Testing only on x86.** x86-TSO emits *nothing* for acquire/release (§39.5), so it physically *cannot* expose a too-weak-ordering bug. Code that passes on Intel can fail on ARM/Graviton (Appendix A). Test on a weakly-ordered machine and/or model-check (Ch. 51) before trusting lock-free code.
- **Ignoring contention (optimizing the wrong thing).** The dominant cost of shared atomics is **cache-line contention** (the line ping-ponging — §39.3, Ch. 8/42), not the ordering. Tuning `seq_cst`→`acquire` saves a barrier; *not sharing the line* (per-thread counters, padding — Ch. 42, sharding — Ch. 40) saves the ~8× contention collapse (and the retrograde scaling behind it). Fix contention first.
- **False sharing of atomics (Ch. 42).** Two unrelated atomics on the same cache line contend even though they're logically independent — the line bounces. Pad hot atomics to separate cache lines (`alignas(hardware_destructive_interference_size)`).
- **Assuming atomic = lock-free for all types.** `std::atomic<T>` for large `T` may use a hidden lock (`is_lock_free()`/`is_always_lock_free`); only suitably-sized/aligned types are truly lock-free. The 16-byte case is the trap the book's own DWCAS designs step on: GCC always calls libatomic and reports `is_always_lock_free == false` even with `-mcx16`; Clang inlines `cmpxchg16b` only with `-mcx16` and its runtime `is_lock_free()` still answers `false` (§39.3, §39.4.3). `static_assert` the compile-time constant under the shipping flags, `alignas(16)`, and read the asm.
- **Plain reads of a struct another thread or process writes.** A `Bbo` in shared memory or a seqlock payload read without `atomic_ref` is a data race — UB that x86 happens to forgive until the compiler hoists the load (§39.4.3). `std::atomic_ref` costs nothing and makes it defined.
- **`atomic::wait` on the hot path.** It is a futex: 3.4 µs to wake (§39.3) against 300 ns of spinning. Park housekeeping threads with it; never the tick-to-trade thread while the market is open.
- **Mixing fence and ordered-atomic reasoning sloppily.** Fence-to-atomic pairing rules differ from ordered-atomic pairing; combining them carelessly creates subtle gaps. Prefer ordered atomics; use fences deliberately (§39.4.2).

## 39.7 Exercises & checklist

**Exercises**

1. **Cost per ordering.** Build `atomics.cpp`; measure `relaxed`/`release`/`seqcst` uncontended stores. Confirm relaxed≈release (free on x86) and seq_cst ~20× (a barrier). Disassemble (§39.5) — what instruction does seq_cst emit?
2. **Contention collapse.** Run `contend` with M=1,2,4,8 cores pinned to different cores; plot ns/op. Watch it collapse (HITM — Ch. 8). Then give each thread its *own* padded counter and sum at the end — confirm it scales (Ch. 42). Which mattered more, ordering or contention?
3. **x86 vs ARM codegen.** On Godbolt, compile an acquire load and release store for x86-64 and AArch64. Confirm x86 = plain `mov`, ARM = `ldar`/`stlr` (§39.5). Then *omit* the acquire/release (use relaxed) — show ARM emits plain `ldr`/`str` (the latent race x86 hides).
4. **Publication bug.** Write a producer that fills a struct then sets a `relaxed` flag, and a consumer that reads the struct after seeing the flag. Show it's a race (TSan — Ch. 51, or reason via §39.6). Fix with release/acquire and confirm.
5. **Weaken seq_cst.** Take an SPSC ring (Ch. 43) written with `seq_cst` everywhere; weaken the indices to acquire/release where correct; measure the throughput gain and confirm correctness reasoning (and ideally on ARM).

**Checklist — the C++ memory model & atomics**

- [ ] All shared mutable access is **`std::atomic`** (or lock-protected) — no data races (UB); **`volatile` is never** used for thread communication.
- [ ] Each atomic uses the **weakest correct `memory_order`**: `relaxed` (atomic only), `acquire`/`release` (publication — the default workhorse), `acq_rel` (RMW), `seq_cst` (only where a total order is genuinely needed) — chosen from the **happens-before edges the algorithm requires**.
- [ ] I did **not** leave everything at the `seq_cst` default where acquire/release suffices (§39.4.1) — but I weakened ordering only **after proving correctness**.
- [ ] Publication (produce data → publish flag/index/pointer) uses **release/acquire**, never `relaxed` (§39.6).
- [ ] I **verified the emitted barriers in the asm on both x86 and ARM** (§39.5) — acquire/release became `ldar`/`stlr` on ARM (not plain loads/stores), and I'm not paying for `seq_cst` barriers I don't need.
- [ ] Correctness was validated on a **weakly-ordered machine and/or a model checker** (Ch. 51) — not on x86 alone.
- [ ] **Contention** (the dominant cost) is addressed first — per-thread/sharded state, cache-line padding (Ch. 42) — before micro-tuning memory orders.
- [ ] Hot-path atomics are confirmed **lock-free** (`is_always_lock_free`) and **padded** to avoid false sharing (Ch. 42).

## 39.8 References

- ISO C++ standard `[atomics]`/`[intro.races]` and cppreference — `std::atomic`, `std::memory_order`, `atomic_thread_fence`, the happens-before/synchronizes-with definitions; the normative model.
- H. Boehm & S. Adve, *"Foundations of the C++ Concurrency Memory Model"* (PLDI 2008) — the rationale and semantics behind C++11 atomics.
- H. Sutter, *"atomic<> Weapons"* (C++ and Beyond) — the definitive practical talk on the memory model, acquire/release, and the x86-vs-weak-ordering distinction.
- P. McKenney, *"Memory Barriers: a Hardware View for Software Hackers"* and *Is Parallel Programming Hard?* — store buffers, coherence, and barriers from the hardware side (ties Ch. 8).
- Intel *SDM* (memory ordering, x86-TSO) and ARM *Architecture Reference Manual* (the weak model, `LDAR`/`STLR`/`DMB`) — the per-architecture ordering that §39.5 and Appendix A rest on.

## 39.9 Additional Reading

- J. Preshing's blog ("Preshing on Programming") — lucid, example-driven posts on acquire/release, memory ordering, and lock-free patterns; an excellent companion to this chapter.
- A. Williams, *C++ Concurrency in Action* (2e) — thorough, practical coverage of the memory model and atomics, with the lock-free chapters that lead into Ch. 43.
- Ch. 8 (*Caches/MESI*) — the coherence mechanics atomics drive; Ch. 42 (*False Sharing*) — the contention cost; Ch. 43–46 (*Lock-Free / Seqlock / Disruptor*) — the structures built on these orderings; Ch. 51 (*Concurrency Tooling*) — TSan and model checking to validate ordering; **Appendix A** — the ARM weak memory model in depth.
- **Appendix E** — atomic/contended-line latency numbers; **Appendix F** — acquire/release/seqlock/HITM glossary.

---

*Next: Ch. 40 — Multithreading & Concurrency Foundations, which steps up from individual atomics to whole-system concurrency design: contention and scalability limits, and the shared-nothing partitioning that makes the atomics of this chapter rare rather than central.*
