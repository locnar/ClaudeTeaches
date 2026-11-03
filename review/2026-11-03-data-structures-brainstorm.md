# Note: esoteric high-performance data structures & algorithms to add

Status: **brainstorm only — not scheduled, not drafted.** Captured 2026-11-03 at the author's request.
Companion to the gap analysis (`2026-10-30-consistency-review-and-gap-analysis.md` §10); items below
reference existing G-rows where one already exists.

## 1. What the book covers today (data structures & algorithms)

| Where | Covered |
|---|---|
| Ch. 8 | AoS vs SoA, hot/cold field splitting, access-pattern-driven layout |
| Ch. 24 | Arena/bump allocators, object pools + free-lists, `std::pmr` resources |
| Ch. 25 | Open-addressing flat hash (linear probing, robin-hood), `std::flat_map`/`flat_set`, intrusive lists, SBO/small-vector, index handles (one bullet), LOB case study: tick-indexed level arrays + intrusive pool-backed FIFOs + flat `orderID→Order` map |
| Ch. 28 | Bitmaps/bitsets with `tzcnt`/`blsr`/`popcnt` (incl. a summary-word hierarchical bitmap for free-lists), branchless min/max/abs, power-of-two masks (libdivide is a reference only), field packing, Morton/Z-order |
| Ch. 34 | SPSC/MPMC queues, ring buffers, tagged pointers vs ABA, index-based designs |
| Ch. 35–37 | Seqlock publication; RCU / hazard pointers / epochs; Disruptor ring with sequence barriers and consumer batching, wrap protection by slowest consumer |
| Ch. 53–54 | Sequence-number dedup and A/B arbitration, software reorder ring buffers for UDP |
| Ch. 3, App. L | HdrHistogram for tail-latency recording |

Already proposed in the gap list (not yet written): **G39** slot maps + generational handles (partial),
**G36** hierarchical timing wheels vs heaps, **G56** LOB in depth (dense tick ladders with re-centering),
**G88** fast hashing incl. compile-time perfect hashing, **G89** branch-free parsing, **G90** branchless
binary search / Eytzinger / SIMD scan / sorting networks, **G52** streaming statistics + monotonic deque,
**G34** conflation slots + dirty bitmap, **G50** `std::inplace_vector` / `std::hive`, **G80** double-mapped
magic ring, **G92** O(1) bounded sampling.

## 2. Proposed additions (D01–D18)

Priority reflects hot-path payoff × how rarely the technique is taught. "Where" is a placement suggestion only.

| # | Topic | What is esoteric / why it pays | Where | Pri |
|---|---|---|---|---|
| D01 | **Slot vectors** (stable-slot dense vector: free-list threaded through a `union` in the dead slots, occupancy bitmap for hole-skipping iteration) vs **slot maps** (indirection table + packed values + erase-by-swap) vs **sparse sets** (Briggs–Torczon: O(1) insert/erase/membership/*clear*) vs `std::hive` skipfields | Four cousins with different trade-offs (iteration density vs pointer stability vs clear cost); generation tags turn use-after-free into one predictable compare | Widen G39 into a full §25.4.x or a new chapter after Ch. 25 | high |
| D02 | **Bounded-probe hash tables**: cuckoo (≤2 probes worst case, both buckets prefetched in parallel), hopscotch, SIMD-metadata groups (SwissTable control bytes: one 16-byte compare per group); note on 2025 "elastic hashing" | Tail latency is set by the *worst* probe chain, not the average; cuckoo makes the worst case a constant | Expand Ch. 25 §25.4.1 | high |
| D03 | **Compile-time perfect hashing** for a static instrument/tag universe (CHD, PTHash, RecSplit; `constexpr` builder) | One probe, no collision branch, table fits L1; G88 names it in one clause | Promote from G88 to its own section with a generator example | med |
| D04 | **Direct-address sliding tables for monotone keys**: ITCH order reference numbers and feed sequence numbers are near-monotone, so `(id - base) & (2^k-1)` into a ring-indexed array replaces the hash map entirely; sliding **sequence bitmap** for A/B dedup | Removes hashing from the two hottest lookups in a feed handler | Ch. 25 LOB case study + Ch. 53 §53.4.3 | high |
| D05 | **Predecessor search over a bounded integer universe**: the Ch. 28 summary bitmap generalised to a 64-ary van Emde Boas-style tree over price ticks; "best level ≥ p" in `log64(U)` `tzcnt`s; x-fast/y-fast as footnote | Beats sorted arrays and `flat_map` for best-bid/ask-above queries on deep books | Ch. 28 §28.4.1 + G56 | high |
| D06 | **Static search layouts beyond Eytzinger**: static B-tree / S+-tree with AVX-512 node compares, implicit layouts, prefetch-ahead | ~10× over `std::lower_bound` on large sorted keys; extends G90 | Fold into G90 | med |
| D07 | **Implicit-array trees for aggregates**: Fenwick tree (cumulative depth "volume at or better than p" in O(log n), no pointers), implicit segment tree for range min/max, SIMD prefix sums | Order-book analytics queries without walking levels | New §25.4.x or G56 | high |
| D08 | **Monotone priority queues for time**: radix heap (keys only increase: deadlines, timestamps), d-ary (4/8-ary) heaps sized to cache lines, calendar queue | Timers and deadline sets where a binary heap's sift is the wrong shape | Extend G36 | high |
| D09 | **k-way merge via loser (tournament) tree**: merge N time-ordered streams in `log N` compares per element with no heap sift | The sequencer's core (G32), multi-venue sequencing, multi-file/per-core journal replay (Ch. 75) | G32 / Ch. 75 | high |
| D10 | **Left-right concurrency control** (Ramalhete/Correia): two instances + versioned read-indicator; wait-free readers that never retry, blocking single writer, no reclamation | Fills the gap between seqlock (readers retry) and RCU (needs reclamation) for config/reference snapshots | Ch. 35 §35.4.x; cross-ref Ch. 36, Ch. 73 | high |
| D11 | **Flat combining / delegation**: one combiner applies a batch of posted operations for everyone | Beats CAS contention on shared structures; counterpoint to Ch. 34's MPMC | Ch. 34 or Ch. 32 | low |
| D12 | **Probabilistic structures sized for L1**: Bloom vs xor / binary-fuse / ribbon filters ("seen this ClOrdID?" at one cache line), cuckoo filter (deletes), count-min sketch (hot-symbol detection), HyperLogLog (cardinality in telemetry), DDSketch / t-digest / mergeable quantile sketches vs HdrHistogram | Memory-bounded answers with bounded error; mergeability fixes G04's cross-host percentile problem | New section; ties G01/G04 | high |
| D13 | **Succinct indexes for capture**: Elias–Fano for monotone sequence/timestamp indexes with O(1) `select`, rank/select bitvectors, frame-of-reference and delta-of-delta encodings | Time-indexed replay storage (Ch. 75) at a fraction of the memory, still random-access | Ch. 75 §75.4.x; ties G92 | med |
| D14 | **Adaptive radix tree (ART) and qp-tries** for variable-length keys (symbol strings, FIX tags, client order IDs) when the universe is not static; cache-line nodes, SIMD Node4/Node16 search | Pointer-light alternative when 8-byte packed symbols (G88) don't fit | Ch. 25 §25.4.x | med |
| D15 | **Compressed sparse row (CSR) fan-out tables**: instrument→subscribers and strategy→instruments as offset + flat index arrays; ≤64 subscribers as a `uint64` mask iterated with `tzcnt` | Replaces `vector<vector<>>`/`map<…,vector>` in the distribution layer | Ch. 25 or G35 | med |
| D16 | **SIMD algorithmic idioms as container ops**: stream compaction (`vpcompressd`), `movemask`+`pext` filtering, SIMD prefix-sum, vectorised histogram with conflict detection | Filtering a book or a message batch without a branch per element | Ch. 29 §29.4.x; overlaps G90 | low |
| D17 | **Fast division by runtime constants**: Granlund–Montgomery reciprocal-multiply (libdivide), Lemire's multiply-shift fast range replacing `%` in hash indexing; tick-size conversions with non-power-of-two ticks | Ch. 28 only cites libdivide in References | Ch. 28 §28.4.3 | med |
| D18 | **Deterministic iteration order** as a structural requirement: seeded hash iteration order breaks bit-exact replay; dense-index containers as the fix | Pitfall that ties data-structure choice to Ch. 74's determinism | Ch. 74 §74.5 / Ch. 25 §25.5 | low |

Considered and left out as too common or off-theme: skip lists, red-black/B-trees in general, HAMTs and
persistent structures, learned indexes, concurrent hash maps (hot paths here are single-writer).

## 3. Placement options (decide when scheduling)

- **Option A:** one new chapter in Part IV after Ch. 25, "Hot-Path Data Structures II" (D01, D02, D04, D05, D07, D14, D15), plus small expansions for the rest. Renumber cost: 51 chapters.
- **Option B:** no new chapter; distribute as new §N.4.x sections across Ch. 25, 28, 29, 34, 35, 75 and the pending G-items (G36, G39, G56, G88, G90). No renumbering, but Ch. 25 grows large.
- Either way: slot vectors (D01) should be written **before** G33 (in-flight order table) and G36 (timer nodes), which adopt it.
