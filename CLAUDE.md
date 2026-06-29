# CLAUDE.md

## Project: Low-Latency C++ on Linux — A Tutorial Series

A set of tutorials (book-length) teaching high-performance, low-latency software
development techniques using **C++ on Linux**. Each chapter is a standalone,
hands-on tutorial that builds toward a coherent whole.

### Audience & framing
- **Primary audience:** Intermediate to advanced C++ developers. Assume solid C++
  (C++17/20) and Linux fundamentals; spend the page budget on performance depth,
  not on teaching the basics.
- **Domain framing:** **HFT / electronic trading.** Examples, case studies, and
  latency budgets should draw on order books, market-data feeds, and
  tick-to-trade paths. Keep core techniques transferable, but motivate them with
  trading scenarios.
- **North-star metric:** tail latency (p99/p99.9) and tick-to-trade, not average
  throughput. Always reason about the *distribution*, not the mean.

### Environment assumptions (state explicitly when a chapter depends on them)
- Modern x86-64 (Intel/AMD), Linux (recent mainline kernel), GCC and/or Clang.
- C++20 by default; call out where C++17 differs or C++23 or newer helps.
- Tools referenced across chapters: `perf`, Linux `perf_events`/PMU, Intel VTune,
  Google Benchmark, Compiler Explorer (Godbolt), TSan/ASan/UBSan, `numactl`,
  `taskset`, `cyclictest`, io_uring (`liburing`), DPDK.

## Authoring conventions

Each chapter SHOULD follow this structure:
1. **Why it matters (HFT motivation)** — the latency problem this solves.
2. **Mental model** — the hardware/OS/language mechanism, with a diagram where useful.
3. **Measure it** — show the cost/effect with a reproducible benchmark or `perf` run.
4. **Techniques** — concrete, idiomatic C++ patterns. Show before/after.
5. **Verify the codegen** — Godbolt/asm where the optimizer matters.
6. **Pitfalls & anti-patterns** — including correctness traps.
7. **Exercises / checklist** — practical takeaways.
8. **References** - any source material that provides background on the topic.
9. **Additional Reading** - other resources, websites, books that contain more information on the topic.

Code & content guidelines:
- Every performance claim is backed by a **measurement** (benchmark numbers,
  `perf` counters, or asm). No "this is faster" without evidence.
- Prefer **minimal, compilable** examples with the exact compiler flags used.
- Always note the **compiler, flags, CPU, and kernel** for any benchmark.
- Show the **trade-offs** (complexity, portability, correctness risk), not just the win.
- Distinguish **steady-state hot path** (zero-allocation, no syscalls) from setup/teardown.

## Table of Contents

### Part I — Foundations & Methodology
1. **The Latency Mindset** — latency vs throughput, tail latency, jitter, the
   tick-to-trade budget; why means lie.
2. **Measure First: Profiling & Hardware Performance Counters** — `perf`, the PMU,
   VTune, top-down microarchitecture analysis, flame graphs, IPC/cache/branch counters.
3. **Micro-benchmarking Done Right** — Google Benchmark, common pitfalls (dead-code
   elimination, warm-up, frequency scaling), statistical rigor; tail-latency
   measurement, HdrHistogram, and avoiding coordinated omission.
4. **Reading the Machine: Assembly & Compiler Output** — Godbolt workflow, reading
   x86-64 asm, verifying the optimizer did what you expect.
5. **System Setup for Low Latency** — BIOS tuning, C-states, frequency scaling/turbo,
   hyperthreading, topology discovery, building a quiet machine; speculative-execution
   mitigations (Spectre/Meltdown/retpoline) and their latency cost on isolated boxes.

### Part II — CPU Microarchitecture
6. **The Memory Hierarchy & Caches** — latencies by level, cache lines, associativity,
   coherence (MESI), prefetching.
7. **Cache-Aware & Data-Oriented Design** — AoS vs SoA, hot/cold splitting,
   access-pattern–driven layout.
8. **Software Prefetching & Non-Temporal Stores** — `__builtin_prefetch`/`_mm_prefetch`,
   prefetch distance & locality hints, streaming (non-temporal) stores, avoiding cache
   pollution, when manual prefetch beats the hardware prefetcher (and when it hurts).
9. **CPU Pipelines & Execution** — superscalar/out-of-order execution, hazards,
   dependency chains, instruction-level parallelism.
10. **Branch Prediction & Branchless Programming** — predictor behavior, misprediction
    cost, conditional moves, lookup tables, removing branches from the hot path.
11. **Virtual Memory, the TLB & Huge Pages** — address translation cost, TLB misses,
    transparent vs explicit huge pages.
12. **NUMA Architecture** — node locality, allocation policy, `numactl`, cross-socket
    penalties, NUMA-aware data structures.
13. **Timekeeping: TSC, rdtsc & Clock Sources** — invariant TSC, `rdtscp`, calibration,
    measuring nanoseconds reliably, on-host hardware timestamping.

### Part III — Compile-Time & Language Mechanics
14. **Compile-Time Mechanics** — `constexpr`/`consteval`, moving work to compile time,
    the cost model of templates.
15. **Template Metaprogramming & Zero-Cost Abstractions** — CRTP, type-based dispatch,
    expression templates, policy-based design.
16. **The Cost of Abstractions** — exception unwinding cost, `noexcept`, RTTI, error
    codes vs exceptions vs `std::expected`, hot-path error handling.
17. **Aliasing, `restrict` & Type Punning** — strict-aliasing rules, `__restrict__`,
    `std::bit_cast`/`std::launder`, type-punning safely, how aliasing assumptions gate
    vectorization and reordering; reading the codegen difference.
18. **Build Toolchain for Speed** — optimization flags, `-march`/`-mtune`, inlining
    control, LTO, PGO, BOLT, linker effects.

### Part IV — Memory Management
19. **Memory Management Fundamentals** — stack vs heap, allocation cost, page faults,
    why `malloc` is dangerous on the hot path.
20. **Custom Allocators** — arena/pool/slab allocators, object pools, `std::pmr`,
    steady-state zero-allocation designs.
21. **Hot-Path-Hostile STL & Cache-Friendly Containers** — costs of `std::unordered_map`,
    `std::map`, `std::function`, `shared_ptr` refcounting and node-based containers;
    flat/open-addressing & robin-hood hashing, intrusive containers, small-buffer types,
    choosing layout for the access pattern.
22. **Memory Mapping** — `mmap`, shared memory, file-backed mappings, page faulting
    and pre-faulting/locking (`mlock`).

### Part V — Numerics & Data Parallelism
23. **Fixed-Point & Floating-Point Arithmetic** — representing prices safely: scaled
    integers vs decimal vs binary float, rounding and tick-size handling; floating-point
    hazards — denormals and the FTZ/DAZ flags, FP op latency/throughput, NaN/Inf traps,
    and cross-build determinism on the hot path.
24. **SIMD & Vectorization** — auto-vectorization, intrinsics, SSE/AVX/AVX-512,
    data layout for vectorization, when SIMD pays off (and downclocking caveats).

### Part VI — Concurrency
25. **The C++ Memory Model & Atomics** — `std::atomic`, `memory_order`,
    acquire/release/seq_cst, fences, compiler and hardware reordering.
26. **Multithreading & Concurrency Foundations** — threads, contention, scalability
    pitfalls, designing for shared-nothing.
27. **Spinlocks, Backoff & Contention Control** — spin vs block, test-and-test-and-set,
    exponential backoff, `pause`/`tpause`, MCS/CLH queue locks, futex internals, and when
    a well-tuned spinlock beats lock-free complexity.
28. **False Sharing & Thread-Safety Anomalies** — cache-line ping-pong, alignment/padding,
    `hardware_destructive_interference_size`, detection.
29. **Lock-Free Data Structures** — SPSC/MPMC queues, ring buffers, CAS loops, ABA,
    progress guarantees.
30. **Seqlocks & Single-Writer Publication** — versioned/sequence-lock snapshots for
    publishing market-data and reference state to many readers without locks; correctness
    under the memory model, torn-read avoidance, single-writer multiple-reader patterns.
31. **Safe Memory Reclamation** — RCU, hazard pointers, epoch-based reclamation for
    lock-free structures.
32. **The Disruptor Pattern** — ring buffer + sequence barriers, batching, mechanical
    sympathy, building a low-latency pipeline.
33. **Coroutines & Async Models** — C++20 coroutines, stackful vs stackless, event
    loops, async I/O without thread-per-connection.
34. **Concurrency Correctness Tooling** — TSan/ASan/UBSan, stress testing, fuzzing,
    model checking lock-free code.

### Part VII — OS, Scheduling & Isolation
35. **Context Switching & Its Mitigation** — direct/indirect costs, syscall overhead,
    keeping threads on-core, busy-poll vs block.
36. **Thread & Interrupt Pinning** — CPU affinity (`taskset`/`sched_setaffinity`),
    IRQ affinity, isolating the hot core.
37. **Real-Time Scheduling & Kernel Tuning** — `isolcpus`, `nohz_full`, RT priorities,
    cgroups, eliminating jitter, measuring with `cyclictest`.
38. **Keeping the Hot Path Warm** — cache/TLB/branch-predictor warming, shadow/dummy
    traffic, pre-touching pages and connections, avoiding cold-start stalls on the first
    real message; measuring warm vs cold tail latency.

### Part VIII — I/O & Networking
39. **Linux Native I/O** — blocking vs non-blocking, `epoll`, readiness vs completion,
    syscall batching.
40. **io_uring Deep Dive** — submission/completion queues, registered buffers/files,
    polling mode, batching for throughput and latency.
41. **Inter-Process Communication** — shared-memory queues, lock-free IPC, pipes vs
    shm, cross-process ring buffers.
42. **Socket Optimization & TCP/Protocol Tuning** — `TCP_NODELAY`/Nagle, buffer sizing,
    congestion control, multicast for market data.
43. **Zero-Copy Wire Handling & Market-Data Decoding** — parsing off the wire without
    copies: ITCH/OUCH/FIX/SBE/FAST, fixed-layout structs vs bit-fields, endianness,
    `std::from_chars`/`to_chars`, branch-free integer/decimal parsing, scatter/gather
    and zero-copy receive paths; feed-handler A/B line arbitration, sequence-gap
    detection and recovery on redundant multicast feeds.
44. **NIC Features & Offloads** — RSS, hardware timestamping, busy-polling,
    checksum/segmentation offload, Solarflare/Onload.
45. **Clock Synchronization & Hardware Timestamping (PTP)** — distributing time across
    hosts with PTP, NIC hardware timestamps, comparing timestamps across machines,
    measuring true wire-to-wire and tick-to-trade latency (builds on Ch. 13 & 44).
46. **Kernel Bypass & Userspace Networking** — DPDK, poll-mode drivers, hugepage
    mempools, userspace TCP stacks, the tick-to-trade fast path.

### Part IX — Heterogeneous Computing & Hardware Acceleration
47. **GPU Computing with CUDA** — offloading data-parallel work to the GPU: the CUDA
    execution and memory model, warps/occupancy, host–device transfer cost, pinned and
    unified memory, streams and overlap; where GPUs pay off in trading (risk, options
    pricing/Greeks, Monte-Carlo, backtesting, ML inference) and why PCIe round-trip
    latency keeps them off the tick-to-trade hot path; measuring end-to-end including
    transfer overhead, not just kernel time.
48. **Distributed Computing with MPI** — scaling across cores and hosts with OpenMPI:
    point-to-point vs collective operations, communicators, non-blocking calls and
    compute/communication overlap, RDMA/InfiniBand transports, NUMA- and topology-aware
    rank placement (builds on Ch. 12); large-scale backtesting and Monte-Carlo risk
    across a cluster, and why message latency keeps MPI off the order path.
49. **FPGA Acceleration** — programmable hardware on the tick-to-trade fast path: HLS
    vs RTL, the host–FPGA boundary, NIC-integrated FPGAs and inline feed handling/order
    entry, deterministic nanosecond pipelines, partial reconfiguration; deciding what
    belongs in fabric vs software, and measuring wire-to-wire latency of a hardware path
    (builds on Ch. 43–46).

### Part X — Observability in Production
50. **Zero-Overhead Logging** — async/lock-free loggers, deferred formatting, binary
    logging, keeping logging off the hot path.
51. **Production Profiling & End-to-End Case Study** — continuous performance
    monitoring, regression detection, and a full tick-to-trade walkthrough tying the
    book together; determinism and capture/replay — deterministic replay of captured
    market data, simulation harnesses, and latency-regression gates in CI.

## Status / Workflow
- Phase 1 (done): TOC established above (51 chapters).
- Phase 2 (current): generating chapters one at a time into `chapters/NN-slug.md`,
  following the authoring conventions. Drafted: **Ch. 1–14 and Ch. 28** (Part I and Part II —
  CPU Microarchitecture — complete; Part III — Compile-Time & Language Mechanics — begun;
  Ch. 28 — False Sharing — drafted ahead of sequence on request). Sequential drafting resumes at
  **Ch. 15 — Template Metaprogramming & Zero-Cost Abstractions**.
- When generating a chapter, confirm scope against this TOC and keep cross-references
  to other chapters by number/title.
- Benchmark output blocks in drafts are **representative** for the stated reference
  machine (authoring is on macOS; target is Linux x86-64); code/flags/`perf` commands are
  reproducible. Real measured runs must be backfilled before a chapter is final.
- Reference machine: Ch. 6–11 use a single-socket Xeon Gold 6326 (Ice Lake-SP); Ch. 12
  (NUMA) uses a **dual-socket 2× Xeon Gold 6326** because NUMA effects need two sockets.
  Keep the per-chapter machine description consistent with these unless a chapter needs otherwise.

### README.md is the reader's topic index (keep it current with every chapter)

`chapters/README.md` is a **table of contents and topic index**, not a progress board. For each
chapter it lists the title, the file link, a one-line focus, and a **Covers:** list of the
sub-topics inside that chapter, so a reader can find which document discusses a given technique.
Planned chapters are marked *(not yet written)* with a **Will cover:** list of intended topics.
Drafting progress is tracked **here in CLAUDE.md** (the "Drafted: …" line above), NOT in README.

After generating (or materially revising) any chapter `chapters/NN-slug.md`, always, in the same
change:

1. **Refresh that chapter's README entry:**
   - Remove the *(not yet written)* marker and relabel its **Will cover:** line to **Covers:**.
   - Replace the planned sub-topic list with one that reflects the chapter's *actual* sections —
     distil the `### N.2.x` (Mental model) and `### N.4.x` (Techniques) subsection titles into a
     deduplicated, `·`-separated list of the concepts/techniques a reader would search for.
   - Verify the title, file link, and one-line focus match the chapter.
2. **Update this file (CLAUDE.md):** advance the Phase 2 "Drafted: …" line (range drafted, current
   Part, next chapter).
3. **Consistency sweep of the new chapter:** confirm it opens with the Part header +
   Prerequisites/Leads-into blockquote, follows the §1–§9 section structure, and ends with a
   `*Next: Ch. NN — …*` teaser pointing at the next planned chapter. Also catch up any earlier
   chapters whose README entry still says *(not yet written)* but which now exist on disk.

Treat this as part of "generating a chapter," not an optional follow-up — a chapter is not done
until its README entry reflects its real contents and CLAUDE.md's progress line is current.
