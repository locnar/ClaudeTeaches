# Low-Latency C++ on Linux — Table of Contents & Topic Index

A book-length tutorial series on high-performance, low-latency software in **C++ on Linux**, framed around **HFT / electronic trading** (order books, market-data feeds, tick-to-trade). This file is the reader's map: each chapter expands into its standard sections (§*N*.1 *Why it matters* → §*N*.2 *Mental model* → §*N*.3 *Measure it* → §*N*.4 *Techniques* → §*N*.5 *Verify the codegen* → §*N*.6 *Pitfalls* → §*N*.7 *Exercises*), with the searchable sub-topics listed under **Mental model** and **Techniques**.

> **Reading the index:** a **linked** chapter title points at its file (`NN-slug.md`). The 76 chapters are grouped into twelve Parts; seven appendices (A–G) follow.

---

### [Preface — How to read this book](00-preface.md)
- The north-star (tail latency, not average throughput), who the book is for, environment assumptions, how the Parts and per-chapter sections are organized, and the benchmark-numbers caveat.

---

## Part I — Foundations & Methodology

*Latency thinking and the measurement discipline the rest of the book stands on.*

### [1. The Latency Mindset](01-the-latency-mindset.md)
- 1.1 Why it matters: when a microsecond is the trade
- 1.2 Mental model
  - 1.2.1 Latency vs throughput — two different objectives
  - 1.2.2 The shape of the distribution: percentiles, tails, jitter
  - 1.2.3 The tick-to-trade budget, broken down by stage
- 1.3 Measure it: percentiles vs the mean on a real latency sample
- 1.4 Techniques
  - 1.4.1 Reasoning about p99/p99.9 instead of averages
  - 1.4.2 Budgeting latency across the pipeline
  - 1.4.3 Identifying and attacking sources of jitter
- 1.5 Pitfalls & anti-patterns: why means lie; throughput-driven design
- 1.6 Exercises & checklist

### [2. Measure First: Profiling & Hardware Performance Counters](02-measure-first-profiling-pmu.md)
- 2.1 Why it matters: you can't optimize what you can't see
- 2.2 Mental model
  - 2.2.1 The PMU and `perf_events`
  - 2.2.2 Top-down microarchitecture analysis
  - 2.2.3 Sampling vs counting
- 2.3 Measure it: a guided `perf stat` / `perf record` session
- 2.4 Techniques
  - 2.4.1 IPC, cache-miss, and branch-mispredict counters
  - 2.4.2 Flame graphs for hot-path attribution
  - 2.4.3 VTune for microarchitectural drill-down
- 2.5 Pitfalls & anti-patterns: skid, multiplexed counters, profiling the wrong build
- 2.6 Exercises & checklist

### [3. Micro-benchmarking Done Right](03-microbenchmarking-done-right.md)
- 3.1 Why it matters: bad benchmarks lie confidently
- 3.2 Mental model
  - 3.2.1 What a microbenchmark actually measures
  - 3.2.2 Coordinated omission and why it hides the tail
- 3.3 Measure it: Google Benchmark harness setup
- 3.4 Techniques
  - 3.4.1 Defeating dead-code elimination (`DoNotOptimize` / `ClobberMemory`)
  - 3.4.2 Warm-up, frequency scaling, and pinning
  - 3.4.3 Tail-latency capture with HdrHistogram
  - 3.4.4 Statistical rigor: repetitions, variance, regression
- 3.5 Pitfalls & anti-patterns: measuring the optimizer, not the code
- 3.6 Exercises & checklist

### [4. Reading the Machine: Assembly & Compiler Output](04-reading-the-machine-asm.md)
- 4.1 Why it matters: trust, then verify the optimizer
- 4.2 Mental model
  - 4.2.1 The x86-64 register and instruction model you need
  - 4.2.2 How source maps to asm under optimization
- 4.3 Measure it: the Compiler Explorer (Godbolt) workflow
- 4.4 Techniques
  - 4.4.1 Reading the hot loop in asm
  - 4.4.2 Confirming inlining, vectorization, and elision
  - 4.4.3 Diffing codegen across flags/compilers
- 4.5 Pitfalls & anti-patterns: debug-build asm, reading without `-O2`
- 4.6 Exercises & checklist

### [5. Debugging Low-Latency & Optimized Code](05-debugging-low-latency-optimized-code.md)
- 5.1 Why it matters: you can't `printf` your way out of a nanosecond bug
- 5.2 Mental model: optimized code, the observer effect, and reproduce-then-debug
- 5.3 Measure it: reproducing and capturing the bug
- 5.4 Techniques
  - 5.4.1 Debugging optimized and LTO builds
  - 5.4.2 Record/replay, reverse debugging, and core-dump analysis
  - 5.4.3 Production tracing without perturbing the hot path
- 5.5 Pitfalls & anti-patterns: the observer effect and optimized-out state
- 5.6 Exercises & checklist

### [6. System Setup for Low Latency](06-system-setup-for-low-latency.md)
- 6.1 Why it matters: the box is part of the program
- 6.2 Mental model
  - 6.2.1 C-states, P-states, turbo, and frequency scaling
  - 6.2.2 Hyperthreading and CPU topology
  - 6.2.3 Speculative-execution mitigations (Spectre/Meltdown/retpoline)
- 6.3 Measure it: jitter before/after tuning
- 6.4 Techniques
  - 6.4.1 BIOS/firmware tuning for determinism
  - 6.4.2 Topology discovery (`lscpu`, `hwloc`)
  - 6.4.3 Building a quiet machine; selectively disabling mitigations on isolated boxes
- 6.5 Pitfalls & anti-patterns: turbo-induced variance, noisy neighbors
- 6.6 Exercises & checklist

## Part II — CPU Microarchitecture

*The hardware reality under all the code — caches, pipelines, branches, the TLB, NUMA, and time.*

### [7. The Memory Hierarchy & Caches](07-memory-hierarchy-and-caches.md)
- 7.1 Why it matters: DRAM is ~200× an L1 hit
- 7.2 Mental model
  - 7.2.1 Latencies by level (L1/L2/L3/DRAM)
  - 7.2.2 Cache lines, sets, and associativity
  - 7.2.3 Coherence and the MESI protocol
  - 7.2.4 The hardware prefetchers
- 7.3 Measure it: a latency-vs-stride / cache-size sweep
- 7.4 Techniques
  - 7.4.1 Sizing the working set to a cache level
  - 7.4.2 Stride and locality for prefetcher friendliness
- 7.5 Pitfalls & anti-patterns: conflict misses, pointer chasing
- 7.6 Exercises & checklist

### [8. Cache-Aware & Data-Oriented Design](08-cache-aware-data-oriented-design.md)
- 8.1 Why it matters: layout decides the miss rate
- 8.2 Mental model
  - 8.2.1 Array-of-Structs vs Struct-of-Arrays
  - 8.2.2 Hot/cold field splitting
- 8.3 Measure it: AoS vs SoA cache-miss comparison on an order book
- 8.4 Techniques
  - 8.4.1 Access-pattern–driven layout
  - 8.4.2 Splitting hot fields from cold metadata
  - 8.4.3 SoA for the book-update loop
- 8.5 Pitfalls & anti-patterns: premature SoA, scattered hot data
- 8.6 Exercises & checklist

### [9. Object Layout, Alignment & Padding](09-object-layout-alignment-padding.md)
- 9.1 Why it matters: silent padding bloats the cache footprint
- 9.2 Mental model
  - 9.2.1 The C++/ABI object model: member ordering and struct padding
  - 9.2.2 `alignof`/`alignas`; natural vs over-alignment (SIMD, cache-line)
  - 9.2.3 `[[no_unique_address]]` and empty-base optimization
  - 9.2.4 Bit-fields and their codegen
- 9.3 Measure it: inspecting layout with `pahole` and `-Wpadded`
- 9.4 Techniques
  - 9.4.1 Reordering members to eliminate padding
  - 9.4.2 Sizing a struct to a cache line
  - 9.4.3 Packing trade-offs (`#pragma pack`) vs misaligned-access cost
- 9.5 Verify the codegen: aligned vs packed access
- 9.6 Pitfalls & anti-patterns: misaligned atomics, false economy of packing
- 9.7 Exercises & checklist

### [10. Software Prefetching & Non-Temporal Stores](10-software-prefetching-nontemporal-stores.md)
- 10.1 Why it matters: hiding miss latency the prefetcher won't
- 10.2 Mental model
  - 10.2.1 `__builtin_prefetch` / `_mm_prefetch` and locality hints
  - 10.2.2 Prefetch distance and timeliness
  - 10.2.3 Streaming (non-temporal) stores and the write path
- 10.3 Measure it: prefetch-distance sweep on a linked traversal
- 10.4 Techniques
  - 10.4.1 Manual prefetch for irregular access
  - 10.4.2 Non-temporal stores to avoid cache pollution
- 10.5 Verify the codegen: prefetch and `movnt` emission
- 10.6 Pitfalls & anti-patterns: when manual prefetch hurts; prefetch the wrong line
- 10.7 Exercises & checklist

### [11. CPU Pipelines & Execution](11-cpu-pipelines-and-execution.md)
- 11.1 Why it matters: the core is doing more than one thing at once
- 11.2 Mental model
  - 11.2.1 Superscalar, out-of-order execution
  - 11.2.2 Structural / data / control hazards
  - 11.2.3 Dependency chains and critical paths
- 11.3 Measure it: IPC and port pressure for a dependent chain
- 11.4 Techniques
  - 11.4.1 Breaking dependency chains for ILP
  - 11.4.2 Unrolling and accumulator splitting
- 11.5 Verify the codegen: latency vs throughput of the hot loop
- 11.6 Pitfalls & anti-patterns: serializing on a single accumulator
- 11.7 Exercises & checklist

### [12. Instruction Cache, Front-End Stalls & Code Layout](12-instruction-cache-frontend-code-layout.md)
- 12.1 Why it matters: the front-end can starve a fast back-end
- 12.2 Mental model
  - 12.2.1 L1i, the µop cache (DSB), I-TLB, fetch/decode bandwidth
  - 12.2.2 Front-end-bound stalls in top-down analysis
- 12.3 Measure it: front-end-bound counters; DSB coverage
- 12.4 Techniques
  - 12.4.1 Taming code bloat from over-inlining
  - 12.4.2 Hot/cold code splitting; `[[likely]]`/`[[unlikely]]`, `__builtin_expect`
  - 12.4.3 Function/section placement and `-freorder-blocks-and-partition`
  - 12.4.4 Linker ordering, PGO and BOLT for I-cache locality
- 12.5 Verify the codegen: hot/cold section placement
- 12.6 Pitfalls & anti-patterns: inlining everything; cold code on the hot path
- 12.7 Exercises & checklist

### [13. Branch Prediction & Branchless Programming](13-branch-prediction-branchless.md)
- 13.1 Why it matters: a mispredict costs ~15–20 cycles
- 13.2 Mental model
  - 13.2.1 Predictor behavior and history
  - 13.2.2 Misprediction cost and the pipeline flush
- 13.3 Measure it: branch-mispredict rate on data-dependent branches
- 13.4 Techniques
  - 13.4.1 Conditional moves vs branches
  - 13.4.2 Lookup tables to replace control flow
  - 13.4.3 Removing branches from the hot path
- 13.5 Verify the codegen: `cmov` vs branch emission
- 13.6 Pitfalls & anti-patterns: branchless code that's slower; data-dependent stalls
- 13.7 Exercises & checklist

### [14. Indirect Calls, Virtual Dispatch & Devirtualization](14-indirect-calls-virtual-dispatch.md)
- 14.1 Why it matters: hidden indirect branches on the dispatch path
- 14.2 Mental model
  - 14.2.1 vtables and the cost of `virtual`
  - 14.2.2 Indirect-branch prediction (BTB) and mispredict cost
  - 14.2.3 `std::function` and type-erasure overhead
- 14.3 Measure it: virtual vs CRTP dispatch microbenchmark
- 14.4 Techniques
  - 14.4.1 `final`, speculative and profile-guided devirtualization
  - 14.4.2 CRTP and static polymorphism
  - 14.4.3 `std::variant`/visitor alternatives
  - 14.4.4 Jump tables vs branch chains
- 14.5 Verify the codegen: devirtualized call sites
- 14.6 Pitfalls & anti-patterns: megamorphic call sites; `std::function` in the loop
- 14.7 Exercises & checklist

### [15. Virtual Memory, the TLB & Huge Pages](15-virtual-memory-tlb-huge-pages.md)
- 15.1 Why it matters: TLB misses tax every memory access
- 15.2 Mental model
  - 15.2.1 Address translation and the page-table walk
  - 15.2.2 TLB structure and miss cost
- 15.3 Measure it: `dtlb_load_misses` with and without huge pages
- 15.4 Techniques
  - 15.4.1 Transparent vs explicit huge pages
  - 15.4.2 Reducing TLB pressure via layout
- 15.5 Pitfalls & anti-patterns: THP stalls, fragmentation
- 15.6 Exercises & checklist

### [16. NUMA Architecture](16-numa-architecture.md)
- 16.1 Why it matters: cross-socket access is a hidden cliff
- 16.2 Mental model
  - 16.2.1 Node locality and the interconnect
  - 16.2.2 First-touch allocation policy
- 16.3 Measure it: local vs remote DRAM latency with `numactl`
- 16.4 Techniques
  - 16.4.1 Allocation policy and binding
  - 16.4.2 NUMA-aware data structures and thread placement
- 16.5 Pitfalls & anti-patterns: accidental remote allocation; cross-node sharing
- 16.6 Exercises & checklist

### [17. Timekeeping: TSC, rdtsc & Clock Sources](17-timekeeping-tsc-rdtsc-clocks.md)
- 17.1 Why it matters: you measure nanoseconds, so the clock matters
- 17.2 Mental model
  - 17.2.1 Invariant TSC; `rdtsc` vs `rdtscp` and serialization
  - 17.2.2 Clock sources and their cost
- 17.3 Measure it: TSC calibration against a reference clock
- 17.4 Techniques
  - 17.4.1 Reliable nanosecond timestamping on the hot path
  - 17.4.2 On-host hardware timestamping
- 17.5 Pitfalls & anti-patterns: unserialized `rdtsc`, frequency drift
- 17.6 Exercises & checklist

## Part III — Compile-Time & Language Mechanics

*Moving work to compile time and paying for abstractions only when they cost.*

### [18. Compile-Time Mechanics](18-compile-time-mechanics.md)
- 18.1 Why it matters: work done at compile time is free at runtime
- 18.2 Mental model: `constexpr` vs `consteval`; the template cost model
- 18.3 Measure it: build-time vs run-time trade-off
- 18.4 Techniques
  - 18.4.1 Moving computation to compile time
  - 18.4.2 `constexpr` tables and lookup generation
- 18.5 Pitfalls & anti-patterns: compile-time blowup, code bloat
- 18.6 Exercises & checklist

### [19. Template Metaprogramming & Zero-Cost Abstractions](19-template-metaprogramming-zero-cost.md)
- 19.1 Why it matters: abstraction without the runtime tax
- 19.2 Mental model: CRTP, type-based dispatch, policy-based design
- 19.3 Measure it: abstraction overhead vs hand-written code
- 19.4 Techniques
  - 19.4.1 CRTP for static polymorphism
  - 19.4.2 Expression templates
  - 19.4.3 Policy-based design
- 19.5 Verify the codegen: zero-overhead confirmation
- 19.6 Pitfalls & anti-patterns: template bloat, error-message cost
- 19.7 Exercises & checklist

### [20. The Cost of Abstractions](20-the-cost-of-abstractions.md)
- 20.1 Why it matters: error handling on the hot path
- 20.2 Mental model: exception unwinding, RTTI, `noexcept`
- 20.3 Measure it: exceptions vs error codes vs `std::expected`
- 20.4 Techniques
  - 20.4.1 `noexcept` and the happy path
  - 20.4.2 `std::expected` for hot-path errors
  - 20.4.3 Disabling RTTI where unused
- 20.5 Verify the codegen: unwinding tables and landing pads
- 20.6 Pitfalls & anti-patterns: throwing on the hot path
- 20.7 Exercises & checklist

### [21. Aliasing, `restrict` & Type Punning](21-aliasing-restrict-type-punning.md)
- 21.1 Why it matters: aliasing assumptions gate optimization
- 21.2 Mental model: strict-aliasing rules; how aliasing blocks vectorization/reordering
- 21.3 Measure it: codegen difference with/without `__restrict__`
- 21.4 Techniques
  - 21.4.1 `__restrict__` to unblock the optimizer
  - 21.4.2 `std::bit_cast` and `std::launder` for safe punning
- 21.5 Verify the codegen: vectorization gated by aliasing
- 21.6 Pitfalls & anti-patterns: UB type punning via casts/unions
- 21.7 Exercises & checklist

### [22. Build Toolchain for Speed](22-build-toolchain-for-speed.md)
- 22.1 Why it matters: flags can swing latency by double digits
- 22.2 Mental model: optimization levels, inlining control, the linker
- 22.3 Measure it: `-O2`/`-O3`/`-march` comparison
- 22.4 Techniques
  - 22.4.1 `-march`/`-mtune` targeting
  - 22.4.2 LTO
  - 22.4.3 PGO and BOLT
- 22.5 Pitfalls & anti-patterns: `-Ofast` hazards, portability loss
- 22.6 Exercises & checklist → *(see Appendix D for the flag reference)*

## Part IV — Memory Management

*Allocation cost, custom allocators, cache-friendly containers, and memory mapping.*

### [23. Memory Management Fundamentals](23-memory-management-fundamentals.md)
- 23.1 Why it matters: `malloc` can stall on the hot path
- 23.2 Mental model: stack vs heap; page faults; allocator internals
- 23.3 Measure it: allocation-cost and page-fault distribution
- 23.4 Techniques
  - 23.4.1 Stack and pre-allocated buffers
  - 23.4.2 Pre-faulting pages
- 23.5 Pitfalls & anti-patterns: hidden allocations; minor/major faults
- 23.6 Exercises & checklist

### [24. Custom Allocators](24-custom-allocators.md)
- 24.1 Why it matters: steady-state zero allocation
- 24.2 Mental model: arena/pool/slab strategies; `std::pmr`
- 24.3 Measure it: pool vs `new`/`delete` latency
- 24.4 Techniques
  - 24.4.1 Arena and bump allocators
  - 24.4.2 Object pools and free-lists
  - 24.4.3 `std::pmr` memory resources
- 24.5 Pitfalls & anti-patterns: fragmentation, lifetime bugs
- 24.6 Exercises & checklist

### [25. Hot-Path-Hostile STL & Cache-Friendly Containers](25-hotpath-hostile-stl-cache-friendly-containers.md)
- 25.1 Why it matters: node-based containers chase pointers
- 25.2 Mental model: costs of `unordered_map`/`map`/`function`/`shared_ptr`
- 25.3 Measure it: `std::unordered_map` vs flat hashing
- 25.4 Techniques
  - 25.4.1 Flat / open-addressing & robin-hood hashing
  - 25.4.2 `std::flat_map`/`std::flat_set` (C++23)
  - 25.4.3 Intrusive containers and small-buffer types
  - 25.4.4 Case Study — Building the Limit Order Book
- 25.5 Pitfalls & anti-patterns: `shared_ptr` refcount churn
- 25.6 Exercises & checklist

### [26. Memory Mapping](26-memory-mapping.md)
- 26.1 Why it matters: shared memory and zero-copy I/O
- 26.2 Mental model: `mmap`, file-backed vs anonymous, page faulting
- 26.3 Measure it: cost of the first touch vs pre-faulted
- 26.4 Techniques
  - 26.4.1 Shared-memory regions
  - 26.4.2 Pre-faulting and `mlock`
- 26.5 Pitfalls & anti-patterns: lazy faults on the hot path
- 26.6 Exercises & checklist

## Part V — Numerics & Data Parallelism

*Representing prices exactly, bit tricks, and SIMD.*

### [27. Fixed-Point & Floating-Point Arithmetic](27-fixed-point-floating-point-arithmetic.md)
- 27.1 Why it matters: representing prices without losing money
- 27.2 Mental model
  - 27.2.1 Scaled integers vs decimal vs binary float
  - 27.2.2 Rounding and tick-size handling
  - 27.2.3 Denormals and the FTZ/DAZ flags
  - 27.2.4 FP op latency/throughput; NaN/Inf traps
- 27.3 Measure it: denormal slowdown; FP op latencies
- 27.4 Techniques
  - 27.4.1 Scaled-integer price math
  - 27.4.2 Enabling FTZ/DAZ
  - 27.4.3 Cross-build determinism on the hot path
- 27.5 Verify the codegen: FP flags and instruction selection
- 27.6 Pitfalls & anti-patterns: float equality, accumulated error
- 27.7 Exercises & checklist

### [28. Bit Manipulation & Integer Tricks](28-bit-manipulation-integer-tricks.md)
- 28.1 Why it matters: one instruction instead of a loop
- 28.2 Mental model: BMI/BMI2 (`popcnt`, `lzcnt`/`tzcnt`, `pdep`/`pext`, `blsr`)
- 28.3 Measure it: intrinsic latency/throughput
- 28.4 Techniques
  - 28.4.1 Bitsets/bitmaps for order-book occupancy and free-lists
  - 28.4.2 Branchless min/max/abs/sign
  - 28.4.3 Power-of-two rounding and fast modulo via masks
  - 28.4.4 Field packing/unpacking; Morton/Z-order interleaving
- 28.5 Verify the codegen: intrinsic emission
- 28.6 Pitfalls & anti-patterns: `pdep`/`pext` on AMD; portability
- 28.7 Exercises & checklist

### [29. SIMD & Vectorization](29-simd-vectorization.md)
- 29.1 Why it matters: data parallelism for batch work
- 29.2 Mental model: auto-vectorization; SSE/AVX/AVX-512; downclocking
- 29.3 Measure it: scalar vs vectorized throughput (and frequency effects)
- 29.4 Techniques
  - 29.4.1 Data layout for vectorization
  - 29.4.2 Intrinsics when the compiler won't
  - 29.4.3 Choosing the ISA width
- 29.5 Verify the codegen: vector instruction selection
- 29.6 Pitfalls & anti-patterns: AVX-512 downclocking; misaligned loads
- 29.7 Exercises & checklist

## Part VI — Concurrency

*The memory model, lock-free structures, the Disruptor, coroutines, std::execution, and correctness tooling.*

### [30. The C++ Memory Model & Atomics](30-cpp-memory-model-atomics.md)
- 30.1 Why it matters: correctness and cost of sharing
- 30.2 Mental model: `memory_order`; acquire/release/seq_cst; fences; reordering
- 30.3 Measure it: cost per memory order
- 30.4 Techniques
  - 30.4.1 Choosing the weakest correct ordering
  - 30.4.2 Fences vs ordered atomics
- 30.5 Verify the codegen: barriers emitted per ordering (x86 vs ARM)
- 30.6 Pitfalls & anti-patterns: seq_cst by default; data races
- 30.7 Exercises & checklist

### [31. Multithreading & Concurrency Foundations](31-multithreading-concurrency-foundations.md)
- 31.1 Why it matters: scaling without contention
- 31.2 Mental model: contention, scalability limits, shared-nothing
- 31.3 Measure it: scalability curve vs thread count
- 31.4 Techniques
  - 31.4.1 Shared-nothing partitioning
  - 31.4.2 Minimizing shared mutable state
- 31.5 Pitfalls & anti-patterns: false scaling, oversubscription
- 31.6 Exercises & checklist

### [32. Spinlocks, Backoff & Contention Control](32-spinlocks-backoff-contention-control.md)
- 32.1 Why it matters: spin vs block on a hot core
- 32.2 Mental model: test-and-test-and-set; `pause`/`tpause`; futex internals
- 32.3 Measure it: spin vs mutex under contention
- 32.4 Techniques
  - 32.4.1 TTAS spinlocks
  - 32.4.2 Exponential backoff
  - 32.4.3 MCS/CLH queue locks
- 32.5 Pitfalls & anti-patterns: spinning across the scheduler; lock convoy
- 32.6 Exercises & checklist

### [33. False Sharing & Thread-Safety Anomalies](33-false-sharing-thread-safety-anomalies.md)
- 33.1 Why it matters: invisible cache-line ping-pong
- 33.2 Mental model: coherence traffic; `hardware_destructive_interference_size`
- 33.3 Measure it: HITM counters with/without padding
- 33.4 Techniques
  - 33.4.1 Alignment and padding to a cache line
  - 33.4.2 Per-thread/per-core data
- 33.5 Pitfalls & anti-patterns: adjacent hot counters; padding bloat
- 33.6 Exercises & checklist

### [34. Lock-Free Data Structures](34-lock-free-data-structures.md)
- 34.1 Why it matters: progress without locks
- 34.2 Mental model: CAS loops, ABA, progress guarantees
- 34.3 Measure it: SPSC ring throughput/latency
- 34.4 Techniques
  - 34.4.1 SPSC and MPMC queues
  - 34.4.2 Ring buffers
  - 34.4.3 Avoiding ABA (tagged pointers)
- 34.5 Pitfalls & anti-patterns: livelock, lost wakeups
- 34.6 Exercises & checklist

### [35. Seqlocks & Single-Writer Publication](35-seqlocks-single-writer-publication.md)
- 35.1 Why it matters: publishing market data to many readers
- 35.2 Mental model: versioned/sequence-lock snapshots; single-writer/multi-reader
- 35.3 Measure it: seqlock read latency under writer activity
- 35.4 Techniques
  - 35.4.1 Sequence-number publication
  - 35.4.2 Torn-read avoidance
- 35.5 Pitfalls & anti-patterns: correctness under the memory model
- 35.6 Exercises & checklist

### [36. Safe Memory Reclamation](36-safe-memory-reclamation.md)
- 36.1 Why it matters: freeing memory under lock-free readers
- 36.2 Mental model: the reclamation problem
- 36.3 Measure it: reclamation overhead comparison
- 36.4 Techniques
  - 36.4.1 RCU
  - 36.4.2 Hazard pointers
  - 36.4.3 Epoch-based reclamation
- 36.5 Pitfalls & anti-patterns: use-after-free, unbounded deferral
- 36.6 Exercises & checklist

### [37. The Disruptor Pattern](37-the-disruptor-pattern.md)
- 37.1 Why it matters: a proven low-latency pipeline
- 37.2 Mental model: ring buffer + sequence barriers; mechanical sympathy
- 37.3 Measure it: Disruptor vs queue latency
- 37.4 Techniques
  - 37.4.1 Sequence barriers and dependency graphs
  - 37.4.2 Batching at the consumer
- 37.5 Pitfalls & anti-patterns: false sharing on sequences
- 37.6 Exercises & checklist

### [38. Coroutines & Async Models](38-coroutines-async-models.md)
- 38.1 Why it matters: async I/O without thread-per-connection
- 38.2 Mental model: C++20 coroutines; stackful vs stackless
- 38.3 Measure it: coroutine resume cost
- 38.4 Techniques
  - 38.4.1 Event loops
  - 38.4.2 Custom awaiters for I/O
- 38.5 Pitfalls & anti-patterns: hidden allocations in coroutine frames
- 38.6 Exercises & checklist

### [39. std::execution: Senders/Receivers & Structured Async](39-std-execution-senders-receivers.md)
- 39.1 Why it matters: a standard async model, if it's actually zero-cost
- 39.2 Mental model: senders, receivers, schedulers, and structured concurrency
- 39.3 Measure it: sender pipeline overhead vs a hand-written state machine
- 39.4 Techniques
  - 39.4.1 A custom low-latency scheduler pinned to the hot core
  - 39.4.2 Sender pipelines and structured concurrency for the hot path
  - 39.4.3 When senders help — and when to stay hand-written
- 39.5 Verify the codegen: does the pipeline compile away?
- 39.6 Pitfalls & anti-patterns: hidden hops, type erasure, and allocation
- 39.7 Exercises & checklist

### [40. Concurrency Correctness Tooling](40-concurrency-correctness-tooling.md)
- 40.1 Why it matters: lock-free bugs are non-deterministic
- 40.2 Mental model: what TSan/ASan/UBSan can and can't catch
- 40.3 Measure it: running the sanitizers on a queue
- 40.4 Techniques
  - 40.4.1 Stress testing
  - 40.4.2 Fuzzing
  - 40.4.3 Model checking lock-free code
- 40.5 Pitfalls & anti-patterns: sanitizer-clean but still racy
- 40.6 Exercises & checklist

## Part VII — OS, Scheduling & Isolation

*Building and keeping a quiet, isolated, warm core — including cache and bandwidth partitioning.*

### [41. Context Switching & Its Mitigation](41-context-switching-mitigation.md)
- 41.1 Why it matters: a switch trashes the caches and TLB
- 41.2 Mental model: direct vs indirect costs; syscall overhead
- 41.3 Measure it: context-switch and null-syscall cost
- 41.4 Techniques
  - 41.4.1 Keeping threads on-core
  - 41.4.2 Busy-poll vs block
- 41.5 Pitfalls & anti-patterns: involuntary preemption, syscalls on the hot path
- 41.6 Exercises & checklist

### [42. Thread & Interrupt Pinning](42-thread-interrupt-pinning.md)
- 42.1 Why it matters: own the core, evict the noise
- 42.2 Mental model: CPU affinity and IRQ routing
- 42.3 Measure it: jitter with/without pinning
- 42.4 Techniques
  - 42.4.1 `taskset`/`sched_setaffinity`
  - 42.4.2 IRQ affinity and isolating the hot core
- 42.5 Pitfalls & anti-patterns: leftover IRQs on the hot core
- 42.6 Exercises & checklist

### [43. SMT / Hyperthreading](43-smt-hyperthreading.md)
- 43.1 Why it matters: a sibling thread steals your resources
- 43.2 Mental model: partitioned vs competitively-shared resources; throughput-vs-latency
- 43.3 Measure it: per-thread interference with the sibling busy
- 43.4 Techniques
  - 43.4.1 Detecting sibling topology
  - 43.4.2 Leaving the sibling idle vs housekeeping-only
  - 43.4.3 Deciding HT on/off per core
- 43.5 Pitfalls & anti-patterns: co-scheduling two hot threads on one core
- 43.6 Exercises & checklist

### [44. Cache Allocation Technology & Intel RDT](44-cache-allocation-intel-rdt.md)
- 44.1 Why it matters: pinning the core isn't enough if the cache is shared
- 44.2 Mental model: RDT, CLOS, capacity bitmasks, and `resctrl`
- 44.3 Measure it: a noisy neighbor vs a partitioned cache
- 44.4 Techniques
  - 44.4.1 Partitioning the L3 with CAT via `resctrl`
  - 44.4.2 Throttling neighbors with MBA; monitoring with CMT/MBM
  - 44.4.3 Partitioning for DDIO and multi-tenant boxes
- 44.5 Pitfalls & anti-patterns: over-partitioning and overlapping masks
- 44.6 Exercises & checklist

### [45. Real-Time Scheduling & Kernel Tuning](45-realtime-scheduling-kernel-tuning.md)
- 45.1 Why it matters: eliminating scheduler-induced jitter
- 45.2 Mental model: `isolcpus`, `nohz_full`, RT priorities, cgroups
- 45.3 Measure it: `cyclictest` before/after
- 45.4 Techniques
  - 45.4.1 CPU isolation and tickless cores
  - 45.4.2 RT scheduling classes and priorities
- 45.5 Pitfalls & anti-patterns: RT throttling, priority inversion
- 45.6 Exercises & checklist

### [46. Keeping the Hot Path Warm](46-keeping-the-hot-path-warm.md)
- 46.1 Why it matters: the first real message must not be cold
- 46.2 Mental model: cache/TLB/branch-predictor state decay
- 46.3 Measure it: warm vs cold tail latency
- 46.4 Techniques
  - 46.4.1 Shadow/dummy traffic
  - 46.4.2 Pre-touching pages and connections
- 46.5 Pitfalls & anti-patterns: cold-start stalls, warming the wrong state
- 46.6 Exercises & checklist

## Part VIII — Kernel I/O, Sockets & Zero-Copy

*Getting data in and out through the kernel as fast as the kernel allows — up to the modern zero-copy fast path.*

### [47. Linux Native I/O](47-linux-native-io.md)
- 47.1 Why it matters: the baseline before bypass
- 47.2 Mental model: readiness vs completion; blocking vs non-blocking
- 47.3 Measure it: `epoll` round-trip latency
- 47.4 Techniques
  - 47.4.1 `epoll` event loops
  - 47.4.2 Syscall batching
- 47.5 Pitfalls & anti-patterns: thundering herd, edge/level confusion
- 47.6 Exercises & checklist

### [48. io_uring Deep Dive](48-io-uring-deep-dive.md)
- 48.1 Why it matters: completion-based I/O with fewer syscalls
- 48.2 Mental model: submission/completion queues; polling mode
- 48.3 Measure it: io_uring vs `epoll`
- 48.4 Techniques
  - 48.4.1 Registered buffers and files
  - 48.4.2 Polling mode for lowest latency
  - 48.4.3 Batching submissions
- 48.5 Pitfalls & anti-patterns: completion-ordering assumptions
- 48.6 Exercises & checklist

### [49. Zero-Copy & the Modern Kernel Fast Path](49-zero-copy-modern-kernel-fast-path.md)
- 49.1 Why it matters: the copy is a cost you pay on every byte
- 49.2 Mental model: where the copies are, and how zero-copy removes them
- 49.3 Measure it: copy cost, the zero-copy threshold, and RX latency
- 49.4 Techniques
  - 49.4.1 Zero-copy send: `MSG_ZEROCOPY` / `SO_ZEROCOPY`
  - 49.4.2 Zero-copy receive: `TCP_ZEROCOPY_RECEIVE` and mmap
  - 49.4.3 io_uring zero-copy send and receive
  - 49.4.4 devmem TCP: payload straight to device memory
- 49.5 Pitfalls & anti-patterns: buffer lifetime and the small-message trap
- 49.6 Exercises & checklist

### [50. Inter-Process Communication](50-inter-process-communication.md)
- 50.1 Why it matters: splitting work across processes cheaply
- 50.2 Mental model: shared-memory queues vs pipes
- 50.3 Measure it: shm ring vs pipe latency
- 50.4 Techniques
  - 50.4.1 Lock-free cross-process ring buffers
  - 50.4.2 Shared-memory layout and addressing
- 50.5 Pitfalls & anti-patterns: pointers across address spaces
- 50.6 Exercises & checklist

### [51. Socket Optimization & TCP/Protocol Tuning](51-socket-optimization-tcp-tuning.md)
- 51.1 Why it matters: defaults cost microseconds
- 51.2 Mental model: Nagle, buffering, congestion control, multicast
- 51.3 Measure it: `TCP_NODELAY` on/off RTT
- 51.4 Techniques
  - 51.4.1 `TCP_NODELAY` and buffer sizing
  - 51.4.2 Multicast for market data
- 51.5 Pitfalls & anti-patterns: Nagle/delayed-ACK interaction
- 51.6 Exercises & checklist

### [52. Advanced TCP Internals & Tuning](52-advanced-tcp-internals-tuning.md)
- 52.1 Why it matters: TCP's defaults optimize for the wrong thing
- 52.2 Mental model: the send path, Nagle, delayed ACK, and congestion control
- 52.3 Measure it: the delayed-ACK stall, cold start, and offload latency
- 52.4 Techniques
  - 52.4.1 Killing the stalls: NODELAY, QUICKACK, and warming
  - 52.4.2 Offloads, congestion control, and buffer tuning
  - 52.4.3 Bypassing the stack where TCP itself is the tax
- 52.5 Pitfalls & anti-patterns: the 40ms stall and offload latency
- 52.6 Exercises & checklist

## Part IX — Market Data, NIC & Fabric

*Decoding the feed, recovering losses, and the NIC, cache, timing, and fabric hardware around the receive and send paths.*

### [53. Zero-Copy Wire Handling & Market-Data Decoding](53-zero-copy-wire-market-data-decoding.md)
- 53.1 Why it matters: parse off the wire without copies
- 53.2 Mental model
  - 53.2.1 ITCH/OUCH/FIX/SBE/FAST encodings
  - 53.2.2 Fixed-layout structs vs bit-fields; endianness
  - 53.2.3 Redundant A/B multicast feeds
  - 53.2.4 Mechanical sympathy for UDP microbursts: hardware RX rings, software reorder buffers, market-open drops
- 53.3 Measure it: decode latency per protocol
- 53.4 Techniques
  - 53.4.1 `std::from_chars`/`to_chars`; branch-free integer/decimal parsing
  - 53.4.2 Scatter/gather and zero-copy receive paths
  - 53.4.3 A/B line arbitration; sequence-gap detection and recovery
  - 53.4.4 Tuning RX ring sizes, managing UDP reordering in software ring buffers, detecting hardware packet drops
- 53.5 Verify the codegen: parse loop
- 53.6 Pitfalls & anti-patterns: unaligned reads, trusting wire input *(ties Ch. 72)*
- 53.7 Exercises & checklist

### [54. Reliable Multicast & Feed Recovery](54-reliable-multicast-feed-recovery.md)
- 54.1 Why it matters: market data is lossy, and a gap blinds the strategy
- 54.2 Mental model: sequence gaps and the three-channel recovery design
- 54.3 Measure it: gap rates, recovery latency, and hot-path impact
- 54.4 Techniques
  - 54.4.1 A/B line arbitration and gap detection
  - 54.4.2 Retransmission and snapshot recovery off the hot path
  - 54.4.3 Trading through a gap: pause, continue, or safe-state
- 54.5 Pitfalls & anti-patterns: blocking on recovery and NAK storms
- 54.6 Exercises & checklist

### [55. NIC Features & Offloads](55-nic-features-offloads.md)
- 55.1 Why it matters: push work into the card
- 55.2 Mental model: RSS, offloads, hardware timestamping
- 55.3 Measure it: offload on/off CPU cost
- 55.4 Techniques
  - 55.4.1 RSS and flow steering
  - 55.4.2 Busy-polling
  - 55.4.3 Solarflare/Onload
- 55.5 Pitfalls & anti-patterns: offloads that add latency
- 55.6 Exercises & checklist

### [56. DDIO & the NIC-to-Cache Data Path](56-ddio-nic-cache-data-path.md)
- 56.1 Why it matters: where a received packet lands decides the RX latency floor
- 56.2 Mental model: DMA into L3, the DDIO region, and allocating writes
- 56.3 Measure it: DDIO hit vs DRAM, and DDIO cache pollution
- 56.4 Techniques
  - 56.4.1 Keeping RX buffers DDIO-resident and NIC-local
  - 56.4.2 Separating the DDIO region from the hot working set with CAT
  - 56.4.3 When to disable DDIO, and cross-socket/peer-to-peer cases
- 56.5 Pitfalls & anti-patterns: DDIO thrashing and the cross-socket trap
- 56.6 Exercises & checklist

### [57. Flow Steering & Receive Scaling](57-flow-steering-receive-scaling.md)
- 57.1 Why it matters: the right packet must reach the right core
- 57.2 Mental model: RSS, flow steering, and the RX-to-core-to-cache chain
- 57.3 Measure it: cross-core handoff cost and steering benefit
- 57.4 Techniques
  - 57.4.1 RSS and RSS contexts for deliberate spreading
  - 57.4.2 Exact flow steering: n-tuple, Flow Director, and `rte_flow`
  - 57.4.3 aRFS, `SO_REUSEPORT`, and batched receive
- 57.5 Pitfalls & anti-patterns: scattering a feed and steering to the wrong core
- 57.6 Exercises & checklist

### [58. Clock Synchronization & Hardware Timestamping (PTP)](58-clock-synchronization-ptp.md)
- 58.1 Why it matters: comparing timestamps across machines
- 58.2 Mental model: PTP distribution; NIC hardware timestamps
- 58.3 Measure it: PTP offset/jitter; true wire-to-wire latency
- 58.4 Techniques
  - 58.4.1 PTP setup and disciplining
  - 58.4.2 Tick-to-trade measurement across hosts
  - 58.4.3 Advanced synchronization: sub-nanosecond White Rabbit, PPS signaling for boundary clocks, grandmaster oscillator drift
- 58.5 Pitfalls & anti-patterns: software-timestamp error
- 58.6 Exercises & checklist

### [59. Precise & Scheduled Transmission](59-precise-scheduled-transmission.md)
- 59.1 Why it matters: "send now" is not the only option
- 59.2 Mental model: EDT, `SO_TXTIME`, qdiscs, and hardware LAUNCHTIME
- 59.3 Measure it: send-time accuracy and pacing precision
- 59.4 Techniques
  - 59.4.1 `SO_TXTIME` + ETF + hardware LAUNCHTIME
  - 59.4.2 Pacing: `SO_MAX_PACING_RATE`, FQ, and the EDT model
  - 59.4.3 TSN, time-aware shaping, and coordinated sends
- 59.5 Pitfalls & anti-patterns: clock discipline and missed deadlines
- 59.6 Exercises & checklist

### [60. Network Fabric & Switching](60-network-fabric-switching.md)
- 60.1 Why it matters: the switch is on the tick-to-trade path
- 60.2 Mental model: store-and-forward, cut-through, and layer-1 switches
- 60.3 Measure it: per-hop and one-way latency with hardware timestamps
- 60.4 Techniques
  - 60.4.1 Choosing switching technology and minimizing hops
  - 60.4.2 Fabric layout, buffering, and microburst handling
  - 60.4.3 Switch-based replication, timestamping, and taps
- 60.5 Pitfalls & anti-patterns: store-and-forward, oversubscription, and hidden hops
- 60.6 Exercises & checklist

## Part X — Kernel Bypass, RDMA & Transport

*Removing the kernel entirely, one-sided remote memory, lossless fabric, and purpose-built transports beyond TCP.*

### [61. eBPF, bpftrace & XDP / AF_XDP](61-ebpf-bpftrace-xdp.md)
- 61.1 Why it matters: low-overhead production tracing and fast packet paths
- 61.2 Mental model: the eBPF VM and verifier
- 61.3 Measure it: probe overhead
- 61.4 Techniques
  - 61.4.1 kprobes/uprobes/tracepoints/USDT; `bpftrace` one-liners
  - 61.4.2 XDP for in-driver drop/redirect/filter
  - 61.4.3 AF_XDP zero-copy sockets
- 61.5 Pitfalls & anti-patterns: verifier limits; tracing overhead near the hot path
- 61.6 Exercises & checklist

### [62. Kernel Bypass & Userspace Networking](62-kernel-bypass-userspace-networking.md)
- 62.1 Why it matters: the lowest-latency software path
- 62.2 Mental model: poll-mode drivers; userspace stacks
- 62.3 Measure it: kernel-stack vs DPDK RX latency
- 62.4 Techniques
  - 62.4.1 DPDK and hugepage mempools
  - 62.4.2 Userspace TCP stacks
  - 62.4.3 The tick-to-trade fast path
  - 62.4.4 Proprietary vendor APIs: Solarflare `ef_vi` and ExaNIC `libexanic` for zero-copy NIC ring access; transparent vs explicit bypass latency floors
- 62.5 Pitfalls & anti-patterns: burning cores; losing kernel tooling
- 62.6 Exercises & checklist

### [63. InfiniBand Verbs & RDMA](63-infiniband-verbs-rdma.md)
- 63.1 Why it matters: one-sided remote memory access
- 63.2 Mental model: queue pairs, completion queues, memory regions/registration, work requests
- 63.3 Measure it: RDMA write vs send/recv latency
- 63.4 Techniques
  - 63.4.1 RDMA read/write vs send/recv
  - 63.4.2 RoCEv2 vs native InfiniBand
  - 63.4.3 Polling completions; registered-memory/zero-copy
- 63.5 Pitfalls & anti-patterns: registration cost, memory pinning
- 63.6 Exercises & checklist

### [64. Lossless Ethernet & RDMA Congestion](64-lossless-ethernet-rdma-congestion.md)
- 64.1 Why it matters: RDMA assumes a lossless fabric that Ethernet isn't
- 64.2 Mental model: PFC, ECN, DCQCN, and the RoCEv2 congestion problem
- 64.3 Measure it: RDMA under congestion and PFC behavior
- 64.4 Techniques
  - 64.4.1 Configuring PFC and DCB for lossless RoCEv2
  - 64.4.2 ECN and DCQCN congestion control
  - 64.4.3 Handling incast and avoiding PFC deadlock
- 64.5 Pitfalls & anti-patterns: PFC deadlock and assuming Ethernet is lossless
- 64.6 Exercises & checklist

### [65. Transport Beyond TCP: Aeron, eRPC, Homa & Custom Reliable UDP](65-transport-beyond-tcp.md)
- 65.1 Why it matters: TCP is a general-purpose stream, and you're sending messages
- 65.2 Mental model: the transport design space
- 65.3 Measure it: message rate, latency, and HOL blocking vs TCP
- 65.4 Techniques
  - 65.4.1 Aeron: reliable UDP messaging, unicast and multicast
  - 65.4.2 eRPC: microsecond RPC on commodity hardware
  - 65.4.3 Homa: receiver-driven, connectionless, HOL-free
  - 65.4.4 Rolling your own reliable UDP
- 65.5 Pitfalls & anti-patterns: reinventing TCP badly
- 65.6 Exercises & checklist

## Part XI — Heterogeneous Computing & Hardware Acceleration

*Pushing work off the CPU onto GPUs, FPGAs, and SmartNICs — and where the PCIe round-trip keeps them off the order path.*

### [66. PCIe & the Host–Device Boundary](66-pcie-host-device-boundary.md)
- 66.1 Why it matters: the round-trip floor under every offload
- 66.2 Mental model
  - 66.2.1 PCIe topology, lanes/generations and bandwidth
  - 66.2.2 MMIO, BAR-mapped registers, DMA and the IOMMU
  - 66.2.3 Descriptor rings and doorbells; posted vs non-posted transactions
- 66.3 Measure it: device-register round-trip and streaming bandwidth
- 66.4 Techniques
  - 66.4.1 Write-combining and relaxed ordering
  - 66.4.2 MSI-X interrupts vs polling for completions
  - 66.4.3 Peer-to-peer DMA (GPUDirect, NIC-to-GPU/FPGA)
- 66.5 Pitfalls & anti-patterns: a device read on the hot path; IOMMU overhead
- 66.6 Exercises & checklist

### [67. GPU Computing with CUDA](67-gpu-computing-cuda.md)
- 67.1 Why it matters: data-parallel offload (and why not the order path)
- 67.2 Mental model: CUDA execution & memory model; warps/occupancy
- 67.3 Measure it: end-to-end including transfer, not just kernel time
- 67.4 Techniques
  - 67.4.1 Host–device transfer; pinned and unified memory
  - 67.4.2 Streams and compute/transfer overlap
  - 67.4.3 Where GPUs pay off (risk, pricing/Greeks, Monte-Carlo, backtesting, ML inference)
- 67.5 Pitfalls & anti-patterns: PCIe latency on the tick-to-trade path
- 67.6 Exercises & checklist

### [68. Distributed Computing with MPI](68-distributed-computing-mpi.md)
- 68.1 Why it matters: scaling across hosts (off the order path)
- 68.2 Mental model: communicators; point-to-point vs collective
- 68.3 Measure it: message latency vs size
- 68.4 Techniques
  - 68.4.1 Non-blocking calls and compute/communication overlap
  - 68.4.2 RDMA/InfiniBand transports
  - 68.4.3 NUMA- and topology-aware rank placement
- 68.5 Pitfalls & anti-patterns: collectives on the critical path
- 68.6 Exercises & checklist

### [69. FPGA Acceleration](69-fpga-acceleration.md)
- 69.1 Why it matters: deterministic nanosecond pipelines inline on the wire
- 69.2 Mental model: HLS vs RTL; the host–FPGA boundary
- 69.3 Measure it: wire-to-wire latency of a hardware path
- 69.4 Techniques
  - 69.4.1 NIC-integrated FPGAs and inline feed handling/order entry
  - 69.4.2 Partial reconfiguration
  - 69.4.3 Deciding what belongs in fabric vs software
- 69.5 Pitfalls & anti-patterns: host-boundary stalls; toolchain complexity
- 69.6 Exercises & checklist

### [70. SmartNICs & DPUs](70-smartnics-dpus.md)
- 70.1 Why it matters: a programmable hop between wire and host
- 70.2 Mental model: NIC-resident ARM cores / FPGA fabric (BlueField, IPU, Pensando)
  - 70.2.1 The two boundaries: wire↔DPU and DPU↔host
- 70.3 Measure it: DPU-offload latency vs host path
- 70.4 Techniques
  - 70.4.1 Programming models (DOCA, P4, eBPF/XDP offload)
  - 70.4.2 On-NIC RDMA and storage/security offload
  - 70.4.3 Partitioning work between DPU and host
- 70.5 Pitfalls & anti-patterns: when the DPU just adds a hop
- 70.6 Exercises & checklist

## Part XII — Observability & Operations in Production

*Running the system: logging, security, hot reload, process topology, capture/replay, and the end-to-end case study.*

### [71. Zero-Overhead Logging](71-zero-overhead-logging.md)
- 71.1 Why it matters: logging must never stall the hot path
- 71.2 Mental model: deferred formatting; binary logging
- 71.3 Measure it: log-call cost on the hot path
- 71.4 Techniques
  - 71.4.1 Async/lock-free loggers
  - 71.4.2 Binary logging with off-line formatting
  - 71.4.3 `std::print`/`std::println` for off-hot-path output
- 71.5 Verify the codegen: the hot-path log call is a few stores
- 71.6 Pitfalls & anti-patterns: formatting in the hot path; blocking on I/O
- 71.7 Exercises & checklist

### [72. Secure Programming for Low-Latency Systems](72-secure-programming-low-latency.md)
- 72.1 Why it matters: security as a hot-path concern
- 72.2 Mental model
  - 72.2.1 Market-data/order-entry messages as untrusted input
  - 72.2.2 Undefined behavior as a vulnerability class
  - 72.2.3 The hardening-vs-latency trade-off
- 72.3 Measure it: hot-path cost of hardening options
- 72.4 Techniques
  - 72.4.1 Bounds/length validation on variable-length fields and zero-copy parses
  - 72.4.2 Memory safety in zero-allocation/arena designs; bounds checks that compile away
  - 72.4.3 Integer/arithmetic safety in price/quantity math
  - 72.4.4 Stack protector, `_FORTIFY_SOURCE`, PIE/ASLR, CFI, CET shadow stacks
  - 72.4.5 Secret handling and secure wiping; keeping secrets out of logs
  - 72.4.6 Privilege reduction (`seccomp`, capabilities, namespaces)
  - 72.4.7 Build/supply-chain integrity; reproducible and signed builds
  - 72.4.8 Fuzzing message parsers (libFuzzer/AFL++); ASan/UBSan/MSan
- 72.5 Pitfalls & anti-patterns: trusting the wire; mitigation interactions
- 72.6 Exercises & checklist

### [73. Hot Reload & Live Reconfiguration](73-hot-reload-live-reconfiguration.md)
- 73.1 Why it matters: change strategy/config without dropping a tick
- 73.2 Mental model: atomic pointer swaps and double-buffering
- 73.3 Measure it: reload-induced tear/stall test
- 73.4 Techniques
  - 73.4.1 Seqlock-/RCU-published config snapshots read lock-free
  - 73.4.2 Versioned configuration and safe reclamation
  - 73.4.3 Shared-memory config segments; validation-before-swap
  - 73.4.4 Draining vs instantaneous cutover; dynamic-library/strategy reload with warm-up
- 73.5 Pitfalls & anti-patterns: torn config, stalls during cutover
- 73.6 Exercises & checklist

### [74. Process Topology & The Deterministic State Machine](74-process-topology-deterministic-state-machine.md)
- 74.1 Why it matters: contain a crash, keep the venue session up
- 74.2 Mental model
  - 74.2.1 Process-per-role vs monolith (feed handler / strategy / order gateway / risk)
  - 74.2.2 The Deterministic State Machine Pattern: pure, side-effect-free core decoupled from I/O for exact replayability
  - 74.2.3 Fault domains and blast radius
  - 74.2.4 Crash-only design
- 74.3 Measure it: failover/restart time and its tail
- 74.4 Techniques
  - 74.4.1 Shared-memory data planes between isolated processes
  - 74.4.2 Single-writer-per-segment discipline
  - 74.4.3 Supervisor/watchdog, heartbeats and liveness
  - 74.4.4 Kill-switch / safe-state on failure; fast restart with warm-up
  - 74.4.5 Core dumps without stalling survivors; symbol/venue sharding
- 74.5 Pitfalls & anti-patterns: shared fate, cascading restarts
- 74.6 Exercises & checklist

### [75. Capture, Persistence & Replay Storage](75-capture-persistence-replay-storage.md)
- 75.1 Why it matters: deterministic replay, research, and compliance
- 75.2 Mental model: append-only/WAL journals; time-indexed storage
- 75.3 Measure it: capture throughput and hot-path impact
- 75.4 Techniques
  - 75.4.1 Lossless line capture and nanosecond packet timestamps
  - 75.4.2 Compact binary log formats
  - 75.4.3 Getting bytes to disk off the hot path (async, io_uring, `O_DIRECT`, NVMe, batching)
  - 75.4.4 Sequencing and gap-free persistence; retention and compaction
  - 75.4.5 Feeding captures into replay, simulation and backtests
- 75.5 Pitfalls & anti-patterns: dropping under load; storage stalls bleeding into the hot path
- 75.6 Exercises & checklist

### [76. Production Profiling & End-to-End Case Study](76-production-profiling-end-to-end-case-study.md)
- 76.1 Why it matters: tying the whole book together
- 76.2 Mental model: continuous performance monitoring in production
- 76.3 Measure it: a full tick-to-trade walkthrough
- 76.4 Techniques
  - 76.4.1 Regression detection and latency-regression gates in CI
  - 76.4.2 Deterministic replay of captured market data
  - 76.4.3 Simulation harnesses
- 76.5 Pitfalls & anti-patterns: drift between test and production
- 76.6 Exercises & checklist

## Appendices

### [Appendix A — ARM / Graviton & Non-x86 Targets](appendix-A-arm-graviton.md)
- A.1 The ARM memory model (`LDAR`/`STLR`, `DMB`) vs x86-TSO
- A.2 NEON/SVE/SVE2 vs AVX
- A.3 Cache-line size (64 vs 128 bytes) and false sharing
- A.4 Timers (`CNTVCT_EL0` vs `rdtsc`); `WFE`/`YIELD` vs `pause`
- A.5 big.LITTLE / heterogeneous cores and SMT differences
- A.6 `-mcpu`/`-mtune` tuning; spec-exec mitigations on ARM
- A.7 Where Graviton in the cloud fits (and where bare-metal still wins)

### [Appendix B — Beyond C++: Alternative Languages for Low-Latency](appendix-B-beyond-cpp.md)
- B.1 Rust (ownership, `unsafe`, no GC, fearless concurrency)
- B.2 C and Zig
- B.3 The case against managed runtimes (Java/C#/Go GC pauses and JIT warm-up)
- B.4 Taming the JVM: allocation-free Java, off-heap, Azul/Zing, GraalVM native-image
- B.5 FFI/interop with a C++ core; when a rewrite pays off

### [Appendix C — System Tuning Checklist](appendix-C-system-tuning-checklist.md)
- C.1 BIOS/firmware (C-states, P-states/turbo, SMT, NUMA/snoop mode)
- C.2 Kernel cmdline (`isolcpus`, `nohz_full`, `rcu_nocbs`, `intel_pstate`, `mitigations=`, huge pages)
- C.3 Runtime knobs (governor, IRQ affinity/`irqbalance`, `tuned`, RPS/XPS, NIC ring/coalescing, `sysctl`, THP, `mlockall`)
- C.4 Per-process (affinity/`taskset`, RT priority, `numactl`)
- C.5 Verification pass (`cyclictest`, jitter measurement)

### [Appendix D — Compiler Flag Reference](appendix-D-compiler-flag-reference.md)
- D.1 Optimization levels (`-O2`/`-O3`/`-Ofast` caveats)
- D.2 Arch/tuning (`-march`/`-mtune`/`-mcpu`)
- D.3 FP and math (`-ffast-math` hazards, `-fno-math-errno`, FTZ/DAZ)
- D.4 Codegen (`-fno-exceptions`/`-fno-rtti`, `-fno-plt`, `-fno-semantic-interposition`, `-fno-omit-frame-pointer`)
- D.5 Inlining/layout (`-finline-limit`, `-freorder-blocks-and-partition`, `-falign-*`)
- D.6 LTO/PGO/BOLT
- D.7 Diagnostics (`-Wpadded`, `-Wfloat-equal`, `-fopt-info`) and sanitizers
- D.8 A representative hot-path build line

### [Appendix E — Latency Numbers Every Trading Developer Should Know](appendix-E-latency-numbers.md)
- E.1 The table
- E.2 How each was measured, with pointers to the deriving chapter

### [Appendix F — Glossary (HFT & Microarchitecture Terms)](appendix-F-glossary.md)
- F.1 Trading & market-structure terms
- F.2 Microarchitecture terms
- F.3 Memory & OS terms
- F.4 Concurrency terms
- F.5 C++/toolchain terms
- F.6 Networking, I/O & Transport terms
- F.7 Hardware Acceleration & Devices
- F.8 Security, Isolation & Operations

### [Appendix G — Annotated Bibliography](appendix-G-annotated-bibliography.md)
- G.1 Foundational papers
- G.2 Vendor & reference manuals
- G.3 Books
- G.4 Influential talks & blogs
- G.5 Tooling documentation
- G.6 How this maps to the Parts
- G.7 A short shelf

### [Appendix H — C++23/26 Feature Availability Matrix](appendix-H-cpp-feature-matrix.md)
- H.1 How to read the matrix (columns, library-vs-language, feature-test gating)
- H.2 Core library features (`std::expected`, `std::span`, `std::mdspan`, `std::flat_map`/`flat_set`, `std::print`)
- H.3 Language features (`consteval`/`constinit`, concepts, coroutines, `[[likely]]`, `[[assume]]`)
- H.4 Concurrency & execution (`std::atomic_ref`, `std::jthread`, `std::execution` senders/receivers)
- H.5 Numerics & bit manipulation (`std::bit_cast`, `<bit>`, `std::byteswap`, saturating arithmetic)
- H.6 Feature-test macros & graceful degradation

### [Appendix I — Tooling Command Cookbook](appendix-I-tooling-cookbook.md)
- I.1 `perf`: stat, record/report, annotate
- I.2 PMU counters & top-down microarchitecture analysis
- I.3 `perf c2c`, `perf mem` & false-sharing / HITM
- I.4 VTune quick recipes
- I.5 `bpftrace` / eBPF one-liners
- I.6 Topology, pinning & scheduling (`lscpu`, `lstopo`, `numactl`, `taskset`, `chrt`)
- I.7 Layout & memory inspection (`pahole`, cachegrind, `smaps`)
- I.8 Network & NIC (`ethtool`, `ss`, `tcpdump`, PTP)
- I.9 Codegen, binaries & sanitizers (`objdump`, Godbolt, BOLT, ASan/UBSan/TSan)

### [Appendix J — HFT Market-Structure & Protocol Primer](appendix-J-market-structure-primer.md)
- J.1 Market microstructure: exchanges, matching engines, price-time priority
- J.2 The order book: levels, BBO, NBBO, depth
- J.3 The order lifecycle (order types, TIF, ack/fill/cancel state machine)
- J.4 Market data: feeds, A/B lines, snapshots vs. increments
- J.5 Protocols: ITCH, OUCH, FIX, SBE, FAST (field-level quick reference)
- J.6 Roles in a trading system
- J.7 The tick-to-trade path, end to end

### [Appendix K — Exchange Connectivity & the Physical Layer](appendix-K-exchange-connectivity.md)
- K.1 Colocation: cages, cross-connects, power & cooling
- K.2 The wire: fiber vs. copper, latency-equalized cabling
- K.3 Wireless: microwave and millimeter-wave
- K.4 Exchange access: ports, gateways, entitlements & throttles
- K.5 Time distribution at the venue (grandmaster, PPS, PTP boundary clocks)
- K.6 Redundancy & path diversity
- K.7 The trade-offs: latency, cost, and regulation

### [Appendix L — Reproducible Benchmark & Measurement Harness](appendix-L-benchmark-harness.md)
- L.1 A Google Benchmark skeleton (throughput & mean latency)
- L.2 Tail-latency capture with HdrHistogram (and avoiding coordinated omission)
- L.3 Making a run repeatable (environment & isolation)
- L.4 The pre-trust checklist
