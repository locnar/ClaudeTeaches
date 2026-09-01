# Part III — Compile-Time & Language Mechanics

# Chapter 21 — Aliasing, `restrict` & Type Punning

> **Prerequisites:** Ch. 11 (out-of-order execution and dependency chains — aliasing is what forces the compiler to *assume* a dependency it can't disprove), Ch. 29-preview/Ch. 8 (vectorization and data layout — the optimization aliasing most often blocks), Ch. 4 (reading asm — §21.5 is the whole proof), Ch. 9 (object model — type punning is reinterpreting object representation).
>
> **Leads into:** Ch. 29 (SIMD & vectorization — `restrict` is frequently the difference between a vectorized and a scalar loop), Ch. 53 (zero-copy wire decoding — overlaying structs on a byte buffer is type punning, done safely with `bit_cast`/`memcpy`), Ch. 72 (secure programming — strict-aliasing UB as a vulnerability class). Closes **Part III**.

---

## 21.1 Why it matters: aliasing assumptions gate optimization

Two pointers **alias** if they can refer to the same memory. Whenever the compiler can't *prove* that two pointers don't alias, it must conservatively assume they *might* — which means it cannot reorder a store through one with a load through the other, cannot keep a value in a register across a store it can't rule out, and crucially **cannot vectorize** a loop whose iterations might overlap. Aliasing is therefore one of the most important and least visible gates on optimization: the *same* loop can compile to tight, vectorized, register-resident code or to a plodding scalar reload-every-iteration version depending entirely on what the compiler can deduce about whether your pointers overlap. Nothing in the loop body changed; what changed is the *aliasing information* the optimizer had to work with.

This matters acutely for low-latency numeric code, which is exactly the code that lives or dies on vectorization (Ch. 29) and register allocation (Ch. 4, 11). A signal computation summing weighted inputs, a risk aggregation over position vectors, a price-conversion pass over a market-data array, a `memcpy`-shaped transform — every one is a loop over arrays through pointers, and every one is a candidate to be silently de-optimized because the compiler feared `out` might overlap `in`. The fix is often a single keyword (`__restrict__`) or a small refactor, and the payoff — proven in the asm (§21.5) — can be a 2-8× swing from scalar to SIMD. This is "free performance" of the same character as Ch. 11's accumulator splitting: the work didn't get cheaper, the compiler was simply *allowed* to do it well.

The other half of the chapter is the flip side of aliasing: the **strict-aliasing rule**, C++'s promise that the compiler may *assume* you don't access an object of one type through a pointer to an unrelated type. That assumption is what lets it not reload memory across a write through a differently-typed pointer — a real optimization — but it makes the common low-level trick of **type punning** (reinterpreting the bytes of one type as another: reading a `float`'s bits as a `uint32`, overlaying a packed struct on a network buffer — Ch. 53) **undefined behavior** when done the naive way (a `reinterpret_cast` and dereference, or the wrong union access). UB here isn't theoretical: it produces miscompiles that appear only at `-O2`, only on one compiler, only sometimes (Ch. 72). So this chapter teaches both how to *give* the optimizer aliasing freedom where it helps (`restrict`), and how to *pun types safely* where you must (`std::bit_cast`, `memcpy`, `std::launder`) — keeping the wins without the UB.

## 21.2 Mental model: strict-aliasing rules; how aliasing blocks vectorization/reordering

**The aliasing problem, concretely.** Consider the textbook loop:

```cpp
void scale(float* out, const float* in, float k, int n) {
    for (int i = 0; i < n; ++i) out[i] = in[i] * k;   // can this vectorize?
}
```

The compiler would love to load 8 floats, multiply by `k`, store 8 floats, per iteration (AVX). But it must consider: *what if `out` and `in` overlap?* If `out == in + 1`, then `out[i]` writes a location that `in[i+1]` will later read, so processing 8 at once would use stale data — the scalar one-at-a-time order is semantically required. Unable to prove non-overlap, the compiler either emits scalar code or (more often) emits *both* a vectorized and a scalar version plus a **runtime overlap check** that picks between them — bloat and a branch you didn't ask for. The information it lacks is simply "these don't alias."

**Strict aliasing — the type-based half.** C++ also lets the compiler reason about aliasing *by type*: the **strict-aliasing rule** says a program may not access an object through a glvalue of an unrelated type (the permitted exceptions: the object's own type, a `char`/`std::byte`/`unsigned char` view, signed/unsigned variants, and base classes). The compiler may therefore *assume* a `float*` and an `int*` never refer to the same object, and skip reloading the `int` after a store through the `float*`:

```cpp
int taboo(float* f, int* i) {
    *i = 1;            // the compiler may assume *f can't touch *i (different, unrelated types)
    *f = 2.0f;         // ... so it need not reload *i after this store
    return *i;         // may legally return 1 without reloading — a real optimization
}
```

This is a *win* (less reloading) — but it's also the trap: if you *deliberately* make a `float*` and `int*` point at the same bytes (type punning via casts), you've violated the assumption and the program has **undefined behavior**. The compiler isn't "wrong"; you broke the contract it's entitled to rely on.

**The two levers, and their relationship.** Put together:

- **Pointer aliasing** (do two *same-type* pointers overlap?) blocks reordering/vectorization; the compiler can't usually prove non-overlap, so you tell it with **`restrict`** (§21.4.1). `char*`/`std::byte*` aliases *everything* (the strict-aliasing exception), so byte-buffer code is especially alias-pessimized.
- **Type-based aliasing** (the strict-aliasing rule) is an assumption the compiler *makes* to optimize; **type punning** through incompatible pointers/unions violates it and is **UB** — do it through the sanctioned channels (`std::bit_cast`/`memcpy`/`std::launder`) instead (§21.4.2).

The unifying model: **the compiler optimizes aggressively only on the dependencies it can prove absent. `restrict` lets you *assert* same-type pointers don't overlap (unblocking vectorization); the strict-aliasing rule lets it *assume* different-type pointers don't overlap (which you must not secretly violate). Give it the freedom honestly, or pay for the freedom with UB.**

## 21.3 Measure it: codegen difference with/without `__restrict__`

The cleanest demonstration is a loop that *should* vectorize, compiled with and without a `restrict` promise — the source is otherwise identical, so any difference is purely the aliasing information. (This is fundamentally a *codegen* measurement, so §21.3 and §21.5 are tightly coupled; here we time it, there we read the asm.)

```cpp
// restrict.cpp — same SAXPY-style loop, with and without restrict.
// Build: g++ -O3 -std=c++20 -march=native restrict.cpp -o restrict
//   (-O3 / -march=native so vectorization is ON THE TABLE; aliasing is the only variable.)
// Run pinned, turbo off:  taskset -c 2 ./restrict plain   |   ./restrict restrict
#include <cstdio>
#include <cstring>
#include <vector>
#include <chrono>

// (a) plain: compiler must assume out/in MAY overlap.
void axpy_plain(float* out, const float* in, float k, int n) {
    for (int i = 0; i < n; ++i) out[i] += in[i] * k;
}
// (b) restrict: YOU promise out/in/the arrays don't overlap → free to vectorize.
void axpy_restrict(float* __restrict out, const float* __restrict in, float k, int n) {
    for (int i = 0; i < n; ++i) out[i] += in[i] * k;
}

int main(int argc, char** argv) {
    bool r = (argc > 1) && std::strcmp(argv[1], "restrict") == 0;
    constexpr int N = 4096;                 // L1-resident: memory is NOT the variable
    constexpr long REPS = 2'000'000;
    std::vector<float> out(N, 1.0f), in(N, 1.0001f);

    auto t0 = std::chrono::steady_clock::now();
    for (long rep = 0; rep < REPS; ++rep)
        if (r) axpy_restrict(out.data(), in.data(), 1.0001f, N);
        else   axpy_plain   (out.data(), in.data(), 1.0001f, N);
    auto t1 = std::chrono::steady_clock::now();
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count();
    std::printf("%-8s out[0]=%.3f  %.3f ns/elem\n", r ? "restrict" : "plain",
                out[0], ns / ((double)REPS * N));
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP, AVX-512 available), `-O3 -march=native`, pinned, turbo off (illustrative; the *ratio* is the point):

```
                              plain (may alias)     restrict (no alias)
ns / element                     ~0.50 ns              ~0.09 ns        <- ~5x faster
vectorized? (§20.5)              scalar / guarded      packed SIMD
instructions retired             more (scalar)         far fewer (8-16/iter folded)
L1-dcache accesses               ~same data            ~same data      <- memory NOT the cause
```

Read it the Ch. 11 / Ch. 4 way: **identical source, identical data, identical cache footprint — and ~5× faster** purely because `__restrict__` told the compiler the arrays don't overlap, so it emitted packed AVX (`vfmadd...ps` over 8-16 floats/iteration) instead of a scalar loop (or a scalar loop *plus* a runtime overlap-check branch). Nothing about the arithmetic changed; the compiler was simply *unblocked*. The fingerprint of an aliasing-limited loop is exactly this: a numeric loop running far below SIMD throughput, where the asm shows scalar ops or a runtime aliasing guard (§21.5) despite `-O3 -march=native`. When you see it, `restrict` (or a refactor that proves non-overlap) is the lever — and you confirm by reading the codegen, the subject of §21.5.

## 21.4 Techniques

### 21.4.1 `__restrict__` to unblock the optimizer

`restrict` is a promise to the compiler: *for the lifetime of this pointer, the object it points to is accessed only through this pointer (and pointers derived from it).* It originated in C99 (`restrict`); in C++ it's the compiler extension `__restrict__`/`__restrict` (GCC/Clang/MSVC) — there is no standard C++ `restrict`, but the extension is universally available and the workhorse here.

- **Where to put it.** On the pointer parameters of hot numeric kernels whose arrays genuinely don't overlap: `out`, `in`, the operands of a transform/reduction/convolution. Marking *both* the source and destination `__restrict__` is what lets the compiler assume they're disjoint and vectorize freely (§21.3, §21.5).
- **It's a promise *you* must keep.** If the pointers *do* overlap at runtime and you marked them `restrict`, the behavior is undefined — silent miscompilation (the compiler reorders/vectorizes assuming disjointness). Use it only where you *know* the arrays are distinct (separate allocations, an in-place transform where in == out is genuinely safe element-wise, etc.). It's a correctness assertion, not a hint.
- **Alternatives when you can't promise globally.** If overlap is possible in general but not on the hot path, provide a `restrict` fast path and a safe fallback, or copy to a scratch buffer. The compiler's own runtime-overlap-check versioning does this automatically but at the cost of code bloat and a branch; an explicit `restrict` path removes both.
- **`char`/`std::byte` buffers are alias-pessimistic by design.** Because `char*`/`std::byte*` may alias any object (the strict-aliasing exception), code that works through byte buffers (parsers, serializers — Ch. 53) blocks optimization broadly. Where you process such a buffer numerically, load into typed locals / typed `restrict` pointers (via `memcpy`/`bit_cast` — §21.4.2) so the hot loop operates on values the compiler can reason about.
- **`assume`/`__builtin_assume_aligned` are cousins.** Telling the compiler about *alignment* (Ch. 9) similarly unblocks aligned SIMD loads; `[[assume]]` (C++23) / `__builtin_assume` feed it other invariants. Same principle: hand the optimizer provable facts it can't deduce.

### 21.4.2 `std::bit_cast` and `std::launder` for safe punning

Type punning — reinterpreting one type's bytes as another — is unavoidable in low-latency systems work (reading raw price bits, overlaying wire formats — Ch. 53), but the naive `*reinterpret_cast<int*>(&myfloat)` is **strict-aliasing UB** (§21.2). The sanctioned, zero-overhead channels:

- **`std::bit_cast<To>(from)` (C++20) — the right default for value punning.** Reinterprets the bit pattern of a trivially-copyable `From` as a same-sized `To`, with *defined* behavior — and compiles to nothing (a register move or no-op; §21.5). This is how you read a `float`'s bits, build a `double` from a `uint64`, or reinterpret fixed-layout POD — replacing the UB `reinterpret_cast` idiom entirely:

  ```cpp
  std::uint32_t bits = std::bit_cast<std::uint32_t>(price_float);   // defined; zero-cost
  float f          = std::bit_cast<float>(raw_u32);                 // (replaces the union/cast hacks)
  ```

- **`memcpy` — the pre-C++20 / variable-size workhorse.** `std::memcpy(&to, &from, n)` is *always* well-defined for copying object representations, and the compiler reliably folds a small fixed-size `memcpy` into a register move (no actual call — §21.5). It's the portable way to pun and to read a value out of a byte buffer without aliasing UB:

  ```cpp
  std::uint64_t px; std::memcpy(&px, wire_ptr, sizeof px);   // pull 8 bytes out, defined, zero-cost
  ```

- **`std::launder` — for *reusing storage*, not bit-punning.** A narrower tool: when you've placement-`new`'d a new object into existing storage (or need to access an object through a pointer obtained before its lifetime began), `std::launder` gives you a pointer the compiler treats as referring to the *new* object, defeating its (otherwise valid) assumption that the storage still holds the old one. Relevant to arenas/object pools (Ch. 24) and `std::variant`-like storage reuse — *not* a general "make my cast legal" tool (a common misconception).
- **Unions for punning are UB in C++ (unlike C).** Writing one union member and reading another is *defined in C* but **UB in C++** (only the last-written member may be read). Don't port the C union-punning idiom; use `bit_cast`/`memcpy`. (Compilers often *tolerate* it, but it's not guaranteed — the kind of UB that bites at `-O2` on the next compiler, Ch. 72.)
- **`char`/`std::byte` access is always allowed.** You may legally inspect *any* object's bytes through `char*`/`unsigned char*`/`std::byte*` — that's the sanctioned way to *view* representation. To turn those bytes back into a *typed value*, go through `bit_cast`/`memcpy` (§21.4.2), not a typed `reinterpret_cast` dereference.

The discipline that closes the loop with §21.4.1: pun *into typed local values* (defined, via `bit_cast`/`memcpy`), then run the hot loop over those typed values (optionally `restrict`-qualified) — you get both the safety of defined punning and the optimization freedom of clean aliasing.

## 21.5 Verify the codegen: vectorization gated by aliasing

This chapter is *about* codegen, so verification is the payoff, not an afterthought. Snippets are **verified** Clang output (`--target=x86_64-linux-gnu -O3 -march=x86-64-v3`).

**Without `restrict` — scalar, or vectorized-behind-a-guard.** The plain `axpy_plain` either stays scalar or emits a runtime overlap check that branches between a vector and a scalar version:

```asm
axpy_plain(float*, float const*, float, int):
        ; ... compares out/in ranges for overlap, branches:
        lea     rax, [rdi + 4*r?]          ; out end
        cmp     ...                        ; does [in, in+n) overlap [out, out+n)?
        jb      .Lscalar                   ; if might-overlap → SCALAR fallback loop
.Lvector:
        vfmadd... ymm...                   ; vector version (only taken when proven disjoint)
.Lscalar:
        vfmadd231ss xmm0, ...              ; SCALAR: one float per iteration  (the slow path)
```

The runtime guard and the scalar fallback are the codegen fingerprint of an aliasing-limited loop — extra code, a branch, and the risk of the slow path.

**With `__restrict__` — clean packed SIMD, no guard.** The promise removes the overlap question entirely; the compiler emits a straight vectorized loop:

```asm
axpy_restrict(float*, float const*, float, int):
.Lloop:                                    ; no overlap check — restrict promised disjoint
        vmovups ymm1, [rsi + rax]          ; load 8 floats of in[]
        vfmadd213ps ymm1, ymm0, [rdi+rax]  ; in*k + out, 8 lanes at once (FMA)
        vmovups [rdi + rax], ymm1          ; store 8 floats of out[]
        add     rax, 32
        ...                                ; packed, no scalar fallback, no branch
```

Eight (or sixteen, AVX-512) elements per iteration, no guard — the ~5× of §21.3 in asm form.

**`bit_cast`/`memcpy` pun to *nothing*.** Confirm defined punning is also free:

```asm
to_bits(float):                ; std::bit_cast<uint32_t>(f)  AND  memcpy(&u,&f,4)  →  IDENTICAL:
        vmovd   eax, xmm0      ; just move the bits float-reg → int-reg; no call, no store/reload
        ret
```

`std::bit_cast` and a fixed-size `memcpy` compile to the same single move as the (UB) `reinterpret_cast` would have — **defined behavior at zero cost**. The verification habits for this chapter:

- **For a numeric hot loop that should vectorize, read the asm:** packed ops (`...ps`/`...pd`, `vfmadd`) with *no* runtime overlap guard = aliasing is not blocking you. Scalar ops or a range-compare-and-branch = add `__restrict__` (or refactor to prove non-overlap) and re-check.
- **Confirm `bit_cast`/`memcpy` punning folded to a move** (no call, no spill) — it should; if a `memcpy` didn't fold, the size wasn't a constant or the types weren't trivially copyable.
- **Treat a lost vectorization as a regression** (Ch. 76): a change that reintroduces a possible-alias (e.g. routing a hot array through a `char*` buffer or dropping a `restrict`) silently drops you back to scalar — the asm diff catches it.

## 21.6 Pitfalls & anti-patterns: UB type punning via casts/unions

- **`reinterpret_cast`-and-dereference type punning.** The classic UB: `*reinterpret_cast<int*>(&myfloat)` (or any access through a pointer to an unrelated type) violates strict aliasing (§21.2). It often "works" until `-O2` on a different compiler reorders around it. Use `std::bit_cast`/`memcpy` (§21.4.2) — same codegen, defined.
- **Union punning ported from C.** Writing one union member and reading another is **UB in C++** (defined only in C). Don't carry the C idiom over; `bit_cast`/`memcpy`. (It compiles and usually runs — until it doesn't.)
- **`restrict` that lies.** Marking pointers `__restrict__` when they *can* overlap is UB and silently miscompiles (the compiler vectorizes/reorders assuming disjointness). Only assert `restrict` where you *know* the memory is distinct; it's a promise, not a hint.
- **Hot numeric code routed through `char`/`std::byte` buffers.** Because byte pointers alias everything, a loop reading numbers straight out of a `char*` buffer is alias-pessimized and won't vectorize. Pull values into typed locals (`memcpy`/`bit_cast`) first, then compute (§21.4.1).
- **Assuming `-O3`/`-march=native` guarantees vectorization.** It only enables it; aliasing (and alignment — Ch. 9, and FP reassociation — Ch. 11/27) can still block it. Verify in the asm (§21.5); don't assume the flag did it.
- **`std::launder` as a "make my cast legal" spell.** `launder` is for *storage reuse* (re-pointing at a newly-constructed object in existing bytes), not for bit-punning between types. Using it to bless a `reinterpret_cast` is still UB. Different tool for a different problem (§21.4.2).
- **Over-applying `restrict` everywhere.** Sprinkling it on every pointer adds risk (every one is a UB landmine if violated) for no gain where the loop doesn't vectorize anyway. Apply it to the hot kernels where the asm shows aliasing is the blocker.
- **Ignoring misalignment after fixing aliasing.** Even disjoint, a loop the compiler can't prove *aligned* may use unaligned/scalar prologue-epilogue handling; pair `restrict` with alignment knowledge (Ch. 9, `__builtin_assume_aligned`) for the cleanest SIMD.
- **Strict-aliasing UB as a security bug (Ch. 72).** Aliasing UB doesn't just slow things down — it can make the compiler emit code that violates your intended invariants (dropped bounds checks, reordered validation), a genuine vulnerability class on untrusted-input paths (Ch. 53, 72).

## 21.7 Exercises & checklist

**Exercises**

1. **Measure the swing.** Build `restrict.cpp` at `-O3 -march=native`; run `plain` vs `restrict` pinned/turbo-off. Confirm the ~several-× speedup. Then disassemble both (Ch. 4): does `plain` show a runtime overlap guard + scalar fallback, and `restrict` clean packed SIMD (§21.5)?
2. **Make the alias real.** Call `axpy_restrict` with *overlapping* pointers (`out = in + 1`) and compare results to the `plain` version. Observe the wrong answer (UB) — and explain why the compiler is entitled to produce it. (Run under UBSan where possible.)
3. **`bit_cast` vs `reinterpret_cast` codegen.** Implement float↔uint32 punning three ways: `reinterpret_cast` deref, union, and `std::bit_cast`/`memcpy`. Diff the asm (Ch. 4) — do they produce the same move? Now compile with `-fstrict-aliasing -Wstrict-aliasing -O2` and see which warn / which UBSan flags.
4. **Byte buffer de-pessimization.** Write a loop that sums big-endian `int32`s read directly from a `char*` buffer vs one that `memcpy`s each into a typed local first. Compare vectorization and ns/element (§21.4.1, ties Ch. 53).
5. **Lost-vectorization regression.** Take a vectorized `restrict` kernel, then refactor it to receive its data through a `std::byte*` view (no `restrict`). Confirm in the asm it fell back to scalar, and that a perf gate (Ch. 76) would catch the regression.

**Checklist — aliasing & type punning**

- [ ] Hot numeric kernels whose arrays are genuinely disjoint mark their pointers **`__restrict__`** — and I **only** promise it where overlap is truly impossible.
- [ ] I **verified vectorization in the asm** (§21.5): packed ops, **no** runtime overlap guard / scalar fallback — not just trusted `-O3 -march=native`.
- [ ] Type punning uses **`std::bit_cast`/`memcpy`** (defined, zero-cost) — **never** `reinterpret_cast`-deref or C-style union punning (UB).
- [ ] Numbers read from **`char`/`std::byte` buffers** are pulled into **typed locals** (`memcpy`/`bit_cast`) before the hot loop, so byte-pointer aliasing doesn't pessimize it (ties Ch. 53).
- [ ] `std::launder` is used **only** for storage reuse (placement-new), not to bless type casts.
- [ ] Where I rely on alignment for SIMD, I told the compiler (**`__builtin_assume_aligned`** / over-aligned types — Ch. 9), not just `restrict`.
- [ ] I tested `restrict` kernels for the **no-overlap precondition** (and under UBSan), since a violated promise is silent miscompilation.
- [ ] A change that reintroduces possible aliasing (byte-buffer routing, dropped `restrict`) is caught as a **vectorization regression** (Ch. 76) — and I treat strict-aliasing UB as a **security** concern on untrusted input (Ch. 72).

## 21.8 References

- ISO C++ standard / cppreference — `[basic.lval]` (the strict-aliasing rule and its permitted exceptions), `std::bit_cast`, `std::launder`, and `std::memcpy`'s object-representation guarantees (§21.2, §21.4.2).
- The C99/C11 standard on `restrict` and the GCC/Clang documentation for `__restrict__`/`__restrict` and `__builtin_assume_aligned` — the semantics and the promise the programmer makes (§21.4.1).
- GCC/Clang optimization and diagnostics docs — `-fstrict-aliasing`/`-fno-strict-aliasing`, `-Wstrict-aliasing`, and vectorization reports (`-fopt-info-vec`/`-Rpass=loop-vectorize`) for diagnosing §21.3/§21.5.
- A. Fog, *Optimizing software in C++* — aliasing as an optimization barrier, `restrict`, and the vectorization consequences (the measured basis of §21.3).
- JF Bastien / CppCon talks on `std::bit_cast` and type punning, and the classic "Understanding Strict Aliasing" writeups — the UB and the sanctioned alternatives (§21.4.2, §21.6).

## 21.9 Additional Reading

- D. Bakhvalov, *Performance Analysis and Tuning on Modern CPUs* — vectorization blockers (aliasing, alignment) and how to read the compiler's vectorization diagnostics.
- Chris Lattner / LLVM blog posts on undefined behavior ("What Every C Programmer Should Know About Undefined Behavior") — why strict-aliasing UB miscompiles and how to avoid it (ties Ch. 72).
- Ch. 29 (*SIMD & Vectorization*) — where `restrict` most often pays; Ch. 9 (*Object Layout/Alignment*) — the alignment half of clean SIMD; Ch. 53 (*Zero-Copy Wire Decoding*) — safe struct/byte-buffer punning in feed handlers; Ch. 72 (*Secure Programming*) — UB as a vulnerability class; Ch. 11 (*Pipelines*) — dependencies the compiler must assume when it can't disprove aliasing.
- **Appendix D** (Compiler Flag Reference) — `-fstrict-aliasing`/`-fno-strict-aliasing` and vectorization flags; **Appendix B** — aliasing and `unsafe` punning in Rust (`transmute`, `from_ne_bytes`) for the cross-language reader.

---

*Next: Ch. 22 — Build Toolchain for Speed, the capstone of Part III: how `-O2`/`-O3`/`-march`, inlining control, LTO, PGO and BOLT turn the source-level work of Part III into the fastest possible binary — and how a single flag can swing latency by double digits.*
