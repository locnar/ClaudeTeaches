# Part I — Foundations & Methodology

# Chapter 4 — Reading the Machine: Assembly & Compiler Output

> **Prerequisites:** Ch. 1–3. You reason about the distribution (Ch. 1), you can find a bottleneck with the PMU (Ch. 2), and you can build a trustworthy microbenchmark (Ch. 3). You do **not** need to be an assembly programmer — this chapter teaches you to *read* x86-64, not write it. For the hands-on parts: [Compiler Explorer](https://godbolt.org) (nothing to install) and/or a local GCC/Clang.
>
> **Leads into:** This is the verification skill the rest of the book leans on. Every "verify the codegen" section from here forward (Ch. 9, 13–14, 18–21, 27–29) assumes you can read the asm in this chapter. Build-flag effects on that asm are Ch. 22.

---

## 4.1 Why it matters: trust, then verify the optimizer

Between your C++ and the silicon stands an optimizing compiler that is free to rewrite your code almost beyond recognition, as long as the observable behavior is preserved ("as-if"). It will inline functions, delete code whose result is unused, hoist work out of loops, replace your branch with a conditional move, vectorize your loop into SIMD, unroll it four ways, and turn `x / 8` into a shift. On the hot path, *the asm is the program* — the C++ is just a request.

Most of the time the optimizer does exactly what you'd hope, and you should trust it. But low-latency work lives precisely at the points where "most of the time" isn't good enough, and three recurring questions can only be answered by looking at the generated code:

1. **Did the optimization I'm relying on actually happen?** Did that hot call get inlined? Did the loop vectorize? Did the bounds check get elided? "I assumed it did" is how p99.9 regressions ship. Ch. 3 already gave you one instance of this: a benchmark that "measures nothing" because the work was deleted — and the only way to *know* is to read the loop.
2. **Why is this slower than it should be?** The PMU (Ch. 2) told you a function is front-end bound or branch-mispredicting; the asm tells you *why* — an unexpected branch, a spill to the stack, a function call you thought was inlined, a `div` instruction where you expected a shift.
3. **Which of two source forms produces better code?** When you can't measure a difference (it's in the noise), or before you've even written the benchmark, diffing the asm of two formulations is often a faster, sharper answer than a benchmark — and it *explains* the benchmark when you do run it.

The mindset is **"trust, then verify."** You are not second-guessing the compiler on every line — that way lies madness and worse code. You are spot-checking the handful of hot functions where nanoseconds and determinism matter, confirming the optimizer kept its promises, and learning the *vocabulary* of x86-64 well enough that when the PMU points at a function, you can read what the machine is actually doing. That is the entire goal of this chapter: not assembly fluency, but **assembly literacy** — enough to verify, debug, and choose.

---

## 4.2 Mental model

### 4.2.1 The x86-64 register and instruction model you need

You don't need the whole ISA. You need enough to follow a hot loop. Here is the working subset.

**General-purpose registers** — 16 of them, 64-bit, each with narrower aliases. The same physical register is `rax` (64-bit), `eax` (low 32), `ax` (low 16), `al` (low 8). Writing the 32-bit name (`eax`) zero-extends into the full 64-bit register — a detail that explains a lot of `mov eax, ...` you'll see where you expected 64-bit ops.

| Register(s) | Conventional role (System V AMD64 ABI — Linux) |
|---|---|
| `rdi, rsi, rdx, rcx, r8, r9` | first six integer/pointer **arguments**, in order |
| `rax` | **return value** (and `rdx:rax` for 128-bit / `div` results) |
| `rsp`, `rbp` | stack pointer; frame pointer (base pointer) |
| `rbx, r12–r15` | **callee-saved** (a function must preserve these) |
| `rax, rcx, rdx, rsi, rdi, r8–r11` | **caller-saved** (free to clobber) |

Knowing the ABI register order (`rdi` = arg1, `rsi` = arg2, …, `rax` = return) lets you map a function's asm back to its signature at a glance — essential for reading code in isolation on Godbolt.

**SIMD registers** — `xmm0–xmm15` (128-bit, SSE), widened to `ymm` (256-bit, AVX/AVX2) and `zmm` (512-bit, AVX-512). When you see `ymm` in a loop, the compiler vectorized it (Ch. 29). Their presence or absence is the single fastest way to confirm vectorization.

**Instruction shape.** This book uses **Intel syntax** (`-masm=intel`), which reads `op dest, src` (destination first) — generally cleaner than AT&T's `op src, dest` with its `%`/`$` sigils. The handful of mnemonics that cover most hot loops:

- **Data movement:** `mov` (copy), `lea` (compute an address *or* do `a*scale+b+c` arithmetic without touching flags — you'll see `lea` used for plain math), `movsxd`/ `movzx` (sign-/zero-extend a narrower value into a wider register).
- **Arithmetic/logic:** `add sub imul` (note: `imul` for multiply — real `div` is rare and *expensive*, ~20–40 cycles; seeing it is a red flag), `and or xor shl shr sar`. `xor eax, eax` is the idiomatic "set to zero."
- **Compare & branch:** `cmp`/`test` set flags; `jne jl jge ja jbe …` branch on them. `j*` are the conditional branches whose mispredicts Ch. 13 is about.
- **Branchless conditionals:** `cmov*` (conditional move) — selects a value based on flags *without branching*. The hero of Ch. 13; learn to recognize it.
- **Calls:** `call`/`ret`; an indirect `call rax`/`jmp rax` is a virtual/function-pointer call (Ch. 14) — a different beast for the branch predictor.

That's most of what a hot loop is made of. Everything else you can look up the moment you hit it.

### 4.2.2 How source maps to asm under optimization

The leap from "I know the mnemonics" to "I can read optimized code" is accepting that the compiler does **not** translate line-by-line. At `-O2`/`-O3` it transforms aggressively, and the asm reflects the *transformed* program. The transformations you'll see constantly:

- **Inlining** — the callee's body is spliced into the caller; the `call` disappears. This is the enabling optimization: once inlined, the code can be further simplified in context (constant-folded, dead-code-eliminated). A *missing* inline (a stray `call` in your hot loop) is one of the most common avoidable costs (Ch. 14, 22).
- **Dead-code elimination** — anything whose result is never observed is deleted. This is the Ch. 3 benchmark hazard, seen from the asm side.
- **Constant folding / propagation** — `square(7)` becomes `49` at compile time; computations on known constants vanish.
- **Strength reduction** — expensive ops become cheap ones: `x * 8` → `shl ... 3`, `x / 4` → `sar`, multiply-by-constant → `lea`/shift/add sequences.
- **Loop transforms** — **unrolling** (one source iteration becomes 4 or 8 in the asm, to expose ILP and amortize loop overhead — Ch. 11), **vectorization** (the loop body becomes SIMD over `ymm`/`zmm` — Ch. 29), **hoisting** (loop-invariant work moved out), and bounds-check elision.
- **Branch → conditional move** — a small `if` becomes a branchless `cmov` when the compiler judges the branch unpredictable or the bodies cheap (Ch. 13).
- **Register allocation & spilling** — values live in registers when they can; when there aren't enough, the compiler **spills** to the stack (`mov [rsp+N], reg` / `mov reg, [rsp+N]`). Spills in a hot loop are a smell worth investigating (too many live values, failed inlining, missing `restrict` — Ch. 21).

The practical consequence: when you read optimized asm, don't look for your source lines. Look for the *shape* — is there a tight loop? does it contain a `call`? a `div`? a spill? `ymm` registers? — and compare that shape against what you expected. The gap between "expected shape" and "actual shape" is the bug or the opportunity.

---

## 4.3 Measure it: the Compiler Explorer (Godbolt) workflow

[Compiler Explorer](https://godbolt.org) (Godbolt) is the indispensable tool: paste C++, pick a compiler and flags, and see the asm side-by-side with **color-coded source↔asm mapping** (click a source line, the corresponding asm highlights). It is the fastest loop for the "which source form is better?" question, and it needs nothing installed. The local equivalents, for code that doesn't fit in a paste buffer:

```bash
# Emit Intel-syntax asm for one translation unit (GCC or Clang):
g++   -O2 -std=c++20 -S -masm=intel -o foo.s foo.cpp
clang++ -O2 -std=c++20 -S -masm=intel -o foo.s foo.cpp

# Annotate a *profiled* binary with its hot asm and sample counts (Ch. 2):
perf annotate -s your_hot_function

# Disassemble an existing binary/object (handy for release artifacts):
objdump -d -M intel --no-show-raw-insn ./your_binary | less
```

Two workflow rules that save hours:

1. **Always read optimized asm (`-O2`/`-O3`), never `-O0`.** Debug-build asm is a faithful, verbose, spill-everything transcription of your source that bears no resemblance to what ships — reading it teaches you nothing about performance and actively misleads (§4.5).
2. **Make the function escape, or the compiler deletes it.** A standalone function with external linkage stays; but if you're inspecting a snippet whose result is unused, the optimizer removes it — exactly the Ch. 3 problem. On Godbolt, return the value or mark things `volatile`/`[[gnu::used]]` so there's something to look at.

A warm-up to calibrate your eye. This trivial function:

```cpp
int square(int x) { return x * x; }
```

compiles (Clang, `--target=x86_64-linux-gnu -O2 -masm=intel`) to exactly three instructions — verified output:

```asm
square(int):
        mov     eax, edi          ; eax = x        (edi = arg1 per the ABI)
        imul    eax, edi          ; eax = x * x
        ret                       ; return eax
```

Read it against §4.2.1: arg1 arrives in `edi`, the result leaves in `eax`, `imul` is the multiply, done. No stack frame, no spill, no call. If *your* one-line function produced twenty instructions and a `call`, you'd know something prevented the inline/simplification — and you'd go find out. That reflex is the whole skill.

---

## 4.4 Techniques

### 4.4.1 Reading the hot loop in asm

The payoff is reading loops, because that's where hot-path time lives. Take the sum from Ch. 2's theme:

```cpp
long sum(const int* a, unsigned long n) {
    long s = 0;
    for (unsigned long i = 0; i < n; ++i) s += a[i];
    return s;
}
```

With vectorization disabled (`-O2 -fno-vectorize -fno-unroll-loops`, to see the bare loop), Clang emits this — verified output, lightly annotated:

```asm
sum(int const*, unsigned long):
        test    rsi, rsi              ; n == 0 ?         (rsi = arg2 = n)
        je      .done                 ;   if so, skip the loop
        xor     ecx, ecx              ; i = 0
        xor     eax, eax              ; s = 0           (eax/rax = return value)
.loop:
        movsxd  rdx, dword ptr [rdi + 4*rcx]  ; load a[i], sign-extend 32->64 bit
        add     rax, rdx              ; s += a[i]
        inc     rcx                   ; ++i
        cmp     rsi, rcx              ; i == n ?
        jne     .loop                 ;   if not, loop
.done:
        ret
```

Everything you need to read a loop is here. The **loop body is the block between the label (`.loop`) and the conditional jump back to it**. Count what's in it: one load (`movsxd` — and note it *sign-extends*, because `int` is 32-bit but `s` is 64-bit, a detail with real codegen consequences), one `add`, the index increment, the compare, the branch. Five instructions per element, a single dependent `add` chain. The `[rdi + 4*rcx]` is x86's scaled-index addressing: base pointer + index×4 (the `int` size), computing `&a[i]` for free inside the load. This is what a clean, scalar, non-vectorized reduction looks like — your reference point for spotting when something is *wrong* (an unexpected `call` in the body, a `div`, a spill `mov [rsp+...]`).

The discipline when reading any hot loop:
- **Isolate the loop body** (label → back-edge branch) and **count the instructions** — fewer is usually (not always) better; compare to your mental model.
- **Find the dependency chain** — `s += a[i]` is a single serial chain through `rax`, which caps throughput at one add per latency (Ch. 11 shows how unrolling into multiple accumulators breaks this).
- **Look for the smells**: `call` (inlining failed), `div`/`idiv` (expensive — Ch. 28), `mov [rsp+…]` round-trips (spilling), or memory operands where you expected registers.

### 4.4.2 Confirming inlining, vectorization, and elision

The three checks you'll run most:

**Inlining.** Search the caller's asm for a `call` to the callee. *Present* → not inlined (a function-call overhead and an optimization barrier on your hot path). *Absent* → inlined. For diagnosing *why* an inline didn't happen, GCC/Clang's `-Winline` and the optimization remarks (`-Rpass=inline`, `-Rpass-missed=inline` on Clang; `-fopt-info-inline` on GCC) report the compiler's reasoning. Fixes — `inline`/`[[gnu::always_inline]]`, LTO, tuning `-finline-limit` — are Ch. 22.

**Vectorization.** Look for SIMD registers (`xmm`/`ymm`/`zmm`) and packed ops (`vpaddq`, `vaddps`, …) in the loop body. The same `sum`, compiled with `-O2 -march=x86-64-v3` (AVX2), vectorizes — verified excerpt of the inner block:

```asm
        vpmovsxdq  ymm4, xmmword ptr [rdi + 4*rax]       ; load 4 ints, sign-extend to 4x64
        vpaddq     ymm0, ymm0, ymm4                      ; add into 4-wide accumulator
        vpmovsxdq  ymm4, xmmword ptr [rdi + 4*rax + 16]  ; ... and 3 more,
        vpaddq     ymm1, ymm1, ymm4                      ;     unrolled across 4 separate
        vpmovsxdq  ymm4, xmmword ptr [rdi + 4*rax + 32]  ;     ymm accumulators (ymm0..ymm3)
        vpaddq     ymm2, ymm2, ymm4                      ;     to break the dependency chain
        ...                                              ;     (the Ch.10 trick, done for you)
        vextracti128 xmm1, ymm0, 1                       ; horizontal reduce the 4 lanes
        vpaddq     xmm0, xmm0, xmm1                      ;     back down to one sum at the end
```

That single excerpt teaches two chapters at once: the `ymm`/`vpaddq` confirm **vectorization** (Ch. 29 — four 64-bit lanes added at a time), and the *four independent* `ymm0..ymm3` accumulators are the compiler **breaking the dependency chain** (Ch. 11) so the four packed adds can issue in parallel. If you needed this loop vectorized and saw the scalar `add rax, rdx` from §4.4.1 instead, you'd know to investigate (aliasing? `-march`? a loop-carried dependency? — Ch. 21, 22, 29). For the compiler's own report use `-Rpass=loop-vectorize` / `-Rpass-missed=loop-vectorize` (Clang) or `-fopt-info-vec` (GCC).

**Elision (and dead-code).** Confirm that bounds checks, redundant copies, or zero-cost-abstraction wrappers compiled away to nothing — the promise of Ch. 19–20. If a `std::span::operator[]` or a `std::unique_ptr` deref produced *extra* instructions versus the raw pointer, you've found a leak in the abstraction. Conversely, if the work you *wanted* measured vanished, that's the Ch. 3 benchmark bug staring back at you from the asm.

### 4.4.3 Diffing codegen across flags/compilers

Reading asm in isolation tells you what the machine does; **diffing** asm tells you which of two choices is better and why. Godbolt makes both axes trivial — two source panes, or one source with two compiler panes side by side.

Two high-value diffs:

- **Source-form A vs B, same flags.** When two formulations should be equivalent, the asm is the referee. Classic example — a branch vs a branchless conditional:

  ```cpp
  int abs_branchless(int x) { return x < 0 ? -x : x; }
  ```

  Clang `-O2` produces (verified) a **branchless** sequence — no conditional jump, so nothing for the branch predictor to mispredict (Ch. 13):

  ```asm
  abs_branchless(int):
          mov     eax, edi
          neg     eax              ; eax = -x
          cmovs   eax, edi         ; if x was negative (sign flag), keep -x; else x
          ret
  ```

  Seeing `cmovs` instead of a `jl`/`jmp` pair *confirms* the branchless intent survived optimization — the thing Ch. 13 will have you verify constantly. (A `max(a,b)` similarly becomes `cmp`/`cmovg`, no branch.)

- **Flag A vs B / compiler A vs B, same source.** `-O2` vs `-O3`, `-march=x86-64` vs `-march=native`, GCC vs Clang — diff the hot function to see what each does (did `-O3` unroll more? did `-march=native` unlock AVX-512? did one compiler vectorize and the other not?). This is how you make *evidence-based* flag decisions instead of cargo-culting `-O3` (Ch. 22), and how you catch a compiler upgrade silently changing your hot loop — the asm-level complement to Ch. 3's regression gates.

The habit: when a change *should* be free or *should* be faster, prove it in the asm before (or alongside) the benchmark. The asm explains the benchmark, and the benchmark confirms the asm matters at runtime — you want both.

---

## 4.5 Pitfalls & anti-patterns: debug-build asm, reading without `-O2`

- **Reading `-O0`/debug asm.** The cardinal error. Unoptimized code reloads every variable from the stack on every use, inlines nothing, and elides nothing — it is verbose, spill-ridden, and *completely unrepresentative* of the binary you ship. Conclusions drawn from it ("look how many memory accesses!") are fiction. Always read `-O2`/`-O3` with the flags you actually build with (Ch. 22). This mirrors Ch. 3's "profile the build you ship."
- **The deleted snippet.** Inspecting a function whose result is unused, finding "no code," and concluding it's free — when really the optimizer deleted it. Make the result escape (return it, `DoNotOptimize`, `volatile`) before reading or you're reading a ghost.
- **Forgetting inlining changes everything.** A function's standalone asm (with a `call` ABI, callee-saved register shuffling, a stack frame) can look heavier than reality, because at the call site it gets *inlined* and simplified into the surrounding code. Judge hot code **in context** (at the call site, with LTO if you ship LTO), not in isolation — isolation is fine for *learning to read*, not for *final verdicts*.
- **Counting instructions as if they were cycles.** Fewer instructions is a heuristic, not a law. A loop with more instructions but no dependency chain, no cache misses, and no mispredicts can beat a "shorter" one (a `div` is one instruction and ~30 cycles; a vectorized body is many instructions doing 8× the work). Instruction count is a clue; the PMU (Ch. 2) and a benchmark (Ch. 3) are the verdict. **Asm reading explains; it does not, by itself, measure.**
- **Mistaking syntax for substance (AT&T vs Intel).** If you switch tools, operand order flips (AT&T is `src, dest`; Intel is `dest, src`). Pick one syntax (this book uses Intel) and set it explicitly (`-masm=intel`, or the Godbolt toggle) so you don't misread a `mov`.
- **Over-reading.** You do not need to hand-verify every function — that's slower code *and* slower you. Reserve asm reading for the hot path the PMU pointed at, the abstraction you need to prove is zero-cost, and the surprising benchmark. Trust the optimizer elsewhere.

---

## 4.6 Exercises & checklist

**Exercises**

1. **Calibrate your eye.** On Godbolt, compile `int square(int)` at `-O0` and at `-O2`. Diff them. Count the instructions and the stack accesses in each. Why is the `-O0` version so much longer, and why is reading it a waste of time?
2. **Read a loop.** Paste the `sum` function. View it at `-O2 -fno-vectorize` and at `-O2 -march=x86-64-v3`. Identify the loop body in each. How many `int`s are processed per iteration in each version? Find the multiple accumulators in the vectorized one and explain (Ch. 11) why they exist.
3. **Confirm a branchless transform.** Compile `x < 0 ? -x : x` and `a > b ? a : b` at `-O2`. Find the `cmov`. Now write the same logic with an explicit `if`/`else` and bitmask tricks — does the compiler converge to the same asm? (Preview of Ch. 13.)
4. **Catch a failed inline.** Write a small function and call it in a loop; confirm it's inlined (no `call`). Now mark it `[[gnu::noinline]]` and re-read — find the `call`, the argument setup in `rdi`/`rsi`, and the `ret`. Add `-Rpass-missed=inline` and read the compiler's reason on a case where it *won't* inline.
5. **Spot the expensive instruction.** Write `int f(int x){ return x / 7; }` and `int g(int x){ return x / 8; }`. Diff the asm. Which became a cheap shift, which a `mul`/`shr` magic-number division, and is there a real `idiv` anywhere? (Preview of Ch. 28.)

**Checklist — verifying codegen**

- [ ] I read **optimized** asm (`-O2`/`-O3`) with my **production flags**, never `-O0`.
- [ ] I set **Intel syntax** explicitly so operand order isn't ambiguous.
- [ ] The function/result **escapes**, so I'm not reading deleted code.
- [ ] I **isolated the loop body** (label → back-edge) and **counted** its instructions.
- [ ] I checked for the **smells**: stray `call`, `div`/`idiv`, stack **spills**, surprise branches.
- [ ] I **confirmed the optimization I rely on** (inline? `ymm` vectorization? `cmov`? elided bounds check?) and used opt-remarks (`-Rpass*`/`-fopt-info`) when it didn't happen.
- [ ] I judged hot code **in context** (inlined, with LTO if I ship it), not in isolation.
- [ ] I treated asm as **explanation**, and confirmed it **matters** with the PMU (Ch. 2) and a benchmark (Ch. 3) — instruction count is a clue, not a verdict.

---

## 4.7 References

- M. Godbolt, *Compiler Explorer* (godbolt.org) and his CppCon talk *"What Has My Compiler Done for Me Lately? Unbolting the Compiler's Lid"* — the tool and the philosophy of this chapter.
- Intel, *64 and IA-32 Architectures Software Developer's Manual (SDM)*, Vol. 2 — the authoritative x86-64 instruction reference (`mov`, `lea`, `cmov`, `imul`, …).
- *System V Application Binary Interface, AMD64 Architecture Processor Supplement* — the calling convention behind §4.2.1 (argument/return registers, callee-saved set).
- A. Fog, *Optimizing Subroutines in Assembly Language* and *Instruction Tables* — how source maps to asm, and the latency/throughput of the instructions you'll read.
- GCC and Clang manuals — `-S`, `-masm=intel`, optimization remarks (`-fopt-info-*`, `-Rpass*`), and the inlining/vectorization flags referenced here and in Ch. 22.

## 4.8 Additional Reading

- *x86-64 Assembly Language Programming with Ubuntu* (Ed Jorgensen, free) and the *Felix Cloutier x86 instruction reference* (felixcloutier.com/x86) — quick lookups when you hit an unfamiliar mnemonic.
- M. Godbolt, *"Correct by Construction"* and assorted blog posts on reading codegen; the Compiler Explorer documentation on the assembly-mapping and diff views.
- D. Bakhvalov, *Performance Analysis and Tuning on Modern CPUs* — pairs asm reading with the PMU-driven workflow of Ch. 2.
- Ch. 22 (*Build Toolchain for Speed*) — the flags (`-march`, LTO, PGO, inlining controls) that change the asm you just learned to read; and Ch. 29 (*SIMD*) for the vector instructions in depth.
- Ch. 5 (*Debugging Low-Latency & Optimized Code*) — where reading asm becomes a debugging skill: recovering `<optimized out>` state from registers and disassembly in a live/replayed process.

---

*Next: Ch. 5 — Debugging Low-Latency & Optimized Code, the other half of the craft: once you can read the asm, you can debug it — `-O2`/LTO builds where state is optimized away, record-and-replay (`rr`) for non-reproducible bugs, production core-dump analysis, and the observer effect that turns a debugger into a heisenbug.*
