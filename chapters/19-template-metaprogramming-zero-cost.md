# Part III — Compile-Time & Language Mechanics

# Chapter 19 — Template Metaprogramming & Zero-Cost Abstractions

> **Prerequisites:** Ch. 18 (compile-time mechanics — `if constexpr`, the template cost model, compile-time evaluation this chapter builds *designs* on), Ch. 14 (indirect dispatch — CRTP here is the by-construction devirtualization that chapter motivated), Ch. 11 (the inlining and back-end ILP that "zero-cost" actually cashes out as), Ch. 4 (reading asm — §19.5 is the whole proof).
>
> **Leads into:** Ch. 20 (the cost of abstractions that *aren't* free — exceptions, RTTI, `std::function`), Ch. 25 (cache-friendly containers and intrusive/policy designs), Ch. 29 (SIMD via expression templates / static specialization), Ch. 24 (policy-based allocators). The bloat/error-message costs tie back to Ch. 12 and Ch. 18.

---

## 19.1 Why it matters: abstraction without the runtime tax

"Zero-cost abstraction" is C++'s defining promise: that a well-designed abstraction compiles to *the same machine code you'd have written by hand* — that you pay nothing at runtime for the expressiveness. Chapter 14 showed the *failure* of this promise (a `virtual` call that can't inline, a `std::function` that allocates); this chapter shows how to *keep* it. The mechanism is the templates and compile-time machinery of Ch. 18, used not to compute values but to make **design decisions at compile time** — which type, which policy, which operation — so that by the time the optimizer runs, there is nothing left to dispatch, nothing to indirect, nothing generic. The abstraction exists in the *source*, for the human; in the *binary* it has evaporated.

For low-latency code this is the difference between a clean, reusable, *fast* codebase and one where every abstraction is a tax you pay per tick. An order book templated on its price-level-storage policy, a feed parser templated on its protocol, a `Strategy` resolved at compile time via CRTP, a `Price` type that's a zero-overhead wrapper around an `int64` — each gives you the readability and safety of an abstraction with the codegen of raw, specialized code (verified in §19.5). The alternative — runtime polymorphism on the hot path (Ch. 14) — buys flexibility you usually don't need there (the set of protocols, strategies, storage policies is *known at build time*) at a per-call cost you can't afford.

But "zero-cost" is a claim that must be **verified, not assumed**, and the abstractions have their own costs on *other* axes — exactly the trade Ch. 18 introduced. Templates that specialize beautifully at runtime can explode build times and bloat the binary (Ch. 12, 18), and produce the legendarily awful error messages that make metaprogramming a productivity hazard if overused. So this chapter is a paired discipline: the techniques that make abstractions compile away (CRTP, expression templates, policy-based design — §19.4), and the verification that they *actually did* (§19.5) plus the costs that bite when they don't (§19.6). The goal is the abstraction that is free where it matters — the runtime hot path — and whose price is paid where you can afford it: the build.

---

## 19.2 Mental model: CRTP, type-based dispatch, policy-based design

The unifying idea: **replace runtime decisions with compile-time ones by encoding the "varying" part in the *type*.** Where runtime polymorphism stores the choice in data (a vptr, a function pointer) and resolves it per call, static polymorphism encodes it in a template parameter and resolves it *once*, at instantiation, leaving the optimizer a fully-concrete program. Three patterns, increasing in scope:

**CRTP (Curiously Recurring Template Pattern) — static polymorphism.** A base class templated on its derived type, so the base can call into the derived *without* a virtual call (introduced in Ch. 14.4.2):

```cpp
template <class Derived>
struct StrategyBase {
    void on_tick(const Tick& t) {                       // shared interface / control flow
        static_cast<Derived*>(this)->handle(t);         // dispatch resolved at COMPILE time → inlined
    }
};
struct Momentum : StrategyBase<Momentum> {
    void handle(const Tick& t) { /* ... */ }            // the "override", inlined into on_tick
};
```

The base provides shared structure (the template-method pattern, the interface); the derived provides the specifics; the `static_cast` to `Derived` makes every call direct and inlinable. No vtable, no vptr, no indirect-branch prediction (Ch. 14) — the dispatch is *gone*.

**Type-based dispatch — choosing code by type at compile time.** Selecting an implementation based on a type's properties, with **no runtime branch**: `if constexpr` (Ch. 18) on a trait, tag dispatch (overloading on a tag type), and concepts/`requires` (C++20) to constrain and select overloads. The taken path is compiled; the others are discarded (Ch. 18). This is how a generic algorithm specializes — a SIMD path for arithmetic types, a `memcpy` path for trivially-copyable types (Ch. 9), a fast path for a known protocol — all resolved at build time.

**Policy-based design — composing behavior from compile-time parameters.** Parameterize a class on *policy* types that each supply one axis of behavior, composed at compile time:

```cpp
template <class StoragePolicy, class LockingPolicy, class AllocPolicy>
class OrderBook : StoragePolicy, LockingPolicy { /* ... */ };   // mix behaviors, zero indirection
// OrderBook<FlatArrayLevels, NoLock, ArenaAlloc>   — each policy inlined, specialized, no vtable
```

Each policy's methods inline; swapping a policy is a compile-time type change, not a runtime branch. (Empty policies cost *nothing* via the empty-base optimization / `[[no_unique_address]]` — Ch. 9.) This is the configurable-yet-zero-cost design: the order book's level storage, locking, and allocation are all choices made at compile time, each compiling to the hand-specialized code.

The cost model carries over unchanged from Ch. 18: these are all **template instantiations** — each combination is a distinct body (good: specialized and inlined; bad: code-size/build-time cost — §19.6). And "zero-cost" cashes out *through the optimizer*: static dispatch enables inlining (Ch. 11), which enables constant propagation and scheduling across the old call boundary. The abstraction is free **because** it inlines — which is exactly why §19.5 verifies inlining actually happened.

---

## 19.3 Measure it: abstraction overhead vs hand-written code

The claim under test is literally "zero overhead," so the measurement compares an *abstracted* implementation against the *hand-written* code it should compile to — and against the runtime-polymorphic version (Ch. 14) it replaces. Same work: dispatch a stream through a "strategy" that does a trivial update, four ways — (a) hand-written direct call, (b) CRTP static polymorphism, (c) `virtual`, (d) `std::function`.

```cpp
// zerocost.cpp — hand-written vs CRTP vs virtual vs std::function, same work.
// Build: g++ -O2 -std=c++20 -march=native zerocost.cpp -o zerocost
// Run pinned, turbo off:  taskset -c 2 ./zerocost hand|crtp|virt|func
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <functional>
#include <memory>
#include <vector>
#include <chrono>

struct Tick { std::int64_t px; std::int32_t qty; };

// (a) hand-written: the code we WANT the abstraction to match.
static inline std::int64_t hand_update(std::int64_t s, const Tick& t) { return s + t.px * t.qty; }

// (b) CRTP static polymorphism.
template <class D> struct Strat { std::int64_t update(std::int64_t s, const Tick& t) {
    return static_cast<D*>(this)->go(s, t); } };
struct Vwap : Strat<Vwap> { std::int64_t go(std::int64_t s, const Tick& t){ return s + t.px*t.qty; } };

// (c) virtual.
struct IStrat { virtual std::int64_t update(std::int64_t s, const Tick& t) const noexcept = 0;
                virtual ~IStrat() = default; };
struct VwapV : IStrat { std::int64_t update(std::int64_t s, const Tick& t) const noexcept override {
    return s + t.px*t.qty; } };

int main(int argc, char** argv) {
    const char* mode = argc > 1 ? argv[1] : "hand";
    constexpr std::size_t N = 1u << 16; constexpr int REPS = 4000;
    std::vector<Tick> in(N);
    for (std::size_t i = 0; i < N; ++i) in[i] = { std::int64_t(100+i), std::int32_t(1+(i&7)) };

    auto bench = [&](auto fn) {
        auto t0 = std::chrono::steady_clock::now();
        std::int64_t s = 0;
        for (int r = 0; r < REPS; ++r) for (const Tick& t : in) s = fn(s, t);
        auto t1 = std::chrono::steady_clock::now();
        double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
        std::printf("%-5s s=%lld  %.3f ns/call\n", mode, (long long)s, ns/((double)REPS*N));
    };

    if (!std::strcmp(mode,"hand")) bench([](std::int64_t s, const Tick& t){ return hand_update(s,t); });
    else if (!std::strcmp(mode,"crtp")) { Vwap v; bench([&](std::int64_t s, const Tick& t){ return v.update(s,t); }); }
    else if (!std::strcmp(mode,"virt")) { std::unique_ptr<IStrat> v=std::make_unique<VwapV>();
        bench([&](std::int64_t s, const Tick& t){ return v->update(s,t); }); }
    else { std::function<std::int64_t(std::int64_t,const Tick&)> f =
        [](std::int64_t s, const Tick& t){ return s + t.px*t.qty; };
        bench([&](std::int64_t s, const Tick& t){ return f(s,t); }); }
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP), `-O2 -march=native`, pinned, turbo off (illustrative; the point is *which match*):

```
                    hand-written    CRTP        virtual      std::function
ns / call             ~0.30 ns      ~0.30 ns    ~0.9 ns      ~1.4 ns
matches hand?            —          YES (≡)      no           no
inlined?               yes          yes          no           no
asm (§18.5)          identical    identical    call [rax]   double indirection
```

Read it the Ch. 14 / Ch. 18 way: **CRTP is *byte-for-byte* the hand-written code** — same ns/call, same asm (§19.5) — because the static dispatch inlined and the optimizer then treated it exactly as the direct version. That is "zero-cost" *demonstrated*, not asserted: the abstraction (a reusable `Strat<>` base, a clean `update()` interface) cost the hot path nothing. The `virtual` and `std::function` versions — flexible at runtime — are 3–5× slower here purely from un-inlinable dispatch (Ch. 14), even though the callee is trivial. The discipline this sets up: when you claim an abstraction is zero-cost, **prove it matches the hand-written codegen** (§19.5); if it doesn't, the abstraction is leaking a runtime cost and you either fix it or account for it.

---

## 19.4 Techniques

### 19.4.1 CRTP for static polymorphism

The default tool for compile-time-known polymorphism on the hot path (the §19.3 winner). Use it to get the structure of an interface/base class without the vtable:

- **Template-method pattern, statically.** The CRTP base holds the *shared* control flow and calls derived hooks via `static_cast<Derived*>(this)->hook()` — every hook inlined. Ideal for a family of strategies/handlers/parsers sharing skeleton logic but differing in specifics, chosen at compile time.
- **Static interface / mixins.** A CRTP base can inject a whole interface implemented in terms of a few derived primitives (e.g. provide `!=`, `>`, `>=` from the derived's `<` and `==` — the pre-C++20 "comparable" mixin; provide iterator boilerplate from a few core ops). Reusable behavior, zero indirection.
- **When CRTP fits vs `virtual` (Ch. 14).** CRTP requires the concrete type be known at the call site at *compile* time — true for the hot path's strategies/policies/protocols (a build-time choice). When the type genuinely comes from runtime (a plugin loaded by config, heterogeneous objects in one container), CRTP can't apply; use a *closed-set* runtime mechanism (`std::variant`, Ch. 14.4.3) or an (devirtualized) virtual call. Don't contort runtime selection into CRTP.
- **Caveats.** Each derived type instantiates the base (code size — §19.6); CRTP types aren't a common base you can store heterogeneously (no `vector<Base*>` — that's the runtime-polymorphism trade); and the `static_cast` is only valid when the derived really is the template argument (a real-but-rare footgun).

### 19.4.2 Expression templates

Expression templates eliminate the **temporaries** that naïve operator overloading creates, letting `d = a + b + c` over vectors/matrices/prices compile into a *single fused loop* with no intermediate allocations — the technique behind high-performance linear-algebra libraries (Eigen, Blaze):

```cpp
// Naive: each + makes a temporary vector → multiple passes, multiple allocations.
Vec d = a + b + c;          // tmp1 = a+b; tmp2 = tmp1+c; copy to d   — 3 passes, 2 temporaries

// Expression templates: a+b+c builds a lightweight EXPRESSION TYPE (no work, no temporary);
// the assignment evaluates it element-wise in ONE fused loop.
template <class L, class R> struct AddExpr {            // represents "L + R", computes nothing yet
    const L& l; const R& r;
    auto operator[](std::size_t i) const { return l[i] + r[i]; }
    std::size_t size() const { return l.size(); }
};
// operator+ returns AddExpr<...>; operator= walks [i] once: d[i] = a[i]+b[i]+c[i]  — single pass
```

Where it pays in trading/quant code: vectorized price/risk/signal math (Greeks, P&L vectors, moving-window computations) where the naïve form would allocate and re-traverse temporaries (Ch. 23's allocation cost, Ch. 7–8's extra passes). The expression type encodes the computation in the *type*; the optimizer fuses and (often) vectorizes the single resulting loop (Ch. 29). Trade-offs are real: expression templates are *advanced* metaprogramming — complex to write, brutal error messages (§19.6), `auto` lifetime traps (an expression type can dangle if it outlives its operands), and they only pay when temporaries/extra passes actually dominate. **Prefer a mature library (Eigen/Blaze) over hand-rolling them**; reach for hand-written ones only when a measured temporary/allocation problem justifies the complexity.

### 19.4.3 Policy-based design

Compose a class from orthogonal **policy** parameters, each supplying one behavioral axis, all resolved and inlined at compile time (the `OrderBook<Storage, Locking, Alloc>` of §19.2):

- **Orthogonal axes as type parameters.** Storage layout (flat array vs intrusive list vs hash — Ch. 25), locking (none / spinlock / seqlock — Ch. 32, 35), allocation (arena / pool / pmr — Ch. 24), threading model. Each policy is a small type with the methods the host calls; swapping one is changing a template argument, not editing the host or adding a runtime branch.
- **Empty policies are truly free.** A stateless policy (e.g. `NoLock`) contributes no size via the empty-base optimization / `[[no_unique_address]]` (Ch. 9) and its (empty) methods inline to nothing — so a "configurable" design pays zero for the configurability on unused axes.
- **Compile-time configuration of the hot path.** This is the idiomatic way to build a *family* of specialized hot-path components from one source — a `OrderBook` tuned per venue, a feed handler per protocol, a queue per concurrency model — each compiling to code as tight as a bespoke implementation, with the variation captured in types (testable, named, reusable).
- **vs runtime configuration.** Policy-based design is compile-time; if the choice must be made at runtime (operator picks at startup), you instantiate the needed combinations and select among them once (a single runtime branch at setup, then the hot path is monomorphic) — *not* a per-call runtime policy. Watch the combinatorial instantiation explosion (§19.6) if axes × options is large.

The synthesis of §19.4: encode the *varying design decision* in a type (CRTP for polymorphism, policies for composed behavior, expression types for fused computation) so the optimizer sees a concrete program and the abstraction inlines away — then **verify it did**.

---

## 19.5 Verify the codegen: zero-overhead confirmation

"Zero-cost" is a codegen claim; §19.5 is how you *settle* it. The test: does the abstracted version produce **the same instructions** as the hand-written one? Snippets are **verified** Clang output (`--target=x86_64-linux-gnu -O2 -march=x86-64-v3`).

**Hand-written vs CRTP — identical.** Both the direct `hand_update` and the CRTP `Vwap::update` compile to the same body — the dispatch and the wrapper vanished:

```asm
; hand_update(long, Tick const&)   AND   Strat<Vwap>::update via Vwap   →  IDENTICAL:
        movsxd  rax, dword ptr [rsi + 8]    ; t.qty (sign-extended)
        imul    rax, qword ptr [rsi]        ; * t.px
        add     rax, rdi                    ; + s
        ret                                 ; CRTP base + static_cast + go() ALL inlined away
```

There is no trace of `StrategyBase`, no `static_cast`, no call — the abstraction is *gone*. This is the proof to demand: **diff the asm of the abstracted and hand-written versions; zero-cost means they're the same.**

**The runtime-polymorphic version, for contrast.** The `virtual` form keeps the indirect call (Ch. 14.5) — the abstraction did *not* compile away:

```asm
        mov     rax, qword ptr [rdi]        ; load vptr
        call    qword ptr [rax]             ; indirect call — NOT inlined, the abstraction leaked
```

**Expression-template fusion.** For `d = a + b + c`, confirm a *single* loop with no temporary buffers — `d[i] = a[i]+b[i]+c[i]` computed in one pass, ideally vectorized (Ch. 29):

```asm
.loop:                                       ; one fused pass, no intermediate vectors
        vmovdqu ymm0, [rsi + rax]            ; a[i..]
        vpaddd  ymm0, ymm0, [rdx + rax]      ; + b[i..]
        vpaddd  ymm0, ymm0, [rcx + rax]      ; + c[i..]   (single loop, fused, SIMD)
        vmovdqu [rdi + rax], ymm0            ; → d[i..]
        ...
```

If instead you see *multiple* loops or temporary allocations, the expression templates didn't fuse (or you forced materialization). The verification habits for this chapter:

- **For any "zero-cost" abstraction on the hot path, diff its asm against the hand-written equivalent.** Same instructions = the claim holds. A stray `call`, extra moves, or a missing inline = it leaked.
- **Confirm the dispatch inlined** (no `call [rax]`, the §19.3 winner pattern), the wrapper types produced no extra loads/stores, and empty policies added no size (`sizeof` unchanged — Ch. 9).
- **Treat a codegen regression as a bug.** When an abstraction *stops* matching hand-written code after a change (a missed inline, an accidental copy), the asm diff catches it — make it part of the perf-regression discipline (Ch. 76).

### Build-time/size caveat

Zero runtime cost is *not* zero cost (Ch. 18): each instantiation is build time + binary size. Verify the *other* axis too — `size`/`bloaty` on the binary, `-ftime-trace` on the build — so a runtime win doesn't hide an I-cache (Ch. 12) or build-time regression (§19.6).

---

## 19.6 Pitfalls & anti-patterns: template bloat, error-message cost

- **Template-instantiation bloat (the runtime cost of a compile-time technique).** Instantiating a *large* function for many types — or a policy class across a big `axes × options` matrix — emits a separate body each time, bloating the binary and the I-cache (Ch. 12). Factor type-*independent* code into a non-template base/helper so only the genuinely-varying part is instantiated. Measure binary size (Ch. 12, 18).
- **Assuming zero-cost without verifying.** The cardinal sin: claiming an abstraction is free while it quietly emits a copy, a missed inline, or an indirect call. **Always diff the asm** (§19.5); "it's a template so it must be free" is false (a template can still produce a `virtual` call, an allocation, a temporary).
- **Brutal error messages / build-time cost.** Heavy metaprogramming (deep SFINAE, expression templates, large packs) produces pages of incomprehensible diagnostics and slow builds (Ch. 18) — a real productivity and iterate-loop (Ch. 3) tax. Use **concepts/`requires`** (C++20) to constrain templates and get *readable* errors; prefer the simplest mechanism that works; don't metaprogram for cleverness.
- **CRTP misused for runtime polymorphism.** CRTP needs the type at compile time; forcing it where selection is genuinely runtime (heterogeneous storage, config-chosen plugins) leads to contortions or doesn't work. Use `std::variant` (closed set) or a devirtualized `virtual` (Ch. 14) instead.
- **Expression-template dangling (`auto` lifetime).** `auto e = a + b;` may capture *references* to `a`/`b` in the expression type; if they go out of scope before `e` is evaluated, it dangles. Evaluate expressions immediately (assign to a concrete `Vec`), and prefer a vetted library (Eigen/Blaze) that handles lifetimes.
- **Over-abstraction.** A tower of policies/CRTP layers for variation you don't actually have (YAGNI) costs build time, readability, and error-message sanity for no runtime benefit. Reach for these when there's a *real* need for compile-time variation on the hot path, not preemptively.
- **Combinatorial instantiation explosion.** `Host<A, B, C>` with several options per axis can instantiate dozens of combinations, each a code copy — build-time and size blowup (Ch. 18). Cap the matrix to combinations you actually ship.
- **Forgetting the abstraction must still be *correct*.** Static dispatch removes the runtime check but not the need for it; `static_cast`-based CRTP and policy composition can hide lifetime/aliasing bugs (Ch. 21) the compiler won't catch. Zero-cost ≠ zero-risk.

---

## 19.7 Exercises & checklist

**Exercises**

1. **Prove zero-cost.** Build `zerocost.cpp`; run `hand` vs `crtp` vs `virt` vs `func` pinned/ turbo-off. Confirm CRTP matches hand-written ns/call. Then disassemble (Ch. 4) and confirm the CRTP and hand-written bodies are **identical**, and `virt` shows `call [rax]` (§19.5).
2. **Break the inline.** Make the CRTP `go()` large (or mark it `[[gnu::noinline]]`). Does CRTP still match hand-written? Explain why "zero-cost" depends on inlining (Ch. 11), and what the asm now shows.
3. **Policy-based order book (sketch).** Write a tiny `Book<StoragePolicy>` with two policies (flat array vs `std::map`). Confirm (a) `sizeof` is unaffected by an *empty* policy (Ch. 9), (b) each policy inlines, and (c) the binary grows per instantiated combination (`size`).
4. **Bloat measurement.** Instantiate a non-trivial templated function for 1, 4, 16 types; measure binary size (`size`/`bloaty`) and build time (`-ftime-trace`). Where does bloat become a concern (Ch. 12), and how does factoring out type-independent code help (§19.6)?
5. **Expression templates vs naïve.** Using Eigen (or a tiny hand-rolled version), compute `d = a + b + c` over large vectors naïvely vs with expression templates. Compare passes/ allocations and ns/element; confirm fusion in the asm (§19.5). When does it *not* pay?

**Checklist — zero-cost abstractions**

- [ ] Hot-path polymorphism known at compile time uses **CRTP / static dispatch**, not `virtual` (Ch. 14).
- [ ] I **verified zero-cost in the asm** (§19.5) — the abstraction's codegen **matches the hand-written** equivalent (inlined, no `call [rax]`, no extra copies).
- [ ] Behavioral variation is composed via **policy parameters** (inlined, empty-base-free — Ch. 9), with runtime selection (if any) resolved **once at setup**, not per call.
- [ ] Templates instantiate **only what must vary**; type-independent code is factored out to bound **binary size / I-cache** (Ch. 12) — I measured it.
- [ ] Heavy metaprograms are constrained with **concepts/`requires`** for readable errors, and I didn't reach for expression templates without a measured temporary/allocation problem.
- [ ] Expression results are **evaluated immediately** (no `auto` dangling), preferably via a **vetted library** (Eigen/Blaze).
- [ ] I checked the **build-time axis** (`-ftime-trace`) and **combinatorial explosion** of policy matrices (Ch. 18) — runtime wins didn't hide a build/size regression.
- [ ] I didn't **over-abstract** for variation that doesn't exist (YAGNI); zero-cost still requires correctness (lifetimes/aliasing — Ch. 21).

---

## 19.8 References

- D. Vandevoorde, N. Josuttis, D. Gregor, *C++ Templates: The Complete Guide* — the definitive reference for CRTP, policy-based design, expression templates, and the instantiation cost model underlying this chapter.
- A. Alexandrescu, *Modern C++ Design* — the original, thorough treatment of **policy-based design** and type-based dispatch (§19.2, §19.4.3).
- T. Veldhuizen, *"Expression Templates"* (the founding C++ Report article) and the Eigen/Blaze documentation — the technique and its modern, production form (§19.4.2).
- ISO C++ / cppreference — concepts and `requires` (C++20), `[[no_unique_address]]`, and the empty-base optimization that make policies free (§19.4.3, ties Ch. 9).
- The C++ Core Guidelines (templates and generic programming) and Sutter/Alexandrescu, *C++ Coding Standards* — when (and when not) to reach for these abstractions (§19.6).

## 19.9 Additional Reading

- CppCon talks on "zero-cost abstractions," CRTP, and policy-based design — including before/after asm walkthroughs that mirror §19.5.
- Ch. 14 (*Indirect Calls & Virtual Dispatch*) — the runtime-polymorphism cost CRTP avoids; Ch. 18 (*Compile-Time Mechanics*) — the `if constexpr`/template foundation; Ch. 20 (*The Cost of Abstractions*) — abstractions that *aren't* free; Ch. 25 (*Cache-Friendly Containers*) and Ch. 24 (*Custom Allocators*) — policy-based containers/allocators in practice; Ch. 29 (*SIMD*) — expression-template vectorization.
- **Appendix B** — static dispatch and zero-cost abstraction in Rust (traits/generics vs `dyn`), for the cross-language reader.

---

*Next: Ch. 20 — The Cost of Abstractions, the counterweight to this chapter: the abstractions that are *not* free — exception unwinding, RTTI, `std::function` — and how to handle errors on the hot path with `noexcept`, error codes, and `std::expected` without paying the unwinder.*
