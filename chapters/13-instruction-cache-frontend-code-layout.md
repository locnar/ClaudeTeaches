# Part II — CPU Microarchitecture

# Chapter 13 — Instruction Cache, Front-End Stalls & Code Layout

> **Prerequisites:** Ch. 12 (the out-of-order back-end this chapter *feeds* — the front-end's only job is to keep that engine supplied with µops), Ch. 8 (cache mechanics — the L1i and I-TLB are caches with the same line/associativity/miss rules as the data side), Ch. 2 (top-down — this is the **Front-End-Bound** quadrant), Ch. 4 (reading asm — used in §13.5).
>
> **Leads into:** Ch. 14 (branch prediction — the front-end *steers* on predictions; a mispredict is a front-end resteer), Ch. 15 (indirect calls — the hardest fetch-steering problem), Ch. 26 (the build toolchain — `-march`, LTO, PGO and BOLT, the heavy machinery that lays out code for I-cache locality). Hot-path warming (Ch. 59) includes warming the L1i and the branch predictors this chapter describes.

---

## 13.1 Why it matters: the front-end can starve a fast back-end

Chapter 12 left you with a back-end that can retire ~5 µops per cycle, keep hundreds of instructions in flight, and find parallelism you never wrote. But that engine doesn't generate its own work — it has to be *fed* a stream of decoded µops, and the machinery that fetches bytes from memory, decodes them into µops, and delivers them to the rename stage is the **front-end**. If the front-end can't keep up, the world's best out-of-order back-end sits idle. You can have a hot loop that is *perfectly* scheduled, branch-predictable, and cache-resident on the data side, and still run at half speed because the core keeps stalling waiting for *instructions*.

This is the blind spot in most performance work, because it's invisible in the source. The data side of caching (Ch. 8–11) is something you reason about constantly — you can *see* the array you're streaming. The instruction side is the bytes of your own compiled code, and nobody pictures their function bodies competing for L1 space. But they do: the L1 instruction cache (L1i) is only ~32 KB, the µop cache that bypasses decode is smaller still (~a few thousand µops), and a sprawling call tree — over-inlined templates, a giant `switch`, error-handling and logging code interleaved with the hot path — blows past those budgets and turns every iteration into an instruction-fetch miss.

The HFT relevance is direct. A tick-to-trade path is, by design, a *rare* code path that must be fast *the first time it runs after a quiet spell* — between market events the hot function isn't executing, so its bytes get evicted from L1i by housekeeping code, and the one message that matters arrives to a cold front-end (this is exactly what Ch. 59's warming attacks). And the structural enemy is **code bloat**: the natural C++ style for expressiveness — deep inlining, templates instantiated many ways, exceptions and logging inlined inline at every call — is precisely what bloats the hot path's instruction footprint. This chapter is about keeping the *instruction stream* as lean and locality-friendly as you already keep your data: measuring front-end stalls (§13.3), splitting cold code away from hot (§13.4.2), and letting the linker, PGO and BOLT pack the hot bytes together (§13.4.3–4).

The discipline, as always, is top-down (Ch. 2): this chapter is the **Front-End-Bound** quadrant — cycles where the back-end *could* retire µops but none were delivered. If your bottleneck is Core-Bound (Ch. 12) or Memory-Bound (Ch. 8–11), the techniques here do nothing. Confirm the signature first.

---

## 13.2 Mental model

### 13.2.1 L1i, the µop cache (DSB), I-TLB, fetch/decode bandwidth

The front-end is a small pipeline of its own, sitting in front of the rename stage that hands work to the back-end of Ch. 12:

```
   ┌────────────┐   ┌─────────┐   ┌──────────────┐   ┌───────────────┐
   │  L1i cache │──►│ predict │──►│   decode     │──►│   µop queue   │──► rename/back-end
   │  (~32 KB)  │   │ (BPU →  │   │ (x86 bytes → │   │  (IDQ) feeds  │     (Ch. 12)
   │  + I-TLB   │   │  next   │   │  µops, ~4-5  │   │  the back-end │
   │            │   │  fetch) │   │  wide)       │   │               │
   └────────────┘   └─────────┘   └──────┬───────┘   └───────────────┘
         ▲                                │
         │          ┌─────────────────────┴────────┐
         │          │  µop cache / DSB (~2.25K µops) │  ◄─ decoded µops cached;
   instruction      │  bypasses fetch+decode entirely│     "DSB hit" skips the
   bytes from L2/   └────────────────────────────────┘     expensive decode stage
   LLC/DRAM on miss
```

The pieces, and why each can stall:

- **L1i cache (~32 KB, 64-byte lines).** Holds instruction *bytes*. Same mechanics as the data L1 (Ch. 8): lines, sets, associativity, eviction. A miss fetches from L2 (~12+ cycles) or worse. Your total *hot code footprint* — every function actually executed in the loop, including everything inlined into it — competes for these 32 KB. Bloat = misses.
- **I-TLB.** Translates instruction virtual addresses to physical (Ch. 16). Small (tens to a few hundred entries). Code spread across many pages — a sprawling call graph jumping all over the binary — causes I-TLB misses, an under-appreciated cost that huge pages for the text segment (Ch. 16) can cut.
- **The µop cache (DSB, Decoded Stream Buffer).** x86's variable-length, hard-to-decode instructions are expensive to turn into µops. So the core *caches the decoded µops* of recently-run code in the DSB. A **DSB hit** delivers µops straight to the queue, skipping fetch+decode entirely — this is the fast path, and you want the hot loop to run almost entirely from the DSB. The DSB is small (Skylake ~1.5K, Ice Lake/Sunny Cove — the reference machine — ~2.25K, Golden Cove ~4K µops) and has alignment/packing rules (µops grouped by 32-byte instruction windows); code that's too large, too spread out, or badly aligned **falls out of the DSB** to the slower legacy decoders (the "MITE" path) — a measurable front-end penalty even on an L1i hit.
- **The legacy decoders (MITE) and the LSD.** On a DSB miss, bytes go through the legacy decode path (~4-5 instructions/cycle, with complex instructions slower). The **Loop Stream Detector (LSD)** can lock a tiny loop entirely inside the µop queue, replaying it with no fetch/decode at all — the very best case for a short hot loop.

The mental hierarchy of front-end "speed tiers" for a hot loop, fast to slow: **LSD-locked → DSB hit → L1i hit + legacy decode → L1i miss (fetch from L2/LLC/DRAM) → + I-TLB miss.** Your goal is to keep the hot path in the top tiers, and the structural lever is *footprint*: small, contiguous, well-aligned hot code.

### 13.2.2 Front-end-bound stalls in top-down analysis

In the top-down model (Ch. 2), every issue *slot* (the core has ~4–6 per cycle) is one of four things: **Retiring** (useful), **Bad Speculation** (mispredict — Ch. 14), **Back-End Bound** (Ch. 8–12, the back-end couldn't accept the µop), or **Front-End Bound** (the back-end *was ready* but the front-end *delivered no µop*). This chapter is that last bucket: idle slots caused not because the work was hard or the data was missing, but because the instruction supply ran dry.

`perf` breaks Front-End-Bound into two sub-categories, and the distinction tells you which technique to reach for:

- **Front-End Latency** — the front-end delivered *nothing* for a stretch: an L1i miss, an I-TLB miss, a branch resteer (the predictor pointed fetch the wrong way and had to restart), or a DSB-to-MITE switch. Symptom of *cold or scattered* code. Cured by reducing footprint and improving locality (§13.4.1–4).
- **Front-End Bandwidth** — the front-end delivered *some* µops but fewer than the back-end could take (e.g. ran out of the DSB and the legacy decoders can't keep the issue width fed, or fetch crossed too many cache lines). Symptom of *decode-unfriendly or DSB-spilling* code. Cured by shrinking/aligning the hot loop so it fits the DSB or LSD.

The key counters (§13.3): `idq_uops_not_delivered.*` (the canonical front-end-bound signal), `frontend_retired.*` (precise tagging of L1i miss, I-TLB miss, DSB miss as the stall cause), and the DSB-vs-MITE delivery mix (`idq.dsb_uops` vs `idq.mite_uops`). The fingerprint of a front-end problem — to contrast with Ch. 12's Core-Bound and Ch. 8's Memory-Bound — is **low IPC with high `idq_uops_not_delivered`, clean back-end (no memory stalls), clean branches**: the engine is starved, not stalled.

---

## 13.3 Measure it: front-end-bound counters; DSB coverage

The experiment: run the *same* per-message arithmetic two ways — once **lean** (one compact handler whose per-type constants are *data*, a table in L1d), once **bloated** (256 constant-specialised copies of that handler, one per message type, all of them executed — what over-inlining and per-type template instantiation produce). Same work, same data; the only difference is the **executed** instruction footprint: ~1 KB of loop vs ~45 KB of straight-line code that no longer fits Ice Lake's 32 KiB L1i or its ~2.25 K-µop DSB. (One thing this experiment deliberately does *not* do is inline *cold* code into the hot function: bytes that are never fetched or decoded cost nothing, and both GCC and Clang lay a never-taken arm out after the loop anyway — §13.4.2. Only code the front-end has to *deliver* counts.)

```cpp
// frontend.cpp — same arithmetic per message, two EXECUTED code footprints. Two builds:
//   lean : ONE generic handler; the per-type constants are DATA (a 256-entry table, 8 KiB, L1d)
//   bloat: 256 constant-specialised copies of the handler (one template instantiation per
//          message type, every one of them executed) — ~40 KiB of straight-line code
// Build: g++ -O2 -std=c++20 -march=native frontend.cpp -o frontend          # lean
//        g++ -O2 -std=c++20 -march=native -DBLOAT frontend.cpp -o frontend_bloat
// Run pinned, turbo off:  taskset -c 2 ./frontend
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <utility>
#include <vector>

struct Msg { std::uint32_t type; std::int64_t px; std::int32_t qty; };
struct Params { std::uint64_t k0, k1, k2, k3, k4, k5; };

constexpr Params params_for(std::uint32_t type) {          // per-type constants (splitmix64)
    std::uint64_t z = 0x9E3779B97F4A7C15ull * (type + 1);
    auto next = [&] { z += 0x9E3779B97F4A7C15ull; std::uint64_t x = z;
                      x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ull;
                      x = (x ^ (x >> 27)) * 0x94D049BB133111EBull; return (x ^ (x >> 31)) | 1; };
    return { next(), next(), next(), next(), next(), next() };
}

// The per-message work: ~45 instructions of multiply/xor/shift on THIS message only (no chain
// through acc), so the back-end can overlap messages — the front-end is the only limiter.
template <class P>
[[gnu::always_inline]] inline std::uint64_t handle(const Msg& m, const P& p) {
    std::uint64_t h = std::uint64_t(m.px) * p.k0 + std::uint64_t(std::uint32_t(m.qty)) * p.k1;
    h ^= h >> 29; h *= p.k2; h ^= h >> 32; h += p.k3;
    h ^= h >> 27; h *= p.k4; h ^= h >> 31; h += p.k5;
    h ^= h >> 29; h *= p.k0; h ^= h >> 32; h += p.k1;
    h ^= h >> 27; h *= p.k2; h ^= h >> 31; h += p.k3;
    return h;
}

#ifdef BLOAT
template <std::uint32_t T> struct Spec { static constexpr Params v = params_for(T); };  // immediates
template <std::size_t... I>
[[gnu::always_inline]] inline std::uint64_t block(const Msg* m, std::index_sequence<I...>) {
    return (... + handle(m[I], Spec<I>::v));           // 256 distinct, constant-specialised bodies
}
#else
constexpr auto kTable = [] {                            // the same constants, as DATA
    std::array<Params, 256> t{};
    for (std::uint32_t i = 0; i < 256; ++i) t[i] = params_for(i);
    return t;
}();
#endif

int main() {
    constexpr std::size_t N = 1024;                      // 24 KiB of messages: L1d — data is NOT the variable
    constexpr int REPS = 200'000;                        // 200 M messages
    std::vector<Msg> in(N);
    for (std::size_t i = 0; i < N; ++i)
        in[i] = { std::uint32_t(i & 255), std::int64_t(100000 + i), std::int32_t(1 + (i & 15)) };

    std::uint64_t acc = 0;
    auto t0 = std::chrono::steady_clock::now();
    for (int r = 0; r < REPS; ++r) {
        for (std::size_t i = 0; i < N; i += 256)
#ifdef BLOAT
            acc += block(&in[i], std::make_index_sequence<256>{});
#else
            for (std::size_t j = 0; j < 256; ++j) acc += handle(in[i + j], kTable[j]);
#endif
        asm volatile("" : "+r"(acc) :: "memory");        // Appendix L: keep every rep honest
    }
    auto t1 = std::chrono::steady_clock::now();
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count();
    std::printf("acc=%llu  %.2f ns/msg\n", (unsigned long long)acc, ns / (double(REPS) * N));
    return 0;
}
```

Profile both with the front-end counters (names are Intel Ice Lake; adjust per `perf list`):

```
perf stat -e cycles,instructions,\
idq_uops_not_delivered.core,\
idq.dsb_uops,idq.mite_uops,\
frontend_retired.l1i_miss,frontend_retired.itlb_miss,\
L1-dcache-load-misses,branch-misses  ./frontend
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP: 32 KiB L1i, ~2.25 K-µop DSB, 16 B/cycle legacy fetch), GCC 13 `-O2 -march=native`, Linux 6.18, pinned, turbo off (illustrative; the *direction* is the point):

```
                                   lean (1 handler, table)   bloat (256 specialised copies)
ns / message                          ~3.5 ns                   ~7 ns        <- ~2x slower
instructions / message                ~50 (incl. 6 table loads) ~45 (constants are immediates)
IPC                                   ~4.5                      ~2.2
idq_uops_not_delivered.core           low                       HIGH         <- front-end starved
idq.dsb_uops  : idq.mite_uops         ~100% DSB                 ~0% DSB      <- 45 KB >> the µop cache: legacy decode
frontend_retired.l1i_miss             ~0                        ~3 / message <- 45 KB cycled through a 32 KiB L1i
L1-dcache-load-misses                 ~0 (same data)            ~0 (same)    <- DATA not the cause
branch-misses                         ~0 (same)                 ~0 (same)    <- branches not the cause
```

Read it the Ch. 2 way: **same data, same data-cache behavior, same branch behavior, *fewer* instructions — and 2× slower.** Every message does the identical multiply/xor/shift work. In the `bloat` build those ~45 instructions arrive as ~180 bytes of straight-line code per message (10-byte `movabs` immediates instead of 4-byte table loads), the 256 bodies total ~45 KB, and the loop cycles through all of them: nothing stays in the DSB, so every µop comes from the legacy decoder at ≤ 16 bytes/cycle — ~11 cycles of fetch for ~9 cycles of back-end work — and the L1i evicts each body before its next use. That is the Front-End-Bound signature: **low IPC, high `idq_uops_not_delivered`, DSB→MITE spill, L1i misses, clean memory, clean branches.** When you see it, the problem is your *code's* footprint, not your data's. (Scale check: a core with a 64 KiB L1i and a 4 K-µop DSB — Golden Cove / Sapphire Rapids — swallows this footprint; on such a box raise the type count until the executed code exceeds *its* L1i before expecting the gap. Compile time grows with the count: 1 024 types is ~40 s with GCC 13.)

---

## 13.4 Techniques

### 13.4.1 Taming code bloat from over-inlining

Inlining is the optimizer's most valuable transformation — it exposes the back-end parallelism of Ch. 12, kills call overhead, and enables constant propagation. But it is not free: every inline *copies* the callee's bytes into the caller, and aggressive inlining of large or rarely-needed callees inflates the hot path's L1i/DSB footprint. The goal is **inline the small and hot; never inline the large and cold.**

Practical levers:

- **Let small leaf functions inline; stop large ones.** Mark a genuinely-rare, bulky helper `[[gnu::noinline]]` (or `__attribute__((noinline))`) so its bytes stay *out* of the hot function. Counter-intuitively, *preventing* an inline can speed the hot path up by keeping it in the DSB (§13.3).
- **Watch template instantiation bloat.** A function template instantiated for many types generates many near-identical bodies; a `std::function`/lambda-heavy pipeline can inline a surprising amount. Measure footprint (`nm --size-sort`, `bloaty`, map files) when a hot path crosses many templates.
- **Tune the inliner, don't fight it blindly.** `-finline-limit=`, `__attribute__((flatten))` (inline *everything* into this function — usually the *wrong* tool for a hot loop with cold branches), and per-function `[[gnu::hot]]`/`[[gnu::cold]]` hints. The reliable signal is the front-end counters of §13.3, not intuition.
- **Prefer outlining the slow path over guarding it inline.** The single most effective move is §13.4.2: keep the hot function tiny by *calling out* to cold code, never *containing* it.

### 13.4.2 Hot/cold code splitting; `[[likely]]`/`[[unlikely]]`, `__builtin_expect`

This is the workhorse technique and the direct cure for §13.3's bloat. The principle: **a hot function should contain only the bytes that run on the common path; everything that runs rarely lives elsewhere in the binary so it never occupies the hot path's L1i/DSB.**

The mechanism is a partnership between you and the compiler:

- **Tell the compiler which way a branch goes.** `[[likely]]`/`[[unlikely]]` (C++20) on the branch, or `__builtin_expect(cond, 0)` (C/older), mark the rare arm. The compiler then reorders blocks so the unlikely arm leaves the fall-through path (it lands after the function's `ret`, still in `.text`) and the hot path is a straight, contiguous run of bytes with no cold code interleaved. By itself the hint does *not* move the arm to `.text.unlikely`: without profile data GCC partitions a block into the `.cold` section only when it can prove it cold — in practice when the arm calls a `[[gnu::cold]]` function (which is why the §13.5 example works), and Clang likewise needs `[[gnu::cold]]` or PGO (checked with GCC 13 and Clang 18 at `-O2`).

  ```cpp
  if (seq != expected_seq) [[unlikely]]      // gap detected — rare
      return handle_gap(seq);                // cold: do NOT inline this
  process(msg);                              // hot fall-through stays contiguous
  ```

- **Attribute whole cold functions.** `[[gnu::cold]]` (and `[[gnu::noinline]]`) on error/recovery/logging functions tells the compiler to place them in the **`.text.unlikely`** section, clustered away from hot code (verified in §13.5). `[[gnu::hot]]` marks the counterparts. Whole error-handling subsystems — gap recovery, reject formatting, slow-path logging (Ch. 91) — should be `cold` and out-of-line by default.
- **Keep the hot function a "dispatcher."** The hot path does the common work and *branches out* (a call to a cold function) for anything unusual. The order-book fast path (Ch. 31) handles the common add/cancel/modify inline and calls out for the rare cases (book reset, gap recovery, auction transitions).

The payoff is exactly §13.3 in reverse: contiguous hot bytes stay resident in L1i and the DSB, the common path runs from the fast front-end tier, and the kilobytes of cold code — which you provably almost never execute — sit harmlessly in `.text.unlikely`, paged in only when actually needed.

A caution that bridges to Ch. 14: `[[likely]]`/`[[unlikely]]` are **layout** hints (where the code goes), not a substitute for the hardware branch predictor (which way fetch *steers*). They help the front-end by making the hot path contiguous; they do **not** improve a genuinely data-dependent, unpredictable branch — that's Ch. 14's problem, and mis-hinting a 50/50 branch can hurt. Use them where the bias is real and overwhelming (errors, rare recovery), and *measure*.

### 13.4.3 Function/section placement and `-freorder-blocks-and-partition`

Hot/cold splitting is most powerful when the compiler can also reorganize code *within* and *across* functions automatically:

- **`-freorder-blocks-and-partition`** (GCC; on at `-O2`/`-O3` for many targets) splits each function's basic blocks into hot and cold partitions, emitting the cold blocks into `.text.unlikely`. This is the engine that makes `[[unlikely]]`/`[[gnu::cold]]` produce the physical separation of §13.5. Confirm it's active for your target; it's the single flag most responsible for keeping unlikely arms off the hot path.
- **`-falign-functions`/`-falign-loops`** align hot entry points and loop tops to (typically) 16/32-byte boundaries so a hot loop sits cleanly within DSB/fetch windows rather than straddling a boundary and losing front-end bandwidth (§13.2.1). Over-alignment wastes I-cache, so this is a tune-and-measure knob, not "more is better." One alignment hazard is specific to the Skylake family (Skylake-SP, Cascade Lake, Comet Lake — still common in colos): after the 2019 **JCC erratum** microcode update, any jump that touches or crosses a 32-byte boundary cannot be cached in the DSB, so a hot loop can fall to MITE despite an L1i hit (symptom: `idq.mite_uops` rising for no other reason). The assembler mitigation is `-Wa,-mbranches-within-32B-boundaries` (GCC/binutils 2.34+; Clang `-mbranches-within-32B-boundaries`), which pads so no jump straddles a boundary at a small code-size cost. Ice Lake and later are unaffected.
- **Section/placement attributes.** `[[gnu::section(".text.hot")]]` and the linker's ability to group sections let you cluster all genuinely-hot functions into a contiguous region — fewer L1i lines and I-TLB pages spanned by the working set. Putting the hot text on **huge pages** (Ch. 16) cuts I-TLB pressure for a large hot footprint.

The theme: the *spatial* layout of your compiled code in the binary is a performance parameter, exactly as data layout is (Ch. 9–10). The default compiler heuristics are decent; the flags here, and the profile-driven tools next, make the layout *match your actual execution profile*.

### 13.4.4 Linker ordering, PGO and BOLT for I-cache locality

The compiler lays out code with *static heuristics* — guesses about which paths are hot. **Profile-guided** tools replace the guess with measured truth, and front-end-bound code (the §13.3 signature) is their prime beneficiary: an accurate profile gives the compiler exact hot/cold splitting (§13.4.2) and inlining decisions (§13.4.1) *everywhere*, not only where you remembered to annotate, and a post-link optimizer straightens the final layout the fetch unit actually sees. The mechanics — the three-step **PGO** build (`-fprofile-generate`/`-fprofile-use`), **AutoFDO** from production `perf` samples, **BOLT** on the linked binary, how they stack with LTO, and the representative-profile caveat — are **Ch. 26 §26.4.3**; what is specific to this chapter is the layout lever and how to check the result:

- **Linker ordering (`--symbol-ordering-file`, `-ffunction-sections`).** With each function in its own section, the linker can place functions in a profile-derived order so that call-related hot functions are *adjacent* in the binary — co-locating them in the same L1i lines and I-TLB pages. PGO can emit such an ordering automatically.
- **Re-check the front-end counters afterwards.** PGO/BOLT is a layout change, so verify it the §13.3 way: DSB coverage (`idq.dsb_uops` vs `idq.mite_uops`), `idq_uops_not_delivered.core`, `frontend_retired.itlb_miss` and the top-down Front-End-Bound share should all fall; if they don't, the profile wasn't representative (Ch. 26 §26.4.3's catch — and one more reason to capture production traffic, Ch. 101). When inlining discipline and hot/cold attributes have done what they can and you're still Front-End-Bound on a large binary, this is the next lever.

---

## 13.5 Verify the codegen: hot/cold section placement

*Toolchain and machine: Appendix L.3.*

The hot/cold split of §13.4.2 is visible — and verifiable — in the asm and the section table. Take the dispatcher pattern:

```cpp
[[gnu::cold, gnu::noinline]] std::int64_t handle_gap(std::uint32_t seq);

std::int64_t process(const Msg& m, std::uint32_t expected, std::int64_t acc) {
    if (m.type != expected) [[unlikely]]
        return handle_gap(m.type);          // cold arm
    return acc + m.px * m.qty;              // hot fall-through
}
```

With `-O2 -freorder-blocks-and-partition`, the hot function body is a straight run that *falls through* on the common case and only *jumps away* for the rare one — the cold arm is not interleaved into the hot bytes:

```asm
process(Msg const&, unsigned int, long):        ; hot body in .text
        mov     eax, dword ptr [rdi]            ; m.type
        cmp     eax, esi                        ; m.type != expected ?
        jne     .Lcold                          ; rare → jump OUT of the hot stream
        movsxd  rax, dword ptr [rdi + 16]       ; hot fall-through: m.qty, sign-extended 32->64
        imul    rax, qword ptr [rdi + 8]        ;   * m.px   (qty is int32 — a qword load here would
                                                ;    multiply in the 4 bytes of tail padding too)
        add     rax, rdx                        ;   + acc
        ret                                     ; hot path: contiguous, no cold bytes
.Lcold:                                         ; emitted into .text.unlikely (cold region)
        mov     edi, eax
        jmp     handle_gap(unsigned int)        ; tail-call the cold, out-of-line handler
```

Two things to confirm, both the *point* of the chapter:

1. **The hot path is contiguous.** The bytes that run on the common case (`mov/imul/add/ret`) are a straight sequence with no cold code between them — they pack densely into L1i lines and the DSB. The unlikely arm is a single forward `jne` to `.Lcold`, which the static predictor treats as not-taken (Ch. 14), and whose target lives elsewhere.

2. **The cold code is in a different section.** Check the section table of the *object file* — the cold body (`process.cold`) and `handle_gap` land in **`.text.unlikely`**, physically separated from `.text` (a `.text.hot` section appears only when something is marked `[[gnu::hot]]` or a PGO profile says so):

   ```
   $ g++ -O2 -std=c++20 -c process.cpp && readelf -SW process.o | grep -E '\.text'
     [ 1] .text             PROGBITS  ... AX
     [ 5] .text.unlikely    PROGBITS  ... AX      <- cold arms (process.cold) + [[gnu::cold]] fns here
   $ nm -C a.out | grep -E 'process|handle_gap'       # linked binary: cold pieces sit at distant addresses
   ```

   In the linked executable GNU ld's default script folds the `.text.unlikely` (and `.text.hot`) *input* sections into the single `.text` *output* section — cold input first, hot after — so `readelf -S a.out` shows only `.text`; the separation survives as address distance, which is what `nm` shows.

   That separation is what keeps the kilobytes of `handle_gap` (and every other cold handler) out of the hot path's L1i working set. Without `[[gnu::cold]]` the cold body stays in `.text` beside `process` — after its `ret` if the branch is hinted `[[unlikely]]`, interleaved with the hot bytes if it is not — and in the interleaved case the front-end fetches and decodes the cold bytes it then jumps over, wasting fetch bandwidth and 32-byte DSB windows. (§13.3's bigger lesson still holds: bytes the front-end never has to *deliver* cost nothing; it is the executed footprint that starves the back-end.)

The verification habit: for a hot function, **disassemble it and confirm the common path is a contiguous fall-through with rare arms tail-jumping to `.text.unlikely`** — and check the section table to confirm cold handlers actually moved. If you see large rarely-run bodies inlined into the hot function, that's footprint you're paying for on every iteration.

---

## 13.6 Pitfalls & anti-patterns: inlining everything; cold code on the hot path

- **Inlining everything (`flatten`, force-inline-all).** The cardinal front-end sin: pulling large, rarely-run callees into the hot function bloats its L1i/DSB footprint and starves the back-end (§13.3). Inline the small and hot; outline the large and cold. More inlining is *not* monotonically faster.
- **Cold code interleaved in the hot path.** Error formatting, logging (Ch. 91), gap recovery, assertions-with-messages, or a giant `default:` arm sitting *inline* in the hot loop. Even if never executed, its *bytes* fragment the hot loop across more 64-byte fetch lines and 32-byte DSB windows — the L1i and DSB hold only what is fetched and decoded, so cold code hurts when it is interleaved *within* the hot path's lines, not when it sits after the `ret` or in `.text.unlikely`. Mark it `[[unlikely]]`/`[[gnu::cold]]` and `noinline` so it moves to `.text.unlikely` (§13.4.2, §13.5).
- **Mis-hinting `[[likely]]`/`[[unlikely]]`.** These are *layout* hints, not magic. Hinting a genuinely 50/50 or data-dependent branch lays out the wrong path as hot and can *slow* it, and it does nothing for the hardware predictor's mispredict cost (that's Ch. 14). Use them only where the bias is overwhelming and real, and verify with `frontend_retired`/branch counters.
- **Optimizing the front-end when you're not Front-End-Bound.** If top-down (Ch. 2) says Memory-Bound (Ch. 8) or Core-Bound (Ch. 12), shrinking code footprint does nothing. Confirm the `idq_uops_not_delivered`/DSB-miss signature *first*.
- **Ignoring the DSB and alignment.** A hot loop that just barely spills the µop cache or straddles a fetch boundary loses front-end bandwidth despite an L1i hit. Watch `idq.dsb_uops : idq.mite_uops`; consider `-falign-loops` and shrinking the loop body — but measure, since over-alignment wastes I-cache.
- **PGO/BOLT with an unrepresentative profile.** Profile-guided layout is only as good as the workload you profiled. A profile from synthetic or idle traffic mis-marks hot/cold and can *pessimize* the real hot path. Profile with representative captured traffic (Ch. 101).
- **Forgetting the cold-start tail (Ch. 59).** A lean, well-laid-out hot path *still* starts cold after a quiet period — its bytes evicted from L1i, its branches unpredicted. Good layout shrinks the warm working set (so warming is cheaper and eviction slower) but doesn't replace warming. The two are complementary.
- **Template/`std::function` footprint creep.** Heavy generic pipelines silently multiply code size; a hot path threading many instantiations can bloat past the DSB without any single obvious culprit. Measure code size (`nm --size-sort`, `bloaty`) when a generic hot path goes front-end-bound.

---

## 13.7 Exercises & checklist

**Exercises**

1. **Measure the bloat.** Build `frontend.cpp` lean and with `-DBLOAT`, run both pinned/turbo-off, and `perf stat -e cycles,instructions,idq_uops_not_delivered.core,idq.dsb_uops,idq.mite_uops,frontend_retired.l1i_miss,L1-dcache-load-misses,branch-misses`. Confirm: same data-cache and branch behavior, but the bloat build is Front-End-Bound (high `idq_uops_not_delivered`, DSB→MITE). What's the IPC of each?
2. **Verify the section split.** Compile the §13.5 dispatcher with `-O2 -freorder-blocks-and-partition`. Disassemble `process` (Ch. 4) and confirm the hot path is a contiguous fall-through with the rare arm tail-jumping out. Run `readelf -S` / `objdump -d --section=.text.unlikely` and confirm `handle_gap` landed in `.text.unlikely`. Now remove `[[unlikely]]`/`[[gnu::cold]]` — where does the cold body go?
3. **DSB coverage of a loop.** Take a hot loop and grow its body (add inlined helpers) until `idq.mite_uops` overtakes `idq.dsb_uops`. Find the size where it spills the µop cache. Does `-falign-loops=32` change the threshold?
4. **PGO the dispatcher.** Build a small message-dispatch program instrumented (`-fprofile-generate`), replay a representative message mix, rebuild `-fprofile-use`. Diff the section placement and the front-end counters vs the non-PGO build. Did hot functions cluster?
5. **Cold-start tail (preview of Ch. 59).** Measure the latency of the *first* hot-path message after sleeping the thread for 100 ms *while a housekeeping thread with a large code footprint runs on the same core* (sleeping alone evicts nothing from an isolated core's L1i; on a non-isolated core with deep C-states the housekeeping is free) vs the steady-state latency — confirm the eviction with `frontend_retired.l1i_miss` on the first message. Then measure again on the bloat build. How does footprint affect the cold tail?

**Checklist — front-end & code layout**

- [ ] I confirmed (top-down, Ch. 2) the hot path is **Front-End-Bound** — high `idq_uops_not_delivered`, **clean back-end and branches** — before reducing footprint.
- [ ] The hot function contains **only common-path bytes**; rare work (errors, recovery, logging) is **`[[gnu::cold]]`/`noinline`** and out of line (`.text.unlikely`).
- [ ] Unlikely branches are marked `[[unlikely]]`/`__builtin_expect` where the bias is **real and overwhelming** — and I verified the layout, not just added the hint.
- [ ] I checked the hot loop runs **mostly from the DSB** (`idq.dsb_uops` ≫ `idq.mite_uops`) and isn't spilling the µop cache; considered `-falign-loops`/`-falign-functions`.
- [ ] I verified in the **asm + section table** (Ch. 4) that the hot path is contiguous and cold handlers actually moved to `.text.unlikely`.
- [ ] For a large binary still front-end-bound, I applied **PGO (and BOLT)** with a **representative captured profile** (Ch. 101), not synthetic traffic.
- [ ] I didn't over-inline (`flatten`/force-inline-all) large cold callees, and I measured **code size** when a generic/template-heavy hot path went front-end-bound.
- [ ] I remembered layout shrinks but doesn't eliminate the **cold-start tail** — warming (Ch. 59) is still needed.

---

## 13.8 References

- Intel, *64 and IA-32 Architectures Optimization Reference Manual* — the front-end pipeline: L1i, the DSB/µop cache and its packing rules, the LSD, legacy decoders, and the `idq_uops_not_delivered` / DSB-coverage performance events used in §13.3.
- A. Yasin, *"A Top-Down Method for Performance Analysis"* — the Front-End-Bound bucket and its Latency-vs-Bandwidth split that frame this whole chapter (Ch. 2).
- A. Fog, *The Microarchitecture of Intel, AMD and VIA CPUs* — per-microarchitecture front-end widths, µop-cache sizes, fetch/decode behavior and alignment effects.
- M. Panchenko et al., *"BOLT: A Practical Binary Optimizer for Data Centers and Beyond"* — the rationale and gains of post-link code layout for I-cache/I-TLB locality (§13.4.4).
- GCC/Clang documentation — `-freorder-blocks-and-partition`, `-falign-functions`/`-falign-loops`, `-ffunction-sections`, function attributes `hot`/`cold`/`noinline`, and PGO (`-fprofile-generate`/`-fprofile-use`).

## 13.9 Additional Reading

- D. Bakhvalov, *Performance Analysis and Tuning on Modern CPUs* — practical chapters on Front-End-Bound analysis, DSB coverage, and code-layout optimization with PGO/BOLT.
- The Facebook/Meta BOLT and Google AutoFDO/Propeller writeups — real-world profile-guided layout on large, front-end-bound services.
- Ch. 14 (*Branch Prediction*) — the predictor that steers fetch and the mispredict resteer that this chapter's layout cooperates with; Ch. 16 (*TLB & Huge Pages*) — huge pages for the text segment to cut I-TLB pressure; Ch. 26 (*Build Toolchain*) — LTO/PGO/BOLT in full; Ch. 59 (*Keeping the Hot Path Warm*) — warming the L1i/predictors this chapter sizes.
- **Appendix E** — the L1i-miss / I-cache-fetch and branch-resteer costs that frame "front-end-bound."

---

*Next: Ch. 14 — Branch Prediction & Branchless Programming, where we follow the front-end's *steering* decision to its source: how the predictor guesses branch direction and target, what a mispredict actually costs in flushed work, and how `cmov`, lookup tables and branchless reformulations remove unpredictable branches from the hot path entirely.*
