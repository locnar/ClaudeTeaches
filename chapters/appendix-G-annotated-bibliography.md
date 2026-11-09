# Appendix G — Annotated Bibliography

> The consolidated reading list behind the whole book — the per-chapter *References* and *Additional Reading* gathered, deduplicated, and annotated. Each entry says **what it's good for** and **which chapters/Parts draw on it**, so you can go deeper on any topic from one place. Organized by kind — foundational papers (G.1), vendor & reference manuals (G.2), books (G.3), influential talks & blogs (G.4), and tooling documentation (G.5) — and within each, roughly by where the book first leans on it.

This is a *map*, not a syllabus: you don't read it front to back. When a chapter sent you here, find the source it cited and use the annotation to decide whether it's the deep-dive you want. The handful of works marked **★** are the ones that repay reading in full for anyone serious about low-latency systems.

---

## G.1 Foundational papers

- **★ Ulrich Drepper, *What Every Programmer Should Know About Memory* (2007).** The definitive long-form treatment of caches, memory hierarchy, NUMA, and access patterns — dated in specifics, timeless in fundamentals. The intellectual backbone of **Part II** (Ch. 7–10) and the NUMA chapter (Ch. 16). If you read one thing from this list, read this.
- **★ Jeff Dean, "Numbers Everyone Should Know" / Peter Norvig's latency table.** The orders-of-magnitude latency intuition the whole book trades on; refreshed and trading-annotated in **Appendix E**. Underlies Ch. 1's "reason about the ratios."
- **★ Jeff Dean & Luiz André Barroso, "The Tail at Scale" (CACM 56(2), 2013).** The canonical argument that tail latency, not the mean, governs latency-critical systems, and why fan-out multiplies rare slowness — the distribution-first framing of **Ch. 1** and the p99/p99.9 discipline of Ch. 3 and 81.
- **★ The LMAX *Disruptor* technical paper (Thompson, Farley, Barker, Gee, Stewart, 2011).** The ring-buffer + sequence-barrier mechanical-sympathy design that anchors **Ch. 37**, and whose ideas recur in the lock-free queues (Ch. 34), logging (Ch. 73), the shared-memory IPC rings (Ch. 51), and the Aeron lineage (Ch. 67). Essential HFT-adjacent reading.
- **Paul McKenney, *Is Parallel Programming Hard, And, If So, What Can You Do About It?* and his RCU papers.** The authoritative source on RCU and safe memory reclamation — **Ch. 36**, with ties to seqlocks (Ch. 35) and hot reload (Ch. 76). Deep, rigorous, free online.
- **Leslie Lamport, "Time, Clocks, and the Ordering of Events in a Distributed System" (CACM 1978) and TLA+.** The happens-before idea beneath every memory-model argument in the book, and the TLA+ / model-checking resources that **Ch. 40** (§40.4.3) cites for checking lock-free designs — the one place a chapter leans on him directly.
- **Maranget, Sarkar & Sewell, *A Tutorial Introduction to the ARM and POWER Relaxed Memory Models*.** Why weakly-ordered hardware reorders, and how to reason about it — the reference behind **Appendix A.1** (and a sharp complement to Ch. 30).
- **Candea & Fox, "Crash-Only Software" (2003).** The crash-only design philosophy of **Ch. 77** — recovery as the only path, fast restart, fault containment.
- **Michael & Scott, "Simple, Fast, and Practical Non-Blocking and Blocking Concurrent Queue Algorithms" (PODC 1996).** The MS-queue — the reference unbounded lock-free queue and the baseline every bounded ring is measured against — **Ch. 34** (§34.4.1), with its ABA/reclamation consequences in Ch. 36 and the Disruptor's contrast in Ch. 37.
- **Hans Boehm & Sarita Adve, "Foundations of the C++ Concurrency Memory Model" (PLDI 2008)** and **Boehm, "Can Seqlocks Get Along with Programming Language Memory Models?"** The rationale and semantics behind C++11 atomics and data-race-freedom — **Ch. 30**; the seqlock paper is the definitive analysis of why a naive seqlock is a data race and how to write one the memory model accepts — **Ch. 35** (§35.5).
- **Maged Michael, "Hazard Pointers: Safe Memory Reclamation for Lock-Free Objects" (IEEE TPDS 15(6), 2004)** and **Keir Fraser, *Practical Lock-Freedom* (PhD thesis, University of Cambridge, 2004).** The two original safe-memory-reclamation schemes — per-pointer protection with bounded memory, and epoch-based reclamation with cheap readers but unbounded deferral — compared and measured in **Ch. 36** (§36.4.2–§36.4.3).
- **Thomas Anderson, "The Performance of Spin Lock Alternatives for Shared-Memory Multiprocessors" (IEEE TPDS 1990)** and **Mellor-Crummey & Scott, "Algorithms for Scalable Synchronization on Shared-Memory Multiprocessors" (ACM TOCS 1991).** TAS vs TTAS vs backoff vs queue locks, and the MCS lock that gives each waiter its own cache line — the measurements and techniques of **Ch. 32** (§32.3–§32.4).
- **David Goldberg, "What Every Computer Scientist Should Know About Floating-Point Arithmetic" (ACM Computing Surveys, 1991).** IEEE 754 representability, rounding and accumulated error — the floating-point half of **Ch. 27** (§27.2) and the case for scaled-integer prices.
- **Yang, Park & Ousterhout, "NanoLog: A Nanosecond Scale Logging System" (USENIX ATC 2018).** Compile-time format extraction, binary records and an offline decoder — the deferred/binary-logging design that **Ch. 73** builds (§73.2, §73.4.2).
- **The low-latency transport and congestion papers** — Zhu et al., "Congestion Control for Large-Scale RDMA Deployments" (**DCQCN**, SIGCOMM 2015); Kalia, Kaminsky & Andersen, "Datacenter RPCs can be General and Fast" (**eRPC**, NSDI 2019); Montazeri, Li, Alizadeh & Ousterhout, "Homa: A Receiver-Driven Low-Latency Transport Protocol Using Network Priorities" (SIGCOMM 2018). Why RoCEv2 needs ECN-driven rate control — **Ch. 66** (§66.2) — and what a µs-scale, head-of-line-free transport looks like beyond TCP — **Ch. 67** (§67.4.2–§67.4.3).
- **Gil Tene, "How NOT to Measure Latency" (and the coordinated-omission material).** Why naive latency measurement lies and how HdrHistogram fixes it — the measurement rigor of **Ch. 3** and the tail-latency framing of Ch. 1.

## G.2 Vendor & reference manuals

- **★ Intel® 64 and IA-32 Architectures Software Developer's Manual (SDM)** and the **Intel® 64 and IA-32 Optimization Reference Manual.** The authoritative x86-64 reference — instruction semantics, the memory model, PMU events, and microarchitectural optimization guidance. Cited across **Parts II-VI** (Ch. 7, 11–13, 28–30) and Appendix E. The optimization manual especially repays study.
- **★ Agner Fog's optimization manuals** (*Optimizing software in C++*, *The microarchitecture of Intel/AMD CPUs*, and the **instruction tables**). The practical bible of instruction latencies/throughputs and per-microarchitecture behavior — the source for "how many cycles does this cost" throughout **Ch. 11–14, 28–29**, Appendix D-E. The instruction tables are a daily-reference artifact.
- **AMD64 Architecture Programmer's Manual** and AMD optimization guides. The AMD counterpart to the Intel SDM — relevant wherever the book says "Intel/AMD" (Ch. 6, 7, 11), and for Zen-specific tuning (Appendix D).
- **Arm Architecture Reference Manual (ARMv8-A/ARMv9-A)** and the **Arm Neoverse / Ampere optimization guides.** The memory model, NEON/SVE, LSE atomics, `CNTVCT_EL0`, and `-mcpu` tuning behind **Appendix A**.
- **The Linux kernel documentation** — especially `Documentation/admin-guide/` (kernel parameters, `nohz_full`/`isolcpus`/RCU), the scheduler/RT docs, and the networking stack docs. The basis of **Part VII** (Ch. 41–47) and **Appendix C**.
- **The `perf` / `perf_events` documentation and the PMU event references.** How to drive the hardware counters and top-down analysis of **Ch. 2** (and the measurement methods of Appendix E).
- **The exchange protocol specifications** — Nasdaq **ITCH/OUCH**, the **FIX**, **SBE**, and **FAST** specs. The wire formats decoded in **Ch. 54**, glossaried in Appendix F.1 and given a field-level quick reference in Appendix J.5.
- **PCIe base specification (and vendor PCIe tuning notes).** The host-device boundary, posted/non-posted transactions, and round-trip cost of **Ch. 68** (and the offload Part XI).
- **Intel *Intrinsics Guide*.** The searchable per-intrinsic reference (semantics, CPUID flags, latency/throughput) behind the SSE/AVX/AVX-512 work of **Ch. 29**, the BMI/BMI2 tricks of Ch. 28 and the prefetch/streaming-store intrinsics of Ch. 10.
- **IEEE 1588 (Precision Time Protocol), including the 2019 revision (High Accuracy / White Rabbit profile).** The protocol behind cross-host timestamps and true wire-to-wire tick-to-trade — **Ch. 60** (§60.2, §60.4.3), with the NIC PHC of Ch. 57; tooling in `linuxptp` (G.5).
- **The RDMA verbs documentation** — `libibverbs`/`librdmacm` (rdma-core) and the `perftest` suite (`ib_*_lat`), the *RDMA Aware Networks Programming User Manual* (NVIDIA/Mellanox), the InfiniBand Architecture Specification and the RoCE v1/v2 standards. QPs, CQs, MRs and work requests, one-sided vs two-sided — **Ch. 65** (§65.2, §65.4), with UCX as the layer MPI runs on (Ch. 70).
- **NVIDIA *CUDA C++ Programming Guide* and *CUDA C++ Best Practices Guide*.** The execution/memory model, warps/occupancy, coalescing, streams, pinned/unified memory — **Ch. 69** (§69.2, §69.4) — and the PCIe transfer cost it shares with Ch. 68.
- **The MPI Standard (MPI Forum) and the Open MPI / MPICH / Intel MPI and UCX documentation.** Communicators, point-to-point, collectives, non-blocking and one-sided RMA, and the RDMA transport underneath — **Ch. 70** (§70.2, §70.4).
- **The FPGA toolchain documentation** — AMD/Xilinx Vivado and Vitis HLS, Intel/Altera Quartus. RTL vs HLS development, synthesis, place-and-route, partial reconfiguration — **Ch. 71** (§71.2, §71.4.2).
- **NVIDIA BlueField / DOCA SDK documentation and the P4 language specification (p4.org).** DPU architecture and the two programming models — C/C++ on the ARM cores vs declarative match/action pipelines — of **Ch. 72** (§72.2, §72.4.1).
- **Intel Resource Director Technology (RDT) documentation and the kernel's `resctrl` interface** (`Documentation/arch/x86/resctrl.rst`), with the `intel-cmt-cat` / `pqos` toolkit and the AMD64 QoS Extensions. CAT/MBA/CMT/MBM, CLOS and capacity masks — **Ch. 44** (§44.2, §44.4).
- **The market-access and timestamping rules** — SEC Rule 15c3-5 ("market access": pre-trade risk controls and the kill-switch) and MiFID II RTS 25 (clock synchronization traceable to UTC), with the Reg NMS / CAT record-keeping requirements. The compliance drivers behind the kill-switch of **Ch. 77** (§77.4.4), the PTP accuracy targets of Ch. 60 and the complete, timestamped capture of Ch. 78.

## G.3 Books

- **★ Hennessy & Patterson, *Computer Architecture: A Quantitative Approach* (6th ed., 2017).** The foundational architecture text — pipelines, caches, ILP, memory systems, multiprocessors. The "why" beneath **Part II** (Ch. 7–17) and the quantitative mindset of the whole book.
- **★ Anthony Williams, *C++ Concurrency in Action* (2nd ed.).** The definitive practical guide to the C++ memory model, atomics, and lock-free programming — **Part VI** (Ch. 30–40). The book to own for the concurrency chapters.
- **★ Maurice Herlihy & Nir Shavit, *The Art of Multiprocessor Programming* (2nd ed., with Luchangco & Spear).** The theory behind lock-free programming — linearizability, progress guarantees (obstruction-/lock-/wait-free), CAS, ABA and the memory-reclamation problem — **Ch. 34** (§34.2) and Ch. 36.
- **Agner Fog's optimization series** (also in G.2, as books) — *Optimizing software in C++* reads as a coherent text on the abstraction-cost and codegen concerns of **Part III** (Ch. 18–22).
- **Vandevoorde, Josuttis & Gregor, *C++ Templates: The Complete Guide* (2nd ed.)** and **Andrei Alexandrescu, *Modern C++ Design*.** The template instantiation/cost model, CRTP, expression templates and policy-based design of **Ch. 18–19**, and the small-object (Loki) allocator behind the pools of Ch. 24 (§24.4.2).
- **Fedor Pikus, *The Art of Writing Efficient Programs* (Packt, 2021) and *Hands-On Design Patterns with C++* (2nd ed., Packt, 2023).** Modern, practical performance-oriented C++ — measurement, memory, concurrency and the design patterns beneath zero-cost abstractions. A general companion to Ch. 3, 23–25 and 30–33 rather than a chapter-cited source.
- **Scott Meyers, *Effective Modern C++*** and **the C++ Core Guidelines.** Idiomatic modern C++ underpinning the language-mechanics Parts (Ch. 18–21) and the safety discipline of Ch. 75.
- **Brendan Gregg, *Systems Performance* (2nd ed.)** and ***BPF Performance Tools*.** The methodology of production performance analysis — the USE method, flame graphs, and the eBPF tooling of **Ch. 63** and the production-monitoring stance of **Ch. 81**.
- **Robert Love, *Linux System Programming*** and **Michael Kerrisk, *The Linux Programming Interface*.** The syscall-level reference behind **Part VIII** (Ch. 48, 49, 51, 52) and the memory/process chapters (Ch. 23, 26, 77).
- **Daniel Lemire's work and *Hacker's Delight* (Warren).** Bit-manipulation and branch-free integer tricks — **Ch. 28** (and the SWAR/SIMD parsing of Ch. 54).
- **Larry Harris, *Trading and Exchanges: Market Microstructure for Practitioners*.** The standard market-structure text — matching, order types, maker/taker, the participants' roles — behind **Appendix J** (J.1–J.3) and the trading framing of Ch. 25, 54 and the case studies; the FIX Trading Community specifications (G.2) cover the wire side.

## G.4 Influential talks & blogs

- **★ Carl Cook, "When a Microsecond Is an Eternity" (CppCon 2017).** The canonical HFT-C++ talk — keeping the hot path warm, branch-free, allocation-free, and measured. Touches Ch. 1, 13, 23–24, 47, and the end-to-end mindset of **Ch. 81**.
- **The Mechanical Sympathy blog and mailing list (Martin Thompson et al.).** The community that articulated "mechanical sympathy" — cache-friendliness, the Disruptor, false sharing, and lock-free design. Underlies **Ch. 7–8, 33, 37**.
- **CppCon / C++Now performance talks** — e.g. Chandler Carruth on optimization and codegen ("Tuning C++," "There Are No Zero-Cost Abstractions"), Fedor Pikus on lock-free, Ben Deane & Jason Turner ("constexpr ALL the Things!", C++Now/CppCon 2017) and Hana Dusíková (compile-time regex) on `constexpr`, Timur Doumler on hardware-aware real-time C++. Spread across **Parts III-VI**.
- **Matt Godbolt and the Compiler Explorer ecosystem.** Godbolt's talks and the **Compiler Explorer** tool itself are the workflow of **Ch. 4** (reading codegen) and recur wherever the book says "verify the asm."
- **Trading-tech engineering blogs** (Jane Street Tech, Optiver, Jump, and various HFT-practitioner write-ups) and the **Cloudflare / kernel-bypass blogs.** Real-world latency engineering, feed handling, and kernel-bypass experience — Ch. 54, 57, 60, 63, 64, 77–81.
- **Paul McKenney's LWN articles on RCU and the kernel memory model.** Accessible companions to his papers (G.1) — **Ch. 36** and Ch. 30.
- **Herb Sutter, "atomic<> Weapons" (C++ and Beyond 2012).** The definitive practical talk on the C++ memory model — acquire/release, `seq_cst`, and the x86-vs-weak-ordering distinction — **Ch. 30**, and the happens-before semantics TSan checks against (Ch. 40).
- **Jeff Preshing, *Preshing on Programming*.** Lucid, example-driven posts on acquire/release, memory ordering, the ABA problem and lock-free patterns — the companion blog to **Ch. 30**, Ch. 34 and Ch. 35 (getting the seqlock fences right).
- **Dmitry Vyukov, *1024cores.net* (and the Relacy Race Detector).** Bounded MPMC queues, SPSC rings and lock-free design patterns — the practical source behind **Ch. 34** (§34.4.1), the ring foundations of Ch. 37 and the shared-memory rings of Ch. 51; Relacy is the model checker of Ch. 40 (§40.4.3).
- **Scott Meyers, "CPU Caches and Why You Care" (talk).** An accessible bridge from the cache hierarchy (Ch. 7) to layout decisions — **Ch. 8**.
- **Cliff Click (and others) on JVM internals, JIT and GC behaviour.** Why managed runtimes' tails look the way they do and what HFT Java shops do about it — **Appendix B** (B.3–B.4); cited there, not by a C++ chapter.
- **Brendan Gregg's blog** (flame graphs, eBPF, latency heatmaps) — the practical companion to his books (G.3) for **Ch. 2, 63, 81**.

## G.5 Tooling documentation

- **Google Benchmark.** The microbenchmark harness of **Ch. 3** — fixtures, statistical reporting, and the dead-code-elimination/`DoNotOptimize` pitfalls.
- **HdrHistogram.** Recording latency *distributions* without coordinated omission — the measurement substrate of **Ch. 1, 3, 81**.
- **Intel VTune Profiler** (and `perf`, in G.2). Microarchitecture and top-down analysis — **Ch. 2**, with the offline-on-replay workflow of Ch. 81.
- **`liburing` / io_uring documentation.** The async-I/O interface of **Ch. 49** and the off-hot-path capture writer of Ch. 78.
- **DPDK** (and poll-mode-driver docs) and the **Solarflare/Onload, ef_vi, ExaNIC/libexanic** SDKs. Kernel bypass and userspace networking — **Ch. 57, 64**.
- **bpftrace / BCC.** Low-overhead production tracing — the eBPF one-liners and tooling of **Ch. 63** and the continuous observability of Ch. 81.
- **The sanitizer documentation (ASan/UBSan/TSan/MSan)** and **libFuzzer / AFL++.** The correctness/security bench of **Ch. 40** and **Ch. 75** (and the CI gates of Ch. 81).
- **The LLVM/GCC documentation, ThinLTO, and BOLT.** The build-toolchain references behind **Ch. 22** and **Appendix D**.
- **`pahole` / `dwarves`.** Struct-layout inspection — the object-layout work of **Ch. 9**.
- **`linuxptp` (`ptp4l`, `phc2sys`, `pmc`) and the kernel timestamping docs** (`Documentation/networking/timestamping.rst`, `PTP_SYS_OFFSET`). Disciplining the NIC PHC to the grandmaster and bridging it to the TSC — the toolchain of **Ch. 60** (§60.4), with hardware timestamping on the NIC (Ch. 57).
- **`cyclictest` / `rt-tests` and the `hwlat` detector.** Jitter measurement and the verification pass of **Ch. 45** and **Appendix C.5**.

## G.6 How this maps to the Parts

For quick navigation, the heaviest sources per Part:

- **Part I (Foundations/Methodology):** Tene & HdrHistogram, Google Benchmark, `perf`/VTune, Dean's numbers, Godbolt/Compiler Explorer (Ch. 1–6).
- **Part II (Microarchitecture):** Drepper, Hennessy & Patterson, Agner Fog, the Intel SDM/optimization manual (Ch. 7–17).
- **Part III (Compile-time/Language):** Williams, Meyers, the Core Guidelines, Agner Fog's C++ manual, Compiler Explorer (Ch. 18–22).
- **Part IV (Memory):** Drepper, Kerrisk/Love, `std::pmr` and allocator literature (Ch. 23–26).
- **Part V (Numerics/SIMD):** *Hacker's Delight*, Lemire, Agner Fog's tables, the Intel intrinsics guide (Ch. 27–29).
- **Part VI (Concurrency):** ★ Williams, ★ McKenney, the Disruptor paper, Michael & Scott, the C++ memory-model references, and the `std::execution`/P2300 material (Ch. 30–40).
- **Part VII (OS/Scheduling):** the kernel RT docs, Intel RDT/`resctrl` docs, `cyclictest`, Gregg's *Systems Performance* (Ch. 41–47, Appendix C).
- **Part VIII (Kernel I/O, Sockets & Zero-Copy):** `liburing`, the kernel zero-copy docs, the socket/TCP references (Ch. 48–53).
- **Part IX (Market Data, NIC & Fabric):** the exchange protocol specs, NIC-vendor and DDIO material, `rte_flow`/flow-steering docs, the PTP/IEEE-1588 references, and low-latency switch/fabric guides (Ch. 54–62).
- **Part X (Kernel Bypass, RDMA & Transport):** bpftrace/XDP, DPDK, the Solarflare/ef_vi SDKs, the InfiniBand/RoCE and DCQCN references, and Aeron/eRPC/Homa (Ch. 63–67).
- **Part XI (Heterogeneous/Accelerators):** the PCIe spec, CUDA/MPI docs, the FPGA toolchain and HFT-FPGA literature, the BlueField/DOCA/P4 docs (Ch. 68–72).
- **Part XII (Observability/Operations):** NanoLog, the crash-only paper, the LMAX/event-sourcing material, Gregg's *BPF Performance Tools*, the regulatory record-keeping rules (Ch. 73–81).
- **Appendices:** the Arm ARM and Graviton guides (A), the Rust/Zig/JVM references (B), the kernel/vendor tuning guides (C), the GCC/Clang/BOLT docs (D), the latency-measurement tools (E), cppreference/the protocol specs (F), the libstdc++/libc++ status tables and cppreference compiler-support pages (H), the `perf`/`bpftrace`/`ethtool` man pages and tool docs (I), Harris and the exchange specs (J), exchange colocation/connectivity documentation (K), and the Google Benchmark / HdrHistogram docs (L).

## G.7 A short shelf

If you build a low-latency C++ shelf from this list, start here: **Drepper** (memory), **Hennessy & Patterson** (architecture), **Williams** (concurrency), **Agner Fog's manuals** (the cycle-level reality), the **Intel optimization manual** (x86 specifics), **Gregg's *Systems Performance*** (production methodology), the **LMAX Disruptor paper** and **Carl Cook's talk** (the HFT mindset), and **McKenney** (reclamation). Everything else in this book is a path between those landmarks and the specific nanosecond you're chasing.

---

*Next: Appendix H — Modern C++ (C++20/23/26) Feature Availability Matrix, a support table for the modern-library and language features this book reaches for (`std::expected`, `std::flat_map`/`flat_set`, `std::print`, `std::mdspan`, `std::span`, `std::bit_cast`, senders/receivers) — which GCC/libstdc++ and Clang/libc++ version each landed in, the feature-test macro to gate on, and the C++17/20 fallback.*
