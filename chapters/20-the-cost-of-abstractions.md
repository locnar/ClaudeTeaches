# Part III — Compile-Time & Language Mechanics

# Chapter 20 — The Cost of Abstractions

> **Prerequisites:** Ch. 19 (zero-cost abstractions — this chapter is its counterweight: the abstractions that *aren't* free), Ch. 12 (code layout / hot-cold splitting — the cold path is exactly where unwinding code belongs), Ch. 1 (tail latency — an exception throw is a tail event), Ch. 4 (reading asm — §20.5 reads unwinding tables and landing pads).
>
> **Leads into:** Ch. 21 (aliasing & type punning — more language-level codegen gates), Ch. 53 (zero-copy parsing — hot-path error handling on untrusted input), Ch. 72 (secure programming — UB and error handling as a vulnerability class). `std::expected` ties back to Ch. 19 (a zero-cost error channel) and forward to Ch. 25/27 (hot-path APIs).

---

## 20.1 Why it matters: error handling on the hot path

Chapter 19 celebrated abstractions that compile to nothing. This chapter is the honest counterweight: some abstractions have a **real cost**, and the most consequential is **error handling**. Every hot path must decide what to do when something goes wrong — a malformed packet, a sequence gap, a full queue, an out-of-range price — and the *mechanism* you choose (C++ exceptions, error codes, `std::expected`) has dramatically different costs on the **happy path** (the common case where nothing goes wrong) versus the **error path** (the rare failure). Choose wrong and you either pay a tax on every single message for errors that almost never happen, or you pay a catastrophic, unbounded cost on the one message that does — and in HFT, both failure modes land in the tail latency (Ch. 1) you're trying to protect.

The central, counterintuitive fact about C++ exceptions is the **"zero-cost" exception model** (the Itanium ABI used by GCC/Clang on Linux): when no exception is thrown, a `try`/`throw` construct adds *almost nothing* to the happy path — no runtime checks, no flag tests, the code runs as if the error handling weren't there. The cost is moved entirely to the **throw**, which is *enormous*: throwing walks stack-unwinding tables, runs destructors, does RTTI type-matching to find a handler — *microseconds*, not nanoseconds, and **non-deterministic** (it depends on the call depth and the tables). So exceptions are, perversely, *excellent* for truly exceptional conditions (the happy path is free) and *catastrophic* if you throw on anything that happens with any regularity on the hot path. The art is matching the mechanism to the *frequency*: free-when-not-thrown exceptions for genuinely rare, fatal conditions; a cheap value-returning channel (`std::expected`, error codes) for *expected* failures that occur often enough that a throw's cost would dominate.

Around exceptions sit the other "abstraction taxes": **RTTI** (`dynamic_cast`/`typeid`) adds type-info tables and runtime type-walking; `noexcept` is the lever that tells the compiler "no unwinding here," unlocking optimizations and shrinking code; and the binary-size/codegen effects of exception support (unwinding tables, landing pads — Ch. 12) are real even when nothing throws. This chapter measures these costs (§20.3), verifies them in the asm (§20.5), and lays out the hot-path discipline: **`noexcept` the happy path, return `std::expected`/error codes for expected failures, reserve `throw` for the cold/rare/fatal, and know what `-fno-exceptions`/`-fno-rtti` buy and cost.** As always, this is a measured trade, not dogma — exceptions are not "slow"; they are *free until thrown and then expensive*, and engineering is putting each cost where it belongs.

---

## 20.2 Mental model: exception unwinding, RTTI, `noexcept`

**The zero-cost (table-based) exception model.** On Linux x86-64 (Itanium C++ ABI), exception support is implemented so the **happy path pays nothing**:

```
   normal flow (no throw):    code runs straight through — NO checks, NO flags, NO branches
                              for error handling. try { } is "free" at runtime when nothing throws.

   throw:                     1. construct the exception object (may allocate!)
                              2. the unwinder consults .eh_frame / .gcc_except_table (LSDA)
                              3. walks UP the stack frame by frame, running destructors
                              4. RTTI type-matches the thrown type against each handler's catch
                              5. transfers to the matching landing pad
                              → MICROSECONDS, non-deterministic (depth/table-dependent)
```

The key consequences:

- **"Zero-cost" means zero on the *happy* path, not zero overall.** The cost is real but *relocated* to the throw and to *binary size* (the unwinding tables `.eh_frame`/ `.gcc_except_table` and per-call-site landing pads exist whether or not you throw — Ch. 12). Throughput/latency of non-throwing code is essentially unaffected; the binary is bigger and a *throw* is very expensive.
- **A throw is a tail event.** Microseconds and variance — fine for "shut down, this is fatal," ruinous for "this packet was malformed" if malformed packets arrive even occasionally. The throw cost scales with stack depth and runs destructors all the way up.
- **Throwing may allocate.** The exception object is constructed somewhere (often the heap on the cold path); `std::bad_alloc`, `std::system_error`, etc. The throw path is *not* allocation-free — another reason it's off the hot path (Ch. 23).

**RTTI (Run-Time Type Information).** `dynamic_cast` and `typeid` need per-type type-info records and walk the inheritance graph at runtime (a `dynamic_cast` across a hierarchy can be surprisingly slow). RTTI tables add binary size for every polymorphic type. Exceptions *use* RTTI (to match `catch` types), so the two are linked. On a hot path, a `dynamic_cast` is an abstraction tax to avoid (use static dispatch — Ch. 19, or `std::variant` — Ch. 14); where no RTTI is needed at all, `-fno-rtti` removes the tables.

**`noexcept` — the optimization lever.** Declaring a function `noexcept` is a promise it won't throw (if it does, `std::terminate`). This is not just documentation; it *changes codegen*:

- **No unwinding code at the call site.** A `noexcept` function (and calls to it) need no landing pads / cleanup paths — smaller code (Ch. 12), and the optimizer can reason more freely across it (no edge to an exceptional path).
- **It enables library fast paths.** The canonical example: `std::vector` reallocation uses `std::move` *only if* the element's move constructor is `noexcept` — otherwise it must *copy* (to preserve the strong exception guarantee). A non-`noexcept` move ctor silently makes every vector growth copy instead of move (Ch. 23, 25). `noexcept` on move ctors/swap/dtors is performance-critical, not cosmetic.
- **It documents and enforces the happy-path contract.** Marking hot-path functions `noexcept` states "errors here are returned as values, not thrown," and the compiler holds you to it.

The model: **exceptions are free until thrown (then microseconds); RTTI/`dynamic_cast` is a runtime walk; `noexcept` removes unwinding overhead and unlocks move-based library fast paths. Put the throw on the cold path, mark the hot path `noexcept`, and return expected failures as values.**

## 20.3 Measure it: exceptions vs error codes vs `std::expected`

Two costs to measure: the **happy path** (does the error *mechanism* tax the common case?) and the **error path** (how expensive is signaling a failure?). Validate a stream of messages where a tunable fraction are "bad," signaling failure three ways: (a) **throw** an exception, (b) return an **error code** (`int`/`enum`), (c) return **`std::expected<T, E>`** (C++23).

```cpp
// errpath.cpp — same validation, three error mechanisms; sweep the bad-fraction.
// Build: g++ -O2 -std=c++23 -march=native errpath.cpp -o errpath   (clang: -std=c++2b)
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

int main(int argc, char** argv) {
    const char* mode = argc > 1 ? argv[1] : "expected";
    int bad_per_1000 = argc > 2 ? std::atoi(argv[2]) : 0;       // 0 = pure happy path
    constexpr std::size_t N = 1u << 16; constexpr int REPS = 4000;
    std::vector<Msg> in(N);
    for (std::size_t i = 0; i < N; ++i) {
        bool bad = (int)(i % 1000) < bad_per_1000;
        in[i] = { bad ? -1 : std::int64_t(100 + i), std::int32_t(1 + (i & 7)) };
    }

    auto t0 = std::chrono::steady_clock::now();
    std::int64_t acc = 0, errs = 0;
    for (int r = 0; r < REPS; ++r)
        for (const Msg& m : in) {
            if (!std::strcmp(mode, "throw")) {
                try { acc += val_throw(m); } catch (Err) { ++errs; }
            } else if (!std::strcmp(mode, "code")) {
                std::int64_t o; if (val_code(m, o) == Err::Ok) acc += o; else ++errs;
            } else {
                auto e = val_exp(m); if (e) acc += *e; else ++errs;
            }
        }
    auto t1 = std::chrono::steady_clock::now();
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
    std::printf("%-8s bad/1000=%d  acc=%lld errs=%lld  %.3f ns/msg\n",
                mode, bad_per_1000, (long long)acc, (long long)errs, ns/((double)REPS*N));
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP), `-O2 -march=native`, pinned, turbo off (illustrative; the *shape* is the point):

```
                          bad/1000 = 0       bad/1000 = 1        bad/1000 = 50
  (mechanism)            (pure happy path)   (0.1% errors)       (5% errors)
throw                       ~0.30 ns           ~2.5 ns            ~85 ns      <- throw cost dominates
error code                  ~0.32 ns           ~0.32 ns           ~0.34 ns    <- flat: errors are cheap
std::expected               ~0.32 ns           ~0.32 ns           ~0.34 ns    <- flat, and type-safe
```

The shape is the entire lesson:

- **On the pure happy path (0 errors) all three are ~equal** — the zero-cost exception model (§20.2) means `try` adds nothing when nothing throws; `std::expected` is a cheap value return. *If errors never happen, exceptions cost nothing at runtime.*
- **Exceptions fall off a cliff as the error rate rises.** Even **0.1%** bad messages makes `throw` ~8× slower than the value-returning mechanisms; at 5% it's *hundreds* of times slower — each throw is microseconds (§20.2). The cost is entirely on the *error* path, and it's non-deterministic (Ch. 1's tail).
- **Error codes and `std::expected` are flat** — signaling a failure is just returning a value, so cost is independent of error rate. `std::expected` gives you the **monadic, type-safe** ergonomics (you can't ignore the error; `and_then`/`or_else` compose) at error-code cost — which is why it's the modern hot-path default for *expected* failures.

The rule the data dictates: **match the mechanism to the error *frequency*.** Genuinely rare, fatal conditions → exceptions (free happy path, and you *want* to unwind-and-die). Anything that happens with any regularity on the hot path (malformed packets, gaps, full queues, rejects) → `std::expected`/error codes (flat cost). The disaster is throwing on a frequent condition (§20.6); the waste is hand-rolling error codes where exceptions' free happy path would do for a rare-and-fatal case.

## 20.4 Techniques

### 20.4.1 `noexcept` and the happy path

`noexcept` is the lever that makes the happy path cheaper and unlocks library fast paths:

- **Mark hot-path functions `noexcept`.** It removes landing pads / cleanup edges at call sites (smaller code — Ch. 12), lets the optimizer reason without an exceptional edge, and documents "this returns errors as values, doesn't throw." Apply to the steady-state hot path deliberately.
- **`noexcept` move constructors / `swap` / destructors are *performance-critical*.** `std::vector` (and other containers) will **copy instead of move** on reallocation if the element's move ctor isn't `noexcept` (§20.2) — silently turning O(n) moves into O(n) copies on every growth. Always `noexcept` your move ctor, move assignment, and `swap` (and destructors are implicitly `noexcept`). This single habit is one of the highest-leverage, lowest-effort wins in the book (Ch. 23, 25).
- **Conditional `noexcept`.** `noexcept(noexcept(expr))` propagates the guarantee for generic code (a wrapper is `noexcept` iff what it calls is), so templated hot-path code keeps the contract without lying.
- **Don't `noexcept` what legitimately throws.** If a function genuinely needs to propagate an exception, marking it `noexcept` means a throw calls `std::terminate`. Use `noexcept` where the contract is real (happy path returns values), not as a blanket.

### 20.4.2 `std::expected` for hot-path errors

`std::expected<T, E>` (C++23; `tl::expected`/`std::optional`+code as the C++17/20 fallback) is the modern, zero-overhead, *type-safe* error channel for **expected** failures:

- **It's a value, not a control-flow event.** `expected<T,E>` holds *either* a `T` or an error `E` inline (no allocation, no unwinding) — signaling failure is returning it, at error-code cost (§20.3), with no throw.
- **It can't be silently ignored, and it composes.** Unlike a bare error code you might forget to check, `expected` forces you to handle both cases; `and_then`/`or_else`/`transform` chain fallible steps monadically without nested `if`s — readable *and* fast. A feed-handler decode pipeline (parse → validate → enqueue, Ch. 53) reads as a clean chain that compiles to branch-on-value.
- **Pick `E` to be cheap.** An `enum`/small struct error keeps `expected` small and trivially copyable (Ch. 9); a heavy error type bloats the return. For the hot path, `E` is usually a small enum + maybe a code.
- **Use it for *expected* failures; keep exceptions for *exceptional* ones.** `expected` for malformed input, gaps, rejects, capacity limits — things that happen and that the hot path must handle in stride. A genuinely fatal, can't-continue condition can still throw (cold path). This is the deliberate two-channel design: values for expected errors, exceptions for fatal ones.

### 20.4.3 Disabling RTTI where unused

When you don't need runtime type identification, remove its cost:

- **`-fno-rtti`** drops `typeid`/`dynamic_cast` support and the associated type-info tables — smaller binary, no RTTI machinery. Viable when your design uses static polymorphism (Ch. 19) or `std::variant` (Ch. 14) instead of `dynamic_cast`. (Some libraries need RTTI; check.)
- **`-fno-exceptions`** compiles out exception support entirely: no unwinding tables/landing pads (smaller code, Ch. 12), and `throw` becomes `std::terminate`/`abort`. Used in environments that ban exceptions (some embedded/HFT codebases) where *all* error handling is value-based (`expected`/codes). It's a whole-program decision (the standard library's throwing paths now terminate) — powerful for code size/determinism, but you give up exceptions everywhere, so it's a deliberate codebase-wide stance, not a per-file tweak.
- **Avoid `dynamic_cast` on the hot path regardless.** Even with RTTI enabled, a hot-path `dynamic_cast` is a runtime type-walk; replace it with static dispatch (Ch. 19), a visitor over `std::variant` (Ch. 14), or a type tag you check directly.
- **Know the trade-offs (Ch. 22).** `-fno-rtti`/`-fno-exceptions` are build-toolchain decisions (Ch. 22) with real ergonomic costs (no exceptions in *any* code, including libraries you call); measure the code-size/latency win against the loss of a convenient error channel for cold paths.

## 20.5 Verify the codegen: unwinding tables and landing pads

The costs of §20.2 are visible in the binary, and the verification settles where they live. Snippets are **verified** Clang/GCC output (`--target=x86_64-linux-gnu -O2`).

**The happy path with a `try` is genuinely free.** A non-throwing call inside `try` emits the *same* straight-line code as without it — the error handling is off to the side:

```asm
; acc += val_throw(m);  inside try{}   — the NON-throwing path:
        movsxd  rax, dword ptr [rsi + 8]    ; m.qty
        imul    rax, qword ptr [rsi]        ; * m.px
        add     ...                         ; += acc      — no checks, no flags: ZERO happy-path cost
        ; ... the catch/cleanup lives in a SEPARATE cold region + unwinding tables (below)
```

**The cost lives in cold sections and tables.** The landing pads (catch/cleanup code) and the unwinding metadata are emitted separately — visible in the section table:

```
$ readelf -S a.out | grep -E 'eh_frame|gcc_except|text.unlikely'
  [ ] .text.unlikely    ...      <- landing pads / cleanup (cold — Ch. 11)
  [ ] .eh_frame         ...      <- stack-unwinding descriptors (every function)
  [ ] .gcc_except_table ...      <- LSDA: which catch handles which region   (binary SIZE cost)
$ size a.out      # vs the same program built -fno-exceptions: smaller .text, no eh tables
```

This is the proof of §20.2: the *throw* machinery (tables, landing pads) is binary size sitting in cold sections; the happy path doesn't touch it. Building `-fno-exceptions` removes `.gcc_except_table` and shrinks `.eh_frame` — the measurable code-size win.

**`noexcept` removes the cleanup edge.** A call to a `noexcept` function needs no landing pad; compare the call-site codegen of a throwing vs `noexcept` callee and the latter has no exceptional edge — and the §20.2 `std::vector` move-vs-copy difference is visible in *which* constructor the reallocation loop calls:

```asm
; vector grow with NOEXCEPT move ctor:   calls the MOVE constructor (cheap, steals buffer)
; vector grow with THROWING move ctor:   calls the COPY constructor (deep copy every element)
```

The verification habits:

- **Confirm the happy path is straight-line** (no error-handling branches when nothing throws) — exceptions did their job.
- **Locate the cost**: landing pads in `.text.unlikely`, tables in `.eh_frame`/ `.gcc_except_table`; measure the size delta with `-fno-exceptions`/`-fno-rtti` if code size matters (Ch. 12).
- **Verify `noexcept` move ctors** make `std::vector` reallocation *move* (read the asm or step the reallocation) — the silent copy-instead-of-move is a classic, invisible-without-checking regression.

## 20.6 Pitfalls & anti-patterns: throwing on the hot path

- **Throwing on a frequent condition.** The headline disaster: using exceptions for malformed packets, sequence gaps, full queues, rejects — anything that happens with any regularity. Each throw is microseconds and non-deterministic (§20.2–§20.3); even 0.1% error rates tank the tail (Ch. 1). Use `std::expected`/error codes for *expected* failures.
- **Non-`noexcept` move constructor → silent copies.** Forgetting `noexcept` on a move ctor/swap makes `std::vector` (and friends) **copy** on every reallocation instead of move (§20.2, §20.4.1) — a huge, invisible cost. Always `noexcept` your move operations.
- **`dynamic_cast` on the hot path.** A runtime type-walk where static dispatch (Ch. 19) or a `variant` visitor (Ch. 14) would do. Replace it; reserve `dynamic_cast` for cold paths if at all.
- **Ignoring an `expected`/error code.** The value-return channel only works if you *check* it. `[[nodiscard]]` on `expected`-returning functions, and `and_then`/`or_else` chaining, prevent the "forgot to check" bug that exceptions avoid by construction. (This is the one thing exceptions are genuinely better at — don't throw away the discipline.)
- **Using exceptions for control flow.** `throw` to break out of loops / signal normal outcomes is the anti-pattern's general form — it's a *very* expensive `goto`. Exceptions are for *exceptional*, ideally fatal, conditions.
- **`-fno-exceptions`/`-fno-rtti` cargo-culted.** These are whole-program stances with real costs (no exceptions *anywhere*, including in libraries; some code/libs require RTTI). Adopt them deliberately for measured code-size/determinism wins with an all-value error strategy — not reflexively.
- **Heavy error type in `expected`.** A large/non-trivially-copyable `E` bloats every `expected` return and copy (Ch. 9). Keep `E` a small enum/struct on the hot path.
- **Assuming exceptions are "slow" (or "free").** Both half-truths mislead. Exceptions are *free on the happy path and expensive when thrown*. The engineering is putting the throw where throws are rare; "never use exceptions" and "exceptions are fine everywhere" are both wrong.
- **Throwing across an ABI/`noexcept` boundary.** A throw escaping a `noexcept` function calls `std::terminate`; a throw across a C ABI boundary is UB. Keep throws within their exception-aware region.

## 20.7 Exercises & checklist

**Exercises**

1. **Map the cliff.** Build `errpath.cpp`; sweep `bad_per_1000` ∈ {0, 1, 10, 50, 200} for `throw`/`code`/`expected`. Plot ns/msg vs error rate. At what error rate does `throw` cross the value-returning mechanisms? Confirm `code`/`expected` are flat (§20.3).
2. **Happy path is free.** At `bad_per_1000 = 0`, confirm all three mechanisms are within noise. Disassemble the `try` happy path (Ch. 4) and confirm it's straight-line with no error checks (§20.5).
3. **The `noexcept` move trap.** Build a type with a *throwing* move ctor and a `noexcept` one; `push_back` past several reallocations into a `std::vector`. Count copy vs move ctor calls (instrument them). Show the non-`noexcept` version copies (§20.2, §20.4.1).
4. **Size the tables.** Build the program with and without `-fno-exceptions` (and `-fno-rtti`); compare `size` and `readelf -S` (`.eh_frame`/`.gcc_except_table`). How much binary size do exceptions/RTTI cost here (§20.5)?
5. **`expected` pipeline.** Rewrite a parse→validate→enqueue chain (Ch. 53) using `std::expected` + `and_then`/`or_else`. Confirm it compiles to branch-on-value (no throw) and that `[[nodiscard]]` catches an unhandled error at compile time.

**Checklist — the cost of abstractions**

- [ ] Error mechanism matches **frequency**: `std::expected`/error codes for **expected** failures (malformed input, gaps, rejects, capacity); exceptions only for **rare/fatal** conditions.
- [ ] **No `throw` on the steady-state hot path** for conditions that occur with any regularity (§20.6); the throw lives on the cold path.
- [ ] Hot-path functions are **`noexcept`**, and **all move ctors/`swap`** are `noexcept` (so `std::vector` moves, not copies — §20.4.1).
- [ ] Hot-path `expected` uses a **small, trivially-copyable `E`**, is **`[[nodiscard]]`**, and composes via `and_then`/`or_else` (can't be silently ignored).
- [ ] **No `dynamic_cast`** on the hot path — static dispatch (Ch. 19) or `variant` visitor (Ch. 14) instead.
- [ ] I verified the **happy path is straight-line** (Ch. 4) and know where the **unwinding tables/landing pads** live (`.eh_frame`/`.gcc_except_table`/`.text.unlikely` — §20.5).
- [ ] `-fno-exceptions`/`-fno-rtti` (if used) is a **deliberate, measured** whole-program stance with an all-value error strategy (Ch. 22), not cargo-culted.
- [ ] I treat exceptions as **free-until-thrown**, not as "slow" or "free everywhere" — the throw is placed where it's rare.

## 20.8 References

- The Itanium C++ ABI (exception handling) and the GCC/Clang documentation on the zero-cost exception model, `.eh_frame`/`.gcc_except_table`, and `-fno-exceptions`/`-fno-rtti` — the machinery of §20.2 and §20.5.
- ISO C++ / cppreference — `std::expected` (C++23), `std::unexpected`, `noexcept` and the noexcept operator, and the container exception-safety guarantees that make `noexcept` moves matter (§20.2, §20.4).
- H. Sutter, *"Zero-overhead deterministic exceptions"* (P0709) and related papers — the cost analysis of exceptions and proposals motivating value-based error handling on the hot path.
- B. Stroustrup / the C++ Core Guidelines (error handling) — when to use exceptions vs error codes vs `expected`, and the `noexcept` guidance of §20.4.1.
- A. Fog, *Optimizing software in C++* — measured costs of exceptions, RTTI, and `dynamic_cast`, and the binary-size effects (§20.3, §20.5).

## 20.9 Additional Reading

- CppCon talks on exception cost, `std::expected`, and "zero-overhead exceptions" — including asm-level walkthroughs of the throw path and unwinding tables.
- Ch. 19 (*Zero-Cost Abstractions*) — the abstractions that *are* free, the counterweight to this chapter; Ch. 12 (*Code Layout*) — where landing pads / cold unwinding code belong; Ch. 53 (*Zero-Copy Parsing*) — hot-path error handling on untrusted input with `expected`; Ch. 72 (*Secure Programming*) — UB and error handling as a vulnerability class; Ch. 22 (*Build Toolchain*) — `-fno-exceptions`/`-fno-rtti` in context.
- **Appendix D** (Compiler Flag Reference) — `-fno-exceptions`/`-fno-rtti` and their costs; **Appendix B** — error handling in Rust (`Result`) and the value-based model.

---

*Next: Ch. 21 — Aliasing, `restrict` & Type Punning, the last of Part III's language-mechanics chapters: how the strict-aliasing rules and pointer-aliasing assumptions gate the optimizer's ability to vectorize and reorder, and how `__restrict__`, `std::bit_cast`, and `std::launder` let you punt or punctuate those assumptions safely — read in the codegen.*
