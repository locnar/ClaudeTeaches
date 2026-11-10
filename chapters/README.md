# Low-Latency C++ on Linux — Table of Contents & Topic Index

A book-length tutorial series on high-performance, low-latency software in **C++ on Linux**, framed around **HFT / electronic trading** (order books, market-data feeds, tick-to-trade). This file is the reader's map: each chapter expands into its standard sections (§*N*.1 *Why it matters* → §*N*.2 *Mental model* → §*N*.3 *Measure it* → §*N*.4 *Techniques* → §*N*.5 *Verify the codegen* → §*N*.6 *Pitfalls* → §*N*.7 *Exercises*; §*N*.5 *Verify the codegen* appears only in the chapters where the optimizer is the point — elsewhere Pitfalls and Exercises are §*N*.5 and §*N*.6), with the searchable sub-topics listed under **Mental model** and **Techniques**.

> **Reading the index:** a **linked** chapter title points at its file (`NN-slug.md`). The 107 chapters are grouped into thirteen Parts; sixteen appendices (A–P) follow.

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
  - 2.4.4 Beyond the core PMU: uncore, IBS, RAPL, SMI and Intel PT
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
- 5.3 Measure it: debugging an `-O2` binary with gdb and `rr`, and what debug info costs
- 5.4 Techniques
  - 5.4.1 Debugging optimized and LTO builds
  - 5.4.2 Record/replay, reverse debugging, and core-dump analysis
  - 5.4.3 Production tracing without perturbing the hot path
  - 5.4.4 Looking inside a live process without stopping the hot thread
  - 5.4.5 Finding the corrupting write in a zero-allocation design
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
  - 6.4.4 Clocksource and SMIs
- 6.5 Pitfalls & anti-patterns: turbo-induced variance, noisy neighbors
- 6.6 Exercises & checklist

### [7. Choosing & Clocking the Hot-Path CPU](07-choosing-clocking-hot-path-cpu.md)
- 7.1 Why it matters: the clock is the biggest single-thread lever you have
- 7.2 Mental model: nanoseconds = cycles ÷ frequency — for the part of the path that is cycles
  - 7.2.1 Core-bound vs memory-bound: which nanoseconds the clock can buy
  - 7.2.2 Server vs client silicon: what you are actually buying
  - 7.2.3 Turbo ratios, bins and limits — the vocabulary
  - 7.2.4 Hybrid cores: Intel's P/E split (and why Appendix A.5 needs a footnote)
- 7.3 Measure it: the clock dependence of four kernels, and a frequency probe that needs no MSRs
- 7.4 Techniques
  - 7.4.1 The hybrid plant: client silicon on the hot box, server silicon for everything else
  - 7.4.2 Turbo on but deterministic: fixing the ratio
  - 7.4.3 Overclocking and the stability-validation protocol
  - 7.4.4 Surviving hybrid cores: pin to the type you measured
- 7.5 Pitfalls & anti-patterns: buying GHz you cannot use, trusting a clock you cannot see
- 7.6 Exercises & checklist

---

## Part II — CPU Microarchitecture

*The hardware reality under all the code — caches, pipelines, branches, the TLB, NUMA, and time.*

### [8. The Memory Hierarchy & Caches](08-memory-hierarchy-and-caches.md)
- 8.1 Why it matters: DRAM is ~55× an L1 hit, ~250 cycles away
- 8.2 Mental model
  - 8.2.1 Latencies by level (L1/L2/L3/DRAM)
  - 8.2.2 Cache lines, sets, and associativity
  - 8.2.3 Coherence and the MESI protocol
  - 8.2.4 The hardware prefetchers
- 8.3 Measure it: a latency-vs-stride / cache-size sweep
- 8.4 Techniques
  - 8.4.1 Sizing the working set to a cache level
  - 8.4.2 Stride and locality for prefetcher friendliness
- 8.5 Pitfalls & anti-patterns: conflict misses, pointer chasing
- 8.6 Exercises & checklist

### [9. Cache-Aware & Data-Oriented Design](09-cache-aware-data-oriented-design.md)
- 9.1 Why it matters: layout decides the miss rate
- 9.2 Mental model
  - 9.2.1 Array-of-Structs vs Struct-of-Arrays
  - 9.2.2 Hot/cold field splitting
- 9.3 Measure it: AoS vs SoA cache-miss comparison on an order book
- 9.4 Techniques
  - 9.4.1 Access-pattern–driven layout
  - 9.4.2 Splitting hot fields from cold metadata
  - 9.4.3 SoA for the book-update loop
- 9.5 Pitfalls & anti-patterns: premature SoA, scattered hot data
- 9.6 Exercises & checklist

### [10. Object Layout, Alignment & Padding](10-object-layout-alignment-padding.md)
- 10.1 Why it matters: silent padding bloats the cache footprint
- 10.2 Mental model
  - 10.2.1 The C++/ABI object model: member ordering and struct padding
  - 10.2.2 `alignof`/`alignas`; natural vs over-alignment (SIMD, cache-line)
  - 10.2.3 `[[no_unique_address]]` and empty-base optimization
  - 10.2.4 Bit-fields and their codegen
- 10.3 Measure it: inspecting layout with `pahole` and `-Wpadded`
- 10.4 Techniques
  - 10.4.1 Reordering members to eliminate padding
  - 10.4.2 Sizing a struct to a cache line
  - 10.4.3 Packing trade-offs (`#pragma pack`) vs misaligned-access cost
- 10.5 Verify the codegen: aligned vs packed access
- 10.6 Pitfalls & anti-patterns: misaligned atomics, false economy of packing
- 10.7 Exercises & checklist

### [11. Software Prefetching & Non-Temporal Stores](11-software-prefetching-nontemporal-stores.md)
- 11.1 Why it matters: hiding miss latency the prefetcher won't
- 11.2 Mental model
  - 11.2.1 `__builtin_prefetch` / `_mm_prefetch` and locality hints
  - 11.2.2 Prefetch distance and timeliness
  - 11.2.3 Streaming (non-temporal) stores and the write path
- 11.3 Measure it: prefetch-distance sweep on a dependent random gather
- 11.4 Techniques
  - 11.4.1 Manual prefetch for irregular access
  - 11.4.2 Non-temporal stores to avoid cache pollution
- 11.5 Verify the codegen: prefetch and `movnt` emission
- 11.6 Pitfalls & anti-patterns: when manual prefetch hurts; prefetch the wrong line
- 11.7 Exercises & checklist

### [12. CPU Pipelines & Execution](12-cpu-pipelines-and-execution.md)
- 12.1 Why it matters: the core is doing more than one thing at once
- 12.2 Mental model
  - 12.2.1 Superscalar, out-of-order execution
  - 12.2.2 Structural / data / control hazards
  - 12.2.3 Dependency chains and critical paths
- 12.3 Measure it: IPC and port pressure for a dependent chain
- 12.4 Techniques
  - 12.4.1 Breaking dependency chains for ILP
  - 12.4.2 Unrolling and accumulator splitting
- 12.5 Verify the codegen: latency vs throughput of the hot loop
- 12.6 Pitfalls & anti-patterns: serializing on a single accumulator
- 12.7 Exercises & checklist

### [13. Instruction Cache, Front-End Stalls & Code Layout](13-instruction-cache-frontend-code-layout.md)
- 13.1 Why it matters: the front-end can starve a fast back-end
- 13.2 Mental model
  - 13.2.1 L1i, the µop cache (DSB), I-TLB, fetch/decode bandwidth
  - 13.2.2 Front-end-bound stalls in top-down analysis
- 13.3 Measure it: front-end-bound counters; DSB coverage
- 13.4 Techniques
  - 13.4.1 Taming code bloat from over-inlining
  - 13.4.2 Hot/cold code splitting; `[[likely]]`/`[[unlikely]]`, `__builtin_expect`
  - 13.4.3 Function/section placement and `-freorder-blocks-and-partition`
  - 13.4.4 Linker ordering, PGO and BOLT for I-cache locality
- 13.5 Verify the codegen: hot/cold section placement
- 13.6 Pitfalls & anti-patterns: inlining everything; cold code on the hot path
- 13.7 Exercises & checklist

### [14. Branch Prediction & Branchless Programming](14-branch-prediction-branchless.md)
- 14.1 Why it matters: a mispredict costs ~15–20 cycles
- 14.2 Mental model
  - 14.2.1 Predictor behavior and history
  - 14.2.2 Misprediction cost and the pipeline flush
- 14.3 Measure it: branch-mispredict rate on data-dependent branches
- 14.4 Techniques
  - 14.4.1 Conditional moves vs branches
  - 14.4.2 Lookup tables to replace control flow
  - 14.4.3 Removing branches from the hot path
- 14.5 Verify the codegen: `cmov` vs branch emission
- 14.6 Pitfalls & anti-patterns: branchless code that's slower; data-dependent stalls
- 14.7 Exercises & checklist

### [15. Indirect Calls, Virtual Dispatch & Devirtualization](15-indirect-calls-virtual-dispatch.md)
- 15.1 Why it matters: hidden indirect branches on the dispatch path
- 15.2 Mental model
  - 15.2.1 vtables and the cost of `virtual`
  - 15.2.2 Indirect-branch prediction (BTB) and mispredict cost
  - 15.2.3 `std::function` and type-erasure overhead
- 15.3 Measure it: virtual vs CRTP dispatch microbenchmark
- 15.4 Techniques
  - 15.4.1 `final`, speculative and profile-guided devirtualization
  - 15.4.2 CRTP and static polymorphism
  - 15.4.3 `std::variant`/visitor alternatives
  - 15.4.4 Jump tables vs branch chains
  - 15.4.5 Lighter type erasure: `function_ref`, `move_only_function`/`copyable_function`, in-place functions
- 15.5 Verify the codegen: devirtualized call sites
- 15.6 Pitfalls & anti-patterns: megamorphic call sites; `std::function` in the loop
- 15.7 Exercises & checklist

### [16. Virtual Memory, the TLB & Huge Pages](16-virtual-memory-tlb-huge-pages.md)
- 16.1 Why it matters: TLB misses tax every memory access
- 16.2 Mental model
  - 16.2.1 Address translation and the page-table walk
  - 16.2.2 TLB structure and miss cost
- 16.3 Measure it: `dtlb_load_misses` with and without huge pages
- 16.4 Techniques
  - 16.4.1 Transparent vs explicit huge pages
  - 16.4.2 Reducing TLB pressure via layout
- 16.5 Pitfalls & anti-patterns: THP stalls, fragmentation
- 16.6 Exercises & checklist

### [17. NUMA Architecture](17-numa-architecture.md)
- 17.1 Why it matters: cross-socket access is a hidden cliff
- 17.2 Mental model
  - 17.2.1 Node locality and the interconnect
  - 17.2.2 First-touch allocation policy
  - 17.2.3 Nodes within a socket: SNC, NPS and the chiplet level (pointer to Ch. 18)
- 17.3 Measure it: local vs remote DRAM latency with `numactl`
- 17.4 Techniques
  - 17.4.1 Allocation policy and binding
  - 17.4.2 NUMA-aware data structures and thread placement
- 17.5 Pitfalls & anti-patterns: accidental remote allocation; cross-node sharing
- 17.6 Exercises & checklist

### [18. Chiplets, Tiles & NUMA Within the Socket: the Core-to-Core Latency Map](18-chiplets-tiles-numa-within-socket.md)
- 18.1 Why it matters: the two cores you pick decide the handoff
- 18.2 Mental model: a socket is a small cluster
  - 18.2.1 AMD: CCDs, CCXs, the I/O die and Infinity Fabric
  - 18.2.2 Intel: monolithic mesh, then tiles over EMIB, and Sub-NUMA Clustering
  - 18.2.3 What a core-to-core transfer costs, and why the domain decides it
  - 18.2.4 Reading the map: the four blocks
- 18.3 Measure it: the core-to-core ping-pong harness and its heat-map
- 18.4 Techniques
  - 18.4.1 Place the pipeline inside one L3 domain — and choose which domain
  - 18.4.2 SNC / NPS: when to split the socket, and what it costs
  - 18.4.3 IRQs, NIC queues and the device's domain
  - 18.4.4 `resctrl` domains, cross-domain traffic and the single-writer rule
  - 18.4.5 Reading the map for the second axis: SMT and hybrid blocks
- 18.5 Pitfalls & anti-patterns: the topology you cannot see
- 18.6 Exercises & checklist

### [19. Timekeeping: TSC, rdtsc & Clock Sources](19-timekeeping-tsc-rdtsc-clocks.md)
- 19.1 Why it matters: you measure nanoseconds, so the clock matters
- 19.2 Mental model
  - 19.2.1 Invariant TSC; `rdtsc` vs `rdtscp` and serialization
  - 19.2.2 Clock sources and their cost
- 19.3 Measure it: TSC calibration against a reference clock
- 19.4 Techniques
  - 19.4.1 Reliable nanosecond timestamping on the hot path
  - 19.4.2 On-host hardware timestamping
- 19.5 Pitfalls & anti-patterns: unserialized `rdtsc`, frequency drift
- 19.6 Exercises & checklist

## Part III — Compile-Time & Language Mechanics

*Moving work to compile time and paying for abstractions only when they cost: `constexpr`/`consteval` and the template cost model, zero-cost design (CRTP, policies, strong types, concepts), a protocol decoder generated from a schema at compile time, the abstractions that are not free (exceptions, the calling convention, statics and TLS), the cost model of modern idioms and strings without allocation, aliasing and object lifetime, the toolchain flags, and the build system as a latency path and a correctness boundary.*

### [20. Compile-Time Mechanics](20-compile-time-mechanics.md)
- 20.1 Why it matters: work done at compile time is free at runtime
- 20.2 Mental model: `constexpr` vs `consteval`; the template cost model
- 20.3 Measure it: build-time vs run-time trade-off
- 20.4 Techniques
  - 20.4.1 Moving computation to compile time
  - 20.4.2 `constexpr` tables and lookup generation
- 20.5 Pitfalls & anti-patterns: compile-time blowup, code bloat
- 20.6 Exercises & checklist

### [21. Template Metaprogramming & Zero-Cost Abstractions](21-template-metaprogramming-zero-cost.md)
- 21.1 Why it matters: abstraction without the runtime tax
- 21.2 Mental model: CRTP, type-based dispatch, policy-based design
- 21.3 Measure it: abstraction overhead vs hand-written code
- 21.4 Techniques
  - 21.4.1 CRTP for static polymorphism
  - 21.4.2 Expression templates
  - 21.4.3 Policy-based design
  - 21.4.4 Strong types and units
  - 21.4.5 Type-state and compile-time state machines
  - 21.4.6 Concepts and constrained overload sets: compile-time dispatch four ways
- 21.5 Verify the codegen: zero-overhead confirmation
- 21.6 Pitfalls & anti-patterns: template bloat, error-message cost
- 21.7 Exercises & checklist

### [22. Compile-Time Protocol Decoders: From Schema to Code](22-compile-time-protocol-decoders.md)
- 22.1 Why it matters: the venue document is the source of truth, and it changes
- 22.2 Mental model: the schema is data; the decoder is a function of it
  - 22.2.1 Three representations of one message
  - 22.2.2 Class-type NTTPs and `fixed_string`: a string literal as a type-level value
  - 22.2.3 `consteval` parsing and the field table
  - 22.2.4 What C++26 static reflection would change (unverified here)
- 22.3 Measure it: decode latency identical, build time proportional to message types
- 22.4 Techniques
  - 22.4.1 From schema string to `constexpr` field table
  - 22.4.2 Generating the overlay accessors
  - 22.4.3 `static_assert` against the venue document and the schema version
  - 22.4.4 Message-type dispatch and repeating groups: what stays at runtime
  - 22.4.5 Keeping the build-time cost bounded
  - 22.4.6 The C++26 reflection form (unverified: no shipping compiler implements it)
- 22.5 Verify the codegen: the generated accessor is the hand-written instruction
- 22.6 Pitfalls & anti-patterns: compile-time layout, runtime data
- 22.7 Exercises & checklist

### [23. The Cost of Abstractions](23-the-cost-of-abstractions.md)
- 23.1 Why it matters: error handling on the hot path
- 23.2 Mental model: exception unwinding, RTTI, `noexcept`
- 23.3 Measure it: exceptions vs error codes vs `std::expected`
- 23.4 Techniques
  - 23.4.1 `noexcept` and the happy path
  - 23.4.2 `std::expected` for hot-path errors
  - 23.4.3 Disabling RTTI where unused
  - 23.4.4 Assertions, contracts and the diagnostics library on the hot path
  - 23.4.5 Passing and returning values: categories, elision and the calling convention
  - 23.4.6 Statics, `thread_local` and initialization: what the compiler emits
- 23.5 Verify the codegen: unwinding tables and landing pads
- 23.6 Pitfalls & anti-patterns: throwing on the hot path
- 23.7 Exercises & checklist

### [24. The Cost of Modern Idioms & Strings Without Allocation](24-cost-of-modern-idioms-strings-without-allocation.md)
- 24.1 Why it matters: idiomatic C++ is not free by default, only by inspection
- 24.2 Mental model
  - 24.2.1 A lambda is a class; a `std::function` is a pointer to one
  - 24.2.2 Iterators, sentinels and views: what a pipeline compiles to
  - 24.2.3 Small values by value: `string_view`, `optional`, `expected` and the ABI
  - 24.2.4 Where strings hide the allocation
- 24.3 Measure it: one book loop, one symbol table
- 24.4 Techniques
  - 24.4.1 Callables: the template parameter first, the pointer second, erasure last
  - 24.4.2 Ranges and views: algorithms freely, `filter` with the asm open
  - 24.4.3 `std::invoke`, `std::apply` and `std::tuple`: free in the call, not always in the layout
  - 24.4.4 Strings without allocation: symbols as integers, views over the buffer, fixed capacity, interning
- 24.5 Verify the codegen: what collapsed, what did not
- 24.6 Pitfalls & anti-patterns: idioms that leave a shape the optimizer cannot cross
- 24.7 Exercises & checklist

### [25. Aliasing, `restrict` & Type Punning](25-aliasing-restrict-type-punning.md)
- 25.1 Why it matters: aliasing assumptions gate optimization
- 25.2 Mental model: strict-aliasing rules; how aliasing blocks vectorization/reordering
- 25.3 Measure it: codegen difference with/without `__restrict__`
- 25.4 Techniques
  - 25.4.1 `__restrict__` to unblock the optimizer
  - 25.4.2 `std::bit_cast`, `std::launder` and `std::start_lifetime_as`: punning and overlays
- 25.5 Verify the codegen: vectorization gated by aliasing
- 25.6 Pitfalls & anti-patterns: UB type punning via casts/unions
- 25.7 Exercises & checklist

### [26. Build Toolchain for Speed](26-build-toolchain-for-speed.md)
- 26.1 Why it matters: flags can swing latency by double digits
- 26.2 Mental model: optimization levels, inlining control, the linker
- 26.3 Measure it: `-O2`/`-O3`/`-march` comparison
- 26.4 Techniques
  - 26.4.1 `-march`/`-mtune` targeting
  - 26.4.2 LTO
  - 26.4.3 PGO and BOLT
  - 26.4.4 Debug info, frame pointers and unwind tables: what the production binary carries
  - 26.4.5 Operating PGO/BOLT: the profile lifecycle
- 26.5 Pitfalls & anti-patterns: `-Ofast` hazards, portability loss
- 26.6 Exercises & checklist

### [27. Build Systems, Build-Time Hygiene & Hermetic Toolchains](27-build-systems-build-time-hygiene-hermetic-toolchains.md)
- 27.1 Why it matters: the build is a latency path, and a correctness boundary
- 27.2 Mental model
  - 27.2.1 Where the time goes: parse, instantiate, optimize, emit, link
  - 27.2.2 The one-definition rule across flags: how builds drift
  - 27.2.3 Hermeticity and reproducibility: controlling inputs, proving outputs
  - 27.2.4 What a build system is for — CMake+Ninja and Bazel/Buck2 as policies
- 27.3 Measure it: profiling the build like a latency path
- 27.4 Techniques
  - 27.4.1 Profile the build: `-ftime-trace`, ClangBuildAnalyzer, `-ftime-report`
  - 27.4.2 Explicit instantiation and `extern template` — when it pays, and when it costs
  - 27.4.3 Precompiled headers and unity builds — and their ODR traps
  - 27.4.4 C++20 modules today: what works, what does not
  - 27.4.5 One flag policy without ODR/ABI drift
  - 27.4.6 Hermetic, pinned toolchains and reproducible builds
- 27.5 Pitfalls & anti-patterns: the build system is part of the type system
- 27.6 Exercises & checklist

## Part IV — Memory Management

*Allocation cost, custom allocators, cache-friendly containers, and memory mapping.*

### [28. Memory Management Fundamentals](28-memory-management-fundamentals.md)
- 28.1 Why it matters: `malloc` can stall on the hot path
- 28.2 Mental model: stack vs heap; page faults; allocator internals
- 28.3 Measure it: allocation-cost and page-fault distribution
- 28.4 Techniques
  - 28.4.1 Stack and pre-allocated buffers
  - 28.4.2 Pre-faulting pages
- 28.5 Pitfalls & anti-patterns: hidden allocations; minor/major faults
- 28.6 Exercises & checklist

### [29. Custom Allocators](29-custom-allocators.md)
- 29.1 Why it matters: steady-state zero allocation
- 29.2 Mental model: arena/pool/slab strategies; `std::pmr`
- 29.3 Measure it: pool vs `new`/`delete` latency
- 29.4 Techniques
  - 29.4.1 Arena and bump allocators
  - 29.4.2 Object pools and free-lists
  - 29.4.3 `std::pmr` memory resources
- 29.5 Pitfalls & anti-patterns: fragmentation, lifetime bugs
- 29.6 Exercises & checklist

### [30. Hot-Path-Hostile STL & Cache-Friendly Containers](30-hotpath-hostile-stl-cache-friendly-containers.md)
- 30.1 Why it matters: node-based containers chase pointers
- 30.2 Mental model: costs of `unordered_map`/`map`/`function`/`shared_ptr`
- 30.3 Measure it: `std::unordered_map` vs flat hashing
- 30.4 Techniques
  - 30.4.1 Flat / open-addressing & robin-hood hashing
  - 30.4.2 `std::flat_map`/`std::flat_set` (C++23)
  - 30.4.3 Intrusive, fixed-capacity and small-buffer containers
  - 30.4.4 Slot maps and generational handles
  - 30.4.5 Case Study — Building the Limit Order Book
- 30.5 Pitfalls & anti-patterns: `shared_ptr` refcount churn
- 30.6 Exercises & checklist

### [31. Memory Mapping](31-memory-mapping.md)
- 31.1 Why it matters: shared memory and zero-copy I/O
- 31.2 Mental model: `mmap`, file-backed vs anonymous, page faulting
- 31.3 Measure it: cost of the first touch vs pre-faulted
- 31.4 Techniques
  - 31.4.1 Shared-memory regions
  - 31.4.2 Pre-faulting and `mlock`
- 31.5 Pitfalls & anti-patterns: lazy faults on the hot path
- 31.6 Exercises & checklist

## Part V — Numerics & Data Parallelism

*Representing prices exactly, bit tricks, and SIMD.*

### [32. Fixed-Point & Floating-Point Arithmetic](32-fixed-point-floating-point-arithmetic.md)
- 32.1 Why it matters: representing prices without losing money
- 32.2 Mental model
  - 32.2.1 Scaled integers vs decimal vs binary float
  - 32.2.2 Rounding and tick-size handling
  - 32.2.3 Denormals and the FTZ/DAZ flags
  - 32.2.4 FP op latency/throughput; NaN/Inf traps
- 32.3 Measure it: denormal slowdown; FP op latencies
- 32.4 Techniques
  - 32.4.1 Scaled-integer price math
  - 32.4.2 Enabling FTZ/DAZ
  - 32.4.3 Cross-build determinism on the hot path
- 32.5 Verify the codegen: FP flags and instruction selection
- 32.6 Pitfalls & anti-patterns: float equality, accumulated error
- 32.7 Exercises & checklist

### [33. Bit Manipulation & Integer Tricks](33-bit-manipulation-integer-tricks.md)
- 33.1 Why it matters: one instruction instead of a loop
- 33.2 Mental model: BMI/BMI2 (`popcnt`, `lzcnt`/`tzcnt`, `pdep`/`pext`, `blsr`)
- 33.3 Measure it: intrinsic latency/throughput
- 33.4 Techniques
  - 33.4.1 Bitsets/bitmaps for order-book occupancy and free-lists
  - 33.4.2 Branchless min/max/abs/sign
  - 33.4.3 Power-of-two rounding and fast modulo via masks
  - 33.4.4 Field packing/unpacking; Morton/Z-order interleaving
- 33.5 Verify the codegen: intrinsic emission
- 33.6 Pitfalls & anti-patterns: `pdep`/`pext` on AMD; portability
- 33.7 Exercises & checklist

### [34. SIMD & Vectorization](34-simd-vectorization.md)
- 34.1 Why it matters: data parallelism for batch work
- 34.2 Mental model: auto-vectorization; SSE/AVX/AVX-512; downclocking
- 34.3 Measure it: scalar vs vectorized throughput (and frequency effects)
- 34.4 Techniques
  - 34.4.1 Data layout for vectorization
  - 34.4.2 Intrinsics when the compiler won't
  - 34.4.3 Choosing the ISA width
  - 34.4.4 `std::simd` and portable wrappers
- 34.5 Verify the codegen: vector instruction selection
- 34.6 Pitfalls & anti-patterns: AVX-512 downclocking; misaligned loads
- 34.7 Exercises & checklist

## Part VI — Concurrency

*The memory model, lock-free structures, the Disruptor, threading topologies and the hot loop, flow control and conflation, coroutines, std::execution, and correctness tooling.*

### [35. The C++ Memory Model & Atomics](35-cpp-memory-model-atomics.md)
- 35.1 Why it matters: correctness and cost of sharing
- 35.2 Mental model: `memory_order`; acquire/release/seq_cst; fences; reordering
- 35.3 Measure it: cost per memory order
- 35.4 Techniques
  - 35.4.1 Choosing the weakest correct ordering
  - 35.4.2 Fences vs ordered atomics
  - 35.4.3 Beyond `std::atomic<T>`: `atomic_ref`, `wait`/`notify`, and wide atomics
- 35.5 Verify the codegen: barriers emitted per ordering (x86 vs ARM)
- 35.6 Pitfalls & anti-patterns: seq_cst by default; data races
- 35.7 Exercises & checklist

### [36. Multithreading & Concurrency Foundations](36-multithreading-concurrency-foundations.md)
- 36.1 Why it matters: scaling without contention
- 36.2 Mental model: contention, scalability limits, shared-nothing
- 36.3 Measure it: scalability curve vs thread count
- 36.4 Techniques
  - 36.4.1 Shared-nothing partitioning
  - 36.4.2 Minimizing shared mutable state
- 36.5 Pitfalls & anti-patterns: false scaling, oversubscription
- 36.6 Exercises & checklist

### [37. Spinlocks, Backoff & Contention Control](37-spinlocks-backoff-contention-control.md)
- 37.1 Why it matters: spin vs block on a hot core
- 37.2 Mental model: test-and-test-and-set; `pause`/`tpause`; futex internals
- 37.3 Measure it: spin vs mutex under contention
- 37.4 Techniques
  - 37.4.1 TTAS spinlocks
  - 37.4.2 Exponential backoff
  - 37.4.3 MCS/CLH queue locks
- 37.5 Pitfalls & anti-patterns: spinning across the scheduler; lock convoy
- 37.6 Exercises & checklist

### [38. False Sharing & Thread-Safety Anomalies](38-false-sharing-thread-safety-anomalies.md)
- 38.1 Why it matters: invisible cache-line ping-pong
- 38.2 Mental model: coherence traffic; `hardware_destructive_interference_size`
- 38.3 Measure it: HITM counters with/without padding
- 38.4 Techniques
  - 38.4.1 Alignment and padding to a cache line
  - 38.4.2 Per-thread/per-core data
- 38.5 Pitfalls & anti-patterns: adjacent hot counters; padding bloat
- 38.6 Exercises & checklist

### [39. Lock-Free Data Structures](39-lock-free-data-structures.md)
- 39.1 Why it matters: progress without locks
- 39.2 Mental model: CAS loops, ABA, progress guarantees
- 39.3 Measure it: SPSC ring throughput/latency
- 39.4 Techniques
  - 39.4.1 SPSC and MPMC queues
  - 39.4.2 Ring buffers
  - 39.4.3 Avoiding ABA (tagged pointers)
- 39.5 Pitfalls & anti-patterns: livelock, lost wakeups
- 39.6 Exercises & checklist

### [40. Seqlocks & Single-Writer Publication](40-seqlocks-single-writer-publication.md)
- 40.1 Why it matters: publishing market data to many readers
- 40.2 Mental model: versioned/sequence-lock snapshots; single-writer/multi-reader
- 40.3 Measure it: seqlock read latency under writer activity
- 40.4 Techniques
  - 40.4.1 Sequence-number publication
  - 40.4.2 Torn-read avoidance
- 40.5 Pitfalls & anti-patterns: correctness under the memory model
- 40.6 Exercises & checklist

### [41. Safe Memory Reclamation](41-safe-memory-reclamation.md)
- 41.1 Why it matters: freeing memory under lock-free readers
- 41.2 Mental model: the reclamation problem
- 41.3 Measure it: reclamation overhead comparison
- 41.4 Techniques
  - 41.4.1 RCU
  - 41.4.2 Hazard pointers
  - 41.4.3 Epoch-based reclamation
  - 41.4.4 The escape hatch: avoid reclamation
- 41.5 Pitfalls & anti-patterns: use-after-free, unbounded deferral
- 41.6 Exercises & checklist

### [42. The Disruptor Pattern](42-the-disruptor-pattern.md)
- 42.1 Why it matters: a proven low-latency pipeline
- 42.2 Mental model: ring buffer + sequence barriers; mechanical sympathy
- 42.3 Measure it: Disruptor vs queue latency
- 42.4 Techniques
  - 42.4.1 Sequence barriers and dependency graphs
  - 42.4.2 Batching at the consumer
- 42.5 Pitfalls & anti-patterns: false sharing on sequences
- 42.6 Exercises & checklist

### [43. Threading Topologies & the Hot Loop](43-threading-topologies-hot-loop.md)
- 43.1 Why it matters: the number of threads a tick touches is the latency floor
- 43.2 Mental model
  - 43.2.1 Three topologies
  - 43.2.2 The hop-cost model
  - 43.2.3 The hot loop is a scheduler
  - 43.2.4 The slot lifecycle is a protocol
- 43.3 Measure it: the hop, the three topologies, and the loop's own tax
- 43.4 Techniques
  - 43.4.1 Choosing the topology: a decision procedure
  - 43.4.2 The many-source hot loop: polling order, batch caps, the timer queue
  - 43.4.3 Mapping stages and shards onto cores and NUMA nodes
  - 43.4.4 The slot-ownership protocol as types
  - 43.4.5 Testing the protocol: a stage that holds a view one sequence too long
- 43.5 Pitfalls & anti-patterns
- 43.6 Exercises & checklist

### [44. Flow Control, Backpressure & Conflation on the Hot Path](44-flow-control-backpressure-conflation.md)
- 44.1 Why it matters: every trading system has a consumer slower than the open
- 44.2 Mental model
  - 44.2.1 The policy taxonomy for a full channel, by consumer class
  - 44.2.2 Conflation structures: latest-value slot + dirty set
  - 44.2.3 Staleness as a first-class metric
  - 44.2.4 Degraded modes and load shedding at the open
- 44.3 Measure it: a replayed microburst against a slow consumer
- 44.4 Techniques
  - 44.4.1 Declaring the policy per channel, in the type
  - 44.4.2 Building the conflator: bitmap, deduplicating queue, priorities, merge semantics
  - 44.4.3 Measuring staleness and setting budgets
  - 44.4.4 Degraded modes: the shedding plan as a state machine
  - 44.4.5 Retiring the scattered policies
- 44.5 Pitfalls & anti-patterns
- 44.6 Exercises & checklist

### [45. Coroutines & Async Models](45-coroutines-async-models.md)
- 45.1 Why it matters: async I/O without thread-per-connection
- 45.2 Mental model: C++20 coroutines; stackful vs stackless
- 45.3 Measure it: coroutine resume cost
- 45.4 Techniques
  - 45.4.1 Event loops
  - 45.4.2 Custom awaiters for I/O
- 45.5 Verify the codegen: did the frame get elided?
- 45.6 Pitfalls & anti-patterns: hidden allocations in coroutine frames
- 45.7 Exercises & checklist

### [46. std::execution: Senders/Receivers & Structured Async](46-std-execution-senders-receivers.md)
- 46.1 Why it matters: a standard async model, if it's actually zero-cost
- 46.2 Mental model: senders, receivers, schedulers, and structured concurrency
- 46.3 Measure it: sender pipeline overhead vs a hand-written state machine
- 46.4 Techniques
  - 46.4.1 A custom low-latency scheduler pinned to the hot core
  - 46.4.2 Sender pipelines and structured concurrency for the hot path
  - 46.4.3 When senders help — and when to stay hand-written
- 46.5 Verify the codegen: does the pipeline compile away?
- 46.6 Pitfalls & anti-patterns: hidden hops, type erasure, and allocation
- 46.7 Exercises & checklist

### [47. Concurrency Correctness Tooling](47-concurrency-correctness-tooling.md)
- 47.1 Why it matters: lock-free bugs are non-deterministic
- 47.2 Mental model: what TSan/ASan/UBSan can and can't catch
- 47.3 Measure it: running the sanitizers on a queue
- 47.4 Techniques
  - 47.4.1 Stress testing
  - 47.4.2 Fuzzing
  - 47.4.3 Model checking lock-free code
- 47.5 Pitfalls & anti-patterns: sanitizer-clean but still racy
- 47.6 Exercises & checklist

## Part VII — OS, Scheduling & Isolation

*Building and keeping a quiet, isolated, warm core — timers and deadlines without jitter, cache and bandwidth partitioning included — and attributing the noise that remains.*

### [48. Context Switching & Its Mitigation](48-context-switching-mitigation.md)
- 48.1 Why it matters: a switch trashes the caches and TLB
- 48.2 Mental model: direct vs indirect costs; syscall overhead
- 48.3 Measure it: context-switch and null-syscall cost
- 48.4 Techniques
  - 48.4.1 Keeping threads on-core
  - 48.4.2 Busy-poll vs block
  - 48.4.3 Signals: delivery cost, masking and funneling
- 48.5 Pitfalls & anti-patterns: involuntary preemption, syscalls on the hot path
- 48.6 Exercises & checklist

### [49. Timers & Deadlines Without Jitter](49-timers-deadlines-without-jitter.md)
- 49.1 Why it matters: every order carries a deadline, and the kernel's clock is 50 µs late by default
- 49.2 Mental model
  - 49.2.1 Two kinds of thread, two kinds of timer
  - 49.2.2 Deadline queues for the busy loop: list, heap, wheel
  - 49.2.3 Timer fires are inputs
  - 49.2.4 The Linux timer stack beneath a sleeping thread
- 49.3 Measure it: the deadline queue at 10⁴–10⁶ timers, and the kernel's wake-up
- 49.4 Techniques
  - 49.4.1 The polled deadline queue: wheel + heap over pooled nodes, bounded per tick
  - 49.4.2 Deadlines as data: TTLs, cancel-if-no-ack, quote refresh, heartbeats, throttles
  - 49.4.3 Replayable time: the fire in the journal, the clock as an input
  - 49.4.4 Sleeping threads done right: slack, `TIMER_ABSTIME`, `timerfd`
  - 49.4.5 Measuring your timers in production
- 49.5 Pitfalls & anti-patterns
- 49.6 Exercises & checklist

### [50. Thread & Interrupt Pinning](50-thread-interrupt-pinning.md)
- 50.1 Why it matters: own the core, evict the noise
- 50.2 Mental model: CPU affinity and IRQ routing
- 50.3 Measure it: jitter with/without pinning
- 50.4 Techniques
  - 50.4.1 `taskset`/`sched_setaffinity`
  - 50.4.2 IRQ affinity and isolating the hot core
  - 50.4.3 Choosing *which* cores: read the core-to-core map
- 50.5 Pitfalls & anti-patterns: leftover IRQs on the hot core
- 50.6 Exercises & checklist

### [51. SMT / Hyperthreading](51-smt-hyperthreading.md)
- 51.1 Why it matters: a sibling thread steals your resources
- 51.2 Mental model: partitioned vs competitively-shared resources; throughput-vs-latency
- 51.3 Measure it: per-thread interference with the sibling busy
- 51.4 Techniques
  - 51.4.1 Detecting sibling topology
  - 51.4.2 Leaving the sibling idle vs housekeeping-only
  - 51.4.3 Deciding HT on/off per core
- 51.5 Pitfalls & anti-patterns: co-scheduling two hot threads on one core
- 51.6 Exercises & checklist

### [52. Cache Allocation Technology & Intel RDT](52-cache-allocation-intel-rdt.md)
- 52.1 Why it matters: pinning the core isn't enough if the cache is shared
- 52.2 Mental model: RDT, CLOS, capacity bitmasks, and `resctrl`
- 52.3 Measure it: a noisy neighbor vs a partitioned cache
- 52.4 Techniques
  - 52.4.1 Partitioning the L3 with CAT via `resctrl`
  - 52.4.2 Throttling neighbors with MBA; monitoring with CMT/MBM
  - 52.4.3 Partitioning for DDIO and multi-tenant boxes
- 52.5 Pitfalls & anti-patterns: over-partitioning and overlapping masks
- 52.6 Exercises & checklist

### [53. Real-Time Scheduling & Kernel Tuning](53-realtime-scheduling-kernel-tuning.md)
- 53.1 Why it matters: eliminating scheduler-induced jitter
- 53.2 Mental model: `isolcpus`, `nohz_full`, RT priorities, cgroups
- 53.3 Measure it: `cyclictest` before/after
- 53.4 Techniques
  - 53.4.1 CPU isolation and tickless cores
  - 53.4.2 RT scheduling classes and priorities
- 53.5 Pitfalls & anti-patterns: RT throttling, priority inversion
- 53.6 Exercises & checklist

### [54. Power Management & Frequency Determinism](54-power-management-frequency-determinism.md)
- 54.1 Why it matters: the first message after a quiet spell
- 54.2 Mental model: four clocks and three sleepers
  - 54.2.1 C-states: exit latency, target residency and the governor's bet
  - 54.2.2 PM QoS: how the kernel is told not to sleep — and what `tuned` actually does
  - 54.2.3 P-states, HWP and EPP: who sets the frequency
  - 54.2.4 Power limits: PL1/PL2, RAPL, ICCmax and thermal
  - 54.2.5 The uncore clock
  - 54.2.6 AVX frequency licences — a pointer
- 54.3 Measure it: the idle-state table, the first-message tail, and proving the clock stayed flat
- 54.4 Techniques
  - 54.4.1 Per-CPU cpuidle control: deep states on housekeeping cores, C1 on hot cores
  - 54.4.2 PM QoS from the process: the holder that must not die
  - 54.4.3 HWP/EPP, governor and the active/passive choice
  - 54.4.4 Pinning the uncore and lifting the power limits
  - 54.4.5 The flat-clock monitor in production
- 54.5 Pitfalls & anti-patterns: the clock that moved without telling you
- 54.6 Exercises & checklist

### [55. Hunting Residual Noise on Isolated Cores](55-hunting-residual-noise-isolated-cores.md)
- 55.1 Why it matters: the last 40 µs has a name
- 55.2 Mental model: the kernel noise catalog (per-CPU kthreads, workqueues, RCU, reschedule/function-call/TLB-shootdown IPIs, managed vectors, NMI/PMI, memory daemons, clocksource watchdog, SMIs — fingerprint and knob for each)
- 55.3 Measure it: `rtla osnoise` and `timerlat` beside `cyclictest`; a user-space TSC gap detector
- 55.4 Techniques
  - 55.4.1 Catalog audit: what is allowed on the core right now
  - 55.4.2 Stop-on-threshold tracing: catching the spike in the act (ftrace, `trace-cmd`/KernelShark, `perf sched`)
  - 55.4.3 Turning the knobs: one fix per catalog row
- 55.5 Pitfalls & anti-patterns: perturbing the hunt, chasing ghosts
- 55.6 Exercises & checklist

### [56. Keeping the Hot Path Warm](56-keeping-the-hot-path-warm.md)
- 56.1 Why it matters: the first real message must not be cold
- 56.2 Mental model: cache/TLB/branch-predictor state decay
- 56.3 Measure it: warm vs cold tail latency
- 56.4 Techniques
  - 56.4.1 Shadow/dummy traffic
  - 56.4.2 Pre-touching pages and connections
- 56.5 Pitfalls & anti-patterns: cold-start stalls, warming the wrong state
- 56.6 Exercises & checklist

## Part VIII — Kernel I/O, Sockets & Zero-Copy

*Getting data in and out through the kernel as fast as the kernel allows — up to the modern zero-copy fast path.*

### [57. Linux Native I/O](57-linux-native-io.md)
- 57.1 Why it matters: the baseline before bypass
- 57.2 Mental model: readiness vs completion; blocking vs non-blocking
- 57.3 Measure it: `epoll` round-trip latency
- 57.4 Techniques
  - 57.4.1 `epoll` event loops
  - 57.4.2 Syscall batching
- 57.5 Pitfalls & anti-patterns: thundering herd, edge/level confusion
- 57.6 Exercises & checklist

### [58. io_uring Deep Dive](58-io-uring-deep-dive.md)
- 58.1 Why it matters: completion-based I/O with fewer syscalls
- 58.2 Mental model: submission/completion queues; polling mode
- 58.3 Measure it: io_uring vs `epoll`
- 58.4 Techniques
  - 58.4.1 Registered buffers and files
  - 58.4.2 Polling mode for lowest latency
  - 58.4.3 Batching submissions
- 58.5 Pitfalls & anti-patterns: completion-ordering assumptions
- 58.6 Exercises & checklist

### [59. Zero-Copy & the Modern Kernel Fast Path](59-zero-copy-modern-kernel-fast-path.md)
- 59.1 Why it matters: the copy is a cost you pay on every byte
- 59.2 Mental model: where the copies are, and how zero-copy removes them
- 59.3 Measure it: copy cost, the zero-copy threshold, and RX latency
- 59.4 Techniques
  - 59.4.1 Zero-copy send: `MSG_ZEROCOPY` / `SO_ZEROCOPY`
  - 59.4.2 Zero-copy receive: `TCP_ZEROCOPY_RECEIVE` and mmap
  - 59.4.3 io_uring zero-copy send and receive
  - 59.4.4 devmem TCP: payload straight to device memory
- 59.5 Pitfalls & anti-patterns: buffer lifetime and the small-message trap
- 59.6 Exercises & checklist

### [60. Inter-Process Communication](60-inter-process-communication.md)
- 60.1 Why it matters: splitting work across processes cheaply
- 60.2 Mental model: shared-memory queues vs pipes
- 60.3 Measure it: shm ring vs pipe latency
- 60.4 Techniques
  - 60.4.1 Lock-free cross-process ring buffers
  - 60.4.2 Shared-memory layout and addressing
  - 60.4.3 Segment setup and cross-process synchronization
- 60.5 Pitfalls & anti-patterns: pointers across address spaces
- 60.6 Exercises & checklist

### [61. Socket Optimization & TCP/Protocol Tuning](61-socket-optimization-tcp-tuning.md)
- 61.1 Why it matters: defaults cost microseconds
- 61.2 Mental model: Nagle, buffering, congestion control, multicast
- 61.3 Measure it: `TCP_NODELAY` on/off RTT
- 61.4 Techniques
  - 61.4.1 `TCP_NODELAY` and buffer sizing
  - 61.4.2 Multicast for market data
- 61.5 Pitfalls & anti-patterns: Nagle/delayed-ACK interaction
- 61.6 Exercises & checklist

### [62. Advanced TCP Internals & Tuning](62-advanced-tcp-internals-tuning.md)
- 62.1 Why it matters: TCP's defaults optimize for the wrong thing
- 62.2 Mental model: the send path, the ACK clock, and congestion control
- 62.3 Measure it: the connection's internal state across a burst and an idle gap
- 62.4 Techniques
  - 62.4.1 Send-path internals: autocorking, the ACK clock, the cold window, pacing
  - 62.4.2 Offloads, congestion control, and buffer tuning
  - 62.4.3 Bypassing the stack where TCP itself is the tax
  - 62.4.4 Failure detection: how long until TCP tells you the gateway is gone
- 62.5 Pitfalls & anti-patterns: autocorking, spurious recovery and offload latency
- 62.6 Exercises & checklist

## Part IX — Market Data, NIC & Fabric

*Decoding the feed, recovering losses, keeping the multicast membership alive, and the NIC, cache, timing, and fabric hardware around the receive and send paths.*

### [63. Zero-Copy Wire Handling & Market-Data Decoding](63-zero-copy-wire-market-data-decoding.md)
- 63.1 Why it matters: parse off the wire without copies
- 63.2 Mental model
  - 63.2.1 ITCH/OUCH/FIX/SBE/FAST encodings
  - 63.2.2 Fixed-layout structs vs bit-fields; endianness
  - 63.2.3 Redundant A/B multicast feeds
  - 63.2.4 Mechanical sympathy for UDP microbursts: hardware RX rings, software reorder buffers, market-open drops
- 63.3 Measure it: decode latency per protocol
- 63.4 Techniques
  - 63.4.1 `std::from_chars`/`to_chars`; branch-free integer/decimal parsing
  - 63.4.2 Scatter/gather and zero-copy receive paths
  - 63.4.3 A/B line arbitration; sequence-gap detection (recovery → Ch. 64)
  - 63.4.4 Tuning RX ring sizes, managing UDP reordering in software ring buffers, detecting hardware packet drops
- 63.5 Verify the codegen: parse loop
- 63.6 Pitfalls & anti-patterns: unaligned reads, trusting wire input *(ties Ch. 90)*
- 63.7 Exercises & checklist

### [64. Reliable Multicast & Feed Recovery](64-reliable-multicast-feed-recovery.md)
- 64.1 Why it matters: market data is lossy, and a gap blinds the strategy
- 64.2 Mental model: sequence gaps and the three-channel recovery design
- 64.3 Measure it: gap rates, recovery latency, and hot-path impact
- 64.4 Techniques
  - 64.4.1 A/B line arbitration and gap detection
  - 64.4.2 Retransmission and snapshot recovery off the hot path
  - 64.4.3 Trading through a gap: pause, continue, or safe-state
  - 64.4.4 Feed liveness, data-quality invariants and session health beyond gaps (age vs heartbeat cadence, A/B skew, crossed/locked-book checks, FIX heartbeat/TestRequest, the stale-data kill, source failover beyond A/B)
- 64.5 Pitfalls & anti-patterns: blocking on recovery and NAK storms
- 64.6 Exercises & checklist

### [65. Multicast Group Management & Dark-Feed Detection](65-multicast-group-management-dark-feed.md)
- 65.1 Why it matters: a lost membership produces no gap
- 65.2 Mental model: IGMP, snooping, the querier, and the distribution tree
  - 65.2.1 Reports, queries and leaves (IGMPv2/v3, MLD, SSM, version agreement)
  - 65.2.2 Snooping switches and the querier problem (the ~260 s dark feed; unknown-multicast flooding)
  - 65.2.3 From the exchange to your port: PIM, RPF and layer-1 replication
  - 65.2.4 The four ways a feed goes dark without a gap
- 65.3 Measure it: join latency (and the market-open join storm), time-to-dark after querier loss, the cost of the watchdog
- 65.4 Techniques
  - 65.4.1 Joining correctly: SSM, interface-bound, group-bound, A/B on disjoint NICs
  - 65.4.2 The Linux traps: `igmp_max_memberships`, `IP_MULTICAST_ALL`, `rp_filter`, versions
  - 65.4.3 Verifying membership from the host (`/proc/net/igmp`, `ip maddr`, `tcpdump igmp`, `nstat`)
  - 65.4.4 The dark-feed watchdog and the re-join policy
  - 65.4.5 Kernel-bypass joins: who sends the report?
- 65.5 Pitfalls & anti-patterns: silence is not a quiet market
- 65.6 Exercises & checklist

### [66. NIC Features & Offloads](66-nic-features-offloads.md)
- 66.1 Why it matters: push work into the card
- 66.2 Mental model: RSS, offloads, hardware timestamping
- 66.3 Measure it: offload on/off CPU cost
- 66.4 Techniques
  - 66.4.1 RSS and flow steering
  - 66.4.2 Busy-polling
  - 66.4.3 Solarflare/Onload
- 66.5 Pitfalls & anti-patterns: offloads that add latency
- 66.6 Exercises & checklist

### [67. DDIO & the NIC-to-Cache Data Path](67-ddio-nic-cache-data-path.md)
- 67.1 Why it matters: where a received packet lands decides the RX latency floor
- 67.2 Mental model: DMA into L3, the DDIO region, and allocating writes
- 67.3 Measure it: DDIO hit vs DRAM, and DDIO cache pollution
- 67.4 Techniques
  - 67.4.1 Keeping RX buffers DDIO-resident and NIC-local
  - 67.4.2 Separating the DDIO region from the hot working set with CAT
  - 67.4.3 When to disable DDIO, and cross-socket/peer-to-peer cases
- 67.5 Pitfalls & anti-patterns: DDIO thrashing and the cross-socket trap
- 67.6 Exercises & checklist

### [68. Flow Steering & Receive Scaling](68-flow-steering-receive-scaling.md)
- 68.1 Why it matters: the right packet must reach the right core
- 68.2 Mental model: RSS, flow steering, and the RX-to-core-to-cache chain
- 68.3 Measure it: cross-core handoff cost and steering benefit
- 68.4 Techniques
  - 68.4.1 RSS and RSS contexts for deliberate spreading
  - 68.4.2 Exact flow steering: n-tuple, Flow Director, and `rte_flow`
  - 68.4.3 aRFS, `SO_REUSEPORT`, and batched receive
- 68.5 Pitfalls & anti-patterns: scattering a feed and steering to the wrong core
- 68.6 Exercises & checklist

### [69. Clock Synchronization & Hardware Timestamping (PTP)](69-clock-synchronization-ptp.md)
- 69.1 Why it matters: comparing timestamps across machines
- 69.2 Mental model: PTP distribution; NIC hardware timestamps
- 69.3 Measure it: PTP offset/jitter; true wire-to-wire latency
- 69.4 Techniques
  - 69.4.1 PTP setup and disciplining
  - 69.4.2 Tick-to-trade measurement across hosts
  - 69.4.3 Advanced synchronization: sub-nanosecond White Rabbit, PPS signaling for boundary clocks, grandmaster oscillator drift
- 69.5 Pitfalls & anti-patterns: software-timestamp error
- 69.6 Exercises & checklist

### [70. Precise & Scheduled Transmission](70-precise-scheduled-transmission.md)
- 70.1 Why it matters: "send now" is not the only option
- 70.2 Mental model: EDT, `SO_TXTIME`, qdiscs, and hardware LAUNCHTIME
- 70.3 Measure it: send-time accuracy and pacing precision
- 70.4 Techniques
  - 70.4.1 `SO_TXTIME` + ETF + hardware LAUNCHTIME
  - 70.4.2 Pacing: `SO_MAX_PACING_RATE`, FQ, and the EDT model
  - 70.4.3 TSN, time-aware shaping, and coordinated sends
- 70.5 Pitfalls & anti-patterns: clock discipline and missed deadlines
- 70.6 Exercises & checklist

### [71. Network Fabric & Switching](71-network-fabric-switching.md)
- 71.1 Why it matters: the switch is on the tick-to-trade path
- 71.2 Mental model: store-and-forward, cut-through, and layer-1 switches
- 71.3 Measure it: per-hop and one-way latency with hardware timestamps
- 71.4 Techniques
  - 71.4.1 Choosing switching technology and minimizing hops
  - 71.4.2 Fabric layout, buffering, and microburst handling
  - 71.4.3 Switch-based replication, timestamping, and taps
  - 71.4.4 Passive measurement: taps vs SPAN, timestamped mirrors and latency appliances
- 71.5 Pitfalls & anti-patterns: store-and-forward, oversubscription, and hidden hops
- 71.6 Exercises & checklist

## Part X — Kernel Bypass, RDMA & Transport

*Removing the kernel entirely, one-sided remote memory, lossless fabric, and purpose-built transports beyond TCP.*

### [72. eBPF, bpftrace & XDP / AF_XDP](72-ebpf-bpftrace-xdp.md)
- 72.1 Why it matters: low-overhead production tracing and fast packet paths
- 72.2 Mental model: the eBPF VM and verifier
- 72.3 Measure it: probe overhead
- 72.4 Techniques
  - 72.4.1 kprobes/uprobes/tracepoints/USDT; `bpftrace` one-liners
  - 72.4.2 XDP for in-driver drop/redirect/filter
  - 72.4.3 AF_XDP zero-copy sockets
- 72.5 Pitfalls & anti-patterns: verifier limits; tracing overhead near the hot path
- 72.6 Exercises & checklist

### [73. Kernel Bypass & Userspace Networking](73-kernel-bypass-userspace-networking.md)
- 73.1 Why it matters: the lowest-latency software path
- 73.2 Mental model: poll-mode drivers; userspace stacks
- 73.3 Measure it: kernel-stack vs DPDK RX latency
- 73.4 Techniques
  - 73.4.1 DPDK and hugepage mempools
  - 73.4.2 Userspace TCP stacks
  - 73.4.3 The tick-to-trade fast path
  - 73.4.4 Proprietary vendor APIs: Solarflare `ef_vi` and ExaNIC `libexanic` for zero-copy NIC ring access; transparent vs explicit bypass latency floors
- 73.5 Pitfalls & anti-patterns: burning cores; losing kernel tooling
- 73.6 Exercises & checklist

### [74. InfiniBand Verbs & RDMA](74-infiniband-verbs-rdma.md)
- 74.1 Why it matters: one-sided remote memory access
- 74.2 Mental model: queue pairs, completion queues, memory regions/registration, work requests
- 74.3 Measure it: RDMA write vs send/recv latency
- 74.4 Techniques
  - 74.4.1 RDMA read/write vs send/recv
  - 74.4.2 RoCEv2 vs native InfiniBand
  - 74.4.3 Polling completions; registered-memory/zero-copy
- 74.5 Pitfalls & anti-patterns: registration cost, memory pinning
- 74.6 Exercises & checklist

### [75. Lossless Ethernet & RDMA Congestion](75-lossless-ethernet-rdma-congestion.md)
- 75.1 Why it matters: RDMA assumes a lossless fabric that Ethernet isn't
- 75.2 Mental model: PFC, ECN, DCQCN, and the RoCEv2 congestion problem
- 75.3 Measure it: RDMA under congestion and PFC behavior
- 75.4 Techniques
  - 75.4.1 Configuring PFC and DCB for lossless RoCEv2
  - 75.4.2 ECN and DCQCN congestion control
  - 75.4.3 Handling incast and avoiding PFC deadlock
- 75.5 Pitfalls & anti-patterns: PFC deadlock and assuming Ethernet is lossless
- 75.6 Exercises & checklist

### [76. Transport Beyond TCP: Aeron, eRPC, Homa & Custom Reliable UDP](76-transport-beyond-tcp.md)
- 76.1 Why it matters: TCP is a general-purpose stream, and you're sending messages
- 76.2 Mental model: the transport design space
- 76.3 Measure it: message rate, latency, and HOL blocking vs TCP
- 76.4 Techniques
  - 76.4.1 Aeron: reliable UDP messaging, unicast and multicast
  - 76.4.2 eRPC: microsecond RPC on commodity hardware
  - 76.4.3 Homa: receiver-driven, connectionless, HOL-free
  - 76.4.4 Rolling your own reliable UDP
- 76.5 Pitfalls & anti-patterns: reinventing TCP badly
- 76.6 Exercises & checklist

## Part XI — Heterogeneous Computing & Hardware Acceleration

*Pushing work off the CPU onto GPUs, FPGAs, and SmartNICs — and where the PCIe round-trip keeps them off the order path.*

### [77. PCIe & the Host–Device Boundary](77-pcie-host-device-boundary.md)
- 77.1 Why it matters: the round-trip floor under every offload
- 77.2 Mental model
  - 77.2.1 PCIe topology, lanes/generations and bandwidth
  - 77.2.2 MMIO, BAR-mapped registers, DMA and the IOMMU
  - 77.2.3 Descriptor rings and doorbells; posted vs non-posted transactions
- 77.3 Measure it: device-register round-trip and streaming bandwidth
- 77.4 Techniques
  - 77.4.1 Write-combining and relaxed ordering
  - 77.4.2 MSI-X interrupts vs polling for completions
  - 77.4.3 Peer-to-peer DMA (GPUDirect, NIC-to-GPU/FPGA)
- 77.5 Pitfalls & anti-patterns: a device read on the hot path; IOMMU overhead
- 77.6 Exercises & checklist

### [78. GPU Computing with CUDA](78-gpu-computing-cuda.md)
- 78.1 Why it matters: data-parallel offload (and why not the order path)
- 78.2 Mental model: CUDA execution & memory model; warps/occupancy
- 78.3 Measure it: end-to-end including transfer, not just kernel time
- 78.4 Techniques
  - 78.4.1 Host–device transfer; pinned and unified memory
  - 78.4.2 Streams and compute/transfer overlap
  - 78.4.3 Where GPUs pay off (risk, pricing/Greeks, Monte-Carlo, backtesting, ML inference)
- 78.5 Pitfalls & anti-patterns: PCIe latency on the tick-to-trade path
- 78.6 Exercises & checklist

### [79. Distributed Computing with MPI](79-distributed-computing-mpi.md)
- 79.1 Why it matters: scaling across hosts (off the order path)
- 79.2 Mental model: communicators; point-to-point vs collective
- 79.3 Measure it: message latency vs size
- 79.4 Techniques
  - 79.4.1 Non-blocking calls and compute/communication overlap
  - 79.4.2 RDMA/InfiniBand transports
  - 79.4.3 NUMA- and topology-aware rank placement
- 79.5 Pitfalls & anti-patterns: collectives on the critical path
- 79.6 Exercises & checklist

### [80. FPGA Acceleration](80-fpga-acceleration.md)
- 80.1 Why it matters: deterministic nanosecond pipelines inline on the wire
- 80.2 Mental model: HLS vs RTL; the host–FPGA boundary
- 80.3 Measure it: wire-to-wire latency of a hardware path
- 80.4 Techniques
  - 80.4.1 NIC-integrated FPGAs and inline feed handling/order entry
  - 80.4.2 Partial reconfiguration
  - 80.4.3 Deciding what belongs in fabric vs software
- 80.5 Pitfalls & anti-patterns: host-boundary stalls; toolchain complexity
- 80.6 Exercises & checklist

### [81. SmartNICs & DPUs](81-smartnics-dpus.md)
- 81.1 Why it matters: a programmable hop between wire and host
- 81.2 Mental model: NIC-resident ARM cores / FPGA fabric (BlueField, IPU, Pensando)
  - 81.2.1 The two boundaries: wire↔DPU and DPU↔host
- 81.3 Measure it: DPU-offload latency vs host path
- 81.4 Techniques
  - 81.4.1 Programming models (DOCA, P4, eBPF/XDP offload)
  - 81.4.2 On-NIC RDMA and storage/security offload
  - 81.4.3 Partitioning work between DPU and host
- 81.5 Pitfalls & anti-patterns: when the DPU just adds a hop
- 81.6 Exercises & checklist

---

## Part XII — Beyond C++: Languages at the Latency Floor

*Stepping outside C++ to the instructions themselves and to the other languages at or near the floor: inline and hand-written assembly, Rust measured against C++ on the book's own kernels, the C ABI boundary and vendor-SDK wrappers, the managed runtimes measured from the hot thread, and the tier-by-tier choice with an equal-rigour cross-language protocol.*

### [82. Hand-Written & Inline Assembly](82-hand-written-inline-assembly.md)
- 82.1 Why it matters: when the compiler has no word for what you mean
- 82.2 Mental model
  - 82.2.1 Extended asm anatomy: template, outputs, inputs, clobbers
  - 82.2.2 What the optimizer may do around an asm statement
  - 82.2.3 The two asm statements you have used since Ch. 3
  - 82.2.4 Hand-written functions: the ABI is the whole contract
- 82.3 Measure it: asm vs intrinsic vs compiler, on this book's own instructions
- 82.4 Techniques
  - 82.4.1 Intrinsics first — and why that is not a cop-out
  - 82.4.2 The irreducible cases (`rdpmc`, serializing timestamps, `clwb`, doorbells)
  - 82.4.3 `asm goto`: a branch the compiler never materialises as a flag
  - 82.4.4 The constraints cookbook
  - 82.4.5 Writing a hand-written function that stays correct
- 82.5 Verify the codegen: the statement in its context
- 82.6 Pitfalls & anti-patterns: the undeclared clobber, and friends
- 82.7 Exercises & checklist
### [83. Rust for the Hot Path](83-rust-for-the-hot-path.md)
- 83.1 Why it matters: a second language at the floor
- 83.2 Mental model
  - 83.2.1 Ownership vs the zero-allocation discipline
  - 83.2.2 Layout you control: `repr`, `align`, padding
  - 83.2.3 Atomics and the memory model: the same model, the same instructions
  - 83.2.4 Bounds checks: where they are elided and where they stay
  - 83.2.5 `&mut` is `noalias`
  - 83.2.6 `unsafe` at the hardware boundary
- 83.3 Measure it: the book's kernels, Rust beside GCC and Clang
- 83.4 Techniques
  - 83.4.1 The wire overlay without `transmute`
  - 83.4.2 Indexing that compiles to a load
  - 83.4.3 The SPSC ring: where `unsafe` lives and why it is small
  - 83.4.4 The build profile for a hot path
  - 83.4.5 What maps to what: Part III in Rust
- 83.5 Verify the codegen: side by side
- 83.6 Pitfalls & anti-patterns: where parity breaks, and the one that is not Rust's fault
- 83.7 Exercises & checklist
### [84. Language Interop & ABI Boundaries](84-language-interop-abi-boundaries.md)
- 84.1 Why it matters: the hot path already crosses a boundary on every packet
- 84.2 Mental model
  - 84.2.1 The hourglass: one narrow C waist
  - 84.2.2 What may not cross
  - 84.2.3 The cost model: a call is a call; a transition is not
  - 84.2.4 Symbols, visibility and the dynamic linker
- 84.3 Measure it: the cross-language call ladder
- 84.4 Techniques
  - 84.4.1 Parity asserts on both sides
  - 84.4.2 Zero-cost RAII wrappers over vendor C SDKs
  - 84.4.3 Exceptions and panics at the boundary
  - 84.4.4 Shared memory across languages
  - 84.4.5 C and Zig idioms worth stealing
  - 84.4.6 Binding generators and where each belongs
- 84.5 Verify the codegen: the wrapper adds nothing; the boundary adds a PLT hop
- 84.6 Pitfalls & anti-patterns: per-message crossings and silent ABI drift
- 84.7 Exercises & checklist
### [85. Managed Runtimes Near the Hot Path, Measured](85-managed-runtimes-measured.md)
- 85.1 Why it matters: the runtime owns the timing of your tail
- 85.2 Mental model
  - 85.2.1 What a collector does to the hot thread
  - 85.2.2 Safepoints and time-to-safepoint
  - 85.2.3 Tiered JIT: interpreter, C1, C2, OSR, deoptimisation
  - 85.2.4 The four runtimes in one frame
- 85.3 Measure it: pauses, warm-up and the garbage-free line
- 85.4 Techniques
  - 85.4.1 Garbage-free code: the flyweight over the bytes
  - 85.4.2 Choosing and tuning the collector
  - 85.4.3 Warming the JIT, and pinning what it compiled
  - 85.4.4 .NET: `Span<T>`, `ref struct`, and the collector you cannot turn off
  - 85.4.5 Go: allocation-free loops, `GOGC`, and the scheduler
  - 85.4.6 OCaml: unboxed ints, the minor heap, and Jane Street's discipline
  - 85.4.7 One hop away: the managed reader of a C++-written ring
- 85.5 Verify the codegen: reading what the JIT emitted
- 85.6 Pitfalls & anti-patterns
- 85.7 Exercises & checklist
### [86. Choosing a Language per Latency Tier](86-choosing-a-language-per-latency-tier.md)
- 86.1 Why it matters: a one-time decision with a per-tick consequence
- 86.2 Mental model
  - 86.2.1 The tiers, restated with what the language controls
  - 86.2.2 What the language does not control
  - 86.2.3 Equal rigour: the protocol
  - 86.2.4 What this Part measured, and what it did not
- 86.3 Measure it: one kernel, nine toolchains
- 86.4 Techniques
  - 86.4.1 Running the protocol
  - 86.4.2 The tier table with measured floors
  - 86.4.3 `constexpr` vs `const fn` vs `comptime`: one table, three compilers, one `.rodata`
  - 86.4.4 The scorecard
  - 86.4.5 The polyglot architecture
  - 86.4.6 Parity testing across languages
  - 86.4.7 Three worked placements
- 86.5 Verify the codegen: the same bytes
- 86.6 Pitfalls & anti-patterns: the benchmark that compares warm-up to steady state
- 86.7 Exercises & checklist
## Part XIII — Observability & Operations in Production

*Running the system: logging, in-band latency tracing and telemetry, security, hot reload, process topology (with checkpointing and segment recovery), the pre-trade risk gate and kill-switch, idempotent order entry and order-state recovery, the order-entry gateway and session layer, crash handling, capture/replay and line-rate wire capture with timing-faithful replay, the sequencer and replicated consumers, failover without split-brain, latency-spike forensics, host and network health, the engineering lifecycle — testing, performance gates in CI and the profile lifecycle, release engineering — and the end-to-end case study.*

### [87. Zero-Overhead Logging](87-zero-overhead-logging.md)
- 87.1 Why it matters: logging must never stall the hot path
- 87.2 Mental model: deferred formatting; binary logging
- 87.3 Measure it: log-call cost on the hot path
- 87.4 Techniques
  - 87.4.1 Async/lock-free loggers
  - 87.4.2 Binary logging with offline formatting
  - 87.4.3 `std::print`/`std::println` for off-hot-path output
- 87.5 Verify the codegen: the hot-path log call is a few stores
- 87.6 Pitfalls & anti-patterns: formatting in the hot path; blocking on I/O
- 87.7 Exercises & checklist

### [88. In-Band Latency Tracing & Budget Attribution](88-in-band-latency-tracing-budget-attribution.md)
- 88.1 Why it matters: the budget you cannot attribute
- 88.2 Mental model
  - 88.2.1 The trace header: a correlation ID and a vector of stage stamps
  - 88.2.2 In-band vs out-of-band carriage
  - 88.2.3 Causality across processes: tick → decision → order
  - 88.2.4 Three clocks on one message
- 88.3 Measure it: what the stamp and the header cost
- 88.4 Techniques
  - 88.4.1 The trace header and the stamping points
  - 88.4.2 Carrying the header through rings, Disruptor slots and shm segments
  - 88.4.3 Contention-free correlation IDs and the tick → order link
  - 88.4.4 Off-path aggregation: from stamps to per-stage histograms
  - 88.4.5 Exchange-provided timestamps: attributing the budget between you, the wire and the matching engine
  - 88.4.6 The per-packet ledger: joining a tap capture to the host's stamps
- 88.5 Verify the codegen: a stage boundary is a counter read and a store
- 88.6 Pitfalls & anti-patterns
- 88.7 Exercises & checklist

### [89. Zero-Overhead Application Telemetry](89-zero-overhead-application-telemetry.md)
- 89.1 Why it matters: the counter that has to leave a busy-poll process
- 89.2 Mental model
  - 89.2.1 Counter, gauge, high-water mark, histogram — and what each may cost
  - 89.2.2 Percentiles are not additive: merge histograms, never average percentiles
  - 89.2.3 Interval vs cumulative, windows and minimum samples
  - 89.2.4 Coordinated omission in production
- 89.3 Measure it: per-op cost of counter/histogram/seqlock publish; shared vs owned counters; scraper-induced RFO interference by cadence
- 89.4 Techniques
  - 89.4.1 The shared-memory telemetry segment
  - 89.4.2 What a trading process must expose
  - 89.4.3 The off-core scraper and export
  - 89.4.4 From metrics to SLOs, alerts and triggers
- 89.5 Verify the codegen: the instrumentation is a few stores
- 89.6 Pitfalls & anti-patterns: shared counters, averaged percentiles, service time, the scraper on the sibling
- 89.7 Exercises & checklist

### [90. Secure Programming for Low-Latency Systems](90-secure-programming-low-latency.md)
- 90.1 Why it matters: security as a hot-path concern
- 90.2 Mental model
  - 90.2.1 Market-data/order-entry messages as untrusted input
  - 90.2.2 Undefined behavior as a vulnerability class
  - 90.2.3 The hardening-vs-latency trade-off
- 90.3 Measure it: hot-path cost of hardening options
- 90.4 Techniques
  - 90.4.1 Bounds/length validation on variable-length fields and zero-copy parses
  - 90.4.2 Memory safety in zero-allocation/arena designs; bounds checks that compile away
  - 90.4.3 Integer/arithmetic safety in price/quantity math
  - 90.4.4 Stack protector, `_FORTIFY_SOURCE`, PIE/ASLR, CFI, CET shadow stacks
  - 90.4.5 Secret handling and secure wiping; keeping secrets out of logs
  - 90.4.6 Privilege reduction (`seccomp`, capabilities, namespaces)
  - 90.4.7 Build/supply-chain integrity; reproducible and signed builds
  - 90.4.8 Fuzzing message parsers (libFuzzer/AFL++); ASan/UBSan/MSan
- 90.5 Pitfalls & anti-patterns: trusting the wire; mitigation interactions
- 90.6 Exercises & checklist

### [91. Hot Reload & Live Reconfiguration](91-hot-reload-live-reconfiguration.md)
- 91.1 Why it matters: change strategy/config without dropping a tick
- 91.2 Mental model: atomic pointer swaps and double-buffering
- 91.3 Measure it: reload-induced tear/stall test
- 91.4 Techniques
  - 91.4.1 Seqlock-/RCU-published config snapshots read lock-free
  - 91.4.2 Versioned configuration and safe reclamation
  - 91.4.3 Shared-memory config segments; validation-before-swap
  - 91.4.4 Draining vs instantaneous cutover; dynamic-library/strategy reload with warm-up
- 91.5 Pitfalls & anti-patterns: torn config, stalls during cutover
- 91.6 Exercises & checklist

### [92. Process Topology & The Deterministic State Machine](92-process-topology-deterministic-state-machine.md)
- 92.1 Why it matters: contain a crash, keep the venue session up
- 92.2 Mental model
  - 92.2.1 Process-per-role vs monolith (feed handler / strategy / order gateway / risk)
  - 92.2.2 The Deterministic State Machine Pattern: pure, side-effect-free core decoupled from I/O for exact replayability
  - 92.2.3 Fault domains and blast radius
  - 92.2.4 Crash-only design
- 92.3 Measure it: failover/restart time and its tail
- 92.4 Techniques
  - 92.4.1 Shared-memory data planes between isolated processes
  - 92.4.2 Single-writer-per-segment discipline
  - 92.4.3 Supervisor/watchdog, heartbeats and liveness
  - 92.4.4 Kill-switch / safe-state on failure; fast restart with warm-up
  - 92.4.5 Symbol/venue sharding (core dumps → Ch. 96)
  - 92.4.6 Divergence forensics and determinism auditing
  - 92.4.7 Checkpointing the core: consistent snapshots without stalling the hot path
  - 92.4.8 Segment lifecycle: reattach, epochs and torn-write recovery
- 92.5 Pitfalls & anti-patterns: shared fate, cascading restarts
- 92.6 Exercises & checklist

### [93. The Pre-Trade Risk Gate & Kill-Switch Path](93-pre-trade-risk-gate-kill-switch.md)
- 93.1 Why it matters: the one stage you may not skip, budgeted at 40 ns
- 93.2 Mental model
  - 93.2.1 The check inventory: regulation → concrete per-order checks
  - 93.2.2 Data layout for a ~40 ns gate
  - 93.2.3 Checked arithmetic and branch-light evaluation
  - 93.2.4 The kill-switch path: triggers, propagation, safe state
- 93.3 Measure it: the gate's cost, and the time from trigger to cancels queued
- 93.4 Techniques
  - 93.4.1 The gate as a pure function over flat records, with limits published lock-free
  - 93.4.2 Self-match prevention and duplicate detection without a book lookup
  - 93.4.3 Throttles: TSC token buckets per venue port
  - 93.4.4 Changing limits under a running gate: validate, publish, verify
  - 93.4.5 Kill-switch triggers as runtime monitors
  - 93.4.6 The safe-state sequence, measured and rehearsed
- 93.5 Pitfalls & anti-patterns
- 93.6 Exercises & checklist

### [94. Idempotent Order Entry & Order-State Recovery](94-idempotent-order-entry-order-state-recovery.md)
- 94.1 Why it matters: a duplicated or orphaned order is a real position
- 94.2 Mental model
  - 94.2.1 Order identity: slot + generation, never a string
  - 94.2.2 The in-flight table and the per-order state machine
  - 94.2.3 The unknown-state matrix after a crash
  - 94.2.4 Session resynchronization and the two sources of truth
- 94.3 Measure it: correlation cost, and time-to-known-state after a kill
- 94.4 Techniques
  - 94.4.1 Allocating ids and sending: journal-before-send
  - 94.4.2 Correlating responses: acks, fills, rejects, cancels, replaces
  - 94.4.3 Position and exposure accumulators: one writer, from fills only
  - 94.4.4 Recovery after a kill: replay, resync, resolve
  - 94.4.5 Reconciliation against drop-copy and order status; position rebuild
  - 94.4.6 Type-state for the session and the order
- 94.5 Pitfalls & anti-patterns
- 94.6 Exercises & checklist

### [95. The Order-Entry Gateway & Session Layer](95-order-entry-gateway-session-layer.md)
- 95.1 Why it matters: the session is the only thing that can cancel your orders
- 95.2 Mental model
  - 95.2.1 Four protocols, one state machine
  - 95.2.2 Sequence numbers are state, and state must survive the process
  - 95.2.3 Liveness has layers, and each one is a clock
  - 95.2.4 The encode fast path and the throttle
- 95.3 Measure it: encode latency, resync time, and how long TCP takes to notice
- 95.4 Techniques
  - 95.4.1 The persisted session state: one page, written before the send
  - 95.4.2 The encode fast path: template, `to_chars`, one checksum pass
  - 95.4.3 The resend store and `ResendRequest` handling
  - 95.4.4 Liveness: heartbeats, `TestRequest`, and setting TCP's timers below the session's
  - 95.4.5 Throttles, batching and the order of operations on send
  - 95.4.6 Reconnect and takeover: who owns the numbers
- 95.5 Pitfalls & anti-patterns
- 95.6 Exercises & checklist

### [96. Crash Handling, Core Dumps & Post-Mortem Diagnostics](96-crash-handling-core-dumps-post-mortem.md)
- 96.1 Why it matters: the fault at 09:30:01 with four hundred working orders
- 96.2 Mental model
  - 96.2.1 From the faulting instruction to the core file
  - 96.2.2 Async-signal safety, and the faults you will see
  - 96.2.3 The order of business: safety, evidence, dump
  - 96.2.4 Why dumps stall the box, and what controls their size
- 96.3 Measure it: a signal's cost, a stack capture's cost, a dump versus its size
- 96.4 Techniques
  - 96.4.1 The async-signal-safe fault handler
  - 96.4.2 Pre-warming, and choosing the stack-capture mechanism
  - 96.4.3 The kill-switch handoff and the flight recorder
  - 96.4.4 Dumps that do not stall the box: `coredump_filter`, `MADV_DONTDUMP`, the pipe handler, systemd-coredump and minidumps
  - 96.4.5 Post-mortem: joining the core, the crash record, the trace record and the capture
- 96.5 Pitfalls & anti-patterns
- 96.6 Exercises & checklist

### [97. Capture, Persistence & Replay Storage](97-capture-persistence-replay-storage.md)
- 97.1 Why it matters: deterministic replay, research, and compliance
- 97.2 Mental model: append-only/WAL journals; time-indexed storage
- 97.3 Measure it: capture throughput and hot-path impact
- 97.4 Techniques
  - 97.4.1 Lossless line capture and nanosecond packet timestamps
  - 97.4.2 Compact binary log formats
  - 97.4.3 Getting bytes to disk off the hot path (async, io_uring, `O_DIRECT`, NVMe, batching)
  - 97.4.4 Sequencing and gap-free persistence; retention and compaction
  - 97.4.5 Feeding captures into replay, simulation and backtests
  - 97.4.6 Research to production: one decoder, two bindings (pybind11/nanobind, zero-copy NumPy views, parity tests, Arrow)
  - 97.4.7 Columnar tick storage: from the journal to Arrow and Parquet (kdb+/ClickHouse/QuestDB tier)
- 97.5 Pitfalls & anti-patterns: dropping under load; storage stalls bleeding into the hot path
- 97.6 Exercises & checklist

### [98. Line-Rate Capture, Timing-Faithful Replay & Passive Wire Measurement](98-line-rate-capture-timing-faithful-replay.md)
- 98.1 Why it matters: the open is the only realistic test, and you only get one a day
- 98.2 Mental model
  - 98.2.1 Capture-point fidelity: what each point loses
  - 98.2.2 The host capture stack: rings, blocks and fan-out
  - 98.2.3 Timestamps and formats
  - 98.2.4 Replay is a timing problem, not a copying problem
- 98.3 Measure it: the host capture stack under a burst, and the replayer's timing error
- 98.4 Techniques
  - 98.4.1 A `TPACKET_V3` capture ring
  - 98.4.2 Proving the capture lossless
  - 98.4.3 Timing-faithful replay onto the wire
  - 98.4.4 Validating replay fidelity with the tap
  - 98.4.5 Passive wire measurement: what to compute from the tap
  - 98.4.6 What this chapter takes from Ch. 97
- 98.5 Pitfalls & anti-patterns
- 98.6 Exercises & checklist

### [99. The Sequencer: Total-Order Broadcast & Replicated Deterministic Consumers](99-sequencer-total-order-replicated-consumers.md)
- 99.1 Why it matters: one truth, or the replay lies
- 99.2 Mental model
  - 99.2.1 The sequencer and its stream
  - 99.2.2 Replicated deterministic consumers and the digest
  - 99.2.3 Standby: hot, warm, cold — RPO, RTO and what each survives
  - 99.2.4 The sequencer as the single point of failure
- 99.3 Measure it: the hop the sequencer adds, the digest, and catch-up
- 99.4 Techniques
  - 99.4.1 Building the sequencer: the loop, the stamp, the log
  - 99.4.2 Fan-out: shm ring on-host, reliable multicast across hosts
  - 99.4.3 Replicas: the fold, the digest, the monitor
  - 99.4.4 The slow replica: gate, drop, or demote
  - 99.4.5 Sequencer failover and the standby sequencer
  - 99.4.6 Reducing what the other chapters carry
- 99.5 Pitfalls & anti-patterns
- 99.6 Exercises & checklist

### [100. Failover Without Split-Brain: Leases, Fencing & Session Takeover on the Order Path](100-failover-without-split-brain-leases-fencing.md)
- 100.1 Why it matters: two emitters under one session is worse than the outage
- 100.2 Mental model
  - 100.2.1 The invariant and where it is enforced
  - 100.2.2 Failure detection over two paths
  - 100.2.3 Leases, epochs and fencing tokens
  - 100.2.4 The venue's safety nets
  - 100.2.5 The takeover timeline
- 100.3 Measure it: detect → lease → logon → first accepted order, and a zombie fenced
- 100.4 Techniques
  - 100.4.1 The lease word and the fencing token
  - 100.4.2 The two-path detector
  - 100.4.3 The takeover sequence: cancel first, then trade
  - 100.4.4 Zombies: bounded pauses and stepping down
  - 100.4.5 Where the token is checked when the venue cannot
  - 100.4.6 Off-the-shelf: consensus beside the path
- 100.5 Pitfalls & anti-patterns
- 100.6 Exercises & checklist

### [101. Latency-Spike Forensics & Anomaly-Triggered Diagnostics](101-latency-spike-forensics-anomaly-triggered-diagnostics.md)
- 101.1 Why it matters: the book can say it happened; this chapter says why
- 101.2 Mental model
  - 101.2.1 From one outlier to a mechanism: the forensic loop
  - 101.2.2 One timeline across clocks
  - 101.2.3 Suspects and fingerprints
  - 101.2.4 Always-on, frozen on trigger
- 101.3 Measure it: the always-on record, the freeze, and the clock alignment
- 101.4 Techniques
  - 101.4.1 Reconstructing the timeline
  - 101.4.2 The flight recorder: always-on, frozen by a swap
  - 101.4.3 Kernel and hardware capture on the same trigger: ftrace, `perf record` in overwrite mode, Intel PT snapshots
  - 101.4.4 Testing the suspects: the decision table in practice
  - 101.4.5 From spike to incident: the bundle and the reproduction
- 101.5 Pitfalls & anti-patterns
- 101.6 Exercises & checklist

### [102. Host Health Telemetry: Proving the Quiet Box Stayed Quiet](102-host-health-telemetry.md)
- 102.1 Why it matters: the quiet box decays
- 102.2 Mental model: isolation as a live invariant
  - 102.2.1 Invariants, not thresholds (the signal table: what healthy reads, what a delta means, where to read it)
  - 102.2.2 The signal classes and the chapter that owns each (isolation, scheduler, memory system, frequency/thermal/power, firmware, clock, hardware RAS, provenance)
  - 102.2.3 Drift: how a tuned box untunes itself
  - 102.2.4 The sampler's own footprint (MSRs, perf events and cpufreq reads that IPI or tick the hot core)
- 102.3 Measure it: the sampler's footprint (right way vs wrong way) and the drift fingerprint table
- 102.4 Techniques
  - 102.4.1 Isolation integrity: the per-second diff (`hostwatch.cpp`)
  - 102.4.2 Frequency, thermal, power and SMIs — read from the right core
  - 102.4.3 Memory system, clock and hardware health (THP/compaction/NUMA, PSI, clocksource and kmsg, EDAC/MCE/AER)
  - 102.4.4 Baseline, drift audit, and wiring into the pipeline
- 102.5 Pitfalls & anti-patterns: the monitor is the noise; thresholds on invariants; unmonitored monitors; stale baselines
- 102.6 Exercises & checklist

### [103. Network Health Telemetry & Drop Forensics](103-network-health-telemetry-drop-forensics.md)
- 103.1 Why it matters: a silent drop at the open is a wrong book and a lost race
- 103.2 Mental model: the counter hierarchy, by the layer that dropped
  - 103.2.1 The receive path and its counters (PHY/MAC, NIC port buffer vs host ring, NAPI, IP, UDP, TCP, bypass stacks, application)
  - 103.2.2 Driver dialects: the same drop, five names (`ixgbe`, `i40e`/`ice`, `mlx5`, `sfc`, the generic fields)
  - 103.2.3 Delay is not loss, and loss is not always a counter (`time_squeeze`, pause frames, the bypass blind spot, the drop with no counter)
- 103.3 Measure it: a market-open replay saturating each layer in turn; the cost and footprint of every poller
- 103.4 Techniques
  - 103.4.1 The sampler: every counter, by layer, from a housekeeping core (`netwatch.cpp`, `TCP_INFO`)
  - 103.4.2 Drop forensics: naming the function that freed the packet (`dropwatch`, `skb:kfree_skb` drop reasons, `perf`, `bpftrace`)
  - 103.4.3 Fabric and membership signals the host can see (querier age, A/B skew, pause frames)
  - 103.4.4 Link, PHY, optics and switch telemetry (FEC/CRC, DOM, flaps, switch-side counters)
  - 103.4.5 When the kernel is not in the path: bypass-stack counters (DPDK, Onload, ef_vi)
- 103.5 Pitfalls & anti-patterns: counters without layers, summed duplicates, `time_squeeze` ignored, zero on bypass, fast polling, arming probes mid-session
- 103.6 Exercises & checklist

### [104. Testing Latency-Critical Code: Unit, Property-Based & Deterministic Simulation Testing](104-testing-latency-critical-code.md)
- 104.1 Why it matters: the bug that loses money is a logic bug in a correct-looking program
- 104.2 Mental model
  - 104.2.1 The test pyramid for a deterministic core
  - 104.2.2 Properties, not expected values
  - 104.2.3 Deterministic simulation testing
  - 104.2.4 Test doubles that cost nothing
- 104.3 Measure it: a test suite for the core, a bug found by seed, and the price of a seam
- 104.4 Techniques
  - 104.4.1 Table-driven tests of `apply(state, input)`
  - 104.4.2 Property-based tests, invariants and shrinking
  - 104.4.3 Deterministic simulation of the gateway against a fault-injecting venue
  - 104.4.4 Golden-master tests cut from captures
  - 104.4.5 Test doubles as policies: production and simulation from one template
  - 104.4.6 Running the suite: sanitizers, ARM, and what belongs where
- 104.5 Pitfalls & anti-patterns
- 104.6 Exercises & checklist

### [105. Performance Engineering in CI & the Profile Lifecycle](105-performance-engineering-ci-profile-lifecycle.md)
- 105.1 Why it matters: a gate that flaps is a gate that gets turned off
- 105.2 Mental model
  - 105.2.1 A benchmark is a test with a noisy oracle
  - 105.2.2 Which statistic to gate, on which machine
  - 105.2.3 The pipeline: fast gate, nightly gate, bisection
  - 105.2.4 Profiles decay like benchmarks
- 105.3 Measure it: the noise floor, the gate, and the stale profile
- 105.4 Techniques
  - 105.4.1 A/A calibration: measure the floor, derive the tolerance
  - 105.4.2 The gate: bootstrap CI, rank test, minimum effect
  - 105.4.3 Deterministic proxies for shared runners
  - 105.4.4 The nightly replay gate and bisection by replay
  - 105.4.5 Operating PGO/AutoFDO/BOLT: the profile lifecycle
  - 105.4.6 Trend, alerting and the human loop
- 105.5 Pitfalls & anti-patterns
- 105.6 Exercises & checklist

### [106. Release Engineering & Measuring Changes in Production](106-release-engineering-measuring-changes-in-production.md)
- 106.1 Why it matters: the deploy is a latency event you schedule
- 106.2 Mental model
  - 106.2.1 The deploy calendar: what may change when
  - 106.2.2 Compatibility windows: attaching a new version to live state
  - 106.2.3 Trust in stages: shadow → canary → fleet
  - 106.2.4 Why the CI gate is necessary and not sufficient
- 106.3 Measure it: a blue/green cutover tick by tick, and paired comparison with its own A/A
- 106.4 Techniques
  - 106.4.1 The release manifest: what "a release" is
  - 106.4.2 Compatibility windows in code
  - 106.4.3 Blue/green cutover for process-per-role
  - 106.4.4 Shadow runs and canary shards
  - 106.4.5 Paired production comparison, with its A/A
  - 106.4.6 Venue conformance and the weekend test
- 106.5 Pitfalls & anti-patterns
- 106.6 Exercises & checklist

### [107. Production Profiling & End-to-End Case Study](107-production-profiling-end-to-end-case-study.md)
- 107.1 Why it matters: tying the whole book together
- 107.2 Mental model: three tiers — always-on, targeted-live, offline-on-replay
- 107.3 Measure it: a full tick-to-trade walkthrough
- 107.4 Techniques
  - 107.4.1 Regression detection and latency-regression gates in CI
  - 107.4.2 Deterministic replay of captured market data
  - 107.4.3 Simulation harnesses
  - 107.4.4 Profiling the live system safely
  - 107.4.5 Closing the loop: from the latency ledger to fills, markouts and race outcomes
- 107.5 Pitfalls & anti-patterns: drift between test and production
- 107.6 Exercises & checklist

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
- B.1 The findings in one table (Rust/C++ parity, where it breaks, asm vs intrinsic, the call ladder, what a GC and a JIT do, `constexpr`/`const fn`/`comptime`, where Python belongs — each row pointing at its Part XII measurement)
- B.2 The tier table in brief (hot / hot-inherited / warm / cold / below software, with conditions)
- B.3 What it is called elsewhere (the Part III–VI constructs in Rust, Zig and Java/.NET, with the chapter that measures each)

### [Appendix C — System Tuning Checklist](appendix-C-system-tuning-checklist.md)
- C.1 BIOS/firmware (C-states, P-states/turbo, SMT, NUMA/snoop mode)
- C.2 Kernel cmdline (`isolcpus`, `nohz_full`, `rcu_nocbs`, `intel_pstate`, `mitigations=`, huge pages)
- C.3 Runtime knobs (governor, IRQ affinity/`irqbalance`, `tuned`, RPS/XPS, NIC ring/coalescing, `sysctl`, THP, `mlockall`)
- C.4 Per-process (affinity/`taskset`, RT priority, `numactl`)
- C.5 Verification pass (`cyclictest`, jitter measurement)
- C.6 Configuration as code and drift detection

### [Appendix D — Compiler Flag Reference](appendix-D-compiler-flag-reference.md)
- D.1 Optimization levels (`-O2`/`-O3`/`-Ofast` caveats)
- D.2 Arch/tuning (`-march`/`-mtune`/`-mcpu`)
- D.3 FP and math (`-ffast-math` hazards, `-fno-math-errno`, FTZ/DAZ)
- D.4 Codegen (`-fno-exceptions`/`-fno-rtti`, `-fno-plt`, `-fno-semantic-interposition`, `-fno-omit-frame-pointer`)
- D.5 Inlining/layout (`-finline-limit`, `-freorder-blocks-and-partition`, `-falign-*`)
- D.6 LTO/PGO/BOLT
- D.7 Diagnostics (`-Wpadded`, `-Wfloat-equal`, `-fopt-info`) and sanitizers
- D.8 Hardening and speculative-execution mitigation flags (CET, stack protector, `_FORTIFY_SOURCE`, PIE, retpolines)
- D.9 A representative hot-path build line

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

### [Appendix H — Modern C++ (C++20/23/26) Feature Availability Matrix](appendix-H-cpp-feature-matrix.md)
- H.1 How to read the matrix (columns, library-vs-language, feature-test gating)
- H.2 Core library features (`std::expected`, `std::span`, `std::mdspan`, `std::flat_map`/`flat_set`, `std::print`)
- H.3 Language features (`consteval`/`constinit`, concepts, coroutines, `[[likely]]`, `[[assume]]`)
- H.4 Concurrency & execution (`std::atomic_ref`, `std::jthread`, `std::execution` senders/receivers)
- H.5 Numerics & bit manipulation (`std::bit_cast`, `<bit>`, `std::byteswap`, saturating arithmetic, `from_chars`/`to_chars`)
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
- I.10 Crash, core-dump, live-introspection & incident one-liners (`coredumpctl`, `eu-stack`, gdb non-stop, `perf record --snapshot`, Intel PT, the ftrace freeze, the bundle)
- I.11 Other toolchains: asm, runtime logs and the C-ABI waist (rustc/cargo asm, zig, `-Xlog:gc*,safepoint`/`PrintCompilation`/hsdis, `DOTNET_JitDisasm`, `gctrace`/`-gcflags`, `ocamlopt -S`, `nm -D`/`abidiff`)
- I.12 Build, toolchain, attributes, statics & TLS

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
- K.2 The wire: fiber vs. copper, and latency-equalized cabling
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
- L.5 Benchmarking across languages (the Ch. 86 protocol: same box/core/capture, digest parity, warm vs cold windows, each toolchain's sink and asm)
- L.6 Benchmarks as tests: the CI skeleton (Ch. 105) — CTest labels, `--benchmark_out=json`, `compare.py`, the A/A table and the artefact layout keyed by revision and runner class
- L.7 The core-to-core map: placement input for every pinned pair (Ch. 18) — run `c2c` once per box and firmware mode, store the matrix, quote the pair's one-way latency with every two-core result

### [Appendix M — Pre-Open Readiness Gate & Market-Open Playbook](appendix-M-readiness-gate-market-open.md)
- M.1 The readiness state machine (cold → warming → syncing → reconciled → armed → trading → disarmed; one predicted branch on the order path)
- M.2 T-60: host attestation (provenance, isolation, memory/clock/frequency, RDT, NIC/PCIe, hardware health — each a command with an expected value)
- M.3 T-30: clock, feed and network gates (PTP servo/offset, membership and querier, both lines live, snapshot rebuilt, zero drops, samplers quiet)
- M.4 T-5: session, positions, warm-up and the kill-switch dry-run
- M.5 T-0 and intraday: arm, re-verify, re-arm after a restart, disarm
- M.6 The host fingerprint micro-suite (Appendix L harness vs Appendix E and the host's history)
- M.7 Configuration as code and drift detection (summary; Appendix C.6)
- M.8 The gate runner (skeleton)

### [Appendix N — Incident Diagnostics Bundle](appendix-N-incident-diagnostics-bundle.md)
- N.1 What a bundle is for: alignable, attributable, checkable (and bounded)
- N.2 Provenance: build IDs, recorded compiler flags, load bases, configuration hash, kernel/firmware/microcode, host fingerprint, NIC settings
- N.3 Clocks and anchors: TSC↔MONOTONIC sandwiches, PHC cross-timestamps, PTP journal and steps, the clock each ring used
- N.4 The evidence, per layer: application (trace record, frozen flight recorder, binary log, telemetry snapshot, crash record, venue-stamp series), kernel ring (ftrace, `perf.data`, Intel PT, journal), host (Ch. 102 samples, `/proc`, RAS), network (Ch. 103 samples, counters, membership, bypass stacks), capture and tap slices
- N.5 The collection script (skeleton): bounded by `taskset`/`nice`/`ionice`/`timeout`, per-group logs, manifest and checksums
- N.6 The ten-minute first look (cheap exonerations first)
- N.7 Retention, pattern analysis and what to scrub

### [Appendix O — Hot-Path Anti-Pattern Catalogue](appendix-O-hot-path-anti-pattern-catalogue.md)
- O.1 Allocation, page faults and memory lifetime
- O.2 Dispatch, type erasure and abstraction cost
- O.3 Control flow, data dependencies and memory ordering
- O.4 Sharing, contention and false sharing
- O.5 Queues, pipelines, flow control and timers
- O.6 Kernel, scheduling, syscalls and isolation
- O.7 I/O, networking, logging and capture
- O.8 Layout, caches and the memory hierarchy
- O.9 Numerics, bits and SIMD
- O.10 Build, flags, codegen and toolchain
- O.11 Measurement and observability mistakes
- O.12 Operational and architectural
- O.13 The ten that pay for the appendix

### [Appendix P — Attributes, Builtins & the UB Catalogue](appendix-P-attributes-builtins-ub-catalogue.md)
- P.1 How to read this appendix
- P.2 Attribute and builtin reference: what each tells the optimizer, and how to verify it
- P.3 The UB catalogue for performance programmers
- P.4 Verifying a hint, and finding a UB-licensed transformation you did not want
