# Part V — Numerics & Data Parallelism

# Chapter 31 — Bit Manipulation & Integer Tricks

> **Prerequisites:** Ch. 30 (integer arithmetic — scaled prices and the integer math this chapter accelerates), Ch. 13 (branchless programming — bit tricks are the branchless toolkit), Ch. 11 (op latency/throughput — intrinsics have their own), Ch. 4 (reading asm — §31.5 confirms the single instruction), Ch. 28 (order book — bitmaps index its levels).
>
> **Leads into:** Ch. 32 (SIMD — the vector generalization of these bit ops), Ch. 60 (wire decoding — field packing/unpacking, branch-free integer parsing). Builds on Ch. 27 (pools — this chapter's bitmap is an alternative free-slot representation to its linked free-list) and Ch. 37 (CAS — for making a bitmap allocator concurrent). Companion to Ch. 13 & 30.

---

## 31.1 Why it matters: one instruction instead of a loop

Modern CPUs have dedicated instructions for operations that, written naively in C++, become loops or branches: counting set bits, finding the lowest or highest set bit, extracting or depositing bit fields, clearing the lowest set bit. A `popcount` is one `popcnt` instruction (~3 cycles) versus a bit-by-bit loop (~64 iterations); finding the first free slot in a 64-element pool is one `tzcnt` (~3 cycles) versus a linear scan. On the hot path — where you're managing order-book level occupancy, free-lists, bitmask flags, and parsing packed wire fields — these single-instruction primitives turn O(n) bit operations into O(1) and remove branches (Ch. 13) the predictor would otherwise mispredict. It's the most concentrated form of "do less work": replace a loop with an instruction.

The HFT relevance is concrete and recurring. An order book (Ch. 28) tracks which price levels are occupied — a **bitmap**, one bit per level — and the operations you do constantly are *exactly* the bit instructions: "is this level occupied?" (bit test), "what's the best occupied level above the touch?" (find-lowest-set, `tzcnt`), "how many levels are active?" (`popcnt`), "mark this level empty" (clear bit, `blsr` for lowest). An object pool's free-list (Ch. 27) is a bitmap: "find a free slot" is `tzcnt`, "allocate it" is clear-bit, "is the pool full?" is "is the word zero?" (no free bit left). Wire decoding (Ch. 60) packs multiple fields into bytes (a flags byte, a packed price-qty word) that you extract with shifts and masks — or, for scattered bits, `pext`. And the branchless idioms (min/max/abs/sign — Ch. 13) that keep unpredictable branches off the hot path are bit tricks. Knowing this toolkit is knowing how to express common hot-path operations as the handful of cycles they should be.

The discipline, as with everything in Part V, is *measure and verify*: these intrinsics have their own latency/throughput (Ch. 11) — `popcnt`/`lzcnt`/`tzcnt` are cheap (~3 cycles), but `pdep`/`pext` are **fast (~3 cycles) on Intel and on AMD Zen 3+, yet microcoded on AMD Zen 1/Zen 2** (~18 to hundreds of cycles, mask-dependent — a trap on legacy or mixed fleets, §31.6) — and the compiler doesn't always emit the instruction you expect from your C++. So this chapter maps the toolkit (§31.2), measures the intrinsics (§31.3), applies them to order-book bitmaps, branchless idioms, power-of-two tricks, and field packing (§31.4), and verifies the single-instruction emission in the asm (§31.5) — with a clear-eyed warning about the `pdep`/`pext` Zen 1/Zen 2 cliff. Prefer the **standard `<bit>` library** (C++20: `std::popcount`, `std::countr_zero`, `std::bit_width`, etc. — libstdc++ 9+/libc++ 9+, `__cpp_lib_bitops`; `std::byteswap` is C++23, libstdc++ 12+/libc++ 14+, `__cpp_lib_byteswap`; the pre-C++20 fallbacks are `__builtin_popcountll`/`__builtin_ctzll`/`__builtin_bswap64` — Appendix H) which compiles to these instructions portably and falls back gracefully — intrinsics only where the standard doesn't reach.

## 31.2 Mental model: BMI/BMI2 (`popcnt`, `lzcnt`/`tzcnt`, `pdep`/`pext`, `blsr`)

The bit-manipulation instruction families on x86-64, and the standard C++20 `<bit>` wrappers that emit them:

- **`popcnt` (population count)** — count the set bits in a word. `std::popcount(x)`. ~3-cycle latency, fully pipelined. Uses: how many order-book levels are occupied, how many flags set, Hamming distance/weight, sparse-set cardinality.
- **`lzcnt` / `tzcnt` (leading / trailing zero count)** — count zeros from the top / bottom, i.e. find the highest / lowest set bit's position. `std::countl_zero(x)` / `std::countr_zero(x)`. ~3-cycle. **The workhorses for bitmap scans**: `tzcnt` = "index of the lowest set bit" = "first free slot" / "best level above touch"; `lzcnt` = "highest set bit" = `floor(log2)` / "best level below touch". (Older `bsr`/`bsf` are similar but with an undefined-on-zero wart that `lzcnt`/`tzcnt` fix — they return the width on zero input.)
- **`blsr` / `blsi` / `blsmsk` (BMI1)** — `blsr` = **clear the lowest set bit** (`x & (x-1)`); `blsi` = **isolate the lowest set bit** (`x & -x`); `blsmsk` = mask up to the lowest set bit. These let you *iterate set bits* efficiently: `while (x) { i = tzcnt(x); use(i); x = blsr(x); }` visits each set bit in one `tzcnt`+`blsr` per bit — the standard way to walk a bitmap's occupied positions (e.g. iterate active order-book levels).
- **`pdep` / `pext` (BMI2 — parallel deposit / extract)** — `pext(x, mask)` **gathers** the bits of `x` selected by `mask` into the low bits of the result (compress scattered bits); `pdep(x, mask)` **scatters** the low bits of `x` out to the positions set in `mask` (the inverse). Enormously powerful for packing/unpacking non-contiguous bit fields, bit-permutations, and certain parsing tasks in *one instruction*. **The catch (§31.6): on Intel and on AMD Zen 3+ ~3 cycles; on AMD Zen 1/Zen 2 they're *microcoded and ~18-to-hundreds of cycles* (the cost grows with the mask's popcount) — a brutal cliff on legacy or mixed fleets.** Use with eyes open.
- **Shifts, masks, and the classics.** `&`/`|`/`^`/`~`/`<<`/`>>`, plus the *idioms*: `x & (x-1)` (clear lowest set), `x & -x` (isolate lowest set), `x | (x-1)` (set trailing bits), power-of-two test `x && !(x & (x-1))`, and so on — the *Hacker's Delight* repertoire. `std::has_single_bit`, `std::bit_ceil`, `std::bit_floor`, `std::bit_width`, `std::rotl`/`rotr` wrap many portably.

The mental model: **common hot-path operations on sets of small things (levels, slots, flags) are bit operations on a word, and the CPU does them in one ~3-cycle instruction. Reach for `<bit>` (portable) first; the operations are: count (`popcount`), find-first/last (`countr_zero`/`countl_zero`), iterate (`tzcnt`+`blsr`), pack/unpack (shifts/masks, or `pext`/`pdep` *on Intel and Zen 3+*).**

## 31.3 Measure it: intrinsic latency/throughput

Two things to measure: the **latency/throughput of the intrinsics** (so you know their cost — Ch. 11), and the **win over the naive loop** they replace. The headline comparison: find the first set bit (first free slot) via `tzcnt` vs a naive scan, plus `pext` on the reference (Intel) machine; the AMD Zen 2 side of the `pdep`/`pext` vendor difference is *quoted* from Agner Fog / uops.info, not measured here (Exercise 5 measures it if you have the hardware).

```cpp
// bits.cpp — tzcnt vs naive first-set-bit scan; popcount and pext throughput.
// Build: g++ -O2 -std=c++20 -march=native bits.cpp -o bits      (-march must enable BMI2 for pext)
// Run pinned, one mode at a time:
//   for m in tzcnt popcnt pext scan; do taskset -c 2 ./bits $m; done
#include <immintrin.h>   // _pext_u64
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <bit>
#include <vector>
#include <random>
#include <chrono>

// The mode is a template parameter, resolved BEFORE the timed loop (a strcmp per element would
// cost more than the ~1-cycle instruction being measured).
enum class Op { Tzcnt, Popcnt, Pext, Scan };
template <Op O>
[[gnu::noinline]] std::uint64_t run(const std::vector<std::uint64_t>& w, int reps) {
    std::uint64_t acc = 0;
    for (int r = 0; r < reps; ++r)
        for (std::uint64_t x : w) {
            if constexpr (O == Op::Tzcnt)       acc += std::countr_zero(x);         // one instruction
            else if constexpr (O == Op::Popcnt) acc += std::popcount(x);            // one instruction
            else if constexpr (O == Op::Pext)   acc += _pext_u64(x, 0x5555'5555'5555'5555ULL); // one instruction (BMI2)
            else { int p = 0; while (!(x & 1)) { ++p; x >>= 1; } acc += p; }        // NAIVE loop
            asm volatile("" : "+r"(acc));   // keep each result live; no vectorizing across elements
        }
    return acc;
}

int main(int argc, char** argv) {
    const char* mode = argc > 1 ? argv[1] : "tzcnt";
    constexpr int N = 1 << 20, REPS = 64;
    std::vector<std::uint64_t> w(N);
    std::mt19937_64 rng(1);
    // Non-zero words whose FIRST set bit sits at a uniformly random position 0..63 — a realistic
    // "first free slot" query. (rng() | 1 would put it at bit 0 every time: the scan would never loop.)
    for (auto& x : w) x = (rng() | 1) << (rng() & 63);

    auto t0 = std::chrono::steady_clock::now();
    std::uint64_t acc = !std::strcmp(mode, "tzcnt")  ? run<Op::Tzcnt>(w, REPS)
                      : !std::strcmp(mode, "popcnt") ? run<Op::Popcnt>(w, REPS)
                      : !std::strcmp(mode, "pext")   ? run<Op::Pext>(w, REPS)
                                                     : run<Op::Scan>(w, REPS);
    auto t1 = std::chrono::steady_clock::now();
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
    std::printf("%-7s acc=%llu  %.3f ns/op\n", mode, (unsigned long long)acc, ns/((double)REPS*N));
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP, 2.9 GHz), GCC 13 `-O2 -std=c++20 -march=native`, Linux 6.18, pinned, turbo off (illustrative). Two harness points (Appendix L): the mode is a template parameter chosen *before* the clock starts (an earlier draft had `strcmp(mode, …)` inside the loop — two PLT calls per element against a 1-cycle instruction), and the test words have their first set bit at a *random* position (an earlier draft's `rng() | 1` put it at bit 0 every time, so the "scan" never looped and all three modes measured the same add):

```
                    ns / op       notes
tzcnt (countr_zero) ~0.35 ns      one 3-cycle-latency, 1/cycle instruction; the loop runs at ~1 cycle/element
popcnt (popcount)   ~0.35 ns      same
pext (even bits)    ~0.35 ns      same on Intel (3-cycle latency, 1/cycle) — see the Zen 2 row below for the other vendor
scan (naive loop)   ~17 ns        ~32 iterations on average (3 instructions each) + one mispredict at exit — ~50x

intrinsic latency/throughput (Agner Fog / uops.info — the AMD rows are quoted, not measured here):
   popcnt/lzcnt/tzcnt   ~3 cyc latency, 1/cyc throughput          (cheap everywhere)
   blsr/blsi/and/shift  ~1 cyc                                    (trivial)
   pdep/pext  Intel, AMD Zen 3+: ~3 cyc   |   AMD Zen 1/Zen 2: ~18-290 cyc, mask-dependent (microcoded!)  <- §31.6
```

Read it the Ch. 11/13 way: the intrinsic is a flat ~3-cycle op; the naive scan is a *variable-length loop with a data-dependent branch* (Ch. 13) whose cost depends on where the first set bit is (0 to 63 iterations) and mispredicts at its exit — ~50× slower on average and with a tail that the single instruction simply does not have. The single instruction is both faster and *deterministic*. The op-latency block is the cost model to keep: `popcnt`/`lzcnt`/`tzcnt`/`blsr` are cheap and pipelined (use freely), while **`pdep`/`pext` carry the Zen 1/Zen 2 warning** (§31.6) — verify the deployment CPU before relying on them. As always, confirm the compiler emitted the instruction (§31.5): `std::countr_zero` *should* be a single `tzcnt`, but check.

## 31.4 Techniques

### 31.4.1 Bitsets/bitmaps for order-book occupancy and free-lists

The flagship hot-path use: represent a *set of small indices* (occupied levels, free slots, active flags) as bits in a word (or array of words), and do set operations with bit instructions.

- **Order-book level occupancy (Ch. 28).** One bit per price level: bit *i* set = level *i* occupied. Then: "best occupied level above the touch" = `tzcnt` of the bits above the touch (one instruction vs scanning levels); "is level *i* active?" = bit test; "number of active levels" = `popcount`; "iterate active levels" = the `tzcnt`+`blsr` loop. A 64-level window is one `uint64`; a deeper book is an array of words scanned word-by-word (skip all-zero words instantly). This is the natural next optimization of the §28.4.5 book: an occupancy bitmap over its tick-indexed level array turns level navigation from level-by-level array walking into single-instruction bit scans.
- **Pool free-lists (Ch. 27).** A bitmap of free slots: "allocate" = `tzcnt` to find a free slot + `blsr`/clear-bit to mark it used (O(1), no free-list traversal); "free" = set the bit; "full?" = `word == 0` (no free bit left; "empty?" = `!~word`, all bits set); "how many free?" = `popcount`. For a 64-slot pool, allocation is two instructions. Scales to arrays of words with a summary bitmap (a bit per word indicating "has free slot") for fast multi-word search — a hierarchical bitmap.
- **`std::bitset` / hand-rolled.** `std::bitset<N>` provides `count()`/`_Find_first()` (libstdc++) but its API is clunky for hot loops; many low-latency codebases hand-roll a `uint64[]` bitmap with `<bit>` ops for full control and to guarantee the intrinsics. Either way the operations are the same.
- **Why bitmaps win.** A 64-element set — a whole cache line as a `bool[64]` — collapses to one 64-bit register as a bitmap (a 512-slot bitmap is one cache line); set operations are single instructions; iteration is branch-light; and it's dense (no pointers, no allocation — Ch. 26–28). For "which of these N small things are active," a bitmap is almost always the right representation on the hot path.

### 31.4.2 Branchless min/max/abs/sign

The branchless toolkit (Ch. 13) expressed in bit/integer tricks — removing unpredictable branches from the hot path:

- **min/max.** `std::min`/`std::max` compile to `cmov`/`minss` (§13.5) — branchless. The bit-trick form `y ^ ((x ^ y) & -(x < y))` selects without a branch where you need to force it.
- **abs / sign.** `abs(x)` branchless: `(x ^ (x >> 31)) - (x >> 31)` (for 32-bit; the arithmetic shift broadcasts the sign bit to a mask). `sign(x)` = `(x > 0) - (x < 0)`. `std::abs` already does the branchless thing; the idiom matters when hand-vectorizing (Ch. 32) or in a mask-select.
- **clamp / saturate.** Branchless clamp via `min`/`max` composition; saturating add/subtract via overflow-detection masks — useful for bounded counters and price-band clamping without branches.
- **select / conditional via mask.** The general pattern (§13.4.3): `result = (a & mask) | (b & ~mask)` where `mask = -(cond)` (all-ones if `cond`, else zero) selects `a` or `b` branchlessly. The scalar shadow of SIMD predication (Ch. 32), and the way to do "conditionally update" without a mispredict.
- **When to use the bit form vs `std::`/`cmov`.** Prefer `std::min`/`max`/`abs`/`clamp` (clear, compiles well) by default; reach for the explicit mask/bit idiom when the compiler *won't* go branchless, when vectorizing (Ch. 32 needs the mask form), or when composing several selects. Verify (§31.5, §13.5) you got branchless code.

### 31.4.3 Power-of-two rounding and fast modulo via masks

When a size/capacity is a **power of two**, expensive operations become cheap bit ops — the reason ring buffers (Ch. 37, 40) and hash tables (Ch. 28) size to powers of two:

- **Fast modulo: `x % N` → `x & (N-1)`** when `N` is a power of two. An integer division (`div r64` ~15-18 cycles latency on Ice Lake-SP, ~35-90 on Skylake-era cores — Agner Fog / uops.info, Ch. 11) becomes an AND (~1 cycle). This is *why* ring-buffer capacities are powers of two — wrapping the index is `idx & (cap-1)`, not a modulo or a branch. The single highest-value power-of-two trick on the hot path.
- **Round up to a power of two / to an alignment.** `round_up(x, A) = (x + A - 1) & ~(A - 1)` for power-of-two `A` (alignment rounding — Ch. 9, 27 allocators use this constantly); `std::bit_ceil(x)` rounds up to the next power of two; `std::bit_floor` rounds down. Used for sizing buffers, aligning pointers, bucketing.
- **Multiply/divide by powers of two = shifts.** `x * 8` = `x << 3`, `x / 8` = `x >> 3` (for unsigned / careful with signed). The compiler does this for constants; relevant when the shift amount is dynamic or when reasoning about scaled-integer prices (Ch. 30).
- **Power-of-two tests and log2.** `std::has_single_bit(x)` (is power of two), `std::bit_width(x)-1` = `floor(log2(x))` via `lzcnt`. Useful for size-class computation (Ch. 27 slab allocators) and bucket indexing.
- **The constraint.** These require power-of-two sizes; choosing capacities/alignments as powers of two up front (a cheap design decision) unlocks all of them. Where a size *can't* be a power of two, a precomputed reciprocal-multiply (libdivide-style) beats a runtime `div` for repeated division by the same constant.

### 31.4.4 Field packing/unpacking; Morton/Z-order interleaving

Packing multiple values into one word and extracting them — pervasive in wire formats (Ch. 60) and compact data structures:

- **Packing/unpacking bit fields with shifts and masks.** Combine several small fields into one word: `packed = (a << SHIFT_A) | (b << SHIFT_B) | c`; extract: `a = (packed >> SHIFT_A) & MASK_A`. This is how a flags byte, a packed (price, qty) word, or a compact key is built/read — exact, fast, allocation-free. Prefer explicit shift/mask over C bit-fields (Ch. 9) on the hot path for predictable codegen (bit-field codegen can be surprisingly poor — Ch. 9).
- **`pext`/`pdep` for *scattered* fields (Intel, AMD Zen 3+).** When the bits to extract/deposit aren't contiguous (an interleaved or irregular layout), `pext(x, mask)` compresses them and `pdep` scatters them — one instruction where shifts+masks+ORs would be a sequence. Powerful for bit-permutations, certain decoders, and Morton codes. **But the Zen 1/Zen 2 cliff (§31.6) applies** — guard it.
- **Morton / Z-order interleaving.** Interleaving the bits of two (or more) coordinates into one key (`z = interleave(x, y)`) gives a **Z-order curve** — a space-filling ordering that preserves 2D locality in a 1D key, useful for spatial indexing, cache-friendly 2D traversal, and some matrix/grid layouts. Computed via magic-number shift/mask sequences, or `pdep` with alternating masks on Intel/Zen 3+ (one instruction per coordinate). Niche in HFT but appears in market-data grids, heatmaps, and certain matching/spatial structures.
- **Endianness (Ch. 60).** Wire fields are often big-endian; `std::byteswap` (C++23) / `bswap` converts in one instruction. Pack/unpack with the wire's byte order in mind; combine with `bit_cast`/`memcpy` (Ch. 23) to move bytes into typed values safely.

## 31.5 Verify the codegen: intrinsic emission

The whole premise is "one instruction," so verify the compiler emitted it. Snippets are **verified** Clang 18.1 output (Appendix L.3; `--target=x86_64-linux-gnu -O2 -march=x86-64-v3` for BMI/BMI2).

**`std::countr_zero` → `tzcnt`, `std::popcount` → `popcnt`.** The standard `<bit>` functions compile to the single instruction (with `-march` enabling the ISA):

```asm
first_free(unsigned long):          ; std::countr_zero(x)
        tzcnt   rax, rdi            ; one instruction (returns 64 if x==0, defined)
        ret
count_active(unsigned long):        ; std::popcount(x)
        popcnt  rax, rdi            ; one instruction
        ret
```

**Iterate set bits → `tzcnt` + `blsr`.** The bitmap-walk idiom compiles to the tight two-instruction-per-bit loop:

```asm
.loop:                              ; while (x) { i = tzcnt(x); use(i); x = blsr(x); }
        tzcnt   rcx, rax            ; i = index of lowest set bit
        ; ... use(i) ...
        blsr    rax, rax            ; x &= x-1  (clear lowest set bit)
        jnz     .loop
```

**Power-of-two modulo → `and`.** `idx % cap` with power-of-two `cap` is a single AND, not a `div`:

```asm
        and     eax, 1023          ; idx & (1024-1)   — no div (~1 cyc vs ~15-18 for div r64 on Ice Lake-SP)
```

**`pext` — and the Zen 1/Zen 2 warning.** `_pext_u64(x, mask)` (`<immintrin.h>`, needs BMI2 via `-mbmi2`/`-march`; the underlying builtin is `__builtin_ia32_pext_di`) emits one `pext`:

```asm
        pext    rax, rdi, rsi      ; gather masked bits — ~3 cyc on Intel and Zen 3+, MICROCODED ~18-290 cyc on Zen 1/Zen 2
```

The verification habits: **for any "one instruction" claim, disassemble and confirm the single op** (`tzcnt`/`popcnt`/`blsr`/`and`) rather than a loop or a `div`; **confirm `-march` enabled the ISA** (without `-mpopcnt`, `std::popcount` falls back to a `__popcountdi2` libgcc call on GCC or an inline ~15-instruction SWAR sequence on Clang — a few × slower in throughput, not a loop; without `-mbmi`, `std::countr_zero` becomes `rep bsf` plus a `cmov` for the zero case — negligible; verified GCC 13.3 / Clang 18.1 at `-O2 -std=c++20` with no `-march`); and **for `pdep`/`pext`, confirm the deployment target is Intel or AMD Zen 3+** (or guard with a runtime-dispatch / shift-mask fallback for Zen 1/Zen 2 — §31.6). When `std::popcount` *didn't* become `popcnt`, your build flags (Ch. 24) didn't enable the ISA — fix `-march`.

## 31.6 Pitfalls & anti-patterns: `pdep`/`pext` on AMD; portability

- **`pdep`/`pext` on AMD Zen 1/Zen 2 (the headline trap).** On Intel and on AMD Zen 3+ (Milan, Genoa, Turin) they're ~3-cycle; on **AMD Zen 1/Zen 2** (through 2019's Rome / Ryzen 3000) they're *microcoded* at ~18 to ~290 cycles depending on the mask's popcount — so a hot path tuned with `pext` on an Intel dev box can be *catastrophically* slow on a legacy AMD box. If you deploy on mixed or older AMD fleets, benchmark on the target, and provide a shift/mask fallback or runtime dispatch. Never assume `pext`/`pdep` are fast without checking the exact microarchitecture.
- **Missing `-march`/ISA → software fallback.** `std::popcount`/`countr_zero` etc. compile to the fast instruction *only if* the ISA is enabled (`-mpopcnt`/`-mbmi`/`-march=...`, Ch. 24). Without it, `std::popcount` becomes a libgcc call (GCC) or an inline SWAR sequence (Clang) — a few × slower in throughput, a silent regression in a tight bitmap loop; `countr_zero` degrades only to `bsf` + `cmov`. Verify the instruction in the asm (§31.5) and set `-march` for the deployment CPU.
- **`bsr`/`bsf` undefined on zero.** The older bit-scan instructions are *undefined* for a zero input (garbage result), a classic bug. `lzcnt`/`tzcnt` (and `std::countl_zero`/`countr_zero`) define the zero case (return the width); prefer them, or guard zero explicitly.
- **Signed shifts and UB.** In C++20 (P0907, two's complement) signed `>>` is an arithmetic shift and signed `<<` wraps modulo 2ⁿ — both well-defined; in C++17 and in C a left shift that overflows or shifts a negative value is **UB** and right-shifting a negative value is implementation-defined (Ch. 23, 87). In *every* standard a shift count that is negative or ≥ the width is UB. Use unsigned types for bit manipulation; be deliberate about sign and about which standard the code is built under.
- **C bit-fields for hot-path packing.** `struct { unsigned a:3, b:5; }` has implementation-defined layout and often *poor* codegen (Ch. 9) — the compiler may emit clumsy masking. For hot-path packing, use explicit shifts/masks (§31.4.4) for predictable, fast code.
- **Endianness bugs.** Packing/unpacking wire fields without accounting for byte order (Ch. 60) corrupts values silently. Use `std::byteswap`/`bswap` and `bit_cast`/`memcpy` (Ch. 23); test against real wire data.
- **Reinventing `<bit>` (and getting it wrong).** Hand-rolled popcount/bit-scan idioms are easy to get subtly wrong (and may not become the instruction). Prefer the standard `<bit>` functions (C++20) — portable, correct, and they emit the instruction with the right flags.
- **Over-cleverness hurting readability/maintainability.** A dense `pext`/magic-number bit hack that saves 1 ns but nobody can read or verify is a liability. Reserve the heavy tricks for *measured* hot spots, comment them with the idiom name (*Hacker's Delight* reference), and keep a tested fallback.
- **Assuming the bitmap fits one word.** A bitmap larger than 64 bits needs word-array logic (per-word scan, summary bitmap for fast search — §31.4.1); a naive single-word assumption breaks for deep books / large pools. Design the multi-word scan deliberately.

## 31.7 Exercises & checklist

**Exercises**

1. **Intrinsic vs loop.** Build `bits.cpp`; run `tzcnt`/`popcnt`/`scan` pinned. Confirm the intrinsics are flat ~3-cyc and the naive scan is several× slower with a data-dependent tail (Ch. 13). Disassemble (§31.5) — single instruction?
2. **`-march` regression.** Build `std::popcount` with and without `-mpopcnt`/`-march=native`. Confirm the no-ISA build emits a `__popcountdi2` call (GCC) or a SWAR sequence (Clang) in the asm, and measure how many × slower it is in a tight loop. This is the §31.6 silent-regression trap.
3. **Order-book occupancy bitmap.** Implement a 256-level book occupancy as a `uint64[4]` bitmap; implement "best level above/below touch" (`tzcnt`/`lzcnt` with the right word), "count active" (`popcount` summed), and "iterate active" (`tzcnt`+`blsr`). Compare to a level-by-level array scan. Measure.
4. **Power-of-two ring index.** Build a ring buffer with capacity a power of two (`idx & (cap-1)`) vs a non-power-of-two (`idx % cap`). Diff the asm (and vs `% cap` with a `div`) and measure the wrap cost (§31.4.3).
5. **`pext` vendor test (if you have AMD).** Implement a scattered-field extract with `pext` vs shifts/masks. Benchmark on Intel and AMD (a Zen 2 box if you can find one, and a Zen 3+ one to see the cliff gone). Quantify the Zen 1/Zen 2 cliff (§31.6); add a runtime-dispatch fallback.

**Checklist — bit manipulation & integer tricks**

- [ ] Set-of-small-things state (order-book occupancy, pool free-lists, flags) uses **bitmaps** with `<bit>` ops (`popcount`/`countr_zero`/`countl_zero`, `tzcnt`+`blsr` iterate) — not scans/loops (§31.4.1).
- [ ] I use the **standard `<bit>` library** (C++20) for portability, and **verified in the asm** (§31.5) that it emitted the single instruction — and that **`-march` enabled the ISA** (no software fallback — §31.6).
- [ ] **`pdep`/`pext` are guarded for microarchitecture** — fast on Intel and Zen 3+, microcoded on Zen 1/Zen 2; I benchmarked on the deployment CPU and have a fallback for mixed fleets (§31.6).
- [ ] Power-of-two sizes are used for ring/hash capacities so **modulo is `& (N-1)`** and rounding is a mask (§31.4.3); `div`-by-constant uses reciprocal-multiply where unavoidable.
- [ ] Branchless min/max/abs/sign/select via `std::`/`cmov`/masks (Ch. 13) where the branch is unpredictable — **verified branchless** (§31.5, §13.5).
- [ ] Hot-path field packing uses **explicit shifts/masks** (not C bit-fields — Ch. 9) with correct **endianness** (`byteswap` — Ch. 60); `bsr`/`bsf` zero-input and signed-shift UB are avoided.
- [ ] Multi-word bitmaps use a deliberate **per-word + summary** scan (not a single-word assumption) for deep books / large pools (§31.4.1).
- [ ] Heavy bit hacks are reserved for **measured** hot spots, **commented** with the idiom, and kept readable with a tested fallback (§31.6).

## 31.8 References

- H. Warren, *Hacker's Delight* — the definitive collection of bit-manipulation and branchless integer idioms (the source for §31.4.2-§31.4.4); the essential reference for this chapter.
- ISO C++ / cppreference — the C++20 `<bit>` header (`popcount`, `countl_zero`/`countr_zero`, `bit_width`, `bit_ceil`/`bit_floor`, `has_single_bit`, `rotl`/`rotr`, `byteswap`) — the portable wrappers (§31.2).
- Intel *Intrinsics Guide* and *SDM* (BMI1/BMI2: `popcnt`, `lzcnt`/`tzcnt`, `blsr`/`blsi`, `pdep`/`pext`) — instruction semantics and the Intel latency/throughput (§31.2-§31.3).
- A. Fog, *Instruction Tables* — per-microarchitecture latency/throughput, **including the AMD `pdep`/`pext` microcode cost** that drives §31.6.
- The libdivide documentation — fast division by runtime constants via reciprocal-multiply, for non-power-of-two divisors (§31.4.3).

## 31.9 Additional Reading

- Daniel Lemire's blog and papers on bit manipulation, fast bitmap iteration, and SIMD bit tricks — practical, measured treatments extending §31.4.1/§31.4.4.
- Sean Eron Anderson's *"Bit Twiddling Hacks"* (Stanford) — a widely-used catalog complementing *Hacker's Delight*.
- Ch. 13 (*Branchless Programming*) — the branch-removal context for §31.4.2; Ch. 30 (*Fixed/Floating-Point*) — the integer math this accelerates; Ch. 32 (*SIMD*) — the vector generalization (and SIMD bitmap/popcount); Ch. 28 (*Containers*) — order-book bitmaps; Ch. 37 (*Lock-Free*) — bitmap free-lists; Ch. 60 (*Wire Decoding*) — field packing and branch-free parsing.
- Agner Fog's *Instruction Tables* and uops.info (§31.8) — per-microarchitecture `popcnt`/`tzcnt`/`pdep`/`pext` latency and throughput (Appendix E's table has no ALU-instruction rows); **Appendix D** — the `-march`/ISA flags that enable these instructions.

---

*Next: Ch. 32 — SIMD & Vectorization, the data-parallel capstone of Part V: auto-vectorization and intrinsics, SSE/AVX/AVX-512, data layout for vectorization, and when SIMD pays off — including the downclocking caveat that can make a fast kernel slow the whole core.*
