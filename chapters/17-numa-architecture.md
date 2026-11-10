# Part II — CPU Microarchitecture

# Chapter 17 — NUMA Architecture

> **Reference machine note:** this chapter uses a **dual-socket 2× Xeon Gold 6326** (two Ice Lake-SP nodes, NUMA node 0 and node 1) — NUMA effects need two sockets, so it departs from the single-socket machine of Ch. 8–16. All other conventions (flags, pinning, turbo off) carry over.
>
> **Prerequisites:** Ch. 8 (the memory hierarchy — NUMA adds a *second axis* of memory latency: not just which cache level, but which socket's DRAM), Ch. 16 (virtual memory — a physical page lives on a *specific node*, and first-touch decides which), Ch. 9–10 (data layout — NUMA rewards node-local, partitioned data), Ch. 2–3 (top-down, benchmarking).
>
> **Leads into:** Ch. 27–28 (allocators — NUMA-aware arenas and where pages come from), Ch. 37 (false sharing — cross-node sharing is its most expensive form), Ch. 49 (thread/IRQ pinning — the affinity machinery NUMA placement depends on), Ch. 77 (MPI — topology-aware rank placement at cluster scale). First-touch and binding reappear in Appendix C.

---

## 17.1 Why it matters: cross-socket access is a hidden cliff

Every chapter so far has assumed memory is *uniform* — that a load costs the same regardless of which core issues it. On a multi-socket machine that assumption is false, and the penalty for ignoring it is a **cliff**: a memory access satisfied by the **local** node's DRAM costs ~80–100 ns, while the *same* access satisfied by the **other socket's** DRAM — traveling across the inter-socket interconnect (UPI on Intel, Infinity Fabric on AMD) — costs ~140–200 ns, roughly **1.5–2× more**, and with worse bandwidth and more variance. Nothing in your source code says "this load is remote"; the cost depends entirely on *where the physical page happens to live* relative to the core touching it — a placement decision made implicitly, usually by whichever thread first wrote the page (§17.2.2). NUMA is the canonical *hidden* performance cliff: invisible in the code, invisible in cache-miss counters, and capable of silently halving your memory performance.

For a trading system this is acute because the natural deployment — a big multi-socket server running feed handlers, strategies, order gateways, and housekeeping — *spreads work across sockets by default*, and the OS, left to its own devices, will allocate memory and schedule threads in ways that scatter a hot data structure's pages across both nodes and migrate the thread that uses them to the wrong socket. The result is a feed handler on node 0 chasing an order book whose pages were faulted in on node 1, paying the remote penalty on every cache miss — a per-access tax that lands squarely in the tail (Ch. 1) and that you cannot see without NUMA-specific measurement (§17.3). Worse, a *shared* cache line bounced between cores on *different* sockets (false sharing across nodes — Ch. 37) pays the cross-interconnect coherence cost, the single most expensive memory event on the box.

The discipline is simple to state and easy to get wrong: **keep each thread's hot data on the thread's local node, and keep each thread on its node.** That means controlling *allocation placement* (first-touch from the right thread, or explicit binding), *thread affinity* (pin the thread so it doesn't migrate off its node — Ch. 49), and *data structure design* (partition per-node, avoid cross-node sharing). Done right, every hot access is local and the cliff never appears. Done by accident — the default — you get a machine whose latency distribution is dominated by remote accesses nobody intended. As always, measure first (Ch. 2): on a single-socket box NUMA is moot; on a multi-socket box it may be your largest single source of memory latency and jitter.

---

## 17.2 Mental model

### 17.2.1 Node locality and the interconnect

A NUMA (Non-Uniform Memory Access) system is several **nodes**, each a bundle of cores + their *own* memory controllers + attached DRAM, stitched together by a cache-coherent interconnect:

```
        NODE 0 (socket 0)                         NODE 1 (socket 1)
   ┌──────────────────────────┐             ┌──────────────────────────┐
   │  cores 0..15             │   UPI link  │  cores 16..31            │
   │  L1/L2/L3                │◄═══════════►│  L1/L2/L3                │
   │  mem controller ─ DRAM0  │  (~remote   │  mem controller ─ DRAM1  │
   └──────────────────────────┘   penalty)  └──────────────────────────┘
        local: ~85 ns                              local: ~85 ns
        remote (DRAM1): ~150 ns  ◄── core on node 0 reaching node 1's memory across UPI
```

The essential facts:

- **Local vs remote latency and bandwidth.** A core accessing its *own* node's DRAM is local and fast. Accessing the *other* node's DRAM goes over the interconnect — higher latency, lower bandwidth, and a shared resource that saturates under cross-node traffic. The ratio (the "NUMA factor") is ~1.5–2× for latency on a 2-socket box, and grows with more sockets and hops.
- **Coherence spans nodes.** The interconnect keeps caches coherent across sockets (Ch. 8's MESI, extended). So a cache line owned/modified by a core on the *other* node must be fetched across UPI — a **remote HITM** (hit-modified), the most expensive coherence event, central to cross-node false sharing (§17.5, Ch. 37).
- **It's not just DRAM — it's the whole node's resources.** L3, memory bandwidth, and even I/O (a NIC is attached to one socket's PCIe — Ch. 64, 75) are node-local. A thread far from its NIC's node pays for *that* too. NUMA-awareness extends to *where your NIC and devices are*.
- **`numactl --hardware` shows you the map.** Node count, cores per node, memory per node, and a **distance matrix** (relative access cost: 10 = local, 21 = one remote hop, etc.). This is the topology you design around (discover it — Ch. 6).

- **One socket may be more than one node.** Ice Lake-SP firmware offers **Sub-NUMA Clustering** (SNC2), which splits each socket into two NUMA nodes along its mesh — four nodes on the 2× 6326 box — each with half the socket's L3 slices and memory channels: lower local latency and less L3 contention for a thread that stays in its half, at the cost of halving the local memory and L3 per node and turning the other half of your *own* socket into a (small) remote hop. AMD's `NPS1/2/4` modes and per-CCX L3 domains are the equivalent. `numactl -H` tells you which mode the box booted in, and every `--cpunodebind`/`--membind` recipe below must follow the map it prints, not the diagram above (Ch. 6, Appendix C). Verify placement after warm-up with `numastat -p <pid>` or `/proc/<pid>/numa_maps`; `migratepages`/`move_pages(2)` fix a misplaced region without a restart.

The mental model: **memory has a "home" node, and access cost depends on the distance between the core touching it and that home.** Your job is to make the distance zero on the hot path.

### 17.2.2 First-touch allocation policy

The decision that quietly determines local-vs-remote is **first-touch**: Linux's default policy is that a freshly-allocated virtual page is backed by physical memory **on the node of the thread that first *writes* to it** — not the thread that `malloc`'d/`mmap`'d it. Allocation (`malloc`, `mmap`) only reserves virtual address space; no physical page exists until the first **touch** (a write that faults it in — Ch. 16, 27), and *that* is when the node is chosen.

The consequences are the source of most accidental NUMA pain:

- **The allocating thread doesn't decide; the touching thread does.** A common bug: a main/setup thread allocates *and initializes* (zeroes, `memset`, fills) a big buffer, faulting every page onto *its* node — then hands the buffer to worker threads on the *other* node, which now access it all remotely. The fix is **first-touch from the using thread**: have each worker initialize the memory it will own (§17.4).
- **`memset`/value-initialization at allocation places everything on one node.** Eagerly touching a buffer at allocation time — an explicit `memset` or fill loop, `std::vector<T>(N)`/`new T[N]()` value-initialization, `MAP_POPULATE` — front-loads first-touch onto the allocating thread's node. (A *large* glibc `calloc` is not a culprit: it comes from a fresh `mmap` whose pages are already zero and are not written, so first-touch still happens at the first real write; only small `calloc`s carved from recycled heap are zeroed in place.) To get per-node placement you must defer the touch to the consuming thread, or bind explicitly.
- **Thread migration breaks locality after the fact.** Even with correct first-touch, if the scheduler later migrates the thread to the other node (Ch. 49), its local data is now remote. NUMA locality requires **pinning** (affinity) so the thread stays on the node its data lives on — placement and affinity are a *pair*; one without the other is fragile.
- **The policy is configurable.** Beyond default first-touch, Linux offers `bind` (force a node), `preferred` (try a node, fall back), and `interleave` (round-robin pages across nodes — good for bandwidth-bound shared structures, bad for latency-bound local ones). `numactl`, `mbind(2)`, `set_mempolicy(2)`, and `libnuma` are the controls (§17.4.1).

The rule to internalize: **memory goes where it's first written, by whichever thread writes it — so initialize hot per-thread data on the thread that will own it, and pin that thread.** Everything in §17.4 is a way to make first-touch (or explicit binding) put pages where you want them.

---

## 17.3 Measure it: local vs remote DRAM latency with `numactl`

The experiment is the cleanest possible isolation of the NUMA factor: run the *identical* pointer-chasing latency probe (a dependent load chain over a large buffer, so each access waits for the last — the classic latency benchmark of Ch. 8) with the **memory** pinned to one node and the **CPU** pinned either to the *same* node (local) or the *other* node (remote). `numactl` does both bindings; nothing in the program changes.

```cpp
// numa_lat.cpp — pointer-chase latency over a large buffer; node placement set by numactl.
// Build: g++ -O2 -std=c++20 -march=native numa_lat.cpp -o numa_lat
// Run (dual-socket reference machine), memory on node 0, CPU local vs remote:
//   local : numactl --cpunodebind=0 --membind=0 ./numa_lat
//   remote: numactl --cpunodebind=1 --membind=0 ./numa_lat   # CPU node 1 reaching node 0 mem
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <vector>
#include <random>
#include <chrono>
#include <sys/mman.h>

int main() {
    constexpr std::size_t BYTES = std::size_t(512) << 20;   // 512 MiB: defeats all caches
    constexpr std::size_t N = BYTES / sizeof(std::size_t);
    constexpr std::size_t STEPS = 100'000'000;

    // The chase buffer on 2 MB pages (THP via madvise; check AnonHugePages in smaps_rollup), so that
    // each step is ONE DRAM access and not DRAM + an STLB-miss page walk — on 4 KiB pages the 512 MiB
    // of PTEs (1 MiB) spill L2 and ~20 ns of every step would be translation (Ch. 16), on both nodes.
    auto* a = static_cast<std::size_t*>(mmap(nullptr, BYTES, PROT_READ | PROT_WRITE,
                                             MAP_PRIVATE | MAP_ANONYMOUS, -1, 0));
    if (a == MAP_FAILED) { std::perror("mmap"); return 1; }
    madvise(a, BYTES, MADV_HUGEPAGE);
    // Build a random permutation cycle so each load depends on the previous (true latency).
    std::vector<std::size_t> perm(N);
    for (std::size_t i = 0; i < N; ++i) perm[i] = i;
    std::mt19937_64 rng(42);
    for (std::size_t i = N - 1; i > 0; --i) std::swap(perm[i], perm[rng() % (i + 1)]);
    for (std::size_t i = 0; i < N; ++i) a[perm[i]] = perm[(i + 1) % N];   // first touch → node per --membind

    auto t0 = std::chrono::steady_clock::now();
    std::size_t p = 0;
    for (std::size_t s = 0; s < STEPS; ++s) p = a[p];        // dependent chase: one miss at a time
    auto t1 = std::chrono::steady_clock::now();
    asm volatile("" :: "r"(p));                               // keep the chain live
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count();
    std::printf("chase end=%zu  %.1f ns/access\n", p, ns / STEPS);
    munmap(a, BYTES);
    return 0;
}
```

Profile with `perf stat`, and use the **uncore/offcore** counters that distinguish local from remote DRAM (names vary by platform; on Ice Lake-SP look for `mem_load_l3_miss_retired.remote_dram` vs `...local_dram`, or the uncore `UNC_M_*`/offcore-response remote events). Representative results — reference machine **2× Xeon Gold 6326** (dual-socket Ice Lake-SP), GCC 13 `-O2 -std=c++20 -march=native`, Linux 6.18 with `kernel.numa_balancing=0` (so the kernel cannot migrate the chased pages mid-run) and the buffer on 2 MB pages as the listing requests (check `AnonHugePages` in `/proc/<pid>/smaps_rollup`), pinned, turbo off (illustrative; the *ratio* is the point):

```
                                          local (CPU0→mem0)   remote (CPU1→mem0)
ns / access (dependent chase)                ~85 ns              ~150 ns      <- ~1.8x: the cliff (4 KB pages: both include ~20 ns of page walk, Ch. 16)
mem_load..._retired.local_dram               ~all                ~0
mem_load..._retired.remote_dram              ~0                   ~all         <- the whole story
L3 / LLC miss rate                           ~same               ~same        <- same access pattern
instructions                                 same                same         <- identical work
```

Read it the Ch. 2 way: **identical instructions, identical access pattern and LLC-miss rate — yet ~1.8× the per-access latency, entirely from `remote_dram` replacing `local_dram`.** The program is byte-for-byte the same; only `numactl`'s `--cpunodebind` moved the *touching core* to the other socket while the data stayed on node 0, so every miss now crosses UPI. That ~65 ns gap *per dependent access* is the NUMA cliff (on 4 KiB pages both columns would carry ~20 ns more — an STLB-miss page walk per access, Ch. 16 — with the page tables themselves sitting on node 0 by `--membind`; the huge-page mapping keeps the measurement DRAM-only), and on a pointer-chasing hot path (a node-based book, a hash probe — Ch. 29) it compounds into a large, invisible-without-NUMA-counters tail. The fingerprint: **high `remote_dram` / cross-socket offcore responses with unchanged cache-miss rates.** When you see it, the cure is placement and pinning (§17.4); if accesses are already local, NUMA isn't your problem.

(Run `numactl --hardware` first to confirm the node→core map and the distance matrix for your box; and note that an *interleaved* allocation — `numactl --interleave=all` — would land ~halfway between local and remote, the bandwidth-vs-latency trade-off of §17.4.1.)

---

## 17.4 Techniques

The whole game is **co-locating each thread with its hot data on one node, and keeping it there.** Two levers: *placement* (where pages live — §17.4.1) and *structure + affinity* (how data and threads are partitioned across nodes — §17.4.2). They must be used together: correct placement without pinning decays when the thread migrates; pinning without placement leaves the data remote.

### 17.4.1 Allocation policy and binding

Control where pages land, from coarsest to finest:

- **`numactl` at launch (process-level).** Wrap the process: `numactl --cpunodebind=0 --membind=0 ./app` forces both CPU and memory to node 0 — the simplest way to make a *single-node* service entirely local. `--preferred=0` is a softer variant (fall back if node 0 is full); `--interleave=all` round-robins pages across nodes — use it **only** for bandwidth-bound, widely-shared structures where no single node is "home," *never* for latency-bound per-thread data.
- **First-touch discipline (the default policy, used deliberately).** Within a multi-node process, get per-node placement for free by having **each thread initialize (first-write) the memory it will own**, while pinned to its node. The pattern:
  ```cpp
  // On each worker thread, pinned to its node (Ch. 49), BEFORE the hot loop:
  for (std::size_t i = lo; i < hi; ++i) my_buffer[i] = 0;   // first-touch → pages land on THIS node
  // ... now my_buffer[lo..hi) is local to this thread. Do NOT pre-zero it on the main thread.
  ```
  This is the most important NUMA technique: **don't `memset`/value-initialize big per-thread buffers on the setup thread** (§17.2.2); defer the touch to the owner.
- **Explicit binding (`libnuma`/`mbind`).** For precise control, `numa_alloc_onnode(size, node)` allocates on a specific node; `mbind(2)`/`set_mempolicy(2)` set per-region or per-thread policy; `numa_alloc_interleaved` interleaves. Use these when first-touch is awkward (e.g. a shared allocator) or you need a region on a node other than the toucher's.
- **NUMA-aware allocators (Ch. 28).** A per-node arena/pool allocator (each node has its own arena, threads draw from their local one) bakes locality into allocation, so steady-state hot-path allocations (or pre-allocations) are automatically local — the production pattern for a multi-node service. Pre-fault and `mlock` (Ch. 16, 30) per node during warm-up.

### 17.4.2 NUMA-aware data structures and thread placement

Placement handles *where pages go*; design handles *whether the access ever needs to be remote*:

- **Shared-nothing partitioning by node.** The cleanest NUMA design is *no cross-node sharing*: shard the work so each node owns its data outright. In trading terms — **partition by symbol/venue**: feed handler + book + strategy for symbols A–M pinned to node 0 with their data local, N–Z on node 1. Each tick is processed entirely within one node; the interconnect carries almost nothing. This is the same shared-nothing discipline as Ch. 35, with NUMA as the motivation.
- **Replicate read-mostly data per node.** Reference/config data read by all threads (symbol tables, tick-size maps, risk limits) can be **copied once per node** so every read is local — trading a little memory for zero remote reads. Combined with lock-free publication (seqlock/ RCU — Ch. 39–40) for updates, each node reads its local replica. (Hot-reload, Ch. 89, re-publishes per-node copies.)
- **Pin threads to nodes (and to cores) — Ch. 49.** Placement is meaningless if the thread drifts. `sched_setaffinity`/`pthread_setaffinity_np` (or `numactl --cpunodebind`, `taskset`) pin each hot thread to a core on the node holding its data, so locality holds. Affinity + first-touch are the inseparable pair.
- **Put threads near their I/O.** A feed handler should run on the node whose PCIe hosts its NIC (Ch. 64, 75); a thread doing disk capture (Ch. 95) near that controller. Cross-node I/O adds an interconnect hop to every packet/block. Discover device-to-node mapping (`/sys/.../numa_node`) and pin accordingly.
- **Avoid cross-node false sharing (Ch. 37).** A hot, frequently-written shared line (a counter, a queue index) bounced between cores on *different* nodes pays the remote-HITM cost on every write — the worst memory event on the box. Pad to a cache line, give each node/thread its own counter, and aggregate off the hot path.

The synthesis: **design so the hot path never needs the other node** — partition data and threads per node, replicate read-mostly state, pin everything, and keep shared writable lines out of the cross-node path. The interconnect should be nearly idle during steady-state trading; if it's busy, you have remote accesses to hunt down (§17.3).

---

## 17.5 Pitfalls & anti-patterns: accidental remote allocation; cross-node sharing

- **Initializing on the wrong thread (first-touch trap).** The classic bug: a setup thread `memset`s/value-initializes/fills a big buffer (faulting all pages onto *its* node), then worker threads on other nodes use it remotely (§17.2.2). Fix: first-touch from the owning thread; never pre-zero per-thread buffers centrally.
- **Placement without pinning (or vice versa).** Correct first-touch decays the moment the scheduler migrates the thread off its node (Ch. 49); pinning a thread whose data is remote doesn't help either. They're a **pair** — always pin the thread to the node holding its data.
- **`numactl --interleave` for latency-bound data.** Interleave is for *bandwidth*-bound shared structures; using it for per-thread latency-sensitive data guarantees ~50% of accesses are remote by design. Match policy to the access pattern (latency → bind local; bandwidth-shared → consider interleave).
- **Cross-node false sharing.** A padded-counter omission (Ch. 37) is bad within a socket and *catastrophic* across sockets — every write is a remote HITM over UPI. Per-node/per-thread counters, padded, aggregated off the hot path.
- **Threads far from their NIC/device.** Pinning compute correctly but ignoring that the NIC (Ch. 64) or NVMe (Ch. 95) hangs off the *other* socket's PCIe adds an interconnect hop to every packet/block. Place I/O threads on the device's node.
- **Huge pages placed remotely (Ch. 16).** First-touch chooses a *whole* 2 MB (or 1 GB!) page's node; pre-faulting a huge-page region from the wrong thread strands a large, coarse chunk remote. And for *explicit* huge pages first-touch only decides the node if that node's pool has free pages: the pool is per node (`/sys/devices/system/node/nodeN/hugepages/hugepages-2048kB/nr_hugepages`; a global `vm.nr_hugepages` is spread round-robin across nodes), and a fault whose local pool is empty silently takes a page from the other node (or fails under a strict `mbind`). Reserve per node (write the per-node sysfs files, or reserve under `numactl --membind`), pre-fault from the using thread on the right node, and check per-node `free_hugepages` / `numastat -m` after warm-up.
- **Trusting the OS auto-balancer on a latency box.** Kernel automatic NUMA balancing (page migration, `numa_balancing`) chases locality heuristically but introduces *migration stalls* and jitter (Ch. 1) — usually disabled on a tuned box (`sysctl -w kernel.numa_balancing=0`, or `numa_balancing=disable` on the kernel command line) in favor of explicit placement + pinning.
- **Measuring without NUMA counters.** Remote access is invisible in cache-miss rates (§17.3); if you don't watch `local_dram`/`remote_dram`/offcore-response, you'll miss it entirely. Measure NUMA explicitly, or you won't know the cliff is there.
- **Optimizing NUMA on a single-socket box.** One node = no remote accesses; the techniques here are moot (and `--membind` is a no-op). Confirm topology (`numactl --hardware`) before spending effort.

---

## 17.6 Exercises & checklist

**Exercises**

1. **Measure the cliff.** Build `numa_lat.cpp` on the dual-socket box; run local (`--cpunodebind=0 --membind=0`) vs remote (`--cpunodebind=1 --membind=0`). Confirm with `perf stat` (`...local_dram` vs `...remote_dram`): identical access pattern, ~1.5–2× latency. What's *your* box's NUMA factor? Cross-check against `numactl --hardware`'s distance matrix.
2. **First-touch in action.** Allocate a large buffer, then (a) `memset` it on the main thread, vs (b) first-touch it in stripes from threads pinned to different nodes. Run a per-thread sum and compare `remote_dram` counts. Demonstrate that placement followed the *first writer*, not the allocator (§17.2.2).
3. **Pinning matters.** Take the first-touch-correct version and *remove* thread pinning (let the scheduler migrate). Does locality survive a busy system? Re-add affinity (Ch. 49) and re-measure. Explain why placement + pinning are a pair.
4. **Interleave vs bind.** Run a *bandwidth*-bound streaming kernel (sum a huge array with many threads) under `--membind=0` vs `--interleave=all`. Which wins, and why is the answer opposite to the latency-bound chase of Exercise 1?
5. **Cross-node false sharing.** Build a shared counter hammered by threads on both nodes, then give each thread a padded per-node counter aggregated at the end (Ch. 37). Measure the cross-node HITM cost (offcore/`mem_load..._retired.remote_hitm` or similar). How much worse is cross-node than within-node sharing?

**Checklist — NUMA**

- [ ] I confirmed the box is **multi-socket** (`numactl --hardware`) and measured **`remote_dram`/offcore** (not just cache misses) before optimizing.
- [ ] Each hot thread's data is **first-touched by that thread**, pinned to its node — no central `memset`/value-initialization of per-thread buffers on the setup thread.
- [ ] Hot threads are **pinned** (Ch. 49) to a core on the node holding their data; placement and affinity are used **together**.
- [ ] The hot path is **partitioned per node** (shared-nothing, e.g. by symbol/venue); read-mostly reference data is **replicated per node**.
- [ ] No **cross-node false sharing** — shared writable lines are padded and per-node/per-thread, aggregated off the hot path (Ch. 37).
- [ ] I/O threads run on the **node hosting their NIC/NVMe** (`/sys/.../numa_node` — Ch. 64, 75, 95); no accidental cross-node device hops.
- [ ] **Huge pages** (Ch. 16) are first-touched on the right node; `interleave` is used **only** for bandwidth-bound shared data, never latency-bound per-thread data.
- [ ] Automatic NUMA balancing is **disabled** (`kernel.numa_balancing=0`, or measured to add no jitter) on the latency box; placement is explicit.

---

## 17.7 References

- U. Drepper, *What Every Programmer Should Know About Memory* — the NUMA sections: node topology, the interconnect, first-touch, and `libnuma`, with the measurement approach behind §17.3.
- C. Lameter, *"NUMA (Non-Uniform Memory Access): An Overview"* (ACM Queue) — a concise, practical treatment of NUMA policy, first-touch, and the Linux memory-policy interfaces.
- The Linux documentation and man pages — `numactl(8)`, `numa(7)`, `mbind(2)`, `set_mempolicy(2)`, `libnuma`/`numa(3)`, and `Documentation/admin-guide/mm/numa_memory_policy` (the controls of §17.4.1) and `numa_balancing` sysctl.
- Intel, *64 and IA-32 Architectures Optimization Reference Manual* (multi-socket/uncore sections) and the UPI/offcore-response performance events used to attribute local vs remote DRAM (§17.3); AMD's equivalents for Infinity Fabric.
- A. Yasin, *"A Top-Down Method for Performance Analysis"* (Ch. 2) — where remote-memory stalls appear in the memory-bound back-end breakdown.

## 17.8 Additional Reading

- D. Bakhvalov, *Performance Analysis and Tuning on Modern CPUs* — NUMA case studies and reading local/remote DRAM counters with `perf`.
- The `numactl`/`numastat` and `lstopo` (hwloc) documentation — discovering and visualizing node topology, core/device-to-node mapping (Ch. 6).
- Ch. 28 (*Custom Allocators*) — NUMA-aware per-node arenas; Ch. 37 (*False Sharing*) — the cross-node coherence cost in depth; Ch. 49 (*Thread & Interrupt Pinning*) — the affinity machinery this chapter depends on; Ch. 77 (*MPI*) — topology-aware placement at cluster scale.
- **Appendix C** (System Tuning Checklist) — NUMA/snoop-mode firmware settings and `numactl` pinning consolidated (`numa_balancing` is stated in §17.5); **Appendix E** — the local vs remote DRAM latency numbers that frame "the cliff."

---

*Next: Ch. 18 — Timekeeping: TSC, rdtsc & Clock Sources, where we close Part II by fixing the instrument we've been using all along: how to read nanoseconds reliably with the invariant TSC and `rdtscp`, calibrate it, and avoid the clock-source pitfalls that corrupt every measurement in this book.*
