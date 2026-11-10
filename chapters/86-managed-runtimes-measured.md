# Part XII — Beyond C++: Languages at the Latency Floor

# Chapter 86 — Managed Runtimes Near the Hot Path, Measured

> **Prerequisites:** Ch. 1 (tail latency — a GC pause *is* a p99.99 event), Ch. 28 (allocation cost and page faults — the mechanism under every "allocation-free" rule here), Ch. 56 (keeping the hot path warm — JIT warm-up is the cold-start problem built into the runtime), Ch. 48 (context switches and safepoint-like stops), Ch. 85 (the boundary a managed component talks to the C++ core across), Ch. 87 §87.3 (the nine-language kernel table these runtimes appear in).
>
> **Leads into:** Ch. 87 (choosing the tier each runtime can serve, with these numbers as its floors), Appendix B (summary), Ch. 92 and 93 (the control-plane and telemetry services that are the right home for a GC'd language).

---

## 86.1 Why it matters: the runtime owns the timing of your tail

Appendix B's case against managed runtimes rested on two sentences — *a collector can stop your thread when it decides to* and *a JIT compiles your hot method after the first thousand ticks, not before* — and on no measurements. The sentences are true, and insufficient. HFT shops run Java on latency-sensitive paths (LMAX built the Disruptor of Ch. 42 for exactly that; Chronicle, Aeron and Agrona exist because of it), Jane Street runs OCaml, Go and .NET services sit a hop from the order path at most firms, and inheriting such a system is as common as building one. The engineering question is not "is a GC bad" but *how large, how often, and where does the pause land relative to the budget*; *how long is the runtime slow before it is fast, and can it get slow again*; and *which disciplines close the gap to the AOT kernel, and by how much*. Those have numbers.

This chapter measures them with the book's own method. The load is Ch. 87's decode-and-apply kernel or its decode half, driven from a replayed capture; the instrument is the **hot thread's own clock** — each batch of 1,000 messages is timed by the thread doing the work, so a collector's stop, a safepoint, or a JIT deoptimisation appears as an outlier batch exactly as it would appear in Ch. 89's trace record — supplemented by the runtimes' own logs (`-Xlog:gc`, `-Xlog:safepoint`, `GC.GetTotalPauseDuration`, `runtime.MemStats`, `Gc.quick_stat`). The headline results: with one 40-byte object allocated per message, Java's stop-the-world collectors pause for 8–10 ms at a time (134 ms worst case for Serial), ZGC's concurrent design brings the stops to ~100 µs but leaves a 4.7 ms p99.99 in the hot thread, .NET's workstation GC spends *half the run* paused, Go's concurrent collector steals a quarter of the CPU, and OCaml's minor collections add 15 µs to p99 — while the **garbage-free** variant of the same code, in every runtime, runs at 2 ns per message with a p99.9 of 8–15 µs and no collection at all. The JIT warm-up curve is 12.6 µs per message for the first hundred calls, 13 ns at steady state, with an 860 µs deoptimisation dip at message 10,000. Appendix B's sentences are now graphs; the techniques of §86.4 are what the shops that make it work actually do.

## 86.2 Mental model

### 86.2.1 What a collector does to the hot thread

A tracing GC must find live objects and reclaim the rest, and every design chooses where the *application* thread pays. **Stop-the-world** collectors (HotSpot Serial, Parallel; the young generation of G1; .NET's blocking gen0/gen1; OCaml's minor GC) stop every mutator at a safepoint, do the whole collection, and resume: the pause is proportional to *live* data for a copying young collection (survivors are copied; garbage is free) and to the heap for a full collection. **Concurrent** collectors (ZGC, Shenandoah, Azul C4, Go's collector, .NET background gen2) do marking and relocation while mutators run, at the price of **barriers** — a few instructions on every reference load (ZGC's load barrier) or store (Go's write barrier, .NET's card marking) — and short stops to begin and end phases; their failure mode is *allocation stall*: when the mutator allocates faster than the collector frees, the mutator waits. A **non-collecting** heap (HotSpot Epsilon) never pauses and runs out of memory; §86.3 shows what it costs instead — page faults on every fresh page (Ch. 28 §28.2), 134 µs per 1,000 objects on the test box, and an `OutOfMemoryError` when the heap is gone. The driver of all of it is **allocation rate**: a thread that allocates nothing triggers nothing, which is why §86.4.1 is the technique that matters.

### 86.2.2 Safepoints and time-to-safepoint

A stop-the-world phase needs every mutator at a point where the runtime knows the exact stack layout and every reference — a **safepoint**. HotSpot threads poll a flag at method returns and loop back-edges; a thread in a long *counted* loop may have its back-edge poll elided (the JIT proves the loop short) and reaches the safepoint only when it exits, so **time-to-safepoint** — the delay between the request and the last thread arriving — is a latency the GC log's pause time does not include. `-Xlog:safepoint` reports it separately (§86.3: 5 µs median, 191 µs max for G1 here; ZGC's generational cycle requests many more safepoints, 721 in 8 s, with a 5.4 ms worst case). .NET uses the same return/loop polling (and *fully interruptible* code for hot methods); Go preempts goroutines asynchronously via signals since 1.14 (no counted-loop hazard, a ~tens-of-µs STW instead); OCaml 4.x polls at allocations, so an allocation-free loop is also a *GC-unreachable* loop (OCaml 5 adds explicit polls).

### 86.2.3 Tiered JIT: interpreter, C1, C2, OSR, deoptimisation

HotSpot runs bytecode in the interpreter, counts invocations and back-edges, compiles hot methods with the fast C1 compiler (tier 3, with profiling counters), and recompiles the hottest with the optimising C2 (tier 4) using the profile — speculating on types, inlining through the observed call targets, eliding checks the profile says never fail. A long-running loop is entered mid-flight via **on-stack replacement**. When a speculation fails — a new receiver type, a branch never taken before, a class loaded — the compiled code is **deoptimised**: execution falls back to the interpreter (the Ch. 56 cold start, mid-session), the method is recompiled, and the `-XX:+PrintCompilation` log shows `made not entrant`. §86.3's curve has all four phases visible. .NET's tiered compilation is the same shape (tier-0 unoptimised JIT → tier-1 with dynamic PGO, OSR for loops, and *ReadyToRun* precompiled code as the starting tier); Go and OCaml are AOT and have no curve.

### 86.2.4 The four runtimes in one frame

| | HotSpot (Java 21) | .NET 9 | Go 1.24 | OCaml 4.14 |
|---|---|---|---|---|
| Collector | Serial/Parallel/G1 (STW young), ZGC/Shenandoah (concurrent), Epsilon (none), Azul C4 (concurrent, commercial) | generational, compacting; workstation (blocking or background) vs server; `SustainedLowLatency` suppresses blocking gen2 | concurrent mark-sweep, non-moving, non-generational; `GOGC`/`GOMEMLIMIT` pacing | generational: minor (copying, STW) + incremental major; minor heap size `s=` |
| Mutator cost | load barrier (ZGC) / card marks | card-marking write barrier | write barrier during marking + 25 % CPU assist | allocation = pointer bump; polls at allocations |
| Code | interpreter → C1 → C2, OSR, deopt | tier-0 → tier-1 (PGO), ReadyToRun, NativeAOT | AOT | AOT (`ocamlopt`), flambda optional |
| Off-heap | `ByteBuffer`, `MemorySegment` (FFM), Agrona `UnsafeBuffer`, Chronicle | `Span<T>`, `stackalloc`, pinned object heap, `NativeMemory` | `unsafe`, `[]byte` into mmap | `Bigarray`, C stubs |
| Warm-up fix | shadow traffic, `-XX:CompileCommand`, CDS/AOT cache (JEP 483), GraalVM native-image | `TieredPGO`, ReadyToRun, NativeAOT | — | — |

## 86.3 Measure it: pauses, warm-up and the garbage-free line

*Toolchain and machine.* OpenJDK 21.0.12 (HotSpot, `-Xms512m -Xmx512m`), .NET 9.0.3, Go 1.24.7, OCaml 4.14.1 (`ocamlopt`, no flambda); the 4-vCPU Intel Xeon VM of Appendix L.2 (Linux 6.18), each runtime confined to `taskset -c 1,2,3` so its GC threads have cores. Not the reference machine; the comparisons within a block are the result. A sleeping "hiccup meter" (jHiccup's method) was tried first and abandoned on this host: the hypervisor's timer jitter put its p50 at 68 µs late on a 1 ms sleep for *every* configuration, GC or not — so the instrument below is the hot thread's own batch clock, which is what a Ch. 89 trace record would show.

**The load.** 1,000 messages per batch from the replayed capture (a 1024-record L1-resident ring), each message decoded off the wire bytes into an `Order{seq, id, qty, px, type, side}`; in the **ALLOC** variant the `Order` is `new`ed per message and one in 64 is kept live in a 1 M-slot ring (≈ 40 MB live, so young collections copy real survivors); in the **GCFREE** variant one pre-allocated `Order` is reused. The batch is timed by the thread (`System.nanoTime`/`Stopwatch`/`time.Now`/`Unix.gettimeofday`), the first 10 % of batches discarded (warm-up), 8 s per run.

```
   Java 21 — batch of 1,000 messages, ns (p50 / p99 / p99.9 / p99.99 / max)          Mmsg/s   -Xlog:gc pauses (count; p50 / p99 / max; total)      -Xlog:safepoint
   ALLOC  SerialGC      4,191 /  9,097 / 56,091 / 16,387,387 / 134,347,707            124     279;  7.9 ms /  32.7 ms / 134.1 ms; 3.40 s of 8 s
   ALLOC  ParallelGC    4,096 / 11,437 / 51,913 /  8,865,894 / 148,341,012            149     281;  8.2 ms /  37.6 ms / 148.1 ms; 2.59 s
   ALLOC  G1GC          4,388 /  9,634 / 50,095 /  9,898,586 /  17,481,953            156     210; 10.0 ms /  14.0 ms /  15.0 ms; 1.87 s      216 safepoints; stopped p50 10.1 ms, max 33.7 ms; time-to-safepoint p50 4.9 µs, max 191 µs
   ALLOC  ZGC (gen.)    4,766 / 27,342 / 48,535 /  4,544,630 /  37,599,762            151     (concurrent: no STW "Pause" lines at -Xlog:gc)          721 safepoints; stopped p50 99 µs, p99 621 µs, max 5.5 ms; time-to-safepoint p50 78 µs, max 5.4 ms
   ALLOC  EpsilonGC   134,067 /251,214 /676,544 /  1,952,866 /   5,098,672            8.4     none — page-faulting fresh heap at ~134 µs per 40 KB; with -XX:+AlwaysPreTouch: OutOfMemoryError at 3 GiB in ~1.5 s
   GCFREE SerialGC      2,048 /  4,221 / 15,203 /    198,537 /   6,058,355            443     none
   GCFREE G1GC          2,003 /  3,099 /  8,005 /     30,328 /   6,884,650            473     none                                                   1 safepoint (29 µs)
   GCFREE ZGC (gen.)    2,002 /  3,835 /  8,611 /     27,193 /   2,437,571            474     none
   GCFREE EpsilonGC     2,011 /  2,198 /  6,815 /     22,157 /     735,098            469     none
```

Read the ALLOC rows against the budget, not against each other first: **a stop-the-world young collection is 8–10 ms** on a 40 MB live set — three to four orders of magnitude over a tick-to-trade budget — and Serial/Parallel's *full* collections reach 134–148 ms; G1 bounds its pause (max 15 ms at the default 200 ms target, which it beat) at the cost of more frequent stops; ZGC removes the long stops (p50 stopped 99 µs) but its generational cycle requests many safepoints, the hot thread's **p99.99 is still 4.5 ms** (allocation stalls and the 5.4 ms time-to-safepoint tail), and its load barrier shows up in p50 (4.8 vs 4.2 µs per batch — ~12 % on this loop). Epsilon's "no GC" is not free: untouched heap pages fault in (Ch. 28 §28.2), 134 µs per thousand objects here; pre-touching fixes the rate and brings the end — the heap is gone in seconds at 150 M allocations/s. **The GCFREE rows are the technique**: the same decode at **2 ns per message, p99.9 of 7–15 µs, zero collections in 8 s, with every collector** — the collector's identity stops mattering the moment the hot thread stops allocating. The residual max (0.7–6.9 ms) is the VM's own noise (one G1 safepoint of 29 µs in 8 s; the rest is Ch. 55's hypervisor territory).

```
   .NET 9 — batch of 1,000 messages, ns                                                            Mmsg/s   gen0/1/2 collections   GC.GetTotalPauseDuration
   ALLOC  workstation, concurrent   9,068 / 22,371 /  94,190 / 57,048,989 / 82,319,160              47      81 / 45 / 10           4,095 ms of an 8 s run
   ALLOC  workstation, SustainedLowLatency                 8,884 / 22,202 / 66,132 / 59,360,904 / 85,320,170   46      80 / 45 / 10   4,233 ms   <- no change: gen0/gen1 are blocking regardless
   ALLOC  server GC                 6,966 / 24,002 / 847,504 /  1,370,177 /  8,687,277              85      4,328 / 1,095 / 30      2,479 ms   <- many small gen0s: max 8.7 ms, p99.99 1.4 ms
   GCFREE workstation               2,209 /  2,244 /   7,797 /     23,132 /  1,090,502             436      1 / 1 / 1               1 ms

   Go 1.24                                                                                          Mmsg/s   numGC   STW total / max STW
   ALLOC  GOGC=100                 14,674 / 53,174 / 231,816 / 1,211,929 / 5,784,693                 57      146     16.6 ms / 721 µs    <- STW is small; the cost is the concurrent marker + 25% assist: 5x slower batches
   ALLOC  GOGC=off GOMEMLIMIT=1GiB 15,810 / 66,727 / 152,998 / 1,107,857 / 5,792,108                 41       47      5.3 ms / 268 µs
   GCFREE                           3,109 /  5,606 /  18,464 /    73,068 / 2,899,690                302        2      0.1 ms /  83 µs

   OCaml 4.14                                                                                       Mmsg/s   minor / major collections
   ALLOC  minor heap 256k words     5,960 / 24,080 /  46,014 /    86,069 / 23,516,893               136      29,052 / 24           <- a minor GC every ~5 batches: +15 µs at p99; major slices reach 23 ms
   ALLOC  minor heap 8M words       6,914 / 13,113 / 605,106 / 2,182,960 / 23,519,039               110         743 / 19           <- fewer, LONGER minor GCs (more survivors per collection): p99.9 605 µs
   GCFREE                           5,960 /  9,059 /  21,934 /    92,029 /  2,164,125               170          18 /  2
```

The same shape in every runtime. .NET's workstation collector is **paused for half the run** with a 57 ms p99.99 — and `SustainedLowLatency` changes nothing, because it only suppresses *blocking gen2*, not the gen0/gen1 collections that an allocating hot path triggers; server GC trades it for many short stops (8.7 ms max). Go's STW is genuinely small (721 µs max) but the concurrent marker and the mutator assist make every batch 5× slower while a cycle runs, and `GOGC=off` with a memory limit only spaces the cycles out. OCaml's minor GC is cheap but *frequent* (one per ~5,000 allocating messages), and the classic fix — a bigger minor heap — moves the cost into the tail rather than removing it. And in all three the GCFREE row is 2–6 ns per message with no collections: Go's `GCFREE` p50 of 3.1 µs per batch is the write barrier being *off* and escape analysis keeping the flyweight on the stack; OCaml's 6 µs is its boxed `Int64` arithmetic (§86.4.6), not its collector.

**The JIT warm-up curve** — Ch. 87's full kernel (decode + book update) in Java, per-call latency over its first calls, default tiered compilation, with `-XX:+PrintCompilation`:

```
   calls [0,100)   12,630 ns/msg    interpreter                                  PrintCompilation:  91ms  tier 3  Kernel::apply (373 bytes)      <- C1 with profiling
   [100,1k)         3,585            interpreter → C1 (OSR of main's loop)                           95ms  tier 4  Kernel::apply                  <- C2 compile starts
   [1k,10k)            68            C1 code                                                        113ms  tier 3  Kernel::apply  made not entrant <- C1 version retired
   [10k,20k)          859            DEOPTIMISATION: tier-3 code invalidated, back through the interpreter while C2's version is installed
   [20k,100k)       55–90            C2 code, but main's loop still runs in the C1 OSR frame
   [100k,200k)         66            …
   [200k,500k)         18            main re-entered through C2 OSR: apply inlined
   [500k,1M)           13            steady state (C++: 7.4; Ch. 87 §87.3)
   -Xint (interpreter only): ~1,000 ns/msg throughout     -XX:TieredStopAtLevel=1 (C1 only): 20–21 ns/msg steady
```

**The first hundred messages cost 1,000× the steady state, the first ten thousand still 5×, and the deoptimisation at 10 k is a 860 µs-per-message window** — a market-open burst of 10,000 messages is exactly the window in which the JIT is slowest. `-Xint` shows the floor the interpreter represents (75× C2); C1-only shows how much of the final speed the cheap compiler delivers (two-thirds) without C2's profile-driven speculation — and without its deoptimisations.

## 86.4 Techniques

### 86.4.1 Garbage-free code: the flyweight over the bytes

The §86.3 GCFREE line is produced by one rule — **the hot path allocates nothing after warm-up** — and in Java the idiom that makes a decoder allocation-free is the *flyweight*: fields are read straight off the wire bytes through a `VarHandle` (or Agrona's `UnsafeBuffer`, or an FFM `MemorySegment`), and the "message object" is a reusable view, not a `new` per message. Ch. 87's Java kernel is exactly this — the `Msg` of the C++ version never exists as an object:

```java
static final VarHandle BE32 = MethodHandles.byteArrayViewVarHandle(int[].class, ByteOrder.BIG_ENDIAN);
static final VarHandle BE64 = MethodHandles.byteArrayViewVarHandle(long[].class, ByteOrder.BIG_ENDIAN);
final long[] bidQty = new long[LEVELS], askQty = new long[LEVELS];          // SoA book (Ch. 9): primitive arrays — no Level objects, no headers, no card marks on stores
long apply(byte[] buf, int off) {                                              // decode + apply fused: fields read in place (Ch. 64's overlay, in Java)
    final byte typ = buf[off], side = buf[off + 1];
    final int qty = (int) BE32.get(buf, off + 4), px = (int) BE32.get(buf, off + 8);
    final long seq = (long) BE64.get(buf, off + 16), id = (long) BE64.get(buf, off + 24);
    final long[] q = side == 'B' ? bidQty : askQty;  final int idx = px & (LEVELS - 1);
    if (typ == 'A') { q[idx] += qty; … } else { … }
    return (((long) bestBid) << 32 | bestAsk) ^ bidQty[bestBid];
}
```

The supporting rules are the ones Appendix B.4 listed, now with the mechanism named: no autoboxing (`Long` in a `Map` is an allocation per lookup — use primitive-keyed collections: Agrona `Long2ObjectHashMap`, Eclipse Collections, or a flat array as above); no `Iterator` in the loop (enhanced-`for` over a `List` allocates one; index instead); no capturing lambdas or `String` concatenation; `ByteBuffer`/`MemorySegment` for I/O so bytes never become objects; object pools (Ch. 29) for anything that must exist as an object; and **`-Xlog:gc` in CI asserting zero collections over a replayed session** — the garbage-free property is tested, not hoped. Agrona (`UnsafeBuffer`, `ManyToOneRingBuffer`), Chronicle Queue/Map (off-heap, memory-mapped journals — Ch. 61's shm ring and Ch. 98's journal in Java) and Aeron (a UDP/shm transport with the Ch. 42 batching) are the production libraries built on these rules.

### 86.4.2 Choosing and tuning the collector

For a garbage-free hot path the collector is almost irrelevant (§86.3), so choose it for *everything else* in the process — the control plane, logging, the parts that do allocate — and for the failure mode you prefer. **Serial/Parallel**: smallest barrier cost, catastrophic full-GC tail (134–148 ms); acceptable only if a full GC provably never runs in-session (sized heap, garbage-free, `-Xlog:gc` proof). **G1** (default): bounded young pauses (`-XX:MaxGCPauseMillis`, 15 ms achieved here), moderate barrier; the sane default when the process allocates somewhere. **ZGC generational** (`-XX:+UseZGC -XX:+ZGenerational`, default-generational in 23+): sub-millisecond stops, a load barrier on every reference read (~12 % on §86.3's loop), many safepoints — right when latency-sensitive code allocates and cannot be made garbage-free; watch allocation stalls (`-Xlog:gc+phases`). **Shenandoah** is the same trade with a different barrier. **Azul Zing/Prime (C4)** is the commercial concurrent-compacting collector trading firms pay for: the same shape as ZGC with a decade's head start and `ReadyNow` JIT warm-up persistence. **Epsilon** for a garbage-free process that must *prove* it: run the replayed session with `-XX:+UseEpsilonGC -XX:+AlwaysPreTouch` and a heap sized for the session's total allocation; an `OutOfMemoryError` is a failed test, and in production it is the kill-switch (Ch. 93). Always: `-Xms == -Xmx` (no resizing stops), `-XX:+AlwaysPreTouch` (Ch. 31's pre-faulting), `-Xlog:gc*,safepoint` to a file, and read time-to-safepoint, not only pause time.

### 86.4.3 Warming the JIT, and pinning what it compiled

The §86.3 curve is Ch. 56's warm-up problem with an extra axis, and the fixes are Ch. 56's with runtime-specific tools. **Shadow traffic before the open**: replay yesterday's capture (Ch. 98) through the engine — with order entry disarmed — until `-XX:+PrintCompilation` shows the hot methods at tier 4 and no `made not entrant` for N minutes; the replay must exercise *every* branch and receiver type the session will, or the first unexercised one deoptimises live. **Avoid deoptimisation triggers**: monomorphic call sites (one implementation per interface on the hot path — Ch. 15's argument in JIT form), no class loading after warm-up (`-XX:+TraceClassLoading` to catch it; `-Xshare`/CDS archives to front-load it), `-XX:CompileCommand=inline,…` / `dontinline` to pin decisions, and `-XX:-BackgroundCompilation` in the warm-up phase so compiles finish before the open. **Persist the warmth**: JDK 24's AOT cache (JEP 483, "Ahead-of-Time Class Loading & Linking") and Azul `ReadyNow` record a training run's profile and compiled code so the next start begins at tier 4; **GraalVM native-image** compiles the application to a native binary ahead of time — no interpreter, no C2, no curve, a smaller runtime (and a weaker peak optimiser than C2, and no dynamic class loading), the closest Java gets to the AOT band. .NET: `TieredPGO` (default), **ReadyToRun** (`PublishReadyToRun`) for a warm starting tier, **NativeAOT** (`PublishAot`) for the GraalVM equivalent; `DOTNET_TieredCompilation=0` to force full JIT at first call (slower start, no tier-up dip).

### 86.4.4 .NET: `Span<T>`, `ref struct`, and the collector you cannot turn off

The C# kernel of Ch. 87 runs at 9–10 ns per message — within 30 % of C++ — because it is written the garbage-free way: `ReadOnlySpan<byte>` over the capture (a `ref struct`: stack-only, no heap), `BinaryPrimitives.ReadUInt32BigEndian` for the in-place decode, a `Level[]` of *structs* indexed by tick with `ref Level L = ref s[idx]` so the struct is updated in place rather than copied (Ch. 10), and no `new` in the loop. The rules are Java's with .NET spellings: `struct` for value types (no header, no GC), `stackalloc`/`Span<T>` for scratch, `ArrayPool<T>` and object pools, `NativeMemory.Alloc`/the **pinned object heap** (`GC.AllocateArray(pinned: true)`) for I/O buffers so the GC never moves them (and so P/Invoke needs no pinning — Ch. 85), no LINQ/closures/boxing on the hot path. The collector: §86.3 showed **workstation GC blocks on gen0/gen1 regardless of `LatencyMode`** — `SustainedLowLatency` and `LowLatency` govern gen2 only — so a .NET hot path is garbage-free or it pauses; **server GC** (`DOTNET_gcServer=1`) gives per-core heaps and shorter, more frequent stops (8.7 ms max here) at a CPU cost; `GCConserveMemory`, `GCgen0size` (`DOTNET_GCgen0size`) and `GCHeapAffinitizeMask` shape when and where it runs. `GC.TryStartNoGCRegion(totalSize)` is .NET's Epsilon-for-a-window: no collection until `totalSize` bytes are allocated or `EndNoGCRegion` — usable around a burst if the budget is known.

### 86.4.5 Go: allocation-free loops, `GOGC`, and the scheduler

Go's kernel runs at 19 ns per message steady (2.5× C++), carrying 7 bounds checks the compiler could not prove (`-gcflags=-d=ssa/check_bce/debug=1` lists them; 4 `runtime.panic` sites remain in `apply`) and no write barrier because the loop stores no pointers. Its GC is concurrent and non-moving: STW phases are short (721 µs max in §86.3), but the **mark phase runs on the same cores** (25 % CPU target plus *mutator assist* when allocation outruns marking), which is where the 5× batch slowdown came from. The rules: keep the hot loop **allocation-free** — `go build -gcflags=-m` prints every escape (`new(Order)` escaped because it was stored into a slice; a value `Order` that does not escape lives on the stack) — reuse `[]byte` buffers and `sync.Pool` for the rest; set `GOGC` and **`GOMEMLIMIT`** so that collection happens on your schedule (a large `GOMEMLIMIT` with `GOGC=off` runs GC only at the limit — 47 cycles instead of 146 here — and `debug.SetGCPercent(-1)` at session start plus a manual `runtime.GC()` at a quiet moment is the Go version of "collect between sessions"); **`runtime.LockOSThread()`** in the hot goroutine so Ch. 50's `taskset` pinning holds (the scheduler would otherwise migrate it), `GOMAXPROCS` sized to leave the pinned core alone, and accept that asynchronous preemption (a `SIGURG` every ~10 ms to a long-running goroutine) is a signal delivery (Ch. 48 §48.4.3) you cannot disable except by making the loop call into the scheduler itself.

### 86.4.6 OCaml: unboxed ints, the minor heap, and Jane Street's discipline

OCaml's kernel runs at 12.4 ns per message — 1.7× C++ — and the gap is visible in the source: the 63-bit tagged `int` is unboxed and free, but every `Int64` (the `seq`/`id` fields read with `Bytes.get_int64_be`) is a *boxed* allocation unless the optimiser removes it, and 4.14's non-flambda `ocamlopt` removed some but not all (the `GCFREE` row's 18 minor collections in 8 s are those boxes). The discipline Jane Street's performance culture is known for follows directly: **write in the unboxed subset** on the hot path (`int`, `float` in local arithmetic — floats are unboxed within a function and in float arrays — `Bytes`/`Bigarray` for bytes, records of immediates, `[@@unboxed]`/`[@untagged]` on externals as in Ch. 85 §85.3), **know the minor heap** (`OCAMLRUNPARAM='s=…'` — §86.3 showed a bigger minor heap *lengthens* each minor GC in proportion to survivors; the right size is the one where survivors per collection are near zero), **avoid polymorphic comparison and closures** in the loop (both allocate or indirect), use **flambda** (`-O3`, a flambda-enabled switch) for inlining and unboxing across functions, and read `ocamlopt -S` (or `-dcmm`) for the `caml_alloc`/`caml_call_gc` calls that mark an allocation site. OCaml 5's multicore runtime changes the GC (per-domain minor heaps, a stop-the-world *minor* collection across domains) and adds safepoints in loops; the single-domain hot path keeps the 4.x profile. Hardcaml — Jane Street's OCaml-embedded hardware description language — is where OCaml meets Ch. 81: the same language describing the FPGA feed handler and the software that tests it.

### 86.4.7 One hop away: the managed reader of a C++-written ring

The placement that survives all of §86.3 is Ch. 87 §87.4.2's: the managed component does not sit *on* the hot path and is not *called* per message; it reads what the C++ core publishes into shared memory (Ch. 61's SPSC ring, Ch. 40's seqlock), in its own loop, on its own core, with its own GC — whose pauses then cost it staleness, never the core a tick. The Java 21 reader of a Ch. 40 seqlock snapshot through the FFM API is the shape (Ch. 85 §85.4.4 asserts the header layout on both sides):

```java
// SeqlockReader.java — read a C++-published BBO snapshot from a shared segment (Ch. 40 protocol, Ch. 61 shm) without a call into the core.
static final VarHandle SEQ = MethodHandles.memorySegmentViewVarHandle(ValueLayout.JAVA_LONG);   // acquire/release access modes: the Ch. 35 orderings
static final long SEQ_OFF = 0, BID_OFF = 64, ASK_OFF = 72;                                        // the C++ struct: alignas(64) seq, then the payload (asserted both sides)
long bid, ask;                                                                                    // fields, not locals, so the snapshot survives the loop without allocation
boolean readSnapshot(MemorySegment seg) {
    long s1 = (long) SEQ.getAcquire(seg, SEQ_OFF);                 // odd = writer mid-update (Ch. 40 §40.2): retry
    if ((s1 & 1) != 0) return false;
    bid = seg.get(ValueLayout.JAVA_LONG, BID_OFF); ask = seg.get(ValueLayout.JAVA_LONG, ASK_OFF);
    VarHandle.loadLoadFence();                                     // the reader's second fence (Ch. 40 §40.4): payload loads before the re-read of seq
    return (long) SEQ.getAcquire(seg, SEQ_OFF) == s1;              // torn if the writer moved on: caller retries — no allocation on any path
}
```

The segment is `FileChannel.map(...)`/`Arena.ofShared()` over the same `/dev/shm` file the C++ writer `mmap`s (Ch. 31); the C++ side is unchanged and unaware; the Java side is garbage-free by construction (two `long` fields, a boolean) and its collector, whichever one §86.4.2 chose for the rest of the process, can run whenever it likes. A `.NET` `MemoryMappedViewAccessor` + `Volatile.Read`, a Go `syscall.Mmap` + `atomic.LoadUint64`, or an OCaml `Bigarray` + a C stub do the same. This is what "Java near the hot path" should mean: adjacent, by shared memory, never in line.

## 86.5 Verify the codegen: reading what the JIT emitted

The Ch. 4 habit does not stop at the runtime boundary; each runtime will show you the machine code and the decisions behind it. **HotSpot:** `-XX:+UnlockDiagnosticVMOptions -XX:+PrintAssembly` (with the `hsdis` disassembler plugin installed) dumps C1/C2 output; `-XX:+PrintCompilation` (as in §86.3) shows tiers, OSR (`%`) and `made not entrant`; `-XX:+PrintInlining` shows what C2 inlined and why it refused (`too big`, `callee is too large`, `not inlineable` for a megamorphic site — Ch. 15's story); **JITWatch** (Chris Newland) turns these logs into a Godbolt-like view with per-method asm, inlining trees and *escape-analysis* results — the check that your flyweight really did not allocate. **.NET:** `DOTNET_JitDisasm=Apply` prints the tier-0 and tier-1 code for a method (`DOTNET_JitStdOutFile` to a file; `DOTNET_TieredCompilation=0` for the final tier only), `DOTNET_JitDisasmSummary=1` lists every compile with its tier; `BenchmarkDotNet`'s `[DisassemblyDiagnoser]` captures the same in a benchmark report. **Go:** `go build -gcflags='-S'` emits the assembly, `-gcflags=-m` the escape and inlining decisions, `-gcflags=-d=ssa/check_bce/debug=1` the surviving bounds checks, and `go tool objdump -s 'main\.\(\*Book\)\.apply' binary` disassembles the shipped function. **OCaml:** `ocamlopt -S -inline 200 kernel.ml` leaves `kernel.s`; grep it for `caml_alloc` and `caml_call_gc` to find every allocation site, and `-dcmm` shows the intermediate form where boxing is explicit. The question is the same in all four: *is the hot method one basic block of loads and arithmetic with no call to the allocator, no barrier and no type check* — and the answer is read, not assumed.

## 86.6 Pitfalls & anti-patterns

- **Reading the GC log's pause time as the hot thread's pause.** Time-to-safepoint, allocation stalls and the barrier's per-access cost are not in it (§86.2.2, §86.3's ZGC row: 99 µs "stopped" median, 4.5 ms p99.99 in the thread). Measure from the thread.
- **Believing a latency mode is a latency guarantee.** `SustainedLowLatency` left .NET paused for half the run (§86.3); `MaxGCPauseMillis` is a goal G1 may miss; `GOMEMLIMIT` spaces cycles out. Only not allocating is a guarantee.
- **A garbage-free hot path with one allocating branch.** The exception path, the log statement with string concatenation, the `Long` autoboxed into a map on a rare event — the collection it eventually triggers lands on a tick. `-Xlog:gc` asserting zero collections over the full replayed session, including its error paths, is the test (§86.4.1).
- **Warming the wrong paths.** A warm-up that replays only normal traffic leaves the gap-recovery branch (Ch. 65) interpreted until the first real gap — at 09:30:01. Replay includes the rare paths, or the deoptimisation does (§86.3's 860 µs window).
- **Epsilon as "no GC".** It is "no collection": page faults on the way up (§86.3), `OutOfMemoryError` at the top. Pre-touch, size for the session, treat exhaustion as the kill-switch.
- **Megamorphic call sites.** Three implementations of `MessageHandler` reachable from one call site and C2 stops inlining it (Ch. 15's BTB argument plus a lost inline); JITWatch shows it (§86.5).
- **Calling the runtime from the hot thread via FFI, or vice versa.** Ch. 85 §85.3's 10–470 ns rungs, plus a possible GC on your thread when the runtime decides your callback is a safepoint (§85.6).
- **Measuring the JIT's steady state with a cold-start workload** — or the reverse. §86.3's curve says which window you are in; Ch. 87 §87.2.3's protocol separates them.
- **Assuming the AOT languages have no warm-up.** They have Ch. 56's: page faults, cold caches, cold branch predictors — 17 ns cold vs 7 ns warm for the Rust kernel (Ch. 84 §84.3). The JIT adds a thousandfold on top; it does not create the problem.

## 86.7 Exercises & checklist

1. **Find the allocation.** Take the GCFREE Java kernel, add one `String.format` on a 1-in-100,000 branch, and run with `-Xlog:gc` over the replayed capture; find the collection it causes and the batch it lands on.
2. **Deoptimise on purpose.** Warm the kernel on `'A'`/`'X'` messages only, then inject a third message type after 1 M calls; capture `made not entrant` in `-XX:+PrintCompilation` and the per-message latency window around it.
3. **Hiccup meter on bare metal.** On the reference machine (Appendix L.3), run jHiccup beside the ALLOC load under G1 and ZGC and compare its distribution with the hot thread's batch clock; explain which stops each instrument sees.
4. **Go escape analysis.** Rewrite the Go ALLOC loop so `Order` does not escape (`-gcflags=-m` prints no "escapes to heap"); confirm `numGC` drops to the GCFREE level and the write barrier disappears from `-gcflags=-S`.
5. **OCaml unboxing.** Replace the `Int64` fields in the OCaml kernel with two `int`s (high/low 32 bits) and re-measure; count `caml_alloc` in `-S` before and after; try a flambda switch with `-O3`.

Checklist for a managed component within one hop of the hot path:

- [ ] The hot thread allocates nothing after warm-up — proven by the runtime's GC log over a full replayed session, error paths included.
- [ ] Message access is a flyweight over bytes (VarHandle/`UnsafeBuffer`/`MemorySegment`, `Span<byte>`, `[]byte`, `Bytes`), book state is primitive/struct arrays.
- [ ] Collector chosen for the *rest* of the process and its failure mode; `-Xms==-Xmx`, pre-touch, GC+safepoint logging on; time-to-safepoint read.
- [ ] JIT warmed by replay covering every live branch and receiver type; compile decisions pinned or persisted; no class loading after warm-up.
- [ ] The hot thread's own latency recorded (Ch. 89's record or a batch clock) — not only the runtime's pause log.
- [ ] Any FFI to the C++ core is batched or replaced by shared memory (Ch. 85).
- [ ] The component sits at the tier its measured floor allows (Ch. 87 §87.4.2), not the one its vendor claims.

## 86.8 References

- Gil Tene, *How NOT to Measure Latency* (Strange Loop 2015) and the jHiccup / HdrHistogram documentation — the hiccup method and coordinated omission (§86.3; Ch. 3).
- OpenJDK: *HotSpot Garbage Collection Tuning Guide* (JDK 21); JEP 439 (Generational ZGC), JEP 318 (Epsilon), JEP 483 (AOT Class Loading & Linking, JDK 24); the `-Xlog` unified logging and `-XX:+PrintCompilation`/`PrintAssembly` documentation (§86.2, §86.3, §86.4.2–§86.4.3, §86.5).
- Azul, *The Pauseless GC Algorithm* (Click, Tene, Wolf, VEE 2005) and the Zing/Prime and ReadyNow documentation — C4 and persisted warm-up (§86.4.2–§86.4.3).
- GraalVM *Native Image* documentation — AOT Java, its restrictions and peak-performance trade (§86.4.3).
- Microsoft .NET documentation: *Fundamentals of garbage collection*, *Workstation and server GC*, *Latency modes*, `GC.TryStartNoGCRegion`, *Tiered compilation*, *ReadyToRun*, *Native AOT*, `DOTNET_JitDisasm` (§86.2.4, §86.3, §86.4.4, §86.5).
- The Go GC Guide (`go.dev/doc/gc-guide`), *A Guide to the Go Garbage Collector*, the `runtime/debug` and `runtime` (MemStats, `LockOSThread`) documentation, and *Proposal: Non-cooperative goroutine preemption* (Austin Clements) (§86.2.2, §86.4.5).
- OCaml manual, *Runtime system* (`OCAMLRUNPARAM`) and *Optimisation with Flambda*; Minsky, Madhavapeddy & Hickey, *Real World OCaml*, "Memory Representation of Values" and "Understanding the Garbage Collector" (§86.2.4, §86.4.6).
- Agrona, Chronicle Queue and Aeron documentation; Thompson et al., *LMAX Disruptor* (Ch. 42) — the garbage-free Java toolkit (§86.4.1).
- Chris Newland, *JITWatch* — inlining, escape analysis and assembly from HotSpot logs (§86.5).

## 86.9 Additional Reading

- Martin Thompson, *Mechanical Sympathy* (blog) and Peter Lawrey's Chronicle engineering blog — the garbage-free Java tradition from its practitioners (§86.4.1).
- Cliff Click, *A JVM Does That?* and his talks on HotSpot's JIT and GC internals — what the runtime does to your tail, from one of its authors.
- Konrad Kokosa, *Pro .NET Memory Management* (Apress) — the .NET collector in the depth this chapter cannot give it (§86.4.4).
- Jane Street tech blog (OCaml performance, Hardcaml) — OCaml at a trading firm (§86.4.6; Ch. 81).
- Ch. 28 (allocation and page faults), Ch. 56 (warming), Ch. 48 §48.4.3 (signals — Go's preemption), Ch. 85 (the boundary), Ch. 87 (the tier table these floors feed), Appendix B (summary), Appendix F (GC pause, JIT vs AOT, safepoint).

---

*Next: Ch. 87 — Choosing a Language per Latency Tier: the capstone of Part XII — the tiers of Ch. 1 with measured language floors on them, the equal-rigour protocol for benchmarking across languages (same box, same core, same replayed capture, warm and cold separated, the `DoNotOptimize` family in nine toolchains), the nine-language decode-and-apply table, `constexpr` beside `const fn` and `comptime` with identical `.rodata`, and the scorecard that turns all of it into a decision.*
