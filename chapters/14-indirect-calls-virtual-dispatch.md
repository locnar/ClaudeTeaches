# Part II — CPU Microarchitecture

# Chapter 14 — Indirect Calls, Virtual Dispatch & Devirtualization

> **Prerequisites:** Ch. 13 (branch *direction* prediction — this chapter is the harder *target* prediction problem for indirect branches), Ch. 12 (the front-end the indirect branch resteers, and the I-cache footprint of many call targets), Ch. 11 (the back-end work squashed on a target mispredict), Ch. 7 (the vtable load is a cache access), Ch. 3–4 (benchmarking, reading asm — §14.3, §14.5).
>
> **Leads into:** Ch. 19 (template metaprogramming & zero-cost abstractions — CRTP and policy-based design in full), Ch. 21 (the cost of abstractions — exceptions, `noexcept`, RTTI), Ch. 28 (cache-friendly containers — `std::function` in node-based structures). The `switch`/jump-table thread continues from Ch. 13's lookup tables.

---

## 14.1 Why it matters: hidden indirect branches on the dispatch path

Chapter 13 was about a branch whose *direction* the predictor must guess. An **indirect branch** is harder: the core knows it will jump, but not *where* — the target address is in a register or memory, computed at runtime. A `virtual` call, a function pointer, a `std::function` invocation, a `switch` lowered to a jump table — all are indirect branches, and the front-end (Ch. 12) must predict the *target address* to keep fetching speculatively. When it predicts wrong, you pay the same ~15–20-cycle flush as a direction mispredict (Ch. 13) — plus, often, a **cache miss to load the vtable pointer and the target** before the call can even resolve.

The reason this gets its own chapter is that indirect dispatch is *everywhere* in idiomatic C++, and it hides on exactly the hot path HFT cares about. A feed handler built with an `IMessageHandler*` per message type, an order router with a `Strategy` base class, an event loop dispatching through `std::function` callbacks — each tick flows through a `virtual` call or a `std::function::operator()`. When the call site is **monomorphic** (always the same concrete type) the BTB predicts it perfectly and it's nearly as cheap as a direct call. When it's **polymorphic** — and especially **megamorphic** (many target types interleaved unpredictably, the natural shape of a feed with mixed message types) — the BTB can't keep up, target mispredicts spike, and every dispatch eats a flush. Combined with the vtable load (a dependent cache access) and the fact that an indirect call **blocks inlining** — the compiler can't see through it, so all the constant-propagation and cross-call optimization of Ch. 11–12 stops at the call boundary — virtual dispatch on a hot, megamorphic path can cost *tens of nanoseconds* per call.

So the chapter's job is twofold: understand *what* an indirect call actually costs (vtable load + target prediction + the lost inlining), and learn the toolkit for **removing indirect calls from the hot path** — `final` and devirtualization to recover direct calls, CRTP/static polymorphism to dispatch at compile time, `std::variant`/visitor as a closed-set alternative, and knowing when a plain `switch` or a *data* lookup (Ch. 13) beats them all. As always, measure first (Ch. 2): a *monomorphic* virtual call is cheap and not worth contorting your design over; the techniques here are for the *megamorphic, hot, mispredicting* dispatch the PMU actually flags.

---

## 14.2 Mental model

### 14.2.1 vtables and the cost of `virtual`

A `virtual` call is not one operation; it's a small dependent chain. Given `base->f()`:

```
   object ──► [ vptr | data... ]          1. load the vptr from the object   (cache access)
                 │
                 ▼
              vtable  ──► [ &A::f | &A::g | ... ]   2. load f's slot from the vtable (cache access)
                              │
                              ▼
                         indirect call ──► target   3. indirect CALL through that pointer
                                                        (BTB target prediction; flush if wrong)
```

Three costs stack up, none present in a direct call:

1. **The vptr load.** Every polymorphic object carries a hidden pointer (the `vptr`) to its class's vtable. The call must load it from the object — a data-cache access (Ch. 7), dependent (the call can't proceed until it lands). If the object is cold, this is a miss.
2. **The vtable slot load.** Index the vtable to get `f`'s address — a second dependent load (the vtable is usually hot/shared, so often an L1 hit, but still a dependency-chain link).
3. **The indirect call itself.** Jump to the loaded address. The front-end must *predict* this target (§14.2.2) to keep going; a wrong guess is a full pipeline flush (Ch. 12–13).

On top of those direct costs is the **opportunity cost that often dominates: lost inlining.** A direct call the compiler can see through gets inlined, after which constant propagation, dead-code elimination, and the Ch. 11 scheduling flow *across* the old call boundary. A `virtual` call is an opaque wall — the optimizer can't know which body runs, so it inlines nothing and optimizes nothing across it. For a *tiny* dispatched function (an order-book accessor, a one-line handler), the lost inlining can cost far more than the call mechanics themselves: a 2-instruction function that *should* fold into its caller instead becomes a full call with register-save overhead and a prediction. The `sizeof(object)` also grows by a pointer, and polymorphic objects can't be as densely packed (Ch. 8–9).

The crucial nuance: **a monomorphic virtual call is cheap.** If a call site only ever sees one concrete type, the BTB predicts the target perfectly, the vtable is hot, and the only real loss is inlining — which `final` and devirtualization (§14.4.1) can often recover. The expensive case is *polymorphism that actually varies at the call site*.

### 14.2.2 Indirect-branch prediction (BTB) and mispredict cost

Direction prediction (Ch. 13) answers taken/not-taken; **target** prediction answers *to which address*. The hardware uses the **BTB** (Branch Target Buffer) and an indirect-branch predictor that, like the direction predictor (Ch. 13.2.1), correlates the target with global history. Call sites fall into three regimes:

- **Monomorphic** (one target): the BTB nails it every time — prediction ~100%, cost ≈ a direct call. The common, benign case.
- **Polymorphic** (a few targets, ~2–4): a good indirect predictor can often learn the pattern *if* it correlates with history (e.g. alternating types in a regular order). If the sequence is data-dependent and irregular, mispredicts climb.
- **Megamorphic** (many targets, interleaved unpredictably): the history-indexed indirect predictor cannot learn a pattern for the target sequence, so it mispredicts at roughly (1 − 1/k) for k uniformly random targets; BTB *capacity* only becomes the limit with hundreds of hot indirect sites. **This is the expensive regime** — and it's the shape of a feed handler dispatching a stream of mixed message types through one `handler->on_message()` site.

A target mispredict costs the same ~15–20-cycle flush as a direction mispredict (Ch. 13.2.2): the front-end speculated down the wrong target's code, the back-end's in-flight work is squashed, refetch and refill. But indirect mispredicts carry an extra tax: each distinct target is *different code*, so a megamorphic site also thrashes the **L1i and I-TLB** (Ch. 12) — many target bodies competing for instruction-cache space — compounding the front-end cost. The signature (§14.3) is therefore high `branch-misses` **localized to indirect branches** (`perf` can break out `BR_MISP_RETIRED.INDIRECT`), often alongside elevated front-end stalls.

### 14.2.3 `std::function` and type-erasure overhead

`std::function<R(Args...)>` is the most expensive common form of indirect dispatch, and it's worth understanding *why* so you reach for it deliberately:

- **It is an opaque indirection.** `std::function` type-erases the callable behind a vtable-like mechanism (a manager pointer, an invoker pointer, plus storage). Calling it is an empty-check branch and *one* indirect call to a type-specific invoker whose body is the stored callable inlined (in libstdc++ the invoker instantiated for a lambda is literally the lambda's body); a second indirect hop occurs only if what you stored is itself a function pointer. The dispatch cost is therefore comparable to a virtual call — what `std::function` adds is that the call site can never inline the target, plus a heap allocation for callables that overflow its small buffer (Ch. 28).
- **It may allocate.** A callable larger than the small-buffer optimization (SBO) capacity (a few pointers — typically capturing more than ~2 pointers' worth of state) is **heap-allocated** on construction. On a hot path that constructs `std::function`s, that's a `malloc` (Ch. 26) — a latency catastrophe. Even when SBO applies, the object is larger and copies are non-trivial.
- **It blocks inlining and bloats.** Like `virtual`, the call target is opaque; the compiler inlines nothing through it. Worse, each distinct callable type instantiates its own manager/invoker, inflating code size (Ch. 12).

The upshot: **`std::function` in a hot loop is an anti-pattern** (§14.6). It's a fine tool for *setup*, configuration, and cold paths where flexibility matters and the call is rare; on the steady-state hot path, prefer a concrete callable type (a template parameter — Ch. 19), a function pointer (lighter, single indirection, no allocation), `std::variant`+visitor (§14.4.3, closed set, no allocation), or CRTP (§14.4.2, no indirection at all). The same caution applies to other type-erased callbacks (e.g. a hand-rolled `void* + fn-ptr`): the erasure that buys flexibility costs an un-inlinable indirect call. The newer standard erasers do not change the dispatch cost but change everything else, and the design space is worth naming before §14.4.5 measures it. A type-erased callable has three parts — *storage* for the callable (owned inline, owned on the heap, or not owned at all), an *invoke thunk* (one function per erased type that knows how to call it) and, for owning erasers, a *manager* (copy/move/destroy). `std::function` owns, copies, keeps a 16-byte small buffer (libstdc++; 32-byte object) and spills to the heap beyond it. `std::move_only_function` (C++23; libstdc++ 12+, libc++ 20; `__cpp_lib_move_only_function`) drops the copy requirement — so a callable owning a `unique_ptr` or a socket fits — adds `const`/`noexcept`/ref-qualified signatures, makes *calling an empty one UB* (so there is no empty check on the call path), and in libstdc++ 13 keeps a 24-byte buffer in a 40-byte object; `std::copyable_function` (C++26) is the copyable twin with the same fixes. `std::function_ref` (C++26, P0792; `__cpp_lib_function_ref` — not in libstdc++ 13 / libc++ 18, use `tl::function_ref`/`llvm::function_ref` or the twenty-line version in §14.4.5) is **non-owning**: an object pointer and a thunk pointer, two words, never allocates, one indirect call, and is the right type for a callable passed *down* a call chain. A fixed-capacity `inplace_function<Sig, N>` (SG14/`stdext`, not standard) owns inline up to `N` bytes and refuses to compile anything larger. `std::any` is *not* a callable eraser: it erases a *type*, and getting the callable back requires naming that type at the `any_cast` — at which point the call is direct and the "erasure" bought nothing.

**Indirect calls you did not write.** Two more sources of indirect branches sit on most trading hot paths uninvited. First, **PLT stubs**: every call into a shared library — libc, libm, a vendor NIC library — is an indirect `jmp [GOT]` through the procedure-linkage table, and under `-fPIC`'s default *semantic interposition* even calls between functions of the same shared library go through it. `-fno-plt` (call through the GOT directly), `-fno-semantic-interposition`, `-Wl,-Bsymbolic`, or static linking remove them (Ch. 24). Second, **Spectre-v2 mitigations**: code compiled with retpolines (`-mindirect-branch=thunk`) turns every indirect call into a `call`/`ret` trampoline that defeats the indirect predictor by design, and IBRS/eIBRS restrict target speculation across privilege levels (Ch. 6 §6.2.3). Measure both the same way as the rest of this chapter — `br_inst_retired.indirect` tells you how many indirect branches you execute per message; a number far above the dispatch sites you wrote is PLT or thunk traffic.

---

## 14.3 Measure it: virtual vs CRTP dispatch microbenchmark

*Toolchain and machine: Appendix L.3.*

The experiment compares four dispatch mechanisms doing the *same* trivial work, over the same data, varying only *how the call is dispatched*: (a) a `virtual` call with a **monomorphic** call site, (b) a `virtual` call with a **megamorphic** site (types interleaved unpredictably), (c) `std::function`, and (d) **CRTP** / static dispatch (no indirection). The trivial work makes the *dispatch cost* — not the callee's work — the variable.

```cpp
// dispatch.cpp — same accumulate, four dispatch mechanisms.
// Build: g++ -O2 -std=c++20 -march=native dispatch.cpp -o dispatch
// Run pinned, turbo off:  taskset -c 2 ./dispatch mono|mega|func|crtp
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
#include <functional>
#include <vector>
#include <random>
#include <chrono>

struct IH { virtual std::int64_t handle(std::int64_t x) const noexcept = 0; virtual ~IH()=default; };
struct AddH  : IH { std::int64_t handle(std::int64_t x) const noexcept override { return x + 1; } };
struct XorH  : IH { std::int64_t handle(std::int64_t x) const noexcept override { return x ^ 7; } };
struct MulH  : IH { std::int64_t handle(std::int64_t x) const noexcept override { return x * 3; } };
struct ShrH  : IH { std::int64_t handle(std::int64_t x) const noexcept override { return x >> 1; } };

// Opaque factories: constructing AddH in the same function as the call site would let GCC's
// speculative devirtualization (-fdevirtualize-speculatively, on at -O2) see the dynamic type
// and inline x + 1, and the std::function's lambda would be constant-folded likewise.
// noinline hides the type from the call site regardless of compiler version.
[[gnu::noinline]] std::unique_ptr<IH> make_handler() { return std::make_unique<AddH>(); }
[[gnu::noinline]] std::function<std::int64_t(std::int64_t)> make_func() {
    return [](std::int64_t x) { return x + 1; };
}

// CRTP / static dispatch: the type is known at compile time → inlinable, no indirection.
template <class Derived> struct Base { std::int64_t handle(std::int64_t x) const noexcept {
    return static_cast<const Derived*>(this)->do_handle(x); } };
struct AddS : Base<AddS> { std::int64_t do_handle(std::int64_t x) const noexcept { return x + 1; } };

int main(int argc, char** argv) {
    const char* mode = argc > 1 ? argv[1] : "mono";
    constexpr std::size_t N = 1u << 16;
    constexpr int REPS = 2000;
    std::vector<std::int64_t> data(N);
    std::mt19937 rng(1); for (auto& d : data) d = rng();

    auto run = [&](auto dispatch) {
        auto t0 = std::chrono::steady_clock::now();
        std::int64_t acc = 0;
        for (int r = 0; r < REPS; ++r)
            for (std::size_t i = 0; i < N; ++i) acc += dispatch(i, data[i]);
        auto t1 = std::chrono::steady_clock::now();
        double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
        std::printf("%-5s acc=%lld  %.3f ns/call\n", mode, (long long)acc, ns/((double)REPS*N));
    };

    if (std::strcmp(mode,"mono")==0) {                       // monomorphic virtual
        std::unique_ptr<IH> h = make_handler();              // opaque: see factory above
        run([&](std::size_t, std::int64_t x){ return h->handle(x); });
    } else if (std::strcmp(mode,"mega")==0) {                // megamorphic virtual
        std::vector<std::unique_ptr<IH>> hs;                 // 4 types, interleaved by data
        hs.emplace_back(new AddH); hs.emplace_back(new XorH);
        hs.emplace_back(new MulH); hs.emplace_back(new ShrH);
        run([&](std::size_t i, std::int64_t x){ return hs[data[i] & 3]->handle(x); });
    } else if (std::strcmp(mode,"func")==0) {                // std::function
        std::function<std::int64_t(std::int64_t)> f = make_func();   // opaque, see above
        run([&](std::size_t, std::int64_t x){ return f(x); });
    } else {                                                 // CRTP / static
        AddS s; run([&](std::size_t, std::int64_t x){ return s.handle(x); });
    }
    return 0;
}
```

Profile with `perf stat -e cycles,instructions,branches,branch-misses,br_misp_retired.indirect`. Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP), `-O2 -march=native`, pinned, turbo off (illustrative; *relative* costs are the point):

```
                          crtp/static   mono virtual   std::function   mega virtual
ns / call                   ~0.3 ns        ~0.9 ns        ~1.4 ns        ~3.5 ns
IPC                          high           good           lower          LOW
indirect branch-misses       ~0             ~0 (BTB nails) ~0–low         HIGH (~50–75%)
inlined?                     YES            no             no             no
notes                     no indirection  predicted     no inlining    unpredictable
```

Read it the Ch. 2 / Ch. 13 way:

- **CRTP/static is fastest** because the call is *inlined away* — no indirection, no prediction, and the callee folds into the loop (Ch. 11). This is the "zero-cost abstraction" (Ch. 19) ideal.
- **Monomorphic virtual is cheap but not free:** the BTB predicts the single target perfectly (`indirect branch-misses ~0`), so the cost is the vtable load + the *lost inlining* of the trivial callee — a real but modest gap vs CRTP.
- **`std::function` is worse** — an empty-check plus an indirect call to the invoker, and no inlining — even though its target is also monomorphic here.
- **Megamorphic virtual is the disaster:** 4 unpredictably-interleaved targets give the indirect predictor nothing to learn, `br_misp_retired.indirect` spikes to ~50–75%, IPC craters, and each dispatch eats a flush (Ch. 13.2.2) plus I-cache thrash (Ch. 12). ~10× the CRTP cost — most of it dispatch overhead, though note the `mega` column also carries one extra dependent load (`hs[data[i] & 3]` before the vptr load) that `mono` does not.

The fingerprint to recognize: **high `br_misp_retired.indirect` with low IPC on a dispatch-heavy loop** = megamorphic indirect-call bottleneck. That's the signal §14.4 addresses; a clean (monomorphic) indirect predictor means the call is *not* your problem and CRTP-ifying it buys little.

**The lighter erasers, on the same loop** (§14.4.5; VM-measured — the 4-vCPU Xeon VM of Appendix L.2, GCC 13.3 / Clang 18.1 `-O2 -std=c++23`; a `[[gnu::noinline]] drive(F&&, n)` calling `f(i)` 100 M times with a monomorphic 8-byte-capture lambda):

```
                                 GCC 13.3     Clang 18.1
template parameter (lambda)      inlined — the loop folded to closed form
function pointer                 1.29 ns      1.29 ns      one indirect call, args in registers
function_ref (two words)         1.04         1.04         one indirect call through the thunk, args in registers
inplace_function<…, 32>          1.24         1.03         same shape, storage inline
std::function                    1.04         1.14         empty check + indirect call; argument SPILLED to the stack (§14.5)
std::move_only_function          1.19         1.03         no empty check; same indirect call
std::any + any_cast per call     0.41         0.39         one type_info compare, then a DIRECT inlined call — not erasure
sizeof: function_ref 16  inplace_function<,32> 48  std::function 32  move_only_function 40  std::any 16
allocation at construction: std::function — 8 B / 16 B capture: none, 24 B: one malloc; move_only_function — 24 B capture: none
```

Every *indirect* form lands within 1.0–1.3 ns of each other once the target is monomorphic and predicted: the cost is the un-inlinable call, not the wrapper. What distinguishes them is what happens *outside* the call — `std::function` may allocate at construction and spills its argument, `function_ref` can never allocate and can dangle, `move_only_function` can hold what `std::function` cannot — and §14.4.5 chooses by those properties.

---

## 14.4 Techniques

The ladder, from least to most invasive: **(1)** recover direct calls/inlining without changing the design (`final`, LTO devirtualization); **(2)** dispatch at compile time where the type set is known (CRTP); **(3)** use a closed-set runtime mechanism that avoids the BTB (`std::variant`/visitor, or a `switch`); **(4)** for an open set, make the *site* monomorphic (sort/batch by type) so even a virtual call predicts. Choose by whether the type set is closed or open, and whether dispatch is known at compile time.

### 14.4.1 `final`, speculative and profile-guided devirtualization

The cheapest win: let the compiler turn the indirect call back into a *direct* (inlinable) one.

- **`final`.** Marking a class or virtual method `final` tells the compiler no further override exists. If it can prove the static type's method is `final` (or the class is `final`), it **devirtualizes** — replaces the indirect call with a direct call, which then **inlines**. This is free and should be the default for leaf classes and methods in a sealed hierarchy:
  ```cpp
  struct AddH final : IH { std::int64_t handle(std::int64_t x) const noexcept final { return x+1; } };
  // AddH* a; a->handle(x);  →  compiler can call AddH::handle directly and inline it
  ```
  The catch: it only fires when the compiler knows the *concrete* static type at the call site (e.g. through a `AddH*`, not a base `IH*`). Through a base pointer it still can't tell which type — `final` helps the optimizer where the type is statically recoverable.
- **Speculative devirtualization.** With profile information (or heuristics), the compiler can emit *"if the type is the common one, call it directly (inlined); else fall back to the indirect call."* This converts a near-monomorphic site into a predictable direct call for the hot type, paying the indirect path only for the rare types.
- **LTO + PGO devirtualization (Ch. 24).** Whole-program LTO lets the compiler see the *entire* class hierarchy and prove a call is monomorphic across the program (e.g. only one override is ever instantiated), devirtualizing calls it couldn't within a single translation unit. PGO supplies the type-frequency profile that drives speculative devirtualization. For a large codebase, **LTO+PGO is the highest-leverage devirtualization lever** because it works without redesigning the dispatch.

Devirtualization is the "keep your `virtual` design but pay direct-call prices" option — try it *first*, since it costs no design change. When it can't fire (genuinely open, runtime-varying types), move down the ladder.

### 14.4.2 CRTP and static polymorphism

When the set of types is known **at compile time** at the call site, dispatch *at* compile time and pay nothing at runtime. The **Curiously Recurring Template Pattern** does this: the base is templated on the derived type and `static_cast`s to it, so every call is a direct, inlinable call resolved by the compiler — no vptr, no indirection, no prediction (the CRTP row in §14.3):

```cpp
template <class Derived>
struct Handler {
    std::int64_t handle(std::int64_t x) const noexcept {
        return static_cast<const Derived*>(this)->do_handle(x);   // resolved at compile time
    }
};
struct AddHandler : Handler<AddHandler> {
    std::int64_t do_handle(std::int64_t x) const noexcept { return x + 1; }   // inlined
};
template <class H> std::int64_t run(const H& h, std::int64_t x) { return h.handle(x); }
```

This is the **zero-cost abstraction** (Ch. 19) and the right default for hot-path policy/strategy dispatch when the strategy is chosen at compile time (a templated order-book, a templated feed parser specialized per protocol). Trade-offs: it's compile-time only (you can't choose the type from a config file at runtime), it can increase code size (one instantiation per type — Ch. 12), and it changes interfaces from `Base*` to templates (more compile time, template error messages — Ch. 18). When you genuinely need *runtime* selection, CRTP doesn't apply; use §14.4.1, §14.4.3, or §14.4.4.

### 14.4.3 `std::variant`/visitor alternatives

When the type set is **closed** (a known, fixed set of message/event types) but the choice is made at **runtime**, `std::variant<A, B, C, ...>` + `std::visit` is the type-safe, **allocation-free, no-vtable** alternative to a `virtual` hierarchy:

```cpp
using Event = std::variant<AddOrder, CancelOrder, Trade>;     // closed set, value semantics
std::int64_t dispatch(const Event& e) {
    return std::visit([](const auto& ev){ return ev.process(); }, e);   // no heap, no vptr
}
```

Why it can beat `virtual` on the hot path:

- **Value semantics, no allocation, dense storage.** A `variant` holds the object *inline* (sized to the largest alternative), so a `vector<Event>` is contiguous and cache-friendly (Ch. 8) — unlike a `vector<unique_ptr<Base>>` whose objects are scattered on the heap (a pointer-chase per element, Ch. 7) and each carry a vptr.
- **The compiler sees all alternatives.** How `std::visit` lowers is library- and size-dependent: libstdc++ (GCC 10+) emits a `switch` over the type index for a single-variant visit with ≤ 11 alternatives — a compare/branch chain for ~3 alternatives, a **jump table** for more — and falls back to a function-pointer table (an indirect *call*) beyond that or for multi-variant visits; libc++ dispatches through a function-pointer table. Either way the bodies are known, so they can **inline**. Where it is a jump table it's still an indirect jump (§14.4.4's caveat) whose target prediction is no better than a vtable's for the same tag stream; the win is inlining and contiguous, allocation-free storage — and, when the lowering is a branch chain, *direction* prediction (Ch. 13) instead of target prediction.

Trade-offs: the set must be *closed* (adding a type means editing the `variant` and every exhaustive `visit` — the opposite of open `virtual` extensibility, sometimes a feature for correctness); the `variant` is sized to its largest member (padding waste if sizes differ wildly); and `std::visit`'s codegen quality varies (verify — §14.5). For a feed handler with a *fixed* protocol message set, `variant`+visit is often the sweet spot: closed, contiguous, inlinable, no allocation.

### 14.4.4 Jump tables vs branch chains

The lowest-level dispatch is a `switch` on an integer type tag — and how the compiler lowers it matters:

- **Branch chain.** A `switch`/`if-else` ladder over a *few* sparse cases compiles to a sequence of compares + conditional branches (Ch. 13). Predictable if the case distribution is biased; mispredicts if data-dependent and balanced.
- **Jump table.** A `switch` over a *dense* range of integer tags compiles to an **indexed indirect jump** through a table of code addresses — O(1), but it's an *indirect branch* with the same BTB target-prediction problem (§14.2.2). For a dense, megamorphic tag stream it can mispredict as badly as a vtable.
- **Data lookup (Ch. 13.4.2).** Often best: if each case maps to *data* (a parameter, a small computation) rather than wildly different *code*, index a **data table** and avoid the indirect jump entirely — no target prediction needed. This is the Ch. 13 lookup-table idea applied to dispatch.

The decision: dispatching to genuinely different *code* over a closed set → prefer `variant`/visit (§14.4.3) or a `switch` (let the compiler pick the lowering — verify in §14.5); dispatching to different *data*/parameters → a data lookup table beats them all by removing the indirect branch. And recall Ch. 13's other lever: if the tag stream is unpredictable, **sort/batch by type** upstream so even a `switch`/vtable sees long monomorphic runs the predictor loves — turning a megamorphic site monomorphic without changing the dispatch mechanism.

---

### 14.4.5 Lighter type erasure: `function_ref`, `move_only_function`/`copyable_function`, in-place functions

When a template parameter is not available — a callback crossing a non-template boundary, a hook table filled at startup, a completion routine handed to an I/O layer (Ch. 54–55) — choose the eraser by *ownership*, because the call cost is the same (§14.3):

- **Passing a callable *down* a call chain: `function_ref`.** Two words (object pointer + thunk), trivially copyable (two registers — Ch. 21 §21.4.5), no allocation ever, no empty state, one `call [thunk]`. C++26's `std::function_ref` (P0792) is not in libstdc++ 13 / libc++ 18; `tl::function_ref`, `llvm::function_ref` or this is what ships today:

  ```cpp
  template <class Sig> class function_ref;
  template <class R, class... A> class function_ref<R(A...)> {
      void* obj_; R (*thunk_)(void*, A...);                       // NOT owned: the callable must outlive the ref
  public:
      template <class F> requires (!std::is_same_v<std::remove_cvref_t<F>, function_ref>)
      function_ref(F&& f) noexcept : obj_(const_cast<void*>(static_cast<const void*>(std::addressof(f)))),
          thunk_([](void* o, A... a) -> R { return (*static_cast<std::remove_reference_t<F>*>(o))(std::forward<A>(a)...); }) {}
      R operator()(A... a) const { return thunk_(obj_, std::forward<A>(a)...); }
  };
  void for_each_level(const Book&, function_ref<void(const Level&)> f);   // callers pass any lambda; no template in the header
  ```

  Its one hazard is lifetime: a `function_ref` *stored* in a member or a container outlives the temporary lambda it was built from and dangles (§14.6). P0792 adds `nontype_t<&free_fn>` to bind a free or member function with no object at all. Use it for parameters, never for members.
- **Storing a callable that owns resources or is move-only: `std::move_only_function` (C++23).** Holds a lambda capturing a `unique_ptr`, a socket or a `std::jthread`; `const`-, `noexcept`- and ref-qualifiable signatures (`move_only_function<void(const Fill&) const noexcept>`), no copy requirement, no empty check on call (calling empty is UB — the type system, not a branch, is the guard). libstdc++ 13 stores up to 24 bytes inline (§14.3: the 24-byte capture that made `std::function` `malloc` did not here); larger captures still allocate — construct at setup. `std::copyable_function` (C++26) when the stored callable must be copied.
- **Storing with a hard no-allocation guarantee: a fixed-capacity in-place function.** `inplace_function<Sig, N>` (SG14's `stdext::inplace_function`, or forty lines of your own) owns the callable in an `alignas(16) unsigned char buf[N]` and `static_assert`s that anything larger does not compile — the only eraser that turns "this might allocate" into a build error. Same call cost (§14.3). The price is `N` bytes per slot whether used or not, and a copy of `N` bytes when the slot is copied.
- **`std::function` for configuration and cold paths**, where copyability and convenience matter and the call is rare. **`std::any` for a value whose type is genuinely unknown**, never as a dispatch mechanism: the `any_cast` names the type, so the call is direct (§14.3's 0.4 ns is a `type_info` compare plus an inlined call) — if you know the type, you did not need `any`.

The decision table: *fixed callee* → template parameter (§14.4.2); *passed through* → `function_ref`; *stored, owning* → `move_only_function` (or `inplace_function` when allocation must be impossible); *stored, copyable, cold* → `std::function`. Ch. 22 §22.4.1 restates this for lambdas and ranges callbacks; Ch. 54–55's completion callbacks and Ch. 88 §88.4.4's strategy hooks are the storing cases.

## 14.5 Verify the codegen: devirtualized call sites

Whether a call is a direct/inlined call or an opaque indirect jump is visible in the asm, and it's the verification that the technique fired. Snippets below are **verified** Clang 18.1 output (Appendix L.3; `--target=x86_64-linux-gnu -O2 -march=x86-64-v3`).

**An indirect virtual call (not devirtualized).** Through a base pointer the compiler can't know the type — it loads the vptr, indexes the vtable, and does an indirect `call`:

```asm
        mov     rax, qword ptr [rdi]        ; load vptr from the object        (dependent load)
        call    qword ptr [rax]             ; indirect call through vtable[0]  (BTB-predicted)
                                            ; — opaque: nothing inlined across it
```

That `call [rax]` is the indirect branch the BTB must predict (§14.2.2) and the wall the optimizer can't see through.

**Devirtualized via `final`.** When the static type is known and the method is `final`, the indirect call becomes a *direct* call — and a trivial body **inlines away entirely**:

```asm
addhandler_handle(long):        ; AddH final; called through a known AddH* / value
        lea     rax, [rdi + 1]  ; the whole body: x + 1 — inlined, NO call, NO vtable
        ret
```

No vptr load, no `call` — the dispatch vanished. This is the §14.4.1 win and the §14.3 CRTP row in asm form: when you mark leaf classes `final` (or use CRTP), confirm you see the *body*, not a `call [rax]`.

**`std::variant` visit — closed-set dispatch.** With a jump-table lowering (libstdc++'s `switch` with enough alternatives, or a `switch` you wrote) `std::visit` becomes an indexed jump over the small type index, with the (known) bodies inlinable — note that for the 3-alternative `Event` above, libstdc++ with GCC 13/Clang 18 actually emits a compare/branch chain (`cmp`/`je` on the index) around the same inlined bodies, and libc++ an indirect `call` through a function-pointer table:

```asm
        movzx   eax, byte ptr [rdi + N]     ; the variant's type index (small int)
        jmp     qword ptr [.Ljt + 8*rax]    ; indexed indirect jump — closed set
.Ljt:   .quad   .Lcase_add, .Lcase_cancel, .Lcase_trade
.Lcase_add:                                 ; bodies known → can inline the work here
        ...
```

Still an indirect jump (so verify the predictor is happy for *your* tag stream — §14.4.4), but over a small closed set the bodies are visible and inlinable, unlike the opaque vtable call.

The verification habit: **for a hot dispatch site, disassemble and check whether you see a direct/inlined body (devirtualized — good), a small closed-set branch chain or jump table (`variant`/`switch` — acceptable, check prediction), or an opaque `call [rax]`/`call [reg]` (live indirect — the thing to eliminate on a megamorphic hot path).** When `final`/LTO *should* have devirtualized and you still see `call [rax]`, the type wasn't statically recoverable — that tells you to change the call site (concrete type, CRTP, or `variant`) rather than hope.

---

**`function_ref` vs `std::function` at the call site (§14.4.5).** Inside the `drive` loop (`clang++ -O2 -std=c++23`):

```asm
; function_ref:  two loads, one indirect call, argument in a register
        mov     rdi, qword ptr [r14]        ; obj_
        mov     rsi, r12                    ; i
        call    qword ptr [r14 + 8]         ; thunk_ — the ONLY indirection
; std::function:  empty check, argument spilled so the invoker can take its address, then the call
        mov     qword ptr [rsp + 8], r13    ; i → stack (libstdc++'s invoker takes forwarding references)
        cmp     qword ptr [r14 + 16], 0     ; _M_manager == nullptr ?  (the empty check)
        je      .throw_bad_function_call
        mov     rdi, r14
        lea     rsi, [rsp + 8]              ; &i
        call    qword ptr [r14 + 24]        ; _M_invoker
```

Both are one indirect call; the `std::function` adds a predicted branch and a store/reload of every argument. `move_only_function` drops the `cmp`/`je`; `inplace_function` looks like `function_ref` with `obj_` replaced by the address of its own buffer. The template-parameter version has no `call` at all — which is the first thing to grep for.

## 14.6 Pitfalls & anti-patterns: megamorphic call sites; `std::function` in the loop

- **Megamorphic virtual dispatch on the hot path.** The headline anti-pattern: one `base->handle()` site fed an unpredictable mix of many concrete types (§14.3 `mega`). The BTB is overwhelmed, `br_misp_retired.indirect` spikes, I-cache thrashes (Ch. 12), and each dispatch eats a flush. Fix: CRTP (compile-time), `variant`/visit (closed set), or sort/batch by type to make the site monomorphic.
- **`std::function` in the inner loop.** Double indirection, no inlining, and *possible heap allocation* on construction (Ch. 26) — the most expensive common dispatch (§14.2.3, §14.3). Reserve it for setup/cold paths; on the hot path use a template parameter, function pointer, `variant`, or CRTP.
- **A dangling `function_ref`.** It does not own its callable; a `function_ref` member initialised from a temporary lambda (`hooks.on_fill = [&]{…};`) points at a destroyed closure after the statement ends (§14.4.5). `function_ref` is a *parameter* type; store `move_only_function`/`inplace_function` instead.
- **`std::any` as a dispatch mechanism.** The `any_cast` must name the type, so the call is direct and the erasure bought a `type_info` compare and a `bad_any_cast` path (§14.3). Erase callables with a callable eraser or, better, a closed `variant`.
- **Constructing `std::function`/callables on the hot path.** Even ignoring the call cost, *building* a `std::function` may `malloc`. Hoist construction to setup; never create type-erased callables per message.
- **Assuming `final` always devirtualizes.** It only fires when the compiler can statically recover the concrete type at the call site; through a base pointer it often can't. Verify in the asm (§14.5); if it didn't fire, change the call site, don't assume.
- **`virtual` on a tiny, hot, monomorphic callee.** Even with perfect BTB prediction, the *lost inlining* of a 2-line function can dominate (§14.2.1, §14.3 `mono` vs `crtp`). If the type is compile-time known, CRTP; if runtime but monomorphic, `final`/LTO to recover the inline.
- **CRTP where you need runtime selection.** CRTP is compile-time only. Forcing it where the type must come from config/runtime leads to contortions; use a closed-set runtime mechanism (`variant`) or accept a (devirtualized/monomorphic) virtual call instead.
- **Jump table assumed cheap.** A `switch` lowered to a jump table is *still an indirect branch* (§14.4.4) — megamorphic, data-dependent tags mispredict through it too. If cases map to *data*, prefer a data lookup (Ch. 13.4.2) that has no indirect branch.
- **Optimizing dispatch when it isn't the bottleneck.** A *monomorphic* indirect call is cheap (§14.3). If `br_misp_retired.indirect` is low, the call isn't your problem — top-down first (Ch. 2), don't CRTP-ify a clean call site for nothing.

---

## 14.7 Exercises & checklist

**Exercises**

1. **Measure the dispatch ladder.** Build `dispatch.cpp`, run `crtp`/`mono`/`func`/`mega` pinned/turbo-off with `perf stat -e cycles,instructions,branches,branch-misses,br_misp_retired.indirect`. Confirm: CRTP fastest (inlined), mega worst (high indirect mispredicts). What's the IPC of each? Which top-down bucket (Ch. 2) is `mega`?
2. **Make the megamorphic site monomorphic.** Take the `mega` case and **sort** the work by type so each handler runs in a long contiguous run. Re-measure `br_misp_retired.indirect` and ns/call. How close to `mono` does it get, and why (Ch. 13.2.1)?
3. **`final` and the asm.** Mark the handler classes/methods `final` and call through a *concrete* type. Disassemble (Ch. 4): did the call devirtualize and inline? Now call through `IH*` — does `final` still help? Explain (§14.4.1, §14.5).
4. **`variant` vs virtual.** Reimplement the closed 4-type set as `std::variant` + `std::visit` in a contiguous `vector<Event>`. Compare ns/call *and* cache behavior (`L1-dcache-load-misses`) against `vector<unique_ptr<IH>>`. Where does the win come from — dispatch, or layout (Ch. 8)?
5. **`std::function` allocation.** Construct `std::function`s from progressively larger captures and find where SBO stops and heap allocation begins (instrument `operator new`, or read the library's SBO size). Confirm constructing on the hot path is a `malloc` (Ch. 26).

**Checklist — indirect dispatch**

- [ ] I confirmed (top-down, Ch. 2) the hot dispatch is actually costly — elevated **`br_misp_retired.indirect`** / low IPC — before redesigning; a *monomorphic* call I left alone.
- [ ] Compile-time-known type sets use **CRTP / static polymorphism** (inlined, zero indirection) on the hot path.
- [ ] Closed runtime type sets use **`std::variant`/visit** (contiguous, no allocation, no vtable) rather than `vector<unique_ptr<Base>>`.
- [ ] Where I kept `virtual`, I applied **`final`** and **LTO/PGO** to devirtualize, and **verified in the asm** (Ch. 4) the call became direct/inlined.
- [ ] **No `std::function` on the hot path** and **no type-erased-callable construction** per message; those live in setup/cold paths.
- [ ] Callables passed *down* a call chain are **`function_ref`** (never stored); stored callables are **`move_only_function`** or a fixed-capacity in-place function whose capacity is a compile-time fact (§14.4.5).
- [ ] Megamorphic sites I couldn't redesign I made **monomorphic by sorting/batching by type** so the BTB predicts (Ch. 13).
- [ ] For tag dispatch, I chose **data lookup** (no indirect branch) when cases map to data, not a jump table, and verified the lowering (§14.5).
- [ ] I measured **dispatch cost *and* layout/cache effects** (Ch. 7–8), not just the call.

---

## 14.8 References

- Intel, *64 and IA-32 Architectures Optimization Reference Manual* — indirect-branch prediction (the BTB), indirect-call mispredict cost, and the `BR_MISP_RETIRED.INDIRECT` event used in §14.3.
- A. Fog, *The Microarchitecture of Intel, AMD and VIA CPUs* and *Optimizing C++* — the cost of virtual calls, indirect-branch prediction behavior, and devirtualization in practice.
- The Itanium C++ ABI (vtable layout) and the GCC/Clang documentation on devirtualization, `-fdevirtualize`/`-fdevirtualize-speculatively`/`-fdevirtualize-at-ltrans`, `-fwhole-program`/LTO, the C++11 `final` specifier, and Clang's `-fwhole-program-vtables`/`-fstrict-vtable-pointers` — the machinery of §14.4.1.
- ISO C++ — `std::variant`/`std::visit`, `std::function` and the small-buffer optimization; cppreference for the type-erasure and visitation semantics of §14.2.3 and §14.4.3.
- A. Yasin, *"A Top-Down Method for Performance Analysis"* (Ch. 2) — isolating Bad Speculation to indirect branches before redesigning dispatch.

## 14.9 Additional Reading

- CppCon talks on "the cost of `virtual`," type erasure, and "inheritance is the base class of evil" — design-level treatments of replacing runtime polymorphism on the hot path.
- D. Bakhvalov, *Performance Analysis and Tuning on Modern CPUs* — indirect-branch mispredict analysis and devirtualization with `perf`/LTO/PGO.
- Ch. 19 (*Template Metaprogramming & Zero-Cost Abstractions*) — CRTP, policy-based design and static dispatch in full; Ch. 21 (*The Cost of Abstractions*) — exception-unwinding, `noexcept` and RTTI costs in depth; Ch. 28 (*Cache-Friendly Containers*) — `std::function` among the hot-path-hostile STL types (§28.2), and why `vector<unique_ptr<Base>>` is a cache anti-pattern; Ch. 24 (*Build Toolchain*) — LTO/PGO devirtualization.
- **Appendix E** — the indirect-branch-mispredict penalty that frames "megamorphic dispatch."

---

*Next: Ch. 15 — Virtual Memory, the TLB & Huge Pages, where we leave the execution pipeline for the address-translation machinery behind every load and store: how the page-table walk and TLB misses tax memory access, and how transparent and explicit huge pages cut that tax for large working sets and hot code.*
