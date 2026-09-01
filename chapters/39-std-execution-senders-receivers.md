# Part VI — Concurrency

# Chapter 39 — std::execution: Senders/Receivers & Structured Async

> **Prerequisites:** Ch. 19–20 (zero-cost abstractions and their cost — the bar this must clear), Ch. 38 (C++20 coroutines and async models — the sibling mechanism), Ch. 31 (threading), Ch. 37 (the Disruptor pipeline — a hand-built version of what senders express), Ch. 42 (pinning — where a scheduler must place work), Ch. 48 (io_uring — an async source to plug in), Ch. 4 (reading codegen — to prove it compiles away).
>
> **Leads into:** Ch. 40 (concurrency correctness tooling — testing the async pipelines this chapter builds), Appendix B (alternative async models). The newest C++ mechanism in the book, closing out the concurrency toolkit: the C++26 standard async model, evaluated by the one question that matters for the hot path — does it compile down to what you'd write by hand?

---

## 39.1 Why it matters: a standard async model, if it's actually zero-cost

Asynchronous, composable work — "receive a packet, decode it, run the strategy, send the order, and do the next stage when the previous finishes" — is the shape of a trading pipeline (Ch. 37's Disruptor is exactly this). Until recently C++ had no *standard* way to express it: you hand-rolled state machines (Ch. 37), used raw C++20 coroutines (Ch. 38), or adopted a library executor. **C++26's `std::execution`** (the "senders/receivers" model, from proposal P2300) is the standard's answer — a composable, typed, structured way to describe asynchronous work and where it runs, designed from the outset to be a **zero-cost abstraction** (Ch. 19–20): the composition happens at compile time, and the generated code is meant to be as tight as a hand-written state machine, with no hidden allocation, no type erasure, and no runtime scheduler overhead on the happy path.

That last clause is the whole reason this chapter exists, and the whole reason to be skeptical. The low-latency question is never "is this a nice abstraction?" — it's "**what does it compile to?**" (Ch. 4, 19–20). A composable async framework that *looks* elegant but allocates per operation, type-erases every stage, or hops through a scheduler on each step would be disqualified from the hot path no matter how clean the API. `std::execution` was explicitly designed to avoid those costs — senders are lazy, composed by value, and the whole pipeline is instantiated as one concrete type the compiler can inline and optimize end-to-end — but "designed to be zero-cost" and "is zero-cost on your compiler for your pipeline" are different claims, and only the codegen (§39.5) settles it.

So this chapter treats `std::execution` the way the book treats every abstraction (Ch. 19–20): explain the model (§39.2), measure the overhead against a hand-written baseline (§39.3), show the techniques for using it on the hot path — custom low-latency schedulers, sender pipelines, structured concurrency (§39.4), **verify it compiles away** (§39.5), and warn about the ways it stops being zero-cost (§39.6). It's both the newest C++ feature in the book and a final case study in the book's central discipline: reach for the modern abstraction, but *prove* it costs nothing before you put it on the hot path. (As of writing, `std::execution` is C++26; the reference implementation is `stdexec` — name the toolchain, per the book's convention.)

## 39.2 Mental model: senders, receivers, schedulers, and structured concurrency

`std::execution` describes asynchronous work as **senders** composed into a pipeline, connected to **receivers**, and run on **schedulers**:

```
   sender  = a lazy description of async work that will produce a value/error/stopped
   receiver = the continuation: what to do with the sender's value / error / stopped signal
   scheduler = a handle to an execution context (a thread, a pool, a pinned core, an io_uring loop)
   connect(sender, receiver) -> operation_state   // the concrete, instantiated work
   start(operation_state)                          // begins it

   decode = schedule(sched)                         // start on this scheduler
          | then(decode_fn)                         // then transform the value
          | then(strategy_fn)                       // then run the strategy
          | then(send_order_fn);                    // then emit the order
   //  ^ the whole pipeline is ONE lazily-composed sender; connect+start instantiates it
```

The pieces and why they're built this way:

- **Senders are lazy and composed by value.** A sender doesn't *do* anything until connected and started — it's a *description*. Composing senders (`then`, `let_value`, `when_all`, `bulk`) builds a bigger sender whose **type encodes the whole pipeline**. Because it's all by-value and typed (no type erasure by default), the compiler sees the entire pipeline as one concrete type and can inline and optimize it end-to-end (Ch. 19) — the mechanism behind the zero-cost claim.
- **Receivers are the typed continuation.** A receiver has three channels — `set_value` (success), `set_error` (failure — the `std::expected`-style hot-path error handling of Ch. 20), and `set_stopped` (cancellation) — so error and cancellation are first-class and typed, not exceptions (Ch. 20) or sentinels.
- **`connect` + `start` instantiate the work.** `connect(sender, receiver)` produces an **`operation_state`** — the concrete object holding the pipeline's state (like a hand-written state machine's struct, Ch. 37) — with **no heap allocation** (the op-state can live on the stack / in a member). `start` begins it. This is where "zero-cost" is won or lost: a good implementation puts the whole op-state inline with no allocation; the codegen (§39.5) confirms it.
- **Schedulers place work.** A **scheduler** is a handle to *where* work runs — a thread pool, a `run_loop`, a custom pinned-core context (Ch. 42), an io_uring loop (Ch. 48). `schedule(sched)` returns a sender that completes *on* that scheduler, so `then(...)` after it runs there. This is how you say "run this stage on the hot core" or "this stage on the io_uring completion" — placement is explicit and typed. Each `schedule` is potentially a **scheduler hop** (a handoff to another context), and hops are where latency hides (§39.6) — so a hot-path pipeline minimizes them (often runs the whole pipeline inline on one scheduler).
- **Structured concurrency.** Senders compose into *structured* graphs (`when_all` for parallel, `let_value` for dependent, `bulk` for data-parallel) with well-defined lifetimes and cancellation propagation (`set_stopped` / stop tokens flow through the graph). "Structured" means async work has scope-bound lifetime like RAII (Ch. 23) — no dangling continuations, no leaked async work — the async analog of the fault-containment discipline (Ch. 74).

The mental model: **a sender pipeline is a hand-written state machine (Ch. 37) that the compiler generates for you from a composable, typed description — and if the implementation is good, the generated machine is the one you'd have written by hand, with the op-state inline and the stages inlined together.** The abstraction's job is to make the pipeline *composable and safe* without adding cost; whether it succeeds is a codegen question (§39.5).

## 39.3 Measure it: sender pipeline overhead vs a hand-written state machine

The measurement that matters: **does a sender pipeline cost more than the hand-written equivalent** (a raw state machine — Ch. 37, or a raw function chain)? Build the same small pipeline (decode → transform → emit) three ways — hand-written inline, C++20 coroutine (Ch. 38), and a `std::execution` sender pipeline on an inline scheduler — and measure per-operation latency (Ch. 3, careful with dead-code elimination) and check for allocation. Representative (recent Clang/GCC with `stdexec`/library support, `-O2`; figures pending real runs):

| Implementation | Per-op latency | Allocation | Notes |
|---|---|---|---|
| **Hand-written inline** (Ch. 37 style) | baseline (~ns) | none | the target — a plain function chain / state struct |
| **`std::execution` sender pipeline** (inline scheduler) | ≈ baseline | none (op-state inline) | composed away; should match hand-written when it inlines |
| **Coroutine** (Ch. 38, `-O2`) | baseline–small | none if HALO applies | depends on coroutine-frame elision (Ch. 38); can allocate if not elided |
| **Sender pipeline with a scheduler *hop* per stage** | + hop latency | none | each `schedule` to another context is a handoff — real latency (§39.6) |
| **Type-erased sender** (`any_sender`) | + indirection | maybe | erasure defeats inlining (Ch. 14) — avoid on the hot path (§39.6) |

The lessons:

- **On an inline scheduler, a well-implemented sender pipeline should match the hand-written baseline** — the composition is a compile-time construction (Ch. 19), the op-state is inline (no allocation), and the stages inline together. This is the zero-cost promise, and when it holds, you get composability and structured lifetimes *for free*. But it's a claim to *verify per compiler* (§39.5), not assume.
- **Scheduler hops are real latency.** Each `schedule(other_sched)` that actually moves work to a different execution context is a handoff — a cross-thread wakeup or a queue push (Ch. 31–34), tens to hundreds of ns or more. A pipeline with a hop per stage is *not* zero-cost; it's a multi-hop async chain. Hot-path pipelines keep the whole chain on **one** scheduler (inline, on the hot core — Ch. 42) so there are no hops on the critical path.
- **Type erasure defeats it.** `any_sender` / erased schedulers reintroduce indirect calls (Ch. 14) and often allocation — the abstraction's cost model collapses to `std::function` (Ch. 25). Keep the hot-path pipeline **concrete** (fully typed, no erasure); use erasure only off the hot path where flexibility matters (the same rule as Ch. 14/25).
- **Compare against coroutines (Ch. 38), not in a vacuum.** Coroutines and senders solve overlapping problems; coroutines can allocate a frame if HALO (heap-allocation elision) doesn't fire (Ch. 38), senders avoid frames but need the pipeline concrete. Measure both on your compiler; the right choice is the one that compiles to the tighter code for *your* pipeline (§39.5).

## 39.4 Techniques

### 39.4.1 A custom low-latency scheduler pinned to the hot core

The key to using senders on the hot path: control *where* work runs with a purpose-built scheduler.

- **Write a scheduler over a pinned-core run loop (Ch. 42).** The stock schedulers (thread-pool, `run_loop`) aren't built for a hot core. Implement a custom scheduler whose execution context is the hot thread's own poll loop (Ch. 47/62), pinned to an isolated core (Ch. 42) — so `schedule(hot_sched)` runs the continuation *inline on the hot core*, with no cross-thread handoff. The scheduler is a thin adapter over the loop you already run; the sender pipeline then expresses the pipeline stages while executing entirely on-core.
- **Keep the whole hot pipeline on one scheduler.** Minimize scheduler hops (§39.3) — compose the decode→strategy→emit stages so they all complete on the hot scheduler, inline, with no handoff on the critical path (§39.6). Off-hot-path stages (logging — Ch. 71, capture — Ch. 75) can `schedule` to a housekeeping scheduler (Ch. 42) — a *deliberate* hop off the critical path.
- **Integrate async sources (io_uring — Ch. 48).** A scheduler/sender adapter over io_uring (Ch. 48) lets a completion be a sender (`async_read | then(handle)`), composing kernel-async I/O into the pipeline with the same model. `stdexec` and related libraries provide io_uring context adapters — the async receive path as a sender, feeding the on-core pipeline.

### 39.4.2 Sender pipelines and structured concurrency for the hot path

- **Express the pipeline as one composed sender.** Build the tick-to-trade stages as a single sender (`recv | then(decode) | then(strategy) | then(risk) | then(send)`) on the hot scheduler — the composable version of the Disruptor pipeline (Ch. 37). The value is *composability and typed error/cancellation* (via the receiver channels — §39.2) without giving up the hand-written codegen (§39.5).
- **Error handling via the error channel (Ch. 20).** Use `set_error` / `let_error` for hot-path errors (a malformed packet — Ch. 72, a risk rejection — Ch. 74) instead of exceptions (Ch. 20) — typed, no unwinding, the `std::expected`-style discipline (Ch. 20) expressed in the sender graph.
- **Structured lifetime and cancellation.** Use structured concurrency (`when_all`, scope-bound senders) so async work has RAII-like lifetime (Ch. 23) — no leaked continuations, deterministic teardown (which matters for the fast-restart/kill-switch of Ch. 74). Stop tokens (`set_stopped`) propagate cancellation through the graph — the clean way to cancel in-flight work on a kill-switch (Ch. 72/74).
- **Data-parallel stages with `bulk`.** For a genuinely parallel stage (e.g. risk across many positions, or a parallel computation — Ch. 29/68), `bulk` expresses data-parallel work over a scheduler — but keep it off the single-order critical path (parallelism is for throughput stages, not the one-order latency path — Ch. 67's lesson).

### 39.4.3 When senders help — and when to stay hand-written

- **Use senders where composability pays and the codegen holds.** For a pipeline that's assembled from reusable, typed stages with structured error/cancellation — and that you've *verified* compiles to the hand-written baseline (§39.5) — senders give maintainability and safety for zero cost. The control-plane, the setup/teardown, and the less-hot pipeline stages are natural fits.
- **Stay hand-written where every nanosecond is counted and the abstraction is thin.** For the very hottest inner loop where the pipeline is fixed and simple, a hand-written state machine (Ch. 37) or plain function chain may be clearer and removes any doubt about the abstraction — there's nothing to verify. Senders earn their place by *composition*; where there's little to compose, the hand-written version is fine (the book's "don't abstract what you won't reuse" pragmatism, Ch. 19–20).
- **Match to coroutines by measurement (Ch. 38).** Senders and coroutines coexist (a coroutine can `co_await` a sender). Choose per stage by which compiles tighter and reads clearer for *that* stage (§39.3) — not by ideology. Both are modern C++ async; the book's rule is the same for both: verify the codegen (§39.5).

## 39.5 Verify the codegen: does the pipeline compile away?

The zero-cost claim is settled only in the asm (Ch. 4, 19–20). Put a small sender pipeline on an inline scheduler in Compiler Explorer next to the hand-written equivalent and compare. A correctly zero-cost pipeline shows:

- **No heap allocation.** No `call` to `operator new` / `malloc` in the pipeline's `connect`/`start` — the `operation_state` is instantiated inline (on the stack or in a member). If you see an allocation per operation, the pipeline is *not* hot-path-ready — find why (type erasure, a scheduler that allocates, a sender that captures by allocation) and remove it (§39.6). This is the single most important thing to check.
- **The stages inlined together.** The decode/transform/emit functions inline into one straight-line sequence, the same instructions the hand-written chain produces (Ch. 19) — no per-stage function-call overhead, no indirection. Diff the asm against the hand-written baseline; they should be substantially the same. Divergence (extra calls, indirection) is the abstraction leaking cost.
- **No indirect calls (no type erasure).** No `call` through a function pointer / vtable (Ch. 14) in the pipeline — a concrete sender pipeline is all direct calls the compiler devirtualizes/inlines. An indirect call means an `any_sender` / erased scheduler crept in (§39.6) — the `std::function` failure mode (Ch. 14/25).
- **No scheduler-hop machinery on the inline path.** When the whole pipeline is on one inline scheduler, there should be no queue-push / wakeup / atomic-handoff code (Ch. 31–34) between stages — the continuation runs inline. If you see handoff code, a stage is hopping to another context (§39.3) — intended for off-path stages, a latency bug on the critical path.

The discipline (Ch. 19–20, one last time): **the abstraction is only zero-cost if the asm says so.** If the sender pipeline compiles to the hand-written state machine — no allocation, stages inlined, no indirection — put it on the hot path and enjoy the composability. If it doesn't, either fix the cause (§39.6) or stay hand-written (§39.4.3). Don't trust "designed to be zero-cost"; verify it, per compiler, per pipeline — exactly as the book has verified every abstraction.

## 39.6 Pitfalls & anti-patterns: hidden hops, type erasure, and allocation

- **Scheduler hops on the critical path (the cardinal latency error).** Each `schedule` to a *different* context is a real handoff (cross-thread wakeup / queue push — Ch. 31–34), tens–hundreds of ns (§39.3). A pipeline that hops per stage is a multi-hop async chain, not a zero-cost inline one. Keep the hot pipeline on **one** inline scheduler on the hot core (Ch. 42); hop only *off* the critical path (§39.4.1). Verify no handoff machinery in the codegen (§39.5).
- **Type erasure (`any_sender`, erased schedulers).** Erasure reintroduces indirect calls (Ch. 14) and often allocation — collapsing the cost model to `std::function` (Ch. 25). Keep the hot pipeline **fully concrete** (typed end-to-end); use erasure only off the hot path where flexibility is worth it (§39.3, §39.5).
- **Hidden allocation in the op-state.** A sender that captures by allocation, an allocating scheduler, or an implementation that heap-allocates the op-state — reintroduces `malloc` on the hot path (Ch. 23). Verify **no allocation** in the codegen (§39.5); it's the first thing to check and the most common leak.
- **Over-composing / over-abstracting.** Building a deep, elaborate sender graph where a simple function chain would do (§39.4.3) — more to verify, more chances for a hop/erasure to sneak in, less clarity. Senders earn their place by *composition*; don't reach for them where there's nothing to compose (Ch. 19–20).
- **Assuming zero-cost without verifying (per compiler).** `std::execution` is *designed* to be zero-cost, but implementation and compiler maturity vary (it's C++26; `stdexec` is the reference — name your toolchain). "Designed to be" ≠ "is on your compiler for your pipeline." Verify the codegen (§39.5) on the actual toolchain (§39.4.3).
- **Toolchain immaturity.** As a brand-new standard feature, compiler/library support, optimization quality, and debug experience (Ch. 5 — senders can be hard to debug when they *don't* inline) are still maturing. Pin the toolchain (Ch. 22), test the codegen on the version you ship, and be ready for the hand-written fallback (§39.4.3) where the compiler doesn't yet optimize it well.
- **Cancellation/lifetime bugs from unstructured use.** Bypassing structured concurrency (detached senders, manual lifetime) reintroduces the dangling-continuation and leaked-work bugs structured concurrency prevents (§39.2) — and those are exactly the hard-to-debug async bugs of Ch. 5. Use the structured forms (`when_all`, scope-bound senders, stop tokens); don't hand-manage async lifetime.
- **Confusing "async model" with "faster."** Senders are a *composition/placement* model, not a speedup — they don't make the work faster, they organize it. The win is composability + structured safety at (verified) zero cost; if you're not composing or you can't verify zero-cost, they're not buying you anything on the hot path (§39.4.3).

## 39.7 Exercises & checklist

**Exercises:**

1. **Sender vs hand-written codegen.** Build a decode→transform→emit pipeline as a hand-written chain and as a `std::execution` sender pipeline on an inline scheduler; diff the asm in Compiler Explorer (§39.5) — confirm no allocation, stages inlined, no indirection, and that they match (or find why not).
2. **Custom hot-core scheduler.** Implement a scheduler over a pinned-core poll loop (Ch. 42/47); run a sender pipeline entirely on it and verify (codegen + measurement) there are no scheduler hops on the critical path (§39.4.1, §39.5).
3. **Cost of a hop.** Insert a `schedule` to a *different* scheduler mid-pipeline and measure the added latency (§39.3) — quantify what a scheduler hop costs, and confirm the handoff machinery appears in the codegen (§39.5).
4. **Type-erasure cost.** Replace a concrete sender with `any_sender` and measure/inspect the added indirection and allocation (§39.6) — reproduce the `std::function`-style regression (Ch. 25).
5. **Senders vs coroutines.** Implement the same pipeline as a coroutine (Ch. 38) and as a sender; compare codegen and per-op latency (§39.3) — decide which compiles tighter for your pipeline on your toolchain (§39.4.3).

**Checklist:**

- [ ] The hot-path sender pipeline is verified in the **codegen** (§39.5): **no allocation**, **stages inlined** to match the hand-written baseline, **no indirect calls**, **no scheduler-hop machinery** on the critical path.
- [ ] The whole hot pipeline runs on **one inline scheduler pinned to the hot core** (Ch. 42); hops happen only **off** the critical path (§39.4.1, §39.6).
- [ ] The pipeline is **fully concrete** (no `any_sender` / erased schedulers on the hot path) (§39.3, §39.6).
- [ ] Errors and cancellation use the **typed receiver channels** (`set_error`/`set_stopped`, Ch. 20) — not exceptions; **structured concurrency** gives RAII-like async lifetime (§39.4.2).
- [ ] Senders are used where **composition pays** and the codegen holds; the very hottest fixed loop may stay **hand-written** (Ch. 37) (§39.4.3).
- [ ] Zero-cost is **verified per compiler/version** (C++26; name the toolchain — `stdexec`), not assumed; a **hand-written fallback** exists where the compiler doesn't yet optimize it (§39.6).
- [ ] Async I/O sources (io_uring — Ch. 48) are integrated as senders on the appropriate scheduler, off the critical path where they block (§39.4.1).

## 39.8 References

- **P2300 (`std::execution`)** and the C++26 working-draft `[exec]` clause — the senders/receivers model, algorithms, and schedulers (§39.2).
- **`stdexec`** (the NVIDIA/reference implementation) documentation and examples — the library to test against, including io_uring and pinned-context schedulers (§39.4).
- Talks on senders/receivers and structured concurrency (Eric Niebler, Lewis Baker, Kirk Shoop — CppCon) — the model and its zero-cost design (§39.2, §39.5).
- Ch. 19–20 (zero-cost abstractions / their cost — the bar and the discipline), Ch. 38 (coroutines — the sibling and the comparison), Ch. 37 (the Disruptor — the hand-written pipeline), Ch. 42 (pinning — the hot scheduler), Ch. 48 (io_uring — an async source), Ch. 4 (reading codegen).

## 39.9 Additional Reading

- The structured-concurrency literature (senders/receivers, and the broader "structured concurrency" idea from other languages) — lifetime and cancellation as first-class (§39.2, §39.4.2).
- `stdexec` performance and codegen studies as the implementations mature — whether it hits the zero-cost target in practice (§39.3, §39.5).
- Ch. 38 (*Coroutines*) — the other modern async model; Ch. 37 (*Disruptor*) — the hand-written pipeline senders express; Ch. 5 (*Debugging*) — debugging async control flow; **Appendix B** (*Beyond C++*) — async models in other languages; **Appendix F** — sender/receiver/scheduler glossary; **Appendix G** — the P2300 and structured-concurrency bibliography.

---

*Next: Ch. 40 — Concurrency Correctness Tooling, closing Part VI: the sanitizers (TSan/ASan/UBSan), stress testing, fuzzing, and model checking that catch the rare, non-deterministic bugs every lock-free structure and async pipeline in this Part can hide.*
