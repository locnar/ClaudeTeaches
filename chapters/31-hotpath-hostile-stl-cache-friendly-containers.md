# Part IV — Memory Management

# Chapter 31 — Hot-Path-Hostile STL & Cache-Friendly Containers

> **Prerequisites:** Ch. 8–9 (caches & data-oriented design — the locality the whole chapter is about), Ch. 29–30 (allocation cost & custom allocators — node containers allocate per element; the order book runs on a pool), Ch. 15 (`std::function`/type erasure), Ch. 16 (TLB — pointer-chasing thrashes it), Ch. 3 (measuring the distribution).
>
> **Leads into:** Ch. 42/45 (lock-free queues / Disruptor — ring buffers as containers), Ch. 67 (feed handlers feeding the book), Ch. 35 (bitmaps for level occupancy), Ch. 95 (hot-reload of reference data). The order-book case study (§31.4.4) is referenced throughout Parts VIII-X.

---

## 31.1 Why it matters: node-based containers chase pointers

The C++ standard library is superb general-purpose engineering and, for several of its most-used containers, **actively hostile to the hot path** — not because the implementations are bad, but because their *data structures* are wrong for a latency-bound, cache-sensitive workload. `std::map`, `std::unordered_map`, `std::list`, `std::set` are **node-based**: each element lives in a separately-allocated node somewhere on the heap, linked by pointers. Every lookup, every traversal, is a **pointer chase** — follow a pointer to a node that's almost certainly a cache miss (Ch. 8, ~80 ns to DRAM) and probably a TLB miss too (Ch. 16), then follow the next. A `std::map` lookup is O(log n) *cache misses*; a `std::unordered_map` lookup is a hash plus a walk down a chain of heap-scattered nodes. The asymptotic complexity looks fine; the *constant* is a string of DRAM round-trips, and on the hot path the constant is everything.

The damage compounds with the allocation story of Ch. 29–30: every `insert` into a node container is a `malloc` (a tail event, §29.3), every `erase` a `free`, and the nodes fragment across the heap so even sequential logical access is physically random. Add the other idiomatic-but-costly types — `std::shared_ptr` (atomic refcount increments/decrements on every copy, a cache-line ping-pong under contention — Ch. 41; plus a separate control-block allocation), `std::function` (double indirection, possible heap capture — Ch. 15) — and a "modern C++" hot path can be a parade of cache misses, atomic contention, and hidden allocations, none visible as a literal performance bug in the source.

The cure is **data-oriented container choice** (Ch. 9): prefer **flat, contiguous** structures whose elements sit next to each other in memory, so access is sequential and prefetchable (Ch. 11) and traversal stays in cache. `std::vector` over `std::list`; **open-addressing / flat hash maps** over `std::unordered_map`; `std::flat_map`/`std::flat_set` (C++23) over `std::map`/`std::set`; **intrusive** containers (the link lives *in* the object, no node allocation) and **small-buffer** types over heap-owning ones; concrete callables over `std::function`. This chapter measures the gap (§31.3), lays out the alternatives (§31.4), and applies them to the canonical HFT data structure — the **limit order book** (§31.4.4) — where the right container choices are the difference between a microsecond and a hundred nanoseconds per update. The rule throughout: choose the container for the **access pattern**, and measure the *distribution* (Ch. 1), because the node-container penalty lives in the cache-miss tail.

## 31.2 Mental model: costs of `unordered_map`/`map`/`function`/`shared_ptr`

A quick, honest cost model of the four most common hot-path-hostile types, in cache/allocation terms:

**`std::map` / `std::set` (red-black tree, node-based).** O(log n) — but each step follows a pointer to a *separately heap-allocated* node, so a lookup is ~log₂(n) **cache misses** (Ch. 8) and likely TLB misses (Ch. 16). Insert/erase = `malloc`/`free` per element (Ch. 29) plus tree rebalancing. Ordered iteration chases pointers in (logical) order over physically-scattered nodes. *Sorted* and *stable-address* are its only real virtues; for hot lookups it's slow.

**`std::unordered_map` / `std::unordered_set` (chained hash, node-based).** The standard *mandates* an interface (bucket iterators, reference stability, node handles) that forces a **chaining** implementation: buckets hold pointers to singly-linked **node** chains, each node separately allocated. So a lookup is: hash → bucket (one miss) → walk a chain of heap-scattered nodes (a miss *per node*). Insert = `malloc` a node; high load factor or bad hashing = long chains. It's far slower than a good flat hash map *by construction* — the standard's interface guarantees prevent the cache-friendly layout. The most over-used hot-path-hostile container.

**`std::shared_ptr` (shared ownership via atomic refcount).** Each copy **atomically increments** a refcount; each destruction atomically decrements (Ch. 38). Under multi-thread sharing, that refcount cache line **ping-pongs** between cores (false-sharing-like coherence traffic — Ch. 41) — a serializing cost wildly out of proportion to "copying a pointer." Plus a control-block allocation (unless `make_shared` fuses it), and the refcount itself is a cache line touched on every copy. On the hot path, `shared_ptr` *copies* are a hidden atomic-contention and allocation cost; pass by `const&`/raw pointer/`unique_ptr` and reserve `shared_ptr` for genuine shared ownership off the hot path.

**`std::function` (type-erased callable).** Double indirection, no inlining, possible heap allocation on construction (Ch. 15). Covered there; listed here because a `std::function` *stored in a container* (a `vector<std::function>`, a `map` of callbacks) multiplies both costs.

The unifying model, the through-line of Ch. 8–9 applied to containers:

```
   node-based (map/unordered_map/list):   elem ─►node─►node─►node     (a cache miss per hop)
                                          scattered across the heap; lookup = chain of DRAM trips

   flat/contiguous (vector/flat_map/flat hash):  [elem|elem|elem|elem|elem]   one region,
                                          sequential, prefetchable, cache- & TLB-resident
```

**The constant factor — cache misses per operation — dominates the hot path, and it's set by whether the container is contiguous or pointer-linked.** Choose flat unless you have a specific reason (stable addresses, ordered + huge + insert-heavy) not to.

## 31.3 Measure it: `std::unordered_map` vs flat hashing

Look up symbols (or order IDs) in a map — the bread-and-butter feed-handler operation — comparing `std::unordered_map` against a flat open-addressing map (here, a stand-in for `absl::flat_hash_map`/`ankerl::unordered_dense`; the point is the *layout*). Same keys, same queries; only the container's memory structure differs.

```cpp
// flatmap.cpp — chained std::unordered_map vs a flat open-addressing map.
// Build: g++ -O2 -std=c++20 -march=native flatmap.cpp -o flatmap
// Run pinned:  taskset -c 2 ./flatmap chained   |   ./flatmap flat
#include <bit>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <unordered_map>
#include <vector>
#include <random>
#include <chrono>

// A minimal flat open-addressing map (linear probing) — contiguous storage, no node allocation.
struct FlatMap {
    struct Slot { std::uint64_t key; std::uint64_t val; bool used; };
    std::vector<Slot> t;
    int shift;                                   // 64 - log2(cap): take the HIGH bits of the hash
    explicit FlatMap(std::size_t cap) : t(cap), shift(64 - std::countr_zero(cap)) {}  // cap: power of 2
    // Fibonacci hashing: the multiply mixes UPWARD, so mask-the-low-bits would discard the mixing
    // (keys stepping by 4, 16, 64... would collide class-wide); shifting keeps the mixed high bits.
    void put(std::uint64_t k, std::uint64_t v) {
        std::size_t i = (k * 0x9E3779B97F4A7C15ull) >> shift;
        while (t[i].used && t[i].key != k) i = (i + 1) & (t.size() - 1);  // probe contiguously
        t[i] = {k, v, true};
    }
    std::uint64_t get(std::uint64_t k) const {
        std::size_t i = (k * 0x9E3779B97F4A7C15ull) >> shift;
        while (t[i].used) { if (t[i].key == k) return t[i].val; i = (i + 1) & (t.size() - 1); }
        return 0;
    }
};

int main(int argc, char** argv) {
    bool flat = (argc > 1) && std::strcmp(argv[1], "flat") == 0;
    constexpr std::size_t N = 1u << 16, CAP = 1u << 17;     // 65k keys, half-full
    std::mt19937_64 rng(1);
    std::vector<std::uint64_t> keys(N), probe(4'000'000);
    for (auto& k : keys) k = rng();
    for (auto& q : probe) q = keys[rng() % N];             // queries hit existing keys

    std::unordered_map<std::uint64_t, std::uint64_t> um; um.reserve(CAP);
    FlatMap fm(CAP);
    for (std::size_t i = 0; i < N; ++i) { um[keys[i]] = i; fm.put(keys[i], i); }

    auto t0 = std::chrono::steady_clock::now();
    std::uint64_t sink = 0;
    if (flat) for (auto q : probe) sink += fm.get(q);
    else      for (auto q : probe) sink += um.find(q)->second;
    auto t1 = std::chrono::steady_clock::now();
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
    std::printf("%-8s sink=%llu  %.2f ns/lookup\n", flat?"flat":"chained",
                (unsigned long long)sink, ns/probe.size());
    return 0;
}
```

Representative results (toolchain and machine: Appendix L.3) — reference machine **Xeon Gold 6326** (Ice Lake-SP), `-O2 -march=native`, pinned, turbo off (illustrative; the *ratio* is the point):

```
                          chained (unordered_map)    flat (open-addressing)
ns / lookup                    ~25 ns                    ~8 ns         <- ~3x faster (L3-resident: N = 2^16)
L1-dcache-load-misses          high (node chase)         low (contiguous probe)
dTLB-load-misses               elevated (scattered)      low
allocations (build)            ~N mallocs (one/node)     1 (the vector)
```

Read it the Ch. 8 way: **the flat map is ~3× faster on the identical workload**, and the counters say why — at N = 2¹⁶ both tables fit the 24 MiB L3, so `std::unordered_map`'s lookup is two to three *dependent* L3 hits (bucket array → node → key, each a scattered line) while the flat map's probe is one L3 hit on a contiguous slot that prefetches (Ch. 11). Size past L3 (N = 2²², exercise 1) and every one of those dependent hops becomes a DRAM round-trip plus, likely, a TLB miss: the gap widens toward ~3–4× and the absolute numbers move to ~100 ns vs ~30 ns. The build phase also did ~N `malloc`s for the chained map vs *one* allocation for the flat map's vector (Ch. 29). The win grows with key count (more misses to chase) and shrinks if everything fits L1. This is the canonical container measurement: **node-based vs flat is a cache-miss-count difference**, and §31.4 is how you get the flat side. (Production: reach for `absl::flat_hash_map`, `ankerl::unordered_dense`, or `boost::unordered_flat_map` — robin-hood/swiss-table maps that beat this toy and `std::unordered_map` substantially.)

**Row 3 — handle dereference: slot map vs flat hash vs raw pointer (§31.4.4).** The same question for the *order table*: given an identifier for an order, how fast is the object reached, and what does the stale-handle check cost? 2 M lookups, uniformly random over the live orders, at two working-set sizes (GCC 13.3 `-O2 -std=c++20 -march=native`, pinned, on the 4-vCPU VM of Appendix L.2 — TSC 2.70 GHz; VM-measured, the ratios transfer):

```
                                        10 k-order book (12 288 live, 1.5 MiB)   1.5 M-order table (>100 MiB, cold)
raw pointer dereference                          0.8 ns                                   13.6 ns
slot map, generation-checked                     1.4 ns                                   21.1 ns      ← +0.3–0.6 ns for the compare; the rest is the sparse[] line
slot map, unchecked index                        1.1 ns                                   17.8 ns
flat open-addressing map (u64 id → index)        5.0 ns                                   36.1 ns      ← hash + probe + key compare, then the same value load
slot map, 10 % of handles stale (mixed)          0.9 ns  (199 596 of 2 M rejected)        10.8 ns  (199 843 rejected)   ← a stale handle costs ONE compare and no value load
slot map insert + erase (erase-by-swap)          5.0 ns per pair                            5.0 ns per pair
```

The slot map is within 2× of a raw pointer cold and within 0.6 ns hot, 3.5× faster than the flat hash that would otherwise front the order table, and the generation check — the thing that makes a reused slot *safe* — is a fraction of a nanosecond. The cold column is memory latency: the handle costs one extra line (`sparse[]`) over the pointer, which is the price of relocatability and validation.

## 31.4 Techniques

### 31.4.1 Flat / open-addressing & robin-hood hashing

Replace chained hashing with **open addressing**: store entries *in* a contiguous array; on collision, probe to another slot in the *same* array rather than chasing a heap node.

- **Why it's faster (§31.3).** All probing happens within a contiguous table — sequential, prefetchable (Ch. 11), cache- and TLB-resident — and there are *no per-element allocations* (one array, sized up front). A lookup is a hash plus a short contiguous probe, not a chain of DRAM trips.
- **Probing schemes.** Linear probing (simple, cache-friendly, sensitive to clustering); **robin-hood** hashing (bounds probe-length variance by displacing richer entries — excellent worst-case, the basis of `ankerl::unordered_dense`); **SwissTable**-style SIMD-group probing (Abseil's `flat_hash_map`, Boost.Unordered's `unordered_flat_map`, folly's F14: a contiguous metadata-byte array scanned 16 slots at a time with SSE2/NEON — Ch. 36 — for very fast probing). All keep data contiguous.
- **Load factor and sizing.** Open addressing degrades as it fills (longer probes); keep load factor moderate (~0.5-0.875 depending on scheme) and **`reserve` to the working set** up front (Ch. 29) so the hot path never rehashes (a rehash = a big reallocation + reinsert, a latency spike — keep it on the cold path).
- **Trade-offs.** No reference/pointer stability (entries move on rehash/displacement — don't hold pointers into the table; use indices or an intrusive design if you need stable identity). Erase needs tombstones or backward-shift. For HFT lookups (symbol→info, orderID→order) the speed wins overwhelmingly; use a production library, not a hand-roll, for the corner cases.

### 31.4.2 `std::flat_map`/`std::flat_set` (C++23)

For *ordered* associative needs, `std::flat_map`/`std::flat_set` (C++23 — shipped in libstdc++ 15 / GCC 15 and libc++ 20 (`flat_map`) / 21 (`flat_set`), so on the book's GCC 13 / Clang 18 baseline you use the fallback below; gate on `__cpp_lib_flat_map`/`__cpp_lib_flat_set >= 202207L`, see Appendix H) are the cache-friendly replacements for `std::map`/`std::set`: they're **sorted contiguous container adaptors** rather than node trees — `std::flat_map` keeps keys and mapped values in two *parallel* sorted sequences (`KeyContainer`/`MappedContainer`, `std::vector` by default: the SoA layout of Ch. 9, so a binary search touches only the dense key array), `std::flat_set` one sorted sequence. (Boost.Container's `flat_map`, by contrast, is a single sorted vector of pairs.)

- **Why faster for lookups/iteration.** Lookup is a **binary search over a contiguous array** — O(log n) but with *cache-friendly* access (Ch. 8) and no pointer chasing, and ordered iteration is a *linear scan* of contiguous memory (maximally prefetchable) instead of a tree walk over scattered nodes. Far better locality than `std::map` for read/scan-heavy use.
- **The trade-off: insert/erase cost.** Inserting into the middle shifts elements (O(n) move), so `flat_map` shines when the container is **lookup/iteration-heavy and mutation-light**, or built once and queried many times (reference data, a static symbol table, a config map). For insert-heavy ordered data, the shift cost can outweigh the locality win — measure.
- **HFT fits.** Reference/static data (symbol metadata, tick-size bands — Ch. 34, venue config), and — carefully — order-book price levels where the active range is small and contiguous (§31.4.4). C++17/20 fallback: a hand-maintained sorted `std::vector` with `std::lower_bound` (exactly what `flat_map` standardizes), or Boost.Container's `flat_map`.
- **Customizable underlying storage.** `flat_map` can use a `std::vector` (or `std::pmr::vector` — Ch. 30, or a small-vector), so you can back it with a custom allocator / stack buffer for zero hot-path allocation.

### 31.4.3 Intrusive, fixed-capacity and small-buffer containers

Two more ways to eliminate node allocation and indirection:

- **Intrusive containers.** The link/hook lives **inside the object**, not in a separate node: the object has a `next`/`prev` (or tree hook) member, and the container threads objects directly. Consequences: **no per-element allocation** (the object *is* the node — pair with a pool, Ch. 30), **stable addresses**, an object can be in multiple intrusive lists at once, and removal is O(1) given the object (no lookup). Boost.Intrusive provides `list`/`slist`/`set`/`unordered_set` hooks; HFT code often hand-rolls intrusive lists for order chains. The order book's price-level FIFO is the canonical use (§31.4.5): orders are pool-allocated and intrusively linked, so adding/cancelling an order is pointer surgery with zero allocation. The trade-off: the object carries the hooks (size, coupling) and you own lifetime (Ch. 30, 94).
- **Small-buffer optimization (SBO) / small-vector types.** Store small payloads **inline** (no heap) and only spill to the heap when they grow: `std::string`'s SBO, and `boost::small_vector`/`absl::InlinedVector`/`llvm::SmallVector` for "usually ≤ N elements" sequences. On the hot path, a small-vector sized to the common case is allocation-free for the common case and contiguous always. Know the SBO threshold and keep hot payloads under it: libstdc++'s `std::string` keeps 15 chars inline (libc++: 22); libstdc++'s `std::function` stores a callable inline only up to 16 bytes / pointer alignment (`sizeof` 32); and a small-vector's `N` is whatever you chose — check each with a `static_assert` or a one-off `sizeof` print, not folklore.
- **Fixed-capacity sequences: `std::inplace_vector<T, N>` (C++26, P0843) and today's equivalents.** The `vector` interface over storage that lives *inside the object*: `N` elements inline, no allocator, no heap, `constexpr`-capable, trivially copyable when `T` is — so `inplace_vector<Level, 8>` is a per-message batch of levels that passes through a ring slot or a shared-memory segment by `memcpy` (Ch. 64) and never calls `malloc`. Availability (Appendix H): libstdc++ 15 (`__cpp_lib_inplace_vector`; absent in 13/14), not yet in libc++ 18; `boost::container::static_vector<T, N>` and `etl::vector<T, N>` are the shipping spellings with the same shape. The hot-path cost is the **capacity branch**: `push_back` compares `size()` against `N` and throws `bad_alloc` when full — a predicted compare per insert, plus a cold throw path (Ch. 23). Where the bound is already proved (a loop over at most `N` wire entries after a length check), `unchecked_push_back`/`unchecked_emplace_back` skip it, and `try_push_back` returns a pointer-or-null for the §23.4.2 value-channel style. Choose `N` from the venue's documented maximum (SBE group counts, ITCH's fixed message set), not from a guess, and `static_assert(sizeof(inplace_vector<T, N>) <= 64)` when it must stay in one cache line (Ch. 10). The stable-slot cousins (`plf::colony`/`std::hive`) and slot maps are §31.4.4.
- **Index handles over pointers.** Storing **indices** into a contiguous arena/vector instead of pointers makes structures relocatable (rehash/grow doesn't invalidate), shrinks the handle (a 32-bit index vs 64-bit pointer — Ch. 10), and keeps everything in one cache-friendly region. The worked form of the idiom — a slot map with generational handles — is §31.4.4.

### 31.4.4 Slot maps and generational handles

The "index handles over pointers" bullet of §31.4.3 is an idiom; this is the structure that makes it safe. A **slot map** gives every object a stable 64-bit **handle** that is cheap to dereference (an index), cheap to *validate* (one compare), survives the container's reallocation and erasure of its neighbours, and is meaningful across processes (it is an integer, not an address). It is the right container for the three tables a trading system keeps in flight — the order table (orderID → order; the LOB's in §31.4.5, the gateway's in Ch. 98), the armed timers (Ch. 52 §52.4.1), and any object that something else holds a reference to without owning it.

```
   handle (64 bits) = [ generation : 32 ][ index : 32 ]           sparse[]                 dense[]  (values, contiguous: iteration = a linear scan)
                                                                  ┌─────────────────────┐  ┌──────────────────────┐
   get(h):  s = sparse[h.index]                                   │ dense_idx  gen  next │  │ Order … back=sparse i │
            s.gen == h.generation ? &dense[s.dense_idx] : null    │ dense_idx  gen  next │  │ Order … back          │   back: which sparse slot owns this value
            ─ one load, one compare, one indexed load             │  (free)    gen  next ├─►│ …                    │   (so erase-by-swap can fix the mover's slot)
   insert:  pop free list → sparse slot s; dense.push(value);     └─────────────────────┘  └──────────────────────┘
            s.dense_idx = size-1; return {s.gen, s}                 free list threads the free sparse slots through `next`
   erase(h): validate; d = s.dense_idx; dense[d] = dense[last]; sparse[dense[d].back].dense_idx = d; --size;      ← erase-by-swap: values stay contiguous
            s.gen += 1 (or 2); push s on the free list              ← the old handle is now INVALID: its generation no longer matches, forever
```

```cpp
// slotmap.hpp — dense values + sparse indirection + free list + generation. Build with the §31.3 harness: g++ -O2 -std=c++20 -march=native slotmap.cpp -o slotmap
struct Handle { std::uint64_t v; static Handle make(std::uint32_t idx, std::uint32_t gen) { return {std::uint64_t(gen) << 32 | idx}; }
                std::uint32_t idx() const { return std::uint32_t(v); } std::uint32_t gen() const { return std::uint32_t(v >> 32); } };   // Ch. 35 §35.4.4: two shifts
template <class T, std::uint32_t CAP> struct SlotMap {                      // CAP a power of two: the index mask is the bounds check (Ch. 35 §35.4.3)
    struct Slot { std::uint32_t dense, gen, next_free; };
    Slot sparse[CAP]; T dense[CAP]; std::uint32_t size = 0, free_head = 0;
    SlotMap() { for (std::uint32_t i = 0; i < CAP; ++i) sparse[i] = {0, 1, i + 1}; }   // gen starts at 1: a zero handle is never valid
    Handle insert(const T& t) { std::uint32_t s = free_head; Slot& sl = sparse[s]; free_head = sl.next_free; sl.dense = size; dense[size] = t; dense[size].back = s; ++size; return Handle::make(s, sl.gen); }
    T* get(Handle h) { Slot& sl = sparse[h.idx() & (CAP - 1)]; return sl.gen == h.gen() ? &dense[sl.dense] : nullptr; }   // ONE predictable compare (Ch. 14): stale → nullptr
    void erase(Handle h) { Slot& sl = sparse[h.idx() & (CAP - 1)]; if (sl.gen != h.gen()) return;                            // stale erase is a no-op, not a corruption
        std::uint32_t d = sl.dense; --size; if (d != size) { dense[d] = dense[size]; sparse[dense[d].back].dense = d; }        // erase-by-swap keeps values contiguous
        sl.gen += 2; sl.next_free = free_head; free_head = h.idx(); }                                                        // += 2 keeps gen odd... or += 1; any bump kills the old handle
};
```

- **Validation is one compare, and it is the point.** A pointer to a freed slot is a use-after-free the allocator cannot catch (Ch. 30, 94); a handle to a freed slot fails `gen == h.gen()` and yields `nullptr` — deterministically, every time, for the life of the process (a 32-bit generation wraps after 4 billion reuses of *one* slot). The branch predicts "valid" on the hot path and the stale case is the slow path; §31.3's measurement shows the check costing ~0.3 ns over the unchecked index.
- **Erase-by-swap vs stable slots.** Moving the last value into the hole keeps `dense[]` contiguous — iteration over all live orders is a linear scan (Ch. 9), which the kill-switch (Ch. 97) and a snapshot writer (Ch. 101) want. The cost: a value's *address* changes on someone else's erase, so **nothing may hold a `T*` across an erase** — hold the `Handle`. When values carry intrusive links to each other (the LOB's per-level FIFO — §31.4.5), use the **stable-slot variant**: values live at `dense[index]` directly, no swap, iteration skips free slots (or walks an intrusive live list); Ch. 98's in-flight table is that variant.
- **Relative handles for shared memory** (Ch. 64 §64.4.2): the handle is an integer, so a process that maps the segment at a different address dereferences it the same way; the segment holds `sparse[]`, `dense[]` and the free list as offsets from the segment base, never pointers — and the generation check guards a *reader* process against a slot the *writer* process recycled between the reader's lookup and its use.
- **Debug-mode poisoning**: under `#ifndef NDEBUG`, `erase` fills the vacated value with `0xDD` and `get` on a stale handle records `(handle, caller)` before returning `nullptr`; the Ch. 46 §46.4.5 outstanding-view counter is the same discipline for ring slots.
- **Codegen check** (Ch. 4): `get()` must be a shift, a masked index, a compare and one indexed load — Clang 18.1 at `-O2` emits `movzwl`/`and`, two `lea`s for the 12-byte stride, `shr $32`, `cmp`, `jne`, then the dense index load; no call, no bounds branch beyond the compare. If a `div` or a second compare appears, `CAP` was not a power of two or the generation was not in the high word.
- **Cousins, in one sentence**: stable-slot vectors (`plf::colony`/C++26 `std::hive` — insertion-order-free, erase-stable, skipfield iteration), sparse sets (the same two arrays without generations, for *sets* of small integers) and `std::inplace_vector` (C++26, Appendix H — fixed-capacity contiguous storage) occupy the same design space with different trade-offs on iteration cost, stability and validation; their full treatment is a separate item and is not written here.

### 31.4.5 Case Study — Building the Limit Order Book

The **limit order book (LOB)** is the central HFT data structure, and it's a perfect crucible for this chapter: a feed handler updates it on *every* market-data message (Ch. 67), and the budget is tens of nanoseconds. The naive STL implementation — `std::map<Price, std::list<Order>>` — is a worst-case of everything above: a tree of nodes (log-n cache misses to find a price level) whose values are heap-linked lists of nodes (a `malloc` per order, a cache miss per order in the queue). It "works" and it's an order of magnitude too slow. The fast design exploits the LOB's structure:

- **Price levels: a flat array indexed by ticks, not a tree.** Prices are discrete (tick-sized — Ch. 34) and the *active* range near the touch is small and contiguous. Represent each side as a **flat array of price levels indexed by (price − base)/tick** — O(1) access to any level, contiguous, prefetchable, no tree, no per-level allocation. The book is a window over the active ticks (a ring/sliding array, or a dense array bounded by the instrument's plausible range), with the **best bid/offer (BBO)** tracked as the current top index. Deep, rarely-touched levels can live in a secondary (sparse/`flat_map`) structure — the hot/cold split of Ch. 1 applied to depth.

  ```
   bids[]  indexed by tick:  [ ... | L | L | L | BBO ] <- top-of-book at the active end
   asks[]                    [ BBO | L | L | L | ... ]   each L is a price-level FIFO (below)
   O(1) to any level by price; BBO is just an index; deep book = cold secondary store
  ```

- **Orders at a level: an intrusive FIFO from a pool, not `std::list`.** Each price level is a **FIFO queue of resting orders** (price-time priority). Implement it as an **intrusive doubly-linked list** of `Order` objects drawn from an **object pool** (Ch. 30): adding an order is pop-from-pool + link-at-tail (a few instructions, no allocation); cancelling is unlink-given-the-order (O(1), no search); a fill walks from the head. **The order table is a stable-slot slot map (§31.4.4)**: the venue's orderID maps — through one flat hash (§31.4.1) for venues whose ids are opaque, or directly when the id is dense — to a `Handle` whose index addresses the pooled `Order` and whose generation rejects a cancel or execute for an order already removed (a late message after a replay, a reused slot): §31.3's row 3 puts the handle dereference at ~1.4 ns against ~5 ns for the flat hash alone, and the stale check is what makes the pool's reuse safe. Orders carry their intrusive level links *in* the slot, so the stable-slot variant (no erase-by-swap) is the one used here. Zero allocation on the hot path, O(1) add/cancel/match, and each order's neighbors are pool-contiguous (locality).
- **BBO vs deep-book asymmetry (the hot/cold split).** The overwhelming majority of activity is at or near the touch — BBO updates, adds/cancels in the top few levels — which the flat-array-near-the-top design makes O(1) and cache-hot. Deep-book modifications are rarer and can afford a slower path (the sparse secondary store). Optimize the structure for the *common* operation (top-of-book), not the worst case (arbitrary-depth insert) — and measure against captured data (Ch. 101) where the real distribution of touched levels lives.
- **Consolidating disparate feeds.** A consolidated book merges multiple venues'/feeds' updates (Ch. 67's A/B arbitration and multi-feed handling). Keep each feed's contribution attributable (so a feed drop/gap — Ch. 67 — can be backed out) while presenting one merged view; per-venue level arrays summed into a consolidated array, or per-order venue tags, with the same flat/intrusive/pool discipline so the merge stays allocation-free and cache-friendly.
- **What this buys.** The flat-array + intrusive-pool LOB turns per-message book updates from a parade of cache misses and `malloc`s (the `map<Price,list>` version) into a handful of contiguous, allocation-free operations — the difference between ~hundreds of ns and ~tens of ns per update, and a *flat* tail (Ch. 1) because there's no allocation or tree-rebalance to spike. It's the synthesis of the whole book so far: data-oriented layout (Ch. 9), no hot-path allocation (Ch. 29–30), flat containers (this chapter), cache/TLB locality (Ch. 8, 16).

## 31.5 Pitfalls & anti-patterns: `shared_ptr` refcount churn

- **`std::map`/`std::unordered_map` on the hot path.** The default reach — and a parade of cache misses + per-element `malloc` (§31.2–31.3). Use a flat/open-addressing map (§31.4.1) or `flat_map` (§31.4.2); reserve the rare justified `std::map` use (need stable addresses *and* ordered *and* insert-heavy) and know you're paying for it.
- **`shared_ptr` copies on the hot path.** Every copy is an **atomic** refcount bump; shared across cores, the refcount line ping-pongs (Ch. 41) — a serializing cost dwarfing the "pointer copy" it looks like. Pass by `const&`/raw/`unique_ptr`; only copy `shared_ptr` when transferring genuine shared ownership, off the hot path. `make_shared` to fuse the control-block allocation when you do use it.
- **Slot-map misuse (§31.4.4).** *Skipping the generation check on a "trusted" path*: the one caller that dereferences by index alone is the one that books a fill against a reused slot. *Holding a `T*` across an erase* in the erase-by-swap variant: the value moved; hold the handle. *Generation wrap*: a 16-bit generation on a hot slot wraps in minutes at 10⁵ reuses/s — use 32 bits (4 billion reuses per slot). *Crossing processes without the generation* (an index alone into a shared segment — Ch. 64) loses exactly the protection the reader needed. *Starting generations at zero* makes `Handle{0}` valid; start at 1.
- **Holding pointers into a flat/rehashing container.** Open-addressing and `vector`-backed containers **move elements** on rehash/grow/insert; a stored pointer/iterator dangles. Use **indices/handles** (§31.4.3) or reserve enough to never rehash on the hot path. (This is the trade for the locality win — design for it.)
- **Not `reserve`-ing → hot-path rehash/realloc.** A flat map or vector that rehashes/reallocates mid-stream is a latency spike (a big alloc + reinsert/move — Ch. 29). Size to the working set up front; keep growth on the cold path.
- **`vector<std::function>` / containers of type-erased callables.** Multiplies `std::function`'s double-indirection and possible allocation (Ch. 15) across every element. Use a concrete callable type, a `variant`, or function pointers (Ch. 15).
- **Intrusive without lifetime discipline.** Intrusive containers + pools mean *you* own lifetimes; an order freed to the pool while still linked, or linked into two lists with conflicting lifetimes, is UAF/corruption (Ch. 30, 94). Keep the ownership rules simple and explicit.
- **Choosing `flat_map` for insert-heavy data.** `flat_map`'s middle-insert is O(n) shifts (§31.4.2); for insert/erase-heavy *ordered* data it can lose to a tree. Match it to lookup/scan-heavy, mutation-light use — and measure.
- **Benchmarking with everything in L1.** A container comparison on a tiny dataset hides the cache-miss difference that *is* the point (§31.3). Test at the real working-set size (and with realistic, cache-cold access — Ch. 3), or you'll conclude node containers are "fine."
- **Ignoring the order book's access distribution.** Optimizing the LOB for worst-case arbitrary-depth ops instead of the real, top-of-book-dominated distribution (§31.4.4) wastes effort and may pessimize the common path. Profile against captured market data (Ch. 101).

## 31.6 Exercises & checklist

**Exercises**

1. **Flat vs chained.** Build `flatmap.cpp`; run `chained` vs `flat` pinned with `perf stat -e L1-dcache-load-misses,dTLB-load-misses` (or `dtlb_load_misses.walk_completed`, as Ch. 16 uses). Confirm the flat map's lower misses and ~3-4× speed. Sweep N from L1-sized to >L3 — how does the gap grow with working-set size?
2. **Library shootout.** Replace the toy flat map with `absl::flat_hash_map`, `ankerl::unordered_dense`, and `boost::unordered_flat_map`; compare against `std::unordered_map` on the same workload. Quantify the production gap.
3. **`shared_ptr` churn.** Hammer a `shared_ptr` copied/destroyed across two threads on different cores; measure throughput and the HITM counters — `mem_load_l3_hit_retired.xsnp_fwd` (older alias `xsnp_hitm`) for two cores on one socket, `mem_load_l3_miss_retired.remote_hitm` only across sockets (Ch. 17), or simply `perf c2c` (Ch. 41, *False Sharing*) vs passing `const&`. How expensive is the "free pointer copy"?
4. **Build the LOB core.** Implement the §31.4.4 book: flat tick-indexed level arrays + intrusive pool-backed FIFOs + a flat `orderID→Order` map. Feed it captured (or synthetic) ITCH-like adds/cancels/executes; measure ns/update distribution and confirm zero hot-path allocation (`operator new` override — Ch. 29). Compare to a `std::map<Price,std::list<Order>>` baseline.
5. **`flat_map` crossover.** For an ordered map, find the insert-rate at which `std::flat_map` loses to `std::map` (shift cost vs locality). Where does each win (§31.4.2)?

**Checklist — cache-friendly containers**

- [ ] Hot-path maps/sets are **flat/open-addressing** (§31.4.1) or **`flat_map`/`flat_set`** (§31.4.2), not `std::map`/`std::unordered_map`; I justified any node container deliberately.
- [ ] Containers are **`reserve`d to the working set**; the hot path never **rehashes/reallocates** (Ch. 29); I hold **indices/handles**, not pointers, into relocating containers.
- [ ] **No `shared_ptr` copies** on the hot path (pass `const&`/raw/`unique_ptr`); `shared_ptr` is reserved for genuine off-hot-path shared ownership (`make_shared`).
- [ ] Per-element node allocation is eliminated via **intrusive containers + pools** (Ch. 30) and/or **small-buffer** types where payloads are usually small (§31.4.3).
- [ ] The **order book** uses flat tick-indexed level arrays + intrusive pool-backed FIFOs + a flat ID map, optimized for the **top-of-book-dominated** access distribution, with deep book as a cold secondary store (§31.4.4).
- [ ] **No `vector<std::function>`** / containers of type-erased callables on the hot path (Ch. 15).
- [ ] Intrusive/pool designs have **explicit, simple lifetime rules** (no linked-while-freed — Ch. 30, 94).
- [ ] Container choices are validated by **cache/TLB counters and the latency distribution** at the **real working-set size** against **captured data** (Ch. 3, 8, 101) — not a tiny in-L1 microbench.

## 31.7 References

- ISO C++ / cppreference — the complexity and interface guarantees of `std::map`/`std::unordered_map`/`std::list`/`std::shared_ptr`, and `std::flat_map`/`std::flat_set` (C++23); the bucket-interface mandate that forces chaining (§31.2).
- M. Kulukundis, *"Designing a Fast, Efficient, Cache-friendly Hash Table, Step by Step"* (CppCon, the Abseil SwissTable talk) — open addressing, SIMD probing, and why `std::unordered_map` is slow (§31.3–31.4.1).
- The Abseil (`flat_hash_map`), `ankerl::unordered_dense` (robin-hood), and Boost.Unordered (`unordered_flat_map`) documentation — production flat hash maps and their design trade-offs.
- The Boost.Intrusive and Boost.Container (`flat_map`, `small_vector`) documentation — intrusive containers and flat/small-buffer types (§31.4.3).
- The LMAX Disruptor paper and Martin Thompson's "Mechanical Sympathy" writings — cache-friendly data structures and the order-book/queue patterns motivating §31.4.4 (ties Ch. 45).

## 31.8 Additional Reading

- C. Cook, *"When a Microsecond Is an Eternity"* (CppCon) — hot-path container/allocation discipline in an HFT context, directly relevant to the LOB.
- Talks and writeups on building low-latency limit order books (various exchange/HFT engineering blogs) — concrete LOB data-structure designs extending §31.4.4.
- Ch. 9 (*Data-Oriented Design*) — the locality principles underneath; Ch. 30 (*Custom Allocators*) — the pools the LOB runs on; Ch. 35 (*Bit Manipulation*) — bitmaps for level occupancy/free-lists; Ch. 42/45 (*Lock-Free / Disruptor*) — ring buffers as containers; Ch. 67 (*Wire Decoding*) — the feed that drives the book.
- **Appendix E** — the cache-miss latency numbers that make "a cache miss per node" concrete; **Appendix F** — order-book/BBO terminology.

---

*Next: Ch. 32 — Limit Order Book Engineering.*
