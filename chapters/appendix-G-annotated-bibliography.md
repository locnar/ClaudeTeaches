# Appendix G — Annotated Bibliography

> The consolidated reading list behind the whole book — the per-chapter *References* and *Additional Reading* gathered, deduplicated, and annotated. Each entry says **what it's good for** and **which chapters/Parts draw on it**, so you can go deeper on any topic from one place. Organized by kind — foundational papers (G.1), vendor & reference manuals (G.2), books (G.3), influential talks & blogs (G.4), and tooling documentation (G.5) — and within each, roughly by where the book first leans on it.

This is a *map*, not a syllabus: you don't read it front to back. When a chapter sent you here, find the source it cited and use the annotation to decide whether it's the deep-dive you want. The handful of works marked **★** are the ones that repay reading in full for anyone serious about low-latency systems.

---

## G.1 Foundational papers

- **★ Ulrich Drepper, *What Every Programmer Should Know About Memory* (2007).** The definitive long-form treatment of caches, memory hierarchy, NUMA, and access patterns — dated in specifics, timeless in fundamentals. The intellectual backbone of **Part II** (Ch. 7–10) and the NUMA chapter (Ch. 16). If you read one thing from this list, read this.
- **★ Jeff Dean, "Numbers Everyone Should Know" / Peter Norvig's latency table.** The orders-of-magnitude latency intuition the whole book trades on; refreshed and trading-annotated in **Appendix E**. Underlies Ch. 1's "reason about the ratios."
- **★ Jeff Dean & Luiz André Barroso, "The Tail at Scale" (CACM 56(2), 2013).** The canonical argument that tail latency, not the mean, governs latency-critical systems, and why fan-out multiplies rare slowness — the distribution-first framing of **Ch. 1** and the p99/p99.9 discipline of Ch. 3 and 92.
- **★ The LMAX *Disruptor* technical paper (Thompson, Farley, Barker, Gee, Stewart, 2011).** The ring-buffer + sequence-barrier mechanical-sympathy design that anchors **Ch. 37**, and whose ideas recur in the lock-free queues (Ch. 34), logging (Ch. 81), the shared-memory IPC rings (Ch. 54), and the Aeron lineage (Ch. 70). Essential HFT-adjacent reading.
- **Paul McKenney, *Is Parallel Programming Hard, And, If So, What Can You Do About It?* and his RCU papers.** The authoritative source on RCU and safe memory reclamation — **Ch. 36**, with ties to seqlocks (Ch. 35) and hot reload (Ch. 85). Deep, rigorous, free online.
- **Leslie Lamport, "Time, Clocks, and the Ordering of Events in a Distributed System" (CACM 1978) and TLA+.** The happens-before idea beneath every memory-model argument in the book, and the TLA+ / model-checking resources that **Ch. 42** (§42.4.3) cites for checking lock-free designs — the one place a chapter leans on him directly.
- **Maranget, Sarkar & Sewell, *A Tutorial Introduction to the ARM and POWER Relaxed Memory Models*.** Why weakly-ordered hardware reorders, and how to reason about it — the reference behind **Appendix A.1** (and a sharp complement to Ch. 30).
- **Candea & Fox, "Crash-Only Software" (2003).** The crash-only design philosophy of **Ch. 86** — recovery as the only path, fast restart, fault containment.
- **Michael & Scott, "Simple, Fast, and Practical Non-Blocking and Blocking Concurrent Queue Algorithms" (PODC 1996).** The MS-queue — the reference unbounded lock-free queue and the baseline every bounded ring is measured against — **Ch. 34** (§34.4.1), with its ABA/reclamation consequences in Ch. 36 and the Disruptor's contrast in Ch. 37.
- **Hans Boehm & Sarita Adve, "Foundations of the C++ Concurrency Memory Model" (PLDI 2008)** and **Boehm, "Can Seqlocks Get Along with Programming Language Memory Models?"** The rationale and semantics behind C++11 atomics and data-race-freedom — **Ch. 30**; the seqlock paper is the definitive analysis of why a naive seqlock is a data race and how to write one the memory model accepts — **Ch. 35** (§35.5).
- **Maged Michael, "Hazard Pointers: Safe Memory Reclamation for Lock-Free Objects" (IEEE TPDS 15(6), 2004)** and **Keir Fraser, *Practical Lock-Freedom* (PhD thesis, University of Cambridge, 2004).** The two original safe-memory-reclamation schemes — per-pointer protection with bounded memory, and epoch-based reclamation with cheap readers but unbounded deferral — compared and measured in **Ch. 36** (§36.4.2–§36.4.3).
- **Thomas Anderson, "The Performance of Spin Lock Alternatives for Shared-Memory Multiprocessors" (IEEE TPDS 1990)** and **Mellor-Crummey & Scott, "Algorithms for Scalable Synchronization on Shared-Memory Multiprocessors" (ACM TOCS 1991).** TAS vs TTAS vs backoff vs queue locks, and the MCS lock that gives each waiter its own cache line — the measurements and techniques of **Ch. 32** (§32.3–§32.4).
- **David Goldberg, "What Every Computer Scientist Should Know About Floating-Point Arithmetic" (ACM Computing Surveys, 1991).** IEEE 754 representability, rounding and accumulated error — the floating-point half of **Ch. 27** (§27.2) and the case for scaled-integer prices.
- **Yang, Park & Ousterhout, "NanoLog: A Nanosecond Scale Logging System" (USENIX ATC 2018).** Compile-time format extraction, binary records and an offline decoder — the deferred/binary-logging design that **Ch. 81** builds (§81.2, §81.4.2).
- **The low-latency transport and congestion papers** — Zhu et al., "Congestion Control for Large-Scale RDMA Deployments" (**DCQCN**, SIGCOMM 2015); Kalia, Kaminsky & Andersen, "Datacenter RPCs can be General and Fast" (**eRPC**, NSDI 2019); Montazeri, Li, Alizadeh & Ousterhout, "Homa: A Receiver-Driven Low-Latency Transport Protocol Using Network Priorities" (SIGCOMM 2018). Why RoCEv2 needs ECN-driven rate control — **Ch. 69** (§69.2) — and what a µs-scale, head-of-line-free transport looks like beyond TCP — **Ch. 70** (§70.4.2–§70.4.3).
- **Gil Tene, "How NOT to Measure Latency" (and the coordinated-omission material).** Why naive latency measurement lies and how HdrHistogram fixes it — the measurement rigor of **Ch. 3** and the tail-latency framing of Ch. 1.
- **Daniel Bristot de Oliveira et al., "Demystifying the Real-Time Linux Scheduling Latency" (ECRTS 2020), and the osnoise/timerlat write-ups (Linux Plumbers 2021; LWN).** Latency as a sum of attributable intervals — the model the `osnoise` and `timerlat` tracers implement and that **Ch. 49** uses to name the source of the last spike on an isolated core.

## G.2 Vendor & reference manuals

- **★ Intel® 64 and IA-32 Architectures Software Developer's Manual (SDM)** and the **Intel® 64 and IA-32 Optimization Reference Manual.** The authoritative x86-64 reference — instruction semantics, the memory model, PMU events, and microarchitectural optimization guidance. Cited across **Parts II-VI** (Ch. 7, 11–13, 28–30) and Appendix E. The optimization manual especially repays study.
- **★ Agner Fog's optimization manuals** (*Optimizing software in C++*, *The microarchitecture of Intel/AMD CPUs*, and the **instruction tables**). The practical bible of instruction latencies/throughputs and per-microarchitecture behavior — the source for "how many cycles does this cost" throughout **Ch. 11–14, 28–29**, Appendix D-E. The instruction tables are a daily-reference artifact.
- **AMD64 Architecture Programmer's Manual** and AMD optimization guides. The AMD counterpart to the Intel SDM — relevant wherever the book says "Intel/AMD" (Ch. 6, 7, 11), and for Zen-specific tuning (Appendix D).
- **Arm Architecture Reference Manual (ARMv8-A/ARMv9-A)** and the **Arm Neoverse / Ampere optimization guides.** The memory model, NEON/SVE, LSE atomics, `CNTVCT_EL0`, and `-mcpu` tuning behind **Appendix A**.
- **The Linux kernel documentation** — especially `Documentation/admin-guide/` (kernel parameters, `nohz_full`/`isolcpus`/RCU), the scheduler/RT docs, and the networking stack docs. The basis of **Part VII** (Ch. 43–50) and **Appendix C**.
- **The `perf` / `perf_events` documentation and the PMU event references.** How to drive the hardware counters and top-down analysis of **Ch. 2** (and the measurement methods of Appendix E).
- **The exchange protocol specifications** — Nasdaq **ITCH/OUCH**, the **FIX**, **SBE**, and **FAST** specs. The wire formats decoded in **Ch. 57**, glossaried in Appendix F.1 and given a field-level quick reference in Appendix J.5.
- **PCIe base specification (and vendor PCIe tuning notes).** The host-device boundary, posted/non-posted transactions, and round-trip cost of **Ch. 71** (and the offload Part XI).
- **Intel *Intrinsics Guide*.** The searchable per-intrinsic reference (semantics, CPUID flags, latency/throughput) behind the SSE/AVX/AVX-512 work of **Ch. 29**, the BMI/BMI2 tricks of Ch. 28 and the prefetch/streaming-store intrinsics of Ch. 10.
- **IEEE 1588 (Precision Time Protocol), including the 2019 revision (High Accuracy / White Rabbit profile).** The protocol behind cross-host timestamps and true wire-to-wire tick-to-trade — **Ch. 63** (§63.2, §63.4.3), with the NIC PHC of Ch. 60; tooling in `linuxptp` (G.5).
- **The RDMA verbs documentation** — `libibverbs`/`librdmacm` (rdma-core) and the `perftest` suite (`ib_*_lat`), the *RDMA Aware Networks Programming User Manual* (NVIDIA/Mellanox), the InfiniBand Architecture Specification and the RoCE v1/v2 standards. QPs, CQs, MRs and work requests, one-sided vs two-sided — **Ch. 68** (§68.2, §68.4), with UCX as the layer MPI runs on (Ch. 73).
- **NVIDIA *CUDA C++ Programming Guide* and *CUDA C++ Best Practices Guide*.** The execution/memory model, warps/occupancy, coalescing, streams, pinned/unified memory — **Ch. 72** (§72.2, §72.4) — and the PCIe transfer cost it shares with Ch. 71.
- **The MPI Standard (MPI Forum) and the Open MPI / MPICH / Intel MPI and UCX documentation.** Communicators, point-to-point, collectives, non-blocking and one-sided RMA, and the RDMA transport underneath — **Ch. 73** (§73.2, §73.4).
- **The FPGA toolchain documentation** — AMD/Xilinx Vivado and Vitis HLS, Intel/Altera Quartus. RTL vs HLS development, synthesis, place-and-route, partial reconfiguration — **Ch. 74** (§74.2, §74.4.2).
- **NVIDIA BlueField / DOCA SDK documentation and the P4 language specification (p4.org).** DPU architecture and the two programming models — C/C++ on the ARM cores vs declarative match/action pipelines — of **Ch. 75** (§75.2, §75.4.1).
- **Intel Resource Director Technology (RDT) documentation and the kernel's `resctrl` interface** (`Documentation/arch/x86/resctrl.rst`), with the `intel-cmt-cat` / `pqos` toolkit and the AMD64 QoS Extensions. CAT/MBA/CMT/MBM, CLOS and capacity masks — **Ch. 47** (§47.2, §47.4).
- **The market-access and timestamping rules** — SEC Rule 15c3-5 ("market access": pre-trade risk controls and the kill-switch) and MiFID II RTS 25 (clock synchronization traceable to UTC), with the Reg NMS / CAT record-keeping requirements. The compliance drivers behind the kill-switch of **Ch. 86** (§86.4.4), the PTP accuracy targets of Ch. 63 and the complete, timestamped capture of Ch. 88.
- **RFC 2236 / 3376 (IGMPv2/v3), RFC 4541 (IGMP/MLD snooping switches), RFC 3678 (multicast source-filter socket API), RFC 7761 (PIM-SM).** The membership protocol, the querier requirement and the SSM socket API behind every market-data join — **Ch. 59**.
- **Linux kernel documentation: `kernel-per-CPU-kthreads.rst`, `osnoise-tracer.rst`/`timerlat-tracer.rst`/`hwlat_detector.rst`, `tools/rtla/`, `ftrace.rst`; `snmp_counter.rst`, `statistics.rst`, `scaling.rst`, `dropreason-core.h`; `proc.rst`, `psi.rst`, `ras.rst`, `pcieaer-howto.rst`.** The noise catalog and tracers of **Ch. 49**, the network counter semantics of **Ch. 91**, and the host-health signals of **Ch. 90**.
- **Prometheus documentation (histograms, exposition format, `histogram_quantile`, native histograms) and the OpenTelemetry exponential-histogram specification.** Why the unit of export is bucket counts and never a percentile — **Ch. 83 §83.4.3**.
- **SEC Rule 15c3-5 (Market Access) and ESMA MiFID II RTS 6.** Pre-trade controls, kill functionality, real-time monitoring and the annual self-assessment that a readiness gate and kill-switch test satisfy — **Appendix M**, Appendix K.4.
- **Exchange timestamp and protocol references: CME *MDP 3.0* and *iLink 3* documentation, Deutsche Börse *T7 EOBI* and *ETI* manuals, Nasdaq *TotalView-ITCH 5.0*, *OUCH 5.0* and *MoldUDP64* specifications.** The field definitions (`SendingTime`, `TransactTime`, `TrdRegTS*`, ITCH timestamps) behind the you/wire/matching-engine attribution of **Ch. 82 §82.4.5** and the table in Appendix J.5; also the decoders of Ch. 57.
- **Commission Delegated Regulation (EU) 2017/574 (MiFID II RTS 25).** The clock-accuracy tolerances (100 µs / 1 µs granularity for HFT-class activity) that bound how far a venue's timestamp can be trusted — **Ch. 82 §82.4.5, Ch. 63**; the record-keeping horizon of Appendix N.7 comes from RTS 6 / RTS 25.
- **Linux man pages for the crash and signal path: `signal-safety(7)`, `sigaction(2)`, `sigaltstack(2)`, `core(5)`, `madvise(2)`, `prctl(2)`, `backtrace(3)`; `fs/coredump.c` and `arch/x86/kernel/signal.c`.** The async-signal-safe function list, the `core_pattern`/`coredump_filter` semantics (including the pipe-ignores-`RLIMIT_CORE` rule) and the sigframe path — **Ch. 87, Ch. 43 §43.4.3**.
- **Intel SDM, Vol. 1 Ch. 13 (XSAVE) and Vol. 3 Ch. 33 (Processor Trace).** Why a signal frame is full-size even with clean vector state (**Ch. 87 §87.3**), and the packet/ToPA/PTWRITE mechanics behind Intel PT snapshots (**Ch. 2 §2.4.4, Ch. 89 §89.4.3**).
- **`perf-record(1)`, `perf-intel-pt(1)`, `perf-script(1)` and `Documentation/trace/ftrace.rst`.** Overwrite/snapshot/`--switch-output`/`--control` modes, `-k CLOCK_MONOTONIC`, `trace_clock x86-tsc`, per-CPU rings — the always-on-frozen-on-trigger recipes of **Ch. 89 §89.4.3** and the uncore/IBS/RAPL/`msr` event syntax of **Ch. 2 §2.4.4**; `perf_event_open(2)`'s mmap page for `rdpmc` (**Ch. 92 §92.4.4**).


- **GCC manual, *Extended Asm*, *Constraints for asm Operands*, *x86 Operand Modifiers*; Clang *Inline Assembly* extensions.** The normative contract behind every `asm` statement in the book — operands, clobbers, `volatile`, `asm goto` — **Ch. 76**.
- **System V ABI, AMD64 Processor Supplement; Itanium C++ ABI; Drepper, *How to Write Shared Libraries*.** Registers, stack, struct passing and return, name mangling, the PLT/GOT and symbol visibility — the C waist of **Ch. 78** and the hand-written functions of **Ch. 76**.
- **JEP 442/454 (Foreign Function & Memory API), JEP 439 (Generational ZGC), JEP 318 (Epsilon), JEP 483 (AOT Class Loading & Linking); the HotSpot GC Tuning Guide; the .NET GC/tiered-compilation/NativeAOT documentation; the Go GC Guide; the OCaml manual's runtime and C-interface chapters; the Rust Reference and rustc codegen options; the Zig language reference.** The primary documentation behind **Ch. 77–80**'s measurements and Appendix B's tables.

## G.3 Books

- **★ Hennessy & Patterson, *Computer Architecture: A Quantitative Approach* (6th ed., 2017).** The foundational architecture text — pipelines, caches, ILP, memory systems, multiprocessors. The "why" beneath **Part II** (Ch. 7–17) and the quantitative mindset of the whole book.
- **★ Anthony Williams, *C++ Concurrency in Action* (2nd ed.).** The definitive practical guide to the C++ memory model, atomics, and lock-free programming — **Part VI** (Ch. 30–42). The book to own for the concurrency chapters.
- **★ Maurice Herlihy & Nir Shavit, *The Art of Multiprocessor Programming* (2nd ed., with Luchangco & Spear).** The theory behind lock-free programming — linearizability, progress guarantees (obstruction-/lock-/wait-free), CAS, ABA and the memory-reclamation problem — **Ch. 34** (§34.2) and Ch. 36.
- **Agner Fog's optimization series** (also in G.2, as books) — *Optimizing software in C++* reads as a coherent text on the abstraction-cost and codegen concerns of **Part III** (Ch. 18–22).
- **Vandevoorde, Josuttis & Gregor, *C++ Templates: The Complete Guide* (2nd ed.)** and **Andrei Alexandrescu, *Modern C++ Design*.** The template instantiation/cost model, CRTP, expression templates and policy-based design of **Ch. 18–19**, and the small-object (Loki) allocator behind the pools of Ch. 24 (§24.4.2).
- **Fedor Pikus, *The Art of Writing Efficient Programs* (Packt, 2021) and *Hands-On Design Patterns with C++* (2nd ed., Packt, 2023).** Modern, practical performance-oriented C++ — measurement, memory, concurrency and the design patterns beneath zero-cost abstractions. A general companion to Ch. 3, 23–25 and 30–33 rather than a chapter-cited source.
- **Scott Meyers, *Effective Modern C++*** and **the C++ Core Guidelines.** Idiomatic modern C++ underpinning the language-mechanics Parts (Ch. 18–21) and the safety discipline of Ch. 84.
- **Brendan Gregg, *Systems Performance* (2nd ed.)** and ***BPF Performance Tools*.** The methodology of production performance analysis — the USE method, flame graphs, and the eBPF tooling of **Ch. 66** and the production-monitoring stance of **Ch. 92**.
- **Robert Love, *Linux System Programming*** and **Michael Kerrisk, *The Linux Programming Interface*.** The syscall-level reference behind **Part VIII** (Ch. 51, 52, 54, 55) and the memory/process chapters (Ch. 23, 26, 86).
- **Daniel Lemire's work and *Hacker's Delight* (Warren).** Bit-manipulation and branch-free integer tricks — **Ch. 28** (and the SWAR/SIMD parsing of Ch. 57).
- **Larry Harris, *Trading and Exchanges: Market Microstructure for Practitioners*.** The standard market-structure text — matching, order types, maker/taker, the participants' roles — behind **Appendix J** (J.1–J.3) and the trading framing of Ch. 25, 57 and the case studies; the FIX Trading Community specifications (G.2) cover the wire side.
- **Beyer, Jones, Petoff, Murphy (eds.), *Site Reliability Engineering* (2016) and *The Site Reliability Workbook* (2018).** SLOs, error budgets and multi-window burn-rate alerting — adapted to session phases in **Ch. 83 §83.4.4**; the production-readiness-review idea behind **Appendix M**.
- **Beau Williamson, *Developing IP Multicast Networks* (Cisco Press).** PIM, RPF and rendezvous points — the routed-multicast background of the venue distribution tree in **Ch. 59 §59.2.3**.
- **Raj Jain, *The Art of Computer Systems Performance Analysis* (1991).** Ch. 2's catalogue of measurement mistakes — comparing unconverted or mis-aligned measurements chief among them — is the methodological footing of the one-timeline rule in **Ch. 89 §89.2.2**.


- **Mara Bos, *Rust Atomics and Locks* (O'Reilly, 2023).** The Rust memory model and its x86/ARM codegen — the companion to Ch. 30 for **Ch. 77** (§77.2.3, §77.4.3).
- **Nicholas Nethercote, *The Rust Performance Book*; Jon Gjengset, *Rust for Rustaceans*.** Bounds-check elision, allocation avoidance, `unsafe` abstraction and FFI engineering — **Ch. 77**, **Ch. 78**.
- **Konrad Kokosa, *Pro .NET Memory Management*; Minsky, Madhavapeddy & Hickey, *Real World OCaml* (memory representation, GC chapters).** The .NET and OCaml runtimes at the depth **Ch. 79** (§79.4.4, §79.4.6) can only summarise.

## G.4 Influential talks & blogs

- **★ Carl Cook, "When a Microsecond Is an Eternity" (CppCon 2017).** The canonical HFT-C++ talk — keeping the hot path warm, branch-free, allocation-free, and measured. Touches Ch. 1, 13, 23–24, 50, and the end-to-end mindset of **Ch. 92**.
- **The Mechanical Sympathy blog and mailing list (Martin Thompson et al.).** The community that articulated "mechanical sympathy" — cache-friendliness, the Disruptor, false sharing, and lock-free design. Underlies **Ch. 7–8, 33, 37**.
- **CppCon / C++Now performance talks** — e.g. Chandler Carruth on optimization and codegen ("Tuning C++," "There Are No Zero-Cost Abstractions"), Fedor Pikus on lock-free, Ben Deane & Jason Turner ("constexpr ALL the Things!", C++Now/CppCon 2017) and Hana Dusíková (compile-time regex) on `constexpr`, Timur Doumler on hardware-aware real-time C++. Spread across **Parts III-VI**.
- **Matt Godbolt and the Compiler Explorer ecosystem.** Godbolt's talks and the **Compiler Explorer** tool itself are the workflow of **Ch. 4** (reading codegen) and recur wherever the book says "verify the asm."
- **Trading-tech engineering blogs** (Jane Street Tech, Optiver, Jump, and various HFT-practitioner write-ups) and the **Cloudflare / kernel-bypass blogs.** Real-world latency engineering, feed handling, and kernel-bypass experience — Ch. 57, 60, 63, 66, 67, 86–92.
- **Paul McKenney's LWN articles on RCU and the kernel memory model.** Accessible companions to his papers (G.1) — **Ch. 36** and Ch. 30.
- **Herb Sutter, "atomic<> Weapons" (C++ and Beyond 2012).** The definitive practical talk on the C++ memory model — acquire/release, `seq_cst`, and the x86-vs-weak-ordering distinction — **Ch. 30**, and the happens-before semantics TSan checks against (Ch. 42).
- **Jeff Preshing, *Preshing on Programming*.** Lucid, example-driven posts on acquire/release, memory ordering, the ABA problem and lock-free patterns — the companion blog to **Ch. 30**, Ch. 34 and Ch. 35 (getting the seqlock fences right).
- **Dmitry Vyukov, *1024cores.net* (and the Relacy Race Detector).** Bounded MPMC queues, SPSC rings and lock-free design patterns — the practical source behind **Ch. 34** (§34.4.1), the ring foundations of Ch. 37 and the shared-memory rings of Ch. 54; Relacy is the model checker of Ch. 42 (§42.4.3).
- **Scott Meyers, "CPU Caches and Why You Care" (talk).** An accessible bridge from the cache hierarchy (Ch. 7) to layout decisions — **Ch. 8**.
- **Cliff Click (and others) on JVM internals, JIT and GC behaviour.** Why managed runtimes' tails look the way they do and what HFT Java shops do about it — **Ch. 79** (§79.2.3, §79.5) and Appendix B; cited there, not by a C++ chapter.
- **Gil Tene, "How NOT to Measure Latency" (Strange Loop 2015).** The hiccup method, coordinated omission and why a sleeping meter sees what the application sees — **Ch. 79 §79.3**, Ch. 3.
- **Stefanus Du Toit, "Hourglass Interfaces for C++ APIs" (CppCon 2014).** The pattern and its name — **Ch. 78 §78.2.1**.
- **Dave Cheney, "cgo is not Go"; Austin Clements, non-cooperative goroutine preemption proposal.** Why a Go→C call costs 50 ns and how Go preempts a hot loop — **Ch. 78 §78.3**, **Ch. 79 §79.4.5**.
- **Brendan Gregg's blog** (flame graphs, eBPF, latency heatmaps) — the practical companion to his books (G.3) for **Ch. 2, 66, 92**.
- **Josh Poimboeuf and others, LWN coverage of SFrame and deferred user-space unwinding (2023–2025).** Where stack capture without frame pointers or DWARF is heading in the kernel — **Ch. 22 §22.4.4, Ch. 87 §87.4.2**.
- **Steven Rostedt, "Finding Origins of Latencies Using Ftrace" (Linux Plumbers / ELC).** The stop-on-threshold technique that Ch. 49's recipe and **Ch. 89**'s frozen kernel ring build on.
- **Brendan Gregg, "Linux Performance Analysis in 60,000 Milliseconds" (Netflix TechBlog).** The triage-order idea behind Appendix N.6's ten-minute first look and **Ch. 89 §89.4.4**'s cheap-exonerations-first table.


## G.5 Tooling documentation

- **Google Benchmark.** The microbenchmark harness of **Ch. 3** — fixtures, statistical reporting, and the dead-code-elimination/`DoNotOptimize` pitfalls.
- **HdrHistogram.** Recording latency *distributions* without coordinated omission — the measurement substrate of **Ch. 1, 3, 92**.
- **Intel VTune Profiler** (and `perf`, in G.2). Microarchitecture and top-down analysis — **Ch. 2**, with the offline-on-replay workflow of Ch. 92.
- **`liburing` / io_uring documentation.** The async-I/O interface of **Ch. 52** and the off-hot-path capture writer of Ch. 88.
- **DPDK** (and poll-mode-driver docs) and the **Solarflare/Onload, ef_vi, ExaNIC/libexanic** SDKs. Kernel bypass and userspace networking — **Ch. 60, 67**.
- **bpftrace / BCC.** Low-overhead production tracing — the eBPF one-liners and tooling of **Ch. 66** and the continuous observability of Ch. 92.
- **The sanitizer documentation (ASan/UBSan/TSan/MSan)** and **libFuzzer / AFL++.** The correctness/security bench of **Ch. 42** and **Ch. 84** (and the CI gates of Ch. 92).
- **The LLVM/GCC documentation, ThinLTO, and BOLT.** The build-toolchain references behind **Ch. 22** and **Appendix D**.
- **`pahole` / `dwarves`.** Struct-layout inspection — the object-layout work of **Ch. 9**.
- **`linuxptp` (`ptp4l`, `phc2sys`, `pmc`) and the kernel timestamping docs** (`Documentation/networking/timestamping.rst`, `PTP_SYS_OFFSET`). Disciplining the NIC PHC to the grandmaster and bridging it to the TSC — the toolchain of **Ch. 63** (§63.4), with hardware timestamping on the NIC (Ch. 60).
- **`cyclictest` / `rt-tests` and the `hwlat` detector.** Jitter measurement and the verification pass of **Ch. 48** and **Appendix C.5**.
- **`rtla` (`osnoise`, `timerlat`, `hwnoise`), `trace-cmd` and KernelShark (Steven Rostedt), `perf sched`.** The stop-on-threshold noise hunt of **Ch. 49** and the one-liners of Appendix I.6.
- **`turbostat`, `numastat`, `rasdaemon`/`ras-mc-ctl`, `dmidecode`; `ethtool` (`-S`, `-m`, `--show-fec`), `nstat`, `ss -ti`, `dropwatch`; the driver statistics sources (`ixgbe`/`ice`/`mlx5`/`sfc` `*ethtool*.c`).** The host- and network-health samplers of **Ch. 90–91** and the readiness attestation of **Appendix M**.
- **Google Breakpad and Chromium Crashpad documentation; `systemd-coredump(8)`, `coredumpctl(1)`; elfutils `eu-stack(1)`/`eu-unstrip(1)`; libunwind.** Minidumps and out-of-process crash handlers, systemd's core store and its size limits, stack extraction from cores without gdb, and the local unwinder's signal-handler caveats — **Ch. 87 §87.4.2–§87.4.5**.
- **The GDB manual — *Non-Stop Mode*, *Debugging Programs with Multiple Threads*, the *Python API* (pretty-printers) — and `gdbserver`.** Looking at one housekeeping thread of a live process without stopping the hot one — **Ch. 5 §5.4.4**.
- **Jane Street's *magic-trace* and Andi Kleen's *simple-pt*.** Application-triggered Intel PT snapshot tracing and practical PT recipes — the open-source relatives of **Ch. 89 §89.4.3**.
- **`tshark(1)` and the pcapng specification (`if_tsresol`).** The tap-side half of the per-packet ledger — **Ch. 82 §82.4.6**.


- **rustc `--emit asm` / `cargo-show-asm`; `zig build-obj -femit-asm`; HotSpot `-XX:+PrintCompilation`/`-XX:+PrintAssembly` with hsdis and Chris Newland's JITWatch; `DOTNET_JitDisasm`; `go tool objdump` and `-gcflags=-m`/`-S`/`-d=ssa/check_bce`; `ocamlopt -S`.** The "verify the codegen" step for every toolchain in Part XII — **Ch. 77 §77.5**, **Ch. 79 §79.5**, **Ch. 80 §80.2.3**, Appendix I.11.
- **JMH, BenchmarkDotNet, Criterion.rs, Go `testing.B`, `core_bench`.** The per-language microbenchmark harnesses and their `Blackhole`/`Consumer`/`black_box` sinks — **Ch. 80 §80.2.3**, Appendix L.5.
- **nanobind and pybind11; `bindgen`/`cbindgen`/`cxx`; `zig translate-c`; `jextract`; libabigail (`abidiff`).** Binding generators and ABI diffing for the waist — **Ch. 78 §78.4.6**, **Ch. 88 §88.4.6**.
- **Agrona, Chronicle Queue, Aeron; jHiccup.** The garbage-free Java toolkit and the pause meter — **Ch. 79 §79.4.1**, §79.3.
- **Apache Arrow / Parquet (pyarrow), DuckDB, Polars; kdb+, ClickHouse, QuestDB documentation.** Columnar tick storage and the research tier — **Ch. 88 §88.4.7**.

## G.6 How this maps to the Parts

For quick navigation, the heaviest sources per Part:

- **Part I (Foundations/Methodology):** Tene & HdrHistogram, Google Benchmark, `perf`/VTune, Dean's numbers, Godbolt/Compiler Explorer (Ch. 1–6).
- **Part II (Microarchitecture):** Drepper, Hennessy & Patterson, Agner Fog, the Intel SDM/optimization manual (Ch. 7–17).
- **Part III (Compile-time/Language):** Williams, Meyers, the Core Guidelines, Agner Fog's C++ manual, Compiler Explorer (Ch. 18–22).
- **Part IV (Memory):** Drepper, Kerrisk/Love, `std::pmr` and allocator literature (Ch. 23–26).
- **Part V (Numerics/SIMD):** *Hacker's Delight*, Lemire, Agner Fog's tables, the Intel intrinsics guide (Ch. 27–29).
- **Part VI (Concurrency):** ★ Williams, ★ McKenney, the Disruptor paper, Michael & Scott, the C++ memory-model references, and the `std::execution`/P2300 material (Ch. 30–42).
- **Part VII (OS/Scheduling):** the kernel RT docs, Intel RDT/`resctrl` docs, `cyclictest`, `rtla`/`osnoise`/`timerlat` and the per-CPU-kthreads documentation, Gregg's *Systems Performance* (Ch. 43–50, Appendix C).
- **Part VIII (Kernel I/O, Sockets & Zero-Copy):** `liburing`, the kernel zero-copy docs, the socket/TCP references (Ch. 51–56).
- **Part IX (Market Data, NIC & Fabric):** the exchange protocol specs, NIC-vendor and DDIO material, `rte_flow`/flow-steering docs, the PTP/IEEE-1588 references, the IGMP/snooping/SSM RFCs, and low-latency switch/fabric guides (Ch. 57–65).
- **Part X (Kernel Bypass, RDMA & Transport):** bpftrace/XDP, DPDK, the Solarflare/ef_vi SDKs, the InfiniBand/RoCE and DCQCN references, and Aeron/eRPC/Homa (Ch. 66–70).
- **Part XI (Heterogeneous/Accelerators):** the PCIe spec, CUDA/MPI docs, the FPGA toolchain and HFT-FPGA literature, the BlueField/DOCA/P4 docs (Ch. 71–75).
- **Part XII (Beyond C++):** the GCC extended-asm manual, the SysV/Itanium ABIs and the Intel SDM's instruction references (Ch. 76); the Rust reference, Bos and Nethercote (Ch. 77); the ABI documents, JEP 442/454, the JNI/cgo/OCaml interface docs and nanobind/pybind11 (Ch. 78); Tene, the HotSpot/.NET/Go/OCaml GC documentation, JITWatch, Agrona/Chronicle/Aeron and Kokosa (Ch. 79); JMH/BenchmarkDotNet/Criterion and the compile-time sections of the three language references (Ch. 80) (Ch. 76–80).
- **Part XIII (Observability/Operations):** NanoLog, the crash-only paper, the LMAX/event-sourcing material, Gregg's *BPF Performance Tools*, Tene/HdrHistogram and the Prometheus histogram docs, the SRE books (SLOs and alerting), the kernel's counter and tracer documentation, the regulatory record-keeping and market-access rules; the venue timestamp specifications and RTS 25 (Ch. 82), the signal/core-dump man pages, the SDM's XSAVE chapter and Breakpad/Crashpad (Ch. 87), the ftrace/`perf` PT documentation, Rostedt and magic-trace (Ch. 89), the gdb non-stop/Python manual (Ch. 5 §5.4.4) (Ch. 81–92, Appendices M–N).
- **Appendices:** the Arm ARM and Graviton guides (A), the Rust/Zig/JVM references (B), the kernel/vendor tuning guides (C), the GCC/Clang/BOLT docs (D), the latency-measurement tools (E), cppreference/the protocol specs (F), the libstdc++/libc++ status tables and cppreference compiler-support pages (H), the `perf`/`bpftrace`/`ethtool` man pages and tool docs (I), Harris and the exchange specs (J), exchange colocation/connectivity documentation (K), and the Google Benchmark / HdrHistogram docs (L).

## G.7 A short shelf

If you build a low-latency C++ shelf from this list, start here: **Drepper** (memory), **Hennessy & Patterson** (architecture), **Williams** (concurrency), **Agner Fog's manuals** (the cycle-level reality), the **Intel optimization manual** (x86 specifics), **Gregg's *Systems Performance*** (production methodology), the **LMAX Disruptor paper** and **Carl Cook's talk** (the HFT mindset), and **McKenney** (reclamation). Everything else in this book is a path between those landmarks and the specific nanosecond you're chasing.

---

*Next: Appendix H — Modern C++ (C++20/23/26) Feature Availability Matrix, a support table for the modern-library and language features this book reaches for (`std::expected`, `std::flat_map`/`flat_set`, `std::print`, `std::mdspan`, `std::span`, `std::bit_cast`, senders/receivers) — which GCC/libstdc++ and Clang/libc++ version each landed in, the feature-test macro to gate on, and the C++17/20 fallback.*
