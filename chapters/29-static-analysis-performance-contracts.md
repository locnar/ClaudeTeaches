# Part III — Compile-Time & Language Mechanics

# Chapter 29 — Enforcing the Hot-Path Rules: Static Analysis, Performance Linters & Contracts in Code

> **Prerequisites:** Ch. 21 §21.4.1 (compile-time validation — the one bullet this chapter grows into §29.4.3), Ch. 11 §11.3 (`static_assert` layout pins), Ch. 24 (`noexcept` as a contract; the cost of exceptions), Ch. 30 §30.5 (the `operator new` counter), Ch. 28 (the build system that runs these checks; hermetic toolchains so a pinned linter means something), Ch. 41 (`is_always_lock_free`), Ch. 44 (`hardware_destructive_interference_size`), Ch. 3 (calibration: a flagged copy is a measurement, not a sin).
>
> **Leads into:** Ch. 53 (dynamic correctness tooling — sanitizers, TSan, fuzzing — the complement to this chapter's *static* enforcement), Ch. 33 §33.5 and Ch. 40 §40.4.6 (the two chapters whose contracts §29.4.3 pins: layout and FP determinism), Ch. 118 (CI — where every check here runs on every merge), Ch. 101 (security — the same tooling catches a different class of bug), Appendix O (the anti-pattern catalogue §29.4.6's review checklist is drawn from). Closes **Part III**: the compiler has been made to work for latency; this chapter makes it *refuse* to work against it.

---

## 29.1 Why it matters: rules enforced by vigilance are not enforced

The preceding twenty-seven chapters state rules: no allocation on the hot path (Ch. 30), no syscall (Ch. 54), no `virtual`/`std::function` dispatch where a static call will do (Ch. 16), no throw (Ch. 24), structs that cross a cache line or a process boundary have a pinned layout (Ch. 11, 67), atomics that are actually lock-free (Ch. 41), windows that are powers of two (Ch. 37), floating-point that contracts the same way on every box (Ch. 40). Every one of them is enforced today by the person who read the chapter remembering it in review, and every one of them is broken in production by a `std::string` copy in a range-for, a `reserve()` that moved to the wrong side of a refactor, a `std::map` that someone found convenient, or a struct that grew a timestamp. The dynamic tools (Ch. 53) catch some of these *when the test hits them*; a latency regression gate (Ch. 118) catches the ones that are large enough to show above the noise. What is missing is the layer that catches them **at build time, on every commit, with the rule written down as code**: a linter configured for the hot path, custom checks that encode the book's own rules, a `static_assert` table that pins every invariant a measurement established, and a link step that cannot succeed if a hot translation unit imports `malloc`.

This chapter builds that layer and, in keeping with the book, *calibrates* it: §29.3 measures what the copies the linter flags actually cost (2 ns, 11 ns, or nothing), because a lint that cries wolf gets suppressed and a lint that is calibrated gets obeyed.

## 29.2 Mental model

### 29.2.1 Three enforcement points: compile, link, run

| Point | Tool | What it can see | What it cannot | Cost |
|---|---|---|---|---|
| **Compile** (per TU) | `static_assert`, `consteval`, `-Werror=…`, clang-tidy, clang-query, a custom check | types, layouts, the AST: every call, every copy, every attribute, every `noexcept` | what the optimiser did; what an inlined callee in another TU does; runtime values | seconds per TU; free at runtime |
| **Link** (per binary / library) | `nm -u`, `-Wl,--wrap`, `--defsym`, section checks, `objdump` greps | the *symbols* a TU imports after optimisation — the truth about `malloc`, `operator new`, `__cxa_throw`, `write`, `pthread_mutex_lock` | which call site; whether the import is on the hot path or a cold branch of the same TU | seconds; free at runtime |
| **Run** (test build) | `operator new` counter (Ch. 30), `seccomp` strict mode, `strace -c`, sanitizers (Ch. 53), the replay hash (Ch. 40) | the hot loop *as executed*: allocations, syscalls, races, bit-exactness | paths the test did not take | minutes; test builds only |

The three are complementary, not alternatives: the compile-time layer catches the *pattern* (a copy, a `new`, a `throw` in a hot function) with the exact line; the link-time layer catches the *fact* (this TU imports `operator new`) whatever pattern produced it, including library code that was inlined in; the runtime layer catches what reached the loop. A rule is *enforced* when it is caught at the earliest point that can see it and cannot be suppressed without a diff someone reviews.

### 29.2.2 What a linter can and cannot see; the hot-path scope

clang-tidy's `performance-*` checks are syntactic-semantic: they see a range-for whose loop variable is a non-trivial type copied by value, a parameter of class type copied for every call and only read, a `std::vector` grown in a loop without `reserve`, a move constructor that is not `noexcept`. They do not know which functions are hot. Applied to a whole codebase they are noise (the cold path is allowed to copy); applied to the hot path they are law. The scope problem is solved by **marking the hot path** — `[[gnu::hot]]` on the functions (which also steers code layout, Ch. 14 §14.4), a `namespace hot`, or a TU naming convention — and writing the custom checks (§29.4.2) to match *inside* that scope only. The book's rules are mostly of the form "no X inside a hot function", and that is exactly the shape an AST matcher expresses in one line.

### 29.2.3 Contracts as code: the invariant the measurement established

Every measured chapter ends with an invariant: Ch. 11 measured a 64-byte `Order` against a 72-byte one and chose 64; Ch. 33 measured a power-of-two slot table; Ch. 41 confirmed `is_always_lock_free`; Ch. 40 confirmed bit-exactness under `-ffp-contract=off`. A **performance contract** is that invariant written as a `static_assert` next to the type — so the measurement's *conclusion* is enforced on every build, by the compiler, with the chapter that justifies it in the message. §29.3 shows the table catching two things in its own first draft; that is the typical yield.

## 29.3 Measure it: calibrating the linter, running the rule set, and what the contract table caught

**Machine, toolchain.** All runs on the author's 4-vCPU KVM guest (Appendix L.2), GCC 13.3 and Clang 18.1 with `clang-tidy-18`/`clang-query-18`, `binutils` 2.42, Linux 6.18. Timings VM-measured; the findings are deterministic.

**1. What a flagged copy costs** (`copycost.cpp`, GCC 13.3 `-O2 -march=native`, 1024 elements, pinned):

```
   range-for by value,  Order with an SSO std::string (56 B)      2.36 ns / element     <- performance-for-range-copy: a copy + a destructor per element
   range-for by const&, same                                      0.28 ns / element
   range-for by value,  Order with a heap-backed std::string     11.35 ns / element     <- a malloc + free per element: 40x
   range-for by const&, same                                      0.28 ns / element
   range-for by value,  trivially-copyable 32-byte POD            0.26 ns / element     <- the compiler elides it: the lint is right to stay quiet
   range-for by const&, same                                      0.26 ns / element
   std::vector<Order> parameter by value (1024 elements)          3.01 ns / element     <- performance-unnecessary-value-param: 3 us per call
   std::vector<Order> parameter by const&                         0.00 ns / element
```

The calibration: a flagged copy of a type with a non-trivial destructor costs 2–11 ns *per element* — a whole budget line per batch — while the same lint on a trivially-copyable type would be noise (the compiler already elides it, and clang-tidy does not flag it). Keep `performance-*` on for the hot path and let it be quiet elsewhere.

**2. The rule set on a hot TU** (`hot.cpp`: a feed-handler fragment with one of every sin, every function marked `[[gnu::hot]]`):

```
   $ clang-tidy-18 -checks='-*,performance-*,bugprone-exception-escape,misc-const-correctness' hot.cpp -- -std=c++20 -O2
   hot.cpp:16:38: warning: the parameter 'orders' is copied for each invocation but only used as a const reference [performance-unnecessary-value-param]
   hot.cpp:16:92: warning: the parameter 'cb' is copied for each invocation but only used as a const reference [performance-unnecessary-value-param]
   hot.cpp:17:15: warning: loop variable is copied but only used as const reference [performance-for-range-copy]
   hot.cpp:31:19: warning: variable 'n' of type 'int' can be declared 'const' [misc-const-correctness]
   54 warnings generated. Suppressed 50 warnings (50 in non-user code).

   $ clang-query-18 -f hotrules.cq hot.cpp -- -std=c++20            # the book's rules, as matchers (§29.4.2)
   hot.cpp:19:9:  virtual-call-on-hot-path                           book.add(o)        — Ch. 16
   hot.cpp:20:9:  std::function-call-on-hot-path                     cb(o)              — Ch. 16
   hot.cpp:21:25: throw-on-hot-path                                  throw ...          — Ch. 24
   hot.cpp:25:15: new-on-hot-path                                    new Order{...}     — Ch. 30
   hot.cpp:32:5:  syscall-or-malloc-on-hot-path                      ::write(fd, ...)   — Ch. 54

   $ nm -u -C hot.o | grep -E 'operator new|malloc|write|__cxa_throw|__throw'           # link-time truth (§29.4.4)
   U operator new(unsigned long)    U __cxa_allocate_exception    U __cxa_throw    U write
   U std::__throw_bad_alloc()       U std::__throw_length_error(char const*)       U std::__throw_bad_function_call()
```

The stock checks found the three copies; the five book rules found the five things the stock checks have no concept of; `nm` found seven imports, three of them (`__throw_bad_alloc`, `__throw_length_error`, `__throw_bad_function_call`) from library code inlined into the TU — which no source-level check could have named.

**3. The link-time trap, and its scope** (§29.4.4). `hot.cpp` partially linked with `malloc`/`operator new` wrapped, then linked against definitions that abort:

```
   $ ld -r -o hot_wrapped.o hot.o --wrap=malloc --wrap=_Znwm --wrap=_Znam ; nm -u hot_wrapped.o | grep wrap
   U __wrap__Znwm
   $ g++ main.cpp cold.cpp hot_wrapped.o trap.cpp -o wrapped && ./wrapped
   HOT-PATH VIOLATION: operator new(56) from a hot TU      Aborted (exit 134)        <- on_tick's `new Order`
```

The first draft had `cold_setup()` (a `reserve(1024)`) in the same TU and tripped on *it* — `operator new(57344)` — which is the lesson about scope: the unit of `--wrap` is the object file, so cold code lives in cold TUs. **4. The contract table** (§29.4.3) compiled clean on both compilers *after* its first draft caught two real defects: `Ring<4096>` had its two atomics unpadded (`sizeof % 64 == 16` — head and tail on one line, Ch. 44) and `std::hardware_destructive_interference_size` does not exist under Clang 18 + libstdc++ 13 (gated on a GCC-only macro; the table now guards it with the feature-test macro). With a timestamp added to `Order`: `error: static assertion failed: OrderV2 layout changed: re-measure before shipping`. **5. The runtime assertion** (§29.4.5): under `SECCOMP_MODE_STRICT`, `write()` is permitted and the first `getpid()` kills the process — `Killed (exit 137)`, no core, no doubt.

## 29.4 Techniques

### 29.4.1 clang-tidy `performance-*` in CI, calibrated

- **The check set for hot-path TUs**: `performance-unnecessary-value-param`, `performance-unnecessary-copy-initialization`, `performance-for-range-copy`, `performance-move-const-arg`, `performance-no-automatic-move`, `performance-inefficient-vector-operation`, `performance-inefficient-string-concatenation`, `performance-noexcept-move-constructor`, `performance-trivially-destructible`, plus `bugprone-exception-escape` (a `noexcept` function that can throw — Ch. 24), `misc-const-correctness` and `cppcoreguidelines-pro-type-member-init`. For the rest of the codebase, the same set as warnings, not errors.
- **Mechanics**: generate `compile_commands.json` from the real build (`CMAKE_EXPORT_COMPILE_COMMANDS=ON`, or `bear` — Ch. 28) so the checks see the real flags and includes; run `run-clang-tidy -p build -checks=… -warnings-as-errors='performance-*'` on the hot-path directories in CI; **pin the clang-tidy version** in the hermetic toolchain (Ch. 28 §28.4.2) — checks change between releases, and a CI that goes red because a toolchain upgrade added a check is a CI people learn to ignore.
- **Suppression policy**: a `// NOLINT(performance-for-range-copy): the copy is the point — the element is mutated` with a *reason* is a reviewable diff; `-checks=-performance-*` in a config file is not. `NOLINTBEGIN/NOLINTEND` for a deliberately cold block inside a hot TU. The suppression count is a metric Ch. 118's dashboard shows.
- **Calibration** (§29.3): run `copycost.cpp`'s pattern on the type in question before arguing about a finding; "2 ns per element, 400 elements per batch, 800 ns per batch" ends the argument either way.

### 29.4.2 Custom checks for the book's rules

Every "no X on the hot path" rule is an AST matcher; `clang-query` runs the matchers from a file in CI with no build step, and a matcher that earns its keep graduates to a clang-tidy module (the same matcher, wrapped in a `ClangTidyCheck` with a `diag()` call — the LLVM tree's `add_new_check.py` scaffolds it; it is ~40 lines of C++ per check and a `-load` of the plugin, or an out-of-tree build against `llvm-18-dev`). The rule file this chapter ran:

```
# hotrules.cq — the book's hot-path rules as clang-query matchers (clang-query-18 -f hotrules.cq file.cpp -- <flags>)
set bind-root false
match functionDecl(hasAttr("attr::Hot"), forEachDescendant(cxxNewExpr().bind("new-on-hot-path")))                       # Ch. 30
match functionDecl(hasAttr("attr::Hot"), forEachDescendant(cxxThrowExpr().bind("throw-on-hot-path")))                   # Ch. 24
match functionDecl(hasAttr("attr::Hot"), forEachDescendant(cxxMemberCallExpr(callee(cxxMethodDecl(isVirtual())),
      unless(callee(cxxMethodDecl(hasAttr("attr::Final"))))).bind("virtual-call-on-hot-path")))                         # Ch. 16
match functionDecl(hasAttr("attr::Hot"), forEachDescendant(cxxOperatorCallExpr(callee(cxxMethodDecl(ofClass(hasName("::std::function"))))).bind("std::function-call-on-hot-path")))
match functionDecl(hasAttr("attr::Hot"), forEachDescendant(callExpr(callee(functionDecl(matchesName(
      "^::(write|read|send|recv|clock_gettime|nanosleep|malloc|calloc|realloc|free)$")))).bind("syscall-or-malloc-on-hot-path")))   # Ch. 54
match functionDecl(hasAttr("attr::Hot"), forEachDescendant(cxxForRangeStmt(hasLoopVariable(varDecl(unless(hasType(referenceType())),
      hasType(cxxRecordDecl(hasMethod(cxxDestructorDecl(unless(isTrivial())))))))).bind("range-for-copies-a-non-trivial-type")))
```

- **Scope by attribute.** `hasAttr("attr::Hot")` scopes every rule to `[[gnu::hot]]` functions (the string form is clang-query's spelling; in C++ it is `hasAttr(attr::Hot)`). A `namespace hot` scope is `hasAncestor(namespaceDecl(hasName("hot")))`; a TU scope is the file list CI passes. Callees reached *through* a hot function are not matched — that is the link-time layer's job (§29.4.4) — but `forEachDescendant` does see the body of anything inlined by the *front end* (templates, lambdas), which is most of a hot path.
- **The rules that pay**: `new`/`delete`/`malloc`; `throw`; `virtual` calls not through a `final` class; `std::function`/`std::any`/`std::shared_ptr` copies (Ch. 16, 32); syscalls by name; `std::map`/`unordered_map`/`list` types in hot signatures (`hasType(cxxRecordDecl(hasName("::std::map")))`); range-for and parameter copies of non-trivial types; a struct marked `[[gnu::hot]]` whose `sizeof` exceeds 64 (needs a check, not a matcher — `getTypeSize` in the check's `check()`); a `std::chrono`/`rdtsc` call inside a function marked pure (Ch. 40 §40.6).
- **Keep it honest**: every rule in the file names the chapter that measured it, and every rule has one test file with a positive and a negative case (`hot.cpp` is the positive), so a clang upgrade that silently stops matching is caught.

### 29.4.3 Performance contracts: `static_assert` tables for layout, concurrency, exceptions and folding

Ch. 21 §21.4.1's "compile-time validation" bullet, grown into the pattern:

```cpp
// contracts.hpp — the latency invariants the hot path relies on, asserted at build time. The MESSAGE names the chapter.
#define LAYOUT_CONTRACT(T, size, align) static_assert(sizeof(T) == (size) && alignof(T) == (align), #T " layout changed: re-measure before shipping")
#define FITS_ONE_LINE(T)                static_assert(sizeof(T) <= 64, #T " must fit one cache line")
#define SHM_SAFE(T)                     static_assert(std::is_trivially_copyable_v<T> && std::is_standard_layout_v<T>, #T " crosses a process/memcpy boundary")
#define LOCK_FREE(T)                    static_assert(std::atomic<T>::is_always_lock_free, "std::atomic<" #T "> would take a mutex on this target")
#define POWER_OF_TWO(n)                 static_assert(std::has_single_bit(std::size_t(n)), #n " must be a power of two (mask, not modulo)")
#define NOTHROW(expr)                   static_assert(noexcept(expr), #expr " may throw on the hot path")
#define FOLDED(expr)                    static_assert(((void)(expr), true), #expr " did not fold at compile time")   // non-constant => hard error

// contracts_test.cpp — one line per invariant a chapter measured; compiled with -fsyntax-only in CI (GCC and Clang)
LAYOUT_CONTRACT(Order, 32, 8);     POWER_OF_TWO(sizeof(Order));                    // Ch. 33: two orders per line
LAYOUT_CONTRACT(Level, 16, 4);                                                     // Ch. 33: four levels per line
FITS_ONE_LINE(Bbo);                SHM_SAFE(Bbo);                                  // Ch. 46, 67: seqlock-published, memcpy'd
SHM_SAFE(Slot);                    POWER_OF_TWO(4096);                             // Ch. 67: ring slots cross processes
LOCK_FREE(std::uint64_t);
#ifdef __cpp_lib_hardware_interference_size
static_assert(std::hardware_destructive_interference_size == 64);                  // Ch. 41, 44 (absent under Clang + libstdc++ 13: §29.3)
#endif
NOTHROW(publish(std::declval<Bbo&>(), std::declval<const Bbo&>()));                // Ch. 24
FOLDED(TABLE.v[200]);                                                              // Ch. 21: the table is data, not code
static_assert(sizeof(Ring<4096>) % 64 == 0);                                       // Ch. 44: caught head/tail sharing a line (§29.3)
```

- **The layout contract** (`sizeof`/`alignof`/`offsetof` — Ch. 11 §11.3 developed the `offsetof` form for wire structs; Ch. 23 generates them for decoders) pins what a measurement chose; the message says "re-measure", not "fix", because the right response to a grown struct may be to accept the new size *after* re-running the chapter's benchmark.
- **The shared-memory contract** (`is_trivially_copyable` + `is_standard_layout`) is what makes a `memcpy` into a ring slot or a shm segment (Ch. 67) defined behaviour; add `has_unique_object_representations_v` when the bytes are hashed or compared.
- **The concurrency contract**: `is_always_lock_free` for every atomic on the hot path (Ch. 41 §41.6 made it a checklist item; here it is a build error), the interference size pinned to what the padding assumed (Ch. 44), and `has_single_bit` for every capacity that is masked (Ch. 37, 45).
- **The exception contract**: `static_assert(noexcept(expr))` for the hot calls — and because `noexcept(f(args))` is *false* if any callee in the expression is not `noexcept`, one assert per hot entry point transitively checks the chain the front end can see (Ch. 24 §24.4.1).
- **The folding contract**: a `consteval`-built table referenced in a `static_assert` is proved to be a compile-time constant (a runtime value is a hard error); `if consteval` (C++23) selects the folded form in the function itself. What the contract cannot prove is that the *optimiser* kept the fold — that is §29.4.4's `objdump` check and Ch. 21 §21.5.
- **Where the table lives**: one `contracts_test.cpp` per library, compiled with `-fsyntax-only` on every toolchain in CI, so a contract failure is a two-second red build with the chapter number in the message — never a 2 a.m. latency alert.

### 29.4.4 Link-time assertions: `--wrap` traps, `nm` scans, `--defsym` and section checks

The front end sees patterns; the linker sees facts. Three facts worth asserting:

- **A hot TU imports no allocator.** Partially link the hot-path objects with the allocation entry points wrapped — `ld -r -o hot.o hot/*.o --wrap=malloc --wrap=calloc --wrap=realloc --wrap=_Znwm --wrap=_Znam --wrap=_ZnwmSt11align_val_t` (the mangled `operator new` variants) — and provide `__wrap_*` definitions that log the size and `abort()` (`trap.cpp`, §29.3). Only references *from the wrapped objects* are redirected; the cold TUs keep the real allocator. A test run that allocates from a hot TU dies with the size and a stack; a production build links the same objects against `__wrap_malloc = __real_malloc` (`-Wl,--defsym=__wrap_malloc=__real_malloc`) if a cold path inside a hot TU genuinely must allocate at startup — or, better, that path moves to a cold TU (§29.3's own lesson).
- **A hot TU imports nothing from the forbidden list.** `nm -u -C hot.o | grep -E 'operator new|malloc|__cxa_throw|__cxa_allocate_exception|__throw_|pthread_mutex|write$|read$|clock_gettime|nanosleep'` in CI, failing on any match; the output in §29.3 is what it looks like when it finds something. This catches the inlined library throws (`std::__throw_length_error` from a `vector` growth path) that no source check names, and it runs in milliseconds.
- **The hot function's code is what the chapter verified.** `objdump -d --no-show-raw-insn app | awk '/<hot_fn>:/,/ret/'` piped through `grep -c 'call\|div\|vfmadd'` with expected counts (zero `call`s in Ch. 33's `remove()`; zero `vfmadd` in Ch. 40's core under the determinism build) — the asm-hash gate Ch. 118 §118.4.3 uses to skip timing runs when the code did not change is the same mechanism with a looser question.
- **`-Wl,--defsym` as a tripwire**: `--defsym=operator_new_forbidden=0` plus a hot TU that references it only inside the `#ifdef` of a disallowed path turns a link into an assertion; and `-Wl,--gc-sections -Wl,--print-gc-sections` lists what the hot library *dragged in*, which is how a `std::ostringstream` reaches a binary that never names it.

### 29.4.5 Runtime assertions in test builds

For what the test *executes*: the `operator new` counter wrapping the timed region (Ch. 30 §30.5; Ch. 33 §33.3 reports it as a column); `SECCOMP_MODE_STRICT` (`prctl(PR_SET_SECCOMP, SECCOMP_MODE_STRICT)`) entered just before the hot loop in a test binary, so that *any* syscall but `read`/`write`/`_exit`/`sigreturn` kills the process (§29.3: `Killed`, exit 137) — the kernel's own no-syscall assertion, zero overhead, and a `clock_gettime` that quietly fell back from the vDSO to a syscall (Ch. 20) is caught by it; `strace -c` and `bpftrace`'s syscall count on the hot thread (Ch. 54, 81) for the softer form; and the replay hash (Ch. 40 §40.4.6) for bit-exactness. These belong in Ch. 116's test suite and run in Ch. 118's CI next to the sanitizers (Ch. 53).

### 29.4.6 Reviewing for latency: the checklist

The static layer does not read a design. A hot-path review asks, in order: (1) **is this on the hot path** (which TU, which attribute, which core — Ch. 49's topology), because everything after depends on it; (2) **allocation**: every container pre-sized, every string an `std::string_view`/`FixedString` (Ch. 25), every `std::function` a template parameter; (3) **dispatch**: every `virtual` through a `final` or a `variant`, every indirect call justified (Ch. 16); (4) **control flow**: the rare path marked `[[unlikely]]` and *measured* rare, no `throw`, `noexcept` on the entry points (Ch. 15, 24); (5) **layout**: every new field in a hot struct accounted against the line budget, the contract table updated (Ch. 11, §29.4.3); (6) **sharing**: nothing written by two cores, nothing read by the hot core that a cold core writes without a seqlock/RCU (Ch. 44, 46); (7) **time**: no clock inside the deterministic core, every timeout a deadline on an input timestamp (Ch. 40, 55); (8) **the measurement**: the benchmark that justifies the change, run A/A (Ch. 118 §118.4.1), with the numbers in the PR. Appendix O's catalogue is the long form, sorted by where the sin lives.

## 29.5 Pitfalls & anti-patterns

- **A linter run on the whole tree with `-Werror`.** The cold path is allowed to copy; the first week's thousand findings get suppressed wholesale and the hot-path ones with them. Scope first (§29.2.2), then enforce.
- **An unpinned clang-tidy.** A toolchain upgrade adds a check, CI goes red for a reason nobody asked for, and the response is `-checks=-new-thing`. Pin the version with the compiler (Ch. 28).
- **Trusting the source-level check for allocation.** `std::vector::push_back` on a reserved vector is allocation-free *until* it is not; `std::string` copies are free until 16 characters. The link-time scan (§29.4.4) sees `__throw_length_error` where the matcher saw nothing.
- **`--wrap` on a TU with cold code in it.** The trap fires on startup (§29.3's `reserve`); the fix is to move the cold code, not to weaken the trap.
- **`static_assert(sizeof(T) == 64)` without `alignof`.** A 64-byte struct at 8-byte alignment straddles lines half the time; the layout contract pins both (and `alignas(64)` makes `sizeof` a multiple of 64 — which is why the §29.3 ring caught its atomics).
- **A `noexcept` assert on a function that lies.** `noexcept(expr)` is true for a function *declared* `noexcept` whatever it calls; the assert certifies the declaration, Ch. 24's discipline certifies the body, and `bugprone-exception-escape` checks one against the other.
- **Suppressions without reasons.** A bare `// NOLINT` is a future incident with a comment; require the check name and a sentence.
- **Seccomp strict in production.** It is a *test* assertion: production needs `mmap` for a late page, `futex` for a wake-up, `sendmsg` for the order. Ch. 101 §101.4.4's filter mode is the production form.

## 29.6 Exercises & checklist

**Exercises**

1. **Scope and calibrate.** Mark your feed handler's hot functions `[[gnu::hot]]`, run the `performance-*` set on that TU alone, and for each finding measure the copy with `copycost.cpp`'s pattern. Which findings exceed 1 ns per element? Fix those; suppress the rest with reasons.
2. **One rule, one check.** Pick a book rule not in `hotrules.cq` (a `std::chrono::steady_clock::now()` inside a function marked pure, Ch. 40 §40.6; a `std::map` member in a hot struct) and write the matcher; test it against a positive and a negative file. Then port it to a clang-tidy module and measure the extra CI time.
3. **The link-time gate.** Add the `nm -u` grep and the `--wrap` partial link to your build; run the test suite. What did the gate find that the sanitizers and the `operator new` counter had not? (Expect: an inlined `__throw_*` and a `std::ostringstream` in a "logging-free" path.)
4. **The contract table.** Write `contracts_test.cpp` for your three hottest structs, your ring buffers and your atomics; compile it under both compilers and under `-m32` (if you have the headers). Which contracts fail where, and which failures were real?
5. **Seccomp strict.** Wrap your replay harness's hot loop (Ch. 109) in `SECCOMP_MODE_STRICT` and run a day of capture. What killed it first? (Expect: a logger's `write`, a timer's `clock_gettime` that is not vDSO on your box, or a `futex` in a "lock-free" queue.)
6. **The asm hash gate.** For Ch. 33's `remove()` and Ch. 40's core, commit the expected `call`/`div`/`vfmadd` counts and make CI diff them; then change `-O2` to `-O3` and see what moves.

**Checklist — enforcing the rules**

- [ ] The hot path is **marked** (`[[gnu::hot]]` / namespace / TU list) and `performance-*` plus `bugprone-exception-escape` run on it as **errors**, with a **pinned** clang-tidy and a reason on every suppression (§29.4.1).
- [ ] The book's rules exist as **matchers** (`hotrules.cq` or a clang-tidy module) with positive/negative test files, scoped to the hot path (§29.4.2).
- [ ] Every measured invariant is a **`static_assert` in `contracts_test.cpp`** — layout, shm-safety, lock-freedom, power-of-two capacities, `noexcept`, folding — with the chapter in the message, compiled on every toolchain (§29.4.3).
- [ ] Hot TUs are **`nm -u`-scanned** for allocator/throw/syscall imports and **`--wrap`-trapped** in test links; cold code lives in cold TUs (§29.4.4).
- [ ] Test builds assert at runtime: the **`operator new` counter**, **`SECCOMP_MODE_STRICT`** around the hot loop, the **replay hash** (§29.4.5).
- [ ] Hot-path reviews follow the **eight questions** (§29.4.6), and a PR that touches a hot TU carries its measurement.

## 29.7 References

- The clang-tidy documentation (the `performance-*`, `bugprone-*`, `misc-*` check lists; "Writing a clang-tidy check" and `add_new_check.py`) and the clang AST Matcher Reference — the vocabulary of §29.4.1–§29.4.2.
- clang-query documentation (`set bind-root`, `-f` command files, the string spelling of enum arguments such as `hasAttr("attr::Hot")`).
- GNU `ld` manual: `--wrap=symbol`, `--defsym`, `-r` (relocatable output), `--gc-sections`/`--print-gc-sections`; `nm` and `objdump` in the binutils manual — §29.4.4.
- `seccomp(2)` and `prctl(2)` (`SECCOMP_MODE_STRICT`); Ch. 101 §101.4.4 for filter mode.
- ISO C++: `static_assert`, `<type_traits>` (`is_trivially_copyable`, `is_standard_layout`, `has_unique_object_representations`), `std::atomic<T>::is_always_lock_free`, `std::hardware_destructive_interference_size` (P0154), `std::has_single_bit`, `if consteval` (P1938) — the contract vocabulary; Appendix H for availability (the interference-size gap under Clang + libstdc++).
- Ch. 11 §11.3, Ch. 21 §21.4.1, Ch. 24 §24.4.1, Ch. 30 §30.5, Ch. 41 §41.6, Ch. 44 §44.5, Ch. 40 §40.4.6 — the chapters whose rules this one enforces.

## 29.8 Additional Reading

- The LLVM project's own `.clang-tidy` configuration and the Chromium/Abseil style checks — large codebases that run a scoped, pinned linter as a merge gate.
- J. Turner, *C++ Best Practices* (the tooling chapters) — a practitioner's view of warnings-as-errors, sanitizers and static analysis in CI.
- Appendix O — the anti-pattern catalogue §29.4.6's review questions are drawn from, with the counter that exposes each.

---

*Next: Ch. 30 — Memory Management Fundamentals, opening Part IV. With the language mastered, the build under control and the rules enforced, we turn to the resource that dominates real hot-path tails: memory — stack vs heap, allocation cost, page faults, and why `malloc` is the most dangerous function on the tick-to-trade path (and now one the linker will refuse to let a hot TU import).*
