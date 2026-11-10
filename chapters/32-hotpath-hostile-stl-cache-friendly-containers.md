# Part IV — Memory Management

# Chapter 32 — Hot-Path-Hostile STL & Cache-Friendly Containers

> **Prerequisites:** Ch. 8–10 (caches & data-oriented design — the locality the whole chapter is about), Ch. 30–31 (allocation cost & custom allocators — node containers allocate per element; the order book runs on a pool), Ch. 16 (`std::function`/type erasure), Ch. 17 (TLB — pointer-chasing thrashes it), Ch. 3 (measuring the distribution).
>
> **Leads into:** Ch. 45/48 (lock-free queues / Disruptor — ring buffers as containers), Ch. 70 (feed handlers feeding the book), Ch. 37 (bitmaps for level occupancy), Ch. 102 (hot-reload of reference data), **Ch. 33 (Limit Order Book Engineering — the chapter the §32.4.5 case study grew into; its ladders, pools and ID maps are this chapter's containers put to work)**, and §32.4.6's small-array search, which the LOB's cold depth store and every tick-band/symbol table use.

---

## 32.1 Why it matters: node-based containers chase pointers

The C++ standard library is superb general-purpose engineering and, for several of its most-used containers, **actively hostile to the hot path** — not because the implementations are bad, but because their *data structures* are wrong for a latency-bound, cache-sensitive workload. `std::map`, `std::unordered_map`, `std::list`, `std::set` are **node-based**: each element lives in a separately-allocated node somewhere on the heap, linked by pointers. Every lookup, every traversal, is a **pointer chase** — follow a pointer to a node that's almost certainly a cache miss (Ch. 8, ~80 ns to DRAM) and probably a TLB miss too (Ch. 17), then follow the next. A `std::map` lookup is O(log n) *cache misses*; a `std::unordered_map` lookup is a hash plus a walk down a chain of heap-scattered nodes. The asymptotic complexity looks fine; the *constant* is a string of DRAM round-trips, and on the hot path the constant is everything.

The damage compounds with the allocation story of Ch. 30–31: every `insert` into a node container is a `malloc` (a tail event, §30.3), every `erase` a `free`, and the nodes fragment across the heap so even sequential logical access is physically random. Add the other idiomatic-but-costly types — `std::shared_ptr` (atomic refcount increments/decrements on every copy, a cache-line ping-pong under contention — Ch. 44; plus a separate control-block allocation), `std::function` (double indirection, possible heap capture — Ch. 16) — and a "modern C++" hot path can be a parade of cache misses, atomic contention, and hidden allocations, none visible as a literal performance bug in the source.

The cure is **data-oriented container choice** (Ch. 10): prefer **flat, contiguous** structures whose elements sit next to each other in memory, so access is sequential and prefetchable (Ch. 12) and traversal stays in cache. `std::vector` over `std::list`; **open-addressing / flat hash maps** over `std::unordered_map`; `std::flat_map`/`std::flat_set` (C++23) over `std::map`/`std::set`; **intrusive** containers (the link lives *in* the object, no node allocation) and **small-buffer** types over heap-owning ones; concrete callables over `std::function`. This chapter measures the gap (§32.3), lays out the alternatives (§32.4), and applies them to the canonical HFT data structure — the **limit order book** (§32.4.4) — where the right container choices are the difference between a microsecond and a hundred nanoseconds per update. The rule throughout: choose the container for the **access pattern**, and measure the *distribution* (Ch. 1), because the node-container penalty lives in the cache-miss tail.

## 32.2 Mental model: costs of `unordered_map`/`map`/`function`/`shared_ptr`

A quick, honest cost model of the four most common hot-path-hostile types, in cache/allocation terms:

**`std::map` / `std::set` (red-black tree, node-based).** O(log n) — but each step follows a pointer to a *separately heap-allocated* node, so a lookup is ~log₂(n) **cache misses** (Ch. 8) and likely TLB misses (Ch. 17). Insert/erase = `malloc`/`free` per element (Ch. 30) plus tree rebalancing. Ordered iteration chases pointers in (logical) order over physically-scattered nodes. *Sorted* and *stable-address* are its only real virtues; for hot lookups it's slow.

**`std::unordered_map` / `std::unordered_set` (chained hash, node-based).** The standard *mandates* an interface (bucket iterators, reference stability, node handles) that forces a **chaining** implementation: buckets hold pointers to singly-linked **node** chains, each node separately allocated. So a lookup is: hash → bucket (one miss) → walk a chain of heap-scattered nodes (a miss *per node*). Insert = `malloc` a node; high load factor or bad hashing = long chains. It's far slower than a good flat hash map *by construction* — the standard's interface guarantees prevent the cache-friendly layout. The most over-used hot-path-hostile container.

**`std::shared_ptr` (shared ownership via atomic refcount).** Each copy **atomically increments** a refcount; each destruction atomically decrements (Ch. 41). Under multi-thread sharing, that refcount cache line **ping-pongs** between cores (false-sharing-like coherence traffic — Ch. 44) — a serializing cost wildly out of proportion to "copying a pointer." Plus a control-block allocation (unless `make_shared` fuses it), and the refcount itself is a cache line touched on every copy. On the hot path, `shared_ptr` *copies* are a hidden atomic-contention and allocation cost; pass by `const&`/raw pointer/`unique_ptr` and reserve `shared_ptr` for genuine shared ownership off the hot path.

**`std::function` (type-erased callable).** Double indirection, no inlining, possible heap allocation on construction (Ch. 16). Covered there; listed here because a `std::function` *stored in a container* (a `vector<std::function>`, a `map` of callbacks) multiplies both costs.

The unifying model, the through-line of Ch. 8–10 applied to containers:

```
   node-based (map/unordered_map/list):   elem ─►node─►node─►node     (a cache miss per hop)
                                          scattered across the heap; lookup = chain of DRAM trips

   flat/contiguous (vector/flat_map/flat hash):  [elem|elem|elem|elem|elem]   one region,
                                          sequential, prefetchable, cache- & TLB-resident
```

**The constant factor — cache misses per operation — dominates the hot path, and it's set by whether the container is contiguous or pointer-linked.** Choose flat unless you have a specific reason (stable addresses, ordered + huge + insert-heavy) not to.

## 32.3 Measure it: `std::unordered_map` vs flat hashing

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
    // Fibonacci hashing (Ch. 38 §38.4.3): the multiply mixes UPWARD, so mask-the-low-bits would discard the mixing
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

Read it the Ch. 8 way: **the flat map is ~3× faster on the identical workload**, and the counters say why — at N = 2¹⁶ both tables fit the 24 MiB L3, so `std::unordered_map`'s lookup is two to three *dependent* L3 hits (bucket array → node → key, each a scattered line) while the flat map's probe is one L3 hit on a contiguous slot that prefetches (Ch. 12). Size past L3 (N = 2²², exercise 1) and every one of those dependent hops becomes a DRAM round-trip plus, likely, a TLB miss: the gap widens toward ~3–4× and the absolute numbers move to ~100 ns vs ~30 ns. The build phase also did ~N `malloc`s for the chained map vs *one* allocation for the flat map's vector (Ch. 30). The win grows with key count (more misses to chase) and shrinks if everything fits L1. This is the canonical container measurement: **node-based vs flat is a cache-miss-count difference**, and §32.4 is how you get the flat side. (Production: reach for `absl::flat_hash_map`, `ankerl::unordered_dense`, or `boost::unordered_flat_map` — robin-hood/swiss-table maps that beat this toy and `std::unordered_map` substantially.)

**Row 3 — handle dereference: slot map vs flat hash vs raw pointer (§32.4.4).** The same question for the *order table*: given an identifier for an order, how fast is the object reached, and what does the stale-handle check cost? 2 M lookups, uniformly random over the live orders, at two working-set sizes (GCC 13.3 `-O2 -std=c++20 -march=native`, pinned, on the 4-vCPU VM of Appendix L.2 — TSC 2.70 GHz; VM-measured, the ratios transfer):

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

**Row 4 — searching a small sorted array: Ch. 3's promise, kept (§32.4.6).** Ch. 3 §3.3 measured `std::binary_search` on a 4096-key sorted vector losing to `unordered_map` (31 vs 19 ns), blamed ~12 unpredictable branches, and promised that this chapter would revisit the comparison "under realistic access patterns". Here it is, with the access pattern made a variable: N = 8 / 64 / 4096 keys (a tick ladder, 25-cent levels), probes either **uniform-random** or **clustered** (a random walk near the touch, ±2 levels per step — the order-book pattern), and each configuration run both as a **dependent chain** (the next key depends on the previous result: the latency a tick-to-trade path sees) and as **independent** lookups (throughput, what Ch. 3 measured). `smallsearch.cpp`, GCC 13.3 `-O2 -std=c++20 -march=native`, pinned, on the 4-vCPU VM of Appendix L.2 (VM-measured; the *orderings* transfer, the absolute figures are ±10 % run to run):

```
   ns / lookup                      N = 8          N = 64          N = 4096 random     N = 4096 clustered
                                   dep    indep   dep    indep    dep      indep      dep      indep
   std::lower_bound (branchy)      14.8   13.5    31.0   29.1     69.7     58.4       33.9     21.3   <- predictable keys: the branches are FREE
   branchless (cmov), Khuong-Morin 11.3   11.2    18.7   18.1     40.3     32.2       34.2     33.3   <- flat: pays the cmov chain whatever the keys do
   branchless + prefetch           11.3   11.2    18.6   18.2     40.4     33.2       34.9     32.2   <- no help: 16 KiB is already in L1
   Eytzinger (BFS layout)          13.9   13.8    20.4   19.8     45.9     35.6       36.9     35.2   <- wins only once the array leaves L1/L2 (exercise 6)
   SIMD linear scan (AVX2)          7.3    7.3     9.8    9.6       -        -          -        -    <- fastest for <= 64 keys, and answers the lower_bound question
   flat open-addressing hash        7.2    7.0     7.6    7.3     19.6     11.9        8.3      7.9   <- fastest for EXACT match; cannot answer "first level >= p"
```

Three results, none of which Ch. 3 could see. (1) **The branchy search is a branch-prediction story, and prediction depends on the key stream.** On random keys at N = 4096 `std::lower_bound` costs 70 ns and the cmov version 40 — the ~12 mispredicts Ch. 3 counted, removed. On the *clustered* stream the predictor learns the walk and the branchy search drops to 34 ns dependent / **21 ns independent**, where the branchless form stays at 33: a `cmov` turns a control dependency into a *data* dependency, so the twelve loads serialise instead of being speculated ahead (Ch. 15 §15.6's trap, now with a number). Order-book probes are clustered; symbol lookups are not — choose per key stream, and measure the one you have. (2) **Below ~64 keys the right search is no search**: eight AVX2 compares, a `movemask` and a `popcount` answer "how many keys are below p" in 7–10 ns regardless of where p falls, with no chain and no mispredict — this is what the LOB's cold-depth store and a tick-size-band table should use (Ch. 33 §33.4.1). (3) **The hash map "wins" because it answers an easier question.** Exact-match is all a hash can do; `lower_bound` answers *predecessor* queries (the first level at or above p, the band containing this price), which is why a sorted array is in the comparison at all. Ch. 3's result stands for symbol→id; for anything ordered, the fair comparison is branchless-vs-branchy-vs-SIMD on *your* key distribution. One more result that belongs in §32.5: **the compiler decides which of these you actually get** — Clang 18 compiled the ternary form of the "branchless" search to a *branch* (62 ns on random keys, worse than its own `std::lower_bound`, which it compiled to `cmov`), so the arithmetic form in §32.4.6 is the one that is branchless under both compilers.

## 32.4 Techniques

### 32.4.1 Flat / open-addressing & robin-hood hashing

Replace chained hashing with **open addressing**: store entries *in* a contiguous array; on collision, probe to another slot in the *same* array rather than chasing a heap node.

- **Why it's faster (§32.3).** All probing happens within a contiguous table — sequential, prefetchable (Ch. 12), cache- and TLB-resident — and there are *no per-element allocations* (one array, sized up front). A lookup is a hash plus a short contiguous probe, not a chain of DRAM trips.
- **Probing schemes.** Linear probing (simple, cache-friendly, sensitive to clustering); **robin-hood** hashing (bounds probe-length variance by displacing richer entries — excellent worst-case, the basis of `ankerl::unordered_dense`); **SwissTable**-style SIMD-group probing (Abseil's `flat_hash_map`, Boost.Unordered's `unordered_flat_map`, folly's F14: a contiguous metadata-byte array scanned 16 slots at a time with SSE2/NEON — Ch. 39 — for very fast probing). All keep data contiguous.
- **Load factor and sizing.** Open addressing degrades as it fills (longer probes); keep load factor moderate (~0.5-0.875 depending on scheme) and **`reserve` to the working set** up front (Ch. 30) so the hot path never rehashes (a rehash = a big reallocation + reinsert, a latency spike — keep it on the cold path).
- **Trade-offs.** No reference/pointer stability (entries move on rehash/displacement — don't hold pointers into the table; use indices or an intrusive design if you need stable identity). Erase needs tombstones or backward-shift. For HFT lookups (symbol→info, orderID→order) the speed wins overwhelmingly; use a production library, not a hand-roll, for the corner cases. The *hash function* and the *key* that go into the slot computation — the multiply above, why its high bits are the ones to use, 8-byte symbols as one integer key, dense exchange IDs as direct indices, a compile-time perfect hash for closed sets, and the flooding defence for wire-chosen keys — are **Ch. 38**, measured on symbols, IDs and random keys.

### 32.4.2 `std::flat_map`/`std::flat_set` (C++23)

For *ordered* associative needs, `std::flat_map`/`std::flat_set` (C++23 — shipped in libstdc++ 15 / GCC 15 and libc++ 20 (`flat_map`) / 21 (`flat_set`), so on the book's GCC 13 / Clang 18 baseline you use the fallback below; gate on `__cpp_lib_flat_map`/`__cpp_lib_flat_set >= 202207L`, see Appendix H) are the cache-friendly replacements for `std::map`/`std::set`: they're **sorted contiguous container adaptors** rather than node trees — `std::flat_map` keeps keys and mapped values in two *parallel* sorted sequences (`KeyContainer`/`MappedContainer`, `std::vector` by default: the SoA layout of Ch. 10, so a binary search touches only the dense key array), `std::flat_set` one sorted sequence. (Boost.Container's `flat_map`, by contrast, is a single sorted vector of pairs.)

- **Why faster for lookups/iteration.** Lookup is a **binary search over a contiguous array** — O(log n) but with *cache-friendly* access (Ch. 8) and no pointer chasing, and ordered iteration is a *linear scan* of contiguous memory (maximally prefetchable) instead of a tree walk over scattered nodes. Far better locality than `std::map` for read/scan-heavy use.
- **The trade-off: insert/erase cost.** Inserting into the middle shifts elements (O(n) move), so `flat_map` shines when the container is **lookup/iteration-heavy and mutation-light**, or built once and queried many times (reference data, a static symbol table, a config map). For insert-heavy ordered data, the shift cost can outweigh the locality win — measure.
- **HFT fits.** Reference/static data (symbol metadata, tick-size bands — Ch. 35, venue config), and — carefully — order-book price levels where the active range is small and contiguous (§32.4.4). C++17/20 fallback: a hand-maintained sorted `std::vector` with `std::lower_bound` (exactly what `flat_map` standardizes), or Boost.Container's `flat_map` — and §32.4.6 for how to search that vector without the branch mispredicts `lower_bound` pays.
- **Customizable underlying storage.** `flat_map` can use a `std::vector` (or `std::pmr::vector` — Ch. 31, or a small-vector), so you can back it with a custom allocator / stack buffer for zero hot-path allocation.

### 32.4.3 Intrusive, fixed-capacity and small-buffer containers

Two more ways to eliminate node allocation and indirection:

- **Intrusive containers.** The link/hook lives **inside the object**, not in a separate node: the object has a `next`/`prev` (or tree hook) member, and the container threads objects directly. Consequences: **no per-element allocation** (the object *is* the node — pair with a pool, Ch. 31), **stable addresses**, an object can be in multiple intrusive lists at once, and removal is O(1) given the object (no lookup). Boost.Intrusive provides `list`/`slist`/`set`/`unordered_set` hooks; HFT code often hand-rolls intrusive lists for order chains. The order book's price-level FIFO is the canonical use (§32.4.5): orders are pool-allocated and intrusively linked, so adding/cancelling an order is pointer surgery with zero allocation. The trade-off: the object carries the hooks (size, coupling) and you own lifetime (Ch. 31, 101).
- **Small-buffer optimization (SBO) / small-vector types.** Store small payloads **inline** (no heap) and only spill to the heap when they grow: `std::string`'s SBO, and `boost::small_vector`/`absl::InlinedVector`/`llvm::SmallVector` for "usually ≤ N elements" sequences. On the hot path, a small-vector sized to the common case is allocation-free for the common case and contiguous always. Know the SBO threshold and keep hot payloads under it: libstdc++'s `std::string` keeps 15 chars inline (libc++: 22); libstdc++'s `std::function` stores a callable inline only up to 16 bytes / pointer alignment (`sizeof` 32); and a small-vector's `N` is whatever you chose — check each with a `static_assert` or a one-off `sizeof` print, not folklore.
- **Fixed-capacity sequences: `std::inplace_vector<T, N>` (C++26, P0843) and today's equivalents.** The `vector` interface over storage that lives *inside the object*: `N` elements inline, no allocator, no heap, `constexpr`-capable, trivially copyable when `T` is — so `inplace_vector<Level, 8>` is a per-message batch of levels that passes through a ring slot or a shared-memory segment by `memcpy` (Ch. 67) and never calls `malloc`. Availability (Appendix H): libstdc++ 15 (`__cpp_lib_inplace_vector`; absent in 13/14), not yet in libc++ 18; `boost::container::static_vector<T, N>` and `etl::vector<T, N>` are the shipping spellings with the same shape. The hot-path cost is the **capacity branch**: `push_back` compares `size()` against `N` and throws `bad_alloc` when full — a predicted compare per insert, plus a cold throw path (Ch. 24). Where the bound is already proved (a loop over at most `N` wire entries after a length check), `unchecked_push_back`/`unchecked_emplace_back` skip it, and `try_push_back` returns a pointer-or-null for the §24.4.2 value-channel style. Choose `N` from the venue's documented maximum (SBE group counts, ITCH's fixed message set), not from a guess, and `static_assert(sizeof(inplace_vector<T, N>) <= 64)` when it must stay in one cache line (Ch. 11). The stable-slot cousins (`plf::colony`/`std::hive`) and slot maps are §32.4.4.
- **Index handles over pointers.** Storing **indices** into a contiguous arena/vector instead of pointers makes structures relocatable (rehash/grow doesn't invalidate), shrinks the handle (a 32-bit index vs 64-bit pointer — Ch. 11), and keeps everything in one cache-friendly region. The worked form of the idiom — a slot map with generational handles — is §32.4.4.

### 32.4.4 Slot maps and generational handles

The "index handles over pointers" bullet of §32.4.3 is an idiom; this is the structure that makes it safe. A **slot map** gives every object a stable 64-bit **handle** that is cheap to dereference (an index), cheap to *validate* (one compare), survives the container's reallocation and erasure of its neighbours, and is meaningful across processes (it is an integer, not an address). It is the right container for the three tables a trading system keeps in flight — the order table (orderID → order; the LOB's in §32.4.5, the gateway's in Ch. 105), the armed timers (Ch. 55 §55.4.1), and any object that something else holds a reference to without owning it.

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
// slotmap.hpp — dense values + sparse indirection + free list + generation. Build with the §32.3 harness: g++ -O2 -std=c++20 -march=native slotmap.cpp -o slotmap
struct Handle { std::uint64_t v; static Handle make(std::uint32_t idx, std::uint32_t gen) { return {std::uint64_t(gen) << 32 | idx}; }
                std::uint32_t idx() const { return std::uint32_t(v); } std::uint32_t gen() const { return std::uint32_t(v >> 32); } };   // Ch. 37 §37.4.4: two shifts
template <class T, std::uint32_t CAP> struct SlotMap {                      // CAP a power of two: the index mask is the bounds check (Ch. 37 §37.4.3)
    struct Slot { std::uint32_t dense, gen, next_free; };
    Slot sparse[CAP]; T dense[CAP]; std::uint32_t size = 0, free_head = 0;
    SlotMap() { for (std::uint32_t i = 0; i < CAP; ++i) sparse[i] = {0, 1, i + 1}; }   // gen starts at 1: a zero handle is never valid
    Handle insert(const T& t) { std::uint32_t s = free_head; Slot& sl = sparse[s]; free_head = sl.next_free; sl.dense = size; dense[size] = t; dense[size].back = s; ++size; return Handle::make(s, sl.gen); }
    T* get(Handle h) { Slot& sl = sparse[h.idx() & (CAP - 1)]; return sl.gen == h.gen() ? &dense[sl.dense] : nullptr; }   // ONE predictable compare (Ch. 15): stale → nullptr
    void erase(Handle h) { Slot& sl = sparse[h.idx() & (CAP - 1)]; if (sl.gen != h.gen()) return;                            // stale erase is a no-op, not a corruption
        std::uint32_t d = sl.dense; --size; if (d != size) { dense[d] = dense[size]; sparse[dense[d].back].dense = d; }        // erase-by-swap keeps values contiguous
        sl.gen += 2; sl.next_free = free_head; free_head = h.idx(); }                                                        // += 2 keeps gen odd... or += 1; any bump kills the old handle
};
```

- **Validation is one compare, and it is the point.** A pointer to a freed slot is a use-after-free the allocator cannot catch (Ch. 31, 101); a handle to a freed slot fails `gen == h.gen()` and yields `nullptr` — deterministically, every time, for the life of the process (a 32-bit generation wraps after 4 billion reuses of *one* slot). The branch predicts "valid" on the hot path and the stale case is the slow path; §32.3's measurement shows the check costing ~0.3 ns over the unchecked index.
- **Erase-by-swap vs stable slots.** Moving the last value into the hole keeps `dense[]` contiguous — iteration over all live orders is a linear scan (Ch. 10), which the kill-switch (Ch. 104) and a snapshot writer (Ch. 109) want. The cost: a value's *address* changes on someone else's erase, so **nothing may hold a `T*` across an erase** — hold the `Handle`. When values carry intrusive links to each other (the LOB's per-level FIFO — §32.4.5), use the **stable-slot variant**: values live at `dense[index]` directly, no swap, iteration skips free slots (or walks an intrusive live list); Ch. 105's in-flight table is that variant.
- **Relative handles for shared memory** (Ch. 67 §67.4.2): the handle is an integer, so a process that maps the segment at a different address dereferences it the same way; the segment holds `sparse[]`, `dense[]` and the free list as offsets from the segment base, never pointers — and the generation check guards a *reader* process against a slot the *writer* process recycled between the reader's lookup and its use.
- **Debug-mode poisoning**: under `#ifndef NDEBUG`, `erase` fills the vacated value with `0xDD` and `get` on a stale handle records `(handle, caller)` before returning `nullptr`; the Ch. 49 §49.4.5 outstanding-view counter is the same discipline for ring slots.
- **Codegen check** (Ch. 4): `get()` must be a shift, a masked index, a compare and one indexed load — Clang 18.1 at `-O2` emits `movzwl`/`and`, two `lea`s for the 12-byte stride, `shr $32`, `cmp`, `jne`, then the dense index load; no call, no bounds branch beyond the compare. If a `div` or a second compare appears, `CAP` was not a power of two or the generation was not in the high word.
- **Cousins, in one sentence**: stable-slot vectors (`plf::colony`/C++26 `std::hive` — insertion-order-free, erase-stable, skipfield iteration), sparse sets (the same two arrays without generations, for *sets* of small integers) and `std::inplace_vector` (C++26, Appendix H — fixed-capacity contiguous storage) occupy the same design space with different trade-offs on iteration cost, stability and validation; their full treatment is a separate item and is not written here.

### 32.4.5 Case Study — Building the Limit Order Book

The **limit order book (LOB)** is the central HFT data structure and the crucible for everything above: a feed handler updates it on *every* market-data message (Ch. 70), the budget is tens of nanoseconds, and the naive STL design — `std::map<Price, std::list<Order>>` with an `unordered_map` from order id to iterator — is a worst case of everything this chapter measured: a node tree to find a level, a `malloc` per order, a cache miss per order in the queue. This case study used to live here as a prose outline; it is now **Ch. 33 — Limit Order Book Engineering**, which builds and *measures* the design this chapter's containers were chosen for: price levels in a **dense tick-indexed ladder** with an occupancy bitmap and a re-centring window (§33.4.1–§33.4.2), orders in a **stable-slot pool** linked into intrusive per-level FIFOs (§32.4.3–§32.4.4 applied, §33.4.3), the order-id map as **direct addressing** for near-monotone venue ids with the slot map and the flat hash (§32.4.1) as the alternatives (§33.4.3), the **BBO/depth hot-cold split** (§33.4.4), snapshot rebuild (§33.4.5) and the **market-by-price vs market-by-order** choice that decides the whole shape (§33.2.1). Its headline measurement (§33.3): 5 M synthetic captured-style events, the ladder book at ~16 ns per event p50 against ~60 ns for the `std::map`/`std::list` baseline, with zero hot-path allocations against 4.8 million. The cold depth store it keeps beyond the window is a sorted small array, searched the way §32.4.6 shows.

### 32.4.6 Searching small sorted arrays: branchless binary search, Eytzinger layout and SIMD scan

The sorted contiguous array is the cache-friendly answer to *ordered* lookup (§32.4.2), and §32.3's row 4 showed that *how* it is searched matters as much as the layout: `std::lower_bound` is a chain of ~log₂N data-dependent branches, each a mispredict on an unpredictable key. The alternatives, with the code:

```cpp
// smallsearch.hpp — four ways to answer lower_bound(key) over a sorted u32 array; all return the index of the first element >= key.
#include <bit>
#include <cstdint>
#include <immintrin.h>
using u32 = std::uint32_t;

// 1. Branchless half-interval search (Khuong & Morin 2017). The select is written as ARITHMETIC, not a ternary:
//    GCC 13 compiles the ternary to cmovb, Clang 18 compiles the same ternary to a branch (jae) — §32.5.
inline u32 lb_branchless(const u32* a, u32 n, u32 key) {
    const u32* base = a;
    while (n > 1) { u32 half = n / 2; base += (base[half] < key) * half; n -= half; }   // cmp + setb + imul/lea: no jump
    return u32((*base < key) + (base - a));
}
// 1b. Fixed trip count for a compile-time size: the loop unrolls completely (no loop branch either).
template <u32 N> inline u32 lb_fixed(const u32* a, u32 key) {
    static_assert(std::has_single_bit(N)); const u32* base = a;
    #pragma GCC unroll 16
    for (u32 half = N / 2; half >= 1; half /= 2) base += (base[half] < key) * half;
    return u32((*base < key) + (base - a));
}
// 2. Eytzinger (BFS) layout: t[1] is the root, children of k are 2k and 2k+1. The top levels share cache lines,
//    the descent is branchless and the next level's line can be prefetched 16 nodes ahead. Static: rebuilt on insert.
struct Eytzinger {
    std::vector<u32> t;                                                 // t[0] unused; t[1..n]
    void build(const u32* sorted, u32 n) { t.assign(n + 1, 0); u32 i = 0; rec(sorted, n, i, 1); }
    void rec(const u32* s, u32 n, u32& i, u32 k) { if (k <= n) { rec(s, n, i, 2 * k); t[k] = s[i++]; rec(s, n, i, 2 * k + 1); } }
    u32 lower_bound_pos(u32 key) const {                               // position in Eytzinger order (0 = past the end)
        u32 k = 1, n = u32(t.size() - 1);
        while (k <= n) { __builtin_prefetch(&t[k * 16]); k = 2 * k + (t[k] < key); }
        return k >> (std::countr_one(k) + 1);                          // undo the trailing right turns
    }
};
// 3. SIMD scan for N <= 64: how many keys are < key? That count IS the lower_bound index. No chain, no branch.
inline u32 lb_simd(const u32* a /* padded to a multiple of 8 with 0xFFFFFFFF */, u32 n_padded, u32 key) {
    const __m256i k = _mm256_set1_epi32(int(key ^ 0x80000000u));        // flip the sign bit: signed compare == unsigned compare
    u32 below = 0;
    for (u32 i = 0; i < n_padded; i += 8) {
        __m256i v = _mm256_xor_si256(_mm256_loadu_si256((const __m256i*)(a + i)), _mm256_set1_epi32(int(0x80000000u)));
        below += u32(std::popcount(unsigned(_mm256_movemask_ps(_mm256_castsi256_ps(_mm256_cmpgt_epi32(k, v))))));
    }
    return below;
}
```

- **Why `std::lower_bound` is slow, precisely.** Each iteration's branch direction is a bit of the answer; on keys the predictor cannot learn, half of ~log₂N iterations mispredict at ~15–20 cycles each (Ch. 15 §15.3), and each iteration's load address depends on the previous compare, so nothing overlaps. On keys it *can* learn (a price walk) the same code is the fastest thing in §32.3's table at N = 4096 — the branches are predicted and the loads are speculated ahead. The decision is per key stream, not per algorithm.
- **The branchless form** replaces the jump with a `cmov` (or the `setb`/`imul` pair the arithmetic form produces) so there is nothing to mispredict — at the price of a *data* dependency chain of ~log₂N loads that can no longer be speculated past. Flat ~40 ns at N = 4096 whatever the keys do; a sure win on random keys, a small loss on predictable ones (§32.3). For a size known at compile time use `lb_fixed<N>`: the loop unrolls and the only branch left is the caller's. Prefetching both candidates one level ahead (`a[half/2]`, `a[half + half/2]`) is free in L1 and only helps once the array has left L2 — where Eytzinger helps more.
- **Eytzinger layout** (van Emde Boas's cache-oblivious cousin, measured by Khuong & Morin): store the array in BFS order so the first four levels of the search touch *one* cache line and each further level's candidates are 16 nodes apart in a predictable direction — a prefetch of `t[16k]` covers the whole next-but-one level. On an in-L1 array it is slightly slower than the plain branchless search (one extra multiply per level and the `countr_one` fix-up); past L2 it is ~2× faster than it (exercise 6). The price is that the layout is **static** — rebuild on insert — which is fine for tick-size bands, symbol tables and venue lists and wrong for a mutating book.
- **SIMD linear scan** is the answer below ~64 keys: the whole array is compared in one to eight vector instructions, `movemask` + `popcount` turns the compare results into the index, and the cost is flat in the position of the key (7–10 ns in §32.3, including the sign-flip trick that makes the signed compare behave as unsigned). The cold-depth store of Ch. 33's book and the `inplace_vector` of levels in a per-message batch (§32.4.3) are this size. AVX-512 (`vpcmpud` + `kmov` + `popcnt`) does 16 keys per compare with native unsigned compares; the Ch. 39 downclocking caveat applies to the 512-bit form.
- **Sorting a small array** belongs here in one paragraph: for ≤ 16 elements (ranking venue quotes for a consolidated BBO, Ch. 33 §33.4.6; the top-N levels of a snapshot) a **sorting network** — a fixed sequence of compare-exchange pairs, each a `min`/`max` (`cmov` or `vpminud`/`vpmaxud`) — sorts in a flat, branch-free time (Batcher's odd-even merge for 8 elements is 19 compare-exchanges, ~10 ns); `std::sort` dispatches to insertion sort at this size and mispredicts per inversion. Larger sorts are off the hot path; where they are not, `std::sort` on contiguous data beats anything node-based for the usual reason.
- **Where each wins** (§32.3, row 4): exact-match on an unordered key → the flat hash, always; ordered lookup with ≤ 64 keys → SIMD scan; ordered lookup on unpredictable keys → branchless, Eytzinger once the array is larger than L2; ordered lookup on a predictable walk → plain `lower_bound` is fine and the branchless form costs ~10 ns more. Ch. 3's "the hash map won" was the first of these, measured on the wrong question for a book.

## 32.5 Pitfalls & anti-patterns: `shared_ptr` refcount churn

- **`std::map`/`std::unordered_map` on the hot path.** The default reach — and a parade of cache misses + per-element `malloc` (§32.2–32.3). Use a flat/open-addressing map (§32.4.1) or `flat_map` (§32.4.2); reserve the rare justified `std::map` use (need stable addresses *and* ordered *and* insert-heavy) and know you're paying for it.
- **`shared_ptr` copies on the hot path.** Every copy is an **atomic** refcount bump; shared across cores, the refcount line ping-pongs (Ch. 44) — a serializing cost dwarfing the "pointer copy" it looks like. Pass by `const&`/raw/`unique_ptr`; only copy `shared_ptr` when transferring genuine shared ownership, off the hot path. `make_shared` to fuse the control-block allocation when you do use it.
- **Slot-map misuse (§32.4.4).** *Skipping the generation check on a "trusted" path*: the one caller that dereferences by index alone is the one that books a fill against a reused slot. *Holding a `T*` across an erase* in the erase-by-swap variant: the value moved; hold the handle. *Generation wrap*: a 16-bit generation on a hot slot wraps in minutes at 10⁵ reuses/s — use 32 bits (4 billion reuses per slot). *Crossing processes without the generation* (an index alone into a shared segment — Ch. 67) loses exactly the protection the reader needed. *Starting generations at zero* makes `Handle{0}` valid; start at 1.
- **Branchless search on a predictable key stream; a ternary that is not a cmov.** The cmov form (§32.4.6) is slower than `std::lower_bound` when the predictor can learn the keys (a price walk: 33 vs 21 ns at N = 4096, §32.3 row 4), because it turns speculable control flow into a serial load chain (Ch. 15 §15.6). And the *source* does not decide which you get: Clang 18 compiled the ternary form to a branch (62 ns) and GCC 13 to `cmovb`; write the select as arithmetic (`base += (cond) * half`) or check the asm (§15.5) before calling anything branchless. **Eytzinger for a mutating set.** Its layout is static; an insert is a rebuild (O(N)) — use it for reference data, never for the live book.
- **Holding pointers into a flat/rehashing container.** Open-addressing and `vector`-backed containers **move elements** on rehash/grow/insert; a stored pointer/iterator dangles. Use **indices/handles** (§32.4.3) or reserve enough to never rehash on the hot path. (This is the trade for the locality win — design for it.)
- **Not `reserve`-ing → hot-path rehash/realloc.** A flat map or vector that rehashes/reallocates mid-stream is a latency spike (a big alloc + reinsert/move — Ch. 30). Size to the working set up front; keep growth on the cold path.
- **`vector<std::function>` / containers of type-erased callables.** Multiplies `std::function`'s double-indirection and possible allocation (Ch. 16) across every element. Use a concrete callable type, a `variant`, or function pointers (Ch. 16).
- **Intrusive without lifetime discipline.** Intrusive containers + pools mean *you* own lifetimes; an order freed to the pool while still linked, or linked into two lists with conflicting lifetimes, is UAF/corruption (Ch. 31, 101). Keep the ownership rules simple and explicit.
- **Choosing `flat_map` for insert-heavy data.** `flat_map`'s middle-insert is O(n) shifts (§32.4.2); for insert/erase-heavy *ordered* data it can lose to a tree. Match it to lookup/scan-heavy, mutation-light use — and measure.
- **Benchmarking with everything in L1.** A container comparison on a tiny dataset hides the cache-miss difference that *is* the point (§32.3). Test at the real working-set size (and with realistic, cache-cold access — Ch. 3), or you'll conclude node containers are "fine."
- **Ignoring the order book's access distribution.** Optimizing the LOB for worst-case arbitrary-depth ops instead of the real, top-of-book-dominated distribution (Ch. 33 §33.2.4) wastes effort and may pessimize the common path. Profile against captured market data (Ch. 109).

## 32.6 Exercises & checklist

**Exercises**

1. **Flat vs chained.** Build `flatmap.cpp`; run `chained` vs `flat` pinned with `perf stat -e L1-dcache-load-misses,dTLB-load-misses` (or `dtlb_load_misses.walk_completed`, as Ch. 17 uses). Confirm the flat map's lower misses and ~3-4× speed. Sweep N from L1-sized to >L3 — how does the gap grow with working-set size?
2. **Library shootout.** Replace the toy flat map with `absl::flat_hash_map`, `ankerl::unordered_dense`, and `boost::unordered_flat_map`; compare against `std::unordered_map` on the same workload. Quantify the production gap.
3. **`shared_ptr` churn.** Hammer a `shared_ptr` copied/destroyed across two threads on different cores; measure throughput and the HITM counters — `mem_load_l3_hit_retired.xsnp_fwd` (older alias `xsnp_hitm`) for two cores on one socket, `mem_load_l3_miss_retired.remote_hitm` only across sockets (Ch. 18), or simply `perf c2c` (Ch. 44, *False Sharing*) vs passing `const&`. How expensive is the "free pointer copy"?
4. **Build the LOB core.** Ch. 33 builds and measures it (`lob.cpp`, §33.3); here, reproduce its §33.3 table on your machine with `perf stat -e L1-dcache-load-misses,branch-misses` per variant, and confirm zero hot-path allocation with the `operator new` counter (Ch. 30).
5. **`flat_map` crossover.** For an ordered map, find the insert-rate at which `std::flat_map` loses to `std::map` (shift cost vs locality). Where does each win (§32.4.2)?
6. **Search past L2.** Rerun `smallsearch.cpp` (§32.3 row 4) at N = 2²⁰ (4 MiB of keys) with `perf stat -e branch-misses,L1-dcache-load-misses,LLC-load-misses`: now the dependent loads miss, and the Eytzinger layout with its 16-ahead prefetch should pull ahead of the plain branchless search by ~2×; confirm it, then add the S-tree variant (16 keys per node, one AVX-512 compare per level — Algorithmica) and see whether it beats both. Then put the clustered key stream back and watch `std::lower_bound` recover.

**Checklist — cache-friendly containers**

- [ ] Hot-path maps/sets are **flat/open-addressing** (§32.4.1) or **`flat_map`/`flat_set`** (§32.4.2), not `std::map`/`std::unordered_map`; I justified any node container deliberately.
- [ ] Containers are **`reserve`d to the working set**; the hot path never **rehashes/reallocates** (Ch. 30); I hold **indices/handles**, not pointers, into relocating containers.
- [ ] **No `shared_ptr` copies** on the hot path (pass `const&`/raw/`unique_ptr`); `shared_ptr` is reserved for genuine off-hot-path shared ownership (`make_shared`).
- [ ] Per-element node allocation is eliminated via **intrusive containers + pools** (Ch. 31) and/or **small-buffer** types where payloads are usually small (§32.4.3).
- [ ] The **order book** is Ch. 33's: a dense tick ladder + pool-backed intrusive FIFOs + an order-id map chosen for the venue's id scheme, optimized for the **top-of-book-dominated** access distribution, with deep book as a cold secondary store (§32.4.5 → Ch. 33).
- [ ] **Small sorted arrays** are searched for their key stream: SIMD scan at ≤ 64 keys, branchless (arithmetic select, asm-verified) on unpredictable keys, plain `lower_bound` on a predictable walk, Eytzinger only for static data past L2 (§32.4.6) — and the hash map is used only where exact match is the question.
- [ ] **No `vector<std::function>`** / containers of type-erased callables on the hot path (Ch. 16).
- [ ] Intrusive/pool designs have **explicit, simple lifetime rules** (no linked-while-freed — Ch. 31, 101).
- [ ] Container choices are validated by **cache/TLB counters and the latency distribution** at the **real working-set size** against **captured data** (Ch. 3, 8, 109) — not a tiny in-L1 microbench.

## 32.7 References

- ISO C++ / cppreference — the complexity and interface guarantees of `std::map`/`std::unordered_map`/`std::list`/`std::shared_ptr`, and `std::flat_map`/`std::flat_set` (C++23); the bucket-interface mandate that forces chaining (§32.2).
- M. Kulukundis, *"Designing a Fast, Efficient, Cache-friendly Hash Table, Step by Step"* (CppCon, the Abseil SwissTable talk) — open addressing, SIMD probing, and why `std::unordered_map` is slow (§32.3–32.4.1).
- The Abseil (`flat_hash_map`), `ankerl::unordered_dense` (robin-hood), and Boost.Unordered (`unordered_flat_map`) documentation — production flat hash maps and their design trade-offs.
- The Boost.Intrusive and Boost.Container (`flat_map`, `small_vector`) documentation — intrusive containers and flat/small-buffer types (§32.4.3).
- The LMAX Disruptor paper and Martin Thompson's "Mechanical Sympathy" writings — cache-friendly data structures and the order-book/queue patterns motivating §32.4.5 (ties Ch. 48).
- P.-V. Khuong & P. Morin, *"Array Layouts for Comparison-Based Searching"* (ACM JEA 2017) — the branchless binary search, the Eytzinger/B-tree/van Emde Boas layouts and the measurements behind §32.4.6; the source of the "branchless loses on predictable keys" caveat.
- S. Slotin, *Algorithmica: Algorithms for Modern Hardware* — the "Binary Search" and "Static B-Trees" (S-tree/S+-tree) chapters: SIMD node compares and prefetching for sorted search (exercise 6).

## 32.8 Additional Reading

- C. Cook, *"When a Microsecond Is an Eternity"* (CppCon) — hot-path container/allocation discipline in an HFT context, directly relevant to the LOB.
- Talks and writeups on building low-latency limit order books (various exchange/HFT engineering blogs) — concrete LOB data-structure designs extending §32.4.4.
- Ch. 10 (*Data-Oriented Design*) — the locality principles underneath; Ch. 31 (*Custom Allocators*) — the pools the LOB runs on; Ch. 37 (*Bit Manipulation*) — bitmaps for level occupancy/free-lists; Ch. 45/48 (*Lock-Free / Disruptor*) — ring buffers as containers; Ch. 70 (*Wire Decoding*) — the feed that drives the book.
- **Appendix E** — the cache-miss latency numbers that make "a cache miss per node" concrete; **Appendix F** — order-book/BBO terminology.

---

*Next: Ch. 33 — Limit Order Book Engineering. The containers above were chosen for one structure: the limit order book. Ch. 33 builds it — market-by-order vs market-by-price, a dense tick ladder with re-centring and a cold overflow, the pool-backed order table behind three id maps, the BBO/depth split, snapshot rebuild — and replays five million captured-style events through it and through the `std::map`/`std::list` baseline to measure the distribution §32.4.5 only promised.*
