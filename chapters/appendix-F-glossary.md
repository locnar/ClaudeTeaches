# Appendix F — Glossary (HFT & Microarchitecture Terms)

> A quick reference for the acronyms and jargon used throughout the book. Each entry is a line or two with a pointer to the chapter that **defines and measures** it — follow the pointer for the real treatment; this is a reminder, not a substitute. Grouped by domain: trading (F.1), microarchitecture (F.2), memory & OS (F.3), concurrency (F.4), C++/toolchain (F.5), networking/I-O/transport (F.6), hardware acceleration & devices (F.7), and security/isolation/operations (F.8). Within each group, alphabetical.

---

## F.1 Trading & market-structure terms

- **A/B feed arbitration** — exchanges send market data on two redundant multicast lines (A and B); the feed handler takes whichever packet arrives first per sequence number and uses the other to fill gaps. (Ch. 53, 54)
- **BBO / Top-of-book** — Best Bid and Offer: the highest bid and lowest ask currently resting. The most-watched, most-updated level of the book. (Ch. 25)
- **Colocation (colo)** — placing your servers in the same datacenter as the exchange's matching engine to minimize wire distance (single-digit µs); physical proximity no software can substitute for. (Ch. 6, Appendix A.7)
- **Drop-copy** — a real-time copy of your order/fill activity fed to risk and compliance systems, separate from the order-entry session. (Ch. 74)
- **FAST** — FIX Adapted for STreaming: a compression encoding for market data (templates, implicit fields). (Ch. 53)
- **Feed handler** — the component that decodes the exchange's market-data feed off the wire, normalizes it, handles sequencing/gaps, and publishes a clean stream. (Ch. 53, 74)
- **FIX** — Financial Information eXchange: a tag-value text protocol for order entry and (FIX/FAST) market data. Verbose; often replaced on the hot path by binary encodings. (Ch. 53)
- **ITCH** — Nasdaq's binary market-data feed protocol (fixed-layout messages); the canonical zero-copy decode target. (Ch. 53)
- **Kill-switch** — a control that immediately cancels working orders and stops new ones to reach a safe state on failure; mandated by market-access risk rules (SEC Rule 15c3-5). Must live in a *surviving* process (the order gateway). (Ch. 72, 74)
- **Maker / Taker** — a *maker* posts resting liquidity (a limit order on the book); a *taker* removes it (a marketable order). Exchanges price the two differently (maker-taker fees). (Ch. 25)
- **Matching engine** — the exchange component that matches incoming buy/sell orders against the book by price-time priority; the thing you race to. (Ch. 1, 60)
- **Microburst** — a sudden spike of packets (classically at the market open) that overflows NIC/switch buffers and manufactures the latency tail. (Ch. 53, 60)
- **NBBO** — National Best Bid and Offer: the best bid/ask across all US venues; the consolidated top-of-book. (Ch. 25, 53)
- **Order book / Limit order book (LOB)** — the set of resting buy/sell orders organized by price level; the core data structure a strategy reads and a venue matches against. (Ch. 25)
- **Order gateway** — the process that owns the venue session (order-entry connection, sequence numbers, working-order state) and executes the kill-switch even when a strategy crashes. (Ch. 74)
- **OUCH** — Nasdaq's binary order-entry protocol (the order-entry counterpart to ITCH). (Ch. 53)
- **SBE** — Simple Binary Encoding: a fixed-layout binary message encoding (FIX community) designed for zero-copy, branch-free decode. (Ch. 53)
- **Sequence gap** — a discontinuity in a feed's sequence numbers signaling lost message(s); the trigger for recovery. (Ch. 54)
- **Snapshot / retransmission recovery** — refilling a feed gap from a full book image (snapshot) or a resend of specific missing messages (retransmission) — the recovery channels beyond A/B. (Ch. 54)
- **Tick-to-trade** — the end-to-end latency from a market-data packet arriving on the wire to the resulting order leaving on the wire; the book's north-star metric. (Ch. 1, 76)
- **Tick size** — the minimum price increment for an instrument; drives price representation (scaled integers) and book layout. (Ch. 27)

## F.2 Microarchitecture terms

- **BTB** — Branch Target Buffer: predicts the *target* of indirect branches/calls (vtables, function pointers — Ch. 14); a BTB miss is an indirect-branch mispredict. (Ch. 13, 14)
- **DSB / µop cache** — Decoded Stream Buffer: caches decoded µops so the front-end skips re-decoding hot code; staying in the DSB avoids legacy-decode front-end stalls. (Ch. 12)
- **HITM** — "Hit Modified": a cache load that hits a line *modified* in another core's cache, forcing a cross-core transfer — the signature of false sharing / contention (`perf c2c`). (Ch. 33)
- **IPC** — Instructions Per Cycle: throughput measure; low IPC signals stalls (cache/branch/front-end). The starting point of top-down analysis. (Ch. 2, 11)
- **LFB** — Line Fill Buffer (MSHR): tracks outstanding cache misses; its count bounds **MLP** (how many misses can be in flight). Running out of LFBs stalls further misses. (Ch. 7, 10)
- **MESI / MOESI** — cache-coherence protocols (Modified/Exclusive/Shared/Invalid, +Owned) that keep per-core caches consistent; the machinery behind HITM and false-sharing cost. (Ch. 7, 33)
- **MLP** — Memory-Level Parallelism: how many independent cache misses are serviced concurrently; high MLP hides DRAM latency (Ch. 7, 10 — prefetching/independent chains raise it). (Ch. 7, 10)
- **µop (micro-op)** — the internal RISC-like operation x86 instructions decode into; the unit the back-end actually executes. (Ch. 11, 12)
- **Prefetcher** — hardware that predicts and pre-loads cache lines ahead of demand; software prefetch (`__builtin_prefetch`) supplements it for irregular patterns. (Ch. 7, 10)
- **ROB** — Re-Order Buffer: holds in-flight instructions for out-of-order, speculative execution and in-order retirement; its size bounds the out-of-order window. (Ch. 11)
- **SMT / Hyper-Threading** — Simultaneous Multi-Threading: two hardware threads sharing one core's resources (front-end, execution ports, L1/L2); a throughput-vs-latency trade — HFT hot cores usually run the sibling idle or SMT off. (Ch. 43)
- **Store buffer** — a per-core buffer holding pending stores before they reach cache; the source of x86-TSO's one permitted reordering (store→load). (Ch. 30)
- **TLB** — Translation Lookaside Buffer: caches virtual→physical page translations; a TLB miss triggers a page-table walk (Ch. 15). Huge pages reduce TLB pressure. (Ch. 15)
- **Top-down analysis** — a PMU methodology attributing each cycle to front-end-bound / back-end-bound / bad-speculation / retiring, to find the dominant bottleneck. (Ch. 2)
- **µop cache** — see **DSB**.

## F.3 Memory & OS terms

- **cgroup** — Linux control groups: partition CPU/memory/I-O among process groups; used to confine housekeeping (and everything non-hot) off the isolated cores. (Ch. 45, Appendix C)
- **Futex** — Fast Userspace muTEX: the Linux primitive under mutexes/condvars; uncontended lock/unlock stays in userspace (fast), contention falls into a syscall to sleep/wake. (Ch. 32)
- **Huge pages** — larger page sizes (2 MiB, 1 GiB) that cover more memory per TLB entry, cutting TLB misses (Ch. 15); *explicit* (reserved) vs *transparent* (THP, kernel-managed, can add jitter). (Ch. 15, Appendix C)
- **`isolcpus`** — kernel cmdline parameter removing CPUs from the scheduler's general balancing so only explicitly-pinned threads run there; the foundation of a quiet core. (Ch. 45, Appendix C)
- **`mlockall`** — locks a process's pages resident in RAM so they're never swapped or paged out, eliminating page-fault stalls on the hot path. (Ch. 26)
- **`nohz_full`** — kernel feature stopping the periodic scheduler-tick interrupt on a core running a single task (tickless), removing that recurring jitter source. (Ch. 45, Appendix C)
- **NUMA** — Non-Uniform Memory Access: multi-socket systems where each CPU has faster access to its *local* memory node than to a remote node; node-local placement matters. (Ch. 16)
- **Page fault** — a trap when a touched virtual page isn't mapped/resident; *minor* (map an existing page) costs ~µs, *major* (from disk) far more — both forbidden on the hot path. (Ch. 23, 26)
- **`rcu_nocbs`** — kernel cmdline offloading RCU callback processing off specified cores, keeping grace-period work off the isolated hot cores. (Ch. 45, 36)
- **Swappiness / swap** — the kernel's willingness to page memory to disk; on a trading box, disabled entirely (a swapped hot page is a multi-ms fault). (Ch. 23, Appendix C)
- **THP** — Transparent Huge Pages: kernel-managed automatic huge pages; convenient but `khugepaged` compaction is a jitter source — often disabled in favor of explicit huge pages. (Ch. 15, Appendix C)

## F.4 Concurrency terms

- **ABA problem** — a lock-free hazard where a value reads as A, changes to B and back to A, fooling a CAS into thinking nothing changed; addressed with tagged pointers / hazard pointers / epochs. (Ch. 34, 36)
- **Acquire / Release** — memory-ordering constraints: an *acquire* load sees all writes that happened-before the matching *release* store; the one-way barriers that make lock-free publication correct (cheap on x86-TSO, real instructions `LDAR`/`STLR` on ARM). (Ch. 30, Appendix A.1)
- **Disruptor** — a high-performance ring-buffer + sequence-barrier pattern (LMAX) for single-producer/multi-consumer pipelines with batching and mechanical sympathy. (Ch. 37)
- **Epoch-based reclamation** — a safe-memory-reclamation scheme where readers enter an epoch and memory is freed only after all readers have advanced past it; lock-free, bounded. (Ch. 36)
- **False sharing** — two unrelated variables on the *same cache line* written by different cores, causing the line to ping-pong (HITM) as if they were shared; fixed by padding to `hardware_destructive_interference_size`. (Ch. 33)
- **Hazard pointer** — a safe-memory-reclamation scheme where readers publish the pointers they're using so a deleter knows what's unsafe to free yet. (Ch. 36)
- **Memory model / `memory_order`** — the rules (C++11+) governing how atomic operations on different threads become visible; `relaxed`/`acquire`/`release`/`acq_rel`/`seq_cst`. (Ch. 30)
- **RCU** — Read-Copy-Update: a synchronization scheme giving readers a lock-free fast path; writers publish a new version and reclaim the old after a grace period when no reader can hold it. (Ch. 36, 73)
- **Seqlock** — sequence lock: a single-writer/multi-reader scheme where the writer bumps a counter around writes and readers retry if the counter changed mid-read; tear-free publication of small snapshots. (Ch. 35, 73)
- **Single-writer principle** — assigning exactly one writer per data structure / memory segment so publication is a simple release store with no inter-writer coordination. (Ch. 35, 74)
- **SPSC / MPMC** — Single/Multi Producer, Single/Multi Consumer: the contention classes of lock-free queues/rings; SPSC is the cheapest (no inter-producer/consumer CAS). (Ch. 34)
- **TSO (Total Store Order)** — x86's relatively strong memory model (only store→load reordering); contrast ARM's weaker model where more reorderings are allowed and orderings cost real instructions. (Ch. 30, Appendix A.1)

## F.5 C++/toolchain terms

- **BOLT** — Binary Optimization and Layout Tool: a post-link optimizer that re-lays-out the final binary using a `perf` profile for I-cache/I-TLB locality. (Ch. 22, Appendix D)
- **Coroutine / HALO** — C++20 stackless coroutines (suspendable functions); HALO (Heap-Allocation eLision Optimization) elides the coroutine frame allocation when the compiler can prove the lifetime. (Ch. 38)
- **CRTP** — Curiously Recurring Template Pattern: static (compile-time) polymorphism via `template<class D> Base`, avoiding the virtual-call indirection of runtime dispatch. (Ch. 19, 14)
- **`constexpr` / `consteval`** — compile-time evaluation: `constexpr` *may* run at compile time, `consteval` *must*; moves work off the runtime hot path. (Ch. 18)
- **`std::execution` (senders / receivers / scheduler)** — the C++26 standard async model: lazily-composed **senders** describe async work, **receivers** are the typed continuation (value/error/stopped), and a **scheduler** places work on an execution context — a zero-cost structured-async abstraction. (Ch. 39)
- **`std::expected`** — a C++23 vocabulary type holding either a value or an error, for hot-path error handling without exceptions or sentinel codes. (Ch. 20)
- **FTZ / DAZ** — Flush-To-Zero / Denormals-Are-Zero: FPU modes treating subnormal floats as zero to avoid the 10-100× denormal-operation penalty; set explicitly on FP hot paths. (Ch. 27, Appendix D)
- **Heisenbug / observer effect** — a bug that changes or vanishes when you observe it, because a `printf`/breakpoint/heavy probe perturbs timing; debug via low-overhead capture and offline replay instead. (Ch. 5)
- **LTO** — Link-Time Optimization: optimizing across translation-unit boundaries at link (cross-module inlining, devirtualization); ThinLTO is the scalable variant. (Ch. 22, Appendix D)
- **`noexcept`** — a specifier promising a function won't throw; enables optimizations (no unwinding setup) and is required for some move-aware container fast paths. (Ch. 20)
- **PGO** — Profile-Guided Optimization: a build that uses a runtime profile (branch/hotness data) to drive inlining, branch layout, and code placement with data instead of heuristics. (Ch. 22, Appendix D)
- **PMR** — Polymorphic Memory Resources (`std::pmr`): standard-library support for custom allocators (arenas/pools) plugged into containers. (Ch. 24)
- **RAII** — Resource Acquisition Is Initialization: tying resource lifetime to object scope (constructor acquires, destructor releases); deterministic cleanup, no GC. (Ch. 23)
- **`restrict` / `__restrict__`** — a pointer qualifier promising no aliasing through it, letting the optimizer reorder/vectorize loads and stores it otherwise couldn't. (Ch. 21)
- **`rr`** — a record-and-replay debugger: records an execution once, then replays it deterministically forwards *and backwards* (reverse execution) — the reproduce-then-debug tool. (Ch. 5)
- **Sanitizer (ASan / UBSan / TSan / MSan)** — compiler instrumentation catching, respectively, memory errors, undefined behavior, data races, and uninitialized reads; a test/CI/fuzzing tool (2-3× cost), never production. (Ch. 40)
- **SoA / AoS** — Structure-of-Arrays vs Array-of-Structures: data layouts; SoA groups each field contiguously for cache/SIMD efficiency, AoS groups whole records. (Ch. 8, 29)
- **`std::span` / `std::mdspan`** — non-owning views over contiguous (1-D) / multi-dimensional data carrying their bounds; bounds-aware zero-copy access. (Ch. 53, 49)
- **Split debug info** — keeping `-g` debug info in a separate file (`-gsplit-dwarf` / `objcopy`) so a stripped production binary's cores and profiles stay symbolizable offline. (Ch. 5, Appendix D)
- **Structured concurrency** — async work with scope-bound lifetime and propagating cancellation (stop tokens) — RAII applied to concurrency; no dangling continuations. (Ch. 39)
- **Zero-cost abstraction** — an abstraction (templates, RAII, iterators) that compiles to the same code you'd write by hand — no runtime penalty for the abstraction. (Ch. 19, 20)

## F.6 Networking, I/O & Transport terms

- **Aeron / eRPC / Homa** — purpose-built low-latency transports: **Aeron** (reliable UDP/multicast/IPC messaging, LMAX lineage), **eRPC** (µs RPC on commodity NICs), **Homa** (receiver-driven, connectionless, HOL-free). (Ch. 65)
- **aRFS** — accelerated Receive Flow Steering: the kernel programs the NIC to steer each flow to the core its socket is consumed on, automatically. (Ch. 57)
- **Busy-poll / poll-mode** — spinning on a NIC/socket for new data instead of taking interrupts — removing interrupt and wakeup latency at the cost of a burned core. (Ch. 47, 62)
- **Cut-through / store-and-forward / layer-1** — switch types: **cut-through** starts forwarding after the header (frame-size-independent, sub-µs); **store-and-forward** receives the whole frame first (serialization delay, grows with size); **layer-1** is a physical cross-connect/replicator (sub-ns, no parsing). (Ch. 60)
- **DCQCN** — Data Center Quantized Congestion Notification: the RoCEv2 congestion-control algorithm that reacts to ECN marks by cutting rate, keeping buffers below the PFC threshold. (Ch. 64)
- **DDIO (Data Direct I/O)** — Intel feature that DMAs inbound NIC data straight into L3 (skipping DRAM) — a receive-latency win that can also pollute the hot working set. (Ch. 56)
- **devmem TCP** — kernel receive path that splits headers to the host and DMAs the payload directly into device memory (GPU/FPGA), never touching host DRAM. (Ch. 49)
- **DPDK** — Data Plane Development Kit: the vendor-neutral kernel-bypass framework (poll-mode drivers, hugepage mempools, userspace stacks). (Ch. 62)
- **eBPF** — extended Berkeley Packet Filter: a verified in-kernel VM for low-overhead production tracing (bpftrace) and programmable packet paths (XDP). (Ch. 61)
- **ECN** — Explicit Congestion Notification: a switch *marks* (rather than drops) a congested packet, signaling the sender to slow — the proactive layer of lossless Ethernet. (Ch. 64)
- **EDT (Earliest Departure Time)** — the Linux egress model where each packet carries a departure time; pacing and precise scheduling are both EDT settings. (Ch. 59)
- **ef_vi / Onload** — Solarflare/AMD kernel-bypass APIs: **ef_vi** is the raw, lowest-latency layer-2 API; **Onload** transparently accelerates existing socket apps (userspace TCP). (Ch. 62)
- **Flow steering (Flow Director / `rte_flow`)** — directing a *specific* flow to a *specific* NIC queue/core by exact-match rules, overriding RSS's random hash. (Ch. 57)
- **GRO / GSO / TSO / LRO** — receive/transmit segmentation offloads that batch segments for CPU efficiency at a latency cost; GRO/LRO are turned off on the hot RX path. (Ch. 52)
- **HOL blocking (head-of-line)** — TCP's ordered byte stream stalls *every* later message behind a single lost segment until it's retransmitted; message transports avoid it. (Ch. 65)
- **Incast** — many senders hitting one receiver simultaneously (a fan-in / scatter-gather), congesting the fabric and collapsing TCP throughput. (Ch. 64, 65)
- **io_uring** — the modern Linux async-I/O interface: submission/completion rings, registered/provided buffers, multishot, and zero-copy send/receive. (Ch. 48)
- **Kernel bypass** — mapping the NIC's rings into userspace and polling them, sending/receiving packets with no kernel, no syscalls, no copies, no interrupts. (Ch. 62)
- **LAUNCHTIME / `SO_TXTIME`** — NIC-hardware / socket mechanism to transmit a packet at an exact scheduled nanosecond, via the ETF qdisc. (Ch. 59)
- **`MSG_ZEROCOPY` / `TCP_ZEROCOPY_RECEIVE`** — kernel zero-copy send/receive: the NIC DMAs to/from *your* buffer, at the cost of asynchronous buffer-lifetime management (send) or page alignment (receive). (Ch. 49)
- **Nagle / delayed ACK** — TCP throughput heuristics whose interaction can inject a ~40 ms stall into small request-response flows; disabled with `TCP_NODELAY` / `TCP_QUICKACK`. (Ch. 52)
- **NAK** — Negative Acknowledgment: a receiver requesting retransmission of specific missing messages (reliable multicast); a *NAK storm* is many subscribers requesting at once. (Ch. 54)
- **One-sided RDMA** — a remote READ/WRITE that completes on the NIC without involving the remote CPU (vs two-sided send/recv). (Ch. 63)
- **Pacing** — spreading sends over time to a smooth rate (`SO_MAX_PACING_RATE` / FQ) to avoid self-inflicted microbursts. (Ch. 59)
- **PFC (Priority Flow Control)** — link-level pause frames (802.1Qbb) that make Ethernet lossless per traffic class; the backstop under RoCEv2, with head-of-line-blocking and deadlock hazards. (Ch. 64)
- **PGM** — Pragmatic General Multicast: a NAK-based reliable-multicast protocol; a model for feed recovery. (Ch. 54)
- **PMD (poll-mode driver)** — a userspace NIC driver that polls the RX ring instead of taking interrupts — the mechanism of kernel bypass. (Ch. 62)
- **PTP / PHC** — Precision Time Protocol (IEEE-1588): sub-µs clock synchronization across hosts via NIC hardware timestamps; the **PHC** is the PTP Hardware Clock. The basis of true wire-to-wire measurement. (Ch. 58)
- **`recvmmsg` / `sendmmsg`** — receive/send many datagrams in one syscall, amortizing the per-message syscall cost for high-rate UDP. (Ch. 57)
- **RoCEv2** — RDMA over Converged Ethernet: RDMA carried on an Ethernet fabric, requiring PFC/ECN/DCQCN to be lossless. (Ch. 63, 64)
- **RSS (Receive Side Scaling)** — the NIC hashes a flow tuple to spread packets across RX queues/cores — good for bulk load, bad for the hot path (random placement, feed splitting). (Ch. 55, 57)
- **Serialization delay** — the time to clock a frame onto the wire (frame_bits ÷ link_rate); why cut-through and high, uniform link speeds matter. (Ch. 60)
- **`SO_REUSEPORT`** — multiple sockets bound to one port with kernel/BPF load-balancing of flows across threads/cores. (Ch. 57)
- **TSN (Time-Sensitive Networking)** — IEEE 802.1 standards for scheduled, deterministic Ethernet (the time-aware shaper, 802.1Qbv). (Ch. 59)
- **Verbs / QP / CQ / MR** — the RDMA API: **Queue Pairs** (send/recv work queues), **Completion Queues**, and **Memory Regions** (registered/pinned memory the NIC may DMA). (Ch. 63)
- **White Rabbit / grandmaster** — sub-nanosecond clock distribution extending PTP; the *grandmaster* is the root clock, disciplined by GPS/PPS. (Ch. 58)
- **XDP (eXpress Data Path) / AF_XDP** — in-driver eBPF packet processing (drop/redirect/filter at the earliest hook); **AF_XDP** is a zero-copy socket for userspace fast paths — a middle ground between the kernel stack and full bypass. (Ch. 61)
- **Zero-copy** — the NIC DMAs to/from the *final* buffer, eliminating the intermediate kernel copy (`MSG_ZEROCOPY`, io_uring zero-copy, devmem TCP). (Ch. 49)

## F.7 Hardware Acceleration & Devices

- **BAR (Base Address Register)** — PCIe config that maps a device's registers/memory into the host address space for MMIO. (Ch. 66)
- **CUDA / warp / occupancy** — NVIDIA's GPU programming model; a **warp** is 32 threads executed in lockstep; **occupancy** is how fully the GPU's warp slots are used. (Ch. 67)
- **DMA (Direct Memory Access)** — a device reading/writing host memory without the CPU; the bulk-transfer mechanism behind NICs and accelerators. (Ch. 66)
- **DOCA / P4** — DPU programming models: **DOCA** (NVIDIA BlueField SDK, C/C++ on ARM cores), **P4** (a declarative packet-processing language for match/action pipelines). (Ch. 70)
- **DPU / SmartNIC** — a NIC with a programmable processor on it (ARM cores and/or FPGA) between wire and host, for offloading packet processing, RDMA, security, and even feed handling. (Ch. 70)
- **FPGA / HLS / RTL** — Field-Programmable Gate Array (configurable hardware, deterministic-nanosecond pipelines); described in **RTL** (Verilog/VHDL, gate-level, lowest latency) or **HLS** (C/C++ compiled to hardware, easier). (Ch. 69)
- **GPUDirect / P2P DMA** — peer-to-peer DMA directly between devices (NIC↔GPU/FPGA) bypassing host memory. (Ch. 66, 67)
- **IOMMU** — I/O Memory Management Unit: translates and protects device DMA addresses (a TLB for devices). (Ch. 66)
- **MMIO** — Memory-Mapped I/O: accessing device registers as memory; a non-posted *read* is a full PCIe round-trip. (Ch. 66)
- **MPI** — Message Passing Interface: the standard for distributed compute across cluster nodes (point-to-point + collectives), used for backtesting/risk. (Ch. 68)
- **MSI-X** — Message-Signaled Interrupts: a device signals a completion by writing memory (vs polling). (Ch. 66)
- **PCIe** — Peripheral Component Interconnect Express: the bus every NIC/GPU/FPGA sits behind; its round-trip latency is the floor under every offload decision. (Ch. 66)
- **Posted vs non-posted** — a *posted* write is fire-and-forget (~tens of ns); a *non-posted* read stalls for a full round-trip (~hundreds of ns) — why device-register reads are costly. (Ch. 66)

## F.8 Security, Isolation & Operations

- **ASLR / PIE** — Address Space Layout Randomization / Position-Independent Executable: load-time memory-safety hardening, ~free on the hot path. (Ch. 72)
- **CAT (Cache Allocation Technology) / RDT** — Intel Resource Director Technology partitions the shared L3 (and, via MBA, memory bandwidth) so a noisy neighbor can't evict the hot working set even when the core is pinned. (Ch. 44)
- **CET / CFI** — Control-flow Enforcement Technology (a hardware shadow stack) / Control-Flow Integrity: hardening against control-flow hijacking; CFI's cost tracks indirect-call density. (Ch. 72)
- **CLOS (Class of Service)** — an RDT group assigned cache ways (CAT) and/or a bandwidth throttle (MBA); cores or tasks are assigned to a CLOS. (Ch. 44)
- **CMT / MBM** — Cache Monitoring Technology / Memory Bandwidth Monitoring: per-group L3 occupancy and memory-bandwidth counters — the "see who's in the cache" half of RDT. (Ch. 44)
- **Crash-only design** — the only way to stop a process is to crash it, and the only way to start is to recover — so the recovery path is always tested and fast. (Ch. 74)
- **Deterministic state machine** — a pure, side-effect-free decision core (time and randomness passed *in* as inputs) that replays a captured input stream bit-for-bit; the basis of debugging, backtesting, and compliance. (Ch. 74)
- **`_FORTIFY_SOURCE`** — compile-time (and light runtime) bounds checks where the buffer size is known; usually free, kept on. (Ch. 72)
- **Hot reload** — changing config / strategy parameters / code in a *running* process without dropping a tick, via atomic pointer swap or seqlock/RCU-published snapshots and safe reclamation. (Ch. 73)
- **MBA (Memory Bandwidth Allocation)** — RDT throttling of a class's memory bandwidth, to stop a streaming neighbor (capture, logging) from saturating the memory controller. (Ch. 44)
- **`resctrl`** — the Linux pseudo-filesystem (`/sys/fs/resctrl`) interface to Intel RDT (CAT / MBA / CMT / MBM). (Ch. 44)
- **`seccomp`** — Linux syscall filtering; installed at the steady-state boundary it costs ~nothing (the hot path makes no syscalls) while containing a compromise. (Ch. 72)
- **Spectre / Meltdown** — speculative-execution vulnerabilities; the mitigations (retpoline, IBRS, `mitigations=`) carry a latency cost you may deliberately reduce on an isolated, single-tenant box. (Ch. 6, 72)
- **UB (Undefined Behavior)** — behavior the standard leaves undefined; in low-latency C++ it is simultaneously a correctness bug, a security vulnerability, and a source of nondeterminism (and the optimizer may delete your checks around it). (Ch. 21, 72)
- **WAL (Write-Ahead Log)** — append-only journaling: persist the event durably *before* acting on it; the capture-journal and replay model. (Ch. 75)

## F.9 References

- Each term's deriving chapter (cited inline) is the authoritative definition; this glossary only points there.
- The Intel SDM and Agner Fog's microarchitecture manual (Appendix G) — the microarchitecture terms of F.2.
- *C++ Concurrency in Action* (Williams) and cppreference — the concurrency (F.4) and C++/toolchain (F.5) terms.
- The exchange protocol specifications (Nasdaq ITCH/OUCH, the FIX/SBE/FAST specs) — the trading terms of F.1.
- The Linux kernel networking docs, DPDK/`liburing`/verbs documentation, and the DCQCN/PTP references (Appendix G) — the networking/transport terms of F.6.
- The PCIe spec, CUDA/MPI docs, FPGA toolchain and BlueField/DOCA references (Appendix G) — the hardware terms of F.7; the Intel RDT/`resctrl` and security-hardening docs — the terms of F.8.
- **Appendix G** (Annotated Bibliography) — the full sources behind every term; **Appendix E** — the *costs* of the F.2/F.3/F.6 phenomena (HITM, TLB miss, page fault, PCIe round-trip, RX latencies); **Appendix A** — the ARM-specific memory-model terms (`LDAR`/`STLR`, LSE, SVE).

## F.10 Additional Reading

- cppreference.com — the day-to-day reference for the C++/concurrency terms (F.4-F.5).
- The Mechanical Sympathy mailing list and blog — a community that coined/popularized much of the "mechanical sympathy" vocabulary (F.2, F.4).
- Vendor and community glossaries for the networking, RDMA, and accelerator terms (F.6-F.7) — NIC/switch vendors, the DPDK/kernel docs, and the RDMA/RoCE community — cross-checked against the standards.
- Wikipedia/vendor glossaries for the protocol and market-structure terms (F.1), cross-checked against the actual exchange specs.
- **Appendix G** — where each of these terms is developed at length in the cited literature.

---

*Next: Appendix G — Annotated Bibliography, a curated reading list consolidating the per-chapter References and Additional Reading — foundational papers, vendor/reference manuals, books, influential talks and blogs, and tooling docs — each with a sentence on what it's good for and which chapters draw on it, organized to mirror the Parts of the book.*
