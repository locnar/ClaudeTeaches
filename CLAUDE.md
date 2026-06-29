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
- Modern x86-64 (Intel/AMD), Linux (recent mainline kernel), GCC and/or Clang. AArch64 /
  AWS Graviton is treated in **Appendix A**, not assumed in the main chapters.
- C++20 by default; call out where C++17 differs or C++23/26 helps. Reach for modern
  niceties when they sharpen an example — e.g. `std::flat_map`/`std::flat_set`,
  `std::print`/`std::println` (`<print>`), `std::expected`, `std::mdspan`, `std::span`.
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
- **Use modern C++ where it helps, and name the standard + toolchain support.** Prefer
  C++23/26 niceties when they improve clarity or performance — `std::flat_map`/`std::flat_set`
  (cache-friendly ordered containers), `std::print`/`std::println` (`<print>`), `std::expected`
  (hot-path error handling), `std::mdspan`, `std::span`, `std::bit_cast`. Show the C++17/20
  fallback where the target toolchain lacks the feature, and note compiler/library availability.

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
8. **Object Layout, Alignment & Padding** — the C++/ABI object model: member ordering and
   struct padding, `alignof`/`alignas`, natural vs over-alignment (SIMD and cache-line),
   `[[no_unique_address]]`, empty-base optimization, bit-fields and their codegen, packing
   trade-offs (`#pragma pack`) vs misaligned-access cost; sizing structs to a cache line and
   inspecting layout with `pahole`/`-Wpadded` (builds on Ch. 6–7).
9. **Software Prefetching & Non-Temporal Stores** — `__builtin_prefetch`/`_mm_prefetch`,
   prefetch distance & locality hints, streaming (non-temporal) stores, avoiding cache
   pollution, when manual prefetch beats the hardware prefetcher (and when it hurts).
10. **CPU Pipelines & Execution** — superscalar/out-of-order execution, hazards,
    dependency chains, instruction-level parallelism.
11. **Instruction Cache, Front-End Stalls & Code Layout** — the front-end: L1i, the µop
    cache (DSB), I-TLB, fetch/decode bandwidth; front-end-bound stalls in top-down analysis,
    code bloat from over-inlining, hot/cold code splitting, `[[likely]]`/`[[unlikely]]` and
    `__builtin_expect`, function/section placement, `-freorder-blocks-and-partition`, linker
    ordering and PGO/BOLT for I-cache locality (builds on Ch. 10; ties Ch. 21).
12. **Branch Prediction & Branchless Programming** — predictor behavior, misprediction
    cost, conditional moves, lookup tables, removing branches from the hot path.
13. **Indirect Calls, Virtual Dispatch & Devirtualization** — the cost of `virtual` calls
    and function pointers: vtables, indirect-branch prediction (BTB) and mispredict cost,
    `std::function` overhead; speculative and profile-guided devirtualization, `final`, CRTP
    and static polymorphism, `std::variant`/visitor and type-erasure alternatives, jump
    tables vs branch chains; removing indirect calls from the hot path (builds on Ch. 12;
    ties Ch. 18–19).
14. **Virtual Memory, the TLB & Huge Pages** — address translation cost, TLB misses,
    transparent vs explicit huge pages.
15. **NUMA Architecture** — node locality, allocation policy, `numactl`, cross-socket
    penalties, NUMA-aware data structures.
16. **Timekeeping: TSC, rdtsc & Clock Sources** — invariant TSC, `rdtscp`, calibration,
    measuring nanoseconds reliably, on-host hardware timestamping.

### Part III — Compile-Time & Language Mechanics
17. **Compile-Time Mechanics** — `constexpr`/`consteval`, moving work to compile time,
    the cost model of templates.
18. **Template Metaprogramming & Zero-Cost Abstractions** — CRTP, type-based dispatch,
    expression templates, policy-based design.
19. **The Cost of Abstractions** — exception unwinding cost, `noexcept`, RTTI, error
    codes vs exceptions vs `std::expected`, hot-path error handling.
20. **Aliasing, `restrict` & Type Punning** — strict-aliasing rules, `__restrict__`,
    `std::bit_cast`/`std::launder`, type-punning safely, how aliasing assumptions gate
    vectorization and reordering; reading the codegen difference.
21. **Build Toolchain for Speed** — optimization flags, `-march`/`-mtune`, inlining
    control, LTO, PGO, BOLT, linker effects.

### Part IV — Memory Management
22. **Memory Management Fundamentals** — stack vs heap, allocation cost, page faults,
    why `malloc` is dangerous on the hot path.
23. **Custom Allocators** — arena/pool/slab allocators, object pools, `std::pmr`,
    steady-state zero-allocation designs.
24. **Hot-Path-Hostile STL & Cache-Friendly Containers** — costs of `std::unordered_map`,
    `std::map`, `std::function`, `shared_ptr` refcounting and node-based containers;
    flat/open-addressing & robin-hood hashing, `std::flat_map`/`std::flat_set` (C++23),
    intrusive containers, small-buffer types, choosing layout for the access pattern.
25. **Memory Mapping** — `mmap`, shared memory, file-backed mappings, page faulting
    and pre-faulting/locking (`mlock`).

### Part V — Numerics & Data Parallelism
26. **Fixed-Point & Floating-Point Arithmetic** — representing prices safely: scaled
    integers vs decimal vs binary float, rounding and tick-size handling; floating-point
    hazards — denormals and the FTZ/DAZ flags, FP op latency/throughput, NaN/Inf traps,
    and cross-build determinism on the hot path.
27. **Bit Manipulation & Integer Tricks** — population count, leading/trailing-zero scan,
    bit set/clear/extract via BMI/BMI2 (`popcnt`, `lzcnt`/`tzcnt`, `pdep`/`pext`, `blsr`),
    bitsets and bitmaps for order-book level occupancy and free-list management, branchless
    min/max/abs/sign, power-of-two rounding and fast modulo via masks, packing/unpacking
    fields, Morton/Z-order interleaving; reading the intrinsics' codegen and op
    latency/throughput (ties Ch. 12 & 28).
28. **SIMD & Vectorization** — auto-vectorization, intrinsics, SSE/AVX/AVX-512,
    data layout for vectorization, when SIMD pays off (and downclocking caveats).

### Part VI — Concurrency
29. **The C++ Memory Model & Atomics** — `std::atomic`, `memory_order`,
    acquire/release/seq_cst, fences, compiler and hardware reordering.
30. **Multithreading & Concurrency Foundations** — threads, contention, scalability
    pitfalls, designing for shared-nothing.
31. **Spinlocks, Backoff & Contention Control** — spin vs block, test-and-test-and-set,
    exponential backoff, `pause`/`tpause`, MCS/CLH queue locks, futex internals, and when
    a well-tuned spinlock beats lock-free complexity.
32. **False Sharing & Thread-Safety Anomalies** — cache-line ping-pong, alignment/padding,
    `hardware_destructive_interference_size`, detection.
33. **Lock-Free Data Structures** — SPSC/MPMC queues, ring buffers, CAS loops, ABA,
    progress guarantees.
34. **Seqlocks & Single-Writer Publication** — versioned/sequence-lock snapshots for
    publishing market-data and reference state to many readers without locks; correctness
    under the memory model, torn-read avoidance, single-writer multiple-reader patterns.
35. **Safe Memory Reclamation** — RCU, hazard pointers, epoch-based reclamation for
    lock-free structures.
36. **The Disruptor Pattern** — ring buffer + sequence barriers, batching, mechanical
    sympathy, building a low-latency pipeline.
37. **Coroutines & Async Models** — C++20 coroutines, stackful vs stackless, event
    loops, async I/O without thread-per-connection.
38. **Concurrency Correctness Tooling** — TSan/ASan/UBSan, stress testing, fuzzing,
    model checking lock-free code.

### Part VII — OS, Scheduling & Isolation
39. **Context Switching & Its Mitigation** — direct/indirect costs, syscall overhead,
    keeping threads on-core, busy-poll vs block.
40. **Thread & Interrupt Pinning** — CPU affinity (`taskset`/`sched_setaffinity`),
    IRQ affinity, isolating the hot core.
41. **SMT / Hyperthreading** — how two hardware threads share one core: partitioned vs
    competitively-shared resources (front-end, execution ports, L1/L2, store buffer, TLB),
    the throughput-vs-latency trade-off and why HFT hot cores usually run with the sibling
    idle or HT disabled; detecting sibling topology, leaving the SMT sibling empty vs pinning
    only housekeeping to it, measuring per-thread interference; deciding HT on/off per core
    (builds on Ch. 5, 10 & 39–40).
42. **Real-Time Scheduling & Kernel Tuning** — `isolcpus`, `nohz_full`, RT priorities,
    cgroups, eliminating jitter, measuring with `cyclictest`.
43. **Keeping the Hot Path Warm** — cache/TLB/branch-predictor warming, shadow/dummy
    traffic, pre-touching pages and connections, avoiding cold-start stalls on the first
    real message; measuring warm vs cold tail latency.

### Part VIII — I/O & Networking
44. **Linux Native I/O** — blocking vs non-blocking, `epoll`, readiness vs completion,
    syscall batching.
45. **io_uring Deep Dive** — submission/completion queues, registered buffers/files,
    polling mode, batching for throughput and latency.
46. **Inter-Process Communication** — shared-memory queues, lock-free IPC, pipes vs
    shm, cross-process ring buffers.
47. **Socket Optimization & TCP/Protocol Tuning** — `TCP_NODELAY`/Nagle, buffer sizing,
    congestion control, multicast for market data.
48. **Zero-Copy Wire Handling & Market-Data Decoding** — parsing off the wire without
    copies: ITCH/OUCH/FIX/SBE/FAST, fixed-layout structs vs bit-fields, endianness,
    `std::from_chars`/`to_chars`, branch-free integer/decimal parsing, scatter/gather
    and zero-copy receive paths; feed-handler A/B line arbitration, sequence-gap
    detection and recovery on redundant multicast feeds.
49. **NIC Features & Offloads** — RSS, hardware timestamping, busy-polling,
    checksum/segmentation offload, Solarflare/Onload.
50. **Clock Synchronization & Hardware Timestamping (PTP)** — distributing time across
    hosts with PTP, NIC hardware timestamps, comparing timestamps across machines,
    measuring true wire-to-wire and tick-to-trade latency (builds on Ch. 16 & 49).
51. **eBPF, bpftrace & XDP / AF_XDP** — programmable kernel observability and fast
    networking: the eBPF VM and verifier, attaching probes (kprobes/uprobes/tracepoints/
    USDT), `bpftrace` one-liners and low-overhead production tracing of latency and syscalls
    (builds on Ch. 2); XDP for in-driver packet processing (drop/redirect/filter at the
    earliest hook) and AF_XDP zero-copy sockets for userspace fast paths — a middle ground
    between the kernel stack and full bypass; measuring overhead and where eBPF/XDP belongs
    off vs near the hot path (leads into Ch. 52).
52. **Kernel Bypass & Userspace Networking** — DPDK, poll-mode drivers, hugepage
    mempools, userspace TCP stacks, the tick-to-trade fast path.
53. **InfiniBand Verbs & RDMA** — one-sided remote memory access for ultra-low-latency
    messaging: the verbs API (queue pairs, completion queues, memory regions and
    registration, work requests), RDMA read/write vs send/recv, RoCEv2 vs native
    InfiniBand, polling completions for lowest latency, registered-memory and zero-copy
    considerations; comparing RDMA to kernel bypass and where it fits intra-datacenter
    fabrics (builds on Ch. 25 & 52; ties Ch. 55 & 57).

### Part IX — Heterogeneous Computing & Hardware Acceleration
54. **GPU Computing with CUDA** — offloading data-parallel work to the GPU: the CUDA
    execution and memory model, warps/occupancy, host–device transfer cost, pinned and
    unified memory, streams and overlap; where GPUs pay off in trading (risk, options
    pricing/Greeks, Monte-Carlo, backtesting, ML inference) and why PCIe round-trip
    latency keeps them off the tick-to-trade hot path; measuring end-to-end including
    transfer overhead, not just kernel time.
55. **Distributed Computing with MPI** — scaling across cores and hosts with OpenMPI:
    point-to-point vs collective operations, communicators, non-blocking calls and
    compute/communication overlap, RDMA/InfiniBand transports, NUMA- and topology-aware
    rank placement (builds on Ch. 15 & 53); large-scale backtesting and Monte-Carlo risk
    across a cluster, and why message latency keeps MPI off the order path.
56. **FPGA Acceleration** — programmable hardware on the tick-to-trade fast path: HLS
    vs RTL, the host–FPGA boundary, NIC-integrated FPGAs and inline feed handling/order
    entry, deterministic nanosecond pipelines, partial reconfiguration; deciding what
    belongs in fabric vs software, and measuring wire-to-wire latency of a hardware path
    (builds on Ch. 48–53).
57. **SmartNICs & DPUs** — programmable network hardware between the wire and the host:
    offloading packet processing, filtering, timestamping and even feed handling/order
    entry onto NIC-resident ARM cores or FPGA fabric (NVIDIA BlueField, Intel IPU, AMD
    Pensando); the host–DPU boundary and programming models (DOCA, P4, eBPF/XDP offload),
    on-NIC RDMA and storage/security offload, partitioning work between DPU and host; when
    a DPU earns its place on the tick-to-trade path vs adding a hop (builds on Ch. 49, 51
    & 56; ties Ch. 53).

### Part X — Observability & Operations in Production
58. **Zero-Overhead Logging** — async/lock-free loggers, deferred formatting, binary
    logging, `std::print`/`std::println` for off-hot-path output, keeping logging off the
    hot path.
59. **Secure Programming for Low-Latency Systems** — security as a hot-path concern:
    treating market-data and order-entry messages as untrusted input — bounds/length
    validation on variable-length fields and zero-copy parses, defending feed handlers
    against malformed/hostile packets (builds on Ch. 48); memory safety in
    zero-allocation/arena designs (buffer overflows, use-after-free, lifetime bugs) and
    bounds checks that compile away; integer/arithmetic safety in price/quantity math
    (overflow, signed/unsigned and scaled-integer pitfalls — builds on Ch. 26);
    undefined behavior as a vulnerability class (builds on Ch. 19–20); the
    hardening-vs-latency trade-off — stack protector, `_FORTIFY_SOURCE`, PIE/ASLR, CFI,
    CET shadow stacks and how they interact with Spectre/Meltdown mitigations (builds on
    Ch. 5), measuring their hot-path cost and choosing what to keep on an isolated box;
    handling session credentials and secrets (non-elided secure wiping, keeping secrets
    out of logs — ties Ch. 58); privilege reduction and isolation (`seccomp`, dropping
    capabilities, namespaces) and their latency implications; build/supply-chain
    integrity (dependency vetting, reproducible and signed builds); and a security
    toolchain — ASan/UBSan/MSan and fuzzing message parsers (libFuzzer/AFL++) on the
    decode path (builds on Ch. 38).
60. **Hot Reload & Live Reconfiguration** — changing strategy parameters, symbol/reference
    data and even code without restarting or dropping a tick: atomic pointer swaps and
    double-buffering, seqlock-/RCU-published config snapshots read lock-free on the hot
    path, versioned configuration and safe reclamation of the old version; shared-memory
    config segments, validation-before-swap, draining vs instantaneous cutover, and
    dynamic-library/strategy reload with warm-up; testing that a reload never tears or
    stalls the hot path (builds on Ch. 29 & 34–35; ties Ch. 25 & 43).
61. **Production Profiling & End-to-End Case Study** — continuous performance
    monitoring, regression detection, and a full tick-to-trade walkthrough tying the
    book together; determinism and capture/replay — deterministic replay of captured
    market data, simulation harnesses, and latency-regression gates in CI.

### Appendices
- **Appendix A — ARM / Graviton & Non-x86 Targets** — porting the low-latency toolkit to
  AArch64 (AWS Graviton, Ampere Altra): the ARM memory model (weaker ordering, `LDAR`/`STLR`,
  `DMB`) vs x86-TSO and what changes for the atomics/seqlock/lock-free chapters (Ch. 29–35);
  NEON/SVE/SVE2 vs AVX (Ch. 28), cache-line size differences (64 vs 128 bytes) and their
  effect on `hardware_destructive_interference_size` and false sharing (Ch. 32); timers
  (`CNTVCT_EL0` vs `rdtsc` — Ch. 16), `WFE`/`YIELD` vs `pause` (Ch. 31), big.LITTLE /
  heterogeneous cores and SMT differences (Ch. 41); `-mcpu`/`-mtune` tuning (Ch. 21),
  spec-exec mitigations on ARM (Ch. 5), and where Graviton in the cloud fits a latency-
  sensitive workload (and where bare-metal colocation still wins).
- **Appendix B — Beyond C++: Alternative Languages for Low-Latency** — surveying Rust
  (ownership, `unsafe`, no GC, fearless concurrency), C, Zig, and the case against managed
  runtimes for the hot path (Java/C#/Go GC pauses and JIT warm-up — and the lengths HFT shops
  go to tame them: allocation-free Java, off-heap memory, Azul/Zing pauseless GC, GraalVM
  native-image); FFI/interop with a C++ core, what transfers from this book and what does not,
  when a rewrite pays off, and matching language to the latency tier.
- **Appendix C — System Tuning Checklist** — a copy-paste-ready, ordered checklist for
  building a quiet, deterministic box: BIOS/firmware (disable C-states deeper than C1, fix
  P-states/turbo, disable SMT where applicable, NUMA/snoop mode); kernel cmdline (`isolcpus`,
  `nohz_full`, `rcu_nocbs`, `intel_pstate`, `mitigations=`, huge pages); runtime knobs
  (`cpupower`/governor, IRQ affinity and `irqbalance` off, `tuned` profiles, RPS/XPS, NIC
  ring/coalescing, `sysctl` net/VM, transparent-huge-page mode, `mlockall`); per-process
  (affinity/`taskset`, RT priority, `numactl`); and a verification pass (`cyclictest`, jitter
  measurement). Each item links to the chapter that explains *why* (consolidates Ch. 5, 14,
  39–43, 47, 49).
- **Appendix D — Compiler Flag Reference** — a categorized GCC/Clang flag cheat sheet with
  the latency-relevant ones called out: optimization levels (`-O2`/`-O3`/`-Ofast` caveats),
  arch/tuning (`-march`/`-mtune`/`-mcpu`), FP and math (`-ffast-math` hazards, `-fno-math-errno`,
  FTZ/DAZ), codegen (`-fno-exceptions`/`-fno-rtti`, `-fno-plt`, `-fno-semantic-interposition`,
  `-fno-omit-frame-pointer` for profiling), inlining/layout (`-finline-limit`,
  `-freorder-blocks-and-partition`, `-falign-*`), LTO/PGO/BOLT, diagnostics (`-Wpadded`,
  `-Wfloat-equal`, `-fopt-info`), and sanitizers; what each costs and when to drop it on the
  hot path (consolidates Ch. 5, 16, 19–21, 26).
- **Appendix E — Latency Numbers Every Trading Developer Should Know** — the
  Norvig/Jeff-Dean "latency numbers" table, refreshed for modern x86-64 server hardware and
  annotated for trading: L1 / L2 / L3 hit, local-DRAM and NUMA-remote-DRAM access, branch
  mispredict, cache-line transfer between cores (HITM), uncontended mutex lock/unlock, a
  null syscall, a context switch, kernel-stack NIC RX vs kernel-bypass RX, and a PCIe
  round-trip — each in ns and in cycles, with how-it-was-measured notes and pointers to the
  chapter that derives it (ties Ch. 3, 6, 12, 15, 16, 31–32, 39, 44, 49 & 52).

## Status / Workflow
- Phase 1 (done): TOC established above (61 chapters + 5 appendices, A–E).
- Phase 2 (current): generating chapters one at a time into `chapters/NN-slug.md` (appendices
  into `chapters/appendix-X-slug.md`, e.g. `appendix-A-arm-graviton.md`), following the authoring
  conventions. Drafted (against the pre-expansion numbering, now remapped): **Ch. 1–7, 9, 10,
  12, 14, 15, 16, 17 and Ch. 32**. That is — Part I complete; Part II drafted *except* the three
  newly inserted chapters **Ch. 8 (Object Layout), Ch. 11 (Instruction Cache) and Ch. 13
  (Indirect Calls)**, which are not yet written; Part III begun with **Ch. 17 — Compile-Time
  Mechanics**; and **Ch. 32 — False Sharing** drafted ahead of sequence on request. Outstanding
  in Part II: draft the three new chapters (8, 11, 13). Sequential drafting otherwise resumes at
  **Ch. 18 — Template Metaprogramming & Zero-Cost Abstractions**.
- When generating a chapter, confirm scope against this TOC and keep cross-references
  to other chapters by number/title.
- Benchmark output blocks in drafts are **representative** for the stated reference
  machine (authoring is on macOS; target is Linux x86-64); code/flags/`perf` commands are
  reproducible. Real measured runs must be backfilled before a chapter is final.
- Reference machine: Ch. 6–14 use a single-socket Xeon Gold 6326 (Ice Lake-SP); Ch. 15
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
