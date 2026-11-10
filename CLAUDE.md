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
  (hot-path error handling), `std::mdspan` — all C++23 — and the C++20 `std::span`,
  `std::bit_cast` and `<bit>` where the toolchain is C++20-capable. Show the C++17/20
  fallback where the target toolchain lacks the feature, and note compiler/library availability
  (Appendix H is the matrix).

## Table of Contents

> **Note:** this TOC is the *original, pre-reorganisation drafting order* (64 chapters + Appendices
> A–G), kept for history. The book was since reorganised into thirteen Parts with 76 chapters and twelve
> appendices (A–L), and on 2026-11-09 extended first to 81 chapters and thirteen appendices (A–M), then to 84 chapters and fourteen appendices (A–N), then — with a new Part XII (Beyond C++) — to 89 chapters in thirteen Parts, and then — with the design-pattern chapters — to 95 chapters in thirteen Parts and fifteen appendices (A–O), and then — with the modern-C++ depth chapters — to **98 chapters in thirteen Parts and sixteen appendices (A–P)**; **the authoritative current structure is `chapters/README.md`** (see Phase 3 under
> *Status / Workflow* below). Do not renumber against this list. The **Appendices** entries (A–P)
> at the end of the list are the exception: they cite the *current, post-reorg* chapter numbers,
> because the appendices themselves were written (or re-pointed) against that numbering.

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
    **Case Study: Building the Limit Order Book** — array-of-pointers vs. intrusive linked 
    lists for price levels, handling BBO (top-of-book) vs. deep-book additions, and efficiently 
    consolidating disparate feeds.
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
    detection and recovery on redundant multicast feeds. **Mechanical Sympathy for UDP 
    Microbursts:** tuning hardware RX ring sizes, managing UDP packet reordering in 
    software ring buffers, and detecting hardware-level packet drops during market open.
49. **NIC Features & Offloads** — RSS, hardware timestamping, busy-polling,
    checksum/segmentation offload, Solarflare/Onload.
50. **Clock Synchronization & Hardware Timestamping (PTP)** — distributing time across
    hosts with PTP, NIC hardware timestamps, comparing timestamps across machines,
    measuring true wire-to-wire and tick-to-trade latency (builds on Ch. 16 & 49).
    **Advanced Synchronization:** sub-nanosecond extensions via White Rabbit, utilizing 
    PPS (Pulse Per Second) signaling for boundary clocks, and accounting for grandmaster 
    oscillator drift.
51. **eBPF, bpftrace & XDP / AF_XDP** — programmable kernel observability and fast
    networking: the eBPF VM and verifier, attaching probes (kprobes/uprobes/tracepoints/
    USDT), `bpftrace` one-liners and low-overhead production tracing of latency and syscalls
    (builds on Ch. 2); XDP for in-driver packet processing (drop/redirect/filter at the
    earliest hook) and AF_XDP zero-copy sockets for userspace fast paths — a middle ground
    between the kernel stack and full bypass; measuring overhead and where eBPF/XDP belongs
    off vs near the hot path (leads into Ch. 52).
52. **Kernel Bypass & Userspace Networking** — DPDK, poll-mode drivers, hugepage
    mempools, userspace TCP stacks, the tick-to-trade fast path. **Proprietary Vendor APIs:** leveraging Solarflare `ef_vi` and ExaNIC `libexanic` for raw, zero-copy access to the 
    NIC ring buffer; exploring the latency floors of transparent vs. explicit bypass.
53. **InfiniBand Verbs & RDMA** — one-sided remote memory access for ultra-low-latency
    messaging: the verbs API (queue pairs, completion queues, memory regions and
    registration, work requests), RDMA read/write vs send/recv, RoCEv2 vs native
    InfiniBand, polling completions for lowest latency, registered-memory and zero-copy
    considerations; comparing RDMA to kernel bypass and where it fits intra-datacenter
    fabrics (builds on Ch. 25 & 52; ties Ch. 56 & 58).

### Part IX — Heterogeneous Computing & Hardware Acceleration
54. **PCIe & the Host–Device Boundary** — the bus every accelerator and modern NIC sits
    behind: PCIe topology, lanes and generations (Gen3/4/5) and their bandwidth, MMIO and
    BAR-mapped device registers, DMA and the IOMMU, descriptor rings and doorbells, posted
    vs non-posted transactions and why a device-register *read* costs a full round-trip,
    write-combining and relaxed ordering, MSI-X interrupts vs polling for completions, and
    peer-to-peer DMA (GPUDirect, NIC-to-GPU/FPGA); measuring round-trip latency and
    streaming bandwidth, and why the PCIe round-trip is the floor under every offload
    decision in this Part (builds on Ch. 6 & 25; leads into Ch. 55–58; ties Ch. 49).
55. **GPU Computing with CUDA** — offloading data-parallel work to the GPU: the CUDA
    execution and memory model, warps/occupancy, host–device transfer cost, pinned and
    unified memory, streams and overlap; where GPUs pay off in trading (risk, options
    pricing/Greeks, Monte-Carlo, backtesting, ML inference) and why PCIe round-trip
    latency keeps them off the tick-to-trade hot path; measuring end-to-end including
    transfer overhead, not just kernel time.
56. **Distributed Computing with MPI** — scaling across cores and hosts with OpenMPI:
    point-to-point vs collective operations, communicators, non-blocking calls and
    compute/communication overlap, RDMA/InfiniBand transports, NUMA- and topology-aware
    rank placement (builds on Ch. 15 & 53); large-scale backtesting and Monte-Carlo risk
    across a cluster, and why message latency keeps MPI off the order path.
57. **FPGA Acceleration** — programmable hardware on the tick-to-trade fast path: HLS
    vs RTL, the host–FPGA boundary, NIC-integrated FPGAs and inline feed handling/order
    entry, deterministic nanosecond pipelines, partial reconfiguration; deciding what
    belongs in fabric vs software, and measuring wire-to-wire latency of a hardware path
    (builds on Ch. 48–53).
58. **SmartNICs & DPUs** — programmable network hardware between the wire and the host:
    offloading packet processing, filtering, timestamping and even feed handling/order
    entry onto NIC-resident ARM cores or FPGA fabric (NVIDIA BlueField, Intel IPU, AMD
    Pensando); the host–DPU boundary and programming models (DOCA, P4, eBPF/XDP offload),
    on-NIC RDMA and storage/security offload, partitioning work between DPU and host; when
    a DPU earns its place on the tick-to-trade path vs adding a hop (builds on Ch. 49, 51
    & 57; ties Ch. 53).

### Part X — Observability & Operations in Production
59. **Zero-Overhead Logging** — async/lock-free loggers, deferred formatting, binary
    logging, `std::print`/`std::println` for off-hot-path output, keeping logging off the
    hot path.
60. **Secure Programming for Low-Latency Systems** — security as a hot-path concern:
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
    out of logs — ties Ch. 59); privilege reduction and isolation (`seccomp`, dropping
    capabilities, namespaces) and their latency implications; build/supply-chain
    integrity (dependency vetting, reproducible and signed builds); and a security
    toolchain — ASan/UBSan/MSan and fuzzing message parsers (libFuzzer/AFL++) on the
    decode path (builds on Ch. 38).
61. **Hot Reload & Live Reconfiguration** — changing strategy parameters, symbol/reference
    data and even code without restarting or dropping a tick: atomic pointer swaps and
    double-buffering, seqlock-/RCU-published config snapshots read lock-free on the hot
    path, versioned configuration and safe reclamation of the old version; shared-memory
    config segments, validation-before-swap, draining vs instantaneous cutover, and
    dynamic-library/strategy reload with warm-up; testing that a reload never tears or
    stalls the hot path (builds on Ch. 29 & 34–35; ties Ch. 25 & 43).
62. **Process Topology & The Deterministic State Machine** — structuring the trading system 
    as multiple cooperating processes for fault containment instead of one monolith: 
    process-per-role (feed handler / strategy / order gateway / risk). **The Deterministic 
    State Machine Pattern:** isolating the core matching or routing logic into a pure, 
    side-effect-free state machine decoupled from network I/O and timers to guarantee 
    exact replayability. Shared-memory data planes between isolated processes (builds on 
    Ch. 25 & 46), fault domains and blast radius, the single-writer-per-segment discipline, 
    supervisor/watchdog processes, heartbeats and liveness detection, crash-only design 
    and fast restart with hot-path warm-up (ties Ch. 43 & 60), kill-switch / safe-state 
    on failure, capturing core dumps without stalling survivors, and symbol/venue sharding 
    so one strategy's crash can't take down the session.
63. **Capture, Persistence & Replay Storage** — recording every inbound tick and outbound
    order for deterministic replay, research and compliance: lossless line capture and
    nanosecond packet timestamps (ties Ch. 49–50), append-only/write-ahead journals and
    compact binary log formats (builds on Ch. 58), getting bytes to disk *off* the hot
    path (async writers, io_uring, `O_DIRECT`, NVMe, batching — ties Ch. 45 & 59),
    sequencing and gap-free persistence, time-indexed storage, retention and compaction,
    and feeding captures into deterministic replay, simulation harnesses and backtests
    (ties Ch. 61 & 64).
64. **Production Profiling & End-to-End Case Study** — continuous performance
    monitoring, regression detection, and a full tick-to-trade walkthrough tying the
    book together; determinism and capture/replay — deterministic replay of captured
    market data through the core state machine, simulation harnesses, and latency-regression 
    gates in CI (builds on Ch. 62 & 63).

### Appendices
- **Appendix A — ARM / Graviton & Non-x86 Targets** — porting the low-latency toolkit to
  AArch64 (AWS Graviton, Ampere Altra): the ARM memory model (weaker ordering, `LDAR`/`STLR`,
  `DMB`) vs x86-TSO and what changes for the atomics/seqlock/lock-free chapters (Ch. 30–36);
  NEON/SVE/SVE2 vs AVX (Ch. 29), cache-line size differences (64 vs 128 bytes) and their
  effect on `hardware_destructive_interference_size` and false sharing (Ch. 33); timers
  (`CNTVCT_EL0` vs `rdtsc` — Ch. 17), `WFE`/`YIELD` vs `pause` (Ch. 32), big.LITTLE /
  heterogeneous cores and SMT differences (Ch. 43); `-mcpu`/`-mtune` tuning (Ch. 22),
  spec-exec mitigations on ARM (Ch. 6), and where Graviton in the cloud fits a latency-
  sensitive workload (and where bare-metal colocation still wins).
- **Appendix B — Beyond C++: Alternative Languages for Low-Latency** — originally the whole
  treatment of Rust, C, Zig, the managed runtimes and FFI; since the 2026-11-09 languages insertion
  a **two-page summary and tier table pointing at Part XII** (Ch. 73–77), where the measured
  chapters now live.
- **Appendix C — System Tuning Checklist** — a copy-paste-ready, ordered checklist for
  building a quiet, deterministic box: BIOS/firmware (disable C-states deeper than C1, fix
  P-states/turbo, disable SMT where applicable, NUMA/snoop mode); kernel cmdline (`isolcpus`,
  `nohz_full`, `rcu_nocbs`, `intel_pstate`, `mitigations=`, huge pages); runtime knobs
  (`cpupower`/governor, IRQ affinity and `irqbalance` off, `tuned` profiles, RPS/XPS, NIC
  ring/coalescing, `sysctl` net/VM, transparent-huge-page mode, `mlockall`); per-process
  (affinity/`taskset`, RT priority, `numactl`); and a verification pass (`cyclictest`, jitter
  measurement). Each item links to the chapter that explains *why* (consolidates Ch. 6, 15, 16,
  41–46, 51, 55, 57).
- **Appendix D — Compiler Flag Reference** — a categorized GCC/Clang flag cheat sheet with
  the latency-relevant ones called out: optimization levels (`-O2`/`-O3`/`-Ofast` caveats),
  arch/tuning (`-march`/`-mtune`/`-mcpu`), FP and math (`-ffast-math` hazards, `-fno-math-errno`,
  FTZ/DAZ), codegen (`-fno-exceptions`/`-fno-rtti`, `-fno-plt`, `-fno-semantic-interposition`,
  `-fno-omit-frame-pointer` for profiling), inlining/layout (`-finline-limit`,
  `-freorder-blocks-and-partition`, `-falign-*`), LTO/PGO/BOLT, diagnostics (`-Wpadded`,
  `-Wfloat-equal`, `-fopt-info`), and sanitizers; what each costs and when to drop it on the
  hot path, plus the hardening/mitigation flags (consolidates Ch. 6, 17, 20–22, 27, 72).
- **Appendix E — Latency Numbers Every Trading Developer Should Know** — the
  Norvig/Jeff-Dean "latency numbers" table, refreshed for modern x86-64 server hardware and
  annotated for trading: L1 / L2 / L3 hit, local-DRAM and NUMA-remote-DRAM access, branch
  mispredict, cache-line transfer between cores (HITM), uncontended mutex lock/unlock, a
  null syscall, a context switch, kernel-stack NIC RX vs kernel-bypass RX, and a PCIe
  round-trip — each in ns and in cycles, with how-it-was-measured notes and pointers to the
  chapter that derives it (ties Ch. 3, 7, 13, 16, 17, 32–33, 41, 47, 55 & 62).
- **Appendix F — Glossary (HFT & Microarchitecture Terms)** — an alphabetized quick
  reference for the acronyms and jargon used throughout, each entry a line or two with a
  pointer to the chapter that develops it: trading & market-structure (tick-to-trade,
  NBBO, order book / level / top-of-book, ITCH/OUCH/FIX/SBE/FAST, A/B feed arbitration,
  colocation, maker/taker, kill-switch); microarchitecture (IPC, µop & DSB, ROB, LFB,
  MLP, MESI/MOESI & HITM, BTB, store buffer, TLB, prefetcher, SMT); memory & OS (NUMA,
  huge pages, page fault, futex, `isolcpus`/`nohz_full`, IRQ affinity); concurrency
  (acquire/release, seqlock, RCU, hazard pointer, ABA, false sharing); and C++/toolchain
  (RAII, `noexcept`, `std::expected`, CRTP, LTO, PGO, BOLT). Cross-references the chapter
  where each term is defined and measured.
- **Appendix G — Annotated Bibliography** — a curated reading list consolidating the
  per-chapter *References* and *Additional Reading*, each entry with a sentence on what
  it's good for and which chapters draw on it: foundational papers (Drepper's *What Every
  Programmer Should Know About Memory*, Dean's latency numbers, the LMAX *Disruptor*
  paper, McKenney on RCU); vendor and reference manuals (Intel SDM and *Optimization
  Reference Manual*, Agner Fog's microarchitecture and instruction tables, AMD and ARM
  architecture references, the kernel and `perf` docs); books (Hennessy & Patterson,
  Williams' *C++ Concurrency in Action*, Fog's optimization series); influential talks and
  blogs (CppCon performance talks, Carl Cook's "When a Microsecond Is an Eternity," the
  Mechanical Sympathy community); and tooling documentation (Google Benchmark, VTune,
  `liburing`, DPDK, bpftrace). Organized to mirror the Parts of the book.
- **Appendix H — Modern C++ (C++20/23/26) Feature Availability Matrix** — a support table for the
  modern-library/language features the book reaches for (`std::expected`, `std::flat_map`/
  `std::flat_set`, `std::print`, `std::mdspan`, `std::span`, `std::bit_cast`, `<bit>`,
  senders/receivers): first GCC/libstdc++ and Clang/libc++ version each landed in, the
  `__cpp_lib_*`/`__cpp_*` feature-test macro to gate on, and the C++17/20 fallback. Makes the
  "use modern C++ where it helps, and name the standard + toolchain support" convention
  actionable in one place.
- **Appendix I — Tooling Command Cookbook** — a copy-paste one-liner reference
  consolidating the commands scattered across chapters: `perf` (stat/record/annotate,
  top-down, `c2c`/`mem`), VTune, `bpftrace`/eBPF, topology/pinning (`lscpu`/`lstopo`/
  `numactl`/`taskset`/`chrt`), layout (`pahole`, cachegrind), network/NIC (`ethtool`/`ss`/
  `tcpdump`/PTP), and codegen/binaries/sanitizers (`objdump`, Godbolt, BOLT, ASan/UBSan/TSan).
  The *operational* companion to Ch. 2–5 and 61 (consolidates Ch. 2, 3, 4, 5, 9, 61 and
  Appendix C).
- **Appendix J — HFT Market-Structure & Protocol Primer** — a domain onramp for the
  advanced-C++-but-new-to-trading reader: matching engines and price-time priority, the order
  book (BBO/NBBO/depth), the order lifecycle, market-data feeds (A/B lines, snapshots vs.
  increments), a field-level quick reference for ITCH/OUCH/FIX/SBE/FAST, the roles in a
  trading system, and the tick-to-trade path end to end. Makes the trading framing
  self-contained (complements Appendix F; ties Ch. 25, 53, 54, 76).
- **Appendix K — Exchange Connectivity & the Physical Layer** — the infrastructure
  floor beneath the software: colocation and cross-connects, latency-equalized cabling, fiber
  vs. copper vs. microwave/millimeter-wave, exchange access/entitlements/throttles and
  pre-trade-risk mandates, venue-side time distribution (grandmaster/PPS/PTP boundary clocks),
  path diversity, and the latency/cost/regulation trade-offs — the physical constraints the
  rest of the book optimizes within (ties Ch. 54, 55, 58, 60, 76).
- **Appendix L — Reproducible Benchmark & Measurement Harness** — the reusable
  scaffold behind every measurement in the book: a copy-paste Google Benchmark skeleton
  (`DoNotOptimize`/`ClobberMemory`, fixture setup vs. steady-state split), a tail-latency
  (HdrHistogram) variant that avoids coordinated omission, the pinning/warm-up/frequency
  environment setup that makes runs repeatable, and a pre-trust checklist. Implements Ch. 3's
  method as copy-paste code (consolidates Ch. 1, 3, 4, 17, 46 and Appendices C, E, I).
- **Appendix M — Pre-Open Readiness Gate & Market-Open Playbook** — the operational
  companion to Appendix C: the cold → warming → syncing → reconciled → armed → trading → disarmed
  readiness state machine published in the telemetry segment and read by the order path with one
  predicted branch; the T-60 (host attestation against a release-generated fingerprint), T-30 (PTP
  servo/offset, multicast membership and querier, both lines live, snapshot rebuilt, zero drops,
  samplers quiet) and T-5 (session logon and sequence reconciliation, positions/limits, warm-up,
  kill-switch dry-run) gates as commands with expected values; T-0 arming, continuous
  re-verification, time-boxed re-arm after an intraday restart, disarm; the host fingerprint
  micro-suite; configuration as code and drift detection (with Appendix C.6); and the gate runner
  as a script (current numbering: Ch. 46, 47, 55, 56, 60, 75, 78, 82, 83; Appendices C, E, J, K, L).
- **Appendix N — Incident Diagnostics Bundle** (added 2026-11-09 with the debugging chapters) — the
  checklist and bounded collector behind Ch. 81's forensic method and Ch. 79's post-mortem: what makes
  a bundle alignable (clock anchors), attributable (build IDs, recorded flags, configuration hash,
  host fingerprint) and checkable (raw rings and sample windows with a manifest and checksums); the
  evidence per layer with the producing chapter; the ten-minute first look; retention and scrubbing
  (current numbering: Ch. 5, 46, 60, 73–75, 77, 79–83; Appendices C, D, I, K, M).
- **Appendix O — Hot-Path Anti-Pattern Catalogue** (added 2026-11-09 with the design-pattern chapters) —
  every chapter's §N.5/§N.6 *Pitfalls & anti-patterns* re-sorted by where the sin lives (allocation;
  dispatch/type erasure; control flow & memory ordering; sharing; queues/flow control/timers;
  kernel/scheduling/syscalls; I/O/networking/logging/capture; layout & caches; numerics; build/flags;
  measurement; operational/architectural), each row symptom → the counter/probe that exposes it
  (Appendix I's invocations) → fix → the §N.6 that develops it; a ten-row short list; the pre-merge
  review checklist Ch. 95 §95.4.1 asks for and Appendix I's companion.
- **Appendix P — Attributes, Builtins & the UB Catalogue** (added 2026-11-09 with the modern-C++ depth
  chapters) — the per-function companion to Appendix D's per-build flags: a table of attribute/builtin →
  what it tells the optimizer → GCC/Clang spelling → the chapter that measures it → the asm fingerprint
  that proves it fired (`[[likely]]`/`expect_with_probability`, hot/cold/inlining/`flatten`, `pure`/`const`,
  `[[assume]]`/`unreachable`, `assume_aligned`, `restrict`/`may_alias`, `target_clones`/ifunc, `musttail`,
  `trivial_abi`, `no_unique_address`, `init_priority`, overflow builtins/`add_sat`, C++26 contracts); and the
  UB catalogue — rule → optimisation licensed → buy-back flag with its measured cost (`-fwrapv` 2.5× on a
  strided loop in GCC, `-fno-strict-aliasing` 3.6× on an `int*`-bounded axpy in Clang,
  `-fno-delete-null-pointer-checks` +0.15 ns/call, `-ftrivial-auto-var-init` 0 where the buffer dies, full
  trapping UBSan 2.5–20× vs the overflow/bounds/shift/null subset 1.0–1.5×) → sanctioned replacement
  (current numbering: Ch. 4, 12–14, 21, 23, 30, 32, 45, 60, 87; Appendices D, H, I).

## Status / Workflow
- Phase 1 (done): TOC established above (64 chapters + 7 appendices A–G; since extended to 98
  chapters and appendices A–P — see Phases 2–3 and the five 2026-11-09 insertions below).
- Phase 2 (done): all chapters drafted into `chapters/NN-slug.md` (appendices into
  `chapters/appendix-X-slug.md`), following the authoring conventions.
  **STATUS: COMPLETE — front matter (`00-preface.md`) + 104 chapters + 16 appendices (A–P);
  Appendices A–G were drafted with the chapters, H–L were added afterwards (next bullet), M
  with the 2026-11-09 monitoring chapters, N with the 2026-11-09 debugging chapters, O with
  the 2026-11-09 design-pattern chapters and P with the 2026-11-09 modern-C++ depth chapters.**
  **Appendices H–L added and fully drafted to the authoring conventions: H (C++23/26 feature
  matrix), I (tooling command cookbook), J (HFT market-structure primer), K (exchange
  connectivity / physical layer), L (reproducible benchmark & measurement harness). Total
  now: front matter + 104 chapters + 16 appendices (A–P). Appendix H's version numbers and
  Appendix L's code should be verified against current toolchains as part of the Phase-4
  revision pass.**
- Phase 3 (done): **the book was reorganized into twelve topical Parts** (thirteen since the
  2026-11-09 languages insertion below) and every chapter
  renumbered accordingly (the `## Table of Contents` prose above predates this reorg and reflects
  the original drafting order — **the authoritative current structure is `chapters/README.md`**).
  The thirteen Parts and their chapter ranges:
  - **I — Foundations & Methodology** (1–6): mindset, profiling, benchmarking, reading asm,
    debugging optimized code, system setup.
  - **II — CPU Microarchitecture** (7–17).
  - **III — Compile-Time & Language Mechanics** (18–25): compile-time mechanics, templates & zero-cost
    abstractions (with concepts, §19.4.6), compile-time protocol decoders (Ch. 20, added 2026-11-09 P3-5), the cost
    of abstractions (with the calling convention and statics/TLS, §21.4.5–§21.4.6), the cost of modern idioms &
    strings without allocation (Ch. 22, P3-5), aliasing & type punning (with object lifetime / `start_lifetime_as`),
    the build toolchain, build systems & hermetic toolchains (Ch. 25, P3-5).
  - **IV — Memory Management** (26–29).
  - **V — Numerics & Data Parallelism** (30–32).
  - **VI — Concurrency** (33–45): …the Disruptor, threading topologies & the hot loop (Ch. 41, added
    2026-11-09 P3-4), flow control/backpressure/conflation (Ch. 42, P3-4), coroutines, `std::execution`,
    correctness tooling.
  - **VII — OS, Scheduling & Isolation** (46–53): context switching, timers & deadlines without jitter
    (Ch. 47, added 2026-11-09 P3-4), pinning, SMT, cache allocation (RDT), RT scheduling, hunting
    residual noise (Ch. 52, added 2026-11-09 P3-1), warming.
  - **VIII — Kernel I/O, Sockets & Zero-Copy** (54–59): native I/O, io_uring, zero-copy fast path,
    IPC, socket tuning, advanced TCP.
  - **IX — Market Data, NIC & Fabric** (60–68): decoding, reliable multicast, multicast membership &
    dark-feed detection (Ch. 59, added 2026-11-09), NIC, DDIO, flow steering, PTP, precise transmission, fabric.
  - **X — Kernel Bypass, RDMA & Transport** (69–73): eBPF/XDP, bypass, RDMA, lossless Ethernet,
    transport beyond TCP.
  - **XI — Heterogeneous Computing & Hardware Acceleration** (74–78): PCIe, GPU, MPI, FPGA, SmartNICs.
  - **XII — Beyond C++: Languages at the Latency Floor** (76–80, added 2026-11-09 P3-3): hand-written
    & inline assembly, Rust for the hot path, language interop & ABI boundaries, managed runtimes
    measured, choosing a language per latency tier (the Part's capstone).
  - **XIII — Observability & Operations in Production** (84–104): logging, in-band latency tracing
    (Ch. 85, added 2026-11-09 P3-2), application telemetry (Ch. 86, P3-1), security, hot reload, process
    topology (with §89.4.7–§89.4.8 checkpointing and segment recovery, P3-6), the pre-trade risk gate &
    kill-switch path (Ch. 90, P3-4), idempotent order entry & order-state recovery (Ch. 91, P3-4), the
    order-entry gateway & session layer (Ch. 92, P3-6), crash handling & post-mortem (Ch. 93, P3-2),
    capture/replay, line-rate capture & timing-faithful replay (Ch. 95, P3-6), the sequencer & replicated
    deterministic consumers (Ch. 96, P3-4), failover without split-brain (Ch. 97, P3-6), latency-spike
    forensics (Ch. 98, P3-2), host health telemetry and network health & drop forensics (Ch. 99–100, P3-1),
    testing latency-critical code (Ch. 101, P3-6), performance engineering in CI & the profile lifecycle
    (Ch. 102, P3-6), release engineering & measuring changes in production (Ch. 103, P3-6), end-to-end case
    study (Ch. 104, the capstone, still last).
  The reorg was done by a scripted renumber + cross-reference remap (chapter numbers, `§N.M`
  section refs, part headers, file renames), followed by an editorial pass refreshing every moved
  chapter's framing/teasers and regenerating `chapters/README.md` from the chapter files.
  A full consistency sweep (title/section numbering, teaser chaining N→N+1, cross-ref range,
  README link resolution, no-hard-wrap) is clean. A PDF build toolchain lives in `tools/book-pdf/`
  (pandoc + tectonic). Remaining work is *revision*: backfill real measured benchmark runs.
- **2026-11-09 monitoring insertion (work-queue item P3-1):** five chapters were inserted with
  `tools/review/insert_chapter.py` (which renumbers files, headings, teasers, every `Ch.`/`§`/file
  cross-reference, the README outline, the preface and the Part ranges above, and appends a step to
  `tools/review/stale_crossrefs.py`'s renumber chain): **46** Hunting Residual Noise on Isolated
  Cores, **56** Multicast Group Management & Dark-Feed Detection, **74** Zero-Overhead Application
  Telemetry, **79** Host Health Telemetry, **80** Network Health Telemetry & Drop Forensics (numbers as
  of that insertion; now 75, 82 and 83 after the debugging insertion below). Renumber
  map: 1–45 unchanged; old 46–54 → 47–55; old 55–71 → 57–73; old 72–75 → 75–78; old 76 → 81.
  Also added: §55.4.4 (feed liveness, invariants, session health), **Appendix M** (readiness gate &
  market-open playbook) and Appendix C.6 (configuration as code). Use `insert_chapter.py` for any
  further insertion; never renumber by hand.
- **2026-11-09 debugging insertion (work-queue item P3-2):** three chapters and one appendix were
  added with `insert_chapter.py` (three ascending insertions, one commit each): **74** In-Band Latency
  Tracing & Budget Attribution, **79** Crash Handling, Core Dumps & Post-Mortem Diagnostics, **81**
  Latency-Spike Forensics & Anomaly-Triggered Diagnostics, and **Appendix N** Incident Diagnostics
  Bundle. Renumber map from the post-P3-1 numbering: 1–73 unchanged; old 74–77 → 75–78; old 78 → 80;
  old 79–81 → 82–84. Composite map from the Phase-3 (pre-P3-1) numbering: 1–45 unchanged; 46–54 →
  47–55; 55–71 → 57–73; 72 → 76; 73 → 77; 74 → 78; 75 → 80; 76 → 84. Also added: §2.4.4 (beyond the
  core PMU), §5.4.4–§5.4.5 and the rr-limits block in §5.4.2, §20.4.4, §22.4.4, §41.4.3 (signals),
  §78.4.6 (divergence forensics; §78.4.5 reduced to sharding + a pointer to Ch. 79), §84.4.4 and a
  rewritten three-tier §84.2, Appendix I.10, Appendix D/H rows, the Appendix J.5 venue-timestamp table,
  the Appendix E signal-delivery row, Ch. 60 §60.4.2's exchange-timestamp paragraph. Part XIII order is
  now: logging → tracing → telemetry → security → hot reload → topology → crash → capture → spike
  forensics → host health → network health → capstone.
- **2026-11-09 languages insertion (work-queue item P3-3):** a new **Part XII — Beyond C++:
  Languages at the Latency Floor** was opened after Part XI with `insert_chapter.py --new-part`
  (which now also shifts every later Part's numeral, inserts the README/preface/CLAUDE.md Part
  entries and bumps the Part counts), followed by four ordinary insertions, one commit each:
  **73** Hand-Written & Inline Assembly, **74** Rust for the Hot Path, **75** Language Interop & ABI
  Boundaries, **76** Managed Runtimes Near the Hot Path, Measured, **77** Choosing a Language per
  Latency Tier. The former Part XII (Observability & Operations in Production) is now **Part XIII**.
  Renumber map from the post-P3-2 numbering: 1–72 unchanged; old 73–84 → 78–89. Composite map from
  the Phase-3 (pre-P3-1) numbering: 1–45 unchanged; 46–54 → 47–55; 55–70 → 57–72; 71 → 78;
  72 → 81; 73 → 82; 74 → 83; 75 → 85; 76 → 89. Also added:
  §85.4.6 (research-to-production: the C++ decoder/book bound into Python, buffer-protocol views,
  parity tests, Arrow interchange) and §85.4.7 (columnar tick storage: journal → Arrow/Parquet,
  kdb+/ClickHouse/QuestDB), a §85.3 measure-it row and a Ch. 89 §89.4.1 parity paragraph; Appendix B
  reduced to a two-page summary + tier table pointing at Part XII; Appendix L.5 (cross-language
  protocol pointer; L.5–L.6 → L.6–L.7); Appendix I.11 (other toolchains' asm and runtime-log
  one-liners; I.11–I.12 → I.12–I.13); Appendix E's C-ABI-call row; Appendix F/G/H entries.
- **2026-11-09 design-patterns insertion (work-queue item P3-4):** six chapters and one appendix were
  added with `insert_chapter.py` (six ascending insertions, one commit each — no new Part): **38**
  Threading Topologies & the Hot Loop and **39** Flow Control, Backpressure & Conflation on the Hot
  Path (Part VI, after the Disruptor), **44** Timers & Deadlines Without Jitter (Part VII, after
  context switching), **87** The Pre-Trade Risk Gate & Kill-Switch Path and **88** Idempotent Order
  Entry & Order-State Recovery (Part XIII, after process topology), **91** The Sequencer: Total-Order
  Broadcast & Replicated Deterministic Consumers (between capture and spike forensics), and
  **Appendix O** Hot-Path Anti-Pattern Catalogue. Renumber map from the post-P3-3 numbering: 1–37
  unchanged; old 38–41 → 40–43; old 42–83 → 45–86; old 84–85 → 89–90; old 86–89 → 92–95. Composite
  from the Phase-3 (pre-P3-1) numbering: 1–37 unchanged; 38–41 → 40–43; 42–45 → 45–48; 46–54 → 50–58;
  55–70 → 60–75; 71 → 81; 72 → 84; 73 → 85; 74 → 86; 75 → 90; 76 → 95. Part XIII order is now: logging
  → tracing → telemetry → security → hot reload → topology → risk gate → order entry → crash → capture
  → sequencer → spike forensics → host health → network health → capstone. Also added: Ch. 19
  §19.4.4 (strong types and units) and §19.4.5 (type-state and compile-time state machines) with the
  register-passing asm proof in §19.5 and the §19.4.3 static-DI/null-object bullet; Ch. 25 §25.4.4
  (slot maps and generational handles — the LOB case study is now §25.4.5 and adopts it) with a §25.3
  measurement row and §25.5 pitfalls; Appendix F (sequencer, replicated state machine, fencing token,
  conflation, hop, slot-ownership protocol, staleness budget, timer slack, timing wheel, slot map, RT
  throttle, strong type, type-state, static DI, pre-trade risk gate, in-flight order table,
  unknown-state matrix, journal-before-send, degraded mode; kill-switch/drop-copy/order gateway
  re-pointed), Appendix G (Schneider, Lamport, Défago et al., Varghese & Lauck, hrtimers, Helland,
  Gray & Reuter, SEDA/Kleinrock, 15c3-5/RTS 6/FIA, venue session specs; G.6 lines), Appendix I.6
  timer-slack/RT-throttle one-liners, Appendix J.6/K.4 and Ch. 1/27/84/86/90/95 pointers.
- **2026-11-09 modern-C++ insertion (work-queue item P3-5):** three chapters and one appendix were
  added with `insert_chapter.py` (three ascending insertions, one commit each — no new Part; the tool
  does not extend a Part's upper bound when inserting after its last chapter, so the Part III range was
  corrected to 18–25 by hand): **20** Compile-Time Protocol Decoders: From Schema to Code (after the
  templates chapter), **22** The Cost of Modern Idioms & Strings Without Allocation (after the
  abstractions chapter), **25** Build Systems, Build-Time Hygiene & Hermetic Toolchains (closing Part
  III), and **Appendix P** Attributes, Builtins & the UB Catalogue. Renumber map from the post-P3-4
  numbering: 1–19 unchanged; old 20–21 → 21, 23 (old 20 abstractions → 21, old 21 aliasing → 23); old 22
  (toolchain) → 24; old 23–95 → 26–98. Composite from the Phase-3 (pre-P3-1) numbering: 1–19 unchanged;
  20 → 21; 21 → 23; 22 → 24; 23–37 → 26–40; 38–41 → 43–46; 42–45 → 48–51; 46–54 → 53–61; 55–70 → 63–78;
  71 → 84; 72 → 87; 73 → 88; 74 → 89; 75 → 93; 76 → 98. Also added: Ch. 19 §19.4.6 (concepts and
  constrained overload sets — four dispatch mechanisms, identical asm, diagnostics compared), Ch. 21
  §21.4.5 (values, copy elision and the SysV calling convention) and §21.4.6 (statics, `thread_local`
  and initialization, measured) with Ch. 4 §4.2.1's classification paragraphs, Ch. 23 §23.2's
  object-lifetime paragraph and §23.4.2 renamed to include `std::start_lifetime_as` (with the
  `memmove`+`launder` C++20 polyfill, zero-instruction proof in §23.5), Ch. 14 §14.4.5 (`function_ref`,
  `move_only_function`, in-place functions, measured; §14.2.3 design space, §14.3 rows), Ch. 33 §33.4.3
  (`atomic_ref`, `wait`/`notify`, 16-byte CAS and the libatomic trap, measured; Ch. 37/57 `static_assert`
  discipline), Ch. 28 §28.4.3 renamed with `std::inplace_vector`, Ch. 32 §32.4.4 (`std::simd` and portable
  wrappers, measured); Appendix D pointers to P, Appendix F (25 entries), G (G.1/G.3/G.4 entries; G.6 Part
  III line), H (rows for concepts, class-type NTTPs, reflection, modules/`import std`, `musttail`,
  `atomic_ref`, DWCAS, `function_ref`, `move_only_function`, `start_lifetime_as`, `std::simd`,
  `inplace_vector`, `add_sat`), I.12 (build/toolchain/attribute/statics/TLS one-liners; I.12–I.13 →
  I.13–I.14), the README legend (98 chapters, A–P) and Part III intro, and the preface counts.
- **2026-11-09 trading/lifecycle insertion (work-queue item P3-6 batch A):** six chapters were added with
  `insert_chapter.py` (six ascending insertions, one commit each — no new Part; the tooling was first
  made three-digit-safe): **92** The Order-Entry Gateway & Session Layer (after order entry), **95**
  Line-Rate Capture, Timing-Faithful Replay & Passive Wire Measurement (after capture), **97** Failover
  Without Split-Brain: Leases, Fencing & Session Takeover on the Order Path (after the sequencer), and the
  engineering-lifecycle run before the capstone: **101** Testing Latency-Critical Code, **102** Performance
  Engineering in CI & the Profile Lifecycle, **103** Release Engineering & Measuring Changes in Production.
  Renumber map from the post-P3-5 numbering: 1–91 unchanged; old 92–93 → 93–94; old 94 → 96; old 95–97 →
  98–100; old 98 → 104. Composite from the Phase-3 numbering: 1–19 unchanged; 20 → 21; 21 → 23; 22 → 24;
  23–37 → 26–40; 38–41 → 43–46; 42–45 → 48–51; 46–54 → 53–61; 55–70 → 63–78; 71 → 84; 72 → 87; 73 → 88;
  74 → 89; 75 → 94; 76 → 104. Also added: Ch. 59 §59.4.4 (TCP failure detection: default ≈ 924 s vs
  `TCP_USER_TIMEOUT` 2.25 s vs keepalive 4.1 s, measured), Ch. 68 §68.4.4 (taps vs SPAN, timestamped
  mirrors, appliances), Ch. 89 §89.4.7 (checkpointing: fork/COW vs double buffer + dirty bitmap, the RTO
  curve) and §89.4.8 (segment reattach, epochs, torn-write recovery, robust mutexes), Ch. 24 §24.4.5 (the
  PGO/BOLT profile lifecycle), Ch. 104 §104.4.1 reduced to principles and new §104.4.5 (post-trade latency
  analytics), Appendix L §L.6 (benchmarks-as-tests CI skeleton; L.6–L.7 → L.7–L.8), Appendix D.6/I.8/I.9
  lines, Appendix F/G/J/K entries. Ch. 94 §94.4.1 reduced to a pointer paragraph. Part XIII order is now:
  logging → tracing → telemetry → security → hot reload → topology → risk gate → order entry → gateway →
  crash → capture → wire capture/replay → sequencer → failover → spike forensics → host health → network
  health → testing → CI → release → capstone. Left for later batches: the remaining item-19 gaps (G52–G58,
  G62, G64–G65, G68 and the rest), Appendix E rows (nothing derivable for the reference machine from the
  VM runs), Appendix O rows for the six chapters.
- When generating or revising a chapter, confirm scope against `chapters/README.md` (the
  authoritative post-reorg structure; the TOC above is the historical 64-chapter drafting list
  and has no entries for the twelve chapters added in Phase 3) and keep cross-references to
  other chapters by number/title.
- Benchmark output blocks in drafts are **representative** for the stated reference
  machine (authoring is on macOS; target is Linux x86-64); code/flags/`perf` commands are
  reproducible. Real measured runs must be backfilled before a chapter is final.
- Reference machine: every chapter that shows numbers uses a single-socket Xeon Gold 6326
  (Ice Lake-SP) — in Part II that is Ch. 7–15 and 17 — except the NUMA chapter (Ch. 16), which
  uses a **dual-socket 2× Xeon Gold 6326** because NUMA effects need two sockets. Keep the per-chapter machine description consistent with
  these unless a chapter needs otherwise.
  The full **reference toolchain and machine statement** (GCC 14.2 / Clang 18.1, Linux 6.18 LTS,
  `performance` governor with turbo off, ns↔cycles at 2.9 GHz = 0.345 ns/cycle) lives in **Appendix L.3**
  (one-paragraph version in the Preface); a §N.3 block either names those facts or cites "Appendix L.3".

### README.md is the reader's topic index (keep it current with every chapter)

`chapters/README.md` is a **detailed table of contents and topic index**, not a progress board.
It is organized by Part, and **each chapter is expanded into its standard section outline** so a
reader can find which document discusses a given technique:

```
### N. Chapter Title
- N.1 Why it matters: <hook>
- N.2 Mental model
  - N.2.1 <mechanism sub-topic>
  - N.2.2 …
- N.3 Measure it: <what is measured>
- N.4 Techniques
  - N.4.1 <pattern>
  - N.4.2 …
- N.5 Verify the codegen: <…>   (only for chapters where the optimizer matters)
- N.6 Pitfalls & anti-patterns: <…>
- N.7 Exercises & checklist
```

The searchable sub-topics live as numbered sub-bullets under **§N.2 (Mental model)** and
**§N.4 (Techniques)**; per-chapter §8 References / §9 Additional Reading are **not** listed in the
index. Appendices use the same outline at one level (`A.1`, `A.2`, …). A short legend at the top
of the file states the convention: a **linked** chapter title (`[N. Title](NN-slug.md)`) points at
its file. (Drafting is complete, so the earlier "plain title = planned, not yet written" half of the
convention is retired; every chapter and appendix is linked.) Drafting/revision status is tracked
**here in CLAUDE.md** (the Phase 2/3 status block under *Status / Workflow* above), NOT in README.

After generating (or materially revising) any chapter `chapters/NN-slug.md`, always, in the same
change:

1. **Refresh that chapter's README outline:**
   - Keep the chapter title linked to its file (`[N. Title](NN-slug.md)`); a new chapter gets a link.
   - Replace the outline with one that mirrors the chapter's *actual* sections — the
     `### N.2.x` (Mental model) and `### N.4.x` (Techniques) subsection titles map directly onto
     the README's `N.2.x` / `N.4.x` sub-bullets; keep the `N.1`/`N.3`/`N.5`/`N.6`/`N.7` lines in
     sync with the chapter's real one-line section summaries.
   - Verify the title and section numbering match the chapter.
2. **Update this file (CLAUDE.md):** if the change alters the book's shape or status (a new or
   removed chapter/appendix, a Part boundary, a revision milestone), update the Phase 2/3 status
   block under *Status / Workflow* — counts, Part ranges and the remaining-work note.
3. **Consistency sweep of the new chapter:** confirm it opens with the Part header +
   Prerequisites/Leads-into blockquote, follows the §1–§9 section structure, and ends with a
   `*Next: Ch. NN — …*` teaser pointing at the next chapter.

Treat this as part of "generating a chapter," not an optional follow-up — a chapter is not done
until its README outline reflects its real contents and CLAUDE.md's progress line is current.
