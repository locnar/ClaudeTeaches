# Part IV — Memory Management

# Chapter 25 — Hot-Path-Hostile STL & Cache-Friendly Containers

> **Prerequisites:** Ch. 7–8 (caches & data-oriented design — the locality the whole chapter is about), Ch. 23–24 (allocation cost & custom allocators — node containers allocate per element; the order book runs on a pool), Ch. 14 (`std::function`/type erasure), Ch. 15 (TLB — pointer-chasing thrashes it), Ch. 3 (measuring the distribution).
>
> **Leads into:** Ch. 34/37 (lock-free queues / Disruptor — ring buffers as containers), Ch. 53 (feed handlers feeding the book), Ch. 28 (bitmaps for level occupancy), Ch. 73 (hot-reload of reference data). The order-book case study (§25.4.4) is referenced throughout Parts VIII-X.

---

## 25.1 Why it matters: node-based containers chase pointers

The C++ standard library is superb general-purpose engineering and, for several of its most-used containers, **actively hostile to the hot path** — not because the implementations are bad, but because their *data structures* are wrong for a latency-bound, cache-sensitive workload. `std::map`, `std::unordered_map`, `std::list`, `std::set` are **node-based**: each element lives in a separately-allocated node somewhere on the heap, linked by pointers. Every lookup, every traversal, is a **pointer chase** — follow a pointer to a node that's almost certainly a cache miss (Ch. 7, ~80 ns to DRAM) and probably a TLB miss too (Ch. 15), then follow the next. A `std::map` lookup is O(log n) *cache misses*; a `std::unordered_map` lookup is a hash plus a walk down a chain of heap-scattered nodes. The asymptotic complexity looks fine; the *constant* is a string of DRAM round-trips, and on the hot path the constant is everything.

The damage compounds with the allocation story of Ch. 23–24: every `insert` into a node container is a `malloc` (a tail event, Ch. 23.3), every `erase` a `free`, and the nodes fragment across the heap so even sequential logical access is physically random. Add the other idiomatic-but-costly types — `std::shared_ptr` (atomic refcount increments/decrements on every copy, a cache-line ping-pong under contention — Ch. 33; plus a separate control-block allocation), `std::function` (double indirection, possible heap capture — Ch. 14) — and a "modern C++" hot path can be a parade of cache misses, atomic contention, and hidden allocations, none visible as a literal performance bug in the source.

The cure is **data-oriented container choice** (Ch. 8): prefer **flat, contiguous** structures whose elements sit next to each other in memory, so access is sequential and prefetchable (Ch. 10) and traversal stays in cache. `std::vector` over `std::list`; **open-addressing / flat hash maps** over `std::unordered_map`; `std::flat_map`/`std::flat_set` (C++23) over `std::map`/`std::set`; **intrusive** containers (the link lives *in* the object, no node allocation) and **small-buffer** types over heap-owning ones; concrete callables over `std::function`. This chapter measures the gap (§25.3), lays out the alternatives (§25.4), and applies them to the canonical HFT data structure — the **limit order book** (§25.4.4) — where the right container choices are the difference between a microsecond and a hundred nanoseconds per update. The rule throughout: choose the container for the **access pattern**, and measure the *distribution* (Ch. 1), because the node-container penalty lives in the cache-miss tail.

## 25.2 Mental model: costs of `unordered_map`/`map`/`function`/`shared_ptr`

A quick, honest cost model of the four most common hot-path-hostile types, in cache/allocation terms:

**`std::map` / `std::set` (red-black tree, node-based).** O(log n) — but each step follows a pointer to a *separately heap-allocated* node, so a lookup is ~log₂(n) **cache misses** (Ch. 7) and likely TLB misses (Ch. 15). Insert/erase = `malloc`/`free` per element (Ch. 23) plus tree rebalancing. Ordered iteration chases pointers in (logical) order over physically-scattered nodes. *Sorted* and *stable-address* are its only real virtues; for hot lookups it's slow.

**`std::unordered_map` / `std::unordered_set` (chained hash, node-based).** The standard *mandates* an interface (bucket iterators, reference stability, node handles) that forces a **chaining** implementation: buckets hold pointers to singly-linked **node** chains, each node separately allocated. So a lookup is: hash → bucket (one miss) → walk a chain of heap-scattered nodes (a miss *per node*). Insert = `malloc` a node; high load factor or bad hashing = long chains. It's far slower than a good flat hash map *by construction* — the standard's interface guarantees prevent the cache-friendly layout. The most over-used hot-path-hostile container.

**`std::shared_ptr` (shared ownership via atomic refcount).** Each copy **atomically increments** a refcount; each destruction atomically decrements (Ch. 30). Under multi-thread sharing, that refcount cache line **ping-pongs** between cores (false-sharing-like coherence traffic — Ch. 33) — a serializing cost wildly out of proportion to "copying a pointer." Plus a control-block allocation (unless `make_shared` fuses it), and the refcount itself is a cache line touched on every copy. On the hot path, `shared_ptr` *copies* are a hidden atomic-contention and allocation cost; pass by `const&`/raw pointer/`unique_ptr` and reserve `shared_ptr` for genuine shared ownership off the hot path.

**`std::function` (type-erased callable).** Double indirection, no inlining, possible heap allocation on construction (Ch. 14). Covered there; listed here because a `std::function` *stored in a container* (a `vector<std::function>`, a `map` of callbacks) multiplies both costs.

The unifying model, the through-line of Ch. 7–8 applied to containers:

```
   node-based (map/unordered_map/list):   elem ─►node─►node─►node     (a cache miss per hop)
                                          scattered across the heap; lookup = chain of DRAM trips

   flat/contiguous (vector/flat_map/flat hash):  [elem|elem|elem|elem|elem]   one region,
                                          sequential, prefetchable, cache- & TLB-resident
```

**The constant factor — cache misses per operation — dominates the hot path, and it's set by whether the container is contiguous or pointer-linked.** Choose flat unless you have a specific reason (stable addresses, ordered + huge + insert-heavy) not to.

## 25.3 Measure it: `std::unordered_map` vs flat hashing

Look up symbols (or order IDs) in a map — the bread-and-butter feed-handler operation — comparing `std::unordered_map` against a flat open-addressing map (here, a stand-in for `absl::flat_hash_map`/`ankerl::unordered_dense`; the point is the *layout*). Same keys, same queries; only the container's memory structure differs.

```cpp
// flatmap.cpp — chained std::unordered_map vs a flat open-addressing map.
// Build: g++ -O2 -std=c++20 -march=native flatmap.cpp -o flatmap
// Run pinned:  taskset -c 2 ./flatmap chained   |   ./flatmap flat
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
    explicit FlatMap(std::size_t cap) : t(cap) {}
    void put(std::uint64_t k, std::uint64_t v) {
        std::size_t i = (k * 0x9E3779B97F4A7C15ull) & (t.size() - 1);
        while (t[i].used && t[i].key != k) i = (i + 1) & (t.size() - 1);  // probe contiguously
        t[i] = {k, v, true};
    }
    std::uint64_t get(std::uint64_t k) const {
        std::size_t i = (k * 0x9E3779B97F4A7C15ull) & (t.size() - 1);
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

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP), `-O2 -march=native`, pinned, turbo off (illustrative; the *ratio* is the point):

```
                          chained (unordered_map)    flat (open-addressing)
ns / lookup                    ~75 ns                    ~22 ns        <- ~3-4x faster
L1-dcache-load-misses          high (node chase)         low (contiguous probe)
dtlb_load_misses               elevated (scattered)      low
allocations (build)            ~N mallocs (one/node)     1 (the vector)
```

Read it the Ch. 7 way: **the flat map is ~3-4× faster on the identical workload**, and the counters say why — `std::unordered_map`'s lookup chases a heap-scattered node (a cache miss, often a TLB miss), while the flat map probes *contiguous* slots that prefetch (Ch. 10) and stay cache-resident. The build phase also did ~N `malloc`s for the chained map vs *one* allocation for the flat map's vector (Ch. 23). The win grows with key count (more misses to chase) and shrinks if everything fits L1. This is the canonical container measurement: **node-based vs flat is a cache-miss-count difference**, and §25.4 is how you get the flat side. (Production: reach for `absl::flat_hash_map`, `ankerl::unordered_dense`, or `boost::unordered_flat_map` — robin-hood/swiss-table maps that beat this toy and `std::unordered_map` substantially.)

## 25.4 Techniques

### 25.4.1 Flat / open-addressing & robin-hood hashing

Replace chained hashing with **open addressing**: store entries *in* a contiguous array; on collision, probe to another slot in the *same* array rather than chasing a heap node.

- **Why it's faster (§25.3).** All probing happens within a contiguous table — sequential, prefetchable (Ch. 10), cache- and TLB-resident — and there are *no per-element allocations* (one array, sized up front). A lookup is a hash plus a short contiguous probe, not a chain of DRAM trips.
- **Probing schemes.** Linear probing (simple, cache-friendly, sensitive to clustering); **robin-hood** hashing (bounds probe-length variance by displacing richer entries — excellent worst-case, the basis of `ankerl::unordered_dense`); **SwissTable** / hash2 (Abseil's `flat_hash_map`: a contiguous control-byte array scanned with SIMD — Ch. 29 — for very fast probing). All keep data contiguous.
- **Load factor and sizing.** Open addressing degrades as it fills (longer probes); keep load factor moderate (~0.5-0.875 depending on scheme) and **`reserve` to the working set** up front (Ch. 23) so the hot path never rehashes (a rehash = a big reallocation + reinsert, a latency spike — keep it on the cold path).
- **Trade-offs.** No reference/pointer stability (entries move on rehash/displacement — don't hold pointers into the table; use indices or an intrusive design if you need stable identity). Erase needs tombstones or backward-shift. For HFT lookups (symbol→info, orderID→order) the speed wins overwhelmingly; use a production library, not a hand-roll, for the corner cases.

### 25.4.2 `std::flat_map`/`std::flat_set` (C++23)

For *ordered* associative needs, `std::flat_map`/`std::flat_set` (C++23) are the cache-friendly replacements for `std::map`/`std::set`: they're **sorted contiguous containers** (a sorted `vector` under the hood) rather than node trees.

- **Why faster for lookups/iteration.** Lookup is a **binary search over a contiguous array** — O(log n) but with *cache-friendly* access (Ch. 7) and no pointer chasing, and ordered iteration is a *linear scan* of contiguous memory (maximally prefetchable) instead of a tree walk over scattered nodes. Far better locality than `std::map` for read/scan-heavy use.
- **The trade-off: insert/erase cost.** Inserting into the middle shifts elements (O(n) move), so `flat_map` shines when the container is **lookup/iteration-heavy and mutation-light**, or built once and queried many times (reference data, a static symbol table, a config map). For insert-heavy ordered data, the shift cost can outweigh the locality win — measure.
- **HFT fits.** Reference/static data (symbol metadata, tick-size bands — Ch. 27, venue config), and — carefully — order-book price levels where the active range is small and contiguous (§25.4.4). C++17/20 fallback: a hand-maintained sorted `std::vector` with `std::lower_bound` (exactly what `flat_map` standardizes), or Boost.Container's `flat_map`.
- **Customizable underlying storage.** `flat_map` can use a `std::vector` (or `std::pmr::vector` — Ch. 24, or a small-vector), so you can back it with a custom allocator / stack buffer for zero hot-path allocation.

### 25.4.3 Intrusive containers and small-buffer types

Two more ways to eliminate node allocation and indirection:

- **Intrusive containers.** The link/hook lives **inside the object**, not in a separate node: the object has a `next`/`prev` (or tree hook) member, and the container threads objects directly. Consequences: **no per-element allocation** (the object *is* the node — pair with a pool, Ch. 24), **stable addresses**, an object can be in multiple intrusive lists at once, and removal is O(1) given the object (no lookup). Boost.Intrusive provides `list`/`slist`/`set`/`unordered_set` hooks; HFT code often hand-rolls intrusive lists for order chains. The order book's price-level FIFO is the canonical use (§25.4.4): orders are pool-allocated and intrusively linked, so adding/cancelling an order is pointer surgery with zero allocation. The trade-off: the object carries the hooks (size, coupling) and you own lifetime (Ch. 24, 72).
- **Small-buffer optimization (SBO) / small-vector types.** Store small payloads **inline** (no heap) and only spill to the heap when they grow: `std::string`'s SBO, and `boost::small_vector`/`absl::InlinedVector`/`llvm::SmallVector` for "usually ≤ N elements" sequences. On the hot path, a small-vector sized to the common case is allocation-free for the common case and contiguous always. Know the SBO threshold (Ch. 23) and keep hot payloads under it.
- **Index handles over pointers.** Storing **indices** into a contiguous arena/vector instead of pointers makes structures relocatable (rehash/grow doesn't invalidate), shrinks the handle (a 32-bit index vs 64-bit pointer — Ch. 9), and keeps everything in one cache-friendly region — a common low-latency idiom (slot maps, generational indices).

### 25.4.4 Case Study — Building the Limit Order Book

The **limit order book (LOB)** is the central HFT data structure, and it's a perfect crucible for this chapter: a feed handler updates it on *every* market-data message (Ch. 53), and the budget is tens of nanoseconds. The naive STL implementation — `std::map<Price, std::list<Order>>` — is a worst-case of everything above: a tree of nodes (log-n cache misses to find a price level) whose values are heap-linked lists of nodes (a `malloc` per order, a cache miss per order in the queue). It "works" and it's an order of magnitude too slow. The fast design exploits the LOB's structure:

- **Price levels: a flat array indexed by ticks, not a tree.** Prices are discrete (tick-sized — Ch. 27) and the *active* range near the touch is small and contiguous. Represent each side as a **flat array of price levels indexed by (price − base)/tick** — O(1) access to any level, contiguous, prefetchable, no tree, no per-level allocation. The book is a window over the active ticks (a ring/sliding array, or a dense array bounded by the instrument's plausible range), with the **best bid/offer (BBO)** tracked as the current top index. Deep, rarely-touched levels can live in a secondary (sparse/`flat_map`) structure — the hot/cold split of Ch. 1 applied to depth.

  ```
   bids[]  indexed by tick:  [ ... | L | L | L | BBO ] <- top-of-book at the active end
   asks[]                    [ BBO | L | L | L | ... ]   each L is a price-level FIFO (below)
   O(1) to any level by price; BBO is just an index; deep book = cold secondary store
  ```

- **Orders at a level: an intrusive FIFO from a pool, not `std::list`.** Each price level is a **FIFO queue of resting orders** (price-time priority). Implement it as an **intrusive doubly-linked list** of `Order` objects drawn from an **object pool** (Ch. 24): adding an order is pop-from-pool + link-at-tail (a few instructions, no allocation); cancelling is unlink-given-the-order (O(1), no search — keep an `orderID → Order*`/index map, itself a *flat* hash map, §25.4.1); a fill walks from the head. Zero allocation on the hot path, O(1) add/cancel/match, and each order's neighbors are pool-contiguous (locality).
- **BBO vs deep-book asymmetry (the hot/cold split).** The overwhelming majority of activity is at or near the touch — BBO updates, adds/cancels in the top few levels — which the flat-array-near-the-top design makes O(1) and cache-hot. Deep-book modifications are rarer and can afford a slower path (the sparse secondary store). Optimize the structure for the *common* operation (top-of-book), not the worst case (arbitrary-depth insert) — and measure against captured data (Ch. 75) where the real distribution of touched levels lives.
- **Consolidating disparate feeds.** A consolidated book merges multiple venues'/feeds' updates (Ch. 53's A/B arbitration and multi-feed handling). Keep each feed's contribution attributable (so a feed drop/gap — Ch. 53 — can be backed out) while presenting one merged view; per-venue level arrays summed into a consolidated array, or per-order venue tags, with the same flat/intrusive/pool discipline so the merge stays allocation-free and cache-friendly.
- **What this buys.** The flat-array + intrusive-pool LOB turns per-message book updates from a parade of cache misses and `malloc`s (the `map<Price,list>` version) into a handful of contiguous, allocation-free operations — the difference between ~hundreds of ns and ~tens of ns per update, and a *flat* tail (Ch. 1) because there's no allocation or tree-rebalance to spike. It's the synthesis of the whole book so far: data-oriented layout (Ch. 8), no hot-path allocation (Ch. 23–24), flat containers (this chapter), cache/TLB locality (Ch. 7, 15).

## 25.5 Pitfalls & anti-patterns: `shared_ptr` refcount churn

- **`std::map`/`std::unordered_map` on the hot path.** The default reach — and a parade of cache misses + per-element `malloc` (§25.2-24.3). Use a flat/open-addressing map (§25.4.1) or `flat_map` (§25.4.2); reserve the rare justified `std::map` use (need stable addresses *and* ordered *and* insert-heavy) and know you're paying for it.
- **`shared_ptr` copies on the hot path.** Every copy is an **atomic** refcount bump; shared across cores, the refcount line ping-pongs (Ch. 33) — a serializing cost dwarfing the "pointer copy" it looks like. Pass by `const&`/raw/`unique_ptr`; only copy `shared_ptr` when transferring genuine shared ownership, off the hot path. `make_shared` to fuse the control-block allocation when you do use it.
- **Holding pointers into a flat/rehashing container.** Open-addressing and `vector`-backed containers **move elements** on rehash/grow/insert; a stored pointer/iterator dangles. Use **indices/handles** (§25.4.3) or reserve enough to never rehash on the hot path. (This is the trade for the locality win — design for it.)
- **Not `reserve`-ing → hot-path rehash/realloc.** A flat map or vector that rehashes/reallocates mid-stream is a latency spike (a big alloc + reinsert/move — Ch. 23). Size to the working set up front; keep growth on the cold path.
- **`vector<std::function>` / containers of type-erased callables.** Multiplies `std::function`'s double-indirection and possible allocation (Ch. 14) across every element. Use a concrete callable type, a `variant`, or function pointers (Ch. 14).
- **Intrusive without lifetime discipline.** Intrusive containers + pools mean *you* own lifetimes; an order freed to the pool while still linked, or linked into two lists with conflicting lifetimes, is UAF/corruption (Ch. 24, 72). Keep the ownership rules simple and explicit.
- **Choosing `flat_map` for insert-heavy data.** `flat_map`'s middle-insert is O(n) shifts (§25.4.2); for insert/erase-heavy *ordered* data it can lose to a tree. Match it to lookup/scan-heavy, mutation-light use — and measure.
- **Benchmarking with everything in L1.** A container comparison on a tiny dataset hides the cache-miss difference that *is* the point (§25.3). Test at the real working-set size (and with realistic, cache-cold access — Ch. 3), or you'll conclude node containers are "fine."
- **Ignoring the order book's access distribution.** Optimizing the LOB for worst-case arbitrary-depth ops instead of the real, top-of-book-dominated distribution (§25.4.4) wastes effort and may pessimize the common path. Profile against captured market data (Ch. 75).

## 25.6 Exercises & checklist

**Exercises**

1. **Flat vs chained.** Build `flatmap.cpp`; run `chained` vs `flat` pinned with `perf stat -e L1-dcache-load-misses,dtlb_load_misses`. Confirm the flat map's lower misses and ~3-4× speed. Sweep N from L1-sized to >L3 — how does the gap grow with working-set size?
2. **Library shootout.** Replace the toy flat map with `absl::flat_hash_map`, `ankerl::unordered_dense`, and `boost::unordered_flat_map`; compare against `std::unordered_map` on the same workload. Quantify the production gap.
3. **`shared_ptr` churn.** Hammer a `shared_ptr` copied/destroyed across two threads on different cores; measure throughput and `mem_load..._retired.remote_hitm`/contention (Ch. 16, 33) vs passing `const&`. How expensive is the "free pointer copy"?
4. **Build the LOB core.** Implement the §25.4.4 book: flat tick-indexed level arrays + intrusive pool-backed FIFOs + a flat `orderID→Order` map. Feed it captured (or synthetic) ITCH-like adds/cancels/executes; measure ns/update distribution and confirm zero hot-path allocation (`operator new` override — Ch. 23). Compare to a `std::map<Price,std::list<Order>>` baseline.
5. **`flat_map` crossover.** For an ordered map, find the insert-rate at which `std::flat_map` loses to `std::map` (shift cost vs locality). Where does each win (§25.4.2)?

**Checklist — cache-friendly containers**

- [ ] Hot-path maps/sets are **flat/open-addressing** (§25.4.1) or **`flat_map`/`flat_set`** (§25.4.2), not `std::map`/`std::unordered_map`; I justified any node container deliberately.
- [ ] Containers are **`reserve`d to the working set**; the hot path never **rehashes/reallocates** (Ch. 23); I hold **indices/handles**, not pointers, into relocating containers.
- [ ] **No `shared_ptr` copies** on the hot path (pass `const&`/raw/`unique_ptr`); `shared_ptr` is reserved for genuine off-hot-path shared ownership (`make_shared`).
- [ ] Per-element node allocation is eliminated via **intrusive containers + pools** (Ch. 24) and/or **small-buffer** types where payloads are usually small (§25.4.3).
- [ ] The **order book** uses flat tick-indexed level arrays + intrusive pool-backed FIFOs + a flat ID map, optimized for the **top-of-book-dominated** access distribution, with deep book as a cold secondary store (§25.4.4).
- [ ] **No `vector<std::function>`** / containers of type-erased callables on the hot path (Ch. 14).
- [ ] Intrusive/pool designs have **explicit, simple lifetime rules** (no linked-while-freed — Ch. 24, 72).
- [ ] Container choices are validated by **cache/TLB counters and the latency distribution** at the **real working-set size** against **captured data** (Ch. 3, 7, 75) — not a tiny in-L1 microbench.

## 25.7 References

- ISO C++ / cppreference — the complexity and interface guarantees of `std::map`/`std::unordered_map`/`std::list`/`std::shared_ptr`, and `std::flat_map`/`std::flat_set` (C++23); the bucket-interface mandate that forces chaining (§25.2).
- M. Kulukundis, *"Designing a Fast, Efficient, Cache-friendly Hash Table, Step by Step"* (CppCon, the Abseil SwissTable talk) — open addressing, SIMD probing, and why `std::unordered_map` is slow (§25.3-24.4.1).
- The Abseil (`flat_hash_map`), `ankerl::unordered_dense` (robin-hood), and Boost.Unordered (`unordered_flat_map`) documentation — production flat hash maps and their design trade-offs.
- The Boost.Intrusive and Boost.Container (`flat_map`, `small_vector`) documentation — intrusive containers and flat/small-buffer types (§25.4.3).
- The LMAX Disruptor paper and Martin Thompson's "Mechanical Sympathy" writings — cache-friendly data structures and the order-book/queue patterns motivating §25.4.4 (ties Ch. 37).

## 25.8 Additional Reading

- C. Cook, *"When a Microsecond Is an Eternity"* (CppCon) — hot-path container/allocation discipline in an HFT context, directly relevant to the LOB.
- Talks and writeups on building low-latency limit order books (various exchange/HFT engineering blogs) — concrete LOB data-structure designs extending §25.4.4.
- Ch. 8 (*Data-Oriented Design*) — the locality principles underneath; Ch. 24 (*Custom Allocators*) — the pools the LOB runs on; Ch. 28 (*Bit Manipulation*) — bitmaps for level occupancy/free-lists; Ch. 34/37 (*Lock-Free / Disruptor*) — ring buffers as containers; Ch. 53 (*Wire Decoding*) — the feed that drives the book.
- **Appendix E** — the cache-miss latency numbers that make "a cache miss per node" concrete; **Appendix F** — order-book/BBO terminology.

---

*Next: Ch. 26 — Memory Mapping, closing Part IV: `mmap`, shared memory, file-backed mappings, and the pre-faulting/`mlock` discipline that gives the containers and allocators of this Part their warm, resident, zero-fault backing memory.*
