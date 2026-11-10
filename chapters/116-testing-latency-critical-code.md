# Part XIII — Observability & Operations in Production

# Chapter 116 — Testing Latency-Critical Code: Unit, Property-Based & Deterministic Simulation Testing

> **Prerequisites:** Ch. 103 §103.2.2 (the `apply(state, input)` core — the thing under test), Ch. 111 (the sequenced stream and the digest — the oracle of determinism), Ch. 105–106 (the order-entry and session state machines the simulation drives), Ch. 109–110 (captures: where golden-master cases come from), Ch. 53 (sanitizers, stress, fuzzing and model checking — the *dynamic* and *concurrency* tooling this chapter complements with logic tests), Ch. 22 §22.4.3 (policy injection — how a test double costs nothing), Ch. 16 (what a virtual seam costs), Ch. 4 (reading the asm to prove it), Ch. 32 §32.4.5 (the order-book case study the examples use).
>
> **Leads into:** Ch. 117 (the exchange simulator — the deterministic venue this chapter's DST harness grows into, with a real matching core, an empirical latency model and seeded fault schedules), Ch. 118 (benchmarks as tests, the statistical gate and the nightly replay gate — the *performance* half of the same CI), Ch. 119 (shadow runs and canaries — testing in production, after this chapter's testing before it), Ch. 120 §120.4.1 (the golden-capture correctness gate the capstone runs). Ch. 53 made the lock-free code trustworthy; this chapter makes the *logic* trustworthy — the book's first test code: table-driven and property-based tests of the deterministic core, a deterministic simulation that drives the gateway through thousands of seeded fault schedules in a tenth of a second, golden masters cut from captures, and test doubles that compile to the same instructions as production.

---

## 116.1 Why it matters: the bug that loses money is a logic bug in a correct-looking program

Part VI's tooling finds data races, Ch. 101's fuzzers find parsers that crash on hostile bytes, and the determinism gate (Ch. 103 §103.4.6, Ch. 120 §120.4.1) finds a core that disagrees with itself. None of them finds the order book that reports the wrong best bid after a partial execution at the top level, the gateway that resends an order the venue already has, or the conflation slot that drops the *last* update instead of the stale one. Those are **logic bugs in deterministic code**, the easiest class to test and — in this book until now — the least tested: the chapters built a pure `apply(state, input)` core precisely so it could be tested, and then tested it only by replaying a golden capture and comparing bytes. A golden capture catches a *change*; it does not say whether the original was right, it never exercises the paths the capture did not take (a venue disconnect mid-fill, a duplicate ack, a reject after a replace), and it is useless for new code.

Three techniques close that gap, and the hot path pays nothing for any of them. **Table-driven tests** encode the venue specification's examples as (input script → expected book) rows. **Property-based tests** generate thousands of random input sequences and check *invariants* (quantity conservation, level non-negativity, determinism) rather than expected values, then **shrink** a failing sequence to the two inputs that matter (§116.3: 200 → 2). **Deterministic simulation testing** (DST) runs the gateway's real state machine against a simulated venue and clock under a seeded fault schedule — drops, duplicates, rejects, disconnects — and asserts the Ch. 105 invariants across 5 000 seeds in 120 ms; with a one-line bug (resend Unknown orders on reconnect) it fails 4 236 of them and names seed 1. And because the shell is injected as a **template policy**, the production gateway and the one under simulation are the same code: §116.3 shows the policy-injected `send_order` compiling to the same eleven instructions as a hand-written one, while the equivalent `virtual` seam adds two indirect calls and a speculative-devirtualization compare.

## 116.2 Mental model

### 116.2.1 The test pyramid for a deterministic core

```
   layer                              what it checks                                        inputs come from                          cost / run
   ───────────────────────────────    ──────────────────────────────────────────────────    ─────────────────────────────────────    ────────────────────
   table-driven unit tests            the spec's examples; one message → one expected state  the venue spec, incident post-mortems    µs; every build
   property-based tests               invariants over random input sequences; shrinking      a seeded generator                        ms; every build
   deterministic simulation (DST)     the impure shell's protocol logic under faults          seeded fault schedules × many seeds       100 ms–s; every build
   golden-master tests                "still produces what it produced" on real data          captures (Ch. 109–110), kept small          s; every build (Ch. 120 §120.4.1)
   sanitizers / stress / model check  memory and ordering (Ch. 53, 101)                        the above, run under the tools            min; nightly
   replay of the full session         determinism and latency on the tuned box               the full capture                          min–h; nightly (Ch. 118)
```

The first three are *new* in this book and all three need the same thing: a core with **no hidden inputs** (Ch. 103 §103.2.2 — no clock, no socket, no global) so that `apply` can be called from a test with exactly the inputs the test chose. If a function reads `rdtsc()` or a socket inside, it cannot be table-tested, properties will flake, and the simulation cannot control it. The book's determinism rule is therefore also its testability rule, and the digest (Ch. 111 §111.2.2) is the cheapest test of all: two folds of the same sequence must agree.

### 116.2.2 Properties, not expected values

A unit test says "after these six messages the best bid is 11 for 30"; a property says "after *any* sequence, Σ level quantities equals Σ live order quantities, per side". The property is weaker per case and vastly stronger per minute of CPU, because the generator explores sequences nobody wrote down. The craft is in choosing invariants that are *true of the thing* and *cheap to check*: for an order book, conservation of quantity, non-negative levels, an order that is live is on exactly the level it was added at, cancel-after-cancel is rejected and changes nothing (idempotency), and determinism (same inputs → same digest). Note what is **not** an invariant of a feed-driven book: that the bid is below the ask — a replayed feed can cross briefly and the book must represent it; a property test that asserts it will fail on real captures and teach the wrong lesson. When a property fails, the **shrinker** (delta debugging — Zeller 2002) deletes chunks of the sequence while the failure persists and hands back a minimal counterexample; §116.3 reduces a 200-input failure to `Add; Exec` with an execution larger than the resting quantity.

### 116.2.3 Deterministic simulation testing

The impure shell — the gateway's session machine, the timers, the recovery logic — cannot be table-tested because its behaviour depends on *when* things happen. DST (FoundationDB's method; Antithesis; TigerBeetle's VOPR) makes time an input: a **simulated clock** advanced by the test, a **simulated venue** whose every effect is an event scheduled on that clock, and a **seed** that decides which faults fire — a dropped order, a duplicated ack, a reject, a disconnect at the worst moment. The real gateway code runs single-threaded against this world, thousands of seeds per second, and the test asserts the invariants of Ch. 105 §105.2.3: an order marked Unknown after a disconnect is never re-sent, every acknowledged order has exactly one position effect, every order reaches a terminal or an explicitly-unknown state. A failing seed is a **reproducible** failure — re-run with the same seed under a debugger (Ch. 5) and step through the exact fault schedule — which is the property that distinguishes DST from a flaky integration test against a real sandbox venue. It needs the shell to take its clock, its venue and its randomness as parameters, which is §116.2.4.

### 116.2.4 Test doubles that cost nothing

A test double for the clock or the wire can be injected two ways: a **virtual interface** (`IClock* c; c->now()`) chosen at runtime, or a **template policy** (`template <class Clock> struct Gateway { Clock::now() }`) chosen at compile time (Ch. 22 §22.4.3). On the hot path the difference is Ch. 16's: the virtual seam is an indirect call per use (plus a possible speculative-devirtualization compare and branch), the policy is inlined and disappears — §116.3's asm shows `GatewayP<RealClock, RealWire>::send_order` as `rdtsc` + stores, identical to code with no seam at all. The rule: **production and test builds instantiate the same template with different policies**; the virtual version exists only where the dispatch is already dynamic (Ch. 16 §16.4.5). A third option, link-time substitution (a test `clock.cpp` replacing the production one), costs nothing at runtime either but prevents the two from coexisting in one binary — fine for a unit-test executable, useless for DST where the simulated and real worlds must both exist.

## 116.3 Measure it: a test suite for the core, a bug found by seed, and the price of a seam

*Toolchain and machine: Appendix L.3. Built with GCC 13.3 (`-O2 -std=c++20 -pthread`), run on the 4-vCPU Intel Xeon VM of Ch. 49 §49.3 (TSC 2.70 GHz, Linux 6.18). The harness is ~40 lines of `CHECK` macro (swap in GoogleTest/Catch2/doctest and RapidCheck at will — the method does not depend on the framework). Labelled VM-measured.*

**The harness** (`tests.cpp`): a dense tick-indexed `Book` with `apply(Input) → {ok, rejected}` and a digest (Ch. 32's case study reduced to 20 lines); a table-driven test of six messages with expected BBO per row; a property test over 2 000 random 200-input sequences with conservation/underflow/determinism invariants and a delta-debugging shrinker, run once against the correct book and once with a switch that disables the execution-quantity check; a DST of a Ch. 105-style gateway (Sent/Acked/Rejected/Unknown, cancel-if-no-ack ageing, disconnect → Unknown, reconnect) against a simulated venue with 5 % drop, 5 % duplicate ack, 5 % reject, 2 % disconnect per order and 5–50 time-unit ack delays, over 5 000 seeds, with and without the bug "resend Unknown orders on reconnect"; and a policy-injected vs virtual-seam gateway timed and disassembled.

```
[table-driven]        6 rows, BBO and top-level quantity after each message; the duplicate cancel is rejected and changes nothing
[property-based]      2 000 cases × 200 inputs: 0 failures; determinism (two folds, equal digests) held in all 2 000
[property-based, execution-quantity check disabled]
                      2 000 failures — first counterexample shrunk from 200 inputs to 2:  {Add id=36 side=0 px=63 qty=411}  {Exec id=36 side=1 px=62 qty=452}   (ask level underflow)
[DST, correct gateway]            5 000 seeds in 117 ms (42 700 seeds/s); violations: 0;   mean orders left in Unknown state per run: 6.7
[DST, resend-on-reconnect bug]    5 000 seeds in 126 ms;  violations: 4 236 — first at seed 1: "resent an Unknown order the venue may already hold";  Unknown at end: 2.3
[test doubles]        policy-injected clock + wire: 13.0 ns/send    virtual clock + wire: 13.5 ns/send    (both dominated by the rdtsc inside)

GatewayP<RealClock, RealWire>::send_order():           GatewayV::send_order():                       (virtual seams; GCC 13.3 -O2)
   rdtsc                                                  mov    (%rdi),%rdi          ; load IClock*
   shl $0x20,%rdx ; or %rdx,%rax                          mov    (%rdi),%rax ; mov (%rax),%rax ; cmp %rdx,%rax ; jne …   ; speculative devirtualization: compare the vptr slot, fall back to an indirect call
   mov (%rdi),%edx ; mov %rax,0x8(%rdi)                   rdtsc …                                     ; the devirtualized fast path
   lea 0x1(%rdx),%ecx ; xor %rax,%rdx ; mov %ecx,(%rdi)   mov 0x8(%rbx),%rdi ; mov (%rdi),%rdx ; mov (%rdx),%rcx ; cmp %rdx,%rcx ; … second compare for IWire::send
   add %rdx,RealWire::sink(%rip) ; ret
```

Reading it:

- **The property test found the bug the table test did not, and told us where.** Six hand-written rows never execute more than rests; the generator did so on its first sequence, and the shrinker cut 200 inputs to the two that matter — an `Add` of 411 and an `Exec` of 452 on the same id — in under a second. The counterexample *is* the bug report, and it becomes a new table row.
- **DST is cheap enough to run on every build**: 5 000 fault schedules in 0.12 s, each a complete gateway lifetime with disconnects and reconnects. The buggy gateway failed 85 % of seeds because the fault rates were chosen to make disconnects common; in production that path runs once a month, which is exactly why it must be simulated. Seed 1 reproduces the failure deterministically under a debugger.
- **The suite is also the determinism gate**: every property case folded the sequence twice and compared digests; a `rdtsc()` or an uninitialised read inside `apply` would have failed it on the first case (Ch. 111 §111.3 showed the same with a clock read).
- **The policy double is free; the virtual one is nearly free here and not free in general.** Both `send_order`s are dominated by the `rdtsc` (~13 ns), so the two indirect calls show as 0.5 ns; but the asm tells the real story: the policy version is the production code with no trace of a seam, the virtual version carries two vtable loads, two compares and two fallback branches that GCC's speculative devirtualization (Ch. 16 §16.4.2) inserted *because* only one implementation was visible — in a real binary with two implementations (the production one and the mock) the compare fails or disappears and the indirect call is paid, with its BTB entry (Ch. 15). Inject by policy.

## 116.4 Techniques

### 116.4.1 Table-driven tests of `apply(state, input)`

```cpp
// book_table_test.cpp — the venue spec's examples as rows. One row = one input and the state a reader of the spec would expect after it.
struct Row { Input in; int exp_bid, exp_ask; std::uint32_t exp_bid_qty; };
const Row rows[] = {
    {{Input::Add,    1, 0, 10, 100}, 10, 64, 100},   // first bid
    {{Input::Add,    2, 1, 12,  50}, 10, 12, 100},   // first ask
    {{Input::Add,    3, 0, 11,  30}, 11, 12,  30},   // better bid
    {{Input::Exec,   3, 0,  0,  30}, 10, 12, 100},   // fully executed: level empties, BBO falls back
    {{Input::Cancel, 1, 0,  0,   0}, -1, 12,   0},   // last bid cancelled
    {{Input::Cancel, 1, 0,  0,   0}, -1, 12,   0},   // DUPLICATE cancel: rejected, nothing changes (idempotency — Ch. 105)
};
Book b; for (const Row& r : rows) { b.apply(r.in); CHECK(b.best_bid() == r.exp_bid); CHECK(b.best_ask() == r.exp_ask); /* … */ }
```

- **One row per spec example and per post-mortem** (Ch. 108): every production incident whose cause was a logic error ends as a row that would have failed before the fix.
- **Check the state, not the output log**: assert on `best_bid()`, level quantities and the digest; outputs (Ch. 103's `apply` returns them) are checked by the golden masters (§116.4.4).
- **Keep rows data**: a table is a `constexpr` array or a CSV the test loads — a product owner can read it, and the same rows drive the Python research decoder's parity test (Ch. 109 §109.4.6).

### 116.4.2 Property-based tests, invariants and shrinking

```cpp
// book_props.cpp — generator, invariants, shrinker. RapidCheck does all three with rc::check(); this is the same thing in 25 lines to show the shape.
struct Gen { std::mt19937_64 g; Input input(std::uint32_t max_id) { /* uniform over kind, id, side, px, qty */ } };
static bool invariants(const Book& b, const char** why) {                       // conservation per side; no level underflow; (NOT "bid < ask": feeds cross)
    std::uint64_t lb = 0, la = 0, ob = 0, oa = 0;
    for (auto q : b.bid) { if (q > 0x7fffffff) { *why = "bid level underflow"; return false; } lb += q; }
    for (auto q : b.ask) { if (q > 0x7fffffff) { *why = "ask level underflow"; return false; } la += q; }
    for (const auto& o : b.orders) if (o.live) (o.side ? oa : ob) += o.qty;
    if (lb != ob) { *why = "bid conservation"; return false; } if (la != oa) { *why = "ask conservation"; return false; } return true;
}
static std::vector<Input> shrink(std::vector<Input> seq, auto fails) {          // delta debugging: drop halves, then quarters, … then single inputs, while it still fails
    for (std::size_t chunk = seq.size() / 2; chunk >= 1; chunk /= 2) { bool progress = true; while (progress) { progress = false;
        for (std::size_t i = 0; i + chunk <= seq.size(); i += chunk) { auto cand = seq; cand.erase(cand.begin() + i, cand.begin() + i + chunk); if (fails(cand)) { seq = cand; progress = true; break; } } } if (chunk == 1) break; }
    return seq;
}
// per case: generate 200 inputs; fold once checking invariants after every apply; fold twice and compare digests (determinism); on failure, shrink and print
```

- **Seeded and reproducible**: the case number is the seed; a failure prints `seed` and the shrunk sequence, and `--seed N` replays it. Never use `std::random_device` in the generator.
- **Generate *valid-looking* inputs mostly**, with a bias toward edge values (qty 0, px at the boundaries, ids that collide): a uniform generator spends its budget on sequences that are rejected at the first check.
- **Determinism is a property**: fold twice, compare digests; fold under ASLR and with a different compiler in the nightly (Ch. 103 §103.4.6's lint).
- **Property tests for the impure pieces too**: the FIX encoder (Ch. 106 §106.4.2) round-trips through the parser; the `ClOrdId` packing (Ch. 105 §105.2.1) round-trips slot and generation; the conflation slot (Ch. 50) never hands out an update older than the one before.

### 116.4.3 Deterministic simulation of the gateway against a fault-injecting venue

```cpp
// dst.cpp — the real gateway machine against a simulated venue on a simulated clock; one seed = one fault schedule.
struct SimVenue {   // every effect is an event on the sim clock; the seed decides the faults
    std::mt19937_64 g; double p_drop = .05, p_dup = .05, p_reject = .05, p_disconnect = .02; std::uint64_t ack_min = 5, ack_max = 50;
    std::deque<Ev> q; std::vector<std::uint32_t> received;                      // what the venue actually took: the oracle
    void on_order(std::uint64_t now, std::uint32_t id) { if (u() < p_drop) return;                     // lost on the wire: no ack ever (the Unknown case)
        bool dup = u() < p_dup; for (int k = 0; k < (dup ? 2 : 1); ++k) { received.push_back(id); q.push_back({now + delay(), u() < p_reject ? Reject : Ack, id}); }
        if (u() < p_disconnect) q.push_back({now + delay() / 2, Disconnect, 0}); std::sort(q.begin(), q.end(), by_time); }
};
for (std::uint64_t seed = 0; seed < N; ++seed) {
    SimVenue v{seed}; Gateway<SimClock, SimWire> gw;                               // the production template with simulation policies (§116.4.5)
    for (int step = 0; step < 400; ++step) { SimClock::t += 10; v.deliver_due(gw); if (gw.reconnect_due()) for (auto id : gw.on_reconnect()) v.on_order(now, id); if (gw.connected && step % 3 == 0) v.on_order(now, gw.send()); gw.age(now, 200); }
    CHECK_SEED(seed, gw.resent_unknown == 0, "resent an Unknown order the venue may already hold (Ch. 105 §105.2.3)");
    CHECK_SEED(seed, every_received_id_is_acked_or_unknown(v, gw), "venue holds an order the gateway thinks is Free");
}
```

- **Fault rates are dials, not realism**: run one configuration at production-like rates (0.01 % drops) for coverage of the normal path and one at 5–10 % so every seed exercises recovery; both every build.
- **The oracle is the venue double's own record** (`received`): the test knows ground truth the production gateway never does, which is what makes "the gateway's view is consistent with the venue's" checkable.
- **Time is the test's**: timeouts (cancel-if-no-ack at 200), heartbeats (Ch. 106 §106.4.4), leases (Ch. 112 §112.4.1) all read `SimClock::now()`, so a schedule can place a disconnect *exactly* between a send and its ack, or a lease expiry *exactly* between the check and the send — the races a real sandbox venue produces once a year.
- **Extend the double, not the test**: a `SimVenue` policy that models sequence numbers and `ResendRequest` (Ch. 106 §106.2.1) tests the session machine; one that models queue position and partial fills (Ch. 120 §120.4.3) tests the strategy's order handling. FoundationDB's lesson is that the simulator is a product.

### 116.4.4 Golden-master tests cut from captures

A golden master is a replay with a frozen expected output; Ch. 120 §120.4.1 runs the full-session version as the correctness gate. For the per-build suite, **cut small masters from captures** (Ch. 109–110): the first 10 000 messages of the open, the 500 around a known gap, the 200 that reproduced an incident — each with the digest and the decoded book after the last message as the expected value. Three rules: the master is *data* (a capture slice plus a 64-bit digest, committed), a failure prints the **first diverging sequence number** (Ch. 103 §103.4.6's per-event digest column), and a deliberate behaviour change updates the master in the same commit with the reason in the message — a master that "just gets regenerated" tests nothing. The Python research module (Ch. 109 §109.4.6) runs the same masters as its parity test.

### 116.4.5 Test doubles as policies: production and simulation from one template

```cpp
// gateway.hpp — the shell's impure dependencies are template parameters; production and simulation instantiate the same code (Ch. 22 §22.4.3)
struct RealClock { static std::uint64_t now() { return __rdtsc(); } };
struct SimClock  { static inline std::uint64_t t = 0; static std::uint64_t now() { return t; } };          // the test advances t
struct RealWire  { static void send(const Frame& f) { nic_tx_ring.push(f); } };                             // Ch. 82
struct RecWire   { static inline std::vector<Frame> log; static void send(const Frame& f) { log.push_back(f); } };
template <class Clock, class Wire> struct Gateway { void send_order(const Order& o) { auto ts = Clock::now(); /* encode (Ch. 106), journal (Ch. 105) */ Wire::send(frame); } };
using ProdGateway = Gateway<RealClock, RealWire>; using SimGateway = Gateway<SimClock, RecWire>;
static_assert(sizeof(ProdGateway) == sizeof(SimGateway));                                                    // no hidden state in the double
```

- **Prove the zero cost once per release** (Ch. 4): `objdump` the production instantiation's hot functions and diff them against a build with the policies hard-coded; §116.3's listing is the expected result — no vptr load, no compare, no indirect call.
- **Where dispatch is genuinely dynamic** (a venue chosen at runtime — Ch. 16 §16.4.5), keep the `virtual` seam and test *through* it; the cost is already being paid.
- **Concepts name the policy contract** (Ch. 22 §22.4.6): `template <Clock C, Wire W>` turns a missing `now()` into a one-line diagnostic instead of a template error inside the gateway.

### 116.4.6 Running the suite: sanitizers, ARM, and what belongs where

Every build: table, property (a fixed seed range) and DST (a fixed seed range) — seconds. Nightly: the same with fresh seed ranges, under ASan/UBSan and TSan where threads exist (Ch. 53 §53.5), on the AArch64 runner (Appendix A — the weaker memory model finds ordering bugs the x86 build hides), with the full-session golden master and the latency gate (Ch. 118). A property or DST failure in the nightly adds its shrunk counterexample / seed to the per-build set, permanently. Fuzzing (Ch. 53 §53.4.2, Ch. 101 §101.4.8) stays on the parsers; it is the right tool for hostile bytes and the wrong one for protocol logic, which DST covers with structured faults.

## 116.5 Pitfalls & anti-patterns

- **Testing only by golden replay.** It catches change, not wrongness, and never visits the paths the capture skipped. Properties and DST cover the space; masters freeze the result.
- **Hidden inputs in the core.** A clock read, a `static` counter, iteration over a hash map with pointer keys (Ch. 103 §103.5): the properties flake, the shrinker cannot converge, the simulation cannot control time. The determinism rule is the testability rule.
- **Mocks with `virtual` on the hot path.** Two indirect calls per send and a BTB entry each (Ch. 15–16), *forever*, so that a test can substitute a clock. Inject by policy (§116.4.5) and prove it in the asm.
- **Asserting things that are not invariants.** "bid < ask" on a feed-driven book, "every order is acked" under a lossy venue — the test fails on real data and gets deleted. Invariants are what the *system* guarantees, not what a quiet day shows.
- **Unseeded randomness.** A failure that cannot be reproduced is a failure nobody fixes; the seed is part of the failure message and `--seed` replays it.
- **A simulator too realistic to be deterministic.** Threads, real sockets, real time inside the DST harness and the seed no longer decides the schedule. Single-threaded, simulated clock, every effect an event.
- **Regenerating golden masters to make the build green.** A master updated without a stated behaviour change is a disabled test.

## 116.6 Exercises & checklist

1. **Properties for the real book.** Take Ch. 32 §32.4.5's order book and write the conservation, level and idempotency invariants; run 10 000 seeds; add "bid < ask" and show which captured session breaks it.
2. **Shrink a real failure.** Introduce a one-character bug into the book (an `>` for `>=`) and record how many inputs the shrinker leaves; make the counterexample a table row.
3. **DST the session layer.** Give `SimVenue` sequence numbers, `ResendRequest` and `PossDup` handling (Ch. 106 §106.2.1) and assert that after any fault schedule the gateway's `next_in`/`next_out` agree with the venue's.
4. **Lease races.** Simulate Ch. 112's primary and standby with a `SimClock` and find a seed where a pause lands between the primary's lease check and its send; show the fencing token catching it.
5. **Prove the double free.** `objdump` your production gateway's `send_order` with and without the policy seam and diff the instruction multiset; repeat with a `virtual` seam and two implementations linked in.

Checklist:

- [ ] The core has **no hidden inputs** (§116.2.1); every property case folds twice and compares digests.
- [ ] The venue spec's examples and every incident are **table rows** (§116.4.1).
- [ ] Properties check **true invariants** with a seeded generator and a shrinker; failures print the seed and the minimal sequence (§116.4.2).
- [ ] The gateway's protocol logic runs under **DST** every build — simulated clock, fault-injecting venue double with an oracle, thousands of seeds (§116.4.3).
- [ ] Golden masters are **small capture slices + digest**, committed, updated only with a stated behaviour change (§116.4.4).
- [ ] Test doubles are **template policies**; the production instantiation's asm has been diffed against a seam-free build (§116.4.5).
- [ ] Nightly: fresh seeds, sanitizers, AArch64, the full-session master and the latency gate (§116.4.6, Ch. 118).

## 116.7 References

- Koen Claessen and John Hughes, "QuickCheck: A Lightweight Tool for Random Testing of Haskell Programs," ICFP 2000 — property-based testing; **RapidCheck** (C++ QuickCheck with shrinking) documentation.
- Andreas Zeller and Ralf Hildebrandt, "Simplifying and Isolating Failure-Inducing Input," IEEE TSE 2002 — delta debugging, the shrinker.
- Will Wilson, "Testing Distributed Systems with Deterministic Simulation" (Strange Loop 2014) and the FoundationDB *Testing* documentation; Antithesis' writing on deterministic hypervisor testing; TigerBeetle's VOPR — DST in production systems.
- Gerard Meszaros, *xUnit Test Patterns* (2007) — the test-double vocabulary (stub, fake, mock) this chapter maps onto policies.
- GoogleTest, Catch2 and doctest documentation — the harnesses to swap in for the `CHECK` macro.
- Ch. 103 §103.2.2 and §103.4.6 (the deterministic core and the digest), Ch. 105 §105.2.3 (the invariants DST asserts), Ch. 22 §22.4.3 (policy injection), Ch. 16 (the cost of a virtual seam), Ch. 53 (the concurrency tooling this complements).

## 116.8 Additional Reading

- Hughes, "Experiences with QuickCheck: Testing the Hard Stuff and Staying Sane" (2016) — what properties to write when the system is stateful, and how to model it.
- The Jepsen methodology (Kyle Kingsbury) — history-based checking of distributed invariants; the heavyweight cousin of §116.4.3's oracle.
- Ch. 118 — the same CI, the performance half: A/A calibration, bootstrap CIs on p99, the nightly replay gate on the tuned box.

---

*Next: Ch. 117 — Case Study: The Exchange Simulator, the venue that §116.4.3's `SimVenue` grows into: Ch. 33's book run from the inside as a deterministic price-time matching engine at 11 M orders/s with pro-rata, self-trade prevention and an auction cross, a latency model sampled from captured ack timestamps with the tail intact (a lognormal fit is 10× wrong at p99.9), a queue-position model fed by the MBO stream, and fault schedules as seeded inputs — the closed loop the CI gates, the quoter's thresholds and the resilience drills run against.*
