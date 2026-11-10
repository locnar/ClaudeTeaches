# Part VII — OS, Scheduling & Isolation

# Chapter 57 — Cache Allocation Technology & Intel RDT

> **Prerequisites:** Ch. 8–10 (the memory hierarchy and why an L3 eviction costs a DRAM round-trip), Ch. 18 (NUMA), Ch. 55–56 (pinning and SMT — a dedicated core; this chapter isolates its *cache*, the piece core-pinning alone can't), Ch. 43 (contention on shared cache lines). Ch. 58 (`isolcpus`/`nohz_full`/RT) and Appendix C come *after* this chapter and are not needed first — this chapter assumes only a pinned core.
>
> **Leads into:** Ch. 74 (DDIO — which lands NIC data in the very L3 region this chapter learns to partition), Ch. 58 (RT scheduling & kernel tuning — the kernel-side isolation of the same core), Appendix C (add these knobs to the quiet-box checklist). The isolation step beyond core pinning (Ch. 55) and SMT control (Ch. 56): partitioning the shared last-level cache so a noisy neighbor can't evict the hot working set even when the core is dedicated.

---

## 57.1 Why it matters: pinning the core isn't enough if the cache is shared

Ch. 55–56 gave the hot thread a *dedicated core* — pinned, IRQs steered away, the SMT sibling idle — and Ch. 58 will quiet it further with `isolcpus`, `nohz_full` and RT priority. But a pinned core is not an *isolated* core, because the one resource it still shares with every other core on the socket is the **last-level cache (L3)**. On Skylake-SP and later Xeons — the Ice Lake-SP reference machine included — the L3 is a shared, **non-inclusive** victim-style cache distributed across per-core slices (a snoop filter tracks what the private L2s hold). What the hot path keeps in its private 1.25 MiB L2 largely survives neighbor traffic; what *spills* to L3 — the order book that doesn't fit L2, the cold half of the decode tables — is in the commons. So the housekeeping thread on core 1, the logging consumer (Ch. 95) on core 0, a capture writer (Ch. 105), even a stray `cron` job (Ch. 58) — anything running anywhere on the socket — allocates into the *same* L3 your hot path depends on, and can **evict your hot working set** (the order book, the strategy state, the decode tables — Ch. 32). That eviction is invisible to `taskset`: the core is yours, but the cache underneath it is a commons, and a noisy neighbor turns an L3 hit (~15–20 ns, Ch. 8) into a DRAM miss (~80–100 ns) at exactly the wrong moment (Ch. 1).

This is the last, quietest source of tail latency on an otherwise-tuned box, and it is precisely the kind of jitter that *only* shows up under production load — when the housekeeping cores are actually busy — and never in a quiet lab (Ch. 115's test-vs-prod drift). The symptom is maddening: p50 is perfect, but p99.9 shows the hot path occasionally paying DRAM latency for data that *should* be resident, because something on another core reached into the shared L3 and knocked it out. Core isolation gave you the compute; it did nothing for the cache.

**Intel RDT (Resource Director Technology)** — and its AMD analog (Platform QoS / L3 QoS) — is the hardware feature that closes this gap. It lets you **partition the shared L3** (and memory bandwidth) between groups of cores, so the hot core gets a dedicated slice of cache that no housekeeping thread can evict, plus **monitoring** so you can *see* who is occupying the cache and consuming bandwidth. Combined with core pinning (Ch. 55) it completes the isolation: the hot path gets a private core *and* a private chunk of L3. This chapter explains the RDT family and the `resctrl` interface (§57.2), measures a noisy neighbor evicting the hot working set and the isolation that fixes it (§57.3), shows how to partition cache and bandwidth in practice (§57.4), and warns about the ways cache partitioning backfires (§57.5). It's the cache half of "give the hot path a quiet machine."

## 57.2 Mental model: RDT, CLOS, capacity bitmasks, and `resctrl`

**Intel RDT is a family of cache/bandwidth partitioning and monitoring features**, controlled through a single kernel interface. The pieces:

- **CAT (Cache Allocation Technology)** — partitions the **L3** (and on some parts, L2) by *ways*. The L3 is N-way associative; CAT lets you assign each *class of service* a **capacity bitmask (CBM)** — a bitmap of which cache ways that class may allocate into. Give the hot class a set of ways nobody else can touch, and its lines are never evicted by another class.
- **CDP (Code/Data Prioritization)** — an extension of CAT that splits the mask into *code* and *data*, so you can protect the hot path's instructions (Ch. 14 I-cache pressure) separately from its data.
- **MBA (Memory Bandwidth Allocation)** — throttles the **memory bandwidth** a class may consume (as a percentage / delay value), so a bandwidth-hungry neighbor (a capture writer streaming to disk, Ch. 105; a `memcpy`-heavy batch job) can't saturate the memory controller and starve the hot path (Ch. 18's bandwidth is shared too).
- **CMT (Cache Monitoring Technology)** and **MBM (Memory Bandwidth Monitoring)** — the *monitoring* half: per-class L3 **occupancy** (how many bytes of L3 a group currently holds) and memory **bandwidth** (local and total). You can't manage what you can't measure (Ch. 2); CMT/MBM show you exactly who is in the cache.

The organizing abstraction is the **CLOS (Class of Service)**: a numbered group. You assign each CLOS a CBM (which ways it may use) and/or an MBA throttle, then assign *cores* (or *tasks*) to a CLOS. Cores in CLOS 1 allocate only into CLOS 1's ways.

The Linux interface is the **`resctrl` pseudo-filesystem** (`/sys/fs/resctrl`), mounted like `cgroup`:

```
   /sys/fs/resctrl/
     schemata            # default CLOS0, two LINES (one per resource):  L3:0=0ff   <- shrunk to the complement of hotpath's ways (§57.5)
                         #                                                MB:0=100   (';' separates cache IDs/sockets WITHIN a line, e.g. L3:0=0ff;1=0ff)
     tasks / cpus        # who is in CLOS0 (cpus is a HEX MASK; cpus_list takes a CPU list)
     info/L3/            # num_closids, cbm_mask, min_cbm_bits, shareable_bits, sparse_masks
     info/L3_MON/        # monitoring: num_rmids, mon_features, max_threshold_occupancy
     info/last_cmd_status  # why the last write was rejected
     mon_data/           # per-group occupancy & bandwidth counters
     hotpath/            # a group YOU create (a CLOS)
       schemata          #   L3:0=f00   (the top 4 of 12 ways = 8 MiB, exclusively)
                         #   MB:0=100
       cpus_list         #   the hot core(s), e.g. "2"
       mon_data/         #   this group's L3 occupancy / BW
```

One subtlety to carry into §57.3: CAT constrains *fills* (which ways a CLOS may allocate into), not *hits* — a line already resident in another CLOS's ways stays hittable until it is evicted from there, so a freshly re-partitioned box converges over the first few seconds of traffic, and occupancy (CMT) is the thing to watch while it does.

The model to hold: **a CLOS is a lease on cache ways and bandwidth; you give the hot path an exclusive lease and everyone else a *different, non-overlapping* lease.** Two CLOSes whose bitmasks don't overlap cannot evict each other's lines — that's the isolation. Overlapping masks share ways (and defeat the purpose — §57.5). AMD's equivalent (L3 QoS / QoS Enforcement) exposes a very similar `resctrl` interface, so the mechanics transfer with different capacity granularity.

## 57.3 Measure it: a noisy neighbor vs a partitioned cache

*Toolchain and machine: Appendix L.3.*

The effect to measure is **hot-path tail latency with and without a cache-thrashing neighbor, before and after partitioning.** Set up: the hot thread on an isolated core (Ch. 55) doing repeated lookups over a working set sized to fit L3 (an order book, Ch. 32); a "noisy neighbor" on another core streaming a large buffer (bigger than L3) to evict everything. Measure the hot path's latency distribution (HdrHistogram, Ch. 3) and its L3 miss rate (`perf stat -C <hot> -e LLC-loads,LLC-load-misses,mem_load_retired.l3_miss`, Ch. 2) in three conditions; the partition for the third is the literal `mkdir /sys/fs/resctrl/hotpath; echo 'L3:0=f00' > /sys/fs/resctrl/hotpath/schemata; echo 'L3:0=0ff' > /sys/fs/resctrl/schemata; echo 2 > /sys/fs/resctrl/hotpath/cpus_list` of §57.4.1. (The benchmark source — a hot lookup loop plus a streaming neighbor thread under Appendix L's harness — is to be added with the real runs.) Representative shape (single-socket Xeon Gold 6326, Ice Lake-SP, 24 MiB L3 — 1.5 MiB/core × 16, 12-way, 2 MiB per way; figures pending real runs):

| Condition | Hot-path L3 miss rate | p50 | p99.9 | Notes |
|---|---|---|---|---|
| **Hot path alone** (quiet box) | ~0.2% | ~0.9 µs | ~1.1 µs | the ideal — working set resident in L3 |
| **+ noisy neighbor, no CAT** | ~18% | ~1.0 µs | ~**4.8 µs** | neighbor evicts the working set; tail explodes with DRAM misses |
| **+ noisy neighbor, CAT partitioned** | ~0.3% | ~0.9 µs | ~1.2 µs | hot path's ways are private; neighbor can't evict — tail restored |

And confirm with **CMT occupancy**: watch `mon_data/.../llc_occupancy` for the hot group and the neighbor group. Without CAT, the neighbor's occupancy balloons and the hot group's shrinks (it's being evicted). With CAT, each group's occupancy stays bounded by its assigned ways — you can *see* the partition holding.

The lessons:

- **A noisy neighbor is a p99.9 weapon that pinning doesn't stop.** The median barely moves (the working set is usually resident), but the *tail* — the lookups that hit a just-evicted line — pays full DRAM latency (Ch. 8). This is the exact profile of a jitter source that passes a quiet-lab test and fails in production (Ch. 115).
- **CAT restores the tail by making eviction impossible, not merely unlikely.** With non-overlapping ways, the neighbor *cannot* touch the hot path's lines — it's a hard partition, not a heuristic. The tail returns to the quiet-box baseline.
- **Measure occupancy, not just latency (CMT).** Occupancy tells you *why* — it shows the neighbor eating the cache and the partition stopping it. It also tells you if you sized the hot partition right (§57.5): if the hot group's occupancy is pinned at its ceiling and its miss rate is still high, it needs more ways.
- **MBA matters when the neighbor is bandwidth-bound, not cache-bound.** If the neighbor streams (capture to disk, Ch. 105), it may not evict much but can saturate memory bandwidth (Ch. 18), stalling the hot path's *own* misses. MBA throttling the neighbor's bandwidth is the fix there — measure with MBM.

## 57.4 Techniques

### 57.4.1 Partitioning the L3 with CAT via `resctrl`

The core technique: carve the L3 so the hot path owns a private set of ways.

- **Probe first, then mount.** `grep -o 'cat_l3\|cdp_l3\|mba\|cqm_llc\|cqm_mbm_total\|cqm_mbm_local' /proc/cpuinfo | sort -u` tells you which RDT features the CPU exposes (`cat_l3` for CAT, `cdp_l3` for CDP, `mba`, `cqm_*` for CMT/MBM); the kernel needs `CONFIG_X86_CPU_RESCTRL` (the `rdt=` command-line parameter forces features on/off), and CDP needs `mount -o cdp`. Then `mount -t resctrl resctrl /sys/fs/resctrl`; read `info/L3/cbm_mask` (the full mask — `fff` = 12 ways on the Xeon Gold 6326; a 20-way part shows `fffff`) and `info/L3/min_cbm_bits` (minimum contiguous ways per allocation) and `info/L3/num_closids` (how many CLOSes you have).
- **Create a hot group and give it exclusive ways.** `mkdir /sys/fs/resctrl/hotpath`; write a bitmask to its `schemata` that reserves the ways *only it* uses, e.g. `L3:0=f00` (4 ways = 8 MiB of the 6326's 24 MiB). Then **shrink the default group** (`CLOS0`, everything else) to the *complementary* ways: `L3:0=0ff` — critically, the default mask must **not overlap** the hot mask, or the isolation is void (§57.5). Bitmasks in CAT must be **contiguous** (a single run of 1-bits — Intel parts with `info/L3/sparse_masks` = 0, which includes all Ice Lake-SP, reject anything else with `EINVAL`), which is why the hot ways go at one *end* of the mask so that both masks stay contiguous.
- **Assign the hot core(s).** Write the hot CPU(s) to the group — `cpus` takes a **hexadecimal bitmask** (`echo 4 > hotpath/cpus` for CPU 2; `echo 5` would mean CPUs 0 *and* 2), so prefer the list form `echo 2 > hotpath/cpus_list` — for CPU-based assignment (everything on that core uses the hot CLOS), or write the hot thread's TID to `hotpath/tasks` (task-based). CPU-based is simplest for a dedicated hot core (Ch. 55). A rejected write says why in `info/last_cmd_status`.
- **Size the partition to the working set.** Give the hot group *just enough* ways to hold its working set (order book + strategy state + decode tables — Ch. 32), measured via CMT occupancy (§57.3). Too few ways and the hot path evicts *itself* (§57.5); too many and you starve everyone else, which can bounce back as bandwidth pressure. Right-size with occupancy monitoring, don't guess.
- **CDP for code/data split (optional).** If the hot path is also front-end-bound (Ch. 14), enable CDP and give its *code* ways protection from its own data streaming, keeping hot instructions resident.

### 57.4.2 Throttling neighbors with MBA; monitoring with CMT/MBM

Cache partitioning handles *capacity*; bandwidth and monitoring complete the picture.

- **MBA to cap a bandwidth-hungry neighbor.** Put the streaming housekeeping work (capture writer — Ch. 105, log flusher — Ch. 95, batch jobs) in a CLOS with an MBA throttle: `MB:0=20` (limit to ~20% of memory bandwidth). This stops a `memcpy`-heavy neighbor from saturating the memory controller (Ch. 18) and stalling the hot path's own DRAM accesses. MBA is a coarse throttle (delay-based, per-core); measure its effect with MBM, don't assume the percentage is exact.
- **Monitor continuously (CMT/MBM) in production.** Create monitoring groups and poll `mon_data/mon_L3_XX/llc_occupancy` and `.../mbm_total_bytes` — this is the production-safe observability (Ch. 110 — sampled by the same off-core scraper as the application's telemetry, Ch. 97) for the cache: it shows, live, who occupies the L3 and who consumes bandwidth. Alert when the hot group's occupancy dips (something is stealing its ways — a misconfiguration or a new neighbor) or when a neighbor's bandwidth spikes.
- **Combine CAT + MBA + pinning as one policy.** The full isolation recipe: pin the hot thread to an isolated core (Ch. 55), give that core a CLOS with exclusive L3 ways (CAT), and put *all other* work in CLOSes with complementary ways and MBA throttles. Bake it into the box's startup (Appendix C) alongside `isolcpus`/IRQ affinity, and verify with CMT/MBM in the readiness check (Appendix M).

### 57.4.3 Partitioning for DDIO and multi-tenant boxes

Two higher-order uses that connect forward:

- **Keep the hot ways disjoint from the NIC's DDIO region (ties Ch. 74).** DDIO lands received packets directly in a slice of L3 (Ch. 74). By default that DDIO region can *conflict* with your hot working set — the NIC's incoming data evicts your order book, or vice versa. On Intel parts up to and including Ice Lake-SP `resctrl` **cannot assign ways to the NIC**: DDIO ignores CLOS and writes into a fixed set of ways (two by default) chosen by the platform (the IIO LLC WAYS MSR / BIOS, which firmware may lock — Ch. 74). What CAT *can* do is keep the *core* groups' masks away from those ways: read `info/L3/shareable_bits` (the ways the hardware may share with I/O) and give the hot group a mask disjoint from it — developed in Ch. 74.
- **Cache pseudo-locking (the most HFT-relevant RDT trick).** `resctrl` can *lock* a working set into a cache region: create a group, write `pseudo-locksetup` to its `mode`, give it a schemata of ways no other group uses, and the kernel pre-loads a memory region into those ways and exposes it as a character device (`/dev/pseudo_lock/<group>`) that you `mmap`; because no CLOS may allocate into the locked ways, the lines are never evicted — an L2/L3-resident buffer for the order book or decode tables by construction (Ch. 61's warming made permanent). Requires `pseudo_lock` in `info/L3/features`, a kernel built with it, and a contiguous mask; read `Documentation/filesystems/resctrl.rst` (§"Cache Pseudo-Locking") before relying on it.
- **Multi-tenant / multi-strategy boxes.** When several strategies (or shards — Ch. 100) share a socket, CAT partitions the L3 *between* them so one strategy's working set can't evict another's — cache-level fault isolation to match the process-level isolation of Ch. 100. Each strategy gets a CLOS sized to its footprint.

## 57.5 Pitfalls & anti-patterns: over-partitioning and overlapping masks

- **Overlapping bitmasks (the cardinal error).** Giving the hot group ways `f00` but leaving the default group at `fff` (which *includes* those ways) means everyone still allocates into the hot ways — you've partitioned nothing. The default/other CLOSes must be shrunk to the **complement** of the hot mask so the masks are **disjoint**. Verify every CLOS's schemata; overlap silently defeats the isolation (and the CMT occupancy will show it — the neighbor still eats the hot ways).
- **Over-partitioning — starving the hot path of its own cache.** Give the hot group too few ways and it can't hold its working set, so it evicts *itself* on every pass — you've manufactured the miss you were trying to prevent. Size to the working set via CMT occupancy (§57.3), with headroom; a partition smaller than the footprint is worse than no partition.
- **Starving everyone else into bandwidth pressure.** Give the hot group almost *all* the ways and the housekeeping cores thrash their tiny slice, miss constantly, and pound memory bandwidth (Ch. 18) — which bounces back as bandwidth contention on the hot path's own misses. Isolation is a balance: enough for the hot path, enough left for the rest. Watch total bandwidth (MBM).
- **Forgetting bandwidth (CAT without MBA).** A neighbor that streams (capture, Ch. 105) may occupy few *ways* but saturate *bandwidth* — CAT won't stop it, only MBA will. If the hot path's misses get slower under a streaming neighbor despite CAT, it's bandwidth, not capacity (§57.4.2).
- **Non-contiguous / invalid masks.** Most CAT implementations require **contiguous** bitmasks (a single run of 1-bits) and a minimum width (`min_cbm_bits`). A non-contiguous or too-narrow mask is rejected with `EINVAL` — resctrl never clamps — and the reason (`Mask not contiguous`, `Need at least N bits in the mask`, `Mask out of range`) is in `/sys/fs/resctrl/info/last_cmd_status`. Read `info/L3/` before writing masks.
- **Hyperthread and shared-L2 subtleties.** CAT partitions L3; two hyperthreads on a core still share L1/L2 (Ch. 56) regardless of CLOS — cache isolation between siblings needs L2 CAT (if supported) or, more simply, leaving the sibling idle (Ch. 56). Assigning two CLOSes to two siblings doesn't isolate their L1/L2.
- **AMD/Intel and generational differences.** Way counts, granularity, L2 CAT availability, and MBA semantics vary by CPU (and AMD's QoS differs in detail). Probe `info/` at runtime; don't hard-code masks for one part (this is the Appendix A portability discipline applied to RDT).
- **Not monitoring — flying blind.** Setting masks once and never checking CMT occupancy means you won't notice when a config drift, a new neighbor, or a working-set change breaks the isolation. Monitor in production (Ch. 115); the partition is a claim to *verify*, not a set-and-forget.

## 57.6 Exercises & checklist

**Exercises:**

1. **Reproduce the noisy neighbor.** Run a hot lookup loop (working set ~half L3) on an isolated core and a buffer-streaming neighbor on another core. Measure the hot path's p99.9 and L3 miss rate (Ch. 2/3) with the neighbor on and off — reproduce the §57.3 tail explosion.
2. **Partition and restore.** Create a `resctrl` hot group with exclusive ways, shrink the default group to the complement, assign the hot core, and re-measure — show the tail returns to baseline and the miss rate drops. Verify the masks are disjoint.
3. **Right-size with CMT.** Poll `llc_occupancy` for the hot group while shrinking its ways one at a time; find the point where occupancy hits the ceiling and the miss rate climbs — that's the working-set size in ways. Set the partition just above it.
4. **Bandwidth vs capacity.** Replace the cache-thrashing neighbor with a bandwidth-streaming one (large sequential `memcpy`). Show CAT alone doesn't fully fix it, then add an MBA throttle and re-measure (MBM) — isolate the bandwidth effect from the capacity effect.
5. **DDIO preview.** Read Ch. 74, then reserve a CAT region for the NIC's DDIO writes disjoint from the hot data ways; measure whether separating them reduces hot-path tail under heavy RX (this is the §57.4.3 / Ch. 74 combined technique).

**Checklist:**

- [ ] The hot core has a **CLOS with exclusive L3 ways** (CAT), and **all other CLOSes use the disjoint complement** — masks verified non-overlapping (§57.4.1, §57.5).
- [ ] The hot partition is **sized to the working set** via **CMT occupancy** — enough ways to stay resident, with headroom, not starving itself (§57.4.1, §57.5).
- [ ] Enough cache is **left for housekeeping** that it doesn't thrash into bandwidth pressure; total bandwidth watched (MBM) (§57.5).
- [ ] **Bandwidth-hungry neighbors** (capture, logging, batch) are **MBA-throttled** so they can't saturate the memory controller (§57.4.2).
- [ ] **CMT/MBM monitoring runs in production** (Ch. 115) — occupancy and bandwidth per group, alerting on the hot group losing ways or a neighbor spiking (§57.4.2, §57.5).
- [ ] Masks respect the part's **contiguity/min-width** rules and are **probed at runtime** (`info/L3/`), not hard-coded across CPU generations (§57.5).
- [ ] The **CAT/MBA policy is baked into box startup** alongside the Appendix C pinning/isolation knobs, and verified in the readiness check (Appendix M); combined with the DDIO region separation of Ch. 74 where a NIC is on the hot path (§57.4.3).

## 57.7 References

- The **Intel RDT** documentation and the *Intel 64 and IA-32 Architectures Software Developer's Manual* (CAT/CDP/MBA/CMT/MBM chapters) — the authoritative feature reference (§57.2).
- The Linux kernel **`resctrl`** documentation (`Documentation/filesystems/resctrl.rst` on Linux 6.16+; `Documentation/arch/x86/resctrl.rst` on 6.5–6.15, `Documentation/x86/resctrl.rst` earlier) — the interface, schemata syntax, and monitoring groups (§57.2, §57.4).
- The `intel-cmt-cat` toolkit (`pqos`) documentation — a userspace tool for CAT/MBA/monitoring, an alternative to raw `resctrl` (§57.4).
- The **AMD64 QoS Extensions** (Platform QoS / L3 QoS Enforcement) documentation — the AMD analog and its `resctrl` support (§57.2, §57.5).
- Ch. 8–10 (cache hierarchy — what CAT protects), Ch. 18 (memory bandwidth — what MBA governs), Ch. 55, 56, 58 (core isolation this completes), Ch. 74 (DDIO — the combined technique), Appendix C (tuning checklist).

## 57.8 Additional Reading

- Intel and vendor whitepapers on cache QoS / RDT for network-function and low-latency workloads — real deployments of CAT/MBA for tail-latency isolation (§57.3).
- Talks on cache partitioning in HFT and latency-sensitive systems — the noisy-neighbor problem and CAT as the fix (§57.1, §57.3).
- The `pqos`/`intel-cmt-cat` examples and the resctrl kernel test suite — practical CLOS/mask configuration patterns (§57.4).
- Ch. 74 (*DDIO*) — separating the NIC's cache region from the hot data region; Ch. 100 (*Process Topology*) — per-strategy cache isolation; **Appendix C** — the quiet-box checklist; **Appendix E** — the L3-vs-DRAM latencies that make this matter; **Appendix F** — RDT/CAT/CLOS glossary.

---

*Next: Ch. 58 — Real-Time Scheduling & Kernel Tuning, the kernel side of isolating the core this chapter just gave a private cache: `isolcpus`, `nohz_full`, RT priorities, cgroups, and `cyclictest` — turning a pinned, cache-partitioned core into a jitter-free one.*
