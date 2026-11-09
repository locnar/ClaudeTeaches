# Appendix O — Hot-Path Anti-Pattern Catalogue

> **Consolidates:** every chapter's *Pitfalls & anti-patterns* section (§N.5 or §N.6 of Ch. 1–99), read and re-sorted by **where the sin lives** rather than by chapter, with the one column no chapter provides in aggregate: **the counter, probe or signature that exposes it**. The probe invocations are Appendix I's; the fixes point at the section that develops them. Companion to Appendix C (tuning knobs), D (flags), E (latency numbers), I (commands) and M (the readiness gate); the pre-merge review checklist Ch. 99 §99.4.1 asks for.

**How to use this:** two ways. *As a reviewer*, read the left column of the sections that touch the change (a new queue → O.5; a new container → O.1 and O.8; a new thread → O.4 and O.6) and ask whether each row applies. *On call*, start from the **symptom** you have — a p99.9 spike every ~N minutes, a flat median with a long tail, throughput that fell when you added a core — find it in the second column, run the probe in the third, and follow the pointer. Each row is one line: **anti-pattern → how it shows up → what exposes it → the fix → where the book develops it**. The "exposes it" column names the cheapest *discriminating* evidence: a counter that moves for this pattern and not for its neighbours. Rows added by the design-pattern chapters (Ch. 41, 42, 47, 90, 91, 95) are marked ◆.

---

## O.1 Allocation, page faults and memory lifetime

| Anti-pattern | Symptom | What exposes it | Fix | Where |
|---|---|---|---|---|
| Hidden allocation on the "allocation-free" hot path (`std::string` temp, `std::function` capture, `vector` growth, `shared_ptr` copy) | p99.9 spikes with no pattern; occasional µs stalls | `perf probe -x libc malloc` / `bpftrace -e 'uprobe:libc:malloc /pid==$1/ { @[ustack]=count() }'` on the hot thread: must be zero after warm-up; `perf stat -e page-faults` non-zero in steady state | Pools/arenas sized at start-up; `reserve`; SBO under the threshold; allocation-counting test in CI | Ch. 26 §26.5, 24 §27.5, 25 §28.5 |
| Un-warmed memory faulting on first touch (fresh `mmap`, THP collapse, lazy stack) | the *first* message after start/idle is 10–100× slower; `minor-faults` ticks during the session | `perf stat -e minor-faults,major-faults -p <pid>`; `/proc/<pid>/stat` fields 10/12 moving; `perf trace -e 'exceptions:page_fault_user'` | Pre-fault and `mlock`/`mlockall`, `MAP_POPULATE`, touch the slab at start-up, THP `madvise` not `always`, warm the stack | Ch. 26 §26.5, 26 §29.5, 15 §15.5, 50 §53.5 |
| THP compaction / `khugepaged` collapse stalls | periodic multi-ms stalls across *all* threads | `/proc/vmstat` `compact_stall`, `thp_collapse_alloc`; `perf trace -e compaction:*`; `sched_switch` to `khugepaged` in the ftrace ring | THP `madvise` + explicit hugetlbfs for the hot segments; `defrag` off; pre-allocate huge pages at boot | Ch. 15 §15.5, Appendix C |
| Use-after-free across an arena reset or pool reuse (silent: the slot is reused, not unmapped) | wrong values, not crashes; a fill booked against the wrong order | ASan on a replay (never in production); debug clear-on-free poison (`0xDD`) appearing in a field; stale-handle counter non-zero | Generational handles / slot maps; poison-on-free in debug; outstanding-view accounting | Ch. 27 §27.5, 25 §28.4.4 ◆, 36 §39.5, 38 §41.4.5 ◆, 84 §87.4.2 |
| Pointers into a flat/rehashing container or across a shared segment | dangling after growth; a pointer from another address space | ASan (replay); `MAP_SHARED` segment mapped at a different base in a test process | Indices/handles with a generation; offsets from the segment base; `reserve` so growth never happens on-path | Ch. 28 §28.5, 54 §57.5, 26 §29.5, 86 §89.5 |
| Silent `malloc` fallback on pool exhaustion | the tail appears only at the peak (the open) | pool high-water telemetry; `malloc` uprobe at the open | Hard cap with a deliberate reject policy; size from captured peaks | Ch. 27 §27.5, 39 §42.2.1 ◆ |
| Coroutine frame / `std::execution` op-state allocated per invocation | `malloc` per message in "async" code | `malloc` uprobe; `-fsanitize=address` alloc counts; check HALO in the asm | Verify frame elision in the asm; preallocate op-states; keep coroutines off the critical path | Ch. 43 §43.6, 41 §44.6 |
| Giant stack frames / unbounded stack use (large locals, recursion, `alloca`) | a stack-guard page fault at the worst moment; a `SIGSEGV` on a sigaltstack-less thread | `-fstack-usage`; `perf stat -e page-faults` on the first deep call; `ulimit -s` experiments | Bound frames; pre-touch the stack; `sigaltstack` per thread | Ch. 26 §26.5, 89 §93.5 |

## O.2 Dispatch, type erasure and abstraction cost

| Anti-pattern | Symptom | What exposes it | Fix | Where |
|---|---|---|---|---|
| `std::function` / megamorphic `virtual` in the inner loop | indirect-branch mispredicts; a `call [rax]` per message | `perf stat -e branch-misses,br_misp_retired.indirect`; `perf annotate` showing `call *%rax`; `objdump` of the hot loop | Templates/CRTP, `variant`+`visit` for closed sets, function pointers for one-level dispatch, `final` + devirtualization verified | Ch. 14 §14.6, 19 §19.6, 25 §28.5 |
| "Zero-cost" abstraction that leaked (a missed inline, a copy, a hidden `call`) | the abstracted version is slower than hand-written; extra moves in the asm | `diff` the asm of abstracted vs hand-written (Godbolt / `objdump -d`); `perf annotate` | Verify the diff is empty; `[[gnu::always_inline]]` where justified; fix the copy | Ch. 19 §19.5, 19 §19.6, 4 §4.5 |
| Throwing on a frequent condition; `dynamic_cast` on the hot path | µs-scale spikes on "error" messages; `__cxa_throw` in the profile | `perf record` + `__cxa_throw`/`_Unwind_RaiseException` in the report; `bpftrace -e 'uprobe:libstdc++:__cxa_throw { @=count() }'` | `std::expected`/error codes on the hot path; exceptions for truly exceptional, off-path conditions; `noexcept` moves | Ch. 21 §21.6 |
| Non-`noexcept` move constructor → silent copies in containers | unexplained bandwidth/time on `vector` growth or `swap` | `static_assert(std::is_nothrow_move_constructible_v<T>)`; the asm shows `memcpy` of whole objects | `noexcept` on moves; `static_assert` in CI | Ch. 21 §21.6 |
| Type-erased senders/coroutine tasks defeating elision | allocation + indirect call where a monomorphic pipeline was intended | `malloc` uprobe; `perf annotate` for indirect calls in the pipeline | Keep the pipeline's types concrete; erase only at the boundary | Ch. 43 §43.6, 41 §44.6 |
| Policy/CRTP used where runtime selection was needed, or runtime selection per call where policies would do | a `switch` per message that is always the same case; or combinatorial template bloat | `perf stat -e L1-icache-load-misses`, `size`/`bloaty` growth; the switch in `perf annotate` | Select the instantiation once at start-up; keep the hot path monomorphic | Ch. 19 §19.6, 14 §14.6 |
| Strong types/units missing: price + quantity, ticks where scaled price expected | wrong orders, not slow ones | a unit test cannot catch what the type system should; review for raw `int64` arithmetic on prices | `Price`/`Qty`/`Notional` wrappers with an allowed-operations matrix; `enum class` ids | Ch. 19 §19.4.4 ◆, 27 §30.6 |

## O.3 Control flow, data dependencies and memory ordering

| Anti-pattern | Symptom | What exposes it | Fix | Where |
|---|---|---|---|---|
| `seq_cst` everywhere (over-strong default) | `mfence`/`lock xchg` per store; store-buffer stalls | `objdump` shows `mfence`/`xchg` on plain publication stores; `perf stat -e cycle_activity.stalls_mem_any` | `release`/`acquire` where publication is the intent; `relaxed` for counters | Ch. 33 §33.6 |
| `relaxed` where publication was needed; wrong seqlock/ring ordering | works on x86, breaks on ARM; torn snapshots under load | TSan (Ch. 45) on the replay; a model checker (CDSChecker/herd) on the protocol; an ARM CI run | Acquire/release pairs; a vetted ring/seqlock; test on weak memory | Ch. 33 §33.6, 34 §37.5, 35 §38.5, 37 §40.5, Appendix A |
| Data race treated as "probably fine"; `volatile` as an ordering tool | heisenbugs; the optimizer "breaks" it at `-O2` | TSan; `-O2` vs `-O0` behaviour difference | `std::atomic` with the right ordering; never `volatile` for sync | Ch. 33 §33.6, 5 §5.5 |
| Serial dependency chain (single accumulator, `div`/`sqrt` in the chain, `cmov` lengthening the critical path) | IPC far below the port limit; "unrolling didn't help" | top-down: Backend-Bound/Core-Bound with low port utilization; `perf stat -e uops_issued.any,cycles` | Multiple accumulators; reciprocal multiply; break the chain; verify the asm | Ch. 11 §11.6, 13 §13.6, 27 §30.6 |
| Branchless code that is slower (cmov on a predictable branch; a lookup table that misses cache) | `cmov` version slower than the branch; L1d misses on a "fast" table | `perf stat -e branch-misses` (near zero → the branch was fine); `mem_load_retired.l1_miss` on the table | Keep predictable branches; tables that fit L1; measure both | Ch. 13 §13.6 |
| Mis-hinting `[[likely]]`/`[[unlikely]]`; optimizing branches when not Bad-Speculation-bound | no change, or code layout got worse | top-down level 1: Bad-Speculation share before and after | Hint only from a profile; PGO | Ch. 13 §13.6, 12 §12.6 |
| Branch chain instead of a bitmask in a validation gate (risk checks) ◆ | each reject mispredicts; the all-pass path pollutes the predictor | `perf stat -e branch-misses` scaled with reject rate | Accumulate `r \|= (cond) * BIT`, one branch on the verdict | Ch. 90 §90.2.3, §90.5 |
| Reading the clock inside the deterministic core; timers as callbacks ◆ | replay digest diverges on the first run | Ch. 89 §89.4.6 digest compare live vs replay; `grep rdtsc\|clock_gettime` in core code | Time as an input (`now` stamped once per iteration); timer fires as events | Ch. 89 §89.5, 44 §47.5, 91 §95.5 |
| Floating-point fold / hash-map iteration order inside a replicated fold ◆ | replicas with different histories disagree | replica digest comparison at equal `seq` | Integer state or fixed reduction order; flat arrays; one binary | Ch. 95 §95.5, 27 §30.6, 29 §32.6 |

## O.4 Sharing, contention and false sharing

| Anti-pattern | Symptom | What exposes it | Fix | Where |
|---|---|---|---|---|
| Adjacent hot counters / per-thread array without padding; ring head and tail on one line | "it doesn't scale"; 3–5× slower than expected ring | `perf c2c record` → HITM on the same line from two cores; `perf stat -e mem_load_l3_hit_retired.xsnp_hitm` | `alignas(hardware_destructive_interference_size)` per writer; separate lines for head/tail | Ch. 36 §36.5, 34 §37.5, 37 §40.5, 81 §84.6 |
| A shared counter / `shared_ptr` refcount bumped from many cores | a "pointer copy" costing 50–100 ns | `perf c2c`; `lock xadd` hot in `perf annotate` | Per-thread counters aggregated off-path; pass by reference; single-writer | Ch. 28 §28.5, 31 §34.5, 83 §86.6 |
| Spinning while the holder is preempted; spin without `pause`; lock convoy | ms stalls under load; CPU at 100 % with no progress | `perf sched latency`; `sched_switch` on the lock holder's core; `perf stat -e cycles` vs instructions | Pinned, isolated holders; `pause`; shared-nothing instead of a lock | Ch. 35 §35.5, 43 §46.5 |
| Multiple writers where single-writer would do (two shards into one SPSC, two config writers, strategy pre-booking position) ◆ | corrupted indices, hangs, racing accumulators | TSan; a stale/duplicate counter that should be zero; the hang itself | One ring per writer or MPSC with per-slot publication; one writer per accumulator | Ch. 34 §34.5, 35 §38.5, 38 §41.5, 85 §88.5, 87 §90.5, 88 §91.5 |
| Cross-node sharing / first-touch on the wrong thread | remote-DRAM latency on "local" data | `perf stat -e offcore_response.*.remote_dram`; `numastat -p <pid>`; `perf mem` | First-touch on the owning thread; `numactl --membind`; per-node replicas of read-mostly data | Ch. 16 §16.5, 15 §15.5 |
| Two hot threads on SMT siblings; housekeeping on the sibling | the hot thread's IPC halves when the other runs | `thread_siblings_list` vs the pins; `perf stat` per sibling; the CPU-wide top-down | Leave the sibling empty or HT off | Ch. 49 §49.5, 45 §48.5 |
| A lock the hot path must take for config; mutating config in place | tear or stall at reload time | TSan; a config-version counter changing mid-message in the journal | RCU/seqlock-published snapshots; validate-then-swap | Ch. 88 §88.5, 87 §90.5 |

## O.5 Queues, pipelines, flow control and timers

| Anti-pattern | Symptom | What exposes it | Fix | Where |
|---|---|---|---|---|
| A thread per concern; staging onto the critical path "to parallelize" ◆ | hop time ≫ work time; tails compounding across cores | Ch. 85's per-stage stamps: time between stages > time in stages; `hops.cpp`-style RTT | Run-to-completion within a shard; stage only off-path consumers or fault domains | Ch. 41 §41.2.2, §41.5 |
| Per-message hops in a burst (no cached indices, no consumer batching) ◆ | microburst → tens of µs of queueing in a 2-stage pipeline | `perf c2c` on the ring's index lines (one transfer per message); queue-depth telemetry | Batch at the consumer; cache the indices; publish once per iteration | Ch. 41 §41.3, 37 §40.4.2, 34 §37.5, 91 §95.5 |
| Polling sources in arrival order, uncapped ◆ | a kill command or an ack delayed by a whole market-data burst | control-message latency under a replayed burst (Ch. 94) | Fixed priority order with per-source batch caps | Ch. 41 §41.4.2 |
| Unbounded queue / "size the ring bigger" ◆ | no drops, but the strategy acts on 20 ms-old data | message *age* at point of use (Ch. 42 §42.4.3), not drop counters | Conflate state; lossless channels sized for the venue burst with fullness = incident | Ch. 42 §42.3, §42.5, 34 §37.5 |
| Ignoring `push`'s return value (drop-newest by omission) ◆ | the newest state lost, the oldest backlog kept | the `Fail` channel's drop counter; stale ages | Declare the policy per channel in the type; publish drops | Ch. 42 §42.4.1 |
| Conflating facts (fills, acks, trade prints); overwriting increments ◆ | missing position, unknown orders, under-reported volume | position vs drop-copy delta; `static_assert(Conflatable<T>)` failing | Facts on lossless channels; merge semantics for accumulators | Ch. 42 §42.4.2, 88 §91.5 |
| Torn reads from an overwrite ring; dirty bit cleared after the read ◆ | garbage ages (10¹⁵ µs), lost updates | an age histogram with impossible values; a lapped counter without a version check | Version word on the slot; clear-then-read | Ch. 42 §42.5, 35 §38.5 |
| Backpressuring the receive path; blocking a hot producer "briefly" ◆ | gaps manufactured on the wire for every consumer | feed gap counters rising while the NIC shows no drops | The receive path never waits; producers fail/overwrite/conflate | Ch. 42 §42.5, 58 §61.5 |
| Wrong Disruptor wait strategy / no wrap protection / allocating in the entry | burned shared core or added latency; overwritten unread slots | CPU time of a non-dedicated consumer; TSan on a 4-slot ring | Match wait strategy to core ownership; gate on the slowest consumer; POD entries | Ch. 40 §40.5 |
| `sleep_for`/`nanosleep`/`epoll_wait(timeout>0)` on a hot thread; a sleeping thread with default timer slack ◆ | 50 µs median lateness; context switches on the hot core | `/proc/<pid>/task/*/timerslack_ns` = 50000; `perf sched`; `cyclictest`/`rtla timerlat` | Hot threads poll a deadline queue; sleepers set `PR_SET_TIMERSLACK=1`, use `TIMER_ABSTIME`/`timerfd` | Ch. 47 §47.3, §47.5, 43 §46.5 |
| `SCHED_FIFO` + busy-poll on a non-isolated core (RT throttle) ◆ | a 38–50 ms stall once per second | `/proc/sys/kernel/sched_rt_runtime_us` = 950000; `sched_switch` to idle on the RT core every second | Isolated cores with the throttle disabled, or `SCHED_OTHER` spinning | Ch. 47 §47.3, 48 §51.5 |
| Unbounded timing-wheel cascade; `std::priority_queue` of cancellable timers; timer-id reuse ◆ | ms stall every 65 k ticks; tombstone-skipping polls; a cancelled guard disappearing | deadline-queue poll p99.9 and max-fires telemetry; stale-timer-id counter | Bounded cascade per poll; heap with position tracking; slot + generation ids | Ch. 47 §47.5 |

## O.6 Kernel, scheduling, syscalls and isolation

| Anti-pattern | Symptom | What exposes it | Fix | Where |
|---|---|---|---|---|
| Involuntary preemption / migration of the hot thread | µs–ms gaps; the thread shows up on another CPU | `perf sched`; `/proc/<pid>/task/*/stat` field 39 changing; `nr_involuntary_switches` in `/proc/<tid>/sched` | Pin, isolate (`isolcpus`/cpusets), `nohz_full`, RT policy where appropriate | Ch. 46 §46.5, 45 §48.5, 48 §51.5 |
| Syscalls on the hot path (`clock_gettime` per record, `write`, `epoll_wait`) | `syscalls:sys_enter_*` on the hot core during the session | `perf trace -p <pid>`; `bpftrace -e 'tracepoint:raw_syscalls:sys_enter /cpu==N/ { @[comm,args->id]=count() }'` | `rdtsc`; batched off-path I/O; kernel bypass or busy-poll modes | Ch. 46 §46.5, 81 §84.6, 83 §86.6 |
| Leftover IRQs / `irqbalance` / stray IPIs on the hot core | periodic 5–50 µs gaps | `/proc/interrupts` deltas on the isolated core; `rtla osnoise`; ftrace `irq_handler_entry` | IRQ affinity away; `irqbalance` off; `nohz_full`; audit `effective_affinity` | Ch. 48 §48.5, 49 §52.5, 48 §51.5 |
| `nohz_full` core with more than one runnable task (the tick comes back) | 1–4 ms periodic tick on a "tickless" core | `/proc/sched_debug` runnable count; ftrace `hrtimer_expire_entry` tick_sched_timer | One task per isolated core; housekeeping elsewhere | Ch. 51 §51.5, 49 §52.5 |
| Turbo/C-state/frequency variance; untuned firmware under a tuned kernel | run-to-run variance; a slow first message after idle | `turbostat`; `perf stat -e cycles,ref-cycles`; C-state residency | Fixed frequency, C1 cap, `performance` governor, BIOS settings per Appendix C | Ch. 6 §6.5, 17 §17.5, 48 §51.5 |
| Signals landing on the hot thread (a kill delivered by signal; unmasked hot thread) ◆ | a 2.5 KB sigframe and an interrupt on the hot core | ftrace `signal_deliver` on the hot CPU | Mask signals on hot threads; control words + control-ring events; `eventfd` only from a crash handler | Ch. 46 §46.5, 87 §90.5, 89 §93.5 |
| Busy-polling on a shared core; blocking on the hot path "to be nice" | starving neighbours, or a wake-up tail | CPU share of the hot core; `perf sched` wake-up latency | Dedicated core for busy-poll; block only on housekeeping threads | Ch. 46 §46.5, 32 §35.5 |
| The core dump that stalls the survivors; `SIGCHLD` as the kill trigger | survivors' max gap grows 20–50× during a dump; cancels wait for the dump | survivor latency during an induced crash; the dump's duration | `MADV_DONTDUMP` on the big segments, piped bounded handler on a housekeeping core, kill-switch handoff from the handler | Ch. 93 §93.5, 86 §89.5 |
| Monitors that are the noise (sampling too fast, on the sibling, with syscalls) | the tail appears when monitoring is on | `rtla osnoise` with monitors on/off; the monitor's own CPU | Housekeeping cores, bounded cadence, shared-memory scraping | Ch. 97 §97.5, 49 §52.5, 83 §86.6 |

## O.7 I/O, networking, logging and capture

| Anti-pattern | Symptom | What exposes it | Fix | Where |
|---|---|---|---|---|
| Forgetting `TCP_NODELAY` (Nagle + delayed ACK) | 40–200 ms stalls on small writes | `ss -ti` (`nagle`), a 40 ms mode in the RTT histogram | `TCP_NODELAY`; `TCP_QUICKACK`; one write per message | Ch. 58 §58.5, 51 §54.5 |
| Batching offloads (GRO/LRO, interrupt coalescing) left on; interrupt-driven RX on the hot path | tens of µs added per packet | `ethtool -k`/`-c`; NIC timestamp vs application timestamp gap | Offloads off for the feed NIC; busy-poll / bypass | Ch. 63 §63.5, 56 §59.5 |
| Undersized RX ring / slow consumer → market-open drops | `rx_missed`/`rx_no_buffer` at 09:30; sequence gaps | `ethtool -S` deltas; feed gap counter vs NIC drop counter | Ring sizing, steering to an isolated core, DDIO sizing, consumer batching | Ch. 60 §60.6, 60 §63.5, 61 §64.5, 94 §98.5 |
| Leaving the feed to RSS / steering to a non-isolated or wrong-NUMA core | locality loss; cross-core handoff per packet | `ethtool -x`; `perf c2c` on the RX path; `numastat` | Flow steering to the consumer's isolated core on the NIC's node | Ch. 65 §65.5, 16 §16.5 |
| Software timestamps for a wire measurement / cross-host comparison | ±µs–ms "latency" that is really clock error | `phc_ctl`/`pmc` offsets; compare SW vs HW stamps on one NIC | Hardware timestamps; PTP with HW stamping; `TAI` offset set | Ch. 66 §66.5, 17 §17.5, 65 §68.5, 90 §94.5 |
| Blocking on recovery; NAK storms; trading through an unrecovered gap | the hot path waits on a retransmit; a quiet feed mistaken for a quiet market | gap/recovery counters; dark-feed detector; `/proc/net/igmp` lies — use the querier | Recovery off-path; per-instrument stale flags; membership health | Ch. 61 §61.5, 59 §62.5 |
| Formatting / `clock_gettime` / a lock / blocking I/O in the logger | logging shows up in the hot profile | `perf record` with `vsnprintf` or `write` on the hot thread | Binary deferred-format logging into an overwrite ring; one `rdtsc` | Ch. 84 §84.6 |
| Synchronous capture on the hot path; storage stall bleeding in | tail follows the disk's latency | `iostat -x` latency aligned with the application's spikes; `perf trace -e 'block:*'` | Async writer, io_uring/`O_DIRECT`, drop-and-flag at the capture layer | Ch. 94 §94.5, 52 §55.5 |
| Zero-copy buffer reused before completion; io_uring completion order assumed | corrupted TX bytes; a use-after-free "sometimes" | `MSG_ZEROCOPY` notifications unmatched; ASan on the harness | Buffer ownership tied to completion; per-request ids | Ch. 56 §56.5, 52 §55.5 |
| Device-register *read* on the hot path; interrupt-driven completions | a 500 ns–1 µs PCIe round trip per message | `perf annotate` on the MMIO read; the doorbell/poll loop's cycles | Posted writes only; poll completions in host memory | Ch. 74 §74.5, 67 §70.5 |
| An accelerator (GPU/DPU) on the tick-to-trade path | µs of PCIe where ns were budgeted | wire-to-wire timestamps vs kernel-only time | Keep them on risk/research; FPGA inline only for the simple, hot, stable stages | Ch. 75 §75.5, 75 §78.5, 74 §77.5 |
| Pointers / absolute addresses / unversioned layout in shared memory | the second process reads garbage after a redeploy | a layout hash mismatch at attach; a test process mapping at a different base | Offsets, fixed versioned layout, generation-checked handles | Ch. 57 §57.5, 26 §29.5, 85 §88.5 |

## O.8 Layout, caches and the memory hierarchy

| Anti-pattern | Symptom | What exposes it | Fix | Where |
|---|---|---|---|---|
| Pointer chasing / node-based containers (`std::map`, `unordered_map`, `list`) on the hot path | one cache miss per hop; flat median with a long tail | `perf stat -e mem_load_retired.l3_miss`; `perf mem`; top-down Memory-Bound | Flat arrays indexed by dense ids; open addressing; intrusive lists from pools; slot maps | Ch. 7 §7.5, 25 §28.5, 25 §28.4.4 ◆ |
| Scattered hot data / AoS with cold bloat; premature SoA | more lines touched than the data needs; vectorization that never happens | `pahole` on the struct; cache-line counts per message in `perf mem` | Hot/cold split; SoA where the access pattern is columnar — measured | Ch. 8 §8.5, 9 §9.6 |
| Silent padding, misaligned atomics, split locks, packing that costs more than it saves | `sizeof` surprises; `split_lock` warnings; slow atomics | `pahole -C`; `-Wpadded`; `perf stat -e sq_misc.split_lock`; `dmesg` split-lock detection | Reorder members; `alignas`; never pack an atomic; `static_assert(sizeof/offsetof)` | Ch. 9 §9.6 |
| Prefetching what the hardware already does; wrong distance; streaming stores without a fence or for the wrong data | no gain or a loss; stale data visible | A/B with the prefetch removed; `sfence` missing in the asm; L2 hardware-prefetch counters | Prefetch only on measured irregular access; `sfence` after NT stores; keep hot data cacheable | Ch. 10 §10.6 |
| Working set exceeding L1/L2 (lookup tables, a 1 GB-page everything) | the table is slower than the computation it replaced | `perf stat -e L1-dcache-load-misses,l2_rqsts.miss` | Tables that fit; compute instead; cache-line-sized records | Ch. 13 §13.6, 15 §15.5, 7 §7.5 |
| Inlining everything; cold code interleaved in the hot path | Front-End-Bound; DSB misses | top-down Frontend-Bound; `perf stat -e idq.dsb_uops,frontend_retired.dsb_miss` | Hot/cold splitting, `[[gnu::cold]]`, PGO/BOLT with a representative profile | Ch. 12 §12.6, 22 §24.5 |
| Benchmarking with everything in L1 / unrepresentative working set | "node containers are fine" in the lab | compare `perf stat` miss rates lab vs production replay | Benchmark at the real working set with cache-cold access | Ch. 3 §3.5, 7 §7.5, 25 §28.5 |
| DDIO cross-socket trap; huge RX rings defeating DDIO | RX data lands in the wrong L3 or DRAM | `perf stat -e offcore_response.*` on the RX path; `pcm-pcie` | NIC and consumer on one socket; rings sized to the DDIO way share | Ch. 64 §64.5 |
| The kill-switch that walks a 4 MiB table ◆ | 300–400 µs to enumerate 400 orders | the kill's measured time vs the live-order count | Live-order bitmap or intrusive live list | Ch. 90 §90.3, §90.5 |

## O.9 Numerics, bits and SIMD

| Anti-pattern | Symptom | What exposes it | Fix | Where |
|---|---|---|---|---|
| `double` for prices/money; float `==`; parsing prices through `double` | 0.1+0.2 bugs; non-deterministic replay across builds | a replay digest diverging between compilers/flags | Scaled integers / fixed point; `from_chars` into integers | Ch. 30 §30.6, 57 §60.6 |
| Unchecked notional/position arithmetic (overflow is UB) ◆ | the fat finger that overflows passes the check — or the check is deleted | UBSan on the replay; a fuzzed quantity | `__builtin_mul_overflow`; overflow as a reject class | Ch. 87 §87.5, 87 §90.5, 27 §30.6 |
| Denormals (no FTZ/DAZ); `div`/`sqrt` in the chain; x87 intermediates | a 100× slowdown on tiny values | `perf stat -e fp_assist.any`; the asm shows `divsd` on the chain | FTZ/DAZ at start-up; reciprocal multiply; SSE/AVX only | Ch. 30 §30.6, 11 §11.6 |
| `-ffast-math`/`-Ofast` on the decision path | numbers change between builds; NaN handling disappears | diff results across flag sets; the replay digest | Per-file flags; never on the hot decision path | Ch. 24 §24.5, 27 §30.6 |
| `pdep`/`pext` on Zen 1/2; `bsr`/`bsf` on zero; missing `-march` → software fallback | 18 cycles per µop, or a `call __popcountdi2` | `objdump` for the library call; CPUID of the target | `-march` matching the fleet; `<bit>`; guard the zero case | Ch. 31 §31.6 |
| AVX-512 on the single-message latency path (downclocking); SIMD for short loops | the whole core slows for ms after a wide instruction | `perf stat -e core_power.lvl2_turbo_license`; frequency telemetry | Keep AVX-512 off the hot core or accept the license; SIMD for batches | Ch. 32 §32.6, 46 §49.5 |
| C bit-fields for wire layout; endianness bugs; unaligned `reinterpret_cast` | wrong fields on another compiler; UB that "works" | UBSan `-fsanitize=alignment`; a cross-compiler layout `static_assert` | `memcpy`/`bit_cast` loads, explicit shifts/masks, `std::byteswap` | Ch. 9 §9.6, 21 §23.6, 28 §31.6, 57 §60.6 |

## O.10 Build, flags, codegen and toolchain

| Anti-pattern | Symptom | What exposes it | Fix | Where |
|---|---|---|---|---|
| Profiling / benchmarking the wrong build (debug, different flags, stale LTO/PGO inputs) | numbers that do not reproduce in production | build-id and recorded flags (`-frecord-gcc-switches`, `readelf -p .GCC.command.line`) in the bundle | One build pipeline; flags recorded in the binary; the gate compares like with like | Ch. 2 §2.5, 3 §3.5, 22 §24.5, Appendix N |
| `-march=native` deployment SIGILL; AVX-512 binary without fallback | crashes on the older box | `objdump` for `zmm`/`vpternlog`; CPUID of the fleet | `-march` = the fleet's minimum; dispatch on CPUID for the rest | Ch. 24 §24.5, 29 §32.6 |
| Reading `-O0` asm; believing `-O3` ≥ `-O2`; trusting the API name over the codegen | conclusions from the wrong code | Godbolt at the production flags; `perf annotate` | Always the production flags; diff asm | Ch. 4 §4.5, 22 §24.5, 81 §84.6 |
| Frame pointers or `.eh_frame` dropped; no debug info archived | unwindable profiles; `?? ()` in the core | `perf record -g` with broken stacks; `eu-unstrip` fails | `-fno-omit-frame-pointer`, keep unwind tables, archive debug info by build id | Ch. 24 §24.5, 5 §5.5, 89 §93.5 |
| Inline asm without the clobbers / `volatile`; the memory clobber mistaken for a hardware fence | the statement deleted, hoisted, or a wrong register | the asm diff; an infinite loop at `-O2` | Declare every clobber; `volatile` for side effects; a real fence where one is needed | Ch. 79 §79.6 |
| Sanitizers in production; TSan+ASan together; fuzzing without sanitizers | 2–10× slowdown, or bugs that slip | the CI matrix | ASan/UBSan/TSan on the replay in CI, separately; fuzz the decoders with sanitizers | Ch. 45 §45.5, 84 §87.5 |
| Per-message crossings into another runtime; callbacks from a managed runtime on the hot thread | 10–500 ns per call and GC pauses on the hot thread | the per-crossing cost in Ch. 81's ladder; safepoint/GC logs aligned with the tail | Batch at the waist; keep managed code one hop away via shared memory | Ch. 81 §81.6, 79 §82.6, 80 §83.6 |

## O.11 Measurement and observability mistakes

| Anti-pattern | Symptom | What exposes it | Fix | Where |
|---|---|---|---|---|
| Reporting the mean; averaging percentiles; a `p99_9` gauge | the tail invisible until the incident | the histogram itself (HdrHistogram) vs the summary statistic | Full histograms, merged off-path; percentiles from enough samples | Ch. 1 §1.5, 3 §3.5, 83 §86.6 |
| Coordinated omission; service time instead of response time | a closed-loop benchmark that cannot see the stall | compare paced-intended vs dequeue-time histograms (Appendix L.2) | Record against the intended time; paced load | Ch. 3 §3.5, 39 §42.7, Appendix L |
| Unserialized `rdtsc` on a tiny region; converting to ns on the hot path; comparing TSC to the venue's clock | numbers off by the measurement's own cost or by a clock domain | the measured cost of the empty region; TSC↔PHC anchors | `rdtscp`/fenced pairs; convert off-path; anchor to PTP | Ch. 17 §17.5, 82 §85.6, 63 §66.5 |
| Measuring kernel-only/component latency, not wire-to-wire; one run, no CI | optimizing a stage the tail does not live in | end-to-end HW-timestamped histogram vs the stage sum | Wire-to-wire with hardware stamps; the regression gate with confidence intervals | Ch. 99 §99.5, 72 §75.5, 75 §78.5 |
| Drops measured instead of age; lag without age ◆ | the strategy traded on stale data with zero drops | message age at point of use | Age histograms and budgets per consumer | Ch. 42 §42.5 |
| Trusting `smp_affinity`, `scaling_cur_freq`, `/proc/net/igmp`, zero counters on a bypass box | the configuration says one thing, the hardware another | `effective_affinity_list`; `turbostat`; the querier; NIC vendor counters | Verify the effective state in the host fingerprint (Appendix M) | Ch. 52 §52.5, 59 §62.5, 93 §97.5, 94 §98.5 |
| Profiling overhead / tracing everything on every CPU / `perf -C <isolated>` all day | the observer is the tail | `rtla osnoise` with and without the tool | Targeted-live tier only, armed from a housekeeping core | Ch. 2 §2.5, 49 §52.5, 92 §96.5, 95 §99.5 |
| Benchmarking only the warm path; shadow traffic with side effects | the first real message is cold; a test order went to the venue | cold-vs-warm histograms; an order you did not mean to send | Measure cold; warm deliberately; shadow traffic that cannot leave the box | Ch. 53 §53.5 |

## O.12 Operational and architectural

| Anti-pattern | Symptom | What exposes it | Fix | Where |
|---|---|---|---|---|
| Monolith creep "for latency"; shared fate through a back door | one crash takes the session down | the fault-injection drill (kill a strategy; does the gateway survive?) | Process-per-role with shm data planes; the gateway and risk separate | Ch. 89 §89.5, 38 §41.4.1 ◆ |
| No kill-switch, or a kill-switch in the dead process; never rehearsed ◆ | orders strand after a crash; unknown time-to-safe-state | Appendix M.4's daily dry run; the measured time-to-zero-working-orders | Kill in the surviving gateway; triggers as monitors; rehearse daily | Ch. 89 §89.5, 87 §90.5 |
| Send before journal; resending an unacked order; string ClOrdIDs; a counter without a generation ◆ | duplicate positions; fills booked to the wrong order | stale-response counter; position vs drop-copy; `unordered_map<string>` in the hot profile | Journal-before-send; the unknown-state matrix; slot + generation ids | Ch. 91 §91.5 |
| Two emitters / two stampers after a failover ◆ | doubled orders; two truths | replica digest mismatch; interleaved sequencer epochs | Emission moves on a control event in the stream; epoch in every record; one promotion decision | Ch. 95 §95.5 |
| Restart that is up-but-cold; reload without warming | the restarted process limps through its first ticks | cold-start histogram after an induced restart | Warm before `trading` (Appendix M.1); shadow traffic | Ch. 89 §89.5, 50 §53.5, 85 §88.5 |
| Heartbeat that only catches exits; thresholds on invariants; a baseline nobody updates | a hung process trades stale; alerts that never fire or always fire | heartbeat *sequence* stalls while the process lives; invariant checks | Progress heartbeats; invariants as hard checks; baselines refreshed per release | Ch. 89 §89.5, 93 §97.5 |
| Trusting the wire / the shared region / the operator | a malformed packet or a bad limit set takes the system down | fuzzing the decoders; validate-before-swap failures | Bounds-check at the gate; validate config off-path; four-eyes on limits | Ch. 60 §60.6, 84 §87.5, 85 §88.5, 87 §90.4.4 ◆ |
| A mode nobody owns (shedding scattered across stages) ◆ | inconsistent behaviour under load, no record of it | the journal has no mode transitions | One owner, one word, journaled transitions | Ch. 42 §42.5 |
| Drift between test and production; tuned once and walked away | the gate passes, production regresses | host fingerprint delta (Appendix M); the production always-on tier | Configuration as code; continuous monitoring; re-tune per kernel/firmware | Ch. 99 §99.5, 6 §6.5, 48 §51.5 |

---

## O.13 The ten that pay for the appendix

If a reviewer has five minutes, these are the rows that have most often been the whole incident:

1. Hidden allocation / first-touch fault on the hot path (O.1) — `malloc` uprobe, `minor-faults`.
2. Head/tail or counters on one cache line (O.4) — `perf c2c` HITM.
3. `TCP_NODELAY` missing, or offloads left on (O.7) — the 40 ms mode; `ethtool -k`.
4. A thread per concern; a queue that only grew (O.5) — stage stamps; message age.
5. Default timer slack / RT throttle on a sleeping or spinning thread (O.5, O.6) — `timerslack_ns`; `sched_rt_runtime_us`.
6. IRQs or a sibling on the isolated core (O.6) — `/proc/interrupts`; `rtla osnoise`.
7. `std::function`/`virtual` in the inner loop (O.2) — `br_misp_retired.indirect`.
8. Reading the clock inside the deterministic core (O.3) — the replay digest.
9. The gate or the kill that walks a map or a table (O.8) — their measured time.
10. Reporting the mean (O.11) — the histogram.

## O.14 References

- The §N.5/§N.6 sections of Ch. 1–99 of this book, from which every row is drawn; Appendix I for the probe invocations; Appendix M for the readiness checks that catch the operational rows before the open.
- Gregg, B. *Systems Performance*, 2nd ed., Ch. 2 §"Methodologies" (the USE and RED methods — symptom-first diagnosis), and *BPF Performance Tools* — the uprobe/tracepoint one-liners.
- Intel. *Optimization Reference Manual*, Appendix B (top-down analysis — the counter names in O.3/O.8).
- Fog, A. *Optimizing software in C++* — the per-construct costs behind O.2/O.3/O.9.

## O.15 Additional Reading

- Brendan Gregg's "Linux Performance" page and the `perf` wiki — the current spellings of the counters named here.
- The LMAX Disruptor and Aeron issue trackers — real reports of O.4/O.5 rows in production systems.
- Carl Cook, "When a Microsecond Is an Eternity" (CppCon 2017) and David Gross, "Trading at Light Speed" (CppCon 2023) — practitioners' own catalogues.
- Postmortem archives (the SEC's Knight Capital order; exchange incident reports) — O.12 in the wild.

---

*This completes Appendix O and the book. The chapters (Parts I–XIII) and Appendices A–O together form the full tutorial series — from the latency mindset (Ch. 1) through the end-to-end tick-to-trade case study (Ch. 99) — with the standing references (ARM port, alternative languages, system tuning, compiler flags, latency numbers, glossary, bibliography, feature matrix, tooling cookbook, market-structure primer, exchange connectivity, benchmark harness, readiness gate, incident bundle, and this anti-pattern catalogue) alongside.*

---

*Next: Appendix P — Attributes, Builtins & the UB Catalogue: the per-function companion to Appendix D's per-build flags — what each attribute and builtin tells the optimizer and how to verify it fired — and the undefined-behaviour rules read as optimizer licences, with the measured cost of buying each one back.*
