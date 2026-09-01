# Part IV — Memory Management

# Chapter 24 — Custom Allocators

> **Prerequisites:** Ch. 23 (memory-management fundamentals — *why* `malloc` is a hot-path hazard and the steady-state-zero-allocation rule this chapter delivers), Ch. 9 (alignment — allocators must return suitably-aligned storage), Ch. 7–8 (caches/layout — arenas exist partly for locality), Ch. 16 (NUMA — per-node arenas), Ch. 3 (measuring the distribution).
>
> **Leads into:** Ch. 25 (cache-friendly containers — which use allocators, and `pmr` containers), Ch. 26 (memory mapping — where an arena's backing memory comes from), Ch. 34/37 (lock-free queues / Disruptor — ring buffers are a specialized allocator), Ch. 72 (secure programming — lifetime/UAF bugs in arena designs). The single-writer/pool discipline reappears in Ch. 73–74.

---

## 24.1 Why it matters: steady-state zero allocation

Chapter 23 ended with a mandate — *the steady-state hot path must allocate zero times* — and an obvious objection: real trading systems have **dynamic-shaped, variable-lifetime data**. Orders arrive and rest and cancel; book levels appear and empty; messages flow through a pipeline of stages; events are queued, processed, recycled. You can't always size everything as a fixed stack array. The answer is **custom allocators**: data structures that pre-acquire a big slab of memory at startup (the cold path, Ch. 1) and then hand out and reclaim pieces of it on the hot path **without ever calling `malloc`** — turning "I need a new order object" from a kernel-risking, lock-taking, tail-spiking `new` (Ch. 23) into a handful of deterministic instructions.

The win is twofold and both halves matter. First, **determinism**: a pool allocation is a pointer pop off a free-list — a few nanoseconds, no lock, no syscall, no page fault, *no tail* — versus general `malloc`'s variable, occasionally-microsecond cost (Ch. 23.3). That collapses the p99.9 that unmanaged allocation inflates (Ch. 1). Second, **locality** (Ch. 7–8): a general allocator scatters related objects across the heap (a pointer-chase per access, a TLB miss per node — Ch. 15), whereas an arena packs objects allocated together *physically* together, so traversing them is cache- and TLB-friendly. A custom allocator is therefore both a *latency* tool (no `malloc` tail) and a *layout* tool (contiguous hot data) — which is exactly why the order-book and feed-handler chapters (Ch. 25) lean on them.

The cost is **complexity and correctness risk**, and this chapter is honest about it. You take over memory management, which means you own the lifetime bugs the general allocator's generality protected you from: use-after-free when an object outlives its arena reset, fragmentation in a pool that allocates variable sizes, alignment mistakes (Ch. 9), and the subtle interaction with object lifetimes (placement-`new`/`std::launder` — Ch. 21). The discipline is to match the *allocator strategy to the lifetime pattern* — an **arena/bump** allocator for "allocate many, free all at once" (a per-message or per-frame scratch), a **pool/free-list** for "many same-sized objects with individual lifetimes" (orders, nodes, events), `std::pmr` to plug these into standard containers — and to keep each allocator's ownership discipline simple enough to reason about. Get the match right and you get `malloc`'s flexibility at stack-allocation's cost; get it wrong and you've traded a latency problem for a memory-corruption one (Ch. 72).

## 24.2 Mental model: arena/pool/slab strategies; `std::pmr`

Custom allocators trade *generality* for *speed* by exploiting something the general allocator can't assume: a known **lifetime or size pattern**. The three canonical strategies, each matched to a pattern:

**Arena / bump allocator — "allocate many, free all at once."** A big contiguous buffer and a single *bump pointer*; allocation is `ptr += size` (plus alignment rounding), and there is **no individual free** — you reclaim everything at once by resetting the pointer to the start. Allocation is ~2-3 instructions, the fastest possible dynamic allocation; deallocation is *free* (one pointer assignment for the whole arena).

```
   arena: [████████░░░░░░░░░░░░░░░░░░]   alloc(n): p = cur; cur += round_up(n, align); return p
           ^used    ^cur          ^end   reset():  cur = base;   // frees EVERYTHING at once
```

Perfect for **per-message / per-frame scratch**: decode a packet into arena-allocated temporaries, process it, `reset()` the arena — every message starts from a clean, warm, contiguous buffer with zero per-object free cost. The constraint: you can't free one object while keeping others; lifetimes must nest with the reset.

**Pool / free-list allocator — "many same-sized objects, individual lifetimes."** Pre-carve the slab into fixed-size slots threaded onto a **free-list**; `allocate` pops the head, `deallocate` pushes it back — O(1), no search, no fragmentation (all slots identical), no lock if per-thread (or a lock-free stack for shared — Ch. 34).

```
   pool of Order slots:  [slot][slot][slot][slot]...    free_head ─► slot ─► slot ─► (nullptr)
   allocate(): p = free_head; free_head = p->next; return p;     // O(1) pop
   deallocate(p): p->next = free_head; free_head = p;            // O(1) push
```

The workhorse for **orders, book nodes, events, messages** — fixed-type objects with individual, interleaved lifetimes. (A **slab allocator** is the pool idea generalized to several size classes, one slab per class — what kernels use; for HFT a per-type pool is usually simpler and enough.)

**`std::pmr` (Polymorphic Memory Resources, C++17) — plugging custom allocators into standard containers.** Historically, using a custom allocator with `std::vector`/`std::map` meant the allocator was a *template parameter* (viral, type-changing). `std::pmr` makes the allocator a *runtime* `memory_resource*` the container holds, so `std::pmr::vector<T>`, `std::pmr::unordered_map`, etc. draw from whatever resource you give them — without changing the container's type. The standard provides `monotonic_buffer_resource` (an arena: bump-allocate, release all at once), `unsynchronized_pool_resource`/`synchronized_pool_resource` (pools), and `null_memory_resource` (allocation = error, to *prove* no allocation). You can back a `monotonic_buffer_resource` with a **stack buffer**, getting an STL container that allocates from the stack:

```cpp
std::array<std::byte, 64*1024> buf;                       // stack-backed
std::pmr::monotonic_buffer_resource arena{buf.data(), buf.size()};
std::pmr::vector<Order> orders{&arena};                   // allocates from the stack arena, not malloc
```

**Cross-cutting requirements every allocator must respect:**

- **Alignment (Ch. 9).** Returned storage must satisfy the type's alignment (`alignof(T)`, or over-alignment for SIMD/cache-line). Bump allocators round `cur` up to the alignment; pools align each slot.
- **Backing memory (Ch. 26).** The slab itself comes from *somewhere* — a static buffer, a one-time `malloc`/`new` at startup, or `mmap` (huge pages — Ch. 15, `mlock`ed and pre-faulted — Ch. 23.4.2). Acquire and warm it on the cold path.
- **Object lifetime vs storage (Ch. 21).** Allocators manage *storage*; you construct/destruct objects in it with placement-`new` and explicit destructor calls (or let the container do it). Reusing storage for a different object may need `std::launder` (Ch. 21).
- **Threading.** Per-thread allocators are lock-free by construction (the shared-nothing ideal — Ch. 31); shared ones need a lock (Ch. 32) or lock-free free-list (Ch. 34). NUMA: one arena/pool per node (Ch. 16).

The unifying model: **pick the allocator whose freeing discipline matches your lifetime pattern — bump for all-at-once, pool for same-size-individual — pre-acquire and warm its slab once, and the hot path allocates in O(1) with no `malloc`, no lock, no fault, contiguous for locality.**

## 24.3 Measure it: pool vs `new`/`delete` latency

The claim is determinism (no tail) and speed, so measure the **distribution** (Ch. 1, 3), not the mean — the same lesson as Ch. 23. Allocate-and-free a fixed-size object in a hot loop two ways: general `new`/`delete` vs a simple free-list **pool**.

```cpp
// pool.cpp — fixed-size object alloc/free: global new/delete vs a free-list pool.
// Build: g++ -O2 -std=c++20 -march=native pool.cpp -o pool
// Run pinned:  taskset -c 2 ./pool heap   |   ./pool pool
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>
#include <algorithm>
#include <chrono>

struct Order { std::int64_t id, px; std::int32_t qty; char side; char pad[3]; }; // ~24B

// A minimal fixed-size pool: pre-carved slots on a free-list.
class Pool {
    std::vector<std::byte> slab_;
    void* free_head_ = nullptr;
public:
    Pool(std::size_t n) : slab_(n * sizeof(Order)) {       // ONE allocation, at startup
        for (std::size_t i = 0; i < n; ++i) {              // thread the free-list
            void* p = slab_.data() + i * sizeof(Order);
            *reinterpret_cast<void**>(p) = free_head_;
            free_head_ = p;
        }
    }
    void* allocate() { void* p = free_head_; free_head_ = *reinterpret_cast<void**>(p); return p; }
    void  deallocate(void* p) { *reinterpret_cast<void**>(p) = free_head_; free_head_ = p; }
};

static inline std::uint64_t ns_now() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

int main(int argc, char** argv) {
    bool use_pool = (argc > 1) && std::strcmp(argv[1], "pool") == 0;
    constexpr int N = 2'000'000;
    Pool pool(1024);
    std::vector<std::uint32_t> samples; samples.reserve(N);
    std::uint64_t sink = 0;

    for (int i = 0; i < N; ++i) {
        std::uint64_t t0 = ns_now();
        if (use_pool) {
            Order* o = new (pool.allocate()) Order{i, 100 + i, 1, 'B', {}};   // placement-new
            sink += o->px; o->~Order(); pool.deallocate(o);                   // destroy + return
        } else {
            Order* o = new Order{i, 100 + i, 1, 'B', {}};                     // global new
            sink += o->px; delete o;                                          // global delete
        }
        samples.push_back(std::uint32_t(ns_now() - t0));
    }
    std::sort(samples.begin(), samples.end());
    auto pct = [&](double p){ return samples[std::size_t(p * (N - 1))]; };
    std::printf("%-5s sink=%llu  p50=%u p99=%u p99.9=%u max=%u ns\n",
                use_pool ? "pool" : "heap", (unsigned long long)sink,
                pct(0.50), pct(0.99), pct(0.999), samples.back());
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP), `-O2`, pinned, turbo off (illustrative; the *distribution shape* is the point):

```
              p50        p99        p99.9       max          notes
heap (new)    ~28 ns     ~95 ns     ~600 ns     ~5,000 ns    general malloc: variable, long tail
pool          ~4 ns      ~5 ns      ~6 ns       ~40 ns       free-list pop/push: flat, no tail
```

Read it the Ch. 1 / Ch. 23 way: the pool is not just ~7× faster at the median — it's **deterministic**, with a p99.9 essentially equal to its p50, while the heap's p99.9 is ~20× its median and its max reaches into the *microseconds* (the allocator's slow path / page-fault tail of Ch. 23). For the hot path the *flatness* matters more than the median: the pool removes the tail entirely. That's the whole value proposition — `malloc`'s flexibility (allocate/free individual objects with interleaved lifetimes) at a cost that's both lower *and* predictable. The fingerprint that justifies a custom allocator is exactly the heap row: a hot path doing per-object alloc/free with a p99.9 far above p50 and occasional microsecond maxes. Swap in the matching allocator (pool here) and the tail collapses — verified by the distribution, not the average.

## 24.4 Techniques

### 24.4.1 Arena and bump allocators

The arena (bump/monotonic) allocator is the tool for **all-at-once lifetimes** — allocate freely, then release everything with one reset.

- **The core.** A buffer + a bump pointer; `allocate(n, align)` rounds the cursor up to `align`, returns it, advances by `n`; `reset()` sets the cursor back to the base. No per-object free, no free-list, no fragmentation within a lifetime epoch.
- **Per-message / per-stage scratch.** The canonical hot-path use: each incoming message decodes into arena-allocated temporaries (parsed fields, intermediate structures, a working vector); after the message is handled, `reset()` — O(1), reclaims all of it, and the next message reuses the same warm, contiguous memory. No allocation, no free, perfect locality. (A pipeline can give each stage its own arena.)
- **`std::pmr::monotonic_buffer_resource` is exactly this**, standardized — back it with a stack buffer or a pre-faulted slab, hand it to `pmr` containers, and you get STL containers that bump-allocate and release together. Re-point/reconstruct the resource to "reset."
- **Constraints and care.** Everything in the arena must be done with the epoch (no object may outlive the `reset` that frees its storage — a UAF if it does; §24.5). Size the arena for the worst-case epoch (or chain fixed blocks when it overflows). Non-trivial destructors aren't called by a bump `reset` — either store only trivially-destructible objects, or track and run destructors before reset (`monotonic_buffer_resource` containers handle their elements' destruction; raw bump arenas don't).
- **Backing + warming (Ch. 23, 26).** Allocate the arena's slab once at startup, pre-fault and `mlock` it (Ch. 23.4.2), ideally on huge pages (Ch. 15) and the right NUMA node (Ch. 16). The hot path then never touches the kernel.

### 24.4.2 Object pools and free-lists

The pool is the tool for **many same-sized objects with individual, interleaved lifetimes** — the order/node/event case.

- **The core (§24.3).** Pre-carve a slab into fixed-size slots threaded on a free-list; `allocate` pops, `deallocate` pushes — O(1), no search, no fragmentation (uniform slots). Intrusively store the free-list `next` pointer *in the free slot itself* (it's unused while free), so the pool needs no side storage.
- **Type-specific pools.** A pool per hot object type (`Pool<Order>`, `Pool<PriceLevel>`) gives each the determinism of §24.3 and packs same-type objects contiguously (locality — Ch. 8, and the order-book case study of Ch. 25). Construct with placement-`new` into the slot, destruct explicitly before returning it (Ch. 21).
- **Capacity and exhaustion.** Size the pool to the **maximum concurrent** object count (peak resting orders, peak book depth) measured from captured data (Ch. 75). Decide the exhaustion policy deliberately: a hard cap that rejects (safest for determinism — never falls back to `malloc` on the hot path), or a cold-path grow (a new slab) accepting that growth is a non-hot-path event. **Never silently fall back to `malloc`** on the hot path — that reintroduces the tail you removed.
- **Threading.** A per-thread pool is lock-free (shared-nothing — Ch. 31) and the default for a pinned hot thread. A pool shared across threads (producer allocates, consumer frees) needs a lock-free free-list / a proper MPSC scheme (Ch. 34) — or, better, design so each thread owns its pool and objects are returned to their owner (or use a ring buffer, Ch. 37). Per-node pools for NUMA (Ch. 16).
- **Free-lists as the basis of other structures.** The free-list pool underlies intrusive containers (Ch. 25), the slot management of a ring buffer (Ch. 34, 37), and occupancy bitmaps (Ch. 28) — the pattern recurs throughout the concurrency and container chapters.

### 24.4.3 `std::pmr` memory resources

`std::pmr` is the bridge from custom allocators to **standard containers** without going viral on the type:

- **Why `pmr` over the classic `Allocator` template parameter.** A classic `std::vector<T, MyAlloc>` bakes the allocator into the *type* — viral, infectious across interfaces, and stateful allocators were painful pre-C++17. `std::pmr::vector<T>` holds a `memory_resource*` at *runtime*: same type regardless of resource, swap the resource freely, pass `pmr` containers across interfaces uniformly.
- **The standard resources.** `monotonic_buffer_resource` (arena — §24.4.1), `unsynchronized_pool_resource` / `synchronized_pool_resource` (pools, the latter thread-safe), `new_delete_resource` (default, wraps global `new`), and `null_memory_resource` — which makes *any* allocation throw, so you can wrap a hot section in a `null_memory_resource`-backed container to **prove zero allocation** at runtime (a powerful test, ties Ch. 23.5's "instrument `operator new`").
- **Stack- or slab-backed pmr.** Back a `monotonic_buffer_resource` with a `std::array` (stack) or a pre-faulted slab; chain an upstream resource for overflow. This is the clean way to give an STL algorithm scratch containers that don't touch `malloc`.
- **Custom `memory_resource`.** Derive from `std::pmr::memory_resource` (implement `do_allocate`/`do_deallocate`/`do_is_equal`) to expose your own arena/pool to the `pmr` ecosystem — your NUMA-aware or huge-page-backed allocator usable by any `pmr` container.
- **Caveats.** `pmr` adds one indirection (a virtual call to the resource per allocation — Ch. 14), negligible vs `malloc` but not vs a hand-inlined bump pointer; for the very hottest inner allocator a concrete (non-virtual) type may edge it out. And `pmr` containers must not outlive their resource (lifetime discipline — §24.5). For most hot-path container needs, `pmr` is the pragmatic, standard, low-risk way to get custom allocation.

## 24.5 Pitfalls & anti-patterns: fragmentation, lifetime bugs

- **Use-after-free across an arena reset.** The defining arena bug: an object (or a pointer/reference into the arena) outlives the `reset()` that freed its storage, so it now aliases a *different* object — silent corruption (Ch. 72). Enforce that nothing escapes the epoch; never hand out arena pointers that outlive the reset.
- **Fragmentation in a variable-size pool.** Pools are fragmentation-free *only* because slots are uniform. Allocating variable sizes from one pool (or a naive free-list) reintroduces fragmentation and search cost. Use one pool per size/type (or a slab allocator with size classes); use an arena for genuinely variable, all-at-once data.
- **Silent `malloc` fallback on exhaustion.** A pool that, when full, falls back to global `new` reintroduces exactly the tail (Ch. 23) you built the pool to avoid — and unpredictably. Decide the policy explicitly (reject, or cold-path grow); **never** fall back to `malloc` on the hot path. Size from measured peaks (Ch. 75).
- **Alignment mistakes (Ch. 9).** Returning under-aligned storage for an over-aligned type (SIMD, cache-line, `alignas`) is UB / a fault. Bump-align the cursor; align pool slots to `alignof(T)` (and to a cache line if false sharing matters — Ch. 33). Use `std::max_align_t`/the type's `alignof`.
- **Forgetting destructors.** Allocators manage *storage*; non-trivial objects need their destructor run before the slot/arena is reused (placement-`new` ⇒ explicit `~T()`). An arena `reset()` does **not** call destructors — leaking resources held by arena objects (file handles, etc.). Store trivially-destructible objects in raw arenas, or track destruction.
- **Storage-reuse aliasing (Ch. 21).** Reusing a slot for a *different* type, or accessing through a pointer obtained before re-construction, can need `std::launder` to be well-defined. Keep pools type-specific to sidestep most of this.
- **Sharing a per-thread allocator across threads.** A pool/arena built for single-thread use, touched by another thread, is a data race (Ch. 30–31). Keep allocators per-thread (lock-free, the default) or make them explicitly thread-safe (lock — Ch. 32, or lock-free free-list — Ch. 34); per-node for NUMA (Ch. 16).
- **Over-engineering / premature custom allocation.** Custom allocators add real complexity and lifetime risk. If a path isn't hot, or `reserve`d standard containers (Ch. 23, 25) already give zero steady-state allocation, you may not need a custom allocator. Match the tool to a *measured* allocation problem (§24.3), not reflexively.
- **Unwarmed slab.** Pre-allocating the arena/pool slab but not pre-faulting/`mlock`ing it (Ch. 23.4.2) means the first hot-path allocations page-fault — defeating the point. Warm the slab during startup.

## 24.6 Exercises & checklist

**Exercises**

1. **Measure the tail collapse.** Build `pool.cpp`; run `heap` vs `pool` pinned, capturing p50/p99/p99.9/max. Confirm the pool is flat (p99.9 ≈ p50) while heap's tail is ~20× its median. Add `perf stat -e page-faults` — which version faults?
2. **Arena per message.** Write a `monotonic_buffer_resource`-backed (or hand-rolled) arena; decode a message into arena-allocated temporaries, process, `reset()`. Measure ns/message and confirm (instrument `operator new`) zero `malloc` after warm-up. Size the arena for the worst-case message.
3. **Prove zero allocation.** Wrap a hot section's containers in `std::pmr` backed by `null_memory_resource`. Run the hot path — does it throw (i.e. did something allocate)? Fix until it doesn't. Compare to the `operator new` override approach (Ch. 23).
4. **Pool exhaustion policy.** Make the §24.3 pool reject on exhaustion vs cold-path grow vs (deliberately) fall back to `malloc`. Drive it past capacity and compare the latency distributions. Why is the `malloc` fallback the worst for the tail (§24.5)?
5. **Locality win.** Allocate 10k `Order`s via global `new` (scattered) vs a pool (contiguous), then traverse them summing a field. Compare `L1-dcache-load-misses`/`dtlb_load_misses` (Ch. 7, 15) and ns/traversal. Quantify the layout benefit, separate from the allocation benefit.

**Checklist — custom allocators**

- [ ] I matched the allocator to the **lifetime pattern**: **arena/bump** for all-at-once scratch, **pool/free-list** for same-size individual-lifetime objects.
- [ ] The slab is acquired **once at startup**, **pre-faulted + `mlock`ed** (Ch. 23.4.2), ideally huge-page (Ch. 15) and **per-NUMA-node** (Ch. 16) — the hot path never calls the kernel.
- [ ] Pools are **sized to measured peak** (Ch. 75) with an **explicit exhaustion policy** (reject / cold-path grow) — **never a silent `malloc` fallback**.
- [ ] Storage is correctly **aligned** for the type (Ch. 9); objects are **placement-constructed and explicitly destructed** (no leaked non-trivial destructors).
- [ ] Nothing **escapes an arena epoch** (no pointer/reference outlives `reset()`); storage-reuse aliasing uses `std::launder` where needed (Ch. 21).
- [ ] Allocators are **per-thread** (lock-free) or **explicitly thread-safe** (Ch. 32, 34) — no accidental cross-thread sharing of a single-thread allocator.
- [ ] I verified the win by the **distribution** (p99.9 flat — §24.3) and, where relevant, **locality counters** (Ch. 7, 15) — not the mean.
- [ ] I used `std::pmr` (incl. `null_memory_resource` to **prove zero allocation**) where standard containers suffice, and only hand-rolled where a measured need justified the complexity (§24.5).

## 24.7 References

- ISO C++ / cppreference — `<memory_resource>`: `std::pmr::memory_resource`, `monotonic_buffer_resource`, `(un)synchronized_pool_resource`, `null_memory_resource`, and `polymorphic_allocator`; the allocator requirements and placement-`new`/lifetime rules (§24.2, §24.4.3).
- A. Alexandrescu, *Modern C++ Design* (small-object allocators) and the classic *Loki* allocator — the pool/fixed-size-allocator design behind §24.4.2.
- J. Bonwick, *"The Slab Allocator"* (USENIX) — the slab/object-caching strategy generalized from kernels (§24.2).
- The jemalloc/tcmalloc design docs — arena and size-class concepts that custom allocators specialize; useful contrast for when a general allocator suffices off the hot path (Ch. 23).
- The Linux man pages — `mmap(2)`/`mlock(2)` for backing and pinning an allocator's slab (§24.2, ties Ch. 26).

## 24.8 Additional Reading

- CppCon talks on `std::pmr` (e.g. Pablo Halpern, Jason Turner, Arthur O'Dwyer) — practical polymorphic-allocator usage, stack-backed arenas, and proving no-allocation with `null_memory_resource`.
- The EASTL and Abseil allocator/container documentation — production custom-allocator and container patterns for low-latency/games.
- Ch. 25 (*Cache-Friendly Containers*) — intrusive containers and `pmr` containers, and the order-book allocator case study; Ch. 26 (*Memory Mapping*) — backing slabs with `mmap`/huge pages/`mlock`; Ch. 34 (*Lock-Free Structures*) — lock-free free-lists/ring buffers; Ch. 23 (*Memory Fundamentals*) — the allocation tail this chapter removes; Ch. 72 (*Secure Programming*) — UAF/lifetime bugs in arena designs.
- **Appendix E** — the allocation latency numbers contrasting `malloc` with a pool pop.

---

*Next: Ch. 25 — Hot-Path-Hostile STL & Cache-Friendly Containers, where allocators meet containers: the real costs of `std::unordered_map`/`std::map`/`std::function`/`shared_ptr`, the flat and intrusive alternatives, and a case study building the limit order book.*
