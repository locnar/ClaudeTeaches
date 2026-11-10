# Part VI — Concurrency

# Chapter 45 — Coroutines & Async Models

> **Prerequisites:** Ch. 28–29 (allocation — the coroutine frame may heap-allocate, the central hazard), Ch. 21 (zero-cost abstractions / the optimizer — coroutine elision depends on inlining), Ch. 4 (reading asm/checking what the compiler did), Ch. 39–42 (the lock-free/Disruptor pipeline coroutines may or may not replace), Ch. 56–57 (epoll/io_uring — the I/O coroutines drive).
>
> **Leads into:** Ch. 46 (`std::execution` — the sender/receiver sibling a coroutine can `co_await`, and the chapter that verifies *its* codegen), Ch. 56 (Linux native I/O — `epoll` event loops) and Ch. 57 (io_uring — completion-based async that pairs naturally with coroutines) — this chapter's awaiters (§45.4.2) wrap those. Coroutines are an *ergonomics* tool here, weighed against the busy-poll hot path of Ch. 42/48.

---

## 45.1 Why it matters: async I/O without thread-per-connection

The traditional way to handle many concurrent connections is **thread-per-connection**: one thread blocks on each socket's read. It's simple to write but scales terribly — thousands of threads mean thousands of stacks (memory), constant context switching (Ch. 48 — the jitter killer), and oversubscription (Ch. 36). The asynchronous alternative — a single thread (or a few) driving an **event loop** over many non-blocking sockets (`epoll`/io_uring — Ch. 56–57) — scales beautifully but has historically been *painful to write*: you shatter your logic into callbacks ("callback hell"), manually threading state through continuations, with control flow turned inside-out. **C++20 coroutines** are the language feature that lets you write asynchronous, event-loop-driven code that *reads like synchronous, sequential code* — `co_await socket.read()` looks like a blocking read but actually suspends the coroutine and returns control to the event loop, resuming when the data arrives. You get the scalability of async with the readability of sequential code.

For a trading system this matters in specific places, and it's important to be clear-eyed about *where*. The **single-message tick-to-trade critical path** is not usually one of them: that path is latency-bound, runs on a dedicated busy-polling core (Ch. 42, 48), processes one message with no I/O wait to hide, and wants the absolute minimum of abstraction — a coroutine's suspend/resume machinery is overhead it doesn't need. Where coroutines *do* earn their place is the **I/O-bound, many-connection, concurrency-shaped** work around that core: managing many venue connections (order entry sessions, drop copies, market-data subscriptions), session/protocol state machines (FIX session logic — Appendix J.3, Ch. 63 §63.4.4), control-plane and admin connections, and anything that's naturally "wait for I/O, then react" across many endpoints. There, coroutines turn a tangle of callbacks and explicit state machines into linear, maintainable code without the thread-per-connection cost — a real ergonomics and correctness win.

The catch — and the reason this chapter is as much warning as endorsement — is that C++20 coroutines have a **performance hazard hiding in plain sight: the coroutine frame** (the state that persists across a suspend) is, in general, **heap-allocated** (Ch. 28). Every coroutine *call* can be a `malloc` — the exact hot-path poison Part IV spent four chapters eliminating. The standard provides a path to *elide* that allocation (HALO — Heap Allocation eLision Optimization), but it's an *optimization the compiler may or may not perform*, depending on inlining and whether the coroutine's lifetime is provably bounded — so "does this coroutine allocate?" is a question you must *verify* (Ch. 4, instrument `operator new` — Ch. 28), not assume. A coroutine that allocates its frame per invocation on a hot path is a latency disaster wearing clean syntax. So this chapter covers the coroutine mental model (§45.2), measures resume cost and frame allocation (§45.3), shows event loops and custom I/O awaiters (§45.4), proves elision in the asm (§45.5), and is emphatic about the hidden-allocation pitfall (§45.6) — with the overarching guidance: **coroutines for the I/O-bound concurrency around the hot path where their ergonomics pay; keep them off the latency critical path unless you've verified zero allocation and acceptable overhead.**

## 45.2 Mental model: C++20 coroutines; stackful vs stackless

**What a coroutine is.** A function that can **suspend** (pause, returning control to its caller/resumer) and later **resume** from where it left off, with its local state preserved across the suspension. C++20 makes a function a coroutine if it uses `co_await`, `co_yield`, or `co_return`. The three keywords:

- **`co_await expr`** — suspend until `expr` (an *awaitable*) is ready, then resume with its result. The workhorse for async I/O: `co_await socket.read()` suspends the coroutine and hands control back to the event loop; when the read completes, the loop resumes the coroutine.
- **`co_yield v`** — produce a value and suspend (generators/lazy sequences).
- **`co_return v`** — finish the coroutine with a result.

**Stackless (C++20) vs stackful coroutines** — a fundamental distinction:

- **Stackless (C++20 coroutines):** the coroutine's persistent state — its locals that live across a suspend, the resume point — is stored in a **coroutine frame** (a flat struct), *not* a full stack. Only one "level" suspends (you can't suspend from a nested ordinary function call inside the coroutine — only the coroutine itself suspends). The frame is small (just what crosses suspends) and **may be heap-allocated** (the central hazard — §45.3, §45.6). Lower memory per coroutine; the language/compiler manages the frame. This is what C++20 gives you. Toolchain: GCC 10 (with `-fcoroutines`) / GCC 11+ by default under `-std=c++20`, Clang 14+ (libstdc++ or libc++ 14+), MSVC 19.28+; gate on `__cpp_impl_coroutine` (language) and `__cpp_lib_coroutine` (`<coroutine>`) — Appendix H.
- **Stackful (libraries: Boost.Context/Fiber, ucontext):** each coroutine (fiber) has its *own full stack*; it can suspend from *any* depth of nested calls (suspend deep inside a helper). More flexible (any call can yield) but each needs a full stack (more memory — KBs each), and switching swaps stacks. Used by fiber libraries and some async frameworks.

For async I/O, **stackless C++20 coroutines** are the standard modern choice (low per-coroutine memory, language-integrated); stackful fibers suit cases needing to yield from arbitrary depth.

**The frame and its allocation (the crux).** When you call a coroutine, the compiler:

```
   1. allocates the coroutine FRAME (holds locals-across-suspend + resume point + promise)
        → in general, via operator new  ← THE HAZARD (a malloc per call — Ch. 28)
   2. runs the body until the first suspend point
   3. returns a HANDLE to the caller (the coroutine is now suspended, or running)
   ... later: resume(handle) continues from the suspend point; final suspend destroys the frame
```

- **The frame is heap-allocated by default**, via `operator new` (customizable per-promise). This is the per-invocation `malloc` that makes coroutines dangerous on the hot path.
- **HALO (Heap Allocation eLision Optimization)** — *if* the compiler can prove the coroutine's frame doesn't outlive the caller (the coroutine is created, awaited, and destroyed within a bounded scope the caller can see — typically requiring inlining), it can **elide** the allocation and put the frame on the caller's stack (or fold it away). This makes a coroutine genuinely zero-overhead — *when it fires*. But it's an *optimization*, not a guarantee: it depends on inlining (Ch. 21), visibility, and the coroutine's lifetime being provable. **You must verify it happened** (§45.3). And it is Clang-only: LLVM implements HALO as the `CoroElide` pass, while GCC — through at least GCC 14/15 — performs no frame elision at all (GCC bugzilla 100579, 115309), so on GCC every coroutine call allocates its frame unless `promise_type::operator new` supplies the memory (§45.4.2).
- **`promise_type` and awaiters** — the customization machinery: the **promise** (per-coroutine, controls behavior: what `co_return` does, the initial/final suspend, custom `operator new`) and **awaiters** (control what `co_await` does: `await_ready`/`await_suspend`/`await_resume` — the hook where you suspend until I/O completes — §45.4.2).

The model: **a coroutine suspends/resumes with its cross-suspend state in a frame; stackless C++20 coroutines keep the frame small but *heap-allocate it by default* (a per-call malloc — the hazard), which HALO *may* elide to the stack if it can prove the lifetime (Clang only — GCC never elides; verify it!). Coroutines give async I/O the readability of sequential code, at the cost of this allocation question and suspend/resume overhead.**

## 45.3 Measure it: coroutine resume cost

Two things to measure: the **suspend/resume cost** (the per-`co_await` overhead) and — the critical one — **whether the frame was heap-allocated or elided (HALO)**. The allocation question dominates: an elided coroutine is fast, an allocating one has the Ch. 28 tail.

```cpp
// coro.cpp — measure coroutine frame allocation (HALO or not) and suspend/resume cost.
// Build: g++ -O2 -std=c++20 coro.cpp -o coro      |   clang++ -O2 -std=c++20 coro.cpp -o coro
//   Detect allocation: operator new is replaced below to count calls.
// Run pinned:  taskset -c 2 ./coro
#include <coroutine>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <atomic>
#include <chrono>
#include <new>

std::atomic<long> g_allocs{0};
void* operator new(std::size_t n) {
    g_allocs.fetch_add(1, std::memory_order_relaxed);
    if (void* p = std::malloc(n ? n : 1)) return p;
    throw std::bad_alloc{};
}
void  operator delete(void* p) noexcept { std::free(p); }
void  operator delete(void* p, std::size_t) noexcept { std::free(p); }

// (1) A minimal eager task that returns a value (no suspend — measuring FRAME cost).
struct Task {
    struct promise_type {
        std::int64_t value;
        Task get_return_object() { return Task{std::coroutine_handle<promise_type>::from_promise(*this)}; }
        std::suspend_never initial_suspend() noexcept { return {}; }   // run eagerly
        std::suspend_always final_suspend() noexcept { return {}; }
        void return_value(std::int64_t v) { value = v; }
        void unhandled_exception() {}
    };
    std::coroutine_handle<promise_type> h;
    std::int64_t get() { auto v = h.promise().value; h.destroy(); return v; }
};
Task add(std::int64_t a, std::int64_t b) { co_return a + b; }   // does THIS allocate a frame?

// (2) A lazy ticker that suspends on every step — measuring SUSPEND/RESUME cost (one frame, N resumes).
struct Ticker {
    struct promise_type {
        std::int64_t current = 0;
        Ticker get_return_object() { return Ticker{std::coroutine_handle<promise_type>::from_promise(*this)}; }
        std::suspend_always initial_suspend() noexcept { return {}; }
        std::suspend_always final_suspend() noexcept { return {}; }
        std::suspend_always yield_value(std::int64_t v) noexcept { current = v; return {}; }
        void return_void() {}
        void unhandled_exception() {}
    };
    std::coroutine_handle<promise_type> h;
    ~Ticker() { if (h) h.destroy(); }
};
Ticker ticks(long n) { for (long i = 0; i < n; ++i) co_yield i; }

int main() {
    constexpr long N = 10'000'000;
    // --- (1) frame cost: create + run + destroy one coroutine per iteration
    long allocs_before = g_allocs.load();
    auto t0 = std::chrono::steady_clock::now();
    std::int64_t sink = 0;
    for (long i = 0; i < N; ++i) {
        std::int64_t a = i;
        asm volatile("" : "+r"(a));                 // opaque input: the loop cannot be folded to a constant
        sink += add(a, 1).get();                    // create+run+destroy a coroutine
        asm volatile("" :: "r"(sink) : "memory");   // the result is used (Appendix L)
    }
    auto t1 = std::chrono::steady_clock::now();
    long allocs = g_allocs.load() - allocs_before;
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
    std::printf("create/run/destroy: sink=%lld  %.2f ns/coro  allocs=%ld (%.3f per call)\n",
                (long long)sink, ns/N, allocs, (double)allocs/N);

    // --- (2) resume cost: one frame, N suspend/resume round trips through the handle
    allocs_before = g_allocs.load();
    Ticker tk = ticks(N);
    t0 = std::chrono::steady_clock::now();
    for (long i = 0; i < N; ++i) { tk.h.resume(); sink += tk.h.promise().current; }
    t1 = std::chrono::steady_clock::now();
    asm volatile("" :: "r"(sink) : "memory");
    ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
    std::printf("suspend/resume:     sink=%lld  %.2f ns/resume  allocs=%ld for the whole run\n",
                (long long)sink, ns/N, g_allocs.load() - allocs_before);
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP, 2.9 GHz), Clang 18.1 vs GCC 13.3 / glibc 2.39, `-O2 -std=c++20`, Linux 6.18, pinned (illustrative; *whether HALO fires* is compiler-dependent — only Clang implements it, GCC always allocates — and under Clang version/flag/inlining-dependent). The harness feeds the coroutine an opaque input and sinks its result: without those barriers Clang folds the elided version's entire loop into one constant and prints 0.00 ns.

```
   scenario                                         ns/coro      allocs/call    note
   HALO fires (frame elided to stack) — Clang       ~1-3 ns      0.000          zero-overhead — the goal
   HALO does NOT fire (frame heap-allocated) — GCC  ~12-30 ns    1.000          a malloc+free per coroutine (Ch. 28): warm tcache here, the full tail in production
   suspend/resume through the handle (Ticker)       ~1-2 ns      0 per resume   the per-co_await overhead: an indirect call + frame state save/restore

   the difference between the first two rows is ENTIRELY whether the compiler elided the frame allocation.
```

Read it: the *same* coroutine is **~1-3 ns (elided) or ~12-30 ns + a malloc/free (not elided)** — a ~10× difference even with glibc's tcache fast path warm, and the not-elided case drags in the full allocation tail of Ch. 28 the moment the allocator is *not* warm (the `allocs/call = 1.000` is the smoking gun). Whether HALO fires depends on inlining and provable lifetime (§45.2) — small, leaf-ish, locally-awaited coroutines elide; coroutines whose handle escapes (stored, passed to an event loop, type-erased) generally *don't*. **The decisive measurement for any hot-path coroutine is `allocs/call`** — instrument `operator new` (as here) and confirm it's **0**. If it's 1, the coroutine allocates per invocation and is unfit for the hot path until you eliminate the allocation (force inlining, custom-allocate the frame via `promise_type::operator new` from a pool — Ch. 29, or don't use a coroutine there). The resume-cost row shows the *intrinsic* suspend/resume overhead (~1-2 ns per round trip through `h.resume()` — an indirect call plus the frame's state save/restore, GCC and Clang alike) — small, but non-zero, so a coroutine isn't free even at best. The guidance the numbers dictate: **coroutines are zero-overhead-ish *only when the frame is elided*; verify allocation, and route per-invocation-allocating coroutines off the hot path** (where I/O-wait dwarfs the overhead anyway).

## 45.4 Techniques

### 45.4.1 Event loops

The natural home for coroutines: a single-threaded **event loop** driving many I/O-bound coroutines over non-blocking sockets (Ch. 56–57):

- **The structure.** One thread runs a loop: wait for I/O readiness/completion (`epoll_wait` — Ch. 56, or io_uring completions — Ch. 57), and for each ready event, **resume** the coroutine waiting on it. Each connection/session is a coroutine that `co_await`s its I/O; when it suspends, the loop moves on to other ready coroutines. Thousands of connections, one (or a few) threads — the scalable alternative to thread-per-connection (§45.1).
- **Coroutine = a suspended computation the loop resumes.** A `co_await read()` registers interest (the awaiter — §45.4.2) and suspends, storing the `coroutine_handle` where the loop can find it; when the I/O completes, the loop calls `handle.resume()` and the coroutine continues as if the read had returned synchronously. The logic reads sequentially; the suspension is invisible in the source.
- **io_uring pairs naturally (Ch. 57).** io_uring's *completion*-based model (submit an op, get notified when done) maps cleanly onto coroutines: `co_await` an io_uring operation; the awaiter submits the SQE and suspends; the loop resumes the coroutine when the CQE arrives. (epoll's *readiness* model also works — `co_await` readiness, then do the non-blocking syscall.) This is the modern low-latency async I/O combination.
- **Where it fits in trading.** Many-connection, I/O-bound work: venue order-entry sessions, drop-copy/clearing connections, market-data subscriptions, admin/control connections, FIX session state machines (Ch. 62). Each session a coroutine, all driven by one event-loop thread — clean, scalable, no thread-per-connection. **Not** the busy-polled single-message hot path (Ch. 42, 48), which has no I/O wait to hide and wants no event-loop indirection.
- **Schedulers and frameworks.** Don't hand-roll the loop/scheduler for production: use a vetted coroutine I/O framework — `libcoro`, `asio` (with C++20 coroutine support), `folly::coro`, `seastar`, or the C++26 senders/receivers (`std::execution`) model (Ch. 46). They provide the event loop, awaiters, schedulers, and cancellation, with the allocation behavior documented.

### 45.4.2 Custom awaiters for I/O

The hook where coroutines meet your I/O layer is the **awaiter** — the object `co_await` operates on, with three methods:

- **`await_ready()`** — "is the result already available?" Return `true` to skip suspension (e.g. data already buffered — a fast path that avoids the suspend/resume cost entirely). A good awaiter checks this so cheap/ready operations don't pay suspension overhead.
- **`await_suspend(handle)`** — called when suspending: **register** the operation (submit the io_uring SQE — Ch. 57, or add the fd to epoll — Ch. 56, and store `handle` so the event loop can resume it on completion), then return control to the loop. This is where you wire the coroutine into your async I/O machinery.
- **`await_resume()`** — called on resume: return the operation's **result** (the bytes read, the error). The `co_await read()` expression evaluates to this.
- **`await_suspend`'s three return types — and symmetric transfer.** Returning `void` always suspends; returning `bool` lets the awaiter say "actually, don't suspend" (`false`) after it has done its registration — closing the race where the I/O completed between `await_ready` and `await_suspend`; returning a **`std::coroutine_handle<>`** makes the compiler *tail-call* that coroutine's resume instead of returning to the resumer — **symmetric transfer**. That last form is what keeps an event loop of chained coroutines (A completes → resumes B → which completes → resumes C …) from nesting `resume()` calls on the machine stack until it overflows: each hand-off is a jump, not a call, and `std::noop_coroutine()` is the handle you return when there is nothing to transfer to. It also sets the per-hop cost: `handle.resume()` is an *indirect call* through the frame's resume pointer (the ~1-2 ns of §45.3), and a symmetric transfer is the same indirect jump without the return — so a resume chain costs one BTB-predicted indirect branch per hop (Ch. 15), not a stack frame.

Practical points:

- **Custom awaiters wrap epoll/io_uring (Ch. 56–57).** Write awaiters for your I/O primitives (`async_read`, `async_write`, `async_accept`) that submit to io_uring / register with epoll in `await_suspend` and return the result in `await_resume`. This is the bridge between the coroutine world and the kernel I/O interface.
- **The ready fast path matters.** `await_ready() == true` (data already available) skips suspend/resume — important when much I/O is immediately satisfiable (buffered data), avoiding the per-await overhead (§45.3) on the common case.
- **Custom frame allocation (`promise_type::operator new`).** To control the allocation hazard (§45.6), give the `promise_type` a custom `operator new` that draws the frame from a **pool/arena** (Ch. 29) instead of the global heap — so even non-elided coroutines don't hit `malloc` on the hot path. This is the escape when HALO won't fire but you still want the coroutine.
- **Cancellation and lifetime.** Async coroutines need cancellation (a connection drops mid-`co_await`) and careful lifetime management (the `coroutine_handle` must outlive the pending I/O, and be destroyed exactly once). Frameworks handle this; hand-rolled awaiters must get it right (use-after-free / leaked frames otherwise — §45.6).

## 45.5 Verify the codegen: did the frame get elided?

HALO is purely an optimizer decision, so the only proof is the asm (Ch. 4). The question has a one-line answer: **is there a call to `operator new`/`malloc` on the coroutine's creation path?** Snippets are **verified** output for the §45.3 `add` coroutine (`-O2 -std=c++20`, x86-64; Appendix L.3).

**Clang 18.1 — the frame is elided and the coroutine disappears.** `add` is inlined into `main`, the `CoroElide` pass proves the frame dies before the caller returns, and what is left of `sink += add(a, 1).get()` is two integer adds — no frame, no allocation, no `.destroy`:

```asm
.LBB5_1:                             ; the §45.3 timing loop, Clang -O2
        mov     rcx, rax             ; a = i (the opaque input)
        add     rbx, rcx             ; sink += a ...
        inc     rbx                  ; ... + 1          <- the whole coroutine, inlined and elided
        inc     rax
        cmp     rax, 10000000
        jne     .LBB5_1
```

**GCC 13.3 — the frame is heap-allocated on every call.** GCC lowers the coroutine in the front end and never elides: the *ramp* (`_Z3addll`, or its inlined body) calls `operator new` — here our counting replacement, so `malloc` — and the loop calls the generated `.Frame.destroy` to free it:

```asm
main:                                ; GCC -O2, the same loop
        ...
        call    malloc@PLT                                   ; <- the frame: one allocation per coroutine call
        ...
        call    _Z3addPZ3addllE14_Z3addll.Frame.destroy      ; <- and its release (frees the frame)
```

**How to read it in your own code.** (1) `objdump -d --no-show-raw-insn app | grep -A3 -B3 -E '_Znwm|operator new|malloc'` around the coroutine's ramp — any hit means allocation per call; (2) with a replaced `operator new` the §45.3 `allocs/call` counter is the runtime form of the same check; (3) a symbol named `*.Frame.destroy` / `*.destroy` / `*.resume` surviving in the binary means the coroutine was *not* folded away (it may still be elided-to-stack under Clang — look for the `new` call, not for the symbol); (4) the **frame size** is visible as the `operator new` size argument (`mov edi, <N>` before the call on GCC) — a frame that grows after a refactor is a sign more state crosses a suspend point. When Clang stops eliding — the handle escapes into an event-loop table, the coroutine is type-erased, or inlining fails — this asm is where you see it first.

## 45.6 Pitfalls & anti-patterns: hidden allocations in coroutine frames

- **Per-invocation frame allocation (the headline hazard).** A coroutine whose frame *isn't* elided (HALO didn't fire) does a `malloc` **per call** (§45.3) — the Ch. 28 allocation tail, hidden behind clean syntax. **Verify `allocs/call == 0`** (instrument `operator new` — Ch. 28, §45.3) for any hot-path coroutine; if it's 1, force inlining, custom-allocate the frame from a pool (§45.4.2), or don't use a coroutine there.
- **Relying on HALO without verifying.** HALO is an *optimization*, not a guarantee — it depends on inlining, visibility, and provable lifetime, and *breaks silently* when the handle escapes (stored in the event loop, type-erased, returned). "Coroutines are zero-cost" is true *only when elided*. Check the codegen / allocation count (§45.5, §45.3); a refactor can turn an elided coroutine into an allocating one.
- **Coroutines on the latency critical path.** The single-message tick-to-trade path (Ch. 42, 48) is busy-polled, has no I/O wait to hide, and wants minimal abstraction — a coroutine's suspend/resume overhead (~1-2 ns per resume even elided — §45.3) and allocation risk are unnecessary cost there. Keep coroutines for the **I/O-bound, many-connection** work where the wait dominates; don't coroutine-ify the hot loop.
- **Type-erasing the coroutine (e.g. a generic `task<T>`) defeating elision.** A type-erased task (storing the handle behind an interface) almost always *prevents* HALO (the lifetime isn't provable) → allocation. Use concrete awaitable types and keep coroutines local where you need elision; accept allocation (from a pool) where you need type erasure off the hot path.
- **Dangling `coroutine_handle` / use-after-free.** Resuming or accessing a coroutine after its frame was destroyed (or destroying it twice) is UB (Ch. 89). Lifetime management — the handle must outlive pending I/O, be resumed/destroyed exactly once — is subtle; use a framework, or be very careful with hand-rolled awaiters.
- **Capturing references into the frame that dangle across suspend.** A reference/pointer to a stack temporary captured in a coroutine local can dangle after a suspend (the caller's stack moved on). Be careful what lives across a `co_await` (the frame holds locals, but references to *external* temporaries can dangle).
- **`std::suspend_never`/`always` and eager-vs-lazy confusion.** Whether a coroutine runs eagerly (initial `suspend_never`) or lazily (`suspend_always`) changes semantics and where work happens; getting it wrong causes work to run at the wrong time or not at all. Understand the promise's initial/final suspend.
- **Stackful fibers' memory cost.** Stackful coroutines (Boost.Fiber) give each a full stack (KBs) — thousands of fibers = lots of memory and TLB pressure (Ch. 16). For high connection counts, stackless C++20 coroutines (small frames) scale better.
- **Hand-rolling the scheduler/event loop for production.** The promise/awaiter/scheduler/cancellation machinery is intricate and easy to get wrong (leaks, UAF, lost resumptions). Use a vetted framework (`asio`, `libcoro`, `folly::coro`, senders/receivers); hand-roll only for learning.

## 45.7 Exercises & checklist

**Exercises**

1. **Did it allocate?** Build `coro.cpp` (override `operator new` to count). Measure `allocs/call` and ns/coro. Does HALO fire at `-O2` under Clang? Build the same source with GCC and confirm it never elides. Under Clang try `-O0` (allocates), force-inline, and make the handle escape (store it) — watch elision break. This is the §45.3 measurement.
2. **Pool-allocated frames.** Give the `promise_type` a custom `operator new`/`delete` that draws from an arena/pool (Ch. 29). Confirm (counter) that even non-elided coroutines no longer hit global `malloc`. Measure the ns/coro improvement.
3. **Event-loop echo server.** Build a tiny coroutine echo server over `epoll` (Ch. 56) or io_uring (Ch. 57): one coroutine per connection, `co_await async_read`/`async_write`. Compare lines-of-code and clarity to a callback-based version. Measure connections/thread vs thread-per-connection.
4. **The ready fast path.** Implement an awaiter with `await_ready()` returning true when data is buffered; measure the overhead saved vs always suspending, on a workload where most reads are immediately satisfiable (§45.4.2).
5. **Critical-path comparison.** Implement a single-message processing step as (a) a plain function and (b) a coroutine; measure the overhead difference on the hot path. Confirm the coroutine adds suspend/resume (and maybe allocation) cost — illustrating why the hot path stays coroutine-free (§45.6).

**Checklist — coroutines & async models**

- [ ] Coroutines are used for **I/O-bound, many-connection** work (sessions, venue connections, control plane) — **not** the busy-polled single-message **latency critical path** (Ch. 42, 48).
- [ ] For any hot-ish coroutine, I **verified `allocs/call == 0`** (instrumented `operator new` — §45.3, Ch. 28) — HALO fired or the frame is **pool-allocated** via custom `promise_type::operator new` (Ch. 29); I did not *assume* elision.
- [ ] I checked that refactors (type erasure, escaping the handle) didn't **silently break HALO** into per-call allocation — in the asm, no `operator new` on the creation path (§45.5, §45.6).
- [ ] Awaiters implement the **`await_ready` fast path** (skip suspension when data is ready) and correctly wire `await_suspend` into the **epoll/io_uring** event loop (Ch. 56–57).
- [ ] **Lifetime/cancellation** is handled — no dangling `coroutine_handle`, no double-destroy, no references-into-temporaries dangling across `co_await` (Ch. 89).
- [ ] I used **stackless C++20 coroutines** (small frames) for high connection counts (not stackful fibers' KB stacks) where memory scales.
- [ ] Production uses a **vetted framework** (`asio`/`libcoro`/`folly::coro`/senders-receivers) — not a hand-rolled scheduler.
- [ ] The intrinsic **suspend/resume overhead** (~1-2 ns per resume) is acceptable for where it's used (I/O-wait dominates) — measured, not assumed free.

## 45.8 References

- ISO C++ / cppreference — coroutines (`co_await`/`co_yield`/`co_return`, `promise_type`, awaiters, `coroutine_handle`) and the coroutine frame / HALO semantics (§45.2).
- L. Baker & G. Nishanov, the C++ coroutines design papers and talks (Nishanov's CppCon talks on coroutines and HALO) — the model and the elision optimization (§45.2-§45.3).
- The `asio`, `libcoro`, `folly::coro`, and Seastar documentation — production coroutine I/O frameworks, event loops, and awaiters (§45.4).
- E. Niebler / the `std::execution` (senders/receivers, P2300) papers — the C++26 async model that generalizes coroutines/awaiters.
- The io_uring (`liburing`) and `epoll(7)` documentation — the I/O primitives awaiters wrap (Ch. 56–57).

## 45.9 Additional Reading

- Lewis Baker's "Asymmetric Transfer" blog series on C++ coroutines — the definitive deep dive on awaiters, promises, and the frame.
- Raymond Chen's coroutine series and various CppCon coroutine talks — practical patterns and the allocation/HALO realities.
- Ch. 28–29 (*Memory*) — the allocation hazard and pool frames; Ch. 56 (*epoll*) and Ch. 57 (*io_uring*) — the async I/O coroutines drive; Ch. 42/48 (*Disruptor / Context Switching*) — the busy-poll alternative on the hot path; Ch. 62 (*Wire Decoding*) — FIX session state machines as coroutines; Ch. 21 (*Zero-Cost*) — the inlining HALO depends on.
- Ch. 46 (*std::execution: Senders/Receivers & Structured Async*) — the C++26 standard async model, the sender/receiver alternative to raw coroutines, and how to verify it compiles away.
- **Ch. 82 §82.4.5** and **Ch. 84 §84.4.5** — async models in other languages (Rust `async`/`await` state machines with a library executor; Go's goroutines and their runtime scheduler), with **Appendix B** (B.3) as the one-line map; **Appendix E** — the context-switch cost a coroutine resume avoids.

---

*Next: Ch. 46 — std::execution: Senders/Receivers & Structured Async, the C++26 standard async model — schedulers, sender pipelines, and structured concurrency as a zero-cost abstraction, and how to verify it compiles down to the hand-written state machine before putting it on the hot path.*
