# Appendix H — C++23/26 Feature Availability Matrix

> **Consolidates:** the modern-C++ features this book reaches for across many chapters — `std::expected` (Ch. 20), `std::flat_map`/`std::flat_set` (Ch. 25), `<bit>` operations and `std::bit_cast`/`std::byteswap` (Ch. 28), `std::print`/`std::println` (Ch. 71), `std::span` (Ch. 4, 72) and `std::mdspan` (Ch. 29 is its natural home), `std::atomic_ref` (Ch. 35) and `std::jthread` (setup threads — Ch. 31), and `std::execution` senders/receivers (Ch. 39). This book's convention (stated in the Preface) is to use modern C++ where it helps and to **name the standard + toolchain support**. This appendix is where that support is named in one place, so a reader on an older toolchain knows what compiles, what to feature-test, and what to fall back to.

**How to read the matrix:** each row lists — the paper (P/N number), the first **GCC + libstdc++** release, the first **Clang + libc++** release, the **feature-test macro** to `#if` on, and the **C++17/20 fallback** to use when the feature is absent. Library support (libstdc++/libc++) lags language support (the compiler) and is tracked separately — a feature can be "a C++23 language feature GCC 13 accepts" yet "a libstdc++ 14 library type." **Always gate on the `__cpp_lib_*` / `__cpp_*` feature-test macro (H.6), never on `__cplusplus` or `-std=`,** because vendor support is piecemeal and a `-std=c++23` compiler is *not* a guarantee the library type exists. Version numbers below are the "first shipped" releases as checked against the libstdc++, libc++, GCC and Clang implementation-status pages in November 2026 (GCC 16 / LLVM 21 era); **treat them as a starting point and confirm against cppreference's compiler-support page for your exact toolchain** — this is the one appendix that dates fastest.

---

## H.1 How to read the matrix (columns, library-vs-language, the golden rule)

Three ideas govern every row below.

- **Language vs. library support are different milestones.** The *compiler* front-end gains language features (`if consteval`, deducing `this`); the *standard library* implementation (libstdc++ ships with GCC, libc++ ships with Clang/LLVM) gains types and headers (`std::expected`, `<print>`). They advance on separate schedules and separate version numbers. "Does GCC 13 have C++23?" is the wrong question; "does *libstdc++ 13* have `std::flat_map`?" (answer: no — it landed in libstdc++ 15) is the right one.
- **Gate on feature-test macros, not the standard version.** `__cplusplus >= 202302L` tells you the *compiler mode*, not what's implemented. `#ifdef __cpp_lib_expected` (a library macro from `<version>`) or `__cpp_if_consteval` (a language macro the compiler predefines once it implements the feature — no header needed) tells you what's *actually there*. This is the only portable way to write code that compiles across the GCC/Clang/MSVC spread this book targets (GCC and Clang; MSVC readers should consult cppreference's compiler-support page, which this appendix does not duplicate).
- **The golden rule: confirm on *your* toolchain.** The fastest confirmation is a one-line Godbolt check (Ch. 4) — `#include <version>` and print the macro, or just `#include <print>` and see if it resolves. The version numbers here save you a lookup; they do not excuse you from checking, because distro toolchains lag upstream and library backports vary.

**Column legend for H.2–H.5:** *Feature* · *Paper* · *GCC/libstdc++* (first release with usable support) · *Clang/libc++* · *`<version>` macro* · *Fallback*. "libc++ partial/late" is called out where Clang's library trails its compiler by a wide margin (it frequently does for C++23 library features).

## H.2 Core library features

The everyday modern-library types the book leans on hardest.

| Feature | Paper | GCC / libstdc++ | Clang / libc++ | Feature-test macro | Fallback |
|---|---|---|---|---|---|
| `std::expected` (Ch. 20 — hot-path error handling) | P0323 | GCC 12 | Clang 16 / libc++ 16 | `__cpp_lib_expected >= 202202` | `tl::expected`, `boost::outcome`, or an error-code + out-param |
| `std::span` (Ch. 4, 72) | P0122 | GCC 10 | Clang 7 / libc++ 7 | `__cpp_lib_span >= 202002` | `gsl::span`, or `(ptr, len)` pair |
| `std::mdspan` (Ch. 29) | P0009 | GCC 16 / libstdc++ 16 (no `<mdspan>` in 13–15) | Clang/libc++ 18 | `__cpp_lib_mdspan >= 202207` | `Kokkos::mdspan` reference impl, or manual index math |
| `std::flat_map` / `std::flat_set` (Ch. 25) | P0429 / P1222 | libstdc++ 15 | libc++ 20 (`flat_map`) / 21 (`flat_set`) | `__cpp_lib_flat_map` / `_flat_set >= 202207` | `boost::container::flat_map`, or a sorted `std::vector` + `lower_bound` |
| `std::print` / `std::println`, `<print>` (Ch. 71) | P2093 | libstdc++ 14 | libc++ 18 | `__cpp_lib_print >= 202207` | `fmt::print` (the library it standardizes) |
| `std::format` (underlies `<print>`) | P0645 | libstdc++ 13 | libc++ 14 (`__cpp_lib_format` defined only from libc++ 19) | `__cpp_lib_format >= 201907` — on libc++ 14–18 also accept `__has_include(<format>)` | `fmt::format` |

**Trading relevance.** `std::expected` is the one to prioritize adopting — it gives branch-predictable, allocation-free error handling on the decode/order path (Ch. 20) with none of the throw-path cost (Ch. 20's exception discussion). `std::flat_map`/`flat_set` matter for small ordered structures where node-based `std::map` cache-misses dominate (Ch. 25) — both mainstream libraries ship them now (libstdc++ 15, libc++ 20/21), but they landed recently enough that the `boost::container::flat_map` / sorted-`vector` fallback is still what most deployed toolchains need. `std::print` is strictly off-hot-path (Ch. 71) — its value is ergonomics and avoiding iostream overhead in setup/logging code, not tick-to-trade.

## H.3 Language features

Compiler front-end features; these are gated by *language* macros (predefined by the compiler, no `<version>` needed).

| Feature | Paper | GCC | Clang | Macro | Fallback |
|---|---|---|---|---|---|
| `consteval` / `constinit` (Ch. 18) | P1073 / P1143 | GCC 10 (`consteval` complete in 11) | Clang 10 (`constinit`); `consteval` partial from 11, complete in 17 | `__cpp_consteval`, `__cpp_constinit` | `constexpr` (weaker guarantee) |
| Concepts (Ch. 19) | P0734 | GCC 10 | Clang 10 | `__cpp_concepts >= 201907` | SFINAE / `std::enable_if` |
| Coroutines (Ch. 38–39) | P0912 | GCC 10 (needs `-fcoroutines`; on by default with `-std=c++20` from GCC 11) | Clang 8 (`-fcoroutines-ts`; the status page still marks P0912 "Partial"); `<coroutine>` header in libc++ 14 | `__cpp_impl_coroutine` + `__cpp_lib_coroutine` | callbacks / state machines by hand |
| `[[likely]]` / `[[unlikely]]` (Ch. 12) | P0479 | GCC 9 | Clang 12 | `__has_cpp_attribute(likely)` | `__builtin_expect(x, 1/0)` |
| `[[assume]]` (Ch. 21) | P1774 | GCC 13 | Clang 19 | `__has_cpp_attribute(assume)` | `__builtin_assume(x)` (Clang) / `__attribute__((assume(x)))` (GCC 13+); GCC has no `__builtin_assume` — portable idiom `if (!(x)) __builtin_unreachable();` |
| `if consteval` (Ch. 18) | P1938 | GCC 12 | Clang 14 | `__cpp_if_consteval >= 202106` | `std::is_constant_evaluated()` (C++20) |
| Deducing `this` (Ch. 19) | P0847 | GCC 14 | Clang 18 | `__cpp_explicit_this_parameter` | CRTP (Ch. 19) or duplicated const/non-const overloads |
| Multidimensional `operator[]` (Ch. 29) | P2128 | GCC 12 | Clang 15 | `__cpp_multidimensional_subscript` | `operator()` or a single flattened index |

**Trading relevance.** `[[likely]]`/`[[unlikely]]` and `[[assume]]` are the direct hot-path levers (Ch. 12, 21) — but note the fallbacks are what the standard attributes lower to anyway: `__builtin_expect` is universal, while the assume fallback is compiler-specific (`__builtin_assume` on Clang, `__attribute__((assume))` on GCC 13+, else the `__builtin_unreachable()` idiom) — so portability here costs a one-line macro at most. `consteval`/`constinit` move work and initialization to compile time (Ch. 18) — high value for lookup tables and configuration baked at build time. Deducing `this` is a nicety that can replace some CRTP boilerplate (Ch. 19), but CRTP remains the portable, well-understood choice.

## H.4 Concurrency & execution

The threading/atomics and async surface (Part VI).

| Feature | Paper | GCC / libstdc++ | Clang / libc++ | Macro | Fallback |
|---|---|---|---|---|---|
| `std::atomic_ref` (Ch. 35) | P0019 | libstdc++ 10 | libc++ 19 (late) | `__cpp_lib_atomic_ref >= 201806` | atomic ops on a `std::atomic*` view, or compiler atomics builtins |
| `std::jthread` / `std::stop_token` (Ch. 31) | P0660 | libstdc++ 10 | libc++ 20 (18–19 only with `-fexperimental-library`) | `__cpp_lib_jthread >= 201911` | `std::thread` + manual join + a `std::atomic<bool>` stop flag |
| `std::barrier` / `std::latch` (Ch. 30) | P1135 | libstdc++ 11 | libc++ 11 | `__cpp_lib_barrier`, `__cpp_lib_latch` | condition-variable + counter |
| `std::hardware_destructive_interference_size` (Ch. 33) | P0154 | libstdc++ 12 (`-Winterference-size` warns when it leaks into an ABI) | libc++ 19 (macro only from 19) | `__cpp_lib_hardware_interference_size >= 201703` | `constexpr std::size_t kLine = 64;` — and note the value is a *tuning default*, not a hardware query: 64 on GCC x86-64, **256** on GCC AArch64 (Appendix A.3) |
| `std::atomic<T>::wait` / `notify_one` / `notify_all` (Ch. 31–32) | P1135 | libstdc++ 11 | libc++ 11 | `__cpp_lib_atomic_wait >= 201907` | spin with `pause` then `futex(2)` (Ch. 32), or a condition variable |
| `std::execution` — senders/receivers (Ch. 39) | P2300 | not in libstdc++ yet | not in libc++ yet | `__cpp_lib_senders` (when shipped) | the **stdexec** reference implementation (NVIDIA), or a hand-rolled executor |
| `std::hive` (C++26) | P0447 | not yet | not yet | `__cpp_lib_hive` (when shipped) | `plf::colony` (the library it standardizes), or a pool allocator (Ch. 24) |

**Trading relevance.** `std::atomic_ref` matters where you must apply atomic operations to a field inside an otherwise-non-atomic struct (Ch. 33 — e.g. a hot counter in a cache-line-padded control block) without making the whole object `std::atomic`; libc++ shipped it very late, so check. `std::execution` (P2300) is the big forward-looking one (Ch. 39) but is **not in either shipping standard library yet** — use stdexec if you want to experiment, but do not build a production hot path on it assuming stdlib availability. `std::jthread` is convenience for setup/control threads, not the hot path.

## H.5 Numerics & bit manipulation

The bit-twiddling and numeric-conversion surface (Part V, Ch. 28; parsing in Ch. 53).

| Feature | Paper | GCC / libstdc++ | Clang / libc++ | Macro | Fallback |
|---|---|---|---|---|---|
| `std::bit_cast` (Ch. 21, 27) | P0476 | GCC 11 / libstdc++ 11 | Clang 14 / libc++ 14 | `__cpp_lib_bit_cast >= 201806` | `memcpy` into the target type (same codegen, defeats strict-aliasing UB — Ch. 21) |
| `<bit>`: `popcount`, `countl/r_zero`, `bit_width`, `rotl/rotr` (Ch. 28) | P0553 / P0556 | libstdc++ 9 | libc++ 9 | `__cpp_lib_bitops >= 201907` | `__builtin_popcount`/`_clz`/`_ctz`, `_mm_popcnt_u64`, BMI intrinsics |
| `std::byteswap` (Ch. 53 — endianness) | P1272 | libstdc++ 12 | libc++ 14 | `__cpp_lib_byteswap >= 202110` | `__builtin_bswap16/32/64`, `bswap_*` |
| `std::endian` (Ch. 53) | P0463 | libstdc++ 8 | libc++ 7 | `__cpp_lib_endian >= 201907` | `__BYTE_ORDER__` / `__ORDER_LITTLE_ENDIAN__` |
| Saturating arithmetic `add_sat` etc. (Ch. 72) | P0543 (C++26) | libstdc++ 14 | libc++ 18 | `__cpp_lib_saturation_arithmetic >= 202311` | manual clamp with overflow check (Ch. 72), or `__builtin_add_overflow` + clamp |
| `std::assume_aligned<N>(p)` (Ch. 21, 29) | P1007 | libstdc++ 10 | libc++ 15 | `__cpp_lib_assume_aligned >= 201811` | `__builtin_assume_aligned(p, N)` |
| `std::unreachable()` (Ch. 13, 21) | P0627 | libstdc++ 12 | libc++ 15 | `__cpp_lib_unreachable >= 202202` | `__builtin_unreachable()` |
| `std::start_lifetime_as<T>(p)` (Ch. 21, 53 — the sanctioned struct-over-bytes overlay) | P2590 | libstdc++ 16 (experimental; absent in 13–15) | not yet | `__cpp_lib_start_lifetime_as >= 202207` | `memcpy`/`std::bit_cast` into a local, or the `reinterpret_cast` the chapters mark as technically UB |
| `std::simd` (C++26; Ch. 29, Appendix A.2) | P1928 | libstdc++ 16 (experimental); `<experimental/simd>` (Parallelism TS v2) since libstdc++ 11 | not yet | `__cpp_lib_simd` (when shipped); `__cpp_lib_experimental_parallel_simd` for the TS | `std::experimental::simd`, xsimd, Google Highway, or raw intrinsics (Ch. 29) |
| `std::inplace_vector` (C++26 — fixed-capacity, no allocation) | P0843 | libstdc++ 15 | not yet (check) | `__cpp_lib_inplace_vector >= 202406` | a `std::array` + size field, `boost::container::static_vector` |
| `std::from_chars` / `std::to_chars` (Ch. 53) | P0067 | libstdc++ 8 (integers) / 11 (floating point) | libc++ 7 (integers) / 14 (FP `to_chars`) / 20 (FP `from_chars`) | `__cpp_lib_to_chars >= 201611` — never defined by libc++ (`long double` unimplemented); there gate on `_LIBCPP_VERSION` or a configure-time test-compile | hand-rolled branch-free integer/decimal parse (Ch. 53) |

**Trading relevance.** The `<bit>` operations are the highest-value adoption here (Ch. 28 — order-book occupancy bitmaps, free-list scans) and the best-supported (shipped in GCC 9 / Clang 9), but the fallback intrinsics are what they lower to and are equally fast — so `<bit>` buys readability and constexpr-friendliness, not speed. `std::bit_cast` is the *correct* type-pun that avoids the strict-aliasing UB the `reinterpret_cast` fallback risks (Ch. 21) — prefer it. `std::from_chars`/`to_chars` are the standard's locale-independent, non-throwing, allocation-free number conversions (Ch. 53 shows where a hand-rolled branch-free parser still wins) — but libc++'s *floating-point* `from_chars` shipped only in libc++ 20 and libc++ never defines `__cpp_lib_to_chars`, so gate on the library version if you parse floats there.

## H.6 Feature-test macros & graceful degradation

The pattern that makes all of the above portable across the toolchain spread.

- **`#include <version>`** exposes every `__cpp_lib_*` library macro in one place — include it before testing library features. Language `__cpp_*` and `__has_cpp_attribute(...)` macros are predefined by the compiler and need no header.
- **`__has_include(<header>)`** is the coarse gate when a whole header may be absent: `#if __has_include(<print>)`. Use it to guard the `#include` itself, then the `__cpp_lib_*` macro to guard *usage*.
- **A reusable compatibility shim.** Wrap the decision once so the chapters' examples stay clean:

```cpp
   // compat/expected.hpp — one place that knows what the toolchain has.
   #include <version>
   #if defined(__cpp_lib_expected) && __cpp_lib_expected >= 202202L
   #  include <expected>
   namespace app { using std::expected; using std::unexpected; }
   #else
   #  include <tl/expected.hpp>          // vendored fallback
   namespace app { using tl::expected; using tl::unexpected; }
   #endif
```

  The hot-path code writes `app::expected<Price, ParseError>` and never sees the version question; the shim absorbs it. Do the same for `flat_map`, `print`, `bit_cast`, and `span`. This is the concrete mechanism behind the Preface's convention of showing the C++17/20 fallback where the target toolchain lacks the feature.
- **Compiler-mode flags.** Selecting the standard: GCC/Clang `-std=c++23` (or `-std=c++2b` on slightly older releases), `-std=c++26`/`-std=c++2c` for the draft; libc++ additionally needs `-stdlib=libc++`. See Appendix D.4 for the `-std=`/`-stdlib=` flag entry.

## H.7 References

- **cppreference** — the *C++ compiler support* and *C++ feature-test macros* pages: the authoritative, continuously-updated matrix this appendix summarizes. Check these for your exact toolchain before relying on any row above.
- The **libstdc++** ("Implementation Status → C++ 2023/2026") and **libc++** ("Implementation Status") pages, and the GCC/Clang release notes — the library-vs-language split (H.1) is documented there.
- The **WG21 papers** cited per row (P0323 `expected`, P0122 `span`, P0009 `mdspan`, P0429/P1222 `flat_map`/`flat_set`, P2093 `print`, P0645 `format`, P2300 `std::execution`, P0553/P0556 `<bit>`, P0476 `bit_cast`, P1272 `byteswap`, P0543 saturating, P0067 `to_chars`, P1007 `assume_aligned`, P2590 `start_lifetime_as`, P1928 `simd`, P0843 `inplace_vector`, P0154 interference sizes).
- The chapters that use each feature: Ch. 4, 8, 12, 18–21, 25, 27–33, 35, 38–39, 53, 71–72.

## H.8 Additional Reading

- **Appendix D** (Compiler Flag Reference) — the `-std=`/`-stdlib=` flags (D.4) that select these standards and libraries.
- **Appendix B** (Beyond C++) — where a feature's *absence* (or a whole missing capability) is an argument for another language; and how `constexpr`, `std::expected`, coroutines and `bit_cast`-style punning compare with their Rust/Zig counterparts (B.1–B.2).
- **Appendix G** (Annotated Bibliography) — the standards-committee and library-design background reading.
- The `<version>` header, `__has_include`, and Godbolt (Ch. 4) — the fastest way to answer "does *my* toolchain have it?" for real, which no static table can.

---

*Next: Appendix I — Tooling Command Cookbook, a copy-paste one-liner reference for the tools used throughout the book — `perf`, VTune, `bpftrace`/eBPF, `numactl`/`taskset`/`chrt`, `pahole`, `ethtool`, `objdump`, and the sanitizers — consolidating the commands scattered across Ch. 2–5, 61 and the operations chapters.*
