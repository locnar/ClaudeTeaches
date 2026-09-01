# Part II — CPU Microarchitecture

# Chapter 8 — Cache-Aware & Data-Oriented Design

> **Prerequisites:** Ch. 7 (*The Memory Hierarchy & Caches*) — you know cache lines are 64 bytes, that you pay per line, that sequential access is prefetcher-friendly, and that DRAM is ~80× an L1 hit. Ch. 1–6 methodology throughout (distributions, PMU, honest benchmarks, a quiet box).
>
> **Leads into:** Ch. 9 (object layout, alignment & padding) makes the *individual struct* as small and line-friendly as this chapter makes the *collection*; Ch. 10 (software prefetch) is the fallback when layout alone can't help. The order-book case study (Ch. 25) is the full-scale version of §8.3–§8.4 here. SoA is also the enabling layout for SIMD (Ch. 29).

---

## 8.1 Why it matters: layout decides the miss rate

Ch. 7 established the costs; this chapter is about the **one design decision that controls how often you pay them: data layout.** Two programs with identical algorithms, identical instruction counts, and identical *logical* data can differ by 5–20× in wall-clock time purely because one arranges its data so the hot loop touches full cache lines of useful bytes and the other scatters the useful bytes across lines it mostly wastes. The algorithm is not the bottleneck; the **memory layout is the bottleneck**, and it is a thing you choose.

This is the core claim of **data-oriented design (DOD)**: *design your data structures around how the hot path actually accesses them, not around how the domain objects are conceptually organized.* Object-oriented modeling encourages you to bundle everything about an "order" — price, quantity, side, order-id, timestamp, owner, flags, callbacks — into one `struct Order`, because that's how a human thinks about an order. But the hot loop usually touches *one or two* of those fields across *thousands* of orders (e.g. "sum quantity at each price level," "find the best bid"). With the OO layout, each order the loop visits drags an entire 64-byte (or larger) object through the cache to use 8 bytes of it — **7/8 of every fetched line, and 7/8 of your cache, wasted.** DOD reorganizes the data so the loop touches contiguous, dense arrays of exactly the fields it needs.

The HFT stakes are concrete. A market-data feed handler updating an order book does the same few operations millions of times per second: apply an add/modify/cancel to a price level, recompute top-of-book. Whether the book's data is laid out so each update touches one resident cache line or chases pointers across DRAM (Ch. 7 §7.2.4) is, quite literally, the difference between a competitive feed handler and an also-ran — and it is a layout decision made before a single line of update logic is written. This chapter is how you make that decision, and §8.3 measures what it's worth.

The mental shift to internalize: **stop thinking "what is an order?" and start thinking "what does my hot loop read, in what order, and how do I make those bytes contiguous?"**

---

## 8.2 Mental model

### 8.2.1 Array-of-Structs vs Struct-of-Arrays

The canonical DOD decision, and the one you'll make most often. Suppose an order book level (simplified) holds a price, a total quantity, an order count, and a pile of cold metadata:

```cpp
struct Level {                 // ~64+ bytes once you add the cold fields
    std::int64_t price;        // hot: read on almost every access
    std::int64_t total_qty;    // hot: updated on every add/cancel
    std::uint32_t order_count; // hot-ish
    std::uint64_t last_update_ts;   // cold: logging/audit
    std::uint32_t venue_id;         // cold
    char          symbol[16];       // cold
    // ... more cold metadata ...
};
```

**Array-of-Structs (AoS)** — `std::vector<Level>` — stores each complete `Level` contiguously: `[price,qty,...cold...][price,qty,...cold...]...`. Natural, and *correct when the hot path touches whole objects one at a time* (random access to a single level). But a loop that scans `total_qty` across all levels strides through memory by `sizeof(Level)`, touching one useful field per 64-byte line and dragging all the cold fields through cache:

```
AoS in memory (each box a Level; loop wants only qty):
  [P Q ....cold....][P Q ....cold....][P Q ....cold....]
     ^used            ^used            ^used
   one cache line per Level, ~7/8 wasted, stride = sizeof(Level)
```

**Struct-of-Arrays (SoA)** — separate arrays per field — stores all prices together, all quantities together, etc.:

```cpp
struct Book {
    std::vector<std::int64_t> price;       // all prices, contiguous
    std::vector<std::int64_t> total_qty;   // all quantities, contiguous
    std::vector<std::uint32_t> order_count;
    // cold fields live in their own arrays (or a separate cold struct, §7.2.2)
};
```

Now the "sum all quantities" loop walks `total_qty` as a dense, contiguous array — **every byte of every fetched line is useful, the stride is the prefetcher's ideal (Ch. 7 §7.2.4), and the effective footprint shrinks from `N×sizeof(Level)` to `N×8 bytes`:**

```
SoA in memory (loop wants only qty):
  price:  [P P P P P P P P ...]
  qty:    [Q Q Q Q Q Q Q Q ...]   <- loop walks this, 100% of each line useful
```

The rule is **access-pattern-driven**: AoS wins when you touch *whole objects, one or a few at a time* (random-access lookups); SoA wins when you touch *one or a few fields across many objects* (bulk scans, the book-update sweep, anything you want to vectorize — Ch. 29). Most hot loops are the latter; most naïve code is the former. SoA is also frequently a prerequisite for SIMD, because a vector load wants 8 adjacent `qty` values, not 8 `qty` fields scattered across 8 fat structs.

### 8.2.2 Hot/cold field splitting

SoA is the extreme; **hot/cold splitting** is the pragmatic middle ground you'll reach for constantly. The observation: within one logical object, some fields are touched on the hot path (`price`, `total_qty`) and others almost never (`symbol`, `last_update_ts`, audit/logging metadata). Mixing them means every hot access pays to fetch cold bytes it won't use — inflating the per-object footprint past a cache line and slashing how many hot objects fit in cache.

The fix: **split the struct into a hot part and a cold part**, linked by index or pointer:

```cpp
struct LevelHot {              // small, dense, cache-line-friendly
    std::int64_t price;
    std::int64_t total_qty;
    std::uint32_t order_count;
};                             // ~20 bytes -> 3 per cache line

struct LevelCold {             // touched only on audit/logging/rare paths
    std::uint64_t last_update_ts;
    std::uint32_t venue_id;
    char          symbol[16];
    // ...
};

std::vector<LevelHot>  hot;    // the hot path scans/indexes only this
std::vector<LevelCold> cold;   // parallel array; hot[i] <-> cold[i]
```

Now the hot loop's working set is *only the hot fields*, so several levels fit per cache line and far more fit in L1/L2 — the per-event footprint (Ch. 7 §7.4.1) drops by the size of the cold tail. The cold data still exists, still indexed in lockstep, just not dragged through cache on every update. This is often **higher-leverage and lower-risk than full SoA**: it keeps related hot fields together (good when the hot path uses several of them per access) while evicting the dead weight. The decision is the same question as AoS/SoA, asked at field granularity: *which fields does the hot path actually touch, and how do I make only those contiguous and dense?*

---

## 8.3 Measure it: AoS vs SoA cache-miss comparison on an order book

Let's put a number on it with the running order-book example: scan every level and sum `total_qty` (a stand-in for "recompute aggregate depth," a real per-event operation), comparing the AoS and SoA layouts on identical data.

```cpp
// aos_vs_soa.cpp — same sum, two layouts. Profile both with perf stat.
// Build: g++ -O2 -std=c++20 -march=native aos_vs_soa.cpp -o aos_vs_soa
// Run pinned, turbo off (Ch.3,5):  taskset -c 2 ./aos_vs_soa aos   |   ... soa
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>

struct Level {                 // a deliberately fat, OO-style level
    std::int64_t price;
    std::int64_t total_qty;    // <- the only field the hot loop reads
    std::uint32_t order_count;
    std::uint64_t last_update_ts;
    std::uint32_t venue_id;
    char          symbol[16];
    char          pad[8];      // make it a round 64 bytes for the demo
};                             // sizeof(Level) == 64

int main(int argc, char** argv) {
    const bool soa = (argc > 1 && std::strcmp(argv[1], "soa") == 0);
    constexpr std::size_t N = 4u * 1024 * 1024;   // 4M levels; AoS = 256 MiB, way past L3
    constexpr int PASSES = 20;

    std::int64_t result = 0;
    if (!soa) {
        std::vector<Level> book(N);
        for (std::size_t i = 0; i < N; ++i) book[i].total_qty = (std::int64_t)i;
        for (int p = 0; p < PASSES; ++p)
            for (std::size_t i = 0; i < N; ++i) result += book[i].total_qty;  // AoS: stride 64
    } else {
        std::vector<std::int64_t> total_qty(N);   // SoA: just the hot field
        for (std::size_t i = 0; i < N; ++i) total_qty[i] = (std::int64_t)i;
        for (int p = 0; p < PASSES; ++p)
            for (std::size_t i = 0; i < N; ++i) result += total_qty[i];        // SoA: dense
    }
    std::printf("%s result=%lld\n", soa ? "soa" : "aos", (long long)result);
    return 0;
}
```

Profile each with `perf stat -e cycles,instructions,L1-dcache-load-misses,LLC-load-misses` (Ch. 2). Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP), GCC 13 `-O2 -march=native`, pinned, turbo off (illustrative; reproduce on your box):

```
                          AoS (stride 64)        SoA (dense 8B)
wall time (20 passes)        ~1.9 s                ~0.16 s          <- ~12x faster
instructions             ~same                  ~same              (same sum)
L1-dcache-load-misses     very high              ~1/8 of AoS
LLC-load-misses           very high              much lower (prefetched)
bytes touched / pass      256 MiB (all of it)    32 MiB (qty only)  <- 8x less traffic
```

Read it through Ch. 7:

- **Same work, ~12× the speed.** Instruction counts match; the only difference is layout. AoS strides by `sizeof(Level)=64`, so it touches *one* useful 8-byte field per 64-byte line — fetching the whole 256 MiB book to use 32 MiB of it. SoA walks a dense 32 MiB array, every byte useful.
- **The miss rate is the story.** AoS misses ~8× more in L1 (one miss per element vs one per 8) and can't lean on the prefetcher as effectively because each line yields only one datum. SoA's dense sequential walk is the prefetcher's ideal (Ch. 7 §7.2.4), so it runs near memory-bandwidth speed.
- **The win is bandwidth and footprint, not cleverness.** SoA moved 8× fewer bytes and let the prefetcher hide the latency. No algorithmic change — just "make the bytes the loop needs contiguous."

This is the order-book case study (Ch. 25) in miniature, and it generalizes: **any per-event bulk scan over a fat struct is paying this 8×–ish tax until you split the hot field out.**

---

## 8.4 Techniques

### 8.4.1 Access-pattern–driven layout

The master technique, of which everything else here is a special case: **choose layout from the dominant hot-path access pattern, measured — not from the domain model.** The procedure:

1. **Identify the hot loops** (Ch. 2 — profile, don't guess) and, for each, list *exactly which fields it reads/writes and in what order*.
2. **Classify the access**: whole-object random access (→ AoS / keep together) vs single-field-across-many (→ SoA / split out) vs a hot cluster of fields with a cold tail (→ hot/cold split, §8.4.2).
3. **Lay out for the *dominant* pattern**, and accept that a secondary pattern may pay for it. Most structures have one hot loop that matters; optimize for it. If two patterns are both hot and conflict, you may keep two views (a denormalized copy — costs memory and update work, buys locality), but only when measurement justifies it.
4. **Measure the footprint and MPKI** (Ch. 7 §7.4.1, Ch. 2) before and after; keep the change only if the cache counters and the *tail* (Ch. 1) improved on a production-sized working set.

The discipline is *empirical*: layout is a hypothesis you test with `perf`, not a style you adopt. The right answer is workload-specific — which is exactly why §8.5 warns against applying SoA reflexively.

### 8.4.2 Splitting hot fields from cold metadata

The highest-ROI, lowest-risk move in practice (§8.2.2): pull the rarely-touched fields out of the hot struct so the hot working set shrinks to a cache line or less. Practical notes:

- **Find the cold fields** by asking, per field, "does a hot-path event read this?" Logging timestamps, audit ids, human-readable symbols, debug flags, error strings, and back-references are almost always cold.
- **Keep the hot struct dense and ideally ≤ one cache line** (Ch. 9 will help you measure and pack it). Several hot objects per line is the goal.
- **Link hot↔cold by index, not pointer**, when they live in parallel arrays — an index is smaller than a 8-byte pointer, doesn't defeat the prefetcher, and survives reallocation (Ch. 25). A pointer reintroduces exactly the chase you're trying to avoid.
- **C++23 niceties:** `std::flat_map`/`std::flat_set` (Ch. 25) give you contiguous, cache-friendly ordered storage out of the box where you'd otherwise reach for a node-based `std::map`; pair them with hot/cold-split value types.

This is often where you get 80% of the SoA benefit for 20% of the disruption — the hot loop sees only hot bytes, but you didn't have to shatter every field into its own array or rewrite every accessor.

### 8.4.3 SoA for the book-update loop

When the hot path is a *bulk sweep* — recompute aggregate depth, find best bid/ask across levels, apply a vectorized transform — go full **SoA** on the swept fields, as §8.3 showed. Guidance specific to the order-book update loop (full treatment in Ch. 25):

- **Store the swept field(s) as dense parallel arrays** indexed by price level (or a compact level index), so the sweep is a unit-stride scan the prefetcher devours.
- **SoA unlocks SIMD** (Ch. 29): a contiguous `total_qty[]` can be summed/compared 4–8 lanes at a time; the same field buried in fat structs cannot. If you intend to vectorize the book-update or risk-aggregation loop, SoA is not optional.
- **Keep the *random-access* path in mind.** A feed handler also does point updates ("modify the qty at *this* price"), which is a single-element access where SoA and AoS are similar — but SoA still wins because the touched arrays are denser (more levels per cache line). The sweep dominates, so SoA is the right call; verify with the per-event footprint.
- **Mind update locality:** if an add/cancel updates several fields of *one* level together, pure SoA scatters that update across N arrays (N lines touched per point-update). That's the tension §8.5 names — measure whether the sweep savings outweigh the point-update cost for *your* feed. Often a hot/cold split (§8.4.2) with the few co-updated hot fields kept together is the sweet spot.

---

## 8.5 Pitfalls & anti-patterns: premature SoA, scattered hot data

- **Premature / dogmatic SoA.** SoA is not universally faster — it's faster for *single-field-across-many* access. Applied to a workload that touches whole objects one at a time (random point lookups using several fields), SoA *scatters* one logical object across N arrays, turning one cache line into N line touches — strictly worse. Don't adopt SoA as a style; adopt it where the *measured* dominant access pattern is a field sweep (§8.4.1). The right question is always "what does the hot loop touch?"
- **Scattered hot data.** The dual mistake: leaving genuinely hot fields spread across a fat struct (or across pointer-linked nodes) so the hot loop chases lines. Symptom: high L1/LLC MPKI (Ch. 2) on a "small" logical working set — the *effective* footprint is many times the useful bytes. Fix: hot/cold split (§8.4.2) or SoA the swept fields.
- **Re-introducing pointers between hot and cold.** Splitting hot from cold but linking them with a pointer you then chase on the hot path re-creates the dependent-load miss you were avoiding (Ch. 7 §7.2.4). Use indices into parallel arrays.
- **Ignoring update/write patterns.** Optimizing the read sweep into SoA while the hot path *also* does point updates that co-modify several fields — now each update touches N arrays (N lines). Layout must serve *all* the hot accesses weighted by frequency, not just the one you measured first.
- **AoS with hidden bloat.** A struct that grew cold fields over time until the hot loop's per-object footprint quietly exceeded a cache line, with no one noticing. Periodically re-measure `sizeof` and per-event footprint (Ch. 9); growth here silently raises your miss rate.
- **Optimizing layout you never measured / unrepresentative working set.** Restructuring for cache on a benchmark whose data fits in L1 (so layout doesn't matter) and concluding "no difference," then shipping to production where it's 256 MiB. Size the working set to production (Ch. 3 §3.2.1) before judging a layout change — exactly the Ch. 7 trap.
- **Trading correctness/clarity for an unmeasured win.** SoA and hot/cold splitting add parallel-array invariants (indices must stay in lockstep; resize carefully). That complexity is worth it when measured, and pure overhead when not. Keep the OO layout for cold/control code (Ch. 1's tiering); spend DOD complexity only on the hot path.

---

## 8.6 Exercises & checklist

**Exercises**

1. **Reproduce the 8×.** Build `aos_vs_soa.cpp`, run both modes pinned with turbo off, and `perf stat -e cycles,instructions,L1-dcache-load-misses,LLC-load-misses` each. What's the speedup, and what's the ratio of L1 misses? Confirm the bytes-touched-per-pass argument (256 MiB vs 32 MiB).
2. **Find the break-even.** Shrink `N` until the AoS book fits in L3, then L2, then L1. At what working-set size does the AoS/SoA gap disappear? Explain via Ch. 7 (when everything's in L1, layout barely matters — and why that's a benchmarking trap).
3. **Hot/cold split.** Add several cold fields to a hot struct and write a loop that uses one hot field. Measure. Now split hot/cold into parallel arrays indexed in lockstep and re-measure footprint and MPKI. How much of the SoA win did the simpler split capture?
4. **Make the point-update pay.** Write the *other* access pattern — a loop that, for random levels, updates `price`, `total_qty`, and `order_count` together. Compare AoS vs full SoA for *this* loop. Which wins now, and why? Reconcile with exercise 1's verdict (§8.5).
5. **SoA enables SIMD.** Sum `total_qty` over the SoA array at `-O2 -march=native` and read the asm (Ch. 4) — is it vectorized (`ymm`/`vpaddq`)? Now try to get the compiler to vectorize the *AoS* sum. Can it? Why is SoA the enabler (Ch. 29)?

**Checklist — data-oriented layout**

- [ ] I identified the **hot loops** (profiled, Ch. 2) and listed the **exact fields** each touches and in what order.
- [ ] I classified the access: **whole-object** (→ keep together / AoS) vs **field-across-many** (→ SoA) vs **hot cluster + cold tail** (→ hot/cold split).
- [ ] I laid out for the **dominant, measured** pattern — not the domain model, not a style.
- [ ] The hot working set is **dense and cache-line-friendly** (hot/cold split applied; cold metadata evicted from the hot struct).
- [ ] Hot↔cold and level links are **indices, not chased pointers**.
- [ ] If the hot path also does **point updates**, the layout serves those too (co-updated hot fields kept together).
- [ ] I verified with **PMU cache counters and per-event footprint** on a **production-sized** working set, and the **tail** improved (Ch. 1–3, 7) — not just the benchmark mean.
- [ ] I kept DOD complexity on the **hot path only**; cold/control code stays clear and OO (Ch. 1 tiering).

---

## 8.7 References

- M. Acton, *"Data-Oriented Design and C++,"* CppCon 2014 — the foundational talk; "the purpose of the program is to transform data," and why layout follows access.
- R. Fabian, *Data-Oriented Design* (book and dataorienteddesign.com) — the most complete written treatment of AoS/SoA, hot/cold splitting, and existence-based processing.
- U. Drepper, *"What Every Programmer Should Know About Memory"* — §7's reference, and the source for why per-line, per-stride access dominates layout decisions.
- Intel, *64 and IA-32 Architectures Optimization Reference Manual* — data-layout and prefetcher guidance, and the SoA-for-SIMD recommendation (Ch. 29).
- N. Llopis, *"Data-Oriented Design (Or Why You Might Be Shooting Yourself in the Foot With OOP),"* 2009 — an early, influential statement of the layout-first mindset.

## 8.8 Additional Reading

- S. Meyers, *"CPU Caches and Why You Care"* (talk) — an accessible bridge from Ch. 7's hardware to this chapter's layout decisions.
- The EnTT / modern ECS literature and `std::flat_map`/`std::flat_set` proposals (P0429, P1222) — contiguous, cache-friendly containers that operationalize §8.4.2 (Ch. 25).
- Ch. 9 (*Object Layout, Alignment & Padding*) — sizing and packing the hot struct you just decided to split; Ch. 29 (*SIMD*) — why SoA is the layout vectorization wants.
- **Appendix E** — the per-line/per-miss costs that make the §8.3 bandwidth argument quantitative; the order-book case study in **Ch. 25** for the production-scale version.

---

*Next: Ch. 9 — Object Layout, Alignment & Padding, where we shrink and align the individual hot struct: member ordering, `alignof`/`alignas`, `[[no_unique_address]]`, and inspecting the real layout with `pahole` so silent padding doesn't bloat the footprint we just worked to minimize.*
