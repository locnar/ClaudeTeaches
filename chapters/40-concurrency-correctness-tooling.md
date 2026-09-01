# Part VI — Concurrency

# Chapter 40 — Concurrency Correctness Tooling

> **Prerequisites:** Ch. 30 (the memory model — TSan checks happens-before; the bugs are ordering bugs), Ch. 34–37 (the lock-free structures this validates), Ch. 23/36 (allocation/UAF — ASan's domain), Ch. 21 (UB — UBSan's domain), Ch. 3 (measurement rigor extends to correctness).
>
> **Leads into:** Ch. 72 (secure programming — sanitizers + fuzzing on the decode path), Ch. 76 (CI/regression — these tools belong in CI). Closes **Part VI** — the verification discipline that makes the lock-free work of Ch. 30–38 *trustworthy*.

---

## 40.1 Why it matters: lock-free bugs are non-deterministic

Every chapter of Part VI came with the same warning, and this chapter is its payoff: **concurrent bugs are the worst bugs in software.** A data race, a missing acquire/release (Ch. 30), an ABA (Ch. 34), a use-after-free in a lock-free structure (Ch. 36), a torn seqlock read (Ch. 35) — these manifest *rarely* (a specific timing window), *non-deterministically* (different every run), *architecture-dependently* (x86's strong ordering hides what ARM exposes — Ch. 30), and often *silently* (corrupting data without crashing). You cannot find them by reading the code (the ordering is subtle), by testing once (it passed — this time), or by running on your x86 dev box (which forgives the ordering bugs). A lock-free queue can run correctly for *months* and then corrupt a message at market open under exactly the load that hits the timing window — losing money in a way you can't reproduce. **Concurrent correctness cannot be hoped for; it must be *verified* with tools designed to expose what testing can't.**

The tools exist and they are extraordinarily effective — but each catches a *different class* of bug, and knowing which tool finds which is the chapter's core. **ThreadSanitizer (TSan)** is the headline: a runtime data-race detector that instruments memory accesses and synchronization to build the happens-before graph (Ch. 30) and flag any two accesses to the same location, at least one a write, not ordered by synchronization — i.e. a data race, *even if it didn't actually race on this run*. TSan finds races that testing would *never* trigger, because it reasons about the ordering, not the timing. **ASan** finds memory errors (use-after-free — the Ch. 36 reclamation bug, buffer overflows, leaks); **UBSan** finds undefined behavior (Ch. 21 — the signed overflow, the strict-aliasing violation, the null deref). Together the sanitizers turn a class of silent, rare, catastrophic bugs into a deterministic test failure with a stack trace.

But sanitizers have a fundamental limit that defines the rest of the chapter: **they only detect bugs on code paths and interleavings that actually execute.** TSan reports a race only if both accesses *run* (under some interleaving the scheduler produces); it can't prove the *absence* of races on paths you didn't exercise. So the sanitizers must be *driven* — by **stress testing** (run the concurrent code under heavy, varied load to hit the timing windows), **fuzzing** (generate inputs that explore code paths — especially the message-decode paths of Ch. 53, where untrusted input meets concurrency), and — for the highest assurance on small lock-free primitives — **model checking** (exhaustively explore *all* possible interleavings and memory-model behaviors, proving correctness rather than sampling it). The discipline that closes Part VI: **write the lock-free code, then prove it with TSan + ASan + UBSan under stress and fuzzing, model-check the critical primitives, and run it all in CI — and never trust concurrent code that x86 testing alone "passed."** This chapter covers what each tool can and can't catch (§40.2), running them on a queue (§40.3), the stress/fuzz/model-check techniques (§40.4), and the crucial pitfall that sanitizer-clean does *not* mean correct (§40.5).

## 40.2 Mental model: what TSan/ASan/UBSan can and can't catch

The sanitizers are **runtime** tools (compile with `-fsanitize=...`, run the instrumented binary); each instruments different things and catches a different bug class. Knowing the division of labor is essential — they're complementary, not interchangeable.

- **ThreadSanitizer (TSan, `-fsanitize=thread`)** — **data races and some deadlocks.** Instruments memory accesses and synchronization operations (atomics, locks, thread create/join) to build the **happens-before** relation (Ch. 30). It flags a **data race**: two accesses to the same memory, at least one a write, not ordered by a happens-before edge. Crucially, TSan reports a race *if the two accesses execute under the observed interleaving* — it understands the *synchronization*, so it catches races that didn't manifest as wrong values this run (it's not just "did the values clash," it's "is there a missing happens-before edge"). This is exactly the tool for missing acquire/release (Ch. 30), unsynchronized shared access (Ch. 31), and the subtle ordering bugs in lock-free code. **The single most important concurrency tool.** Cost: ~5-15× slowdown, ~5-10× memory — for testing, not production.
- **AddressSanitizer (ASan, `-fsanitize=address`)** — **memory errors.** Use-after-free (the Ch. 36 reclamation bug, dangling coroutine handles — Ch. 38), heap/stack buffer overflows (Ch. 9, 72), use-after-return, double-free, leaks (with LSan). Instruments allocations with redzones and a shadow map. Catches the *memory-corruption* consequences of concurrency bugs (a freed lock-free node dereferenced). Cost: ~2× slowdown. **Run it alongside TSan** (separate builds — they're incompatible together) — TSan finds the race, ASan finds the UAF the race causes.
- **UndefinedBehaviorSanitizer (UBSan, `-fsanitize=undefined`)** — **undefined behavior.** Signed integer overflow (Ch. 27, 72 — price/qty math), strict-aliasing violations and misaligned access (Ch. 21), null/invalid pointer use, shifts past width (Ch. 28), invalid enum/bool values. Cheap (~little overhead); compatible with ASan. Catches the UB that miscompiles (Ch. 21) — relevant because aliasing/UB bugs interact viciously with concurrency.
- **What none of them catch (the limit — §40.1, §40.5):**
  - **Bugs on un-executed paths/interleavings.** TSan only sees the interleaving the scheduler happened to produce *this run* — it doesn't explore *all* interleavings. A race reachable only under a rare interleaving you didn't hit goes unreported. (This is why you need stress testing — §40.4.1 — to *produce* the interleavings, and model checking — §40.4.3 — to *exhaust* them.)
  - **Memory-model-legal-but-wrong logic.** A correctly-synchronized-but-logically-wrong algorithm (right happens-before, wrong result) is invisible to TSan — that's a *logic* bug, not a race.
  - **The x86-hides-ARM problem partially.** TSan reasons about the *abstract* memory model (catches missing synchronization regardless of x86's strong ordering — better than testing), but subtle weak-ordering bugs and `relaxed`-ordering correctness still benefit from running on ARM and model-checking.

The model: **TSan = data races / missing happens-before (the lock-free ordering bug — the key tool); ASan = memory errors / UAF (the reclamation bug); UBSan = undefined behavior. They're runtime, complementary, and only see *executed* code/interleavings — so they must be *driven* by stress/fuzzing and supplemented by model checking, and "sanitizer-clean on x86" is necessary but not sufficient.**

## 40.3 Measure it: running the sanitizers on a queue

The demonstration: take a deliberately-buggy lock-free queue (missing the acquire/release of Ch. 30) that **passes ordinary testing on x86** and watch TSan flag the race that testing missed. Then a UAF version under ASan.

```cpp
// race.cpp — a buggy SPSC ring with RELAXED (wrong) ordering. Passes on x86; TSan catches it.
// Build (test, passes on x86):  g++ -O2 -std=c++20 race.cpp -o race -pthread
// Build (TSan):                 g++ -O2 -std=c++20 -fsanitize=thread -g race.cpp -o race_tsan -pthread
// Build (ASan, for the UAF variant): g++ -fsanitize=address -g ...
#include <atomic>
#include <cstdio>
#include <cstdint>
#include <thread>
#include <vector>

template <class T, std::size_t CAP>
class BadRing {
    std::vector<T> buf_{CAP};
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

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP), illustrative:

```
   ./race          (plain, x86):     "done" — PASSES. x86-TSO hides the missing acquire/release (Ch. 29).
                                       runs millions of times, never visibly wrong. → false confidence.

   ./race_tsan     (TSan):           ==================
                                       WARNING: ThreadSanitizer: data race
                                         Write of size 8 by thread T1 (push: buf_[...] = v)
                                         Previous read of size 8 by thread T2 (pop: v = buf_[...])
                                         (missing happens-before: head_ stored relaxed, loaded relaxed)
                                       → TSan catches it on the FIRST run, with both stacks.

   UAF variant     (ASan):           ==================
                                       ERROR: AddressSanitizer: heap-use-after-free
                                         READ ... by lock-free reader, freed by reclaimer (Ch. 35)
```

Read it: the buggy ring **passes ordinary testing on x86** — the `relaxed` ordering is *wrong* (the consumer can read a slot before the producer's write is visible), but x86-TSO (Ch. 30) happens to enforce enough ordering that it never visibly fails, so a million test iterations give *false confidence*. **TSan flags the data race on the first run** — it reasons about the *missing happens-before edge* (the `head_` published relaxed, consumed relaxed → no synchronizes-with — Ch. 30), not about whether values actually clashed, so it catches the latent bug that would only *manifest* on ARM (Appendix A) or under a rare x86 interleaving. That's the entire value: **TSan converts a rare, architecture-dependent, silent ordering bug into a deterministic failure with both stack traces.** Fix it (release on the store, acquire on the load — Ch. 30, 34) and TSan goes quiet. The ASan line shows the complementary catch: a use-after-free from a reclamation bug (Ch. 36). The workflow this dictates: **build and run the concurrency tests under TSan (and ASan, and UBSan) routinely — they find what x86 testing structurally cannot.**

## 40.4 Techniques

### 40.4.1 Stress testing

Sanitizers only see *executed* interleavings (§40.2), so you must *produce* the rare ones — stress testing drives the code hard enough to hit the timing windows:

- **Run hot, varied, and long.** Many threads, high contention, many iterations, under TSan/ASan — the goal is to maximize the variety of interleavings the scheduler produces so TSan observes the racy one. A race that needs a specific window is far more likely to surface under heavy concurrent load than a single calm test.
- **Perturb the scheduling.** Add randomized delays/yields (`std::this_thread::yield`, random `sleep`/`pause` at suspect points), run on *more* threads than cores (oversubscribe — the opposite of the hot-path rule, but here it *helps* by forcing context switches and interleavings — Ch. 41), vary thread priorities, and run on machines with different core counts. TSan also has a mode to randomize scheduling. The more interleavings explored, the more confidence.
- **Run on ARM / weakly-ordered hardware (Ch. 30, Appendix A).** The single highest-value supplement: ARM exposes the ordering bugs x86 hides. Run the concurrency suite under TSan **on ARM (Graviton)** — many real ordering bugs surface there and nowhere else. If you deploy on x86 only, *still* test on ARM for the ordering coverage.
- **Property/invariant checks under stress.** Beyond "did it crash," assert structural invariants continuously (the queue never loses or duplicates a message, the count is consistent, the sequence is monotonic) so a corruption is *caught*, not silently absorbed. Combine with stress to catch logic bugs sanitizers miss (§40.5).
- **Long-soak and load-replay.** Run the realistic workload (captured market data — Ch. 75) at high rate for hours under sanitizers; market-open-burst replay is especially valuable (it's the load that hits the windows in production).

### 40.4.2 Fuzzing

Fuzzing generates inputs to explore code paths — essential where **untrusted input meets concurrency**, the feed-handler decode path (Ch. 53, 72):

- **Coverage-guided fuzzing (libFuzzer / AFL++).** Mutate inputs to maximize code coverage, finding paths (and their bugs) you didn't think to test. Run **with ASan/UBSan** so a malformed-input bug (buffer overflow in a zero-copy parse — Ch. 53, integer overflow in price math — Ch. 27, a UB — Ch. 21) is caught the instant the fuzzer reaches it. This is the primary defense for the **market-data decode path** (Ch. 53, 72) — feed it malformed/hostile packets and let ASan/UBSan catch the mishandling.
- **Fuzz the message parsers.** The decode of ITCH/OUCH/FIX/SBE (Ch. 53) parses variable-length, untrusted wire data — exactly where bounds bugs live. A fuzz harness that throws random/mutated packets at the parser under ASan finds the buffer overruns and UB that a malicious or malformed feed could trigger (Ch. 72's security framing).
- **Structure-aware fuzzing.** For structured protocols, a grammar/structure-aware fuzzer (or a custom mutator) generates *plausible-but-malformed* messages (valid headers, corrupt bodies) that reach deeper into the parser than random bytes — more effective for protocol code.
- **Differential / replay fuzzing.** Feed the same input to two implementations (or the same one across versions) and diff — catching logic divergences (ties Ch. 75–76's determinism). Fuzz the *replay* path to ensure deterministic processing of arbitrary captures.
- **Fuzzing finds inputs; combine with concurrency.** Fuzzing primarily explores *input* paths; combined with stress (concurrent fuzzing) it can also explore input×interleaving — but its main concurrency value is reaching the parsing/state-machine code that concurrency then operates on.

### 40.4.3 Model checking lock-free code

For the highest assurance on small, critical lock-free primitives, **model checking** *exhaustively* explores all interleavings and memory-model behaviors — proving correctness rather than sampling it:

- **What it does.** A model checker systematically explores **every** possible interleaving (and, for memory-model-aware checkers, every allowed reordering under the C++ model — including weak/relaxed behaviors x86 never exhibits) for a small test, verifying assertions hold in *all* of them. Where TSan *samples* interleavings, model checking *exhausts* them — so it can prove the *absence* of races/bugs for the modeled scenario, not just the absence of *observed* ones.
- **The tools.** **CDSChecker** and **GenMC** (memory-model-aware checkers for C/C++ atomics — they explore relaxed/acquire/release behaviors per the standard model), **Relacy** (a header-only race detector that simulates the memory model), **TLA+/Spin** (model the algorithm abstractly). For C++ lock-free primitives, GenMC/CDSChecker/Relacy check the actual atomic code against the real memory model.
- **When to use it.** The *small, critical* primitives — your SPSC/MPMC ring (Ch. 34), seqlock (Ch. 35), the core of a reclamation scheme (Ch. 36), a hand-rolled lock — where a subtle ordering bug is catastrophic and the state space is small enough to exhaust. Model checking is *expensive* (state-space explosion) so it's for *bounded* tests of *small* primitives, not whole programs.
- **The payoff.** Model checking is the only technique that catches a bug reachable *only* under a relaxed-memory reordering that *neither x86 nor your ARM test* happened to produce — the deepest class of lock-free bug. For a hand-written lock-free primitive you're betting the system on, model-check it; for primitives you can instead take from a vetted library (Ch. 34), that library was (hopefully) already verified — another reason to prefer libraries over hand-rolling.
- **Complements, doesn't replace.** Model checking proves the *small* primitive; TSan/stress/fuzzing cover the *whole system* and the input paths. Use both: model-check the core, sanitize-and-stress the integration.

## 40.5 Pitfalls & anti-patterns: sanitizer-clean but still racy

- **"It passed on x86, so it's correct" (the cardinal sin).** x86-TSO hides missing acquire/release (§40.3, Ch. 30) — code can pass millions of x86 test runs and be fundamentally racy (failing on ARM or a rare interleaving). x86 testing is *structurally incapable* of catching the ordering bugs. **Run TSan; run on ARM; model-check** — don't trust x86 green.
- **Sanitizer-clean but still racy (the limit of sampling).** TSan only flags races on interleavings it *observed*; a race reachable only under an interleaving you didn't hit goes unreported (§40.2). Clean TSan is necessary, **not sufficient**. *Drive* TSan with heavy **stress** (§40.4.1) and exhaust the small primitives with **model checking** (§40.4.3) — and don't conclude "no race" from a few clean runs.
- **Not running the sanitizers at all / not in CI.** Sanitizers find these bugs *only if you run them*. Concurrency code that's never been under TSan/ASan is untested for its most dangerous bug class. **Put TSan/ASan/UBSan builds in CI** (Ch. 76), gating merges — make it impossible to land un-sanitized concurrent code.
- **Running sanitizers in production.** TSan (~10×) / ASan (~2×) slow things down and change timing — they're *testing* tools, not production hardening. (For production, the hardening is `-fstack-protector`/`_FORTIFY_SOURCE`/CFI — Ch. 72, not sanitizers.) Don't ship a sanitizer build; do run it relentlessly in test/CI.
- **TSan + ASan together (incompatible).** TSan and ASan can't be combined in one build; run **separate** builds (a TSan job and an ASan job). UBSan combines with ASan. Plan the CI matrix accordingly.
- **Forgetting logic bugs (memory-model-legal but wrong).** A correctly-synchronized algorithm can still be *logically* wrong (right ordering, wrong result) — invisible to TSan. Assert **invariants** continuously under stress (§40.4.1) and test the *behavior*, not just the absence of races.
- **Fuzzing without sanitizers.** Fuzzing that isn't run under ASan/UBSan finds far fewer bugs (it only catches crashes, not silent corruption/UB). Always fuzz *with* sanitizers on (§40.4.2).
- **Trusting hand-rolled lock-free code without model checking.** A hand-written ring/seqlock/reclamation scheme that's only been TSan-tested may harbor a relaxed-reordering bug TSan didn't sample. Model-check it (§40.4.3) — or, better, use a vetted library (Ch. 34) that already was.
- **Sanitizer slowdowns masking timing bugs.** TSan/ASan change timing (slower), which can *hide* a race that needs a tight window (or expose different ones). Run *both* sanitized (for detection) and unsanitized-but-stressed (for the real timing), and on multiple core counts.

## 40.6 Exercises & checklist

**Exercises**

1. **TSan catches what x86 hides.** Build `race.cpp` plain and under TSan. Confirm the plain build "passes" on x86 while TSan reports the data race on the first run with both stacks (§40.3). Fix the ordering (release/acquire — Ch. 30) and confirm TSan goes quiet. If you have ARM, run the plain build there — does it fail?
2. **ASan on a reclamation bug.** Build a lock-free stack that frees immediately on pop (the Ch. 36 UAF) under ASan; reproduce the heap-use-after-free with both the freeing and the reading stacks. Fix with hazard pointers / no-free (Ch. 36); confirm ASan clean.
3. **Stress to expose.** Take a race that TSan *doesn't* catch in a single calm run (low contention); crank up threads/iterations/oversubscription and randomized yields until TSan observes the interleaving. Quantify how much stress was needed (§40.4.1) — the lesson about sampling.
4. **Fuzz a parser.** Write a libFuzzer harness for a small message parser (a length-prefixed binary format — Ch. 53) built with ASan+UBSan. Let it run; find a bounds/overflow bug on malformed input. Fix and re-fuzz (ties Ch. 72).
5. **Model-check a primitive.** Take your SPSC ring (Ch. 34) or seqlock (Ch. 35) and check it with GenMC/CDSChecker/Relacy. Introduce a relaxed-ordering bug and confirm the model checker finds it (and that TSan on x86 might not). Appreciate exhaustive vs sampled (§40.4.3).

**Checklist — concurrency correctness tooling**

- [ ] All concurrency code is run under **TSan** (data races / missing happens-before — Ch. 30), **ASan** (UAF/overflow — Ch. 36), and **UBSan** (UB — Ch. 21) — in **separate builds** where required — and these are in **CI** (Ch. 76), gating merges.
- [ ] I **never conclude "correct" from x86 testing alone** — I run TSan, test on **ARM** (Appendix A), and **model-check** small critical primitives (§40.5).
- [ ] Sanitizers are **driven by stress** (many threads, high contention, oversubscription, randomized scheduling, long soak, captured-data replay — §40.4.1) and **continuous invariant assertions** (to catch logic bugs TSan misses).
- [ ] The **message-decode path** (Ch. 53) is **fuzzed (libFuzzer/AFL++) under ASan+UBSan** with malformed/hostile input (ties Ch. 72).
- [ ] Hand-rolled lock-free primitives (ring/seqlock/reclamation) are **model-checked** (GenMC/CDSChecker/Relacy) — or replaced with **vetted libraries** (Ch. 34) that were verified.
- [ ] I treat **sanitizer-clean as necessary, not sufficient** (it only samples executed interleavings — §40.2, §40.5) — and supplement with exhaustive (model checking) and broad (stress/fuzz) coverage.
- [ ] Sanitizers are a **testing** tool, **not run in production** (production hardening is Ch. 72's `_FORTIFY_SOURCE`/stack-protector/CFI); the production build is separate.
- [ ] Fuzzing always runs **with sanitizers on**; runs happen on **multiple core counts / architectures** to vary timing.

## 40.7 References

- The ThreadSanitizer, AddressSanitizer, and UndefinedBehaviorSanitizer documentation (Clang/GCC) and the original papers (Serebryany et al., *"ThreadSanitizer – data race detection in practice"*; *"AddressSanitizer: A Fast Address Sanity Checker"*) — what each catches and how (§40.2-§40.3).
- The libFuzzer and AFL++ documentation — coverage-guided fuzzing with sanitizers (§40.4.2; ties Ch. 72).
- GenMC, CDSChecker (Norris & Demsky, *"CDSChecker: Checking Concurrent Data Structures Written with C/C++ Atomics"*), and Relacy (Vyukov) — memory-model-aware model checking of lock-free code (§40.4.3).
- H. Sutter, *"atomic<> Weapons"* and the C++ memory-model references (Ch. 30) — the happens-before semantics TSan checks against.
- The OSS-Fuzz project and Google's sanitizer/fuzzing-in-CI writeups — running these tools continuously at scale (§40.5, ties Ch. 76).

## 40.8 Additional Reading

- P. McKenney, *Is Parallel Programming Hard?* — validation of concurrent code, stress testing, and formal methods.
- The TLA+ / Spin model-checking resources (Lamport) — abstract algorithm verification, for the design level above the C++ code.
- Ch. 30 (*Memory Model*) — the happens-before TSan verifies; Ch. 34–38 (*Lock-Free / Seqlocks / Reclamation / Disruptor / Coroutines*) — the code this validates; Ch. 53 (*Wire Decoding*) — the fuzz target; Ch. 72 (*Secure Programming*) — sanitizers+fuzzing as security tooling; Ch. 76 (*CI/Regression*) — running it all continuously; **Appendix A** — ARM as the weak-ordering test bed.
- **Appendix F** — TSan/ASan/UBSan/model-checking glossary.

---

*Next: Ch. 41 — Context Switching & Its Mitigation, opening Part VII (OS, Scheduling & Isolation): the direct and indirect costs of context switches and syscalls, keeping threads on-core, and busy-poll vs block — the OS-level foundation that the pinned, isolated, busy-polling hot path of the rest of Part VII is built on.*
