# Part III — Compile-Time & Language Mechanics

# Chapter 20 — Compile-Time Mechanics

> **Prerequisites:** Ch. 4 (reading asm — the proof that a `constexpr` computation left *no* runtime instructions behind), Ch. 1 (the latency mindset — "free at runtime" is the whole motivation), Ch. 13 (code bloat — compile-time techniques can *generate* runtime code, so the I-cache caveat applies). Light C++20 assumed (the audience's baseline per the book's framing).
>
> **Leads into:** Ch. 21 (template metaprogramming & zero-cost abstractions — CRTP, policy-based design, expression templates built on this foundation), Ch. 14–15 (the lookup tables and dispatch this chapter can *generate* at compile time), Ch. 34–35 (compile-time numeric tables — tick maps, bit tables). Opens **Part III**.

---

## 20.1 Why it matters: work done at compile time is free at runtime

Every cycle you spend at *compile* time is a cycle you don't spend on the *hot path*. That is the entire thesis of compile-time programming, and it's the most absolute optimization in this book: not "faster," but **gone** — a value computed by the compiler leaves behind a literal in the binary and *zero* runtime instructions, no cache footprint for the computation, no branches, nothing to mispredict. Where Part II fought to make runtime work cheaper, Part III asks the prior question: *does this need to run at runtime at all?* For a tick-to-trade path measured in nanoseconds, the answer is often no — the tick-size table, the protocol field offsets, the CRC lookup table, the dispatch structure, the validation bounds are all *known when you compile*, and computing them then costs the hot path nothing.

C++ has quietly become a powerful compile-time language. `constexpr` (C++11, vastly expanded through C++14/17/20) lets ordinary-looking functions run *either* at compile time or runtime; `consteval` (C++20) *forces* compile-time evaluation; and C++20 allows `constexpr` containers (`std::vector`, `std::string` with transient allocation), algorithms, and far more, so you can write a normal-looking function that builds a lookup table, parses a format descriptor, or computes a perfect hash — and have it *all* happen during the build, materializing as a `static constexpr` array in `.rodata`. The HFT payoff is direct: a feed handler that indexes a compile-time-generated table instead of branching (Ch. 14), an order book sized by compile-time constants, a SBE/protocol decoder whose field layout is resolved at compile time (Ch. 67) — work moved off the nanosecond-critical path into the build, where a few extra seconds of compilation are free.

But compile-time work is not *unconditionally* free, and this chapter is as much about the *costs* as the wins. Templates and heavy `constexpr` can **blow up build times** (which slow the edit-measure-iterate loop this book depends on — Ch. 3), and template instantiation can **generate** large amounts of runtime code (Ch. 13's bloat — a compile-time technique with a runtime I-cache cost). The discipline is to know *which* work genuinely moves to compile time and vanishes, versus which merely *generates* runtime code, and to verify the difference in the asm (Ch. 4). Done right, compile-time mechanics are the cleanest latency win available — work that simply isn't there when the market opens.

---

## 20.2 Mental model: `constexpr` vs `consteval`; the template cost model

Two distinct mechanisms move work to compile time, and it's worth being precise about what each guarantees:

**`constexpr` — *may* run at compile time.** A `constexpr` function/variable is *eligible* for compile-time evaluation but the compiler runs it at compile time only when it *must* (in a constant-expression context — array bounds, template args, a `constexpr`/`static constexpr` initializer, a `case` label) or chooses to. Called with runtime arguments in a runtime context, the *same* function runs at runtime. So `constexpr` is "compute at compile time *if possible*." To **guarantee** compile-time evaluation you must force the context:

```cpp
constexpr int crc_poly = compute_poly(0x04C11DB7);   // initializer of a constexpr → at compile time
static constexpr auto kTable = build_table();         // static constexpr → materialized in .rodata
constexpr int n = factorial(10);                      // const-expr context → forced
int m = factorial(x);                                 // x runtime → runs at RUNTIME (same function)
```

**`consteval` — *must* run at compile time.** A `consteval` function (an "immediate function," C++20) is evaluated at compile time on *every* call; calling it with a non-constant argument is a **compile error**. Use `consteval` when running at runtime would be a *bug* — e.g. a function that must only ever produce a compile-time constant (a parsed format string, a validated config). It removes the "did this actually evaluate at compile time?" doubt that plagues `constexpr`:

```cpp
consteval std::uint32_t field_offset(Field f) { /* ... */ }   // ERROR if f isn't constant
std::uint32_t o = field_offset(Field::Price);                 // guaranteed compile-time
```

Supporting cast worth knowing: **`constinit`** guarantees a variable is *initialized* at compile time (no static-init-order/runtime-init cost) without making it `const`; **`if constexpr`** selects branches at compile time (inside a template the discarded branch is not even *instantiated*, and it emits no code either way — the basis of static dispatch, Ch. 21; outside a template both branches are still checked for well-formedness); and the language kept widening what `constexpr` may do: **C++14** relaxed it to allow loops, local variables and mutation (N3652), **C++17** made `std::array` indexing writable in constant evaluation, and **C++20** expanded it further to most of `<algorithm>`/`<numeric>`, `std::vector`/`std::string` with *transient* allocation (allocated and freed within the same constant evaluation), virtual calls and `try` — so building a table with a normal loop-and-push is legal at compile time, and has been since C++14 for a plain array.

**The template cost model.** Templates are the *other* compile-time mechanism — they generate code by instantiation. The model to carry:

- **Each distinct instantiation generates a separate body.** `process<ITCH>` and `process<FIX>` are different functions in the binary. This is *good* for specialization (each is optimized for its type — zero-overhead, Ch. 21) and *bad* for bloat (N instantiations = N copies of code — Ch. 13's I-cache problem).
- **Instantiation costs *build* time, not run time.** Heavy metaprogramming (deep recursion, large parameter packs, SFINAE overload sets) makes the *compiler* work hard; the runtime cost is whatever code it ultimately emits. Slow builds are a real tax (Ch. 3's iterate loop), even when the runtime is perfect.
- **Compile-time computation and templates compose.** `if constexpr`, `constexpr` functions, and templates together let you do arbitrary computation and code selection at build time — generating tables, specializing dispatch, unrolling by a compile-time count.

The unifying model: **`constexpr`/`consteval` move *values and computation* to compile time (leaving literals, no runtime code); templates move *code generation* to compile time (leaving specialized runtime code, one body per instantiation).** The first is free at runtime by construction; the second trades build time and code size for specialization. Both are verified the same way — read the asm (Ch. 4) and the build profile.

---

## 20.3 Measure it: build-time vs run-time trade-off

*Toolchain and machine: Appendix L.3.*

The measurement has *two* axes, because compile-time programming trades one resource for another: **runtime** (should drop to nothing for genuinely compile-time work) and **build time + code size** (the price). Demonstrate both on a lookup table — a CRC/parity-style table — built two ways: (a) computed at **runtime** on startup, (b) built at **compile time** via `constexpr` (whose codegen should be indistinguishable from a hand-written literal array).

```cpp
// ct_table.cpp — a 256-entry table, runtime-built vs constexpr-built.
// Build: g++ -O2 -std=c++20 -march=native ct_table.cpp -o ct_table
//   Compare build time:  /usr/bin/time -v g++ ... ; and code size:  size ct_table
// Run pinned:  taskset -c 2 ./ct_table
#include <array>
#include <cstdint>
#include <cstdio>
#include <chrono>

// A normal function, constexpr: runs at compile time in a constexpr context, runtime otherwise.
constexpr std::array<std::uint8_t, 256> build_parity() {
    std::array<std::uint8_t, 256> t{};
    for (int i = 0; i < 256; ++i) {              // loops in constexpr: C++14; writable array[]: C++17
        int p = 0, v = i;
        while (v) { p ^= 1; v &= v - 1; }        // popcount parity (Ch. 35)
        t[i] = std::uint8_t(p);
    }
    return t;
}

static constexpr auto kParity = build_parity();  // (b) BUILT AT COMPILE TIME → lives in .rodata

std::array<std::uint8_t, 256> runtime_parity() { // (a) the SAME work, but at startup
    std::array<std::uint8_t, 256> t{};
    for (int i = 0; i < 256; ++i) { int p=0,v=i; while(v){p^=1; v&=v-1;} t[i]=std::uint8_t(p); }
    return t;
}

int main(int argc, char**) {
    // Use kParity on the hot path: it's already in the binary, zero setup.
    auto rt = runtime_parity();                  // pay this cost at startup (cold path)
    std::uint64_t acc = 0;
    constexpr int REPS = 50'000'000;
    auto t0 = std::chrono::steady_clock::now();
    for (int i = 0; i < REPS; ++i) acc += kParity[(acc + i) & 0xFF];   // hot path: just an indexed load
    auto t1 = std::chrono::steady_clock::now();
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
    std::printf("acc=%llu rt[7]=%u  %.3f ns/lookup\n",
                (unsigned long long)acc, rt[7], ns / REPS);
    return 0;
}
```

Two measurements, not one. Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP, 2.9 GHz), GCC 13 `-O2 -std=c++20 -march=native`, Linux 6.18, pinned (`taskset -c 2`), turbo off (illustrative):

```
RUNTIME (the hot-path lookup itself):
   kParity (constexpr) lookup        ~2.5 ns/lookup   <- one L1 load from .rodata (the loop is deliberately
                                                       latency-bound: lea+movzx+5-cycle load+add ≈ 8 cyc); setup was FREE
   runtime_parity()  build (startup)  ~hundreds of ns  <- paid once, but on the (cold) startup path

BUILD-TIME / SIZE (the price of (b)):
   $ size ct_table
      text    data     bss        <- kParity's 256 bytes appear in .rodata (text/rodata), no init code
   build time: a few ms more for the constexpr evaluation (negligible here; can balloon — §20.5)
```

The lessons:

- **The compile-time table costs the hot path *nothing*** — `kParity` is already bytes in `.rodata`; the lookup is a single L1 load (Ch. 8), and the *construction* work (the popcount loop, ×256) executed in the compiler, not the program. The `runtime_parity()` version does the identical work but pays it at startup (acceptable if it's truly one-time setup, Ch. 1's hot/cold split — but it *is* paid, and it's code in the binary).
- **The price shows up on the *other* axis** — `kParity`'s bytes are in the binary's `.rodata` (data/size), and the `constexpr` evaluation cost a little build time. For a 256-byte table that's nothing; for a megabyte table or a deeply recursive metaprogram it can dominate build time and bloat the binary (§20.5).
- **Verify it actually vanished (Ch. 4).** Disassemble `main`: the lookup should be a `movzx` from a fixed `.rodata` address with **no construction code** for `kParity`. GCC 13 `-O2` (verified) gives exactly that:

  ```asm
          lea     rcx, _ZL7kParity[rip]          ; table address: a constant, no call, no loop
  .L12:   lea     rdx, [rax+rbx]                 ; (acc + i)
          movzx   edx, dl                        ;   & 0xFF
          movzx   edx, BYTE PTR [rcx+rdx]        ; kParity[...]: ONE load
          inc     rax
          add     rbx, rdx                       ; acc +=
          cmp     rax, 50000000
          jne     .L12
          ...
          .section .rodata
          .size   _ZL7kParity, 256               ; the table is 256 bytes of data in the binary
  ```

  while `runtime_parity()` is a real `call` into a function containing the build loop. If you instead see a runtime loop building the table, your `constexpr` didn't force compile-time evaluation (§20.2) — the most common silent failure.

The takeaway for the rest of Part III: compile-time work moves cost from the runtime axis to the build/size axis. That's almost always a great trade for the hot path — but it *is* a trade, and §20.5 is where it goes wrong.

---

## 20.4 Techniques

### 20.4.1 Moving computation to compile time

The general move: identify work whose inputs are **known at compile time** and compute it then.

- **Mark pure, input-known functions `constexpr` (or `consteval` to force it).** Anything computing from literals, configuration constants, or template parameters — sizes, masks, offsets, scaling factors, derived limits. Use `consteval` when running at runtime would be a bug, so the compiler *guarantees* it folded away (§20.2).
- **`constinit` for zero-cost initialization of globals.** A global that must be ready before `main` (a config snapshot, a singleton-ish table) marked `constinit` is initialized at compile time — no runtime static-init cost, no static-init-order fiasco, and it can live in `.rodata` if also `const`.
- **`if constexpr` to specialize and discard dead code.** Select implementation branches at compile time so only the taken branch is instantiated and emitted — specializing a parser per protocol, a container per size, a path per platform, with *no* runtime branch and no dead code emitted (Ch. 13–14). This is the workhorse for compile-time dispatch (Ch. 21).
- **Template/`constexpr` parameters instead of runtime arguments.** A loop count, a buffer size, a field offset passed as a template non-type parameter (or `constexpr`) lets the compiler unroll, size stack buffers, and fold address arithmetic — versus a runtime argument it must handle generically.
- **Compile-time validation.** `static_assert` and `consteval` checks catch errors at build time (a missized struct vs a cache line, Ch. 10; an out-of-range constant) — zero runtime cost, and the bug never ships (ties Ch. 95's security-as-correctness).

The judgment call: move work to compile time when its inputs are genuinely known then **and** it either removes runtime work or catches a bug — not reflexively, because heavy compile-time work costs build time (§20.5) and can hurt the iterate loop (Ch. 3).

### 20.4.2 `constexpr` tables and lookup generation

The highest-leverage compile-time technique for the hot path: **generate lookup tables at compile time** so the runtime is a single indexed load (Ch. 14.4.2) instead of computation or branching. C++20's expanded `constexpr` makes this clean — write the table-builder as a normal function and assign it to a `static constexpr` array:

```cpp
// Generate at compile time; runtime is one load. (Ch. 14 lookup tables, Ch. 35 bit tables.)
static constexpr auto kDigitPairs = [] {                 // "00".."99" for fast integer formatting
    std::array<char, 200> t{};
    for (int i = 0; i < 100; ++i) { t[2*i] = char('0'+i/10); t[2*i+1] = char('0'+i%10); }
    return t;
}();

static constexpr auto kTickSize = [] {                   // price-band → tick size (Ch. 34)
    std::array<std::int64_t, kNumBands> t{};
    for (std::size_t b = 0; b < kNumBands; ++b) t[b] = tick_for_band(b);
    return t;
}();
```

Where this pays in trading code:

- **Protocol/decoding tables (Ch. 67).** Field offsets and lengths for a fixed-layout message, digit-pair tables for `to_chars`-style fast integer formatting (Ch. 67's branch-free parsing), validation bitmaps — all compile-time-known, all reducible to a load.
- **Numeric tables (Ch. 34–35).** Tick-size-by-band maps, scaled-integer conversion factors, bit-manipulation tables (parity, reversal, de Bruijn sequences for bit-scan), power-of-two masks — generated once at build, indexed at runtime.
- **Dispatch tables (Ch. 14–15).** A compile-time table of handler parameters (or, carefully, function pointers) indexed by message type, turning an `if/else` ladder into a load.
- **Perfect hashing / closed sets.** For a *fixed* set of keys (symbols, message types), a compile-time-computed perfect hash or sorted `constexpr` array with binary search (or `std::ranges` over a `constexpr` container) gives branch-light, allocation-free lookup.

(The same table is built with Rust's `const fn` and Zig's `comptime` in Ch. 91 §91.4.3 — identical `.rodata` bytes from all three compilers, and a comparison of what each mechanism lets you write.) Two cautions that bridge to §20.5: keep the table's **size** in mind (it's `.rodata`/data, and a huge table thrashes the *data* cache at runtime, Ch. 8 — the table is only a win if it stays hot), and confirm the construction actually ran at compile time (Ch. 4 — see a fixed-address load, not a build loop). A compile-time table that's too big to stay cache-resident can be slower than recomputing; size it like any other hot data structure (Ch. 9).

---

## 20.5 Pitfalls & anti-patterns: compile-time blowup, code bloat

- **`constexpr` that silently runs at runtime.** The most common failure: you *intended* compile-time evaluation, but the call wasn't in a constant-expression context, so the function ran at runtime (§20.2). Force it — assign to a `static constexpr`/`constexpr` variable, use `consteval`, or check the asm (Ch. 4). "I marked it `constexpr`" is not proof it folded.
- **Compile-time blowup (build-time explosion).** Deep template recursion, large parameter packs, heavy SFINAE overload resolution, or a `constexpr` that builds a giant structure can make the *compiler* take minutes and gigabytes of RAM. This taxes the edit-measure iterate loop (Ch. 3) and CI. Prefer iterative `constexpr` over deep recursion, cap pack sizes, and measure build time as a first-class metric.
- **Template-instantiation code bloat (runtime I-cache cost).** Each distinct instantiation emits a separate body (§20.2); over-templatizing (instantiating a big function for many types, or on values that needn't be template params) bloats the binary and the I-cache (Ch. 13) — a *compile-time* technique with a *runtime* penalty. Share non-dependent code in a non-template base/helper; templatize only what must vary.
- **Huge compile-time tables that blow the data cache.** A `constexpr` table is free to *construct* but its bytes are real and occupy the data cache at runtime (Ch. 8). A multi-KB/MB table indexed randomly can miss cache worse than recomputing a small value. Size tables to stay hot; a table is a cache-locality decision (Ch. 9), not just a compile-time trick.
- **`consteval` over-constraining the interface.** Forcing `consteval` where callers legitimately have runtime inputs makes the function unusable there (a compile error). Use `constexpr` (works both ways) unless runtime use is genuinely a bug you want to forbid.
- **Debugging/diagnostics pain.** Compile-time errors in heavy metaprograms produce notoriously long, opaque messages (Ch. 21), and you can't step a debugger through compile-time evaluation the usual way. Factor metaprograms small, `static_assert` intermediate invariants, and prefer the simplest mechanism that works (`constexpr` function > clever template).
- **Moving work to compile time that didn't need moving.** If the computation is on the *cold* setup path (Ch. 1), a runtime one-time build is fine and keeps the code simpler/buildable. Reserve compile-time effort for the hot path and for compile-time *validation*; don't metaprogram for its own sake.
- **`static constexpr` local vs `constexpr` local.** A `constexpr` *local* may be rematerialized; a `static constexpr` local guarantees a single `.rodata` instance with a stable address (good for tables you take pointers into). Know which you want for tables.

---

## 20.6 Exercises & checklist

**Exercises**

1. **Prove it vanished.** Build `ct_table.cpp`; disassemble `main` (Ch. 4) and confirm the `kParity` lookup is a load from a fixed `.rodata` address with **no** table-construction code, while `runtime_parity()` emits a build loop. Now *break* it two ways and predict each outcome first: (i) change `static constexpr kParity` to a plain `static auto kParity = build_parity();` — construction does **not** reappear (a constant-expression initializer of a static is constant-initialized by [basic.start.static] regardless of `constexpr`; only `.rodata` becomes `.data`); (ii) remove `constexpr` from `build_parity` itself, or feed it a runtime argument such as `argc & 1` — now a dynamic initializer (`_GLOBAL__sub_I_...`) or an in-`main` loop appears.
2. **Both axes.** Measure (a) hot-path ns/lookup, (b) build time (`/usr/bin/time g++ ...`), and (c) binary size (`size`) for the runtime-built vs `constexpr`-built table. Then scale the table to 64 KiB and re-measure all three. Where does the compile-time win turn into a data-cache loss (§20.5)?
3. **`constexpr` vs `consteval`.** Take a `field_offset(Field)` function; make it `constexpr`, call it with a runtime `Field` — does it compile, and where does it run? Make it `consteval` — what error do you get? When is forcing `consteval` the right call (§20.2, §20.4.1)?
4. **`if constexpr` specialization.** Write a `parse<Proto>()` with `if constexpr` branches per protocol; confirm (Ch. 4) the untaken branch emits **no** code. Compare against a runtime `switch (proto)` version's codegen.
5. **Build-time blowup.** Write a deeply *recursive* `constexpr` factorial/Fibonacci vs an *iterative* one; push the input until build time/RAM spikes. Measure build time for each. Relate to §20.5's "prefer iterative `constexpr`."

**Checklist — compile-time mechanics**

- [ ] Work with **compile-time-known inputs** that removes runtime cost (or catches a bug) is moved to compile time via `constexpr`/`consteval`/`constinit`/`if constexpr`.
- [ ] I **forced** compile-time evaluation where I rely on it (`static constexpr` initializer, `consteval`, or a const-expr context) and **verified in the asm** (Ch. 4) — not just added the keyword.
- [ ] Hot-path lookups use **`constexpr`-generated tables** (single indexed load) instead of runtime computation/branching (Ch. 14) — and the tables are **sized to stay cache-hot** (Ch. 8–9).
- [ ] I used **`consteval`** only where runtime evaluation is a bug; otherwise `constexpr` so the function works both ways.
- [ ] I watched **build time and binary size** as first-class costs; heavy metaprograms use **iterative `constexpr`** over deep recursion and cap pack sizes (§20.5).
- [ ] Templates vary **only what must vary**; shared non-dependent code is factored out to avoid **instantiation bloat** (Ch. 13).
- [ ] Compile-time-only work that's really on the **cold setup path** wasn't over-engineered into a metaprogram when a simple runtime build would do (Ch. 1).
- [ ] Compile-time **validation** (`static_assert`/`consteval` checks) guards invariants (sizes, ranges) at build time (ties Ch. 10, 95).

---

## 20.7 References

- ISO C++ standard / cppreference — `constexpr`, `consteval`, `constinit`, `if constexpr`, and the C++20 expansions to constant evaluation (transient allocation, `constexpr` algorithms/containers) that §20.2/§20.4 rely on.
- B. Stroustrup, *The C++ Programming Language* and the C++ Core Guidelines — guidance on constant expressions, when to prefer compile-time evaluation, and the template cost model.
- J. Boccara, *Fluent C++* and various CppCon talks on `constexpr`/`consteval` programming and compile-time table generation — practical patterns behind §20.4.
- L. Dionne, *"Compile-time programming and reflection in C++20 and beyond"* (and the Boost.Hana documentation) — the modern metaprogramming model and its build-time costs (§20.5).
- The GCC/Clang documentation on `-ftime-report`/`-ftime-trace` — measuring where build time goes (the §20.3 build-time axis and the §20.5 blowup diagnosis).

## 20.8 Additional Reading

- B. Saks / CppCon talks on `constexpr` everything, and "constexpr all the things" — the reach and limits of compile-time evaluation in modern C++.
- D. Vandevoorde, N. Josuttis, D. Gregor, *C++ Templates: The Complete Guide* — the definitive treatment of the template instantiation/cost model underlying §20.2 and Ch. 21.
- Ch. 21 (*Template Metaprogramming & Zero-Cost Abstractions*) — CRTP, policy-based design and expression templates built on this foundation; Ch. 14–15 (*Branches / Dispatch*) — the lookup and dispatch this chapter generates; Ch. 34–35 (*Numerics / Bit Tricks*) — compile-time numeric and bit tables; Ch. 26 (*Build Toolchain*) — managing build time, LTO and instantiation.
- **Ch. 91 §91.4.3** — this section's tick-size table rebuilt with Rust `const fn` and Zig `comptime`, with identical `.rodata` shown; **Appendix B** (B.3) — the one-line map of compile-time constructs across languages.

---

*Next: Ch. 21 — Template Metaprogramming & Zero-Cost Abstractions, where compile-time mechanics become a design tool: CRTP and static polymorphism (the devirtualization of Ch. 15 by construction), policy-based design, and expression templates — abstractions that compile away to the same code you'd have written by hand, verified in the asm.*
