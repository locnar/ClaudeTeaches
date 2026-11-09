# Part VI — Concurrency

# Chapter 45 — Concurrency Correctness Tooling

> **Prerequisites:** Ch. 33 (the memory model — TSan checks happens-before; the bugs are ordering bugs), Ch. 37–40 (the lock-free structures this validates), Ch. 26/39 (allocation/UAF — ASan's domain), Ch. 23 (UB — UBSan's domain), Ch. 3 (measurement rigor extends to correctness).
>
> **Leads into:** Ch. 87 (secure programming — sanitizers + fuzzing on the decode path), Ch. 102 (CI/regression — these tools belong in CI). Closes **Part VI** — the verification discipline that makes the lock-free and async work of Ch. 33–44 *trustworthy*.

---

## 45.1 Why it matters: lock-free bugs are non-deterministic

Every chapter of Part VI came with the same warning, and this chapter is its payoff: **concurrent bugs are the worst bugs in software.** A data race, a missing acquire/release (Ch. 33), an ABA (Ch. 37), a use-after-free in a lock-free structure (Ch. 39), a torn seqlock read (Ch. 38) — these manifest *rarely* (a specific timing window), *non-deterministically* (different every run), *architecture-dependently* (x86's strong ordering hides what ARM exposes — Ch. 33), and often *silently* (corrupting data without crashing). You cannot find them by reading the code (the ordering is subtle), by testing once (it passed — this time), or by running on your x86 dev box (which forgives the ordering bugs). A lock-free queue can run correctly for *months* and then corrupt a message at market open under exactly the load that hits the timing window — losing money in a way you can't reproduce. **Concurrent correctness cannot be hoped for; it must be *verified* with tools designed to expose what testing can't.**

The tools exist and they are extraordinarily effective — but each catches a *different class* of bug, and knowing which tool finds which is the chapter's core. **ThreadSanitizer (TSan)** is the headline: a runtime data-race detector that instruments memory accesses and synchronization to build the happens-before graph (Ch. 33) and flag any two accesses to the same location, at least one a write, not ordered by synchronization — i.e. a data race, *even if it didn't actually race on this run*. TSan finds races that testing would *never* trigger, because it reasons about the ordering, not the timing. **ASan** finds memory errors (use-after-free — the Ch. 39 reclamation bug, buffer overflows, leaks); **UBSan** finds undefined behavior (Ch. 23 — the signed overflow, the misaligned access, the null deref; *not* strict-aliasing violations, which no sanitizer checks). Together the sanitizers turn a class of silent, rare, catastrophic bugs into a deterministic test failure with a stack trace.

But sanitizers have a fundamental limit that defines the rest of the chapter: **they only detect bugs on code paths and interleavings that actually execute.** TSan reports a race only if both accesses *run* (under some interleaving the scheduler produces); it can't prove the *absence* of races on paths you didn't exercise. So the sanitizers must be *driven* — by **stress testing** (run the concurrent code under heavy, varied load to hit the timing windows), **fuzzing** (generate inputs that explore code paths — especially the message-decode paths of Ch. 60, where untrusted input meets concurrency), and — for the highest assurance on small lock-free primitives — **model checking** (exhaustively explore *all* possible interleavings and memory-model behaviors, proving correctness rather than sampling it). The discipline that closes Part VI: **write the lock-free code, then prove it with TSan + ASan + UBSan under stress and fuzzing, model-check the critical primitives, and run it all in CI — and never trust concurrent code that x86 testing alone "passed."** This chapter covers what each tool can and can't catch (§45.2), running them on a queue (§45.3), the stress/fuzz/model-check techniques (§45.4), and the crucial pitfall that sanitizer-clean does *not* mean correct (§45.5).

## 45.2 Mental model: what TSan/ASan/UBSan can and can't catch

The sanitizers are **runtime** tools (compile with `-fsanitize=...`, run the instrumented binary); each instruments different things and catches a different bug class. Knowing the division of labor is essential — they're complementary, not interchangeable.

- **ThreadSanitizer (TSan, `-fsanitize=thread`)** — **data races and some deadlocks.** Instruments memory accesses and synchronization operations (atomics, locks, thread create/join) to build the **happens-before** relation (Ch. 33). It flags a **data race**: two accesses to the same memory, at least one a write, not ordered by a happens-before edge. Crucially, TSan reports a race *if the two accesses execute under the observed interleaving* — it understands the *synchronization*, so it catches races that didn't manifest as wrong values this run (it's not just "did the values clash," it's "is there a missing happens-before edge"). This is exactly the tool for missing acquire/release (Ch. 33), unsynchronized shared access (Ch. 34), and the subtle ordering bugs in lock-free code. **The single most important concurrency tool.** Cost: ~5-15× slowdown, ~5-10× memory — for testing, not production.
- **AddressSanitizer (ASan, `-fsanitize=address`)** — **memory errors.** Use-after-free (the Ch. 39 reclamation bug, dangling coroutine handles — Ch. 43), heap/stack buffer overflows (Ch. 60, 87), use-after-return, double-free, leaks (with LSan). Instruments allocations with redzones and a shadow map. Catches the *memory-corruption* consequences of concurrency bugs (a freed lock-free node dereferenced). **ASan and custom allocators:** inside a pool/arena (Ch. 27) ASan sees *one* live block, so a node returned to the free list and then dereferenced — exactly the Ch. 39 bug — is invisible to it. Poison slots on release and unpoison on allocate (`ASAN_POISON_MEMORY_REGION`/`ASAN_UNPOISON_MEMORY_REGION` from `<sanitizer/asan_interface.h>`, compiled out when ASan is off), or build the test binary with the pool swapped for plain `malloc`. Cost: ~2× slowdown. **Run it alongside TSan** (separate builds — they're incompatible together) — TSan finds the race, ASan finds the UAF the race causes.
- **UndefinedBehaviorSanitizer (UBSan, `-fsanitize=undefined`)** — **undefined behavior.** Signed integer overflow (Ch. 30, 87 — price/qty math), misaligned access (`-fsanitize=alignment`, Ch. 23), null/invalid pointer use, shifts past width (Ch. 31), invalid enum/bool values. Cheap (~little overhead); compatible with ASan. Catches the UB that miscompiles (Ch. 23) — relevant because UB bugs interact viciously with concurrency. **Not** caught: strict-aliasing (type-based aliasing) violations (Ch. 23) — no UBSan check exists; use Clang's experimental TypeSanitizer (`-fsanitize=type`, Clang 19+) or review the punning sites by hand. **MemorySanitizer (MSan, `-fsanitize=memory`, Clang only)** — uninitialised reads, which in a pool-allocated hot path (Ch. 27) means stale slot contents; it needs every linked library instrumented, so it runs in a dedicated build (Ch. 87).
- **What none of them catch (the limit — §45.1, §45.5):**
  - **Bugs on un-executed paths/interleavings.** TSan only sees the interleaving the scheduler happened to produce *this run* — it doesn't explore *all* interleavings. A race reachable only under a rare interleaving you didn't hit goes unreported. (This is why you need stress testing — §45.4.1 — to *produce* the interleavings, and model checking — §45.4.3 — to *exhaust* them.)
  - **Memory-model-legal-but-wrong logic.** A correctly-synchronized-but-logically-wrong algorithm (right happens-before, wrong result) is invisible to TSan — that's a *logic* bug, not a race.
  - **Standalone fences.** TSan does **not** model `std::atomic_thread_fence` (LLVM issue 52942; GCC 11+ warns via `-Wtsan`: "ThreadSanitizer does not support std::atomic_thread_fence and can report false positives"). Fence-synchronised code — the Ch. 38 seqlock, fence-based Disruptor variants — produces *false positives* (and can miss real races) under TSan. For the TSan build, write the synchronisation with release/acquire *stores and loads* (`std::atomic_ref` for the data), or check the fence-based form with the memory-model checkers of §45.4.3.
  - **Uninstrumented code.** TSan sees only what it instrumented: a library built without `-fsanitize=thread` hides its synchronisation (missed happens-before → false positives) and its accesses (missed races). Instrument everything you link, or suppress deliberately (`TSAN_OPTIONS=suppressions=…`, `ignore_noninstrumented_modules=1`); raise `history_size` when reports show truncated stacks.
  - **The x86-hides-ARM problem partially.** TSan reasons about the *abstract* memory model (catches missing synchronization regardless of x86's strong ordering — better than testing), but subtle weak-ordering bugs and `relaxed`-ordering correctness still benefit from running on ARM and model-checking.

The model: **TSan = data races / missing happens-before (the lock-free ordering bug — the key tool); ASan = memory errors / UAF (the reclamation bug); UBSan = undefined behavior. They're runtime, complementary, and only see *executed* code/interleavings — so they must be *driven* by stress/fuzzing and supplemented by model checking, and "sanitizer-clean on x86" is necessary but not sufficient.**

## 45.3 Measure it: running the sanitizers on a queue

The demonstration: take a deliberately-buggy lock-free queue (missing the acquire/release of Ch. 33) that **passes ordinary testing on x86** and watch TSan flag the race that testing missed. Then a UAF version under ASan.

```cpp
// race.cpp — a buggy SPSC ring with RELAXED (wrong) ordering. Passes on x86; TSan catches it.
// Build (test, passes on x86):  g++ -O2 -std=c++20 race.cpp -o race -pthread
// Build (TSan):                 g++ -O2 -std=c++20 -fsanitize=thread -g race.cpp -o race_tsan -pthread
// (The UAF variant for ASan is the second listing below.)
#include <atomic>
#include <cstdio>
#include <cstdint>
#include <thread>
#include <array>

template <class T, std::size_t CAP>
class BadRing {
    static_assert((CAP & (CAP - 1)) == 0, "power of two");
    std::array<T, CAP> buf_{};   // NOT std::vector<T>{CAP}: that is a one-element initializer list (UB on every push)
    std::atomic<std::size_t> head_{0}, tail_{0};
public:
    bool push(const T& v) {
        auto h = head_.load(std::memory_order_relaxed);
        if (h - tail_.load(std::memory_order_relaxed) == CAP) return false;   // BUG: relaxed
        buf_[h & (CAP-1)] = v;                                                 // data write...
        head_.store(h + 1, std::memory_order_relaxed);                        // BUG: should be RELEASE
        return true;
    }
    bool pop(T& v) {
        auto t = tail_.load(std::memory_order_relaxed);
        if (t == head_.load(std::memory_order_relaxed)) return false;         // BUG: should be ACQUIRE
        v = buf_[t & (CAP-1)];                                                 // ...read RACES the write
        tail_.store(t + 1, std::memory_order_relaxed);
        return true;
    }
};

BadRing<std::uint64_t, 1024> ring;
int main() {
    constexpr long N = 1'000'000;
    std::thread prod([&]{ for (long i=0;i<N;) if (ring.push(i)) ++i; });
    std::uint64_t v; long got = 0;
    std::thread cons([&]{ while (got<N) if (ring.pop(v)) ++got; });
    prod.join(); cons.join();
    std::printf("done (passed? on x86 it 'works')\n");
    return 0;
}
```

And the ASan half — the Ch. 39 reclamation bug as a listing: a lock-free `pop` that frees the node the moment it is unlinked, while a reader that loaded the pointer a few hundred cycles earlier is still about to dereference it:

```cpp
// uaf.cpp — the Ch. 39 bug: a lock-free pop that deletes a node another reader is still dereferencing.
// Build (ASan): g++ -O1 -std=c++20 -fsanitize=address -g uaf.cpp -o uaf_asan -pthread
#include <atomic>
#include <cstdio>
#include <thread>

struct Node { int val; Node* next; };
std::atomic<Node*> top{nullptr};

void push(int v) { Node* n = new Node{v, top.load()}; while (!top.compare_exchange_weak(n->next, n)) {} }
int  peek() {                                              // reader: loads the pointer ...
    Node* n = top.load(std::memory_order_acquire);
    for (int k = 0; k < 2000; ++k) asm volatile("");       // ... does some work while holding it ...
    return n ? n->val : -1;                                // ... and dereferences it — AFTER pop() may have freed it
}
void pop() { Node* n = top.load(); if (n && top.compare_exchange_strong(n, n->next)) delete n; }  // unlink, then FREE: no grace period

int main() {
    for (int i = 0; i < 1000; ++i) push(i);
    std::thread reader([]{ long s = 0; for (int i = 0; i < 200000; ++i) s += peek(); std::printf("reader sum=%ld\n", s); });
    std::thread popper([]{ for (int i = 0; i < 1000; ++i) pop(); });
    reader.join(); popper.join();
    return 0;
}
```

Measured results — g++ 13.3 (libtsan / libasan 13), `-std=c++20`, Linux 6.x, x86-64 (the reports are real, trimmed to the lines that matter; the machine does not change them — Appendix L.3):

```
   ./race          (plain -O2, x86):  "done (passed? on x86 it 'works')" — PASSES, every run. → false confidence.

   ./race_tsan     (-fsanitize=thread -g):
   ==================
   WARNING: ThreadSanitizer: data race (pid=12886)
     Read of size 8 at 0x55cac3487040 by thread T2:
       #0 BadRing<unsigned long, 1024ul>::pop(unsigned long&) race.cpp:27      <- v = buf_[t & (CAP-1)]
     Previous write of size 8 at 0x55cac3487040 by thread T1:
       #0 BadRing<unsigned long, 1024ul>::push(unsigned long const&) race.cpp:20  <- buf_[h & (CAP-1)] = v
     Location is global 'ring' of size 8208 at 0x55cac3487040 (race_tsan+0x4040)
   SUMMARY: ThreadSanitizer: data race race.cpp:27 in BadRing<unsigned long, 1024ul>::pop(unsigned long&)
   ==================
   ThreadSanitizer: reported 1 warnings        <- on the FIRST run, with both stacks

   ./uaf_asan      (-fsanitize=address -g):
   ==================================================================
   ==13014==ERROR: AddressSanitizer: heap-use-after-free on address 0x502000006ed0
   READ of size 4 at 0x502000006ed0 thread T1
       #0 in peek() uaf.cpp:14                                   <- return n->val
   0x502000006ed0 is located 0 bytes inside of 16-byte region
   freed by thread T2 here:
       #0 in operator delete(void*, unsigned long)
       #1 in pop() uaf.cpp:16                                    <- delete n
   previously allocated by thread T0 here:
       #1 in push(int) uaf.cpp:10
```

Read it: the buggy ring **passes ordinary testing on x86** — the `relaxed` ordering is *wrong* (the consumer can read a slot before the producer's write is visible), and it passes only because x86-TSO (Ch. 33) forbids the *hardware* reorderings **and** the compiler happened not to perform the reorderings `relaxed` permits (it may sink `buf_[…] = v` below the `head_` store, or hoist the consumer's `buf_` read above its `head_` load — neither is guaranteed; a different inlining decision or compiler version can break this ring on x86 with no ARM involved). So a million test iterations give *false confidence*. **TSan flags the data race on the first run** — it reasons about the *missing happens-before edge* (the `head_` published relaxed, consumed relaxed → no synchronizes-with — Ch. 33), not about whether values actually clashed, so it catches the latent bug that would only *manifest* on ARM (Appendix A) or under a rare x86 interleaving. That's the entire value: **TSan converts a rare, architecture-dependent, silent ordering bug into a deterministic failure with both stack traces.** Fix it (release on the store, acquire on the load — Ch. 33, 37) and TSan goes quiet. The ASan report shows the complementary catch: `uaf.cpp`'s reader loaded `n`, the popper unlinked *and freed* it (no grace period, no hazard pointer — Ch. 39), and the reader's `n->val` lands in freed memory — ASan names the read, the free, and the allocation, with all three stacks. The workflow this dictates: **build and run the concurrency tests under TSan (and ASan, and UBSan) routinely — they find what x86 testing structurally cannot.**

## 45.4 Techniques

### 45.4.1 Stress testing

Sanitizers only see *executed* interleavings (§45.2), so you must *produce* the rare ones. Be precise about what each needs: TSan needs both racing accesses to *execute* and not be accidentally ordered by incidental synchronisation (a lock, a join, a `seq_cst` counter on the way) — not a timing window; an un-instrumented invariant test needs the real window. Stress serves both — more paths and interleavings, fewer masking edges:

- **Run hot, varied, and long.** Many threads, high contention, many iterations, under TSan/ASan — the goal is to maximize the variety of interleavings the scheduler produces so TSan observes the racy one. A race that needs a specific window is far more likely to surface under heavy concurrent load than a single calm test.
- **Perturb the scheduling.** Add randomized delays/yields (`std::this_thread::yield`, random `sleep`/`pause` at suspect points), run on *more* threads than cores (oversubscribe — the opposite of the hot-path rule, but here it *helps* by forcing context switches and interleavings — Ch. 46), vary thread priorities, and run on machines with different core counts. TSan itself does *not* perturb the schedule (it finds races by happens-before analysis, not schedule exploration); for schedule perturbation use `rr record --chaos` (Ch. 5), randomised yields in the harness, oversubscription, or the systematic checkers of §45.4.3. Useful real `TSAN_OPTIONS`: `history_size=7` (longer stacks), `halt_on_error=1`, `report_atomic_races=1`, `suppressions=file`. The more interleavings explored, the more confidence.
- **Run on ARM / weakly-ordered hardware (Ch. 33, Appendix A).** The single highest-value supplement: ARM exposes the ordering bugs x86 hides. Run the concurrency suite under TSan **on ARM (Graviton)** — many real ordering bugs surface there and nowhere else. If you deploy on x86 only, *still* test on ARM for the ordering coverage.
- **Property/invariant checks under stress.** Beyond "did it crash," assert structural invariants continuously (the queue never loses or duplicates a message, the count is consistent, the sequence is monotonic) so a corruption is *caught*, not silently absorbed. Combine with stress to catch logic bugs sanitizers miss (§45.5).
- **Long-soak and load-replay.** Run the realistic workload (captured market data — Ch. 94) at high rate for hours under sanitizers; market-open-burst replay is especially valuable (it's the load that hits the windows in production).

### 45.4.2 Fuzzing

Fuzzing generates inputs to explore code paths — essential where **untrusted input meets concurrency**, the feed-handler decode path (Ch. 60, 87):

- **Coverage-guided fuzzing (libFuzzer / AFL++).** Mutate inputs to maximize code coverage, finding paths (and their bugs) you didn't think to test. Run **with ASan/UBSan** so a malformed-input bug (buffer overflow in a zero-copy parse — Ch. 60, integer overflow in price math — Ch. 30, a UB — Ch. 23) is caught the instant the fuzzer reaches it. This is the primary defense for the **market-data decode path** (Ch. 60, 87) — feed it malformed/hostile packets and let ASan/UBSan catch the mishandling.
- **Fuzz the message parsers.** The decode of ITCH/OUCH/FIX/SBE (Ch. 60) parses variable-length, untrusted wire data — exactly where bounds bugs live. A fuzz harness that throws random/mutated packets at the parser under ASan finds the buffer overruns and UB that a malicious or malformed feed could trigger (Ch. 87's security framing).
- **Structure-aware fuzzing.** For structured protocols, a grammar/structure-aware fuzzer (or a custom mutator) generates *plausible-but-malformed* messages (valid headers, corrupt bodies) that reach deeper into the parser than random bytes — more effective for protocol code.
- **Differential / replay fuzzing.** Feed the same input to two implementations (or the same one across versions) and diff — catching logic divergences (ties Ch. 94–102's determinism). Fuzz the *replay* path to ensure deterministic processing of arbitrary captures.
- **Fuzzing finds inputs; combine with concurrency.** Fuzzing primarily explores *input* paths; combined with stress (concurrent fuzzing) it can also explore input×interleaving — but its main concurrency value is reaching the parsing/state-machine code that concurrency then operates on.

### 45.4.3 Model checking lock-free code

For the highest assurance on small, critical lock-free primitives, **model checking** *exhaustively* explores all interleavings and memory-model behaviors — proving correctness rather than sampling it:

- **What it does.** A model checker systematically explores **every** possible interleaving (and, for memory-model-aware checkers, every allowed reordering under the C++ model — including weak/relaxed behaviors x86 never exhibits) for a small test, verifying assertions hold in *all* of them. Where TSan *samples* interleavings, model checking *exhausts* them — so it can prove the *absence* of races/bugs for the modeled scenario, not just the absence of *observed* ones.
- **The tools.** **CDSChecker** and **GenMC** (memory-model-aware checkers for C/C++ atomics — they explore relaxed/acquire/release behaviors per the standard model), **Relacy** (a header-only race detector that simulates the memory model), **TLA+/Spin** (model the algorithm abstractly). For C++ lock-free primitives, GenMC/CDSChecker/Relacy check the actual atomic code against the real memory model.
- **When to use it.** The *small, critical* primitives — your SPSC/MPMC ring (Ch. 37), seqlock (Ch. 38), the core of a reclamation scheme (Ch. 39), a hand-rolled lock — where a subtle ordering bug is catastrophic and the state space is small enough to exhaust. Model checking is *expensive* (state-space explosion) so it's for *bounded* tests of *small* primitives, not whole programs.
- **The payoff.** Model checking is the only technique that catches a bug reachable *only* under a relaxed-memory reordering that *neither x86 nor your ARM test* happened to produce — the deepest class of lock-free bug. For a hand-written lock-free primitive you're betting the system on, model-check it; for primitives you can instead take from a vetted library (Ch. 37), that library was (hopefully) already verified — another reason to prefer libraries over hand-rolling.
- **Complements, doesn't replace.** Model checking proves the *small* primitive; TSan/stress/fuzzing cover the *whole system* and the input paths. Use both: model-check the core, sanitize-and-stress the integration.

## 45.5 Pitfalls & anti-patterns: sanitizer-clean but still racy

- **"It passed on x86, so it's correct" (the cardinal sin).** x86-TSO hides missing acquire/release (§45.3, Ch. 33) — code can pass millions of x86 test runs and be fundamentally racy (failing on ARM or a rare interleaving). x86 testing is *structurally incapable* of catching the ordering bugs. **Run TSan; run on ARM; model-check** — don't trust x86 green.
- **Sanitizer-clean but still racy (the limit of sampling).** TSan only flags races on interleavings it *observed*; a race reachable only under an interleaving you didn't hit goes unreported (§45.2). Clean TSan is necessary, **not sufficient**. *Drive* TSan with heavy **stress** (§45.4.1) and exhaust the small primitives with **model checking** (§45.4.3) — and don't conclude "no race" from a few clean runs.
- **Not running the sanitizers at all / not in CI.** Sanitizers find these bugs *only if you run them*. Concurrency code that's never been under TSan/ASan is untested for its most dangerous bug class. **Put TSan/ASan/UBSan builds in CI** (Ch. 102), gating merges — make it impossible to land un-sanitized concurrent code.
- **Running sanitizers in production.** TSan (~10×) / ASan (~2×) slow things down and change timing — they're *testing* tools, not production hardening. (For production, the hardening is `-fstack-protector`/`_FORTIFY_SOURCE`/CFI — Ch. 87, not sanitizers.) Don't ship a sanitizer build; do run it relentlessly in test/CI. The production-capable alternatives — hardened library modes (`_GLIBCXX_ASSERTIONS`, measured at +3–8 % on a decode loop), guard pages, poison-on-free and a hardware data breakpoint armed on the live PID from a sidecar — are Ch. 5 §5.4.5.
- **TSan + ASan together (incompatible).** TSan and ASan can't be combined in one build; run **separate** builds (a TSan job and an ASan job). UBSan combines with ASan. Plan the CI matrix accordingly.
- **Forgetting logic bugs (memory-model-legal but wrong).** A correctly-synchronized algorithm can still be *logically* wrong (right ordering, wrong result) — invisible to TSan. Assert **invariants** continuously under stress (§45.4.1) and test the *behavior*, not just the absence of races.
- **Fuzzing without sanitizers.** Fuzzing that isn't run under ASan/UBSan finds far fewer bugs (it only catches crashes, not silent corruption/UB). Always fuzz *with* sanitizers on (§45.4.2).
- **Trusting hand-rolled lock-free code without model checking.** A hand-written ring/seqlock/reclamation scheme that's only been TSan-tested may harbor a relaxed-reordering bug TSan didn't sample. Model-check it (§45.4.3) — or, better, use a vetted library (Ch. 37) that already was.
- **TSan on fence-based code.** A seqlock or Disruptor written with `std::atomic_thread_fence` (Ch. 38) gets *false positives* from TSan, which does not model standalone fences (§45.2) — and a team that learns to ignore TSan noise soon ignores a real race. Keep a TSan-checkable variant (release/acquire stores and loads, `std::atomic_ref` data) or route the fence-based form to GenMC/CDSChecker (§45.4.3) instead of suppressing.
- **Sanitizer slowdowns masking timing bugs.** TSan/ASan change timing (slower), which can *hide* a race that needs a tight window (or expose different ones). Run *both* sanitized (for detection) and unsanitized-but-stressed (for the real timing), and on multiple core counts.

## 45.6 Exercises & checklist

**Exercises**

1. **TSan catches what x86 hides.** Build `race.cpp` plain and under TSan. Confirm the plain build "passes" on x86 while TSan reports the data race on the first run with both stacks (§45.3). Fix the ordering (release/acquire — Ch. 33) and confirm TSan goes quiet. If you have ARM, run the plain build there — does it fail?
2. **ASan on a reclamation bug.** Build `uaf.cpp` (§45.3) under ASan; reproduce the heap-use-after-free with both the freeing and the reading stacks. Then move the nodes into a pool (Ch. 27) and watch ASan go blind until you poison the free list (§45.2). Fix with hazard pointers / no-free (Ch. 39); confirm ASan clean.
3. **Stress to expose.** Take a race that TSan *doesn't* catch in a single calm run (low contention); crank up threads/iterations/oversubscription and randomized yields until TSan observes the interleaving. Quantify how much stress was needed (§45.4.1) — the lesson about sampling.
4. **Fuzz a parser.** Write a libFuzzer harness for a small message parser (a length-prefixed binary format — Ch. 60) built with ASan+UBSan. Let it run; find a bounds/overflow bug on malformed input. Fix and re-fuzz (ties Ch. 87).
5. **Model-check a primitive.** Take your SPSC ring (Ch. 37) or seqlock (Ch. 38) and check it with GenMC/CDSChecker/Relacy. Introduce a relaxed-ordering bug and confirm the model checker finds it (and that TSan on x86 might not). Appreciate exhaustive vs sampled (§45.4.3).

**Checklist — concurrency correctness tooling**

- [ ] All concurrency code is run under **TSan** (data races / missing happens-before — Ch. 33), **ASan** (UAF/overflow — Ch. 39), and **UBSan** (UB — Ch. 23) — in **separate builds** where required — and these are in **CI** (Ch. 102), gating merges.
- [ ] I **never conclude "correct" from x86 testing alone** — I run TSan, test on **ARM** (Appendix A), and **model-check** small critical primitives (§45.5).
- [ ] Sanitizers are **driven by stress** (many threads, high contention, oversubscription, randomized scheduling, long soak, captured-data replay — §45.4.1) and **continuous invariant assertions** (to catch logic bugs TSan misses).
- [ ] The **message-decode path** (Ch. 60) is **fuzzed (libFuzzer/AFL++) under ASan+UBSan** with malformed/hostile input (ties Ch. 87).
- [ ] Hand-rolled lock-free primitives (ring/seqlock/reclamation) are **model-checked** (GenMC/CDSChecker/Relacy) — or replaced with **vetted libraries** (Ch. 37) that were verified.
- [ ] I treat **sanitizer-clean as necessary, not sufficient** (it only samples executed interleavings — §45.2, §45.5) — and supplement with exhaustive (model checking) and broad (stress/fuzz) coverage.
- [ ] Sanitizers are a **testing** tool, **not run in production** (production hardening is Ch. 87's `_FORTIFY_SOURCE`/stack-protector/CFI); the production build is separate.
- [ ] Fuzzing always runs **with sanitizers on**; runs happen on **multiple core counts / architectures** to vary timing.

## 45.7 References

- The ThreadSanitizer, AddressSanitizer, and UndefinedBehaviorSanitizer documentation (Clang/GCC) and the original papers (Serebryany et al., *"ThreadSanitizer – data race detection in practice"*; *"AddressSanitizer: A Fast Address Sanity Checker"*) — what each catches and how (§45.2-§45.3).
- The libFuzzer and AFL++ documentation — coverage-guided fuzzing with sanitizers (§45.4.2; ties Ch. 87).
- GenMC, CDSChecker (Norris & Demsky, *"CDSChecker: Checking Concurrent Data Structures Written with C/C++ Atomics"*), and Relacy (Vyukov) — memory-model-aware model checking of lock-free code (§45.4.3).
- H. Sutter, *"atomic<> Weapons"* and the C++ memory-model references (Ch. 33) — the happens-before semantics TSan checks against.
- The OSS-Fuzz project and Google's sanitizer/fuzzing-in-CI writeups — running these tools continuously at scale (§45.5, ties Ch. 102).

## 45.8 Additional Reading

- P. McKenney, *Is Parallel Programming Hard?* — validation of concurrent code, stress testing, and formal methods.
- The TLA+ (Lamport) and Spin (Holzmann) model-checking resources — abstract algorithm verification, for the design level above the C++ code.
- Ch. 33 (*Memory Model*) — the happens-before TSan verifies; Ch. 37–44 (*Lock-Free / Seqlocks / Reclamation / Disruptor / Coroutines / Senders*) — the code this validates; Ch. 60 (*Wire Decoding*) — the fuzz target; Ch. 87 (*Secure Programming*) — sanitizers+fuzzing as security tooling; Ch. 102 (*CI/Regression*) — running it all continuously; **Appendix A** — ARM as the weak-ordering test bed.
- **Appendix F** — the sanitizer (ASan / UBSan / TSan / MSan) glossary entry.

---

*Next: Ch. 46 — Context Switching & Its Mitigation, opening Part VII (OS, Scheduling & Isolation): the direct and indirect costs of context switches and syscalls, keeping threads on-core, and busy-poll vs block — the OS-level foundation that the pinned, isolated, busy-polling hot path of the rest of Part VII is built on.*
