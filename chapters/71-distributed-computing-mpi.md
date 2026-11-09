# Part XI — Heterogeneous Computing & Hardware Acceleration

# Chapter 71 — Distributed Computing with MPI

> **Prerequisites:** Ch. 70 (GPU — the other batch-tier accelerator; MPI scales across nodes/GPUs), Ch. 66 (RDMA/InfiniBand — MPI's fast transport), Ch. 16 (NUMA — rank placement), Ch. 31 (shared-nothing — MPI is shared-nothing across hosts), Ch. 1 (latency vs throughput — MPI is throughput/scale).
>
> **Leads into:** Ch. 72 (FPGA — the latency-tier accelerator, by contrast), Ch. 90 (large-scale backtesting/risk in the case study). Part XI's scale-out tier — for the *off-order-path* compute spread across a cluster, explicitly not the tick-to-trade path.

---

## 71.1 Why it matters: scaling across hosts (off the order path)

Chapter 70's GPU scaled compute *within* a box; **MPI (Message Passing Interface)** scales it *across boxes* — a cluster of machines cooperating on one large computation, each running a copy of the program (a **rank**), exchanging data via messages. When a single host (even with GPUs) isn't enough — a **Monte-Carlo risk** run over millions of scenarios, a **backtest** across decades of history × thousands of parameter combinations, a large **ML training** job — you spread it across tens or hundreds of nodes with MPI. It's the standard for HPC (high-performance computing) and the way a trading firm's *research and risk* tier scales beyond one machine. For the big, parallel, latency-insensitive analytics of Ch. 70, MPI is how you go from "one fast box" to "a cluster."

And the framing is, once again, the same Ch. 1 dichotomy: MPI is a **throughput/scale** tool, **off the order path**. Crossing a *network* between hosts (even the fast RDMA/InfiniBand transports — Ch. 66) costs microseconds per message — so, exactly like the GPU's PCIe round-trip (Ch. 69–70), **message latency keeps MPI off the tick-to-trade path**. You don't distribute the order decision across a cluster (the network hop alone blows the nanosecond budget); you distribute the *batch analytics* around it — backtests, risk, research — where the work is large, parallelizable across nodes, and latency-insensitive (budgets in seconds-to-hours). MPI is the cluster-scale version of the off-hot-path tier; the tick-to-trade path stays single-host (ideally single-thread, single-core — Ch. 31, 65), and inline-on-NIC for hardware acceleration (Ch. 72).

What makes MPI worth a chapter (beyond "run it across nodes") is the **communication structure** — and that the communication is usually the bottleneck, not the compute. MPI gives you **point-to-point** (send/recv between two ranks) and **collective** operations (broadcast, scatter/gather, all-reduce — operations across *all* ranks), and the art of scaling is (1) minimizing and structuring communication (it's the cluster's version of the contention/coordination cost — Ch. 31), (2) **overlapping communication with computation** (non-blocking calls — like the GPU's transfer/compute overlap, §70.4.2), (3) using the fast **RDMA/InfiniBand transport** (Ch. 66) rather than TCP, and (4) **topology-aware rank placement** (NUMA — Ch. 16, and network topology) so ranks that talk a lot are close. A poorly-structured MPI program is dominated by communication (a collective on the critical path serializing everything — §71.5); a well-structured one overlaps communication with compute and scales near-linearly. This chapter explains the communicator/point-to-point/collective model (§71.2), measures message latency vs size (§71.3), details non-blocking overlap, RDMA transports, and topology-aware placement (§71.4), and warns about collectives on the critical path (§71.5). It's the scale-out tier for off-order-path compute.

## 71.2 Mental model: communicators; point-to-point vs collective

**MPI — many processes (ranks), communicating by messages, shared-nothing across hosts.** You launch *N* copies of your program (`mpirun -n N`); each is a **rank** (0..N-1), running on some node, with its *own* memory (shared-nothing — Ch. 31, across hosts). They cooperate by **passing messages** — no shared memory between nodes (that's RDMA's job under the hood — Ch. 66, but the MPI model is messages). For a C++ program that means the **C API** (`MPI_Send`, …) through the `mpicxx` wrapper — the MPI C++ bindings were deleted in MPI-3.0 (2012); Boost.MPI and MPL are the common C++ wrappers if you want RAII and templates over it:

```
   mpirun -n 4 ./app   →   rank 0 (node A) ┐
                           rank 1 (node A) ├─ each runs the program, own memory,
                           rank 2 (node B) │   identified by rank, communicating via messages
                           rank 3 (node B) ┘
   the program: "if (rank == 0) ... else ..."; MPI_Send/Recv between ranks; collectives across all.
```

- **Communicators — groups of ranks.** A **communicator** defines a group of ranks that can communicate (`MPI_COMM_WORLD` = all ranks); you can create sub-communicators for subsets. Operations happen *within* a communicator. (The scoping mechanism.)
- **Point-to-point — between two ranks.** `MPI_Send`/`MPI_Recv` (blocking) or `MPI_Isend`/`MPI_Irecv` (non-blocking — §71.4.1) send a message from one rank to another. The basic communication. `MPI_Recv` blocks until the message has arrived; `MPI_Send` blocks only until the *send buffer is reusable* — for small (eager-protocol) messages that is before the receiver has the data, for large (rendezvous) messages it effectively waits for the receiver; `MPI_Ssend` is the one that truly waits for the match. Non-blocking returns immediately and you check/wait later (enabling overlap — §71.4.1).
- **Collective — across all ranks in a communicator.** Operations involving *all* ranks:
  - **Broadcast** (`MPI_Bcast`) — one rank's data to all.
  - **Scatter/Gather** (`MPI_Scatter`/`MPI_Gather`) — distribute chunks of data to ranks / collect them back (split the work, collect results).
  - **All-reduce** (`MPI_Allreduce`) — combine values from all ranks (sum, max, ...) and give the result to all — the workhorse for parallel reductions (e.g. summing partial risk/Monte-Carlo results across ranks). 
  - **Barrier** (`MPI_Barrier`) — synchronize all ranks (wait until all reach the barrier).
  Collectives are *synchronization points* — they involve all ranks and (often) wait for the slowest — so they're the scaling bottleneck (§71.5).
- **The cost model — communication dominates.** Each message crosses the network (~µs for small over RDMA — Ch. 66, more over TCP), and collectives involve many messages + synchronization. The compute is local (fast, parallel); the **communication is the bottleneck and the thing to minimize/overlap** (§71.4). Scaling efficiency = how well you keep communication off the critical path (the cluster's contention problem — Ch. 31).

**The typical pattern (master-worker / SPMD):**

```
   scatter the work (data/parameters) to ranks → each rank computes its chunk (local, parallel)
   → all-reduce / gather the results → done.
   GOOD: compute >> communication, overlap comm with compute (§71.4.1).
   BAD:  frequent collectives / fine-grained messages → communication dominates → poor scaling (§71.5).
```

The model: **MPI runs N ranks (shared-nothing processes across hosts) that pass messages — point-to-point (send/recv) and collectives (broadcast/scatter/gather/all-reduce/barrier) within communicators. Communication (network, µs+) is the bottleneck, not compute; scaling means minimizing/structuring communication, overlapping it with compute (non-blocking), using RDMA transport, and placing ranks topology-aware. It's throughput/scale across hosts — off the order path (network latency disqualifies it), for batch analytics.**

## 71.3 Measure it: message latency vs size

The fundamental MPI cost is the **message** — its latency (small messages, dominated by the per-message overhead) and bandwidth (large messages, dominated by the transport — Ch. 66). The classic measurement is the latency/bandwidth curve, which (with the transport choice) tells you how communication will scale. Read the knee in it: small messages go **eager** (copied into the receiver's buffer immediately), larger ones **rendezvous** (a handshake, then a zero-copy RDMA transfer — one extra round-trip), and the threshold is tunable (UCX `UCX_RNDV_THRESH`, Open MPI `btl_*_eager_limit`) — the step in `osu_latency` output is that switch, not noise.

```cpp
// mpi_pingpong.cpp — the two MPI costs in one program: small-message latency (non-blocking ping-pong) and a collective.
// Build: mpicxx -O2 -std=c++20 mpi_pingpong.cpp -o mpi_pingpong    (Open MPI 5.0 + UCX 1.17; mpicxx wraps the C API —
//        the C++ bindings were removed in MPI-3.0)
// Run:   mpirun -n 2 --map-by ppr:1:node --bind-to core ./mpi_pingpong     # two nodes over RDMA (UCX_TLS=rc,sm)
//        mpirun -n 2 -x UCX_TLS=tcp ./mpi_pingpong                         # the same over TCP, for the §71.3 gap
#include <cstdio>
#include <cstdint>
#include <mpi.h>

int main(int argc, char** argv) {
    MPI_Init(&argc, &argv);
    int rank = 0, size = 0; MPI_Comm_rank(MPI_COMM_WORLD, &rank); MPI_Comm_size(MPI_COMM_WORLD, &size);
    constexpr int iters = 10000; alignas(64) std::uint8_t buf[8]{};
    const int peer = rank ^ 1;

    double t0 = MPI_Wtime();
    for (int i = 0; i < iters; ++i) {                             // ping-pong: rank 0 sends, rank 1 echoes
        MPI_Request rq[2];
        if (rank == 0) { MPI_Isend(buf, 8, MPI_BYTE, peer, 0, MPI_COMM_WORLD, &rq[0]); MPI_Irecv(buf, 8, MPI_BYTE, peer, 0, MPI_COMM_WORLD, &rq[1]); }
        else           { MPI_Irecv(buf, 8, MPI_BYTE, peer, 0, MPI_COMM_WORLD, &rq[0]); MPI_Isend(buf, 8, MPI_BYTE, peer, 0, MPI_COMM_WORLD, &rq[1]); }
        MPI_Waitall(2, rq, MPI_STATUSES_IGNORE);                  // non-blocking + wait: the §71.4.1 overlap primitive
    }
    double rtt_us = (MPI_Wtime() - t0) / iters * 1e6;

    double local = rank + 1.0, total = 0.0;                       // the collective: a synchronisation point across ALL ranks
    t0 = MPI_Wtime();
    for (int i = 0; i < iters; ++i) MPI_Allreduce(&local, &total, 1, MPI_DOUBLE, MPI_SUM, MPI_COMM_WORLD);
    double ar_us = (MPI_Wtime() - t0) / iters * 1e6;

    if (rank == 0) std::printf("8-byte one-way latency ~%.2f us (RTT/2)   allreduce(N=%d) ~%.2f us\n", rtt_us / 2, size, ar_us);
    MPI_Finalize();
    return 0;
}
```


```
   # Standard MPI benchmarks: OSU Micro-Benchmarks (osu_latency, osu_bw, osu_allreduce) or Intel MPI Benchmarks.
   $ mpirun -n 2 osu_latency       # point-to-point latency vs message size
   $ mpirun -n 2 osu_bw            # bandwidth vs message size
   $ mpirun -n N osu_allreduce     # collective (all-reduce) latency vs ranks and size
   # Compare transports: TCP vs RDMA/InfiniBand (§71.4.2) — and rank placement (§71.4.3).
```

Representative results (toolchain and machine: Appendix L.3 per node) — a cluster of reference hosts with ConnectX-6 HDR100 (RDMA — Ch. 66) vs the same hosts over 25 GbE TCP, Open MPI 5.0 + UCX 1.17, one rank per node bound to a core (illustrative; the *transport gap* and *small-message floor* are the point):

```
   point-to-point latency:           RDMA/InfiniBand (Ch.66)    TCP/Ethernet (Ch.49)
   small message (8 bytes)           ~1-2 µs                    ~20-50 µs        ← RDMA ~10-25x better
   large message (1 MB)              bandwidth-bound (~GB/s)    bandwidth-bound (lower)

   all-reduce (collective) latency:  grows with rank count (log N or worse) — a SYNC point (§71.5)
     N=8:   ~few µs        N=128:  ~tens of µs       ← collectives scale with N; the critical-path cost

   the lessons: small-message latency is the FLOOR (per-message overhead) — batch messages;
                RDMA transport (Ch.66) is ~10-25x TCP — use it; collectives cost grows with N — minimize them.
```

Read it: the **small-message latency** (~1-2 µs over RDMA, ~20-50 µs over TCP) is the per-message floor — so **fine-grained messaging is expensive** (many small messages pay the floor each time); **batch** data into fewer larger messages (the batching theme — Ch. 37, 70). The **transport** matters enormously: **RDMA/InfiniBand (Ch. 66) is ~10-25× faster than TCP** for small messages — so a serious MPI cluster uses RDMA, not Ethernet TCP (§71.4.2). And **collective latency grows with rank count** (all-reduce is a synchronization across all N ranks — log N or worse) — so collectives are the scaling bottleneck (§71.5): a frequent collective on the critical path serializes the whole cluster and caps scaling. The measurement discipline mirrors the GPU's (Ch. 70): **measure the *communication* cost (it dominates), choose the RDMA transport, and structure the program so communication is minimized and overlapped with compute** (§71.4) — and remember all of this is *microseconds-plus per message*, which is why MPI (like the GPU) is for batch analytics, **off the order path** (one network hop blows the nanosecond budget). The OSU/IMB benchmarks give you these curves for your cluster — run them before designing the communication pattern.

## 71.4 Techniques

### 71.4.1 Non-blocking calls and compute/communication overlap

The key scaling technique — hide communication behind computation (the MPI version of §70.4.2):

- **Non-blocking point-to-point (`MPI_Isend`/`MPI_Irecv`).** Blocking `MPI_Send`/`Recv` *wait* for the transfer (the rank stalls — like a blocking GPU transfer, Ch. 70). **Non-blocking** versions return *immediately* (the transfer proceeds in the background), and you `MPI_Wait`/`MPI_Test` later. This lets a rank **compute while its messages are in flight** — overlapping communication with computation, so the communication cost is hidden (the rank isn't stalled waiting). The single most important MPI scaling technique.
- **The overlap pattern.** Post the non-blocking receives early, start the (independent) computation, and `MPI_Wait` for the data only when you actually need it — by which time it's (ideally) already arrived (transfer overlapped with compute). Structure the algorithm to have *independent* work to do while communicating (the GPU/Disruptor overlap idea — Ch. 37, 70).
- **Non-blocking collectives (`MPI_Iallreduce` etc.).** Modern MPI has *non-blocking* collectives — start an all-reduce, compute other work, then wait for it. Overlapping the (expensive — §71.3) collective with compute is a big win (vs a blocking collective stalling all ranks — §71.5).
- **Avoid unnecessary synchronization.** Each blocking call / collective / barrier is a sync point where ranks wait for each other (the slowest rank — load imbalance). Minimize barriers, balance the load across ranks (so none lag), and overlap to keep ranks busy rather than waiting.
- **Coarse-grained communication.** Communicate *less often* with *more* data per message (batch — §71.3) rather than fine-grained chatty messaging (which pays the per-message latency floor repeatedly). Aggregate.

### 71.4.2 RDMA/InfiniBand transports

Using the fast transport under MPI (ties Ch. 66):

- **MPI runs over a transport — choose RDMA.** MPI implementations (OpenMPI, MPICH, Intel MPI) support multiple transports: TCP/Ethernet (slow — §71.3), shared memory (intra-node — fast, for ranks on the same host), and **RDMA/InfiniBand** (Ch. 66 — fast inter-node). For a low-latency cluster, configure MPI to use the **RDMA transport** (InfiniBand verbs, or RoCE — Ch. 66), often via **UCX** (Unified Communication X — the modern MPI transport layer) — ~10-25× lower small-message latency than TCP (§71.3).
- **Intra-node shared memory.** Ranks on the *same* host communicate via **shared memory** (Ch. 26, 52) — much faster than going through the network stack. MPI does this automatically for co-located ranks; place communicating ranks on the same node where possible (§71.4.3).
- **RDMA's benefits carry over (Ch. 66).** Zero-copy, kernel-bypass, one-sided operations — MPI over RDMA gets the Ch. 66 latency, with the registration/pinning handled by the MPI layer. **MPI one-sided** (`MPI_Put`/`MPI_Get` — RMA, remote memory access) maps onto RDMA read/write (Ch. 66) for one-sided communication patterns.
- **GPUDirect for multi-GPU (Ch. 69–70).** For multi-GPU clusters, **CUDA-aware MPI** + GPUDirect RDMA (Ch. 69) lets MPI transfer *directly* between GPU memories across nodes — bypassing the host bounce (Ch. 69). Essential for distributed GPU training/Monte-Carlo (Ch. 70).
- **Configure and verify the transport.** Misconfigured MPI silently falling back to TCP (when you expected RDMA) is a common, large performance loss. Verify the transport in use (MPI's verbose/debug output, or the OSU latency benchmark — §71.3 — showing RDMA-level numbers).

### 71.4.3 NUMA- and topology-aware rank placement

Placing ranks so communicating ranks are close (ties Ch. 16, 43):

- **Rank-to-core/node binding (Ch. 43).** Bind each rank to specific cores (and NUMA nodes — Ch. 16) — `mpirun --bind-to core`/`--map-by`. Unpinned ranks migrate (Ch. 42) and access remote NUMA memory (Ch. 16) — the same pinning discipline as the rest of the book, applied to ranks. Pin ranks to cores on their NUMA node.
- **Topology-aware mapping.** Place ranks that **communicate a lot** *close* together (same node → shared memory; same NUMA node; close in the network topology) and spread independent ranks out. MPI's mapping options (`--map-by node/socket/numa`) and topology-aware libraries place ranks to minimize communication distance — the network/NUMA version of co-locating communicating threads (Ch. 31, 16).
- **NUMA-aware memory (Ch. 16).** Each rank's data should be NUMA-local to its core (first-touch — Ch. 16); a rank accessing remote-node memory pays the NUMA cliff (Ch. 16) on top of everything. Bind rank memory to its node.
- **Match ranks to hardware.** One rank per core, or one rank per NUMA node with threads (hybrid MPI+OpenMP) — match the rank/thread structure to the hardware topology (cores, NUMA nodes, GPUs). Over/under-subscribing cores (Ch. 31, 42) hurts as much here as anywhere.
- **Network topology.** On a large cluster, the *network* topology (fat-tree, dragonfly) matters — ranks far apart in the network have higher latency. Topology-aware placement (and topology-aware collectives) minimize the network distance of communication. (For a small cluster, intra-node + RDMA dominates; for hundreds of nodes, network topology matters.)

## 71.5 Pitfalls & anti-patterns: collectives on the critical path

- **MPI on the order path (the cardinal sin).** Distributing the tick-to-trade decision across the cluster — even one MPI message is microseconds (network — §71.3), disqualifying it from the nanosecond budget (§71.1). MPI is for **batch analytics off the order path**; the order path is single-host (Ch. 65). Don't put a network hop in the critical path.
- **Collectives / barriers on the critical path (the scaling killer).** A collective (all-reduce, barrier) synchronizes *all* ranks and waits for the *slowest* (§71.2-3) — frequent collectives serialize the cluster and cap scaling (Amdahl — Ch. 31, at cluster scale). Minimize collectives, use **non-blocking** ones (overlap — §71.4.1), and balance the load so no rank lags.
- **Fine-grained / chatty messaging.** Many small messages each pay the per-message latency floor (§71.3) — communication dominates. **Batch** into fewer larger messages (§71.4.1). Aggregate data before sending.
- **No compute/communication overlap.** Blocking sends/recvs/collectives stall ranks waiting (§71.4.1) — leaving the cluster idle during communication. Use **non-blocking** calls and overlap communication with independent computation (the §71.4.1 win). 
- **Falling back to TCP transport.** MPI silently using TCP when you expected RDMA (misconfiguration) is ~10-25× slower small-message latency (§71.3, §71.4.2) — a huge, silent loss. Verify the transport (OSU benchmark numbers, MPI debug output).
- **Load imbalance.** If ranks get unequal work (or some nodes are slower), collectives wait for the slowest — wasting the fast ranks. Balance the work across ranks (dynamic load balancing where the work is irregular — e.g. a backtest with uneven parameter costs).
- **Unpinned / NUMA-blind ranks (Ch. 16, 43).** Ranks migrating (Ch. 42) or accessing remote-NUMA memory (Ch. 16) waste performance — bind ranks to cores/nodes (§71.4.3). The pinning discipline applies to ranks.
- **Ignoring intra-node shared memory.** Ranks on the same host communicating via the network (instead of shared memory — §71.4.2) waste latency. Co-locate communicating ranks and let MPI use shared memory intra-node.
- **Treating MPI as the only scaling tool.** For *embarrassingly parallel* batch work (independent parameter sweeps, scenario runs — Ch. 70) with *no* inter-task communication, a simpler **task-queue / map-reduce** (or just independent jobs) may be easier and as fast as MPI — MPI shines when ranks must *communicate*. Match the tool to the communication structure.

## 71.6 Exercises & checklist

**Exercises**

1. **Latency vs size & transport.** Run `osu_latency`/`osu_bw` over TCP vs RDMA/InfiniBand (§71.3); plot latency vs message size. Quantify the small-message floor and the ~10-25× RDMA advantage. Verify which transport MPI actually used.
2. **Collective scaling.** Run `osu_allreduce` for N = 2, 8, 32, 128 ranks; plot latency vs N. Confirm collective cost grows with rank count (§71.3) — the scaling bottleneck (§71.5).
3. **Overlap.** Implement a stencil/reduction with blocking vs non-blocking (`MPI_Isend`/`Irecv` + compute + `Wait`) communication; measure the scaling efficiency. Confirm overlap hides communication (§71.4.1).
4. **Rank placement.** Run a communication-heavy job with ranks placed (a) randomly/unpinned vs (b) topology-aware + NUMA-bound (§71.4.3, Ch. 16, 43). Measure the difference. 
5. **Monte-Carlo risk at scale.** Distribute a Monte-Carlo risk run (or a backtest sweep) across a cluster with MPI: scatter scenarios, compute locally (CPU/GPU — Ch. 70), all-reduce the results. Measure strong/weak scaling. Confirm it scales (and that it's a *batch* job, not the order path — §71.1, §71.5).

**Checklist — distributed computing with MPI**

- [ ] MPI is used for **batch analytics across hosts** (large-scale Monte-Carlo risk, backtesting, ML training/sweeps — §71.1) — **never on the tick-to-trade order path** (network latency disqualifies it — §71.1, §71.5).
- [ ] Communication is **minimized and coarse-grained** (batch into fewer larger messages — §71.3-4), and **overlapped with computation** via **non-blocking** point-to-point and collectives (§71.4.1).
- [ ] **Collectives/barriers are minimized** on the critical path (they sync all ranks / wait for the slowest — §71.5); the load is **balanced** across ranks.
- [ ] MPI uses the **RDMA/InfiniBand transport** (Ch. 66, via UCX) — **verified** (not silently TCP — §71.4.2) — and **shared memory intra-node**; **CUDA-aware MPI + GPUDirect** for multi-GPU (Ch. 69–70).
- [ ] Ranks are **pinned to cores and NUMA-bound** (Ch. 16, 43), **topology-aware** (communicating ranks close — same node/NUMA/network — §71.4.3); no migration / remote-NUMA access.
- [ ] The **communication cost is measured** (OSU/IMB benchmarks — §71.3) and the program structured to keep it off the critical path (the cluster's contention problem — Ch. 31).
- [ ] For **embarrassingly parallel** work with no inter-task communication, a simpler task-queue/independent-jobs approach is considered over MPI (§71.5).
- [ ] All of this is the **off-order-path scale tier** — the tick-to-trade path stays single-host (Ch. 65) / inline-hardware (Ch. 72).

## 71.7 References

- The MPI Standard (MPI Forum) and the OpenMPI / MPICH / Intel MPI documentation — the API (communicators, point-to-point, collectives, non-blocking, one-sided RMA) of §71.2, §71.4.
- W. Gropp, E. Lusk, A. Skjellum, *Using MPI* — the definitive practical MPI text.
- The OSU Micro-Benchmarks and Intel MPI Benchmarks documentation — measuring latency/bandwidth/collectives (§71.3).
- The UCX (Unified Communication X) documentation — the modern RDMA/transport layer under MPI (§71.4.2, ties Ch. 66).
- HPC scaling / topology-aware-placement literature and the MPI binding/mapping documentation (`--bind-to`/`--map-by`) — §71.4.3 (ties Ch. 16, 43).

## 71.8 Additional Reading

- Talks/papers on large-scale Monte-Carlo risk and distributed backtesting in finance — the workloads of §71.1 at cluster scale.
- The CUDA-aware MPI / GPUDirect documentation — multi-GPU distributed compute (ties Ch. 69–70).
- Ch. 70 (*GPU/CUDA*) — the per-node accelerator MPI scales across; Ch. 66 (*RDMA*) — MPI's fast transport; Ch. 16 (*NUMA*) / Ch. 43 (*Pinning*) — rank placement; Ch. 31 (*Concurrency Foundations*) — shared-nothing/communication-cost at cluster scale; Ch. 69 (*PCIe*) — GPUDirect; Ch. 90 (*Case Study*) — large-scale backtesting/risk.
- **Appendix F** — MPI/rank/collective glossary.

---

*Next: Ch. 72 — FPGA Acceleration, the accelerator that *can* go on the tick-to-trade path: programmable hardware placed *inline on the NIC* — no PCIe round-trip (Ch. 69), no software — delivering deterministic nanosecond wire-to-wire pipelines for feed handling and order entry, and the decision of what belongs in fabric vs software.*
