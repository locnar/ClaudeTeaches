# Part II — CPU Microarchitecture

# Chapter 11 — CPU Pipelines & Execution

> **Prerequisites:** Ch. 2 (top-down analysis — this chapter is the **Back-End → Core Bound** branch, the complement to the Memory-Bound branch of Ch. 7–10), Ch. 4 (reading asm — used heavily in §11.5), Ch. 3 (benchmarking). Ch. 10's prefetch works by overlapping misses with execution — this chapter is what that execution *is*.
>
> **Leads into:** Ch. 12 (the front-end that feeds this back-end), Ch. 13–14 (control hazards — branches and indirect calls — in depth), Ch. 28–29 (the instruction-level and SIMD throughput these ideas unlock). FP reassociation here ties to Ch. 27 (`-ffast-math` hazards).

---

## 11.1 Why it matters: the core is doing more than one thing at once

So far Part II has treated the CPU as something that *waits* — for caches (Ch. 7–10). This chapter is about what it does when the data is *there*: execute. And the surprising truth, if your mental model is "one instruction at a time," is that **a modern core executes many instructions simultaneously, out of program order, and the order you wrote them in is mostly a fiction the hardware reconstructs.** An Ice Lake core can *issue* up to ~5 µops per cycle, has ~10 execution ports, keeps ~350 instructions in flight in its reorder buffer, and will happily run instruction #50 before instruction #20 if #20 is stalled and #50's inputs are ready.

This changes how you reason about hot-path cost. Instruction *count* is a weak predictor of time (Ch. 4 warned you), because the core overlaps independent instructions for free. What actually bounds a hot loop, once its data is cache-resident, is one of two things:

- **Throughput limits** — you've saturated some execution resource (a port, the issue width, load/store units). The core is doing useful work as fast as it physically can.
- **Latency limits (dependency chains)** — each instruction must wait for the previous one's *result*, so the core can't overlap anything, and the loop crawls at the speed of one serial chain *regardless of how many idle execution units sit waiting.*

The second case is the quiet killer, and the central lesson of this chapter: **a loop can be running at 5% of the core's capacity not because the work is hard, but because you accidentally wrote it as one long dependency chain.** The fix isn't a faster algorithm or better data layout — it's *restructuring the computation to expose independent work the out-of-order engine can run in parallel.* The same `N` additions, reorganized from one serial chain into four independent ones, can run ~4× faster on identical hardware with identical data (§11.3, §11.5). That is free performance sitting on the table of most numeric hot paths — risk aggregation, P&L sums, signal computations, the order-book math (Ch. 25, 27).

Knowing where this matters is the top-down discipline (Ch. 2): this chapter is the **Core-Bound** quadrant — high execution-unit activity, *not* stalled on memory or mispredicts. If you're memory-bound, go back to Ch. 7–10; if you're core-bound on a dependency chain, read on.

---

## 11.2 Mental model

### 11.2.1 Superscalar, out-of-order execution

A modern x86 core is a little dataflow machine wearing a sequential-ISA costume. The pipeline, simplified:

```
  front-end                          back-end (out-of-order)
  ┌─────────┐  ┌──────┐  ┌────────┐  ┌─────────────┐  ┌──────────────────┐  ┌────────┐
  │  fetch  │─►│decode│─►│ rename │─►│  scheduler  │─►│ execution ports   │─►│ retire │
  │ (L1i,   │  │ →µops│  │(remove │  │ (reservation │  │ (ALU/LD/ST/FP/   │  │(commit │
  │  Ch.11) │  │      │  │ false  │  │  stations,   │  │  vector/branch)  │  │ in     │
  │         │  │      │  │ deps)  │  │  reorder buf)│  │  ~10 ports        │  │ order) │
  └─────────┘  └──────┘  └────────┘  └─────────────┘  └──────────────────┘  └────────┘
```

The pieces that matter for performance:

- **Superscalar:** multiple execution units (ports), so several µops execute *per cycle*. Different ports do different things (some do integer ALU, some loads, some stores, some FP/ vector); "port pressure" means too many µops competing for the same port.
- **Out-of-order:** the scheduler issues any µop whose inputs are ready, regardless of program order. A stalled instruction doesn't block independent younger ones — the core *finds* parallelism you didn't explicitly write.
- **Register renaming:** the 16 architectural registers are mapped onto a much larger physical register file, eliminating *false* dependencies (two instructions reusing `rax` for unrelated values don't actually serialize). This is why *true* (data) dependencies are the real constraint — the hardware removes the fake ones for you.
- **In-order retirement:** results commit in program order (for precise exceptions and a consistent architectural state), but that's bookkeeping behind the scenes; the *execution* already happened out of order.
- **Speculation:** the core runs *past* unresolved branches by predicting them (Ch. 13), executing speculatively, and squashing if wrong — which is why a mispredict is so expensive (wasted in-flight work).

The upshot: the core is *hunting* for independent instructions to run in parallel. Your job is to give it some.

### 11.2.2 Structural / data / control hazards

A *hazard* is anything that prevents the next instruction from executing in the next cycle. The three classic kinds, in low-latency terms:

- **Structural hazard:** the execution resource you need is busy. Two µops both want the one divide unit, or three loads want two load ports — one waits. This is **throughput/port pressure**; the cure is fewer ops of the contended kind or spreading work across ports (often what SIMD and good instruction selection buy you).
- **Data hazard (true dependency):** an instruction needs the *result* of an earlier one. This is the **latency** constraint and the subject of §11.2.3. Renaming kills the false flavors (write-after-read, write-after-write); the *true* read-after-write dependency is fundamental — the result simply isn't ready yet.
- **Control hazard:** a branch whose direction isn't known yet. The core predicts and speculates (Ch. 13); a correct prediction is nearly free, a mispredict throws away ~15–20 cycles of speculative work. Indirect branches (virtual calls — Ch. 14) are a harder prediction problem.

For cache-resident, branch-predictable numeric code — the Core-Bound case — **data hazards (dependency chains) and structural hazards (port pressure) are what's left**, and they're what §11.3–§11.4 attack.

### 11.2.3 Dependency chains and critical paths

This is the concept to take from the chapter. A **dependency chain** is a sequence of instructions where each needs the previous one's result; its length × the per-link latency is the **critical path** — a hard floor on execution time that *no amount of superscalar width can beat*, because there's never more than one ready instruction in the chain at a time.

The canonical example — a floating-point reduction:

```cpp
double s = 0.0;
for (i) s += a[i];     // s depends on s depends on s ... ONE serial chain
```

Every `s += a[i]` reads the `s` produced by the previous iteration. Each FP add has a latency of ~4 cycles (the result isn't available for 4 cycles after the add starts), so this loop is pinned at **~4 cycles per element** — even though the core could *start* a new FP add every cycle (its add *throughput* is ~2/cycle). You're using a fraction of the FP capacity:

```
serial chain (latency-bound):   add ──4cy──► add ──4cy──► add ──4cy──►   ~4 cy / element
   (the core's other add units sit idle, no independent work to feed them)

split into 4 chains (throughput-bound):
   add0 ─► add0 ─►        4 independent chains interleave; the core issues
   add1 ─► add1 ─►        ~2 adds/cycle across them ──►  ~1 cy / element (≈4x)
   add2 ─► add2 ─►
   add3 ─► add3 ─►
```

The critical insight: **the loop's speed is set by the longest dependency chain, not the instruction count.** Break the one long chain into several short independent ones and the out-of-order engine overlaps them, converting a latency-bound loop into a throughput-bound one. That single transformation — *accumulator splitting* (§11.4.2) — is the highest-leverage move in numeric hot-path code, and §11.5 shows it in verified asm. (It also explains why a prefetch, Ch. 10, needs independent work to overlap with: the dependency structure is what gives the core something to do during a miss.)

---

## 11.3 Measure it: IPC and port pressure for a dependent chain

The signature of a dependency-chain bottleneck is **low IPC with low cache-miss and low-mispredict counts** — the core is neither stalled on memory (Ch. 7) nor on branches (Ch. 13), it's just serialized. Measure it on the FP reduction, single vs split accumulator, both summing the same cache-resident array (so memory is *not* the variable — this is pure back-end execution):

```cpp
// dep_chain.cpp — same sum, one serial chain vs four independent chains.
// Build: g++ -O2 -std=c++20 -march=native dep_chain.cpp -o dep_chain
//   (NOTE: -O2 WITHOUT -ffast-math, so the compiler must preserve FP order — §10.5.)
// Run pinned, turbo off: taskset -c 2 ./dep_chain 1   |   ./dep_chain 4
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <vector>
#include <chrono>

int main(int argc, char** argv) {
    int chains = (argc > 1) ? std::atoi(argv[1]) : 1;
    constexpr std::size_t N = 4096;                 // fits in L1 — memory is NOT the issue
    constexpr int REPS = 2'000'000;
    std::vector<double> a(N, 1.0000001);

    auto t0 = std::chrono::steady_clock::now();
    double result = 0.0;
    if (chains == 1) {
        double s = 0.0;
        for (int r = 0; r < REPS; ++r)
            for (std::size_t i = 0; i < N; ++i) s += a[i];      // ONE serial chain
        result = s;
    } else {
        double s0=0,s1=0,s2=0,s3=0;
        for (int r = 0; r < REPS; ++r)
            for (std::size_t i = 0; i + 4 <= N; i += 4) {       // FOUR independent chains
                s0+=a[i]; s1+=a[i+1]; s2+=a[i+2]; s3+=a[i+3];
            }
        result = s0+s1+s2+s3;
    }
    auto t1 = std::chrono::steady_clock::now();
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
    std::printf("chains=%d result=%.2f  %.3f ns/elem\n", chains, result,
                ns / ((double)REPS * N));
    return 0;
}
```

Profile each with `perf stat -e cycles,instructions,fp_arith_inst_retired.scalar_double, L1-dcache-load-misses,branch-misses`. Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP, FP add ~4-cycle latency, ~2/cycle throughput), `-O2 -march=native` (no fast-math), pinned, turbo off (illustrative; the *ratio* is the point):

```
                              chains=1 (serial)      chains=4 (split)
ns / element                     ~1.4 ns                ~0.36 ns       <- ~4x faster
IPC                              low (~1.0)             higher (~3+)
L1-dcache-load-misses            ~0 (L1-resident)       ~0              <- memory NOT the cause
branch-misses                    ~0                     ~0             <- branches NOT the cause
fp adds retired                  same                   same           <- identical work
```

Read it the Ch. 2 way: **same instructions, same (near-zero) misses, same (near-zero) mispredicts — and 4× the speed.** The only thing that changed is the *dependency structure*. `chains=1` is latency-bound at one FP-add-latency per element (~4 cyc ≈ 1.4 ns); `chains=4` keeps four adds in flight, saturating FP-add throughput (~2/cyc), and runs ~4× faster. The PMU fingerprint of a dependency-chain bottleneck is exactly this: **low IPC, clean memory, clean branches** — top-down would call it Back-End → Core-Bound. When you see that signature, look for a serial chain to break.

---

## 11.4 Techniques

### 11.4.1 Breaking dependency chains for ILP

The general technique behind the specific trick: **find the longest dependency chain on the hot path and shorten it by exposing independent work.** Ways to do it:

- **Multiple accumulators** (§11.4.2) — the workhorse for reductions (sum, dot product, min/ max, hash combine, checksum): use several independent accumulators and combine at the end.
- **Reassociate the computation** so independent sub-expressions can run in parallel. `((a+b) +c)+d` is a chain of 3; `(a+b)+(c+d)` is depth 2 with two independent adds — the tree form halves the critical path. (For FP this needs `-ffast-math` or manual rewriting — §11.5, Ch. 27.)
- **Interleave independent work** — if two computations are independent, software-interleave their instructions so the core has something to chew on while each waits. (Batch processing several order-book updates / several chains at once, c.f. Ch. 10's batched prefetch.)
- **Lift loop-carried dependencies out** — a value recomputed each iteration from the last (running index, pointer, accumulator) is a chain; where possible compute it from the loop counter (`base + i*stride`) so iterations are independent.
- **Reduce per-link latency** — pick lower-latency instructions where the result feeds the next op (e.g. avoid a `div` in a chain — ~20–40 cyc each — by reciprocal-multiply; Ch. 28).

The mental procedure: ask *"what is the longest chain of 'this needs that's result' on my hot path?"* — that's your critical path — then either shorten it or split it into independent chains the out-of-order engine can overlap.

### 11.4.2 Unrolling and accumulator splitting

The two are related but **not the same**, and conflating them is a common mistake:

- **Unrolling** replicates the loop body to amortize loop overhead (the counter increment, the compare, the branch) and expose more instructions to the scheduler. Compilers do this automatically (`-O2`/`-O3`, or `#pragma unroll`).
- **Accumulator splitting** changes the *dependency structure* by using independent accumulators. This is the part that breaks the critical path.

The crucial subtlety, proven in §11.5: **unrolling alone does NOT break a dependency chain.** If the compiler unrolls `s += a[i]` ×8 but feeds all eight adds into the *same* `s`, it's still one serial chain — eight adds deep — and runs at the same latency-bound speed. You need *separate accumulators* (`s0..s3`) to get independent chains. The split version:

```cpp
double s0=0,s1=0,s2=0,s3=0;          // 4 independent accumulators
for (i; i+4<=n; i+=4) {
    s0 += a[i];   s1 += a[i+1];      // 4 independent chains; the core runs them in parallel
    s2 += a[i+2]; s3 += a[i+3];
}
double s = (s0+s1) + (s2+s3);        // combine at the end (tree-reduce the partials)
// + a scalar remainder loop for n % 4
```

How many accumulators? Roughly **latency ÷ throughput** of the chained op — for FP add on modern Intel, ~4-cycle latency ÷ ~2/cycle throughput ⇒ **~8 accumulators** to fully saturate (4 is a good start and captures most of the win). More than that hits diminishing returns and register pressure (spills — Ch. 4, 9). For **integer** add (1-cycle latency) the chain is rarely the bottleneck, so splitting matters most for *higher-latency* ops: FP add/mul, and anything with multi-cycle latency in the loop-carried path.

This is also exactly what auto-vectorization does *for* you when allowed (§11.5): packing 4 doubles into a `ymm` accumulator *is* four independent lanes. So accumulator splitting and SIMD (Ch. 29) are two faces of the same idea — and SoA layout (Ch. 8) is what makes both possible.

---

## 11.5 Verify the codegen: latency vs throughput of the hot loop

The asm makes the latency-vs-throughput distinction undeniable. All three snippets below are **verified** Clang output (`--target=x86_64-linux-gnu -O2 -march=x86-64-v3`).

**Single accumulator, `-O2` (no `-ffast-math`).** The compiler *does* unroll the loop ×8 — but every add targets the same `xmm0`, so it's one serial chain eight links deep:

```asm
.loop:                                       ; sum1: s += a[i], unrolled x8
        vaddsd  xmm0, xmm0, qword ptr [rdi + 8*rcx]        ; s += a[i]
        vaddsd  xmm0, xmm0, qword ptr [rdi + 8*rcx + 8]    ; s += a[i+1]  (waits for prev)
        vaddsd  xmm0, xmm0, qword ptr [rdi + 8*rcx + 16]   ; ... all into xmm0:
        vaddsd  xmm0, xmm0, qword ptr [rdi + 8*rcx + 24]   ;     a single serial chain
        vaddsd  xmm0, xmm0, qword ptr [rdi + 8*rcx + 32]
        vaddsd  xmm0, xmm0, qword ptr [rdi + 8*rcx + 40]
        vaddsd  xmm0, xmm0, qword ptr [rdi + 8*rcx + 48]
        vaddsd  xmm0, xmm0, qword ptr [rdi + 8*rcx + 56]
        add     rcx, 8
        cmp     rsi, rcx
        jne     .loop
```

`vaddsd` is *scalar* double-add. Eight per iteration, all chained through `xmm0` — **unrolled but still latency-bound.** This is the proof that unrolling ≠ breaking the chain (§11.4.2). The compiler *can't* reassociate, because IEEE FP addition isn't associative and `-O2` must preserve your program's exact result (Ch. 27).

**Four accumulators, `-O2`.** By writing independent accumulators, *you* gave the compiler license to treat the lanes as independent — and it vectorizes the chains into packed adds:

```asm
        vaddpd  ymm0, ymm0, ymmword ptr [rdi + 8*rax]    ; sum4: 4 doubles added at once,
        ...                                              ;       4 independent lanes -> ILP
        vextractf128 xmm0, ymm0, 1                       ; horizontal-combine the partials
        vaddsd  xmm1, xmm0, xmm1                          ;       at the very end
```

`vaddpd ymm` — *packed* double, four independent additions per instruction — **throughput-bound**, the ~4× win measured in §11.3.

**Single accumulator, `-O2 -ffast-math`.** Allow reassociation and the compiler does the splitting *itself*, vectorizing into multiple independent `ymm` accumulators (verified — `ymm0`, `ymm1`, `ymm2`, …):

```asm
        vaddpd  ymm0, ymm0, ymmword ptr [rdi + 8*rcx]
        vaddpd  ymm1, ymm1, ymmword ptr [rdi + 8*rcx + 32]   ; multiple independent
        vaddpd  ymm2, ymm2, ymmword ptr [rdi + 8*rcx + 64]   ; vector accumulators
```

The lesson in three listings: **the dependency structure, not the instruction count, sets the speed.** Without permission to reassociate, the compiler faithfully preserves your serial chain (listing 1); give it independent accumulators (listing 2) or permission to make its own (listing 3 — with `-ffast-math`'s correctness caveats, Ch. 27) and the same arithmetic runs 4–8× faster. When you intend a reduction to be fast, **read the asm and confirm you see `vaddpd`/multiple accumulators, not a single-register `vaddsd` chain.**

---

## 11.6 Pitfalls & anti-patterns: serializing on a single accumulator

- **The single serial accumulator.** The headline anti-pattern: a reduction (`sum`, dot product, hash, checksum, min/max) written as one accumulator, latency-bound at one op-latency per element while the core's execution units idle (§11.3, §11.5). Fix: split accumulators. Signature: low IPC, clean memory, clean branches.
- **Believing unrolling fixed it.** Unrolling the loop (or trusting `-O3` to) without separate accumulators leaves the chain intact — same speed, more code (§11.4.2, §11.5 listing 1). Verify in the asm that the adds target *different* registers, not one.
- **Expecting the compiler to reassociate FP.** Without `-ffast-math`/`-fassociative-math`, the compiler **must** preserve your FP evaluation order and *cannot* break the chain or vectorize the reduction (§11.5). Either split manually (safe, exact) or opt into reassociation *knowing* it changes results and can introduce FP hazards (Ch. 27). Don't assume `-O3` did it.
- **Over-splitting / register pressure.** Too many accumulators spill to the stack (Ch. 4, 9), reintroducing memory traffic and latency. Match the count to latency÷throughput (~8 for FP add); more is waste.
- **Optimizing a chain that isn't the bottleneck.** If you're memory-bound (Ch. 7) or mispredict-bound (Ch. 13), breaking dependency chains does nothing — top-down first (Ch. 2). Accumulator splitting is the cure for the *Core-Bound, latency* signature specifically.
- **Counting instructions instead of finding the critical path.** "Fewer instructions" can be *slower* if they form a longer chain; "more instructions" (SIMD, split accumulators) can be faster (Ch. 4's warning, made precise). Reason about the dependency graph, then measure.
- **Ignoring loop-carried dependencies you didn't notice.** A running pointer, a `prev` reference, a conditional that feeds the next iteration — hidden chains. Recompute from the index where possible to make iterations independent (§11.4.1).
- **`div`/`sqrt` in a dependency chain.** A 20–40-cycle `div` on the critical path dominates everything; hoist it, replace with reciprocal-multiply, or restructure so it's off the chain (Ch. 27–28).

---

## 11.7 Exercises & checklist

**Exercises**

1. **Measure the 4×.** Build `dep_chain.cpp`, run `chains=1` vs `chains=4` pinned/turbo-off, and `perf stat -e cycles,instructions,L1-dcache-load-misses,branch-misses`. Confirm: same instructions, ~0 misses, ~0 mispredicts, ~4× speed. What's the IPC of each? Which top-down bucket (Ch. 2) is `chains=1`?
2. **Sweep the accumulator count.** Generalize the split to 1, 2, 4, 8, 16 accumulators. Plot ns/element. Where do diminishing returns start, and where does register-spill regression begin (read the asm, Ch. 4)? Relate the knee to FP add latency÷throughput (§11.4.2).
3. **Unroll ≠ split.** Force `#pragma unroll 8` on the *single*-accumulator loop. Does it speed up? Read the asm — are the eight adds chained through one register? Now split the accumulators. Which change mattered (§11.5)?
4. **The `-ffast-math` lever.** Compile the single-accumulator sum at `-O2` and `-O2 -ffast-math`; diff the asm (Ch. 4). Confirm `-ffast-math` vectorizes it into multiple `ymm` accumulators. Then check: does the numeric *result* differ? (Preview of Ch. 27's hazard.)
5. **Find a hidden chain.** Take a loop with a loop-carried scalar (e.g. a running min with an index, or `x = f(x)`). Identify the critical path, then restructure to expose independent work. Measure before/after and explain via §11.2.3.

**Checklist — execution & dependency chains**

- [ ] I confirmed (top-down, Ch. 2) the loop is **Core-Bound** — low IPC, **clean memory and branches** — before attacking dependency chains.
- [ ] I identified the **longest loop-carried dependency chain** (the critical path) — not the instruction count.
- [ ] Reductions use **multiple independent accumulators** (~latency÷throughput of the chained op), combined at the end — not a single serial accumulator.
- [ ] I verified in the **asm** (Ch. 4) that adds target **different registers** / `vaddpd`-style packed ops — not one chained `vaddsd`/register.
- [ ] If I rely on the compiler to reassociate/vectorize FP, I **opted into `-ffast-math` knowingly** (Ch. 27) — or split manually for exact results.
- [ ] I didn't **over-split** into register spills, and I kept high-latency ops (`div`/`sqrt`) **off** the critical path (Ch. 27–28).
- [ ] I measured the **distribution/IPC** before and after, on a cache-resident working set so memory isn't a confound.

---

## 11.8 References

- A. Fog, *The Microarchitecture of Intel, AMD and VIA CPUs* and *Instruction Tables* — per-microarchitecture pipeline width, port assignments, and the **latency/throughput** numbers that drive accumulator-count decisions (the essential reference for this chapter).
- Intel, *64 and IA-32 Architectures Optimization Reference Manual* — the out-of-order engine, ports, reorder buffer, and reduction/ILP guidance; AMD's *Software Optimization Guide* for AMD cores.
- J. L. Hennessy and D. A. Patterson, *Computer Architecture: A Quantitative Approach* — pipelining, hazards, ILP, and dependency/critical-path analysis from first principles.
- A. Yasin, *"A Top-Down Method for Performance Analysis"* (Ch. 2) — locating the Core-Bound vs Memory-Bound vs Bad-Speculation quadrant before applying these techniques.
- The `llvm-mca` (machine-code analyzer) and Intel IACA/uiCA documentation — static throughput and dependency analysis of a hot loop's asm (a §11.5 power tool).

## 11.9 Additional Reading

- D. Bakhvalov, *Performance Analysis and Tuning on Modern CPUs* — practical chapters on ILP, dependency chains, and using `llvm-mca`/`uiCA` to read a loop's critical path.
- T. Mattson / various CppCon talks on data-parallel reductions and "the surprising speed of splitting your accumulators."
- Ch. 12 (*Instruction Cache & Front-End*) — the front-end that must feed this back-end; Ch. 29 (*SIMD*) — accumulator splitting taken to its vector conclusion; Ch. 27 (*Fixed/Floating- Point*) — the `-ffast-math`/reassociation correctness trade-offs invoked in §11.5.
- **Appendix E** — the cycle costs of FP/integer ops and the branch-mispredict penalty that frame "latency-bound."

---

*Next: Ch. 12 — Instruction Cache, Front-End Stalls & Code Layout, where we turn around and look at the *front* of the pipeline: how the L1i, the µop cache, and decode bandwidth can starve even a perfectly-scheduled back-end, and how code layout, inlining discipline, and PGO/ BOLT keep the instruction stream flowing.*
