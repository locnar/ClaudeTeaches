# Part XI — Heterogeneous Computing & Hardware Acceleration

# Chapter 67 — GPU Computing with CUDA

> **Prerequisites:** Ch. 66 (PCIe — the host-device round-trip that keeps GPUs off the order path), Ch. 29 (SIMD — GPUs are SIMD taken to the extreme), Ch. 1 (latency vs throughput — GPUs are a throughput device), Ch. 23 (pinned memory — for fast transfers), Ch. 7–8 (memory/data layout — GPU memory coalescing).
>
> **Leads into:** Ch. 68 (MPI — multi-GPU/multi-node scaling), Ch. 66 (the PCIe boundary), Ch. 70 (SmartNICs/GPUDirect). Part XI's first concrete accelerator — the throughput offload for the *off-hot-path* compute (risk, pricing, ML), explicitly *not* the tick-to-trade path.

---

## 67.1 Why it matters: data-parallel offload (and why not the order path)

A GPU is a **throughput** machine of staggering scale: thousands of cores executing the same operation across thousands of data elements in parallel — SIMD (Ch. 29) taken to an extreme, with terabytes-per-second of memory bandwidth. For **data-parallel** work — the same computation applied independently across a huge dataset — a GPU can be 10-100× a CPU. In trading, that work *exists*, in abundance, just **not on the tick-to-trade hot path**: **risk** calculation across a whole portfolio, **options pricing** and Greeks across thousands of instruments, **Monte-Carlo** simulation (millions of paths), **backtesting** across years of history and parameter sweeps, and **ML inference/training** for signal models. These are big, parallel, latency-*insensitive* (they run periodically/in batch, with budgets in milliseconds-to-seconds, not nanoseconds) — the perfect GPU workload. So GPUs are a real and valuable tool in a trading firm — for the **analytical/research/risk** tier, run off the latency-critical path.

The "why **not** the order path" is the chapter's other half, and it follows directly from Ch. 66: the GPU is a **PCIe device**, so using it costs a **PCIe round-trip** (Ch. 66) — you must transfer data *to* the GPU, launch a kernel, and transfer the result *back*, and that round-trip is **microseconds** (the PCIe floor — Ch. 66) *plus* kernel-launch overhead *plus* the transfer time for the data. On a tick-to-trade path budgeted in *hundreds of nanoseconds*, a microsecond-plus PCIe round-trip is disqualifying — the GPU could compute the answer infinitely fast and you'd *still* lose on the transfer. GPUs are **throughput-optimized, latency-pessimal** for small operations: their strength (massive parallelism) requires *large* batches to fill, and feeding/draining them crosses PCIe. So the tick-to-trade decision (one message, latency-bound) is *never* a GPU; the GPU is for the *batch* analytics around it. This is the Ch. 1 latency-vs-throughput dichotomy and the Ch. 66 PCIe floor, made concrete in the first accelerator.

The discipline that makes GPU use *correct* — and the chapter's measurement theme — is to **measure end-to-end, including the transfer, not just the kernel time.** The classic mistake is to benchmark the GPU *kernel* (the compute) in isolation, see a 50× speedup, and conclude the GPU "wins" — while ignoring that the host↔device transfer (Ch. 66's bandwidth) and the PCIe round-trip dominate the *actual* end-to-end time. A computation that's 50× faster on the GPU kernel but spends 90% of its wall-clock transferring data may be *slower* than the CPU overall. The real win requires the work to be **big enough to amortize the transfer** (compute-bound, not transfer-bound) and ideally to **overlap transfer with compute** (§67.4.2) and keep data resident on the GPU across operations. This chapter explains the CUDA execution/memory model (§67.2), measures the end-to-end-vs-kernel-only gap (§67.3), details transfers/pinned-memory/streams/overlap and where GPUs pay off (§67.4), and warns about putting a GPU on the tick-to-trade path (§67.5). It's the throughput accelerator for the off-hot-path tier.

## 67.2 Mental model: CUDA execution & memory model; warps/occupancy

**The GPU — massive parallelism via the SIMT model.** A GPU has thousands of cores grouped into **Streaming Multiprocessors (SMs)**; CUDA programs them via the **SIMT (Single Instruction, Multiple Thread)** model:

```
   a KERNEL (your GPU function) launches a GRID of thousands/millions of THREADS:
     grid → BLOCKS (thread groups, scheduled to SMs) → threads
     threads execute in WARPS of 32 (a warp = 32 threads running the SAME instruction in lockstep — SIMD!)
   each thread has an index → computes on its own data element (data-parallel)

   host (CPU) ──PCIe(Ch.54)──► GPU: copy data → launch kernel → copy result back
```

- **Threads, blocks, warps.** You launch a **grid** of thread **blocks**; each block runs on an SM; threads execute in **warps of 32** — a warp is 32 threads running the *same* instruction in lockstep (SIMT = SIMD with a thread abstraction — Ch. 29). Massive parallelism = many warps across many SMs.
- **Occupancy.** How many warps an SM can keep *resident* (to hide latency — when one warp stalls on a memory access, the SM runs another, hiding the stall with parallelism). High **occupancy** (enough warps) is how the GPU hides its (high) memory latency — the GPU's version of ILP (Ch. 11). Limited by registers/shared-memory per thread.
- **Warp divergence (the GPU branch cost — Ch. 13).** Within a warp, if threads take *different* branches (data-dependent `if`), the warp executes *both* paths serially (masking off the inactive threads) — **divergence**, a big slowdown. The GPU analog of branch misprediction (Ch. 13): data-parallel code wants threads in a warp to take the *same* path. Minimize divergence (like minimizing branches — Ch. 13).

**The CUDA memory model — a hierarchy (like the CPU's, Ch. 7, but explicit):**

- **Global memory** — the GPU's main DRAM (large, high bandwidth ~TB/s, but high latency). **Coalesced access** matters enormously (Ch. 7–8): threads in a warp should access *contiguous* global memory (so the hardware combines them into few wide transactions) — the GPU version of cache-line/SoA layout (Ch. 8). Strided/scattered access wastes bandwidth (like the CPU, amplified).
- **Shared memory** — fast on-SM memory shared by a block's threads (a programmer-managed cache — Ch. 7) — used to stage data for reuse, avoiding repeated global-memory access. The key optimization for many kernels.
- **Registers** — per-thread, fastest; **constant/texture** memory — specialized caches.
- **Host↔device transfer (Ch. 66) — the bottleneck.** Data starts in host memory; you `cudaMemcpy` it to GPU global memory (over PCIe — Ch. 66), launch the kernel, and copy results back. **This transfer is the cost that dominates small offloads** (§67.3) — the PCIe floor of Ch. 66.

**Execution flow and its overheads:**

```
   1. cudaMemcpy host→device   (PCIe transfer — Ch.54, bandwidth-bound for big data)
   2. kernel<<<grid,block>>>(...)   (launch overhead ~µs + the actual compute)
   3. cudaMemcpy device→host   (PCIe transfer back)
   → end-to-end = transfer + launch + compute + transfer  — NOT just the kernel (§55.3)
```

The model: **a GPU runs thousands of threads (in warps of 32, SIMT/SIMD) across many SMs — massive data-parallel throughput, hiding memory latency with occupancy. Memory is a hierarchy (global/shared/registers) with coalescing and divergence as the key perf levers (the GPU versions of cache layout — Ch. 8, and branches — Ch. 13). But it's a PCIe device (Ch. 66): every use costs a host↔device transfer + launch + transfer — so it's a *throughput* device for *big batch* work, measured end-to-end, and disqualified from the latency-bound order path by the PCIe round-trip.**

## 67.3 Measure it: end-to-end including transfer, not just kernel time

The decisive measurement — the one the classic mistake skips: **end-to-end time (transfer + launch + kernel + transfer) vs kernel-only**, across problem sizes. The kernel-only number flatters the GPU; the end-to-end number tells the truth about whether to offload.

```cpp
// gpu_e2e.cpp (CUDA) — measure kernel-only vs end-to-end (with transfer) for a vector op.
// Build: nvcc -O3 gpu_e2e.cpp -o gpu_e2e
__global__ void saxpy(float* y, const float* x, float a, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) y[i] = a * x[i] + y[i];           // data-parallel: one thread per element
}
// host: cudaMemcpy h2d (TIME IT), launch kernel (TIME IT), cudaMemcpy d2h (TIME IT)
//   end_to_end = h2d + launch + kernel + d2h     ← the real cost
//   kernel_only = kernel                          ← the flattering, misleading number
//   compare to a CPU SIMD version (Ch.28) for the same work.
```

Representative results — a mid-range GPU over PCIe Gen4 x16, vector op (illustrative; the *gap* is the point):

```
   problem size      CPU (SIMD, Ch.28)   GPU kernel-only   GPU end-to-end (w/ transfer)   verdict
   small (1K elems)  ~0.5 µs             ~2 µs (launch!)   ~10 µs (transfer + launch)     CPU wins (PCIe floor)
   medium (1M)       ~300 µs             ~20 µs            ~600 µs (transfer-bound)        ~tie / CPU (transfer)
   large (100M)      ~30 ms              ~2 ms             ~8 ms (transfer dominates)      GPU wins (amortized)
   large, DATA RESIDENT on GPU (no transfer per op)        ~2 ms                           GPU wins big

   the trap: GPU "kernel-only" is 15-50x faster — but END-TO-END, transfer dominates until the work is BIG.
```

Read it: the **kernel-only number is 15-50× faster and *misleading*** — the **end-to-end** number (including the PCIe transfer — Ch. 66) tells the real story. For **small** work, the GPU *loses badly*: the kernel-launch overhead (~µs) and PCIe round-trip (Ch. 66) dwarf the tiny compute — a 1K-element op is ~10 µs end-to-end on the GPU vs ~0.5 µs on the CPU. For **medium** work, the transfer dominates (transfer-bound — the data movement, not the compute) — often a tie or CPU win. Only for **large, compute-bound** work does the GPU win end-to-end (the compute saving exceeds the transfer cost) — and it wins *hugely* when **data stays resident on the GPU** across many operations (no per-op transfer — the last row). The lessons that govern GPU use: **(1) measure end-to-end, never kernel-only** (the classic mistake); **(2) the work must be big enough to amortize the PCIe transfer** (Ch. 66) — small ops belong on the CPU; **(3) keep data resident on the GPU** across operations to avoid repeated transfers (the biggest practical win); **(4) the latency-bound order path — small, single, nanosecond — is *never* a GPU** (§67.5). The GPU is for large batch analytics where the throughput swamps the transfer.

## 67.4 Techniques

### 67.4.1 Host–device transfer; pinned and unified memory

Minimizing and accelerating the transfer that dominates (§67.3, Ch. 66):

- **Pinned (page-locked) host memory (Ch. 23).** `cudaMemcpy` from *pageable* host memory is slow — the driver must stage it through a pinned bounce buffer. **Pinned memory** (`cudaHostAlloc`/`cudaMallocHost`) — page-locked (Ch. 23, like RDMA registration — Ch. 63) — DMA's directly, ~2× the transfer bandwidth, and enables *async* transfer (§67.4.2). Use pinned host buffers for all host↔device transfer. (Pinning is the Ch. 23 discipline; pin a bounded pool, reuse it.)
- **Minimize transfers — keep data resident.** The biggest win (§67.3): transfer data to the GPU *once* and run *many* operations on it without copying back, copying results back only at the end. Restructure pipelines so data stays GPU-resident across kernels — avoid the per-operation round-trip (Ch. 66). A sequence of GPU operations on resident data amortizes one transfer over all of them.
- **Unified memory (`cudaMallocManaged`).** A *single* pointer accessible from both host and device, with the driver migrating pages on demand — *convenient* (no explicit copies). But the migration is *implicit* (page faults move data — Ch. 23, at GPU scale) and can be *slower/less predictable* than explicit transfers if you don't prefetch/hint (`cudaMemPrefetchAsync`). Good for productivity/complex access patterns; for performance-critical transfers, explicit pinned transfers give control. Know which you're using.
- **GPUDirect (Ch. 66).** For network-fed GPU work (Ch. 68), **GPUDirect RDMA** (Ch. 66.4.3) DMA's network data *directly* into GPU memory — bypassing the host-memory bounce. Relevant for distributed/streaming GPU pipelines.
- **Right-size the transfer.** Many small transfers pay per-transfer overhead; **batch** into fewer large transfers (Ch. 37's batching, at the PCIe level). Transfer the whole batch, not element-by-element.

### 67.4.2 Streams and compute/transfer overlap

Hiding the transfer cost by overlapping it with compute (the key throughput technique):

- **CUDA streams — concurrent operation queues.** A **stream** is a queue of GPU operations (transfers, kernels) that execute in order; *different* streams can execute **concurrently**. By splitting work across streams, you overlap operations that would otherwise serialize.
- **Overlap transfer with compute.** The classic pattern: while the GPU *computes* on chunk N (kernel), simultaneously *transfer* chunk N+1 (h2d) and chunk N-1's result (d2h) — using multiple streams and **async** transfers (`cudaMemcpyAsync`, which requires *pinned* memory — §67.4.1). This **hides the transfer behind the compute** — so the end-to-end time approaches the compute time (instead of compute + transfer), closing the §67.3 gap. The GPU pipelines transfer and compute like the Disruptor pipelines stages (Ch. 37).
- **Overlap with the CPU.** Async kernel launches don't block the CPU — the CPU can do other work (or queue more GPU work) while the GPU computes. Keep both busy.
- **Multiple kernels concurrently.** Independent kernels in different streams can run concurrently on the GPU (if resources allow) — more parallelism. 
- **The result.** With overlap, a transfer-bound workload (§67.3) can become compute-bound — the transfer is hidden, so end-to-end ≈ kernel time. This is how you *get* the GPU's throughput on a streaming workload, and a key reason to keep enough work in flight to fill the pipeline.

### 67.4.3 Where GPUs pay off (risk, pricing/Greeks, Monte-Carlo, backtesting, ML inference)

The trading workloads that fit the GPU (big, parallel, latency-insensitive — off the hot path):

- **Risk calculation.** Portfolio risk (VaR, stress scenarios, sensitivities) across thousands of positions × scenarios — massively parallel, run periodically (intraday/overnight), latency budget in seconds. A canonical GPU win — independent computations across a large grid.
- **Options pricing & Greeks.** Pricing thousands of options (and their Greeks — delta, gamma, vega...) — each independent, the same model (Black-Scholes, binomial, PDE solvers) applied across instruments. Embarrassingly parallel; GPUs excel.
- **Monte-Carlo simulation.** Millions of independent random paths (path-dependent option pricing, risk scenarios) — the *ideal* GPU workload (massive independent parallelism, compute-bound — amortizes transfer). GPUs are dramatically faster for Monte-Carlo.
- **Backtesting & parameter sweeps.** Running a strategy across years of history × thousands of parameter combinations — parallelize across the parameter/scenario space. (Single-strategy sequential backtest is less parallel; the *sweep* is.) Large, batch, latency-insensitive — fits the GPU (and the cluster — Ch. 68).
- **ML inference/training.** Signal models, alpha research, market prediction — training (very GPU-heavy) and *batch* inference. (Latency-critical *online* inference on the hot path is a different story — small models on CPU, or FPGA — Ch. 69 — for the latency tier; GPUs for training and batch.)
- **The common thread.** All are **big, data-parallel, compute-bound, and latency-insensitive** (batch/periodic, not per-tick) — so they amortize the PCIe transfer (§67.3, Ch. 66) and exploit the GPU's throughput. The **research/risk/analytics tier**, run off the latency-critical path. *Not* the tick-to-trade path (§67.5).

## 67.5 Pitfalls & anti-patterns: PCIe latency on the tick-to-trade path

- **A GPU on the tick-to-trade path (the cardinal sin).** The latency-critical path (one message, hundreds of ns) **cannot** use a GPU — the PCIe round-trip (Ch. 66) + kernel launch + transfer is *microseconds*, disqualifying regardless of how fast the kernel is (§67.1, §67.3). The order path's compute stays on the CPU (or FPGA inline — Ch. 69). GPUs are for the *batch* tier only.
- **Measuring kernel-only, not end-to-end (the classic mistake).** Benchmarking the GPU kernel in isolation shows a flattering 15-50× speedup while hiding that the transfer (Ch. 66) dominates the *actual* time (§67.3). **Always measure end-to-end** (transfer + launch + kernel + transfer); the kernel-only number is misleading.
- **Too-small work / per-operation transfer.** Offloading work too small to amortize the transfer (§67.3) — or transferring data per operation instead of keeping it GPU-resident — makes the GPU *slower* than the CPU. Keep data resident, batch operations, only offload big work.
- **Pageable (un-pinned) host memory (Ch. 23).** Transferring from pageable memory is ~2× slower (staged through a bounce buffer) and can't be async. Use **pinned** memory (§67.4.1) for all transfers.
- **Warp divergence (Ch. 13).** Data-dependent branches within a warp serialize both paths (§67.2) — the GPU branch cost. Structure data-parallel code so threads in a warp take the same path (sort/partition data — Ch. 13, minimize divergence).
- **Uncoalesced global-memory access (Ch. 7–8).** Threads in a warp accessing scattered/strided global memory waste bandwidth (the GPU's cache-layout problem — Ch. 8, amplified). Lay out data so warp accesses are **coalesced** (contiguous) — SoA (Ch. 8) for the GPU.
- **Low occupancy.** Too few resident warps (high register/shared-memory use per thread) means the SM can't hide memory latency — low utilization. Tune occupancy (block size, register usage) — the GPU's latency-hiding (Ch. 11's ILP analog).
- **Unified memory used naively.** `cudaMallocManaged` is convenient but its implicit page migration (Ch. 23) can be slow/unpredictable without prefetch hints. For performance-critical paths, explicit pinned transfers; use unified memory knowingly (§67.4.1).
- **Not overlapping transfer and compute.** Leaving the transfer un-overlapped (§67.4.2) means end-to-end = transfer + compute (serial) instead of ≈ compute (overlapped) — leaving throughput on the table. Use streams + async + pinned to overlap.
- **GPU contention / sharing.** Multiple processes/jobs sharing one GPU contend (and context-switch on the GPU — expensive); for predictable batch performance, schedule GPU work deliberately (MPS, dedicated GPUs). Less critical (it's the batch tier) but real for throughput.

## 67.6 Exercises & checklist

**Exercises**

1. **Kernel-only vs end-to-end.** Build a CUDA vector op; measure kernel-only vs end-to-end (with transfer) across sizes from 1K to 100M elements (vs a CPU SIMD version — Ch. 29). Reproduce the §67.3 gap; find the size where the GPU wins *end-to-end*. Confirm kernel-only is misleading.
2. **Pinned vs pageable.** Measure `cudaMemcpy` bandwidth from pageable vs pinned host memory (§67.4.1, Ch. 23). Quantify the ~2× and the async capability.
3. **Resident data.** Run a sequence of GPU operations (a) transferring data to/from the GPU each op vs (b) keeping data GPU-resident and transferring only once. Quantify the win of residency (§67.3-4). 
4. **Overlap.** Implement the chunked transfer/compute overlap with streams + async + pinned (§67.4.2); compare end-to-end to the un-overlapped version. Confirm the transfer is hidden behind compute.
5. **Where it pays.** Implement a Monte-Carlo option pricing (or a risk scenario sweep) on CPU vs GPU; measure end-to-end. Confirm the GPU wins big for this big-parallel-compute-bound workload (§67.4.3) — and note it's a *batch* (latency-insensitive) job, not the order path (§67.5).

**Checklist — GPU computing with CUDA**

- [ ] GPUs are used **only for the off-hot-path batch tier** (risk, pricing/Greeks, Monte-Carlo, backtesting, ML training/batch-inference — §67.4.3) — **never on the tick-to-trade order path** (PCIe round-trip disqualifies it — §67.1, §67.5, Ch. 66).
- [ ] Offload decisions are based on **end-to-end measurement** (transfer + launch + kernel + transfer), **not kernel-only** (§67.3); the work is **big enough to amortize the PCIe transfer** (Ch. 66).
- [ ] **Data is kept GPU-resident** across operations (transfer once, compute many) — not transferred per operation (§67.3-4); transfers use **pinned host memory** (Ch. 23, §67.4.1).
- [ ] **Transfer and compute are overlapped** (streams + async + pinned — §67.4.2) so end-to-end approaches compute time; the pipeline is kept full.
- [ ] Kernels avoid **warp divergence** (Ch. 13) and use **coalesced global-memory access** (SoA — Ch. 8) with adequate **occupancy** to hide latency (§67.2, §67.5).
- [ ] **Unified memory** (if used) is used knowingly with prefetch hints; performance-critical paths use **explicit pinned transfers** (§67.4.1).
- [ ] **GPUDirect** (Ch. 66) is used for network-fed GPU work to avoid the host bounce (§67.4.1, ties Ch. 68); GPU sharing/contention is managed for predictable batch throughput.
- [ ] Latency-critical *online* compute (e.g. small-model inference on the hot path) stays on **CPU or FPGA** (Ch. 69) — not the GPU.

## 67.7 References

- The NVIDIA **CUDA C++ Programming Guide** and **Best Practices Guide** — the execution/memory model, warps/occupancy, coalescing, streams, pinned/unified memory (the basis of §67.2, §67.4).
- M. Harris / NVIDIA developer blog posts on overlapping transfer and compute, pinned memory, and end-to-end vs kernel timing (§67.3, §67.4.2).
- The CUDA samples and `nvprof`/Nsight profiling tools — measuring end-to-end vs kernel time, occupancy, and transfer (§67.3).
- Quantitative-finance-on-GPU literature (Monte-Carlo option pricing, risk on GPUs) — the workloads of §67.4.3 (e.g. the QuantLib-GPU / cuQuant ecosystems).
- Ch. 66 references (PCIe, GPUDirect) — the host-device boundary that governs §67.1, §67.3.

## 67.8 Additional Reading

- The NVIDIA Nsight Systems/Compute documentation — profiling GPU end-to-end and kernel internals.
- Talks on GPUs in quant finance and risk — where GPUs pay off and the batch-vs-latency framing (§67.4.3, §67.5).
- Ch. 66 (*PCIe*) — the round-trip floor that keeps GPUs off the order path; Ch. 29 (*SIMD*) — the CPU data-parallelism GPUs extend; Ch. 68 (*MPI*) — multi-GPU/multi-node scaling; Ch. 69 (*FPGA*) — the *latency*-tier accelerator (inline, no PCIe round-trip) for the order path; Ch. 1 (*Latency vs Throughput*) — the framing; Ch. 8/13 (*Layout/Branches*) — coalescing/divergence analogs.
- **Appendix E** — PCIe transfer and GPU offload end-to-end numbers; **Appendix F** — CUDA/warp/occupancy/coalescing glossary.

---

*Next: Ch. 68 — Distributed Computing with MPI, scaling the batch tier across *hosts*: point-to-point and collective operations, non-blocking compute/communication overlap, RDMA transports (Ch. 63), and topology-aware placement — for large-scale backtesting and Monte-Carlo risk across a cluster, and why message latency keeps MPI (like the GPU) off the order path.*
