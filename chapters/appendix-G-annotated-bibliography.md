# Appendix G — Annotated Bibliography

> The consolidated reading list behind the whole book — the per-chapter *References* and *Additional Reading* gathered, deduplicated, and annotated. Each entry says **what it's good for** and **which chapters/Parts draw on it**, so you can go deeper on any topic from one place. Organized by kind — foundational papers (G.1), vendor & reference manuals (G.2), books (G.3), influential talks & blogs (G.4), and tooling documentation (G.5) — and within each, roughly by where the book first leans on it.

This is a *map*, not a syllabus: you don't read it front to back. When a chapter sent you here, find the source it cited and use the annotation to decide whether it's the deep-dive you want. The handful of works marked **★** are the ones that repay reading in full for anyone serious about low-latency systems.

---

## G.1 Foundational papers

- **★ Ulrich Drepper, *What Every Programmer Should Know About Memory* (2007).** The definitive long-form treatment of caches, memory hierarchy, NUMA, and access patterns — dated in specifics, timeless in fundamentals. The intellectual backbone of **Part II** (Ch. 7–10) and the NUMA chapter (Ch. 16). If you read one thing from this list, read this.
- **★ Jeff Dean, "Numbers Everyone Should Know" / Peter Norvig's latency table.** The orders-of-magnitude latency intuition the whole book trades on; refreshed and trading-annotated in **Appendix E**. Underlies Ch. 1's "reason about the ratios."
- **★ The LMAX *Disruptor* technical paper (Thompson, Farley, Barker, Gee, Stewart, 2011).** The ring-buffer + sequence-barrier mechanical-sympathy design that anchors **Ch. 37**, and whose ideas recur in the lock-free queues (Ch. 34), logging (Ch. 71), capture (Ch. 75), and the deterministic-core/process-topology pattern (Ch. 74). Essential HFT-adjacent reading.
- **Paul McKenney, *Is Parallel Programming Hard, And, If So, What Can You Do About It?* and his RCU papers.** The authoritative source on RCU and safe memory reclamation — **Ch. 36**, with ties to seqlocks (Ch. 35) and hot reload (Ch. 73). Deep, rigorous, free online.
- **Leslie Lamport, "Time, Clocks, and the Ordering of Events" and the sequential-consistency papers.** The conceptual roots of the memory-model reasoning in **Ch. 30** and the distributed-time thinking in Ch. 58.
- **Maranget, Sarkar & Sewell, *A Tutorial Introduction to the ARM and POWER Relaxed Memory Models*.** Why weakly-ordered hardware reorders, and how to reason about it — the reference behind **Appendix A.1** (and a sharp complement to Ch. 30).
- **Candea & Fox, "Crash-Only Software" (2003).** The crash-only design philosophy of **Ch. 74** — recovery as the only path, fast restart, fault containment.
- **Cliff Click et al. on lock-free hash tables, and Michael & Scott's lock-free queue paper.** The classic lock-free-structure literature behind **Ch. 34** (and the ABA/hazard discussion of Ch. 36).
- **Gil Tene, "How NOT to Measure Latency" (and the coordinated-omission material).** Why naive latency measurement lies and how HdrHistogram fixes it — the measurement rigor of **Ch. 3** and the tail-latency framing of Ch. 1.

## G.2 Vendor & reference manuals

- **★ Intel® 64 and IA-32 Architectures Software Developer's Manual (SDM)** and the **Intel® 64 and IA-32 Optimization Reference Manual.** The authoritative x86-64 reference — instruction semantics, the memory model, PMU events, and microarchitectural optimization guidance. Cited across **Parts II-VI** (Ch. 7, 11–13, 28–30) and Appendix E. The optimization manual especially repays study.
- **★ Agner Fog's optimization manuals** (*Optimizing software in C++*, *The microarchitecture of Intel/AMD CPUs*, and the **instruction tables**). The practical bible of instruction latencies/throughputs and per-microarchitecture behavior — the source for "how many cycles does this cost" throughout **Ch. 11–14, 28–29**, Appendix D-E. The instruction tables are a daily-reference artifact.
- **AMD64 Architecture Programmer's Manual** and AMD optimization guides. The AMD counterpart to the Intel SDM — relevant wherever the book says "Intel/AMD" (Ch. 6, 7, 11), and for Zen-specific tuning (Appendix D).
- **Arm Architecture Reference Manual (ARMv8-A/ARMv9-A)** and the **Arm Neoverse / Ampere optimization guides.** The memory model, NEON/SVE, LSE atomics, `CNTVCT_EL0`, and `-mcpu` tuning behind **Appendix A**.
- **The Linux kernel documentation** — especially `Documentation/admin-guide/` (kernel parameters, `nohz_full`/`isolcpus`/RCU), the scheduler/RT docs, and the networking stack docs. The basis of **Part VII** (Ch. 41–46) and **Appendix C**.
- **The `perf` / `perf_events` documentation and the PMU event references.** How to drive the hardware counters and top-down analysis of **Ch. 2** (and the measurement methods of Appendix E).
- **The exchange protocol specifications** — Nasdaq **ITCH/OUCH**, the **FIX**, **SBE**, and **FAST** specs. The wire formats decoded in **Ch. 53** and glossaried in Appendix F.1.
- **PCIe base specification (and vendor PCIe tuning notes).** The host-device boundary, posted/non-posted transactions, and round-trip cost of **Ch. 66** (and the offload Parts IX).

## G.3 Books

- **★ Hennessy & Patterson, *Computer Architecture: A Quantitative Approach*.** The foundational architecture text — pipelines, caches, ILP, memory systems, multiprocessors. The "why" beneath **Parts II** (Ch. 7–15) and the quantitative mindset of the whole book.
- **★ Anthony Williams, *C++ Concurrency in Action* (2nd ed.).** The definitive practical guide to the C++ memory model, atomics, and lock-free programming — **Part VI** (Ch. 30–37). The book to own for the concurrency chapters.
- **Agner Fog's optimization series** (also in G.2, as books) — *Optimizing software in C++* reads as a coherent text on the abstraction-cost and codegen concerns of **Part III** (Ch. 18–22).
- **Fedor Pikus, *The Art of Writing Efficient Programs* and *C++ Optimization*.** Modern, practical low-latency C++ — concurrency, memory, and measurement — overlapping Ch. 3, 23–25, 30–33.
- **Scott Meyers, *Effective Modern C++*** and **the C++ Core Guidelines.** Idiomatic modern C++ underpinning the language-mechanics Parts (Ch. 18–21) and the safety discipline of Ch. 72.
- **Brendan Gregg, *Systems Performance* (2nd ed.)** and ***BPF Performance Tools*.** The methodology of production performance analysis — the USE method, flame graphs, and the eBPF tooling of **Ch. 61** and the production-monitoring stance of **Ch. 76**.
- **Robert Love, *Linux System Programming*** and **Michael Kerrisk, *The Linux Programming Interface*.** The syscall-level reference behind **Part VIII** (Ch. 47, 48, 50, 51) and the memory/process chapters (Ch. 23, 26, 74).
- **Daniel Lemire's work and *Hacker's Delight* (Warren).** Bit-manipulation and branch-free integer tricks — **Ch. 28** (and the SWAR/SIMD parsing of Ch. 53).
- **Mark Newman / the FIX-and-market-microstructure texts** and **Larry Harris, *Trading and Exchanges*.** Market structure and microstructure context for the trading framing throughout (Ch. 25, 53, and the case studies).

## G.4 Influential talks & blogs

- **★ Carl Cook, "When a Microsecond Is an Eternity" (CppCon 2017).** The canonical HFT-C++ talk — keeping the hot path warm, branch-free, allocation-free, and measured. Touches Ch. 1, 13, 23–24, 46, and the end-to-end mindset of **Ch. 76**.
- **The Mechanical Sympathy blog and mailing list (Martin Thompson et al.).** The community that articulated "mechanical sympathy" — cache-friendliness, the Disruptor, false sharing, and lock-free design. Underlies **Ch. 7–8, 33, 37**.
- **CppCon / C++Now performance talks** — e.g. Chandler Carruth on optimization and codegen ("Tuning C++," "There Are No Zero-Cost Abstractions"), Fedor Pikus on lock-free, Timur Doumler / Hana Dusíková on `constexpr`. Spread across **Parts III-VI**.
- **Carl Cook, Matt Godbolt, and the Compiler Explorer ecosystem.** Godbolt's talks and the **Compiler Explorer** tool itself are the workflow of **Ch. 4** (reading codegen) and recur wherever the book says "verify the asm."
- **Trading-tech engineering blogs** (Jane Street Tech, Optiver, Jump, and various HFT-practitioner write-ups) and the **Cloudflare / kernel-bypass blogs.** Real-world latency engineering, feed handling, and kernel-bypass experience — Ch. 53, 55, 58, 61, 62, 74–76.
- **Paul McKenney's LWN articles on RCU and the kernel memory model.** Accessible companions to his papers (G.1) — **Ch. 36** and Ch. 30.
- **Brendan Gregg's blog** (flame graphs, eBPF, latency heatmaps) — the practical companion to his books (G.3) for **Ch. 2, 61, 76**.

## G.5 Tooling documentation

- **Google Benchmark.** The microbenchmark harness of **Ch. 3** — fixtures, statistical reporting, and the dead-code-elimination/`DoNotOptimize` pitfalls.
- **HdrHistogram.** Recording latency *distributions* without coordinated omission — the measurement substrate of **Ch. 1, 3, 76**.
- **Intel VTune Profiler** (and `perf`, in G.2). Microarchitecture and top-down analysis — **Ch. 2**, with the offline-on-replay workflow of Ch. 76.
- **`liburing` / io_uring documentation.** The async-I/O interface of **Ch. 48** and the off-hot-path capture writer of Ch. 75.
- **DPDK** (and poll-mode-driver docs) and the **Solarflare/Onload, ef_vi, ExaNIC/libexanic** SDKs. Kernel bypass and userspace networking — **Ch. 55, 62**.
- **bpftrace / BCC.** Low-overhead production tracing — the eBPF one-liners and tooling of **Ch. 61** and the continuous observability of Ch. 76.
- **The sanitizer documentation (ASan/UBSan/TSan/MSan)** and **libFuzzer / AFL++.** The correctness/security bench of **Ch. 40** and **Ch. 72** (and the CI gates of Ch. 76).
- **The LLVM/GCC documentation, ThinLTO, and BOLT.** The build-toolchain references behind **Ch. 22** and **Appendix D**.
- **`pahole` / `dwarves`.** Struct-layout inspection — the object-layout work of **Ch. 9**.
- **`cyclictest` / `rt-tests` and the `hwlat` detector.** Jitter measurement and the verification pass of **Ch. 45** and **Appendix C.5**.

## G.6 How this maps to the Parts

For quick navigation, the heaviest sources per Part:

- **Part I (Foundations/Methodology):** Tene & HdrHistogram, Google Benchmark, `perf`/VTune, Dean's numbers, Godbolt/Compiler Explorer (Ch. 1–6).
- **Part II (Microarchitecture):** Drepper, Hennessy & Patterson, Agner Fog, the Intel SDM/optimization manual (Ch. 7–17).
- **Part III (Compile-time/Language):** Williams, Meyers, the Core Guidelines, Agner Fog's C++ manual, Compiler Explorer (Ch. 18–22).
- **Part IV (Memory):** Drepper, Kerrisk/Love, `std::pmr` and allocator literature (Ch. 23–26).
- **Part V (Numerics/SIMD):** *Hacker's Delight*, Lemire, Agner Fog's tables, the Intel intrinsics guide (Ch. 27–29).
- **Part VI (Concurrency):** ★ Williams, ★ McKenney, the Disruptor paper, Michael & Scott, the C++ memory-model references, and the `std::execution`/P2300 material (Ch. 30–40).
- **Part VII (OS/Scheduling):** the kernel RT docs, Intel RDT/`resctrl` docs, `cyclictest`, Gregg's *Systems Performance* (Ch. 41–46, Appendix C).
- **Part VIII (Kernel I/O, Sockets & Zero-Copy):** `liburing`, the kernel zero-copy docs, the socket/TCP references (Ch. 47–52).
- **Part IX (Market Data, NIC & Fabric):** the exchange protocol specs, NIC-vendor and DDIO material, `rte_flow`/flow-steering docs, the PTP/IEEE-1588 references, and low-latency switch/fabric guides (Ch. 53–60).
- **Part X (Kernel Bypass, RDMA & Transport):** bpftrace/XDP, DPDK, the Solarflare/ef_vi SDKs, the InfiniBand/RoCE and DCQCN references, and Aeron/eRPC/Homa (Ch. 61–65).
- **Part XI (Heterogeneous/Accelerators):** the PCIe spec, CUDA/MPI docs, the FPGA toolchain and HFT-FPGA literature, the BlueField/DOCA/P4 docs (Ch. 66–70).
- **Part XII (Observability/Operations):** NanoLog, the crash-only paper, the LMAX/event-sourcing material, Gregg's *BPF Performance Tools*, the regulatory record-keeping rules (Ch. 71–76).
- **Appendices:** the Arm ARM and Graviton guides (A), the Rust/Zig/JVM references (B), the kernel/vendor tuning guides (C), the GCC/Clang/BOLT docs (D), the latency-measurement tools (E), and cppreference/the protocol specs (F).

## G.7 A short shelf

If you build a low-latency C++ shelf from this list, start here: **Drepper** (memory), **Hennessy & Patterson** (architecture), **Williams** (concurrency), **Agner Fog's manuals** (the cycle-level reality), the **Intel optimization manual** (x86 specifics), **Gregg's *Systems Performance*** (production methodology), the **LMAX Disruptor paper** and **Carl Cook's talk** (the HFT mindset), and **McKenney** (reclamation). Everything else in this book is a path between those landmarks and the specific nanosecond you're chasing.

---

*Next: Appendix H — C++23/26 Feature Availability Matrix, a support table for the modern-library and language features this book reaches for (`std::expected`, `std::flat_map`/`flat_set`, `std::print`, `std::mdspan`, `std::span`, `std::bit_cast`, senders/receivers) — which GCC/libstdc++ and Clang/libc++ version each landed in, the feature-test macro to gate on, and the C++17/20 fallback.*
