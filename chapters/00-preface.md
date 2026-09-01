# Preface

## Low-Latency C++ on Linux — A Tutorial Series

This is a book about the last nanoseconds. Most software-performance writing is about throughput — how many requests per second, how many gigabytes per second, how to make the average fast. This book is about the opposite discipline: making the *slowest* response fast, making latency *predictable*, and shaving a path that is already measured in microseconds down to hundreds — sometimes tens — of nanoseconds. It is written for the domain where that obsession is rational and the money is real: **high-frequency trading and electronic markets**, where the time from a market-data packet hitting your network card to your order leaving it — the **tick-to-trade** path — is the product, and where the *tail* of that time, not its mean, determines whether you win the trade or eat the loss.

The techniques are general — caches, branches, memory layout, lock-free concurrency, kernel bypass, and the rest are the same physics whatever you build on top of them — but they are *motivated* throughout by trading: order books, market-data feeds, latency budgets, the microburst at the open, the kill-switch on failure. If you work in another latency-sensitive domain (real-time audio, robotics, game engines, telecom, HPC), almost everything here transfers; you will simply substitute your own scenarios for the order books.

## Who this is for

This book assumes you are already a competent C++ programmer and comfortable on Linux. It is aimed at **intermediate-to-advanced C++ developers** who know the language (C++17/20, RAII, templates, the standard library) and the basics of how a Linux system works (processes, threads, syscalls, the shell), and who now want to understand *what the machine is actually doing* and how to bend it to a latency budget. It does **not** teach C++ or Linux fundamentals — the page budget is spent on performance depth, not on the basics. If you have written multithreaded C++, used a profiler at least once, and can read a stack trace, you are ready.

You do **not** need a background in finance. The trading concepts you need — what an order book is, what tick-to-trade means, what a feed handler does — are introduced as they come up and gathered in the glossary (**Appendix F**). The trading framing is there to make the techniques concrete and to set honest latency targets, not to assume you are already an HFT engineer.

## The north-star: tail latency, not average throughput

One idea runs through every chapter, and it is worth stating before anything else: **reason about the distribution, not the mean.** A path with a wonderful average latency and an occasional millisecond stall is, for trading, a *bad* path — that stall is a missed trade or an unhedged position, and it tends to arrive at the worst possible moment (the open, a volatility spike, a microburst). So the metric that matters is **tail latency** — p99, p99.9, the maximum — and the goal is not just *low* latency but *predictable, jitter-free* latency. Chapter 1 makes this case in full; the rest of the book is, in a sense, an extended answer to the question it raises: *where does the tail come from, and how do you cut it?*

A corollary shapes the book's method: **every performance claim is backed by a measurement.** There is no "this is faster" without a benchmark number, a `perf` counter, or the generated assembly to prove it. You will be taught to measure first (Chapter 2–3), to read the machine code (Chapter 4), and to distrust your intuition until the numbers confirm it. A flag, a technique, a data structure — each is a *hypothesis* until measured on your hardware.

## Environment and assumptions

The main chapters assume **modern x86-64** (recent Intel or AMD server parts), a **recent mainline Linux kernel**, and **GCC and/or Clang**. C++20 is the default dialect; the text calls out where C++17 differs and where C++23/26 features (`std::flat_map`/`std::flat_set`, `std::print`, `std::expected`, `std::span`, `std::mdspan`, `std::bit_cast`) sharpen an example, always naming the standard and the toolchain support. AArch64 — AWS Graviton, Ampere Altra — is treated deliberately in **Appendix A**, which ports the whole toolkit to ARM and names exactly what changes (chiefly the weaker memory model). Alternative languages (Rust, C, Zig, and the case against managed runtimes) are surveyed in **Appendix B**.

A recurring cast of tools appears throughout: `perf` and the Linux PMU, Intel VTune, Google Benchmark and HdrHistogram, Compiler Explorer (Godbolt), the sanitizers (ASan/UBSan/TSan/MSan), `numactl`/`taskset`/`cyclictest`, io_uring (`liburing`), and DPDK. You do not need all of them to start; each is introduced where it earns its place.

**About the benchmark numbers.** The measurements shown in the drafted chapters are **representative** — calibrated for the stated reference machines (a single-socket Xeon Gold 6326 / Ice Lake-SP for most microarchitecture chapters; a dual-socket box where NUMA effects require two sockets) — and the *code, compiler flags, and `perf` commands are reproducible*. They are there to teach the *ratios and the shape*, not to be quoted as gospel. The one number that should drive any decision you make is the one **you** measure on **your** hardware, with your build and your tuning. Treat the tables as intuition; treat your own box as the authority. **Appendix E** collects the order-of-magnitude latency numbers worth memorizing.

## How the book is organized

The seventy-six chapters are grouped into twelve Parts that build from the foundations upward, roughly following the path a packet takes and the stack it traverses:

- **Part I — Foundations & Methodology** (Ch. 1–6): the latency mindset, profiling, micro-benchmarking, reading assembly, debugging optimized code, and building a quiet machine. Start here; everything later assumes you can measure.
- **Part II — CPU Microarchitecture** (Ch. 7–17): caches, data-oriented design, object layout, prefetching, pipelines, the front-end, branch prediction, virtual dispatch, the TLB, NUMA, and timekeeping. The hardware reality under all the code.
- **Part III — Compile-Time & Language Mechanics** (Ch. 18–22): `constexpr`, templates and zero-cost abstractions, the cost of abstractions, aliasing, and the build toolchain.
- **Part IV — Memory Management** (Ch. 23–26): allocation cost, custom allocators, hot-path-hostile STL and cache-friendly containers (with the limit-order-book case study), and memory mapping.
- **Part V — Numerics & Data Parallelism** (Ch. 27–29): fixed/floating-point for prices, bit manipulation, and SIMD.
- **Part VI — Concurrency** (Ch. 30–40): the memory model and atomics, multithreading, spinlocks, false sharing, lock-free structures, seqlocks, safe reclamation, the Disruptor, coroutines, `std::execution`, and correctness tooling.
- **Part VII — OS, Scheduling & Isolation** (Ch. 41–46): context switching, pinning, SMT, cache allocation (Intel RDT), real-time scheduling, and keeping the hot path warm.
- **Part VIII — Kernel I/O, Sockets & Zero-Copy** (Ch. 47–52): native I/O, io_uring, the modern zero-copy fast path, IPC, socket tuning, and advanced TCP internals.
- **Part IX — Market Data, NIC & Fabric** (Ch. 53–60): zero-copy market-data decoding, reliable multicast and feed recovery, NIC offloads, DDIO, flow steering, PTP, precise transmission, and the network fabric.
- **Part X — Kernel Bypass, RDMA & Transport** (Ch. 61–65): eBPF/XDP, kernel bypass, RDMA, lossless Ethernet, and transports beyond TCP.
- **Part XI — Heterogeneous Computing & Hardware Acceleration** (Ch. 66–70): the PCIe boundary, GPUs, MPI, FPGAs, and SmartNICs/DPUs — and where each does (and does not) belong on the order path.
- **Part XII — Observability & Operations in Production** (Ch. 71–76): zero-overhead logging, secure programming, hot reload, process topology and the deterministic state machine, capture/replay, and a full end-to-end case study that ties the book together.

Seven appendices (**A-G**) are standing references that outlive any single chapter: the ARM port, the alternative-languages survey, a copy-paste system-tuning checklist, a compiler-flag reference, the latency-numbers table, a glossary, and an annotated bibliography. The detailed, searchable topic index — every chapter expanded into its sub-topics — is in [`README.md`](README.md).

## How each chapter is built

Every chapter follows the same rhythm, so you always know where to look:

1. **Why it matters** — the HFT latency problem the chapter solves.
2. **Mental model** — the hardware, OS, or language mechanism, with a diagram where it helps.
3. **Measure it** — the cost or effect, shown with a reproducible benchmark or `perf` run.
4. **Techniques** — concrete, idiomatic C++ patterns, before and after.
5. **Verify the codegen** — the generated assembly, for the chapters where the optimizer is the point (omitted where it isn't).
6. **Pitfalls & anti-patterns** — the traps, including correctness traps.
7. **Exercises & checklist** — practical takeaways to apply on your own system.
8. **References** and **9. Additional Reading** — where to go deeper.

Each chapter opens with a short blockquote naming its **prerequisites** and what it **leads into**, and closes with a one-line teaser for the next — so the book reads as a path, but any chapter can also be entered directly via the index. Cross-references are dense and by number ("Ch. 33", "§53.4.1") because the techniques genuinely interlock: false sharing (Ch. 33) shows up in your lock-free queue (Ch. 34), which feeds your logger (Ch. 71) and your capture journal (Ch. 75); the tick-to-trade budget of the final chapter (Ch. 76) is the sum of nearly every Part. Follow the references when a chapter leans on one — they are clickable, and they are the connective tissue of the whole.

## A note on trade-offs

There is no free lunch in this material, and the book does not pretend otherwise. Every technique here buys latency with *something* — complexity, portability, correctness risk, developer time, hardware cost, or all of them. Kernel bypass gives up the kernel's conveniences; a custom allocator gives up `malloc`'s generality; branchless code can be harder to read; an FPGA path costs months of specialized work. The chapters show the *cost* alongside the win, and they are explicit about the distinction between the **steady-state hot path** — where zero allocation, zero syscalls, and zero surprises are the law — and the **setup/teardown and control planes**, where ordinary, readable, productive code is not just acceptable but correct. The art of low-latency engineering is not applying every technique everywhere; it is knowing *which* nanoseconds are worth chasing, spending your complexity budget only on the hot path, and **measuring** to prove the chase paid off.

With that, turn to Chapter 1 — and start thinking in distributions.

---

*Next: Ch. 1 — The Latency Mindset, on why a microsecond can be the trade, why means lie, and how to think about the tick-to-trade budget as a distribution with a tail.*
