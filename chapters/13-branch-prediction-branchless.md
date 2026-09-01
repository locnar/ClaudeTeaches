# Part II — CPU Microarchitecture

# Chapter 13 — Branch Prediction & Branchless Programming

> **Prerequisites:** Ch. 11 (the out-of-order back-end — a mispredict throws away the speculative work that engine had in flight), Ch. 12 (the front-end the predictor *steers* — a mispredict is a front-end resteer plus a back-end flush), Ch. 2 (top-down — this is the **Bad Speculation** quadrant), Ch. 3–4 (benchmarking and reading asm — used in §13.3, §13.5).
>
> **Leads into:** Ch. 14 (indirect branches — virtual calls and function pointers, the harder *target* prediction problem this chapter's *direction* prediction sets up), Ch. 28 (bit tricks — branchless min/max/abs/sign and masking, the toolkit §13.4.3 draws on), Ch. 29 (SIMD — vectorized code is branchless by construction). The `[[likely]]`/ `[[unlikely]]` *layout* hints of Ch. 12 are the complement to the *prediction* this chapter is about.

---

## 13.1 Why it matters: a mispredict costs ~15–20 cycles

A modern core does not wait to find out which way a branch goes. By the time a conditional jump is *resolved* — its condition actually computed — the front-end (Ch. 12) has already fetched, decoded, and the back-end (Ch. 11) has already *speculatively executed* dozens of instructions down the path it *predicted*. When the prediction is right, this is free: the branch costs essentially nothing and the pipeline never hiccups. When it's wrong, the core must **squash** all that speculative work, flush the pipeline, resteer the front-end to the correct address, and refill — a penalty of roughly **15–20 cycles** on current Intel/AMD cores. At ~3 GHz that's ~5–7 ns of pure waste, *per mispredict*, doing nothing.

Put that in tick-to-trade terms. A feed handler's hot loop might branch on every message: *is this an add or a cancel? is the price inside the book? is this our symbol? did the sequence number gap?* If one of those branches is **data-dependent and unpredictable** — its direction effectively random with respect to the predictor's history — you pay the full mispredict penalty on a large fraction of messages. A single 50/50 unpredictable branch in the hot path can add several nanoseconds to *every* tick, and worse, it adds them to the **tail**: mispredicts cluster on exactly the unusual inputs (the wide market, the burst, the malformed packet) that you most need to handle fast. The whole book's north-star — p99/p99.9 (Ch. 1) — is precisely where bad speculation bites.

The good news is that branch prediction is *very* good at predictable patterns — loop back-edges, always-taken error checks, branches correlated with recent history are predicted ~99%+ and cost nothing. So the goal of this chapter is **not** "remove all branches." It's narrower and more useful: **identify the branches the predictor *cannot* learn — the data-dependent, high-entropy ones — and either make them predictable, hoist them out of the hot path, or remove them entirely with branchless code (`cmov`, masks, lookup tables).** And, crucially, to know *when branchless is a pessimization*: a `cmov` converts a control dependency into a *data* dependency (Ch. 11), and on a *predictable* branch the predicted branch is faster. The discipline is, as always, measure first (Ch. 2): this is the **Bad Speculation** quadrant, and if your branches already predict well, the techniques here cost you performance rather than buy it.

---

## 13.2 Mental model

### 13.2.1 Predictor behavior and history

The branch predictor's job, every cycle the front-end fetches, is to answer two questions *before* the branch executes: **taken or not-taken?** (direction) and, if taken, **to what address?** (target). This chapter is about *direction* prediction for conditional branches; *target* prediction for indirect branches is Ch. 14. The hardware is a set of cooperating predictors:

- **Direction prediction is history-based, not per-branch-static.** Modern predictors (TAGE and perceptron variants) correlate a branch's outcome with the **global history** of recent branch outcomes — a shift register of the last *N* taken/not-taken decisions, hashed with the branch address to index prediction tables. This is why the predictor can learn startlingly complex patterns: not just "this branch is usually taken," but "this branch is taken *when these other branches went this way*." Correlated, repeating, and even moderately long periodic patterns are learned and predicted ~99%+.

- **What it learns well vs badly.** The predictor excels at:
  - *Biased* branches — an error check taken 0.01% of the time is predicted not-taken every time, near-perfectly (this is why a well-marked rare path, Ch. 12, is nearly free).
  - *Loop* branches — the back-edge taken (N−1) times then not, learned by dedicated loop predictors.
  - *Correlated* branches — direction determined by recent control flow.

  It fails on **high-entropy, data-dependent** branches: a condition that is genuinely ~50/50 and *uncorrelated* with history — e.g. `if (price[i] > threshold)` on random-ish market data, or `if (side == BUY)` on a balanced order flow. No history pattern predicts a coin flip; the predictor sits near its 50% floor and you eat a mispredict roughly every other time.

- **Finite capacity and aliasing.** History tables and the BTB (branch target buffer) are finite. A hot path with *thousands* of distinct branches (over-inlined, sprawling code — Ch. 12) can **alias** in the tables: two branches collide on an entry and destructively interfere, degrading prediction even for individually-predictable branches. Smaller, denser hot code (Ch. 12) helps the predictor too — another reason the front-end and prediction chapters are siblings.

- **Cold predictors.** After a quiet spell the history tables are stale/evicted; the first pass through a rarely-run path mispredicts more (a cold-start tail — Ch. 46). Steady-state hot loops warm the predictor; the rare-but-latency-critical path may not be warm when it matters.

The actionable model: **a branch is cheap if the predictor can learn its pattern, and expensive in proportion to its *entropy given history*.** Your lever is to lower that entropy (make it predictable or biased), eliminate the branch, or move it off the hot path.

### 13.2.2 Misprediction cost and the pipeline flush

Why ~15–20 cycles? Walk the pipeline (Ch. 11–12). When the front-end predicts a branch, it keeps fetching and the back-end keeps speculatively executing down the predicted path, tagging that work as speculative. The branch's condition is computed somewhere in the execution units — possibly *many* cycles later if it depends on a load that missed (Ch. 7) or a long dependency chain (Ch. 11). When it resolves and the prediction was **wrong**:

```
   predicted path (wrong):  fetch ─► decode ─► execute ─► ... (dozens of in-flight µops)
                                                   │
                            branch RESOLVES here ──┘  ▶ all younger speculative work SQUASHED
                                                          (it was on the wrong path)
   recovery:                resteer front-end ─► refetch correct target ─► redecode ─► refill
                            └──────────── ~15–20 cycle bubble: back-end starved ────────────┘
```

The penalty is essentially **the pipeline depth from fetch to branch-resolve**: everything in flight on the wrong path is discarded, and the front-end must restart from the correct address and refill the whole pipeline before the back-end retires useful work again. Two consequences worth internalizing:

- **The cost is roughly fixed (~the pipe depth), regardless of how "simple" the branch is.** An `if` on a trivially cheap condition still costs the full flush if mispredicted — the *condition's* cost is irrelevant; the *misprediction* is the cost.
- **A mispredict whose condition depends on a cache miss is the worst case.** The branch can't resolve until the missing load returns (~hundreds of cycles, Ch. 7), so the core speculates further and further down the wrong path, then squashes *more* work. Branches on freshly-loaded, cache-missing data are doubly dangerous.

In top-down terms (Ch. 2), this is **Bad Speculation**: issue slots wasted on µops that were later squashed. Its `perf` signature — `branch-misses` high, and the top-down Bad-Speculation fraction elevated — is distinct from Front-End-Bound (Ch. 12) and Back-End-Bound (Ch. 7, 11), and it's the prerequisite measurement before reaching for §13.4.

---

## 13.3 Measure it: branch-mispredict rate on data-dependent branches

The experiment isolates the *only* thing that matters: branch **predictability**. Sum the elements of an array that exceed a threshold — identical work, identical memory access — but run it on two datasets: one **sorted** (so `a[i] >= threshold` is false-false-…-true-true-… — one direction change, trivially predicted) and one **shuffled** (so the branch is ~50/50 and unpredictable). Same instructions, same cache behavior; only the branch entropy differs. This is the classic "why is processing a sorted array faster" demonstration, recast as a measurement.

```cpp
// branch.cpp — same conditional sum on sorted vs shuffled data.
// Build: g++ -O2 -std=c++20 -march=native branch.cpp -o branch
//   (Keep the compiler from turning the branch into a cmov for THIS experiment — see note.)
// Run pinned, turbo off:  taskset -c 2 ./branch sorted   |   ./branch shuffled
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <random>
#include <vector>
#include <chrono>

int main(int argc, char** argv) {
    bool sorted = (argc > 1) && std::strcmp(argv[1], "sorted") == 0;
    constexpr std::size_t N = 1u << 15;            // 32K ints ~128KB: L2-resident, same both runs
    constexpr int REPS = 4000;
    std::vector<std::int32_t> a(N);
    std::mt19937 rng(12345);
    for (auto& x : a) x = std::int32_t(rng() & 0xFF);   // values 0..255
    if (sorted) std::sort(a.begin(), a.end());          // ONLY difference: branch predictability

    const std::int32_t threshold = 128;
    auto t0 = std::chrono::steady_clock::now();
    std::int64_t sum = 0;
    for (int r = 0; r < REPS; ++r)
        for (std::size_t i = 0; i < N; ++i)
            if (a[i] >= threshold)                  // data-dependent branch
                sum += a[i];                        // taken ~50% (shuffled) vs in a run (sorted)
    auto t1 = std::chrono::steady_clock::now();
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
    std::printf("%-9s sum=%lld  %.3f ns/elem\n", sorted ? "sorted" : "shuffled",
                (long long)sum, ns / ((double)REPS * N));
    return 0;
}
```

Profile both with `perf stat -e cycles,instructions,branches,branch-misses, L1-dcache-load-misses`. Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP), `-O2 -march=native`, pinned, turbo off (illustrative; the *ratio* is the point):

```
                                 sorted (predictable)   shuffled (unpredictable)
ns / element                        ~0.4 ns                ~1.6 ns        <- ~4x slower
IPC                                 high (~3+)             low (~1)
branch-misses / branch              ~0.1%                  ~50%           <- the whole story
branch-misses (count)               ~tiny                  HUGE
L1-dcache-load-misses               ~same                  ~same          <- memory NOT the cause
instructions                        ~same                  ~same          <- identical work
```

Read it the Ch. 2 way: **identical instructions, identical memory traffic — and 4× slower, entirely from `branch-misses`.** The sorted array's branch flips direction *once*; the predictor learns it and the loop runs near peak IPC. The shuffled array's branch is a coin flip; the predictor floors at ~50% misprediction and every other element eats a ~15–20-cycle flush. That is the **Bad Speculation** fingerprint: high `branch-misses`, low IPC, clean memory. When you see it, you have an unpredictable branch on the hot path — and §13.4 is how you remove it. (Note: at higher `-O2`/`-O3` the compiler may *itself* convert this exact loop to a branchless `cmov` form — §13.5 — erasing the gap; to *observe* the mispredict cost you may need to obscure the pattern or inspect the asm to confirm a real branch was emitted.)

---

## 13.4 Techniques

The decision tree, applied only after the §13.3 signature confirms a real mispredict problem: **(1)** can I make the branch *predictable* (sort/partition the data, hoist an invariant branch out of the loop, specialize)? **(2)** if not, can I *remove* it — `cmov`, a mask, a lookup table, arithmetic? **(3)** if neither, can I move the unpredictable decision *off* the hot path entirely (batch, pre-classify)? Branchless is technique (2), and it is not always the answer — §13.6.

### 13.4.1 Conditional moves vs branches

A **conditional move** (`cmov` on x86) computes *both* possible results and selects one based on a flag, with **no branch** — so there is nothing to mispredict. The canonical transform:

```cpp
// Branchy: a control dependency the predictor must guess.
int r = (a > b) ? a : b;          // may compile to a branch OR a cmov (§12.5)

// Branchless idioms the compiler recognizes (and you can write explicitly):
int r = (a > b) ? a : b;          // max — std::max; compilers emit cmov/maxss readily
int s = x & -(int)cond;           // select 0 or x with a mask (cond is 0/1)
int sel = b ^ ((a ^ b) & -(int)(a > b));   // branchless select a:b  (Ch. 27)
```

The critical trade-off, and the whole reason `cmov` is not a free win: **`cmov` replaces a *control* dependency with a *data* dependency** (Ch. 11). The branch version, *when predicted correctly*, breaks the dependency — the core speculates past it and overlaps work, so a well-predicted branch can be *faster* than a `cmov` because the `cmov` forces the result to wait for the condition's inputs (and lengthens the dependency chain). `cmov` wins precisely when the branch is **unpredictable**: you trade a ~15–20-cycle mispredict (sometimes) for a ~1–2-cycle `cmov` (always). The rule:

- **Unpredictable branch → `cmov`/branchless wins** (no mispredict penalty).
- **Predictable branch → keep the branch** (correct prediction is cheaper than the forced data dependency; §13.6).

Compilers apply this heuristic themselves but conservatively and without your knowledge of the *data distribution* — they can't know the branch is 50/50. So you guide them: write `std::max`/`std::min`/ternaries the optimizer recognizes, use `[[likely]]`/`[[unlikely]]` *only* when the bias is real (a hint of "predictable" discourages `cmov`), and **verify the emission in the asm** (§13.5). Where you *need* branchless and the compiler won't cooperate, write the mask/`cmov` idiom explicitly.

### 13.4.2 Lookup tables to replace control flow

When a value selects among several outcomes, a **table indexed by the value** replaces a chain of branches with a single (predictable) load:

```cpp
// Branchy classification: a chain of data-dependent compares.
int weight(char c) {
    if (c == 'A') return 3; else if (c == 'C') return 7;
    else if (c == 'M') return 2; else return 0;          // mispredicts on mixed input
}

// Table-driven: one load, no data-dependent branch.
static constexpr std::array<std::int8_t, 256> kWeight = make_weight_table();
int weight(unsigned char c) { return kWeight[c]; }       // index the table; branch gone
```

This is the workhorse for **message-type dispatch** in a feed handler: rather than an `if/else` ladder or a `switch` over message types (which may compile to a branch chain — Ch. 14), index a table of handlers or precomputed parameters by the type byte. It converts unpredictable control flow into a predictable, cache-resident *data* access. Considerations:

- **The table is a *data* cache access** (Ch. 7) — keep it small and hot. A 256-byte table stays in L1d; a large sparse table trades branch mispredicts for cache misses, which can be worse. Size and measure.
- **Precompute at compile time.** Build the table with `constexpr`/`consteval` (Ch. 18) so it costs nothing at runtime and lives in read-only memory.
- **Branchless arithmetic is a "table" too.** Sometimes the mapping is a formula (`base + scale*x`), avoiding even the load — the limit case of this technique (Ch. 28).
- **A jump table is the control-flow cousin** — a `switch` the compiler lowers to an indexed *indirect jump*. That's still an indirect branch with its own (BTB) prediction problem — Ch. 14 — so a *data* lookup that avoids the indirect jump is often preferable on the hot path.

### 13.4.3 Removing branches from the hot path

Beyond `cmov` and tables, a kit of transforms that delete or relocate branches:

- **Branchless min/max/abs/sign/clamp via masks and bit tricks** (Ch. 28). `abs`, `sign`, `clamp`, saturating arithmetic, and "select" can all be written with shifts and masks that emit no branch — the standard library's `std::min`/`std::max`/`std::clamp` and `std::abs` already do this where profitable.
- **Predication via masking.** Instead of `if (cond) acc += x;`, compute `acc += x & -(mask)` (or multiply by a 0/1 predicate). The work is always done; the *result* is masked. This is the scalar shadow of SIMD predication (Ch. 29) and the natural way to vectorize a conditional accumulate.
- **Hoist loop-invariant branches out of the loop.** A branch whose condition doesn't change across iterations belongs *outside* the loop (or specialize the loop into two versions, one per branch direction). The compiler does this (loop unswitching) when it can prove invariance; help it by making invariance obvious (a `const` flag, a template parameter — Ch. 18–19).
- **Sort/partition to make branches predictable** (§13.3's lesson, used deliberately). If you control data order, grouping like cases together turns a high-entropy branch into long predictable runs — often cheaper than going branchless, and it improves locality too. (In a feed handler: process a batch grouped by message type.)
- **Pre-classify off the hot path.** Decide the unpredictable thing *once*, early, and store the outcome (a tag, a function pointer, a pre-split bucket), so the hot loop branches on something predictable or not at all. Moving the entropy upstream (setup, Ch. 1's hot/cold split) is often better than fighting it in the inner loop.

The unifying idea: an unpredictable branch is a bet the predictor keeps losing. You can stop betting (compute both sides — `cmov`/mask), look up the answer (table), or change the data so the bet becomes easy (sort/partition/pre-classify). Pick by *measuring* which is cheapest for *your* distribution — and confirm the codegen.

---

## 13.5 Verify the codegen: `cmov` vs branch emission

Whether the compiler emits a *branch* or a *`cmov`* for `(a > b) ? a : b` is the central codegen question of this chapter, and it's verifiable. All snippets below are **verified** Clang output (`--target=x86_64-linux-gnu -O2 -march=x86-64-v3`).

**A select the compiler makes branchless.** `std::max` of two ints compiles to a compare and a `cmov` — no branch, nothing to mispredict:

```asm
maxi(int, int):
        cmp     edi, esi
        mov     eax, esi
        cmovge  eax, edi        ; eax = (edi >= esi) ? edi : esi  — branchless select
        ret
```

There is no `jmp`/`jcc` over the alternative — `cmovge` selects in a single µop. This is the form you *want* for an unpredictable select: constant ~1-cycle cost, immune to misprediction.

**The §13.3 loop, branchless.** At `-O2`/`-O3` the compiler often converts the conditional *accumulate* itself into a branchless masked add — which is exactly why the sorted/shuffled gap can *vanish* (§13.3's caveat):

```asm
.loop:                                  ; sum += (a[i] >= 128) ? a[i] : 0
        mov     ecx, dword ptr [rdi + 4*rax]
        xor     edx, edx
        cmp     ecx, 128
        cmovl   ecx, edx                ; if a[i] < 128, replace with 0  — no branch
        add     ... , ecx               ; always add (0 or a[i]) → no mispredict possible
        ...
```

The data-dependent *branch* is gone, replaced by a `cmovl` that zeroes the addend when the predicate is false. The loop now runs at the same speed on sorted and shuffled data — the compiler removed the bet. (When it *doesn't* do this — sometimes a `jcc` survives — that's when you see the §13.3 mispredict gap, and when writing the mask/`cmov` explicitly pays.)

**A branch the compiler keeps.** If the "then" arm has side effects or is large, or the branch looks predictable/biased, the compiler emits a real conditional jump:

```asm
        cmp     edi, esi
        jle     .Lskip          ; a real branch — predicted by hardware, flushed if wrong
        ...                     ; (the then-arm)
.Lskip:
```

The verification habit: **for any hot-path select on data you believe is unpredictable, disassemble and confirm you see `cmov`/`maxss`/masking, not a `jcc`** — and conversely, if a branch is highly predictable, confirm the compiler *kept* the branch rather than forcing a `cmov` that lengthens the dependency chain (§13.6). When the compiler's choice disagrees with your knowledge of the data distribution, that's your signal to nudge it (hints, explicit branchless idioms, or restructuring) and re-measure (§13.3) rather than trust intuition.

---

## 13.6 Pitfalls & anti-patterns: branchless code that's slower; data-dependent stalls

- **Going branchless on a *predictable* branch.** The headline trap: `cmov`/masking a branch that predicts ~99% replaces a free (correctly-predicted) branch with an *always-paid* data dependency (Ch. 11), lengthening the critical path and running *slower*. Branchless wins **only** for genuinely unpredictable branches. Measure the mispredict rate (§13.3) before converting.
- **`cmov` lengthening a dependency chain.** Even on an unpredictable branch, a `cmov` whose inputs sit on the loop's critical path (Ch. 11) can serialize iterations. Sometimes a predictable branch *plus* the speculation it enables beats branchless; the only arbiter is measurement. Watch IPC, not just `branch-misses`.
- **Assuming the compiler emitted what you wrote.** `(a>b)?a:b` may be a branch *or* a `cmov` depending on version, flags, and surrounding code; a `switch` may be a jump table, a branch chain, *or* a `cmov` cascade. Never assume — **read the asm** (§13.5).
- **Mis-using `[[likely]]`/`[[unlikely]]` as a predictor hint.** They are *layout* hints (Ch. 12) and bias the compiler's branch-vs-`cmov` choice; they do **not** improve the hardware predictor on a 50/50 branch, and mis-hinting a balanced branch hurts both layout and codegen. Use only where bias is real.
- **Lookup table that misses cache.** Replacing a branch chain with a *large* or sparse table trades a mispredict for an L2/LLC miss (Ch. 7) — often a worse trade. Keep tables small (L1d-resident) and measure; a 256-byte table is great, a 1 MB one may not be.
- **Branch on cache-missing data.** A branch whose condition depends on a just-loaded, cache-missing value is the worst case (§13.2.2): it can't resolve for hundreds of cycles while the core speculates deep down a possibly-wrong path. Prefetch (Ch. 10) or restructure so the condition's data is resident before the branch.
- **Optimizing branches when you're not Bad-Speculation-bound.** If top-down (Ch. 2) says Memory-Bound (Ch. 7) or Core-Bound (Ch. 11), removing branches does nothing. Confirm elevated `branch-misses` / Bad-Speculation *first*.
- **Predictor aliasing from code bloat.** Thousands of branches in over-inlined hot code (Ch. 12) can alias in the history tables and degrade otherwise-predictable branches. Lean hot code (Ch. 12) helps the predictor — the two chapters reinforce each other.
- **Forgetting the cold predictor (Ch. 46).** A rarely-run but latency-critical branch may be *cold* when it finally matters, mispredicting on the message you most need fast. Branchless code (no prediction needed) can be a deliberate choice for cold-but-critical paths even when it'd lose in steady state.

---

## 13.7 Exercises & checklist

**Exercises**

1. **Measure the mispredict cost.** Build `branch.cpp`, run `sorted` vs `shuffled` pinned/ turbo-off with `perf stat -e cycles,instructions,branches,branch-misses, L1-dcache-load-misses`. Confirm: same instructions and memory, but ~50% `branch-misses` and ~4× slowdown on shuffled. What's the IPC of each? Which top-down bucket (Ch. 2) is shuffled?
2. **Did the compiler go branchless?** Disassemble the §13.3 loop at `-O1`, `-O2`, `-O3` (Ch. 4). At which level does the `jcc` become a `cmov`/masked add? When it does, re-run §13.3 — does the sorted/shuffled gap disappear? Explain why.
3. **`cmov` vs branch on predictable data.** Take a *highly predictable* branch (e.g. 99% one-way) and force a branchless version (explicit `cmov`/mask). Measure both. Is branchless *slower*? Tie the result to the control-vs-data-dependency trade-off (§13.4.1, §13.6).
4. **Build a dispatch table.** Replace an `if/else` (or `switch`) message-type classifier with a `constexpr` lookup table indexed by the type byte. Measure on a mixed (unpredictable) message stream. Compare against the branch chain *and* against sorting the stream by type first (§13.4.3).
5. **Branch on cache-missing data.** Construct a loop whose branch condition depends on a pointer-chased, cache-missing load. Measure. Then prefetch the condition's data (Ch. 10) or make it resident, and re-measure. Explain via §13.2.2.

**Checklist — branches & speculation**

- [ ] I confirmed (top-down, Ch. 2) the hot path is **Bad-Speculation-bound** — elevated `branch-misses`, **clean memory** — before removing branches.
- [ ] I identified **which** branch is unpredictable (high entropy given history), not just that branches exist; predictable branches I **left alone**.
- [ ] For each unpredictable hot branch I chose deliberately: make it **predictable** (sort/partition/hoist), **remove** it (`cmov`/mask/table), or **move it off** the hot path (pre-classify/batch).
- [ ] I went branchless **only** where the branch is genuinely unpredictable — and checked I didn't lengthen the **critical path** (Ch. 11) into a regression.
- [ ] I **verified the asm** (Ch. 4): `cmov`/masking where I want branchless, a kept branch where prediction is cheap; the compiler's choice matches my data distribution.
- [ ] Lookup tables are **small and L1d-resident**; I didn't trade a mispredict for a worse cache miss.
- [ ] I used `[[likely]]`/`[[unlikely]]` **only** where the bias is real, and measured the effect — not as a fix for a 50/50 branch.
- [ ] I measured **IPC and the distribution** (Ch. 1, 3) before/after, not just the `branch-misses` count.

---

## 13.8 References

- A. Fog, *The Microarchitecture of Intel, AMD and VIA CPUs* — per-microarchitecture branch predictor structure, misprediction penalties, and `cmov` latency/throughput; the numbers behind §13.2 and §13.4.1.
- Intel, *64 and IA-32 Architectures Optimization Reference Manual* — branch prediction, the Bad-Speculation top-down category, and `cmov`/branch codegen guidance.
- A. Seznec, *"TAGE" predictor papers* and D. Jiménez, *"Perceptron-based branch prediction"* — how modern history-based predictors actually learn patterns (the basis of §13.2.1).
- A. Yasin, *"A Top-Down Method for Performance Analysis"* (Ch. 2) — locating the Bad-Speculation quadrant before applying these techniques.
- The classic Stack Overflow question *"Why is processing a sorted array faster than an unsorted array?"* — the §13.3 experiment and the canonical intuition for data-dependent mispredicts.

## 13.9 Additional Reading

- D. Bakhvalov, *Performance Analysis and Tuning on Modern CPUs* — practical Bad-Speculation analysis and branchless transformation case studies with `perf`.
- H. Warren, *Hacker's Delight* — the bit-twiddling foundation for branchless min/max/abs/sign/ select and masking idioms (developed in Ch. 28).
- Ch. 14 (*Indirect Calls & Virtual Dispatch*) — *target* prediction (BTB) for indirect branches, the harder cousin of this chapter's *direction* prediction; Ch. 28 (*Bit Manipulation*) — the branchless toolkit; Ch. 29 (*SIMD*) — predication/masking taken to the vector level; Ch. 46 (*Keeping the Hot Path Warm*) — warming the predictor for cold-but-critical branches.
- **Appendix E** — the branch-mispredict penalty in cycles/ns that frames "Bad Speculation."

---

*Next: Ch. 14 — Indirect Calls, Virtual Dispatch & Devirtualization, where the prediction problem gets harder: not just *which way* a branch goes but *to which address* an indirect call jumps — the cost of `virtual`, function pointers and `std::function`, BTB target mispredicts, and how `final`, CRTP and type-erasure alternatives remove indirect calls from the hot path.*
