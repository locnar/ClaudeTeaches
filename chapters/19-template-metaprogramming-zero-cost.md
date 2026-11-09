# Part III — Compile-Time & Language Mechanics

# Chapter 19 — Template Metaprogramming & Zero-Cost Abstractions

> **Prerequisites:** Ch. 18 (compile-time mechanics — `if constexpr`, the template cost model, compile-time evaluation this chapter builds *designs* on), Ch. 14 (indirect dispatch — CRTP here is the by-construction devirtualization that chapter motivated), Ch. 12 §12.4.1 and Ch. 24 §24.2 (inlining — what "zero-cost" actually cashes out as) and Ch. 11 (the back-end ILP that inlining exposes), Ch. 4 (reading asm — §19.5 is the whole proof).
>
> **Leads into:** Ch. 21 (the cost of abstractions that *aren't* free — exceptions, RTTI — and `std::expected` as the hot-path alternative; `std::function`'s cost was Ch. 14 §14.2.3), Ch. 28 (cache-friendly containers and intrusive/policy designs), Ch. 32 (SIMD via expression templates / static specialization), Ch. 27 (policy-based allocators). The bloat/error-message costs tie back to Ch. 12 and Ch. 18.

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

The cost model carries over unchanged from Ch. 18: these are all **template instantiations** — each combination is a distinct body (good: specialized and inlined; bad: code-size/build-time cost — §19.6). And "zero-cost" cashes out *through the optimizer*: static dispatch enables inlining (Ch. 12 §12.4.1, Ch. 24 §24.2), which enables constant propagation and the ILP of Ch. 11 and scheduling across the old call boundary. The abstraction is free **because** it inlines — which is exactly why §19.5 verifies inlining actually happened.

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

// The virtual object comes from a noinline factory: constructed in the same function as the
// call site, GCC's speculative devirtualization (-fdevirtualize-speculatively, on at -O2)
// would see the dynamic type, guard it and inline VwapV::update — Ch. 14 §14.4.1's trick,
// which is exactly what the 'virt' column must NOT get.
struct IStrat;
[[gnu::noinline]] std::unique_ptr<IStrat> make_virt();

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
    else if (!std::strcmp(mode,"virt")) { std::unique_ptr<IStrat> v = make_virt();   // opaque factory
        bench([&](std::int64_t s, const Tick& t){ return v->update(s,t); }); }
    else { std::function<std::int64_t(std::int64_t,const Tick&)> f =
        [](std::int64_t s, const Tick& t){ return s + t.px*t.qty; };
        bench([&](std::int64_t s, const Tick& t){ return f(s,t); }); }
    return 0;
}
```

Representative results (toolchain and machine: Appendix L.3) — reference machine **Xeon Gold 6326** (Ice Lake-SP), `-O2 -march=native`, pinned, turbo off (illustrative; the point is *which match*):

```
                    hand-written    CRTP        virtual      std::function
ns / call             ~0.35 ns      ~0.35 ns    ~0.9 ns      ~1.4 ns      <- 0.35 ns = 1 cycle at 2.9 GHz
matches hand?            —          YES (≡)      no           no
inlined?               yes          yes          no           no
asm (§19.5)          identical    identical    call [rax]   double indirection
```

Read it the Ch. 14 / Ch. 18 way (the two runtime-polymorphic columns are Ch. 14 §14.3's ladder, re-run here only as the yardstick): **CRTP is the hand-written code** — same ns/call, the same instructions once inlined (§19.5) — because the static dispatch inlined and the optimizer then treated it exactly as the direct version. That is "zero-cost" *demonstrated*, not asserted: the abstraction (a reusable `Strat<>` base, a clean `update()` interface) cost the hot path nothing. The `virtual` and `std::function` versions — flexible at runtime — are 3–5× slower here purely from un-inlinable dispatch (Ch. 14), even though the callee is trivial. The discipline this sets up: when you claim an abstraction is zero-cost, **prove it matches the hand-written codegen** (§19.5); if it doesn't, the abstraction is leaking a runtime cost and you either fix it or account for it.

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

Where it pays in trading/quant code: vectorized price/risk/signal math (Greeks, P&L vectors, moving-window computations) where the naïve form would allocate and re-traverse temporaries (Ch. 26's allocation cost, Ch. 7–8's extra passes). The expression type encodes the computation in the *type*; the optimizer fuses and (often) vectorizes the single resulting loop (Ch. 32). Trade-offs are real: expression templates are *advanced* metaprogramming — complex to write, brutal error messages (§19.6), `auto` lifetime traps (an expression type can dangle if it outlives its operands), and they only pay when temporaries/extra passes actually dominate. **Prefer a mature library (Eigen/Blaze) over hand-rolling them**; reach for hand-written ones only when a measured temporary/allocation problem justifies the complexity.

### 19.4.3 Policy-based design

Compose a class from orthogonal **policy** parameters, each supplying one behavioral axis, all resolved and inlined at compile time (the `OrderBook<Storage, Locking, Alloc>` of §19.2):

- **Orthogonal axes as type parameters.** Storage layout (flat array vs intrusive list vs hash — Ch. 28), locking (none / spinlock / seqlock — Ch. 35, 38), allocation (arena / pool / pmr — Ch. 27), threading model. Each policy is a small type with the methods the host calls; swapping one is changing a template argument, not editing the host or adding a runtime branch.
- **Empty policies are truly free — static dependency injection and no-op policies.** A stateless policy (e.g. `NoLock`) contributes no size via the empty-base optimization / `[[no_unique_address]]` (Ch. 9) and its (empty) methods inline to nothing — so a "configurable" design pays zero for the configurability on unused axes. This is *dependency injection resolved at compile time*: the host takes its collaborators as type parameters (`Engine<Logger, RiskGate, Clock>`) and a test or a benchmark substitutes a **null object** — `NullLogger` whose `log(...)` is an empty inline body, `NoRisk` whose `check()` returns `accept` as a constant, `FixedClock` returning a journaled time — with no virtual call, no `if (logger)` and no runtime seam. The production binary instantiates `Engine<BinaryLogger, Gate, TscClock>`; the unit test instantiates `Engine<RecordingLogger, NoRisk, FixedClock>`; both are monomorphic and §19.5 shows the null versions leave no instructions behind.
- **Compile-time configuration of the hot path.** This is the idiomatic way to build a *family* of specialized hot-path components from one source — a `OrderBook` tuned per venue, a feed handler per protocol, a queue per concurrency model — each compiling to code as tight as a bespoke implementation, with the variation captured in types (testable, named, reusable).
- **vs runtime configuration.** Policy-based design is compile-time; if the choice must be made at runtime (operator picks at startup), you instantiate the needed combinations and select among them once (a single runtime branch at setup, then the hot path is monomorphic) — *not* a per-call runtime policy. Watch the combinatorial instantiation explosion (§19.6) if axes × options is large.

### 19.4.4 Strong types and units

Ch. 30 wraps a scaled price in `struct Price { std::int64_t ticks; }` and defers "why this costs nothing" here. The idea is older than C++ (Hungarian notation was an attempt at it) and the trading version is concrete: `Price`, `Qty`, `Notional`, `PriceDelta`, `SymbolId`, `ClOrdId`, `Seq`, `Nanos` are all `int64`/`uint64` at runtime and *all different things*, and the bugs that matter — a price added to a quantity, a tick count where a scaled price was expected, an order id compared with a sequence number, nanoseconds passed as microseconds — are type errors that `int64_t` cannot see. A **strong type** is a single-member wrapper whose *allowed operations* are the only ones defined:

```cpp
// units.hpp — strong types for the order path. Trivially copyable, one register each, zero cost (§19.5 proves it). C++20.
#include <compare>
#include <concepts>
#include <cstdint>
struct Price      { std::int64_t ticks; constexpr explicit Price(std::int64_t t) : ticks(t) {} auto operator<=>(const Price&) const = default; };   // scaled integer, Ch. 30
struct PriceDelta { std::int64_t ticks; auto operator<=>(const PriceDelta&) const = default; };
struct Qty        { std::int64_t v;     constexpr explicit Qty(std::int64_t q) : v(q) {}      auto operator<=>(const Qty&) const = default; };
struct Notional   { std::int64_t v;     auto operator<=>(const Notional&) const = default; };
enum class SymbolId : std::uint32_t {};  enum class Seq : std::uint64_t {};                 // ids: an enum class is the cheapest strong type — no arithmetic, no implicit conversion
struct ClOrdId    { std::uint64_t v; std::uint32_t slot() const { return std::uint32_t(v); } std::uint32_t gen() const { return std::uint32_t(v >> 32); } bool operator==(const ClOrdId&) const = default; };   // Ch. 91

// the allowed-operations matrix — everything NOT listed does not compile:
[[nodiscard]] constexpr PriceDelta operator-(Price a, Price b)      { return {a.ticks - b.ticks}; }   // price − price → delta
[[nodiscard]] constexpr Price      operator+(Price a, PriceDelta d) { return Price{a.ticks + d.ticks}; }
[[nodiscard]] constexpr Price      operator-(Price a, PriceDelta d) { return Price{a.ticks - d.ticks}; }
[[nodiscard]] constexpr Notional   operator*(Price p, Qty q)        { return {p.ticks * q.v}; }       // price × qty → notional (checked variant: Ch. 90 §90.2.3)
[[nodiscard]] constexpr Qty        operator+(Qty a, Qty b)          { return Qty{a.v + b.v}; }
[[nodiscard]] constexpr Qty        operator-(Qty a, Qty b)          { return Qty{a.v - b.v}; }
Price      operator+(Price, Price) = delete;                                                            // no price + price
Price      operator+(Price, Qty)   = delete;                                                            // no price + qty (would not compile anyway: say so explicitly)
Notional   operator*(Price, Price) = delete;

template <class T> concept Money = std::same_as<T, Price> || std::same_as<T, Notional> || std::same_as<T, PriceDelta>;   // constrain generic code to the units it means
template <Money M> constexpr M scale_bp(M m, std::int64_t bp) { return M{m.ticks * bp / 10'000}; }                      // (Notional has .v: a real version uses a member trait)
static_assert(sizeof(Price) == 8 && std::is_trivially_copyable_v<Price> && std::is_standard_layout_v<Price>);           // the ABI passes it in a register (§19.5)
```

- **Explicit construction, defaulted comparison, no implicit conversion.** `explicit Price(int64)` means a raw tick count must be *named* as a price where it enters the typed world (the decoder, Ch. 60 §60.4); `operator<=>() = default` gives the full set of comparisons for free; there is no `operator int64_t`, so a `Price` cannot leak into untyped arithmetic by accident — reach `.ticks` on purpose at the codec and the journal.
- **The matrix is the design.** Write down what each pair of units may do and define exactly those operators; `= delete` the tempting wrong ones so the compiler's error names the intent. The Ch. 90 gate reads as `if (o.px * o.qty > L.max_notional)` with every operand's unit checked; a transposed argument is a compile error, not a Tuesday.
- **Ids are `enum class`.** `SymbolId`, `Seq`, a port id: no arithmetic should exist, so the cheapest strong type is an `enum class` with an explicit underlying type — comparable, hashable, switchable, convertible only by `static_cast`. `ClOrdId` is a struct because it *has* operations (slot/gen unpacking — Ch. 91 §91.2.1).
- **`[[nodiscard]]` on every operator** — a computed notional that is dropped is a bug.
- **Concepts, not SFINAE, to constrain generics** (C++20): `Money` above lets a `clamp`, a `scale_bp` or a serializer accept exactly the unit family it is written for.
- **Trivially copyable, standard layout, one member** — this is what makes the wrapper free: the Itanium ABI passes such a struct in the same register as its member and the optimizer sees through the constructor; §19.5's asm is the proof. (Members with non-trivial special functions, or two members, change the ABI class and *do* cost — a `Price` with a destructor is passed in memory.)
- **Units libraries.** For physical/time units with derived dimensions, `mp-units` (C++20/23; an Appendix H row) gives `quantity<si::nano<si::second>>` with dimensional analysis at compile time and the same zero cost; for the handful of trading units above, the hand-written matrix is smaller, faster to compile and easier to read than a dimensional framework. `std::chrono::duration` is the standard library's own strong type and is the right `Nanos` — use it, with `std::chrono::nanoseconds` and `duration_cast` at the boundaries, rather than a bespoke wrapper.

### 19.4.5 Type-state and compile-time state machines

The order lifecycle of Appendix J.3 and the session state machine beneath it (Logon → LoggedOn → Logout) are *runtime* state — a byte in the in-flight table (Ch. 91 §91.2.2), because states are data that must be journaled and replayed. The *API* through which code performs transitions can nevertheless make the illegal ones unrepresentable: **type-state** encodes the state in the type, so that `send()` exists only on a `Session<LoggedOn>` and `on_fill()` only on an `Order<Acked>`.

```cpp
// typestate.hpp — the state in the type; illegal transitions do not compile. The runtime table still stores the state as a byte (Ch. 91).
struct Disconnected {}; struct LoggedOn {}; struct LoggingOut {};
template <class State> class Session;
template <> class Session<Disconnected> { public: [[nodiscard]] Session<LoggedOn> logon(SeqNums ours) &&;   /* no send(): there is nothing to send on */ };
template <> class Session<LoggedOn>     { public: [[nodiscard]] ClOrdId send(const NewOrder&) &;  [[nodiscard]] Session<LoggingOut> logout() &&; [[nodiscard]] Session<Disconnected> dropped() &&; };
template <> class Session<LoggingOut>   { public: [[nodiscard]] Session<Disconnected> confirmed() &&; };
// Session<LoggedOn> s = Session<Disconnected>{}.logon(seqs);  s.send(o);   // ok
// Session<Disconnected> d; d.send(o);                                      // error: no member 'send' — the bug Ch. 91 §91.5 lists is now a compile error

struct Sent {}; struct Acked {}; struct Terminal {};
template <class S> struct Order;
template <> struct Order<Sent>  { ClOrdId id; [[nodiscard]] Order<Acked> on_ack() &&; [[nodiscard]] Order<Terminal> on_reject() &&; /* no on_fill(): a fill before an ack is Ack+Fill, handled explicitly */ };
template <> struct Order<Acked> { ClOrdId id; Qty leaves; [[nodiscard]] Order<Acked> on_partial(Qty) &&; [[nodiscard]] Order<Terminal> on_fill() &&; [[nodiscard]] Order<Terminal> on_cancelled() &&; };
template <> struct Order<Terminal> { ClOrdId id; /* nothing: a terminal order cannot be cancelled, replaced or filled */ };

// a compile-time transition table for the same machine, checked once, used by the runtime byte-state code:
enum class OState : std::uint8_t { Sent, Acked, PartFilled, Filled, Cancelled, Rejected, N };
enum class Event  : std::uint8_t { Ack, Partial, Fill, Cancel, Reject, N };
constexpr OState X = OState::N;                                                        // "illegal"
constexpr OState TRANSITION[int(OState::N)][int(Event::N)] = {
    /*            Ack              Partial             Fill            Cancel             Reject          */
    /*Sent     */ {OState::Acked,  OState::PartFilled, OState::Filled, OState::Cancelled, OState::Rejected},
    /*Acked    */ {X,              OState::PartFilled, OState::Filled, OState::Cancelled, X},
    /*PartFill */ {X,              OState::PartFilled, OState::Filled, OState::Cancelled, X},
    /*Filled   */ {X, X, X, X, X}, /*Cancelled*/ {X, X, X, X, X}, /*Rejected*/ {X, X, X, X, X} };
static_assert(TRANSITION[int(OState::Filled)][int(Event::Cancel)] == X, "terminal states accept nothing");
constexpr OState step(OState s, Event e) { return TRANSITION[int(s)][int(e)]; }       // one table load at runtime (Ch. 13 §13.4.2); X → the Ch. 91 stale/illegal path
```

- **Rvalue-qualified transitions** (`&&`) consume the old state object, so the stale `Session<Disconnected>` cannot be used after `logon()` — the C++ approximation of a move-only typestate (Rust's borrow checker enforces it fully — Ch. 80).
- **Type-state is the API; the byte is the data.** Ch. 91's table stores `OState` because it must be journaled, snapshotted and replayed; `on_venue` reconstructs the typed view for the transition and writes the byte back. The two agree because both derive from the one `TRANSITION` table — `static_assert` the typed transitions against it.
- **Compile-time tables** are the branch-light form of a state machine (Ch. 13 §13.4.2): one load instead of a `switch`, with every illegal cell visible at compile time. For the deterministic core (Ch. 89 §89.2.2) this is the shape `apply()` takes for its protocol states.
- **Runtime-selected states still need a `variant`.** When the state is not known at compile time (which it never is for an *incoming* message), the typed objects live inside a `std::variant<Order<Sent>, Order<Acked>, Order<Terminal>>` and `std::visit` dispatches (Ch. 14 §14.4.3) — a jump table, cheap, but the type-state's zero cost applies *within* a known state, not to the dispatch into it.
- **The honest trade-off**: one class per state multiplies instantiations (§19.6's bloat axis), error messages name `Session<LoggedOn>` rather than "session not logged on", and a state added later touches every specialization. Use type-state for the handful of protocol objects whose misuse is expensive (session, order, the Ch. 41 slot handle) — not for every enum in the program.

The synthesis of §19.4: encode the *varying design decision* in a type (CRTP for polymorphism, policies for composed behavior and static dependency injection, expression types for fused computation, strong types for units, type-state for protocol objects) so the optimizer sees a concrete program and the abstraction inlines away — then **verify it did**.

---

### 19.4.6 Concepts and constrained overload sets: compile-time dispatch four ways

§19.2 listed three ways to choose code by *type* with no runtime branch — tag dispatch, `if constexpr`, SFINAE — and C++20 adds the fourth, **concepts**, which is the one the book's "resolve the policy at build time" philosophy (§19.4.3) actually wants: a *named, checkable requirement* on a template parameter. One feed-handler example, written four ways: read a wire field, byte-swapping it if it is an integer and copying it verbatim if it is an 8-character symbol (`struct Sym { char c[8]; }`).

```cpp
// (1) tag dispatch — overload on a tag chosen by a trait            // (2) if constexpr — one body, dead branch discarded
namespace tag {                                                      namespace ifc {
  template <class T> T read_impl(const uint8_t* p, std::true_type)      template <class T> T read(const uint8_t* p) {
  { T v; std::memcpy(&v, p, sizeof v); return std::byteswap(v); }        static_assert(std::is_integral_v<T> || is_sym_v<T>);
  template <class T> T read_impl(const uint8_t* p, std::false_type)     T v; std::memcpy(&v, p, sizeof v);
  { T v; std::memcpy(&v, p, sizeof v); return v; }                       if constexpr (std::is_integral_v<T>) return std::byteswap(v);
  template <class T> T read(const uint8_t* p)                            else return v;
  { return read_impl<T>(p, std::bool_constant<std::is_integral_v<T>>{}); } }
}                                                                    }
// (3) SFINAE — candidates removed by substitution failure            // (4) concepts — constrained overloads; the more constrained wins
namespace sfinae {                                                   namespace con {
  template <class T, std::enable_if_t<std::is_integral_v<T>, int> = 0>  template <class T> concept WireInt = std::integral<T> && sizeof(T) <= 8;
  T read(const uint8_t* p) { … byteswap … }                              template <class T> concept WireSym = std::is_same_v<T, Sym>;
  template <class T, std::enable_if_t<is_sym_v<T>, int> = 0>             template <WireInt T> T read(const uint8_t* p) { … byteswap … }
  T read(const uint8_t* p) { … copy … }                                  template <WireSym T> T read(const uint8_t* p) { … copy … }
}                                                                    }
```

All four produce **identical code** — `read<uint64_t>` is one `movbe`, `read<Sym>` one `mov`, for every mechanism, both compilers (§19.5) — and all four cost the **same build time** (400 instantiations: GCC 471–484 ms, Clang 320–344 ms; the spread is noise). They differ in what they do when *misused*, which is the whole point of choosing among them:

- **Tag dispatch** (the C++98 idiom) selects an overload by a trait but **rejects nothing**: `tag::read<double>(p)` compiles — `is_integral_v<double>` is false, so the *symbol* overload runs and silently skips the byte-swap. Zero diagnostic lines, wrong code. Use it only when every type has a valid meaning in every branch.
- **`if constexpr`** (Ch. 18) is the right tool for *one algorithm with type-dependent steps* — one body, one instantiation, no overload set to reason about. Misuse is rejected by whatever `static_assert` you wrote (7–8 lines of diagnostic here) and nothing else; without the `static_assert`, `read<double>` compiles and returns an unswapped `double`.
- **SFINAE** (`std::enable_if`) removes candidates from the overload set, so `read<double>` is "no matching function" with each candidate's failed condition listed (10–19 lines). It works, it is what every pre-C++20 library does, and its two costs are the ones Ch. 19 §19.6 complains about: the requirement is spelled as a type-trait expression in a defaulted template parameter (unreadable, un-nameable, easy to get subtly non-exclusive), and the compiler cannot *rank* two candidates whose conditions overlap — it reports ambiguity.
- **Concepts** state the requirement by name and let overloads be **ranked by subsumption**: if `concept Wide = WireInt<T> && sizeof(T) == 8`, then `template <Wide T> rank(T)` is *more constrained* than `template <WireInt T> rank(T)`, and `rank(uint64_t{})` picks it with no ambiguity (`mov eax, 2` — §19.5). Subsumption works only through *named* concepts: two `requires`-clauses that spell the same condition out twice are *different* atomic constraints and ambiguous. The diagnostic for `con::read<double>` is the longest (25–29 lines) and the only one that says *why*: `note: because 'double' does not satisfy 'WireInt'`. The standard library's concepts — `std::integral`, `std::invocable<F, Args…>`, `std::ranges::contiguous_range` (the one a zero-copy decoder wants for its buffer argument), `std::same_as`, `std::convertible_to` — compose into your own.

Concepts also give §19.4.3's policies the static interfaces they were missing. The `OrderBook<Storage, Locking, Alloc>` template accepted *any* three types and reported a missing method as a 200-line instantiation trace from inside the host; with concept-checked parameters it reports it at the template argument:

```cpp
template <class S> concept BookStorage = requires(S s, std::int64_t px, std::int32_t q) {
    { s.add(px, q) } -> std::same_as<void>;
    { s.best_bid() } -> std::convertible_to<std::int64_t>;
    { S::capacity } -> std::convertible_to<std::size_t>;          // a static member is a requirement too
};
template <class L> concept LockingPolicy = requires(L l) { l.lock(); l.unlock(); } && std::is_nothrow_default_constructible_v<L>;
template <class St> concept Strategy = requires(St s, std::int64_t px, std::int32_t qty) { { s.on_tick(px, qty) } noexcept -> std::same_as<void>; };

template <BookStorage Storage, LockingPolicy Locking, class Alloc> class OrderBook : Storage, Locking { /* unchanged */ };
```

A concept checks **syntax, not semantics** — `LockingPolicy` is satisfied by a type whose `lock()` does nothing, which is exactly what `NoLock` relies on; the semantic contract still lives in documentation and tests. And a concept is free: it is evaluated at instantiation, produces no code, and §19.5's asm is byte-identical with or without the constraint. Ch. 20 uses exactly this machinery — `fixed_string` NTTPs plus `if constexpr` over a `consteval` table — to generate a whole decoder.

## 19.5 Verify the codegen: zero-overhead confirmation

"Zero-cost" is a codegen claim; §19.5 is how you *settle* it. The test: does the abstracted version produce **the same instructions** as the hand-written one? Snippets are **verified** Clang 18.1 output (Appendix L.3; `--target=x86_64-linux-gnu -O2 -march=x86-64-v3`).

**Hand-written vs CRTP — identical.** Both the direct `hand_update` and the CRTP `Vwap::update` compile to the same body — the dispatch and the wrapper vanished:

```asm
; hand_update(long, Tick const&)       — and Strat<Vwap>::update is the SAME three instructions,
;                                        shifted one register because the member takes a hidden
;                                        `this` (s in rsi, t in rdx); inlined into the bench loop
;                                        the two bodies are byte-for-byte identical.
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

**Expression-template fusion.** For `d = a + b + c`, confirm a *single* loop with no temporary buffers — `d[i] = a[i]+b[i]+c[i]` computed in one pass, ideally vectorized (Ch. 32):

```asm
.loop:                                       ; one fused pass, no intermediate vectors
        vmovdqu ymm0, [rsi + rax]            ; a[i..]
        vpaddd  ymm0, ymm0, [rdx + rax]      ; + b[i..]
        vpaddd  ymm0, ymm0, [rcx + rax]      ; + c[i..]   (single loop, fused, SIMD)
        vmovdqu [rdi + rax], ymm0            ; → d[i..]
        ...
```

If instead you see *multiple* loops or temporary allocations, the expression templates didn't fuse (or you forced materialization).

**Strong types pass in bare registers — identical to `int64_t`.** The §19.4.4 wrappers against their raw equivalents (`clang++ -O2 -std=c++20 -S`, GCC 13.3 produces the same three bodies):

```asm
; within_collar(Price px, Price lo, Price hi)  — and within_collar_raw(long, long, long): the SAME six instructions
        cmp     rdi, rsi            ; px >= lo        (each Price is ITS member, in the register the ABI gives an int64)
        setge   cl
        cmp     rdi, rdx            ; px <= hi
        setle   al
        and     al, cl
        ret
; notional(Price, Qty)  — and notional_raw(long, long):
        mov     rax, rdi
        imul    rax, rsi            ; price × qty: one imul; the Notional{} braces produced nothing
        ret
; bump(Price, PriceDelta) — and bump_raw(long, long):
        lea     rax, [rdi + rsi]    ; the explicit constructor, the operator and the return type all vanished
        ret
```

No wrapper, no load/store through a stack slot, no extra `mov`: a trivially-copyable single-member struct *is* its member to the ABI, and the `explicit` constructors and `[[nodiscard]]` operators are compile-time facts only. The `= delete`d operators leave no trace because the code that would have used them does not exist. The type-state transitions of §19.4.5 compile the same way (an `Order<Sent>::on_ack() &&` is a `mov` of the id) — the state is in the *type*, and types are free. The verification habits for this chapter:

- **For any "zero-cost" abstraction on the hot path, diff its asm against the hand-written equivalent.** Same instructions = the claim holds. A stray `call`, extra moves, or a missing inline = it leaked.
- **Confirm the dispatch inlined** (no `call [rax]`, the §19.3 winner pattern), the wrapper types produced no extra loads/stores, and empty policies added no size (`sizeof` unchanged — Ch. 9).
- **Treat a codegen regression as a bug.** When an abstraction *stops* matching hand-written code after a change (a missed inline, an accidental copy), the asm diff catches it — make it part of the perf-regression discipline (Ch. 101).

**Four dispatch mechanisms, one instruction.** The §19.4.6 `read<uint64_t>` under tag dispatch, `if constexpr`, SFINAE and concepts (`clang++ -O2 -std=c++23 -march=x86-64-v3`; GCC 13.3 identical bar `endbr64`):

```asm
; tag::read<uint64_t>   ifc::read<uint64_t>   sfinae::read<uint64_t>   con::read<uint64_t>   — ALL FOUR:
        movbe   rax, qword ptr [rdi + 30]   ; load + byte-swap, offset folded
        ret
; tag::read<Sym>  and  con::read<Sym>:                 ; con::rank(uint64_t) — subsumption picked the Wide overload:
        mov     rax, qword ptr [rdi + 22]              ;         mov     eax, 2
        ret                                            ;         ret
```

The choice of mechanism is a *diagnostics and design* decision; the optimizer sees the same function four times. Concept checks, `enable_if` and tag parameters leave no instructions — the empty tag argument is passed in no register and the constraint is evaluated before codegen begins.

### Build-time/size caveat

Zero runtime cost is *not* zero cost (Ch. 18): each instantiation is build time + binary size. For the four dispatch mechanisms of §19.4.6 that cost is the same — 400 instantiations compiled in 471/471/478/484 ms (tag/`if constexpr`/SFINAE/concepts, GCC 13.3) and 322/320/344/326 ms (Clang 18.1) — so concepts are not a build-time tax; what differs is the *length and quality* of the error when a constraint fails (0 lines and wrong code for tag dispatch, 7–8 for `if constexpr`+`static_assert`, 10–19 for SFINAE, 25–29 with a named reason for concepts). Verify the *other* axis too — `size`/`bloaty` on the binary, `-ftime-trace` on the build — so a runtime win doesn't hide an I-cache (Ch. 12) or build-time regression (§19.6).

---

## 19.6 Pitfalls & anti-patterns: template bloat, error-message cost

- **Template-instantiation bloat (the runtime cost of a compile-time technique).** Instantiating a *large* function for many types — or a policy class across a big `axes × options` matrix — emits a separate body each time, bloating the binary and the I-cache (Ch. 12). Factor type-*independent* code into a non-template base/helper so only the genuinely-varying part is instantiated. Measure binary size (Ch. 12, 18).
- **Assuming zero-cost without verifying.** The cardinal sin: claiming an abstraction is free while it quietly emits a copy, a missed inline, or an indirect call. **Always diff the asm** (§19.5); "it's a template so it must be free" is false (a template can still produce a `virtual` call, an allocation, a temporary).
- **Brutal error messages / build-time cost.** Heavy metaprogramming (deep SFINAE, expression templates, large packs) produces pages of incomprehensible diagnostics and slow builds (Ch. 18) — a real productivity and iterate-loop (Ch. 3) tax. Use **concepts/`requires`** (C++20) to constrain templates and get *readable* errors; prefer the simplest mechanism that works; don't metaprogram for cleverness.
- **CRTP misused for runtime polymorphism.** CRTP needs the type at compile time; forcing it where selection is genuinely runtime (heterogeneous storage, config-chosen plugins) leads to contortions or doesn't work. Use `std::variant` (closed set) or a devirtualized `virtual` (Ch. 14) instead.
- **Expression-template dangling (`auto` lifetime).** `auto e = a + b;` may capture *references* to `a`/`b` in the expression type; if they go out of scope before `e` is evaluated, it dangles. Evaluate expressions immediately (assign to a concrete `Vec`), and prefer a vetted library (Eigen/Blaze) that handles lifetimes.
- **Over-abstraction.** A tower of policies/CRTP layers for variation you don't actually have (YAGNI) costs build time, readability, and error-message sanity for no runtime benefit. Reach for these when there's a *real* need for compile-time variation on the hot path, not preemptively.
- **Combinatorial instantiation explosion.** `Host<A, B, C>` with several options per axis can instantiate dozens of combinations, each a code copy — build-time and size blowup (Ch. 18). Cap the matrix to combinations you actually ship.
- **Forgetting the abstraction must still be *correct*.** Static dispatch removes the runtime check but not the need for it; `static_cast`-based CRTP and policy composition can hide lifetime/aliasing bugs (Ch. 23) the compiler won't catch. Zero-cost ≠ zero-risk.
- **Subsumption and ambiguity traps (§19.4.6).** Two overloads constrained by *equivalent but separately spelled* `requires`-clauses are ambiguous — subsumption only relates *named* concepts (`Wide` subsumes `WireInt` because `Wide` is defined as `WireInt<T> && …`); a concept that checks the *wrong thing* (syntax present, semantics absent) silently admits a policy that compiles and misbehaves; and tag dispatch that forgets to reject (`read<double>` taking the symbol path) produces no error at all. Constrain with named concepts, keep the semantic contract in tests, and reserve tag dispatch for sets where every branch is valid for every type.

---

## 19.7 Exercises & checklist

**Exercises**

1. **Prove zero-cost.** Build `zerocost.cpp`; run `hand` vs `crtp` vs `virt` vs `func` pinned/turbo-off. Confirm CRTP matches hand-written ns/call. Then disassemble (Ch. 4) and confirm the CRTP and hand-written bodies are **identical**, and `virt` shows `call [rax]` (§19.5).
2. **Break the inline.** Make the CRTP `go()` large (or mark it `[[gnu::noinline]]`). Does CRTP still match hand-written? Explain why "zero-cost" depends on inlining (Ch. 12 §12.4.1, Ch. 24 §24.2) and the ILP it exposes (Ch. 11), and what the asm now shows.
3. **Policy-based order book (sketch).** Write a tiny `Book<StoragePolicy>` with two policies (flat array vs `std::map`). Confirm (a) `sizeof` is unaffected by an *empty* policy (Ch. 9), (b) each policy inlines, and (c) the binary grows per instantiated combination (`size`).
4. **Bloat measurement.** Instantiate a non-trivial templated function for 1, 4, 16 types; measure binary size (`size`/`bloaty`) and build time (`-ftime-trace`). Where does bloat become a concern (Ch. 12), and how does factoring out type-independent code help (§19.6)?
5. **Expression templates vs naïve.** Using Eigen (or a tiny hand-rolled version), compute `d = a + b + c` over large vectors naïvely vs with expression templates. Compare passes/allocations and ns/element; confirm fusion in the asm (§19.5). When does it *not* pay?

**Checklist — zero-cost abstractions**

- [ ] Template parameters on the hot path are **constrained by named concepts** (§19.4.6) — a bad policy type fails at the argument, not 200 lines inside the host — and the constrained and unconstrained versions produce **the same asm** (§19.5).

- [ ] Hot-path polymorphism known at compile time uses **CRTP / static dispatch**, not `virtual` (Ch. 14).
- [ ] I **verified zero-cost in the asm** (§19.5) — the abstraction's codegen **matches the hand-written** equivalent (inlined, no `call [rax]`, no extra copies).
- [ ] Behavioral variation is composed via **policy parameters** (inlined, empty-base-free — Ch. 9), with runtime selection (if any) resolved **once at setup**, not per call.
- [ ] Templates instantiate **only what must vary**; type-independent code is factored out to bound **binary size / I-cache** (Ch. 12) — I measured it.
- [ ] Heavy metaprograms are constrained with **concepts/`requires`** for readable errors, and I didn't reach for expression templates without a measured temporary/allocation problem.
- [ ] Expression results are **evaluated immediately** (no `auto` dangling), preferably via a **vetted library** (Eigen/Blaze).
- [ ] I checked the **build-time axis** (`-ftime-trace`) and **combinatorial explosion** of policy matrices (Ch. 18) — runtime wins didn't hide a build/size regression.
- [ ] I didn't **over-abstract** for variation that doesn't exist (YAGNI); zero-cost still requires correctness (lifetimes/aliasing — Ch. 23).

---

## 19.8 References

- D. Vandevoorde, N. Josuttis, D. Gregor, *C++ Templates: The Complete Guide* — the definitive reference for CRTP, policy-based design, expression templates, and the instantiation cost model underlying this chapter.
- A. Alexandrescu, *Modern C++ Design* — the original, thorough treatment of **policy-based design** and type-based dispatch (§19.2, §19.4.3).
- T. Veldhuizen, *"Expression Templates"* (the founding C++ Report article) and the Eigen/Blaze documentation — the technique and its modern, production form (§19.4.2).
- ISO C++ / cppreference — concepts and `requires` (C++20), `[[no_unique_address]]`, and the empty-base optimization that make policies free (§19.4.3, ties Ch. 9).
- The C++ Core Guidelines (templates and generic programming) and Sutter/Alexandrescu, *C++ Coding Standards* — when (and when not) to reach for these abstractions (§19.6).

## 19.9 Additional Reading

- CppCon talks on "zero-cost abstractions," CRTP, and policy-based design — including before/after asm walkthroughs that mirror §19.5.
- Ch. 14 (*Indirect Calls & Virtual Dispatch*) — the runtime-polymorphism cost CRTP avoids; Ch. 18 (*Compile-Time Mechanics*) — the `if constexpr`/template foundation; Ch. 21 (*The Cost of Abstractions*) — abstractions that *aren't* free; Ch. 28 (*Cache-Friendly Containers*) and Ch. 27 (*Custom Allocators*) — policy-based containers/allocators in practice; Ch. 32 (*SIMD*) — expression-template vectorization.
- **Ch. 80 §80.4.5** — static dispatch and zero-cost abstraction in Rust (monomorphized traits/generics vs `dyn Trait`'s fat-pointer vtable), measured beside this chapter's C++; **Appendix B** (B.3) — the cross-language map.

---

*Next: Ch. 20 — Compile-Time Protocol Decoders: From Schema to Code, where the techniques of this chapter and Ch. 18 build something a feed handler ships: a venue's field list written once as a schema, parsed in `consteval`, turned into overlay accessors by `if constexpr`, `static_assert`ed against the venue document — and proven in the asm to be the same `movbe` as the hand-written decoder of Ch. 60.*
