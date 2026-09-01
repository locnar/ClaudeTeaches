# Part II — CPU Microarchitecture

# Chapter 9 — Object Layout, Alignment & Padding

> **Prerequisites:** Ch. 7 (cache lines are 64 bytes; you pay per line) and Ch. 8 (layout decides the miss rate; hot/cold splitting). This chapter zooms from the *collection* to the *individual struct*: how big it really is, why, and how to control it. Ch. 4 (reading asm) is used directly in §9.5.
>
> **Leads into:** Ch. 10 (prefetch/non-temporal stores) and the order-book case study (Ch. 25) consume these sizing decisions; the cache-line-alignment idea here becomes the *false-sharing* fix in Ch. 33 (`alignas(64)`, `hardware_destructive_interference_size`). Atomics alignment ties to Ch. 30. ARM's different cache-line size is Appendix A.

---

## 9.1 Why it matters: silent padding bloats the cache footprint

Ch. 8 taught you to put only the hot fields in the hot struct. This chapter is about a quieter tax: **the struct you wrote is probably bigger than the sum of its fields, because the compiler inserted padding you never asked for — and that padding inflates your cache footprint, your miss rate, and your tail.** A `struct` that *looks* like 13 bytes of useful data can silently occupy 24, and if it's the element type of a hot array, you just put 45% dead air into every cache line, fetched on every access, for nothing.

The mechanism is **alignment** (§9.2). Each scalar type has an alignment requirement, the compiler must place each member at a properly-aligned offset, and it inserts *padding* bytes to make that happen — plus tail padding so arrays of the struct keep every element aligned. The amount of padding depends on **the order you declared the members**. The exact same fields, reordered, can shrink a struct from 24 bytes to 16 — a free 33% cut to the hot-path footprint, no logic changed, just declaration order.

Why this matters at HFT scale: layout (Ch. 8) and sizing (this chapter) compound. If your order-book `LevelHot` is 24 bytes, two-and-a-fraction fit per 64-byte line; squeeze it to 16 and four fit per line — *halving* the lines touched by the book-update sweep, halving the effective footprint, doubling how much of the book stays in L1. The same reasoning, inverted, is why **over**-alignment matters too: a value that must not share a cache line with another thread's writes (Ch. 33) needs `alignas(64)`, deliberately *adding* padding to buy isolation. Padding is a tool — silent padding is a bug; deliberate padding is a technique. This chapter teaches you to *see* it (§9.3), eliminate the wasteful kind (§9.4.1), and add the useful kind (§9.4.2).

The discipline: **never assume `sizeof` — measure it, and know why it is what it is.**

---

## 9.2 Mental model

### 9.2.1 The C++/ABI object model: member ordering and struct padding

Every scalar type `T` has a size and an **alignment** `alignof(T)` — an address must be a multiple of `alignof(T)` to hold a `T`. On x86-64 (LP64): `char`=1, `short`=2, `int`=4, `long`/`double`/pointer=8, each aligned to its own size. The compiler lays out a struct by placing members **in declaration order**, advancing an offset, and **inserting padding** so each member lands on its required alignment. Then it rounds the total size up to a multiple of the struct's overall alignment (the max of its members') — **tail padding** — so that in an *array*, element `i+1` is still aligned.

A worked example (verified: `sizeof == 16`):

```cpp
struct Aligned { char c; long v; };   // alignof(long)==8
// layout:  c at 0,  [7 bytes PADDING],  v at 8.   sizeof == 16, alignof == 8
//          ^^^^^^^^^ the compiler inserted 7 dead bytes so v is 8-aligned
```

That one `char` cost 8 bytes, not 1. Now the order-dependence that §9.4.1 exploits — the *same three fields*, two orderings:

```cpp
struct Bad  { char a; long b; char c; };  // a@0, pad[7], b@8, c@16, pad[7]  => sizeof 24
struct Good { long b; char a; char c; };  // b@0, a@8, c@9, pad[6]           => sizeof 16
```

Identical data, identical alignment rules, **24 vs 16 bytes** — purely from declaration order. `Bad` scatters two `char`s around the 8-byte `long`, paying tail padding twice; `Good` puts the big member first and packs the small ones together. The heuristic that falls out: **declare members in decreasing size/alignment order** and you'll usually hit minimum padding automatically.

```
Bad  (24B):  [a][pad x7][b b b b b b b b][c][pad x7]
Good (16B):  [b b b b b b b b][a][c][pad x6]
                                      ^ one padding region, not two
```

### 9.2.2 `alignof`/`alignas`; natural vs over-alignment (SIMD, cache-line)

- **`alignof(T)`** queries a type's alignment; **`alignas(N)`** *raises* it (you can't lower alignment below natural with `alignas`). **Natural alignment** is the type's default (matches its size for scalars); **over-alignment** is asking for more than natural, for two hot-path reasons:
  - **SIMD:** AVX wants 32-byte-aligned, AVX-512 64-byte-aligned loads/stores for the aligned (`vmovdqa`) path and to avoid cache-line-split penalties (Ch. 29). `alignas(32)` / `alignas(64)` on a buffer or struct guarantees it.
  - **Cache-line isolation (false sharing):** `alignas(64)` (or `alignas(std::hardware_destructive_interference_size)`) forces a value onto its own cache line so a *different* thread writing a neighboring value doesn't trigger MESI ping-pong (Ch. 7 §7.2.3, Ch. 33). Here padding is the *point*.
- **`std::hardware_destructive_interference_size`** (C++17, `<new>`) — the recommended minimum offset to avoid false sharing (64 on x86); its twin `hardware_constructive_interference_size` is the line size to *pack within*. (Compilers warn about ABI-stability of these constants; many shops hardcode 64 with a `static_assert` — Ch. 33.)
- **`alignas` adds padding too.** Over-aligning a struct member or the struct itself inserts padding to honor the request — deliberate, not silent. Know the size cost you're buying.

### 9.2.3 `[[no_unique_address]]` and empty-base optimization

A subtlety that bites generic, policy-heavy hot-path code (Ch. 19). An **empty** type (no nonstatic data members — a stateless comparator, allocator, or tag) still has `sizeof >= 1` as a standalone object, so that it has a unique address. Stored as a *member*, that 1 byte (plus its own alignment padding) bloats your struct for nothing.

Two mechanisms reclaim it:

- **Empty Base Optimization (EBO):** an empty *base class* contributes **zero** size. The classic trick (used pervasively in the STL — `std::vector`'s allocator, `std::tuple`) is to *inherit* from the empty policy rather than store it as a member.
- **`[[no_unique_address]]`** (C++20): apply it to an empty *member* and the compiler may give it zero size (overlapping it with adjacent members), achieving EBO's benefit without the inheritance gymnastics:

  ```cpp
  template <class Compare>
  struct Node {
      [[no_unique_address]] Compare cmp;   // empty comparator -> 0 bytes
      std::int64_t price;                  // no longer pushed past a wasted byte
  };
  ```

For low-latency code this is how zero-cost abstractions stay *actually* zero-cost in `sizeof` — a stateless policy must not enlarge the hot struct. Verify with `static_assert` on `sizeof` (§9.3).

### 9.2.4 Bit-fields and their codegen

When several fields each need only a few bits (flags, small enums, a side bit, a venue id), **bit-fields** pack them into shared storage:

```cpp
struct OrderFlags {
    std::uint32_t side       : 1;   // buy/sell
    std::uint32_t type       : 3;   // limit/market/...
    std::uint32_t venue      : 6;
    std::uint32_t tif        : 3;   // time-in-force
    std::uint32_t reserved   : 19;
};                                  // 4 bytes instead of 4 separate fields
```

This shrinks the struct (good for footprint), but the trade-off is **codegen**: reading or writing a bit-field is not a plain load/store — it's a load + **mask + shift** (and read-modify-write for stores), which is more instructions and, crucially, makes the field a **non-atomic** sub-word (you cannot `std::atomic` a bit-field; concurrent writes to *different* bit-fields in the same unit race — Ch. 30, 33). Bit-field layout (bit order within the unit, straddling) is also **implementation-defined**, so they're risky for wire/overlay formats (Ch. 53 prefers explicit shifts/masks for protocol parsing). Use them to shrink *internal* hot structs where the mask/shift cost is below the cache-footprint win — and measure both (§9.5, Ch. 28 covers the bit-manipulation codegen in depth).

---

## 9.3 Measure it: inspecting layout with `pahole` and `-Wpadded`

You should never *guess* a struct's layout. Three tools make it visible:

**`static_assert(sizeof(T) == ...)`** — the cheapest guard. Pin the size (and `alignof`, and `offsetof` for overlay types) so a future field reorder or addition that bloats the struct fails the build, not production:

```cpp
static_assert(sizeof(LevelHot) == 16, "LevelHot grew — check padding/footprint");
static_assert(alignof(LevelHot) == 8);
```

**`-Wpadded`** (GCC/Clang) — warns *every time* the compiler inserts padding, naming the struct and the gap. Noisy by design (you don't want it on globally), but invaluable when aimed at a specific hot header: compile that TU with `-Wpadded` and it tells you exactly where the dead bytes are.

**`pahole`** (from `dwarves`, reads DWARF debug info) — the definitive layout inspector. It prints each member's offset and size, the holes (internal padding) and the tail padding, and a summary:

```
$ g++ -g -O2 -c book.cpp && pahole -C LevelHot book.o
struct LevelHot {
        long int   price;                /*     0     8 */
        long int   total_qty;            /*     8     8 */
        unsigned int order_count;        /*    16     4 */
        /* size: 24, cachelines: 1, members: 3 */
        /* padding: 4 */                                  <- 4 wasted tail bytes
};
```

`pahole` tells you the truth: this `LevelHot` is 24 bytes (3 per cache line) with 4 bytes of tail padding — drop or reorder to hit 20→ aligned-16 isn't possible here without removing a field, but you *see* the cost and decide. `pahole --reorganize` even suggests a minimal-padding ordering. The workflow: **`-Wpadded` to spot it, `pahole` to quantify it, `static_assert` to lock it.**

---

## 9.4 Techniques

### 9.4.1 Reordering members to eliminate padding

The free win from §9.2.1: **order members from largest/most-aligned to smallest.** Doubles and longs and pointers (8) first, then ints (4), then shorts (2), then chars/bools (1), then bit-fields. This packs the small members into what would otherwise be tail/inter-member padding, usually achieving the minimum size automatically. Re-run `pahole` to confirm; lock with `static_assert`.

Caveats: declaration order is also **readability and API**, so don't obfuscate a cold struct to save bytes nobody pays for (Ch. 1 tiering — this matters on hot structs in hot arrays, not on a config object instantiated once). And for **wire/overlay** formats (Ch. 53) you *can't* reorder freely — the layout is dictated by the protocol; there you use explicit field offsets and packing, accepting the constraints. The reorder technique is for your *internal* hot data types, where you own the layout.

### 9.4.2 Sizing a struct to a cache line

The goal for a hot array element: **make `sizeof` a clean divisor of 64** (or a small multiple) so elements pack tightly into lines with no element straddling a line boundary. A 16-byte element gives 4 per line, all aligned; a 24-byte element gives a fractional 2.67 per line, so some elements *cross* a cache-line boundary and cost two line fetches to read (Ch. 7). Two levers:

- **Shrink to fit:** reorder (§9.4.1), hot/cold split (Ch. 8), use smaller types (a scaled-integer price in `int32` instead of `double`? — Ch. 27), bit-fields (§9.2.4) — get the hot element to 16 or 32 bytes.
- **Pad up to a power-of-two-friendly size** when shrinking isn't possible and you want guaranteed no-straddle: `alignas` the element and let tail padding round it to 32, so it never crosses a line. You spend bytes to buy alignment determinism.

And the **deliberate over-align** case (§9.2.2): a per-thread counter or queue head written by one thread and read/written near another's data gets `alignas(64)` to claim its own line and dodge false sharing (Ch. 33) — here you *want* the padding. Sizing is two-directional: shrink the silent bloat, add the intentional isolation.

### 9.4.3 Packing trade-offs (`#pragma pack`) vs misaligned-access cost

`#pragma pack(1)` / `__attribute__((packed))` removes **all** padding, laying members back-to-back regardless of alignment. Tempting for shrinking a struct or matching a wire layout — but it makes members **misaligned**, and that has real costs:

- On **x86-64**, a misaligned *scalar* load/store is one instruction and usually cheap — the hardware handles it — *until* the access **crosses a cache-line (or page) boundary**, where it costs a second line fetch (and a page-table walk at a page edge). So packed scalar access degrades from "free" to "occasionally 2×" depending on where the object lands.
- **Vectorization is the real casualty** (see §9.5): the compiler often **refuses to auto-vectorize** a loop over packed structs because it can't assume element alignment — turning a wide SIMD reduction back into a scalar loop. On a hot aggregation, that's a far bigger loss than the padding you saved.
- **Atomics on a packed (misaligned) member are undefined / a "split lock"** — catastrophic (§9.6).
- On **ARM** (Appendix A) misaligned access can fault or trap-and-emulate — far worse than x86's tolerance.

So `pack` is the right tool for **wire/overlay structs you parse off the network** (Ch. 53), where the layout is fixed by the protocol and you typically copy fields out rather than vectorize over them — and the wrong tool for **internal hot compute structs**, where you want alignment for SIMD and atomics. Don't reach for `pack` to save footprint on a hot array; reorder and hot/cold-split instead.

---

## 9.5 Verify the codegen: aligned vs packed access

Read the asm (Ch. 4) and the trade-off becomes concrete. First, a single scalar load — the case people *worry* about, which turns out to be a non-event on x86. Aligned vs packed `long` member (verified, Clang `--target=x86_64-linux-gnu -O2`):

```cpp
struct Aligned { char c; long v; };                              // v at offset 8
struct __attribute__((packed)) Packed { char c; long v; };       // v at offset 1
long load_aligned(const Aligned* p) { return p->v; }
long load_packed (const Packed*  p) { return p->v; }
```

```asm
load_aligned(Aligned const*):
        mov     rax, qword ptr [rdi + 8]    ; one load, aligned offset
        ret
load_packed(Packed const*):
        mov     rax, qword ptr [rdi + 1]    ; ALSO one load — x86 tolerates misalignment
        ret
```

Identical instruction count — on x86 a misaligned *scalar* access is just a different offset. This is why "packing scalars is slow" is a myth *for isolated scalar access*; the penalty only appears on cache-line/page-crossing accesses, which the single-`mov` view hides.

The real cost shows in a **loop**. Summing a field across an array — aligned struct vs packed struct (verified, `-O2 -march=x86-64-v3`, AVX2):

```cpp
struct A { long v; long w; };                                    // 16B, aligned
struct __attribute__((packed)) P { char c; long v; long w; };    // 17B, packed/misaligned
long sumA(const A* a, unsigned long n){ long s=0; for(...) s += a[i].v; return s; }
long sumP(const P* p, unsigned long n){ long s=0; for(...) s += p[i].v; return s; }
```

`sumA` **vectorizes** — the inner loop is wide AVX2 (verified excerpt):

```asm
        vmovdqu ymm4, ymmword ptr [rax - 192]   ; 4x64-bit lanes loaded
        vpaddq  ...                             ; ... and added, 4-wide, unrolled
```

`sumP` **does not vectorize** — the compiler falls back to a scalar, stride-17 loop because it can't assume the packed elements are aligned for vector loads (verified excerpt):

```asm
.loop:
        ...
        add     rax, qword ptr [rcx]    ; scalar load of one v
        add     rcx, 17                 ; stride = sizeof(P) = 17 bytes, one element
        dec     rsi
        jne     .loop
```

There it is: the same reduction, **SIMD vs scalar**, decided purely by whether the element type was packed. On a hot aggregation that's a 4–8× throughput cliff — vastly more than the few padding bytes `pack` saved. **This is the §9.4.3 lesson, proven in asm: keep internal hot structs aligned (reorder/split for size); reserve `pack` for wire formats you don't vectorize over.** Always verify with `-Rpass=loop-vectorize` / `-fopt-info-vec` (Ch. 4) when you intend a loop to be SIMD.

---

## 9.6 Pitfalls & anti-patterns: misaligned atomics, false economy of packing

- **Misaligned atomics / split locks.** A `std::atomic` or `LOCK`-prefixed operation on a misaligned address (e.g. an atomic field inside a `packed` struct, or straddling a cache line) triggers a **split lock** — the CPU locks the *bus*/whole cache subsystem instead of one line, costing *hundreds to thousands* of cycles and stalling **other** cores. Modern kernels can even SIGBUS or rate-limit split-lock-detection. Atomics **must** be naturally aligned (and you want them cache-line-isolated anyway — Ch. 30, 33). Never `pack` a struct containing an atomic.
- **False economy of packing.** Using `#pragma pack` to shave a few bytes off an internal hot struct, then losing auto-vectorization (§9.5) and eating cache-line-split penalties — a net *loss*. Reorder and hot/cold-split for size instead; reserve packing for wire formats.
- **Silent padding bloat.** Adding a field (or declaring in a careless order) that pushes a hot array element across a cache-line size boundary — quietly raising the miss rate of the sweep that walks it. Defense: `-Wpadded` on the hot header and a `static_assert(sizeof)` that fails the build on regression (§9.3).
- **Assuming `sizeof` = sum of fields.** It almost never is. Always measure with `pahole`; don't compute footprint by adding field sizes.
- **Over-aligning everything.** `alignas(64)` on every struct "to be safe" wastes huge amounts of memory and cache (a 16-byte struct padded to 64 = 4× footprint, *worse* miss rate). Over-align *only* what needs SIMD alignment or false-sharing isolation, and measure the size cost.
- **`[[no_unique_address]]` assumptions / empty-member bloat.** Forgetting that a stateless policy stored as a plain member costs a byte + padding (defeating a "zero-cost" abstraction); or assuming `[[no_unique_address]]` *always* collapses to zero (it's "may" — two `[[no_unique_address]]` members of the *same* empty type can't both overlap the same address). Verify with `static_assert(sizeof)`.
- **Bit-field hazards.** Treating bit-fields as atomic or as a stable wire layout — both wrong (§9.2.4): concurrent writes to sibling bit-fields race (Ch. 33), and bit ordering is implementation-defined (use explicit masks/shifts for protocols — Ch. 28, 53).
- **Wire structs without `static_assert(offsetof/sizeof)`.** An overlay struct for a market-data message whose layout silently shifted across compilers/flags → you parse garbage. Pin every offset and the total size (Ch. 53).

---

## 9.7 Exercises & checklist

**Exercises**

1. **See the padding.** Define `struct Bad { char a; long b; char c; };`. Predict its `sizeof`, then check with `static_assert` and `pahole`. Reorder to minimize padding, confirm the new size, and explain the difference byte-by-byte (§9.2.1).
2. **Footprint → miss rate.** Make a hot struct 24 bytes, fill a large array, sum one field, and `perf stat` the L1 misses (Ch. 2). Reorder/shrink it to 16 bytes and re-measure. How did per-line element count and miss rate change? Tie to Ch. 7–8.
3. **Reproduce the vectorization cliff.** Build `sumA`/`sumP` from §9.5 at `-O2 -march=native`. Confirm via `-fopt-info-vec` (or reading the asm, Ch. 4) that the aligned one vectorizes and the packed one doesn't. Benchmark both (Ch. 3) — what's the throughput ratio?
4. **Scalar packed is (almost) free.** Show that a *single* packed-scalar load is one `mov` (§9.5), then construct a case where a packed access **crosses a cache line** and measure the penalty (place the field at offset 60 of a 64-byte-straddling object). When does packing actually cost on x86?
5. **Zero-cost policy.** Make a struct templated on an empty comparator stored as a plain member; check `sizeof`. Switch to `[[no_unique_address]]` (or EBO); check `sizeof` again. How many bytes did the abstraction cost before and after (§9.2.3)?

**Checklist — struct layout & alignment**

- [ ] I **measured** `sizeof`/`alignof`/layout with `pahole` (and `-Wpadded` on the hot header) — never assumed.
- [ ] Hot array elements are **ordered largest-to-smallest** to minimize padding, and sized to **pack cleanly into a cache line** (no straddling).
- [ ] I **`static_assert`** the size/alignment (and `offsetof` for overlay types) so regressions fail the build.
- [ ] I **over-align (`alignas(64)`) only** where needed — SIMD or false-sharing isolation (Ch. 29, 33) — and accounted for the size cost.
- [ ] No `#pragma pack`/`packed` on **internal hot** structs (it kills vectorization, §9.5, and risks split-lock atomics); packing reserved for **wire/overlay** formats (Ch. 53).
- [ ] Every **atomic** is naturally aligned and ideally cache-line-isolated (no split locks — §9.6, Ch. 30).
- [ ] Empty policies use **`[[no_unique_address]]`/EBO** so abstractions are zero-cost in `sizeof` (verified).
- [ ] Bit-fields used only to shrink **internal** structs where the mask/shift cost < the footprint win — never for atomicity or wire layout.

---

## 9.8 References

- *System V AMD64 ABI* and the **Itanium C++ ABI** — the authoritative rules for member layout, alignment, padding, tail padding, and EBO/`[[no_unique_address]]`.
- cppreference: *Object layout*, `alignas`/`alignof`, `[[no_unique_address]]`, `std::hardware_destructive_interference_size`, and bit-field rules.
- `pahole`/`dwarves` documentation (Arnaldo Carvalho de Melo) — DWARF-based struct layout inspection and `--reorganize`/`--reorder` (the §9.3 tool).
- Intel, *64 and IA-32 Architectures Optimization Reference Manual* — alignment and cache-line-split penalties, vector-load alignment, and split-lock cost; AMD's optimization guide for AMD parts.
- The Linux kernel "split lock detection" documentation — the real-world cost and handling of misaligned `LOCK` operations (§9.6).

## 9.9 Additional Reading

- E. Bendersky, *"The lost art of structure packing"* and similar write-ups — a practical tour of padding and reordering with `pahole`.
- CppCon talks on `[[no_unique_address]]` and EBO in the standard library (`std::vector`/`std::tuple` layout) — §9.2.3 in real library code.
- Ch. 33 (*False Sharing*) — the over-alignment side of this chapter, with `hardware_destructive_interference_size` and `perf c2c`; Ch. 53 (*Zero-Copy Wire Handling*) — packed overlay structs done safely; Ch. 28 (*Bit Manipulation*) — bit-field and mask/shift codegen.
- **Appendix A** — ARM/Graviton alignment and 128-byte cache-line differences and their effect on the sizing decisions here.

---

*Next: Ch. 10 — Software Prefetching & Non-Temporal Stores, the fallback for when layout and the hardware prefetcher still can't hide the miss: issuing `__builtin_prefetch` at the right distance, and using streaming stores to keep write-only data from polluting the cache you just worked to fit your hot set into.*
