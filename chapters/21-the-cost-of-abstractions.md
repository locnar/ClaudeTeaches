# Part III — Compile-Time & Language Mechanics

# Chapter 21 — The Cost of Abstractions

> **Prerequisites:** Ch. 19 (zero-cost abstractions — this chapter is its counterweight: the abstractions that *aren't* free) and Ch. 20 (the compile-time decoder, whose generated accessors return the small values §21.4.5 classifies for the calling convention), Ch. 4 §4.2.1 (the SysV register model), Ch. 12 (code layout / hot-cold splitting — the cold path is exactly where unwinding code belongs), Ch. 1 (tail latency — an exception throw is a tail event), Ch. 4 (reading asm — §21.5 reads unwinding tables and landing pads).
>
> **Leads into:** Ch. 23 (aliasing & type punning — more language-level codegen gates), Ch. 60 (zero-copy parsing — hot-path error handling on untrusted input), Ch. 87 (secure programming — UB and error handling as a vulnerability class). `std::expected` ties back to Ch. 19 (a zero-cost error channel) and forward to Ch. 28/30 (hot-path APIs).

---

## 21.1 Why it matters: error handling on the hot path

Chapter 19 celebrated abstractions that compile to nothing. This chapter is the honest counterweight: some abstractions have a **real cost**, and the most consequential is **error handling**. Every hot path must decide what to do when something goes wrong — a malformed packet, a sequence gap, a full queue, an out-of-range price — and the *mechanism* you choose (C++ exceptions, error codes, `std::expected`) has dramatically different costs on the **happy path** (the common case where nothing goes wrong) versus the **error path** (the rare failure). Choose wrong and you either pay a tax on every single message for errors that almost never happen, or you pay a catastrophic, unbounded cost on the one message that does — and in HFT, both failure modes land in the tail latency (Ch. 1) you're trying to protect.

The central, counterintuitive fact about C++ exceptions is the **"zero-cost" exception model** (the Itanium ABI used by GCC/Clang on Linux): when no exception is thrown, a `try`/`throw` construct adds *almost nothing* to the happy path — no runtime checks, no flag tests, the code runs as if the error handling weren't there. The cost is moved entirely to the **throw**, which is *enormous*: throwing walks stack-unwinding tables, runs destructors, does RTTI type-matching to find a handler — *microseconds*, not nanoseconds, and **non-deterministic** (it depends on the call depth and the tables). So exceptions are, perversely, *excellent* for truly exceptional conditions (the happy path is free) and *catastrophic* if you throw on anything that happens with any regularity on the hot path. The art is matching the mechanism to the *frequency*: free-when-not-thrown exceptions for genuinely rare, fatal conditions; a cheap value-returning channel (`std::expected`, error codes) for *expected* failures that occur often enough that a throw's cost would dominate.

Around exceptions sit the other "abstraction taxes": **RTTI** (`dynamic_cast`/`typeid`) adds type-info tables and runtime type-walking; `noexcept` is the lever that tells the compiler "no unwinding here," unlocking optimizations and shrinking code; and the binary-size/codegen effects of exception support (unwinding tables, landing pads — Ch. 12) are real even when nothing throws. This chapter measures these costs (§21.3), verifies them in the asm (§21.5), and lays out the hot-path discipline: **`noexcept` the happy path, return `std::expected`/error codes for expected failures, reserve `throw` for the cold/rare/fatal, and know what `-fno-exceptions`/`-fno-rtti` buy and cost.** As always, this is a measured trade, not dogma — exceptions are not "slow"; they are *free until thrown and then expensive*, and engineering is putting each cost where it belongs.

## 21.2 Mental model: exception unwinding, RTTI, `noexcept`

**The zero-cost (table-based) exception model.** On Linux x86-64 (Itanium C++ ABI), exception support is implemented so the **happy path pays nothing**:

```
   normal flow (no throw):    code runs straight through — NO checks, NO flags, NO branches
                              for error handling. try { } is "free" at runtime when nothing throws.

   throw:                     1. construct the exception object (may allocate!)
                              2. the unwinder consults .eh_frame / .gcc_except_table (LSDA)
                              3. SEARCH phase: walks UP the stack, RTTI type-matching the thrown
                                 type against each frame's catch clauses (no destructors yet)
                              4. CLEANUP phase: walks again, running destructors/cleanups up to
                                 the matching frame (an uncaught throw terminates with the stack intact)
                              5. transfers to the matching landing pad
                              → MICROSECONDS, non-deterministic (depth/table-dependent)
```

The key consequences:

- **"Zero-cost" means zero on the *happy* path, not zero overall.** The cost is real but *relocated* to the throw and to *binary size* (the unwinding tables `.eh_frame`/`.gcc_except_table` and per-call-site landing pads exist whether or not you throw — Ch. 12). Throughput/latency of non-throwing code is essentially unaffected; the binary is bigger and a *throw* is very expensive.
- **A throw is a tail event.** Microseconds and variance — fine for "shut down, this is fatal," ruinous for "this packet was malformed" if malformed packets arrive even occasionally. The throw cost scales with stack depth and runs destructors all the way up.
- **Throwing may allocate.** The exception object is constructed somewhere (often the heap on the cold path); `std::bad_alloc`, `std::system_error`, etc. The throw path is *not* allocation-free — another reason it's off the hot path (Ch. 26).

**RTTI (Run-Time Type Information).** `dynamic_cast` and `typeid` need per-type type-info records and walk the inheritance graph at runtime (a `dynamic_cast` across a hierarchy can be surprisingly slow). RTTI tables add binary size for every polymorphic type. Exceptions *use* RTTI (to match `catch` types), so the two are linked. On a hot path, a `dynamic_cast` is an abstraction tax to avoid (use static dispatch — Ch. 19, or `std::variant` — Ch. 14); where no RTTI is needed at all, `-fno-rtti` removes the tables.

**`noexcept` — the optimization lever.** Declaring a function `noexcept` is a promise it won't throw (if it does, `std::terminate`). This is not just documentation; it *changes codegen*:

- **No unwinding code at the call site.** Call sites of a `noexcept` callee need no landing pad or cleanup edge (the function itself still carries its `.eh_frame` entry and personality; GCC gives it an empty LSDA so a throw escaping it terminates, Clang adds a small `__clang_call_terminate` pad) — smaller code (Ch. 12), and the optimizer can reason more freely across it (no edge to an exceptional path).
- **It enables library fast paths.** The canonical example: `std::vector` reallocation uses `std::move` *only if* the element's move constructor is `noexcept` — otherwise, if the type is also copy-constructible, it *copies* (move-only types are moved regardless, forfeiting the strong guarantee) — to preserve the strong exception guarantee). A non-`noexcept` move ctor silently makes every vector growth copy instead of move (Ch. 26, 28). `noexcept` on move ctors/swap/dtors is performance-critical, not cosmetic.
- **It documents and enforces the happy-path contract.** Marking hot-path functions `noexcept` states "errors here are returned as values, not thrown," and the compiler holds you to it.

The model: **exceptions are free until thrown (then microseconds); RTTI/`dynamic_cast` is a runtime walk; `noexcept` removes unwinding overhead and unlocks move-based library fast paths. Put the throw on the cold path, mark the hot path `noexcept`, and return expected failures as values.**

## 21.3 Measure it: exceptions vs error codes vs `std::expected`

Two costs to measure: the **happy path** (does the error *mechanism* tax the common case?) and the **error path** (how expensive is signaling a failure?). Validate a stream of messages where a tunable fraction are "bad," signaling failure three ways: (a) **throw** an exception, (b) return an **error code** (`int`/`enum`), (c) return **`std::expected<T, E>`** (C++23).

```cpp
// errpath.cpp — same validation, three error mechanisms; sweep the bad-fraction.
// Build: g++ -O2 -std=c++23 -march=native errpath.cpp -o errpath
//   (clang++ >= 19 with libstdc++ 13+, or clang++ >= 16 with -stdlib=libc++: libstdc++'s <expected>
//    is gated on __cpp_concepts >= 202002L, which Clang only reached in 19)
// Run pinned, turbo off:  taskset -c 2 ./errpath throw|code|expected <bad_per_1000>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <expected>
#include <vector>
#include <chrono>

struct Msg { std::int64_t px; std::int32_t qty; };
enum class Err { Ok, BadPrice };

static inline bool is_bad(const Msg& m) { return m.px < 0; }     // the "validation"

// (a) throw
std::int64_t val_throw(const Msg& m) { if (is_bad(m)) throw Err::BadPrice; return m.px * m.qty; }
// (b) error code (out-param result)
Err val_code(const Msg& m, std::int64_t& out) {
    if (is_bad(m)) return Err::BadPrice; out = m.px * m.qty; return Err::Ok; }
// (c) std::expected
std::expected<std::int64_t, Err> val_exp(const Msg& m) {
    if (is_bad(m)) return std::unexpected(Err::BadPrice); return m.px * m.qty; }

// The timed loop, instantiated once per mechanism: the mode is decided BEFORE t0, never inside the
// per-message loop (a strcmp per message would cost more than the ~0.3 ns being measured). The asm
// barrier (Appendix L) keeps acc live per message and stops the compiler vectorizing (b)/(c) but not (a).
enum class Mode { Throw, Code, Expected };
template <Mode M>
[[gnu::noinline]] void run(const std::vector<Msg>& in, int reps, std::int64_t& acc, std::int64_t& errs) {
    for (int r = 0; r < reps; ++r)
        for (const Msg& m : in) {
            if constexpr (M == Mode::Throw) {
                try { acc += val_throw(m); } catch (Err) { ++errs; }
            } else if constexpr (M == Mode::Code) {
                std::int64_t o; if (val_code(m, o) == Err::Ok) acc += o; else ++errs;
            } else {
                auto e = val_exp(m); if (e) acc += *e; else ++errs;
            }
            asm volatile("" : "+r"(acc));
        }
}

int main(int argc, char** argv) {
    const char* mode = argc > 1 ? argv[1] : "expected";
    int bad_per_1000 = argc > 2 ? std::atoi(argv[2]) : 0;       // 0 = pure happy path
    constexpr std::size_t N = 1u << 16; constexpr int REPS = 4000;
    std::vector<Msg> in(N);
    for (std::size_t i = 0; i < N; ++i) {
        bool bad = (int)(i % 1000) < bad_per_1000;
        in[i] = { bad ? -1 : std::int64_t(100 + i), std::int32_t(1 + (i & 7)) };
    }

    std::int64_t acc = 0, errs = 0;
    auto t0 = std::chrono::steady_clock::now();
    if      (!std::strcmp(mode, "throw")) run<Mode::Throw>(in, REPS, acc, errs);
    else if (!std::strcmp(mode, "code"))  run<Mode::Code>(in, REPS, acc, errs);
    else                                  run<Mode::Expected>(in, REPS, acc, errs);
    auto t1 = std::chrono::steady_clock::now();
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
    std::printf("%-8s bad/1000=%d  acc=%lld errs=%lld  %.3f ns/msg\n",
                mode, bad_per_1000, (long long)acc, (long long)errs, ns/((double)REPS*N));
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP, 2.9 GHz), GCC 13 `-O2 -std=c++23 -march=native`, Linux 6.18, pinned, turbo off (illustrative; the *shape* is the point). The mode is resolved to a template parameter *before* the clock starts: an earlier draft had `strcmp(mode, …)` inside the per-message loop, and its two PLT calls per message (~1-2 ns, biased by match order) swamped the ~0.3 ns being compared — the harness measured itself (Ch. 3, Appendix L).

```
                          bad/1000 = 0       bad/1000 = 1        bad/1000 = 50
  (mechanism)            (pure happy path)   (0.1% errors)       (5% errors)
throw                       ~0.35 ns           ~2.3 ns            ~95 ns      <- throw cost dominates (~1.9 µs each)
error code                  ~0.35 ns           ~0.35 ns           ~0.37 ns    <- flat: errors are cheap
std::expected               ~0.35 ns           ~0.35 ns           ~0.37 ns    <- flat, and type-safe
```

The shape is the entire lesson:

- **On the pure happy path (0 errors) all three are ~equal** — the zero-cost exception model (§21.2) means `try` adds nothing when nothing throws; `std::expected` is a cheap value return. *If errors never happen, exceptions cost nothing at runtime.*
- **Exceptions fall off a cliff as the error rate rises.** Even **0.1%** bad messages makes `throw` ~6-7× slower than the value-returning mechanisms; at 5% it's *hundreds* of times slower — each throw is microseconds (§21.2). The cost is entirely on the *error* path, and it's non-deterministic (Ch. 1's tail).
- **Error codes and `std::expected` are flat** — signaling a failure is just returning a value, so cost is independent of error rate. `std::expected` gives you the **monadic, type-safe** ergonomics (the error is in the type and `and_then`/`or_else` compose; with `[[nodiscard]]` it is hard to drop by accident) at error-code cost — which is why it's the modern hot-path default for *expected* failures.

The rule the data dictates: **match the mechanism to the error *frequency*.** Genuinely rare, fatal conditions → exceptions (free happy path, and you *want* to unwind-and-die). Anything that happens with any regularity on the hot path (malformed packets, gaps, full queues, rejects) → `std::expected`/error codes (flat cost). The disaster is throwing on a frequent condition (§21.6); the waste is hand-rolling error codes where exceptions' free happy path would do for a rare-and-fatal case.

## 21.4 Techniques

### 21.4.1 `noexcept` and the happy path

`noexcept` is the lever that makes the happy path cheaper and unlocks library fast paths:

- **Mark hot-path functions `noexcept`.** It removes landing pads / cleanup edges at call sites (smaller code — Ch. 12), lets the optimizer reason without an exceptional edge, and documents "this returns errors as values, doesn't throw." Apply to the steady-state hot path deliberately.
- **`noexcept` move constructors / `swap` / destructors are *performance-critical*.** `std::vector` (and other containers) will **copy instead of move** on reallocation if the element's move ctor isn't `noexcept` (§21.2) — silently turning O(n) moves into O(n) copies on every growth. Always `noexcept` your move ctor, move assignment, and `swap` (and destructors are implicitly `noexcept`). This single habit is one of the highest-leverage, lowest-effort wins in the book (Ch. 26, 28).
- **Conditional `noexcept`.** `noexcept(noexcept(expr))` propagates the guarantee for generic code (a wrapper is `noexcept` iff what it calls is), so templated hot-path code keeps the contract without lying.
- **Don't `noexcept` what legitimately throws.** If a function genuinely needs to propagate an exception, marking it `noexcept` means a throw calls `std::terminate`. Use `noexcept` where the contract is real (happy path returns values), not as a blanket.

### 21.4.2 `std::expected` for hot-path errors

`std::expected<T, E>` (C++23 — libstdc++ 12 / GCC 12, libc++ 16 / Clang 16; `and_then`/`or_else`/`transform` from GCC 13 / Clang 17; gate on `__cpp_lib_expected >= 202202L`, see Appendix H; `tl::expected`/`std::optional`+code as the C++17/20 fallback) is the modern, zero-overhead, *type-safe* error channel for **expected** failures:

- **It's a value, not a control-flow event.** `expected<T,E>` holds *either* a `T` or an error `E` inline (no allocation, no unwinding) — signaling failure is returning it, at error-code cost (§21.3), with no throw.
- **The error is in the type, and it composes.** Unlike a bare error code, the failure is visible in the signature — but enforcement is not automatic: `std::expected` is not `[[nodiscard]]`, a returned value can be dropped, and `*e`/`e->` on an error-holding object is undefined behaviour (only `.value()` checks and throws `bad_expected_access`). Put `[[nodiscard]]` on the function (a warning unless `-Werror`) and test `has_value()` before dereferencing; `and_then`/`or_else`/`transform` chain fallible steps monadically without nested `if`s — readable *and* fast. A feed-handler decode pipeline (parse → validate → enqueue, Ch. 60) reads as a clean chain that compiles to branch-on-value.
- **Pick `E` to be cheap.** An `enum`/small struct error keeps `expected` small and trivially copyable (Ch. 9); a heavy error type bloats the return. For the hot path, `E` is usually a small enum + maybe a code.
- **Use it for *expected* failures; keep exceptions for *exceptional* ones.** `expected` for malformed input, gaps, rejects, capacity limits — things that happen and that the hot path must handle in stride. A genuinely fatal, can't-continue condition can still throw (cold path). This is the deliberate two-channel design: values for expected errors, exceptions for fatal ones.

### 21.4.3 Disabling RTTI where unused

When you don't need runtime type identification, remove its cost:

- **`-fno-rtti`** drops `typeid`/`dynamic_cast` support and the associated type-info tables — smaller binary, no RTTI machinery. Viable when your design uses static polymorphism (Ch. 19) or `std::variant` (Ch. 14) instead of `dynamic_cast`. (Some libraries need RTTI; check.)
- **`-fno-exceptions`** makes `throw`/`try`/`catch` in *your* code compile errors (GCC: "exception handling disabled, use '-fexceptions' to enable") and turns the standard library's internal `__throw_*` helpers into `abort()`; landing pads and `.gcc_except_table` disappear (smaller code, Ch. 12), but `.eh_frame` stays unless you also pass `-fno-asynchronous-unwind-tables` — which breaks `perf`/gdb backtraces, so usually you don't. Used in environments that ban exceptions (some embedded/HFT codebases) where *all* error handling is value-based (`expected`/codes). It's a whole-program decision (the standard library's throwing paths now terminate) — powerful for code size/determinism, but you give up exceptions everywhere, so it's a deliberate codebase-wide stance, not a per-file tweak.
- **Avoid `dynamic_cast` on the hot path regardless.** Even with RTTI enabled, a hot-path `dynamic_cast` is a runtime type-walk; replace it with static dispatch (Ch. 19), a visitor over `std::variant` (Ch. 14), or a type tag you check directly.
- **Know the trade-offs (Ch. 24).** `-fno-rtti`/`-fno-exceptions` are build-toolchain decisions (Ch. 24) with real ergonomic costs (no exceptions in *any* code, including libraries you call); measure the code-size/latency win against the loss of a convenient error channel for cold paths.

### 21.4.4 Assertions, contracts and the diagnostics library on the hot path

The abstractions above decide how the hot path reports an *expected* failure. The standard also has a growing set of tools for the *unexpected* one — where am I, how did I get here, is anyone watching, what did the caller promise — and each has a cost that decides which side of the hot/cold line it lives on. *Measured with GCC 13.3 (libstdc++ 13), `-O2 -std=c++23 -g`, on a 4-vCPU Xeon VM (TSC 2.7 GHz), Linux 6.18; Appendix L.3 for the reference machine.*

- **`std::source_location` (C++20; GCC 11+, Clang 16+ via `__builtin_source_location`; `__cpp_lib_source_location >= 201907L`) — the zero-cost site tag.** As a defaulted parameter it names the *caller's* file, line, column and function without a macro: `void log_fill(std::int64_t px, std::source_location loc = std::source_location::current())`. Measured as a 40-byte log record (file, function, line, column, `rdtscp`, argument) into a ring: **55 cycles per call, 46 of which are the `rdtscp`** — the location itself is one `lea` of a static `.rodata` object hoisted out of the caller's loop (`lea 0x159a5(%rip),%rbp` once; `mov %rbp,%rsi` per call) and a 24-byte copy in the callee (`movdqu` + two `mov`s), i.e. exactly what `__FILE__`/`__LINE__`/`__func__` immediates cost, with the function name correct inside templates and lambdas where `__func__` is not. This is the Ch. 84 logger's C++20 default for format-site IDs (§84.4.2), with the macro as the C++17 fallback; GCC emits a `test %rsi,%rsi` null-check on the impl pointer per call (its implementation stores a pointer) that Clang's does not — one predicted branch.
- **`assert`, `NDEBUG` and the hardened library — cheap checks that may stay on.** A plain `assert` is a predictable compare and a cold `abort` path; `-D_GLIBCXX_ASSERTIONS` (libstdc++) / `-D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_FAST` (libc++) turns the library's own preconditions into the same, at **+3–8 %** on a decode loop with three checked indexings (Ch. 5 §5.4.5, Ch. 87 §87.3). Keep the ones whose bound the optimizer cannot prove and whose violation is a corrupted index (Ch. 87); a failed check is a `SIGABRT`/`SIGILL` the Ch. 93 handler turns into a crash record naming the line.
- **`std::unreachable()` (C++23; GCC 12, Clang 15; `__cpp_lib_unreachable >= 202202L`) and `[[assume]]` (C++23; GCC 13, Clang 19 — Appendix H.3).** The *opposite* of a check: a promise to the optimizer that lets it delete the branch (Ch. 13, 23). `std::unreachable()` after a `switch` over a closed enum removes the default path and its range check; `[[assume(n < 4096)]]` removes a bounds check the hardened library would otherwise keep. In non-release builds map both to `__builtin_trap()` (`#ifdef NDEBUG … #else __builtin_trap()`), so a false promise is a `SIGILL` in test rather than UB in production — Ch. 87 §87.2.2.
- **`std::stacktrace` (C++23; libstdc++ 12+ with `-lstdc++_libbacktrace` on GCC 12–13 and `-lstdc++exp` on GCC 14; not in libc++ 18; `__cpp_lib_stacktrace >= 202011L`) — capture is cheap, symbolisation is not.** Measured at ~12 frames: **`std::stacktrace::current()` 4,100–4,200 cycles (≈ 1.5 µs); `std::to_string(…)` of it 35,000–40,000 cycles (≈ 13–15 µs)** — the capture is a `.eh_frame` unwind (Ch. 24 §24.4.4's ~200 cycles per frame plus a vector allocation), the symbolisation reads DWARF through libbacktrace. Neither belongs on the hot path, and neither is async-signal-safe (allocation, DWARF reads — Ch. 93 §93.2.2). The right uses: the `std::set_terminate` handler and the assertion-failure path (Ch. 93 §93.4.5), with `std::basic_stacktrace<pmr_alloc>` on a Ch. 27 monotonic resource so the capture does not touch the heap, and **deferred symbolisation** — log the `stacktrace_entry::native_handle()` addresses (8 bytes each) and `to_string` them offline against the build-ID-matched binary (Ch. 5 §5.4.1), exactly as Ch. 93's crash record does with raw frames.
- **`<debugging>` (C++26, P2546; `__cpp_lib_debugging >= 202311L`) — `std::breakpoint()`, `std::breakpoint_if_debugging()`, `std::is_debugger_present()`.** Standardises the `int3`-when-attached idiom. libstdc++ 13 and 14 do not ship the header (verified: no `__cpp_lib_debugging` in `<version>` on 13.3); gate on the macro and fall back to `__builtin_debugtrap()` (Clang), `raise(SIGTRAP)`, and `TracerPid != 0` in `/proc/self/status` for `is_debugger_present`. On a trading box the fallback has a sharp edge: a `SIGTRAP` with no debugger attached is a fatal signal (Ch. 93 §93.2.2's table), so `breakpoint_if_debugging` — which checks first — is the only form that belongs in shipped code.
- **Contracts (C++26, P2900) — `pre(...)`, `post(...)`, `contract_assert(...)` with evaluation semantics *ignore*, *observe*, *enforce*, *quick_enforce*.** GCC 15 ships them as experimental (`-fcontracts`, `-fcontract-evaluation-semantic=`); Clang 18/19 do not. The hot-path stance writes itself from this chapter: *enforce* (call the violation handler, then terminate) in test and CI; *quick_enforce* (trap, no handler — a `ud2`, Ch. 93 catches it) for the preconditions whose violation means a corrupted index or an impossible price in production; *ignore* for the ones the optimizer can prove or whose cost you have measured and declined. The violation handler is a cold path and may `std::stacktrace` and log; the check itself is the same predicted compare as `assert`. Until the toolchain arrives, `assert` + `[[assume]]` under a macro that selects the semantic per build is the C++20 shape of the same policy.

The through-line: **a check is a predictable compare; a report is a cold path.** `source_location` makes the report free to *locate*; `stacktrace` makes it affordable to *explain*, if you symbolise later; `<debugging>` and contracts make the policy standard. None of them changes §21.3's conclusion about the error *channel* — values on the hot path, exceptions for the fatal — they decide what the fatal path writes down.

### 21.4.5 Passing and returning values: categories, elision and the calling convention

Every abstraction in Part III eventually becomes a function call, and the System V x86-64 ABI decides whether its arguments and result travel in registers or through memory (Ch. 4 §4.2.1 states the rule; this section measures it). *VM-measured, GCC 13.3 / Clang 18.1, `-O2 -std=c++23`; asm in §21.5.*

- **Trivially copyable and ≤ 16 bytes: registers.** `Price{int64_t}` is passed *as* its member (`lea rax, [rdi + 1]` for `Price f(Price)`); `Fill{int64_t px; int32_t qty, venue}` (16 bytes) arrives in `rdi:rsi` and returns in `rax:rdx`; `std::string_view` (two words), `std::optional<int64_t>` (value + flag → `rax` + `dl`) and `std::expected<int64_t, Err>` (GCC: `rax` + `dl`, with the error path a pair of `cmov`s) all stay in registers. This is the §19.5 proof generalised: a strong type, a view or a small sum type costs what its members cost.
- **Over 16 bytes: memory.** `Fill3{int64_t px, qty, id}` (24 bytes) is read from `[rsp + 8..24]` on entry and returned through a hidden pointer; `std::expected<Big, Err>` with a 16-byte `Big` is 24 bytes and returns through `[rdi]` with three stores. The jump from 16 to 24 bytes is a jump from two registers to a stack round-trip on *every* call and return — which is why Ch. 22 keeps `expected`'s `T` at 8 bytes plus the discriminant, and why a 24-byte `Order` is passed by `const&`.
- **A non-trivial destructor: invisible reference, however small.** `struct Guarded { int64_t v; ~Guarded(); }` is 8 bytes and is still passed by hidden pointer (`[rsi]`) and returned through `[rdi]`: the ABI requires the *caller* to own the object's storage so it can run the destructor. `std::unique_ptr<T>` by value is the famous case: the callee gets a pointer to the caller's stack slot and loads through it (`mov rax, [rdi]` then the real `T*`), and the *caller* must still test the slot and call `operator delete` after the call returns. Measured on a `noinline` callee: `T*` 0.36–0.52 ns per call, `unique_ptr<T>` by value 0.64–0.67 ns, `const unique_ptr&` 0.52–0.55 ns — a 0.1–0.3 ns tax plus a destructor branch in the caller, per call. Pass `T*`/`T&` for non-owning access; pass `unique_ptr<T>&&` only where ownership *transfers*, and not per message.
- **Value categories and elision.** A **prvalue** return (`return Order{px, 1, 2, 3};`) is *guaranteed* elided since C++17: the object is constructed directly in the caller's slot — four stores through `[rdi]`, no temporary, no move. **NRVO** (`Order o{…}; o.qty = …; return o;`) is permitted and both compilers do it here, but it is not guaranteed and is *lost* when the function can return two different named objects (`return cond ? a : b;` — the asm builds both on the stack and copies the winner with a `cmove` plus two 16-byte moves). Return a single prvalue or a single named object; never the conditional of two.
- **`[[clang::trivial_abi]]`** (Clang only) makes a class with a non-trivial destructor pass in registers anyway, with the *callee* running the destructor: `GuardedTA f(GuardedTA)` takes its argument in `rdi` and returns in `rax`, calling `~GuardedTA` inside. It is an **ABI break** (the mangled name does not change, so a TU compiled without the attribute passes the type differently — Ch. 79 §79.4 for the C-ABI waist, Ch. 25 §25.4.5 for why the flag policy must be uniform), and GCC ignores it. Inside one binary with one flag policy it is how `unique_ptr`-like handles can be passed by value for free; across a library boundary it is a landmine.

The rule for hot-path value types follows from the measurements: **trivially copyable, ≤ 16 bytes, returned as prvalues** — strong types, `string_view`, `optional<trivial>`, `expected<≤8-byte trivial, enum>` qualify; owning handles, 24-byte records and anything with a destructor are passed by reference or pointer.

### 21.4.6 Statics, `thread_local` and initialization: what the compiler emits

Three idioms hide runtime in plain declarations. *VM-measured, GCC 13.3 / Clang 18.1, `-O2 -std=c++20`, 100 M calls to a `noinline` accessor; asm in §21.5.*

- **The function-local static (magic static) pays a guard check on every call, and a lock on the first.** `const Cfg& cfg() { static const Cfg c = load(); return c; }` compiles to `movzx eax, byte ptr [guard]; test al, al; je .slow` before the address is returned: an acquire load and a predicted branch, **0.52 ns per call against 0.32–0.35 ns** for a `constinit` global read the same way. The slow path calls `__cxa_guard_acquire` — a futex-capable lock — exactly once, on the *first* call, which for a hot-path singleton is the first tick of the day (Ch. 53's cold-start list). `-fno-threadsafe-statics` removes the lock (the check becomes `cmp byte ptr [guard], 0`) but not the check, and is unsafe if two threads can race to the first call. The fix is to not have the question: a `constinit` global (Ch. 18 §18.4.1) when the value is a constant expression, or an explicit `init()` at startup writing a plain global that the hot path reads with one load.
- **Static initialization order is link order, except where you say otherwise.** Dynamic initializers run from `.init_array` in the order the linker placed the TUs — unspecified across TUs (the "static initialization order fiasco"), so a strategy's global that reads a venue table's global may run first. `[[gnu::init_priority(101)]]` (101–65535; lower runs earlier) and `__attribute__((constructor(prio)))` impose an order; `constinit`/`constexpr` globals have *no* dynamic initializer and cannot be part of the problem; a `dlopen`'d strategy `.so` (Ch. 88 §88.4.4) runs its `.init_array` inside `dlopen`, under the loader lock, on the thread that called it. Prefer constant initialization, then explicit ordered `init()` calls from `main`, and treat `init_priority` as the escape hatch.
- **`thread_local` costs depend on where the variable lives.** In the main executable the TLS model is *local-exec*: `mov rax, qword ptr fs:t@tpoff` — one load off the thread pointer, 0.52 ns for a `++counter`, identical to a plain global. In a shared object the default is *general-dynamic*: `lea rdi, t@tlsgd[rip]; call __tls_get_addr@PLT`, a call per access — **1.34–1.43 ns, 2.7×**. Building the `.so` with `-ftls-model=initial-exec` gives `mov rdx, [t@gottpoff]; mov rax, fs:[rdx]` — 0.63–0.87 ns — at the price that the library must be loaded at startup (its TLS lands in the static block; glibc keeps a small surplus for `dlopen`'d initial-exec objects and fails the load when it is exhausted). A `thread_local` in a `dlopen`'d library stays general-dynamic — **2.85–3.10 ns per access**, and its *first* access on each thread allocates that thread's DTV slot: **1.5–2.6 µs**, a `malloc` inside the hot thread's first touch. Per-thread logger rings (Ch. 84), pool handles (Ch. 27) and `errno` are all TLS; put them in the executable or an initial-exec library, touch every one at thread start (Ch. 53), or pass a per-thread context pointer explicitly and avoid the question.

## 21.5 Verify the codegen: unwinding tables and landing pads

The costs of §21.2 are visible in the binary, and the verification settles where they live. Snippets are **verified** Clang 18.1 / GCC 14.2 output (Appendix L.3; `--target=x86_64-linux-gnu -O2`).

**The happy path with a `try` is genuinely free.** A non-throwing call inside `try` emits the *same* straight-line code as without it — the error handling is off to the side:

```asm
; acc += val_throw(m);  inside try{}   — the NON-throwing path (GCC 13, inlined into the loop):
        test    r14, r14                    ; is_bad(m)? — the validation test EVERY mechanism shares
        js      .Lthrow                     ;   (the throw itself lives in val_throw.cold:
                                            ;    __cxa_allocate_exception / __cxa_throw)
        movsxd  rax, dword ptr [rsi + 8]    ; m.qty
        imul    rax, qword ptr [rsi]        ; * m.px
        add     rbx, rax                    ; += acc      — no instructions beyond that test: the
                                            ;   exception MECHANISM adds nothing to the happy path
        ; ... the catch/cleanup lives in a SEPARATE cold region + unwinding tables (below)
```

**The cost lives in cold sections and tables.** The landing pads (catch/cleanup code) and the unwinding metadata are emitted separately — visible in the section table:

```
$ g++ -O2 -std=c++23 -c errpath.cpp && readelf -SW errpath.o | grep -E 'text|eh_frame|gcc_except'
  [ ] .text             ...      <- the hot path
  [ ] .text.unlikely    ...      <- landing pads / cleanup / .cold clones (cold — Ch. 12)
  [ ] .eh_frame         ...      <- stack-unwinding descriptors (every function)
  [ ] .gcc_except_table ...      <- LSDA: which catch handles which region   (binary SIZE cost)
$ readelf -SW errpath | grep -E 'eh_frame|gcc_except'     # the linked binary: ld folds .text.unlikely
                                                            # into .text, but the tables stay visible
$ size errpath    # vs the same program built -fno-exceptions: smaller .text, no .gcc_except_table
```

This is the proof of §21.2: the *throw* machinery (tables, landing pads) is binary size sitting in cold sections; the happy path doesn't touch it. Building `-fno-exceptions` removes `.gcc_except_table` and the landing pads (`.eh_frame` stays for backtraces unless you also drop `-fasynchronous-unwind-tables`) — the measurable code-size win.

**`noexcept` removes the cleanup edge.** A call to a `noexcept` function needs no landing pad; compare the call-site codegen of a throwing vs `noexcept` callee and the latter has no exceptional edge — and the §21.2 `std::vector` move-vs-copy difference is visible in *which* constructor the reallocation loop calls:

```asm
; vector grow with NOEXCEPT move ctor:   calls the MOVE constructor (cheap, steals buffer)
; vector grow with THROWING move ctor:   calls the COPY constructor (deep copy every element)
```

The verification habits:

- **Confirm the happy path is straight-line** (no error-handling branches when nothing throws) — exceptions did their job.
- **Locate the cost**: landing pads in `.text.unlikely`, tables in `.eh_frame`/`.gcc_except_table`; measure the size delta with `-fno-exceptions`/`-fno-rtti` if code size matters (Ch. 12).
- **Verify `noexcept` move ctors** make `std::vector` reallocation *move* (read the asm or step the reallocation) — the silent copy-instead-of-move is a classic, invisible-without-checking regression.

**Registers vs the hidden pointer (§21.4.5).** `clang++ -O2 -std=c++23`; GCC 13.3 agrees on every classification:

```asm
; Price f_price(Price)          — a strong type IS its int64:        ; Fill f_fill(Fill)  — 16 bytes: two registers in, two out
        lea     rax, [rdi + 1]                                        ;         mov     rax, rdi            ; px
        ret                                                           ;         lea     ecx, [rsi + 1]      ; qty + 1 …
                                                                      ;         …                            ; … repacked into rdx with venue
; Fill3 f_fill3(Fill3)          — 24 bytes: MEMORY both ways:         ; Guarded f_guarded(Guarded) — 8 bytes + a destructor: invisible reference
        mov     rax, rdi                    ; hidden return pointer   ;         mov     rax, rdi            ; hidden return pointer
        inc     qword ptr [rsp + 16]        ; the argument is ON THE STACK      mov     rcx, qword ptr [rsi]  ; the argument is BEHIND A POINTER
        movaps  xmm0, xmmword ptr [rsp + 8] ; copy 24 bytes out…      ;         inc     rcx
        movups  xmmword ptr [rdi], xmm0     ; …through the pointer    ;         mov     qword ptr [rsi], rcx ; (caller's copy, so its dtor sees it)
                                                                      ;         mov     qword ptr [rdi], rcx
; int64 use_uptr(unique_ptr<int>) — pointer to a pointer:             ; int64 use_ptr(int*) — the pointer:
        mov     rax, qword ptr [rdi]        ; load the T* from the caller's slot;         test    rdi, rdi
        test    rax, rax                                              ;         je      .null
        movsxd  rax, dword ptr [rax]        ; then the value           ;         movsxd  rax, dword ptr [rdi]
;  …and the CALLER, after the call:  mov rdi, [rsp]; test rdi, rdi; je …; call operator delete   — the destructor it still owns
; Order make_order(int64)        — prvalue: guaranteed elision, built in place:   ; make_order_no_nrvo — `return c ? a : b`: both built, one copied
        mov     rax, rdi                                              ;         mov     qword ptr [rsp - 32], rsi  … (8 stores for two Orders)
        mov     qword ptr [rdi], rsi                                  ;         cmove   rdx, rcx                   ; pick the address
        mov     qword ptr [rdi + 8], 1                                ;         movups  xmm0, xmmword ptr [rdx]    ; copy the winner
        mov     qword ptr [rdi + 16], 2                               ;         movups  xmm1, xmmword ptr [rdx + 16]
        mov     qword ptr [rdi + 24], 3                               ;         movups  xmmword ptr [rdi], xmm0 …
; optional<int64> f_opt(int64)  — rax + dl, no memory:                ; expected<Big,Err> f_exp_big — 24 bytes: sret, three stores through [rdi]
        test    rdi, rdi
        setns   dl                          ; engaged flag
        lea     rax, [rdi + 1]
```

**Statics and TLS (§21.4.6).** `g++ -O2 -std=c++20`:

```asm
; const Cfg& cfg_magic()   — function-local static:             ; const Cfg& cfg_global() — constinit global:
        movzx   eax, byte ptr [_ZGVZ9cfg_magicvE1c]  ; guard byte (acquire)       lea     rax, [g_cfg]
        test    al, al                                                       ;         ret
        je      .Linit                               ; first call only →
        lea     rax, [_ZZ9cfg_magicvE1c]             ;   __cxa_guard_acquire / load_cfg / __cxa_guard_release
        ret
; thread_local in the EXECUTABLE (local-exec):                  ; in a -fPIC .so (general-dynamic, the default):
        mov     rax, qword ptr fs:t_exe@tpoff        ; one load     data16 lea rdi, t_counter@tlsgd[rip]
        add     rax, 1                                            ; rex64 call __tls_get_addr@PLT   ; a CALL per access
        mov     qword ptr fs:t_exe@tpoff, rax                    ;         mov     rax, qword ptr [rax]
; in a .so built -ftls-model=initial-exec:
        mov     rdx, qword ptr t_counter@gottpoff[rip] ; offset from the GOT
        mov     rax, qword ptr fs:[rdx]               ; one fs-relative load
```

What to look for: `[rsp + …]` reads on entry or an `sret` pointer in `rdi` where you expected registers (a type crossed 16 bytes or grew a destructor); a `movzx` of a `_ZGV…` guard symbol in a hot accessor (`nm -C | grep 'guard variable'` lists them all); `call __tls_get_addr` anywhere in a hot `.so` (`objdump -d lib.so | grep -c __tls_get_addr`; Appendix I.12).

## 21.6 Pitfalls & anti-patterns: throwing on the hot path

- **Throwing on a frequent condition.** The headline disaster: using exceptions for malformed packets, sequence gaps, full queues, rejects — anything that happens with any regularity. Each throw is microseconds and non-deterministic (§21.2–§21.3); even 0.1% error rates tank the tail (Ch. 1). Use `std::expected`/error codes for *expected* failures.
- **Non-`noexcept` move constructor → silent copies.** Forgetting `noexcept` on a move ctor/swap makes `std::vector` (and friends) **copy** on every reallocation instead of move (§21.2, §21.4.1) — a huge, invisible cost. Always `noexcept` your move operations.
- **`dynamic_cast` on the hot path.** A runtime type-walk where static dispatch (Ch. 19) or a `variant` visitor (Ch. 14) would do. Replace it; reserve `dynamic_cast` for cold paths if at all.
- **Ignoring an `expected`/error code.** The value-return channel only works if you *check* it. `[[nodiscard]]` on `expected`-returning functions (a warning — make it `-Werror`), and `and_then`/`or_else` chaining, prevent the "forgot to check" bug (and never `*e` without `has_value()`: that is UB on the error state, not a check) that exceptions avoid by construction. (This is the one thing exceptions are genuinely better at — don't throw away the discipline.)
- **Using exceptions for control flow.** `throw` to break out of loops / signal normal outcomes is the anti-pattern's general form — it's a *very* expensive `goto`. Exceptions are for *exceptional*, ideally fatal, conditions.
- **`-fno-exceptions`/`-fno-rtti` cargo-culted.** These are whole-program stances with real costs (no exceptions *anywhere*, including in libraries; some code/libs require RTTI). Adopt them deliberately for measured code-size/determinism wins with an all-value error strategy — not reflexively.
- **Heavy error type in `expected`.** A large/non-trivially-copyable `E` bloats every `expected` return and copy (Ch. 9). Keep `E` a small enum/struct on the hot path.
- **Assuming exceptions are "slow" (or "free").** Both half-truths mislead. Exceptions are *free on the happy path and expensive when thrown*. The engineering is putting the throw where throws are rare; "never use exceptions" and "exceptions are fine everywhere" are both wrong.
- **Throwing across an ABI/`noexcept` boundary.** A throw escaping a `noexcept` function calls `std::terminate`; a throw across a C ABI boundary is UB. Keep throws within their exception-aware region.
- **`unique_ptr<T>` (or any destructor-bearing type) by value on the per-message path.** Invisible reference in, a destructor test and possible `operator delete` in the caller out — 0.1–0.3 ns and a branch per call (§21.4.5). `T*`/`T&` for access; `&&` only where ownership moves.
- **A hot `expected`/`optional`/record that crosses 16 bytes.** It left the registers for a stack round-trip on every call (§21.4.5, §21.5); keep `T` ≤ 8 bytes beside the discriminant, or pass by reference.
- **`return cond ? a : b;` of two named locals.** NRVO is lost; both are built and one is copied (§21.5). One named return object, or two `return` statements.
- **The function-local static singleton on the hot path.** A guard load and branch per call, and `__cxa_guard_acquire` on the first tick (§21.4.6). `constinit`, or initialise at startup into a plain global.
- **`thread_local` in a `dlopen`'d strategy or a default-built `.so`.** `__tls_get_addr` per access (2.7–6×) and a `malloc` on each thread's first touch (§21.4.6). Executable or `-ftls-model=initial-exec`, and pre-touch at thread start.
- **Cross-TU dynamic initialization order assumed.** A global's constructor that reads another TU's global reads it uninitialized on the wrong link order (§21.4.6). Constant-initialise, or order explicitly from `main`.

## 21.7 Exercises & checklist

**Exercises**

1. **Map the cliff.** Build `errpath.cpp`; sweep `bad_per_1000` ∈ {0, 1, 10, 50, 200} for `throw`/`code`/`expected`. Plot ns/msg vs error rate. At what error rate does `throw` cross the value-returning mechanisms? Confirm `code`/`expected` are flat (§21.3).
2. **Happy path is free.** At `bad_per_1000 = 0`, confirm all three mechanisms are within noise. Disassemble the `try` happy path (Ch. 4) and confirm it's straight-line with no error checks (§21.5).
3. **The `noexcept` move trap.** Build a type with a *throwing* move ctor and a `noexcept` one; `push_back` past several reallocations into a `std::vector`. Count copy vs move ctor calls (instrument them). Show the non-`noexcept` version copies (§21.2, §21.4.1).
4. **Size the tables.** Build the program with and without `-fno-exceptions` (and `-fno-rtti`); compare `size` and `readelf -S` (`.eh_frame`/`.gcc_except_table`). How much binary size do exceptions/RTTI cost here (§21.5)?
5. **`expected` pipeline.** Rewrite a parse→validate→enqueue chain (Ch. 60) using `std::expected` + `and_then`/`or_else`. Confirm it compiles to branch-on-value (no throw) and that `[[nodiscard]]` turns a dropped result into a compiler warning (build with `-Werror`; note that it cannot catch an unchecked `*e`, which is UB on the error state).

**Checklist — the cost of abstractions**

- [ ] Error mechanism matches **frequency**: `std::expected`/error codes for **expected** failures (malformed input, gaps, rejects, capacity); exceptions only for **rare/fatal** conditions.
- [ ] **No `throw` on the steady-state hot path** for conditions that occur with any regularity (§21.6); the throw lives on the cold path.
- [ ] Hot-path functions are **`noexcept`**, and **all move ctors/`swap`** are `noexcept` (so `std::vector` moves, not copies — §21.4.1).
- [ ] Hot-path `expected` uses a **small, trivially-copyable `E`**, is **`[[nodiscard]]`**, and composes via `and_then`/`or_else`; the error state is never dereferenced unchecked.
- [ ] **No `dynamic_cast`** on the hot path — static dispatch (Ch. 19) or `variant` visitor (Ch. 14) instead.
- [ ] I verified the **happy path is straight-line** (Ch. 4) and know where the **unwinding tables/landing pads** live (`.eh_frame`/`.gcc_except_table`/`.text.unlikely` — §21.5).
- [ ] `-fno-exceptions`/`-fno-rtti` (if used) is a **deliberate, measured** whole-program stance with an all-value error strategy (Ch. 24), not cargo-culted.
- [ ] Hot-path value types are **trivially copyable and ≤ 16 bytes** so they ride in registers; records above that and owning handles are passed by **reference/pointer**, never `unique_ptr` by value per message (§21.4.5).
- [ ] Hot functions return a **single prvalue or single named object** (guaranteed elision / NRVO), never the conditional of two (§21.4.5).
- [ ] **No function-local `static` on the hot path** — state is `constinit` or initialised at startup — and `nm -C | grep 'guard variable'` on the hot TU is empty (§21.4.6).
- [ ] Every `thread_local` the hot path touches lives in the **executable or an initial-exec library** and is **pre-touched at thread start**; `objdump -d | grep __tls_get_addr` on hot objects is empty (§21.4.6).
- [ ] I treat exceptions as **free-until-thrown**, not as "slow" or "free everywhere" — the throw is placed where it's rare.

## 21.8 References

- The Itanium C++ ABI (exception handling) and the GCC/Clang documentation on the zero-cost exception model, `.eh_frame`/`.gcc_except_table`, and `-fno-exceptions`/`-fno-rtti` — the machinery of §21.2 and §21.5.
- ISO C++ / cppreference — `std::expected` (C++23), `std::unexpected`, `noexcept` and the noexcept operator, and the container exception-safety guarantees that make `noexcept` moves matter (§21.2, §21.4).
- H. Sutter, *"Zero-overhead deterministic exceptions"* (P0709) and related papers — the cost analysis of exceptions and proposals motivating value-based error handling on the hot path.
- B. Stroustrup / the C++ Core Guidelines (error handling) — when to use exceptions vs error codes vs `expected`, and the `noexcept` guidance of §21.4.1.
- A. Fog, *Optimizing software in C++* — measured costs of exceptions, RTTI, and `dynamic_cast`, and the binary-size effects (§21.3, §21.5).

## 21.9 Additional Reading

- CppCon talks on exception cost, `std::expected`, and "zero-overhead exceptions" — including asm-level walkthroughs of the throw path and unwinding tables.
- Ch. 19 (*Zero-Cost Abstractions*) — the abstractions that *are* free, the counterweight to this chapter; Ch. 12 (*Code Layout*) — where landing pads / cold unwinding code belong; Ch. 60 (*Zero-Copy Parsing*) — hot-path error handling on untrusted input with `expected`; Ch. 87 (*Secure Programming*) — UB and error handling as a vulnerability class; Ch. 24 (*Build Toolchain*) — `-fno-exceptions`/`-fno-rtti` in context.
- **Appendix D** (Compiler Flag Reference) — `-fno-exceptions`/`-fno-rtti` and their costs; **Ch. 80 §80.4.5** and **Ch. 81 §81.4.3** — error handling in Rust (`Result`/`?`) and Zig (error unions), and what exceptions and panics may not do at a C ABI boundary; **Appendix B** (B.3): the value-based model without exceptions.

---

*Next: Ch. 22 — The Cost of Modern Idioms & Strings Without Allocation: lambdas, `std::ranges` pipelines, `std::function`, `std::apply`/`tuple`, `string_view`/`optional`/`expected` by value, measured on one book loop — which of them the optimizer collapses, where a `views::filter` takes the vectorizer away, and why a symbol on the hot path is a `uint64_t`, not a `std::string`.*
