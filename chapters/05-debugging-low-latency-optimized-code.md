# Part I — Foundations & Methodology

# Chapter 5 — Debugging Low-Latency & Optimized Code

> **Prerequisites:** Ch. 2 (perf/PMU — production observation) and Ch. 4 (reading asm — debugging optimized code means reading its codegen). The chapter is self-contained: its worked example (§5.3) needs only `g++ -O2 -g`, gdb and `rr`, and the build flags it discusses are Ch. 22's (forward-referenced, not needed first). The production machinery it leans on later — capture/replay, lock-free logging, eBPF — is developed in Parts X–XIII and is forward-referenced here.
>
> **Leads into:** the reproduce-then-debug approach draws on tools built later — the build flags (Ch. 22) and sanitizer flags (Appendix D), the sanitizers (Ch. 42), zero-overhead logging as a debug channel (Ch. 81), and — the single best debugging tool — the deterministic state machine and capture/replay (Ch. 86–89). Every "verify the codegen" and async pipeline in the book (e.g. Ch. 41) eventually needs what this chapter teaches. It's the methodology Part I implied: how to *debug* a low-latency system when you can't attach a debugger to the hot path and the optimizer has erased your variables.

---

## 5.1 Why it matters: you can't `printf` your way out of a nanosecond bug

Part I taught you to *measure* — profile, benchmark, read the asm. This chapter is about the other half of the craft: *debugging* — finding out why the system did something wrong. And low-latency systems are uniquely hostile to every debugging technique you learned on ordinary software, because the two properties that make them fast make them nearly undebuggable by conventional means.

First, **the code is optimized past recognition.** You ship `-O2`/`-O3` with LTO and PGO (Ch. 22); the optimizer inlines functions out of existence (Ch. 14), keeps variables in registers or eliminates them entirely, reorders and merges instructions (Ch. 11), and vectorizes loops (Ch. 29). Attach `gdb` to that binary and you get `<optimized out>` where your variable should be, a call stack that only matches your source if you kept debug info (gdb reconstructs inlined frames from DWARF — strip `-g` and they collapse; tail-called frames vanish regardless), and a program counter that jumps around because instructions were reordered. The debugger and the source have diverged — debugging optimized code is a skill of its own (Ch. 4's "read the machine" applied to a live process).

Second, **you cannot perturb the hot path to observe it.** The classic debugging moves — add a `printf`, set a breakpoint, single-step, attach a debugger — all *stop or slow the thread*, and a low-latency thread that's stopped isn't doing the thing you're trying to debug. Worse, they change the timing: the bug is often a *race* or a *timing-dependent* condition (Ch. 30, 42), and the moment you add a `printf` (~1–10 µs of formatting, locking and a `write` syscall on every flush — every line on a terminal or stderr, every ~4 KiB to a file — Ch. 81 §81.3) or a breakpoint, the timing shifts and the bug vanishes — a **heisenbug**. Attaching `gdb` to a live hot process stalls it for milliseconds (blowing every latency guarantee) and, if it holds a lock or a NIC ring, can take the whole system down. You cannot debug the hot path *while it's hot*.

The resolution is a different toolkit, built on the same architecture the book already established: **make the system deterministic and captured (Ch. 86–89), then debug the *replay*, not the live system.** Because a deterministic state machine (Ch. 86) fed a captured input stream (Ch. 89) reproduces the exact bug offline, bit-for-bit, you can attach every heavy tool — debugger, sanitizer, record/replay — to the reproduction *without* touching production. This chapter covers debugging optimized code (§5.2 the mental model), a worked gdb/`rr`/split-DWARF session on an `-O2` binary with the measured cost of debug info (§5.3), the techniques — record/replay, core dumps, replay-debugging, production tracing (§5.4), and the pitfalls, chiefly the observer effect (§5.5). It's how you find the bug the profiler can't see and the debugger can't safely touch.

## 5.2 Mental model: optimized code, the observer effect, and reproduce-then-debug

Three ideas frame low-latency debugging:

- **Optimized code doesn't match your source, so debug at the level it runs.** With `-O2`/LTO, the mapping from source lines to instructions is many-to-many and lossy: variables live in registers (and get reused), functions are inlined (their frames vanish), dead code is deleted (including, if you had UB — Ch. 21/84 — code you *thought* would run), and instructions are reordered. To debug it you (a) keep **debug info even in optimized builds** (`-g` doesn't change codegen — §5.4), so the debugger can map what it *can*; (b) read the **asm** (Ch. 4) when the source view lies; and (c) sometimes build a **`-Og`** (optimize-for-debugging) or `-O0` reproduction where the bug still occurs. But beware: some bugs *only* appear at `-O2` (the optimizer exposed a UB — Ch. 21, or a race the reordering made visible — Ch. 30), and vanish at `-O0` — those you must debug *as optimized*, reading registers and asm.
- **The observer effect is the central constraint.** Observing the hot path changes it. A `printf`, a breakpoint, a debugger attach, even a heavyweight `perf` mode perturbs timing and CPU state enough to hide timing-dependent bugs and to violate the latency guarantees you're debugging. The corollary: **observe with the lowest-overhead tool that can see the bug** (a lock-free log record — Ch. 81, an eBPF probe — Ch. 66, a hardware counter — Ch. 2), and do heavy observation *off* the hot path (on a replay). Never debug a heisenbug by adding observation that changes the timing.
- **Reproduce-then-debug (the winning strategy).** Instead of catching the bug live (which you can't safely do), *reproduce it offline* and debug the reproduction with unlimited tooling. The two engines for reproduction: **deterministic replay** (Ch. 86–89 — feed the captured production input through the deterministic core and the bug recurs exactly), and **record/replay debuggers** (`rr` — record the program's execution once, then replay it deterministically forwards *and backwards* as many times as you like). Both turn a fleeting, timing-dependent, production-only bug into a stationary, inspectable, infinitely-repeatable one. This is why the book built determinism (Ch. 86) and capture (Ch. 89): they are, among other things, the debugging substrate.

The mental model: **you don't debug the live hot path — you capture enough to reproduce the bug, then debug the reproduction with heavy tools, reading the code at the level it actually runs (asm/registers) because the optimizer erased the source-level view.**

## 5.3 Measure it: debugging an `-O2` binary with gdb and `rr`, and what debug info costs

*Toolchain and machine: Appendix L.3 (the listing, the gdb session and the size/identity measurements below were last rebuilt and run with GCC 13.3 and gdb 15 on Linux 6.18; `rr` 5.8).*

Debugging's "measure it" has two halves: *see* what the optimizer did to the code you are debugging, and *know* what the debug information you keep around costs. Both can be measured on a small program. `snap_notional` snaps a price to the tick grid and returns the notional; it has a real bug (`/` truncates toward zero, so a negative price offset snaps *up* a tick) and, more to the point here, two intermediates that the optimizer will not keep anywhere:

```cpp
// optbug.cpp — a tick-rounding bug whose intermediates are <optimized out> at -O2.
// Build: g++ -O2 -g -std=c++20 optbug.cpp -o optbug          (debug info, production codegen)
//        g++ -O0 -g -std=c++20 optbug.cpp -o optbug.O0       (the "easy" build — same bug, see below)
#include <cstdint>
#include <cstdio>
#include <cstdlib>

struct Order { std::int64_t price; std::int32_t qty; std::int32_t tick; };   // price in 1e-4 units

// Snap a price to the tick grid and return the notional. Bug: `/` truncates toward zero, so a
// negative price offset (a short leg quoted below the reference) snaps UP a tick, not down.
[[gnu::noinline]] std::int64_t snap_notional(const Order& o) {
    std::int64_t ticks   = o.price / o.tick;      // <- the suspect intermediate
    std::int64_t snapped = ticks * o.tick;        // <- and this one
    return snapped * o.qty;
}

int main(int argc, char** argv) {
    Order o{ argc > 1 ? std::atoll(argv[1]) : -100'005, 7, 10 };
    std::printf("notional=%lld\n", static_cast<long long>(snap_notional(o)));
    return 0;
}
```

**1. gdb on the `-O2` binary: `<optimized out>` means "read the location list".** Break on the function, ask for the locals, then ask gdb *where* they are:

```
   $ g++ -O2 -g -std=c++20 optbug.cpp -o optbug && gdb -q ./optbug
   (gdb) break snap_notional
   (gdb) run
   Breakpoint 1, snap_notional (o=...) at optbug.cpp:12
   (gdb) info locals
   ticks = <optimized out>
   snapped = <optimized out>
   (gdb) info address ticks
   Symbol "ticks" is multi-location:
     Range 0x...21b-0x...223: a complex DWARF expression:  DW_OP_breg2 0 [$rcx]; DW_OP_breg4 0 [$rsi]; DW_OP_div; DW_OP_stack_value
     Range 0x...223-0x...227: a variable in $rax
     Range 0x...227-0x...22a: a complex DWARF expression:  DW_OP_breg2 0 [$rcx]; DW_OP_breg4 0 [$rsi]; DW_OP_div; DW_OP_stack_value
   (gdb) info address snapped
   Symbol "snapped" is multi-location:
     Range 0x...21b-0x...22a: a complex DWARF expression:  [$rcx] [$rsi] DW_OP_div [$rsi] DW_OP_mul DW_OP_stack_value
   (gdb) disassemble
      mov    (%rdi),%rcx            # rcx = o.price
      movslq 0xc(%rdi),%rsi         # rsi = o.tick
      mov    %rcx,%rax
      cqto
      idiv   %rsi                   # rax = price / tick  (ticks lives in rax for exactly 4 bytes of code)
      movslq 0x8(%rdi),%rax         # rax = o.qty          <- ticks is gone again
      sub    %rdx,%rcx              # rcx = price - price % tick   <- "snapped", computed WITHOUT ticks
      imul   %rcx,%rax
      ret
```

Read it the Ch. 4 way. `ticks` exists as a register value for a four-byte window after the `idiv`; everywhere else DWARF describes it as a *recipe* (`DW_OP_div` of two registers — "if you want it, compute it from `rcx` and `rsi`"), and gdb's `info locals` prints the recipe's result only when the current PC is inside a range it can evaluate. `snapped` **never exists**: the compiler rewrote `(price / tick) * tick` as `price - price % tick` and reused the remainder `idiv` left in `rdx`, so the variable you'd most like to inspect was optimized into a different expression. That is what `<optimized out>` means on an `-O2` binary — not "lost" but "not materialized here" — and the location list plus the disassembly recover the value: at the `sub`, `rdx` holds `-5` (the remainder) and `rcx` becomes `-100000`, which is the bug made visible (`-100005` should have snapped to `-100010`). An `-O0` build shows both variables plainly — and prints the same wrong `-700000`, so this is a logic bug that *survives* `-O0` and can be debugged there; §5.4.1 is about telling that kind from the kind that doesn't.

**2. `rr`: record once, replay deterministically, step backwards.** `rr` records the process (syscall results, signals, the few non-deterministic instructions) so that `rr replay` re-executes it *identically* under gdb, as many times as you like, in both directions:

```
   $ rr record ./optbug -100005            # the recording is a few MB; the run is ~1.5-2x slower
   $ rr replay                             # gdb attached to the deterministic replay
   (rr) break optbug.cpp:20
   (rr) continue
   (rr) finish                             # run snap_notional to its return
   Value returned is $1 = -700000          # wrong: expected -700070
   (rr) reverse-step                       # back INTO the callee, at its last statement
   15        return snapped * o.qty;
   (rr) p o.price % o.tick                 # the remainder the optimizer kept in rdx
   $2 = -5
   (rr) p $rdx
   $3 = -5                                 # identical on every replay — no heisenbug
   (rr) watch -l o.price                   # a hardware watchpoint ...
   (rr) reverse-continue                   # ... run BACKWARDS to the write that set it
```

Two limits matter for this book's systems. `rr` needs hardware performance counters (it counts retired conditional branches to place events — `kernel.perf_event_paranoid ≤ 1`, and most virtual machines don't expose the counter), and it serializes all threads onto **one core**, so a multi-threaded hot path records at a fraction of its real speed and with different interleavings (`rr record -h`, "chaos mode", randomizes scheduling to shake out races). And `rr` cannot record memory written by *another process* — a shared-memory ring (Ch. 54) or a DMA'd NIC ring (Ch. 67) changes under the recorded process without a syscall, so you record the replay *harness* that feeds the core from a capture (Ch. 86–89), never the live consumer of a shared segment.

**3. What debug info costs — measured.** The reason to keep `-g` on production builds is that it is free at run time; the reason people strip it is size. Both are measurable:

```
   build (GCC 13.3, -O2 -std=c++20)            binary      debug payload      .text identical to no -g?    compile+link
   optbug.cpp (the listing above)
     no -g                                      16,128 B    —                  (reference)                  —
     -g                                         24,760 B    +8.6 KB in-binary  YES (byte-for-byte)          —
     -g -gsplit-dwarf                           19,608 B    + 9,336 B .dwo     YES                          —
     -g, then objcopy --only-keep-debug/strip   16,080 B    13,400 B .debug    YES                          —
   a 20-line TU that instantiates std::map, std::unordered_map<std::string, std::vector<…>> and std::to_chars
     no -g                                      22,952 B    —                  (reference)                  0.96 s
     -g                                        335,832 B    320 KB (14x the code)  YES                      1.08 s
     -g -gsplit-dwarf                          220,072 B    + 538 KB .dwo      YES                          1.15 s
     -g -fno-omit-frame-pointer                336,040 B    (same DWARF)       NO: .text 8,325 -> 8,340 B   1.08 s
```

How it was measured: `objcopy -O binary --only-section=.text a.out a.text` on each pair and `cmp` the dumps (identical means the optimizer made the same decisions — `-g` only adds `.debug_*` sections and never changes codegen); `ls -l`/`size` for the sizes; `objcopy --only-keep-debug optbug optbug.debug && strip --strip-debug optbug && objcopy --add-gnu-debuglink=optbug.debug optbug` for the archive-and-strip workflow (`gdb` and `perf` find `optbug.debug` through the link, or through `debuginfod`); `-gsplit-dwarf` puts the bulk of the DWARF in a per-object `.dwo` that the linker never reads (`dwp` packs the `.dwo`s for shipping). Read it: **`-g` is codegen-neutral** — the `.text` bytes are identical, so there is no run-time argument against it, only a size one, and a stripped binary plus an archived `.debug` file is the same size as a never-`-g` build. The debug payload is **large relative to the code** (14× here; template-heavy TUs are the worst case), which is why you archive it rather than ship it. **Split DWARF is not a size optimization for one TU** (the `.dwo` carries its own string tables, so the total grew); its payoff is on a large binary, where it takes megabytes of DWARF out of every link and relink. The one flag in the table that *does* change code is **`-fno-omit-frame-pointer`**: +15 bytes of `.text` here (a `push %rbp; mov %rsp,%rbp; leave` per non-leaf function), and a run-time cost usually quoted at 0–2 % — representative; measure it on your hot loop with the Ch. 3 harness — in exchange for stacks that `perf` can unwind without DWARF or LBR (Ch. 2). The convention this book follows: `-O2 -g` (or `-O3 -g`) everywhere, `-fno-omit-frame-pointer` wherever you profile, strip at package time, archive the debug info keyed by build ID.

**Beyond one process: reproduce first.** Everything above assumes you can *run the bug*. In a trading system the bug usually happened once, in production, at 09:31:42, and the measurement that matters is your ability to reproduce it: if the system captures its input stream (Ch. 89) and the core is deterministic (Ch. 86), the production incident is reproduced by replaying the captured session offline, bit-for-bit (Ch. 86.2.2) — and *that* replay is what you point gdb and `rr` at. A crash leaves a **core dump** (symbolized with the archived `.debug` above — Ch. 88 on the fault handler that writes the crash record and on capturing the core without stalling survivors); a race or timing bug (Ch. 42) that doesn't recur in one replay is forced with deterministic stress — adversarial captured inputs (the microburst, the gap, the malformed packet — Ch. 57/58/84), under sanitizers (§5.4.1), many times. **Debugging effort goes into reproduction first**; a bug you can reproduce offline is a bug the tools above will solve.

## 5.4 Techniques

### 5.4.1 Debugging optimized and LTO builds

- **Keep debug info in optimized builds.** Compile the production build with `-g` (and consider `-g` + `-fno-omit-frame-pointer` — Ch. 2/22 — for usable stacks); `-g` does **not** change codegen, it only adds a `.debug_*` section the debugger/profiler reads. Ship or archive the debug info (split debug info: `-gsplit-dwarf`, or a separate `.debug` file via `objcopy --only-keep-debug` + `--add-gnu-debuglink` — the commands, and the measured sizes and the byte-identical `.text`, are §5.3) so you can symbolize a stripped production binary's cores and profiles offline. A stripped binary with no archived debug info is a binary you can't debug (§5.5).
- **Read `<optimized out>` as "read the location list" (Ch. 4).** When a variable is optimized out, its value is in a register for part of the function, is a DWARF *expression* over other registers, or was rewritten into a different computation altogether (§5.3's `snapped`); `info address`/`info scope` show which, and `disassemble` + `info registers` recover the value at the PC you care about. This is Ch. 4's skill applied live.
- **Build a debug reproduction — carefully.** A `-Og` or `-O0` build is easier to debug *if the bug survives the optimization change*. Many logic bugs do (debug them at `-Og`); but timing/UB/race bugs often *don't* (§5.2) — they exist *because* of the optimization. Know which kind you have: if the bug vanishes at `-O0`, it's likely a UB or race the optimizer exposed (Ch. 21/30/42) — debug it *as optimized* with sanitizers (below) rather than chasing it at `-O0` where it hides.
- **Sanitizers to catch the optimizer-exposed bugs (Ch. 42, 84).** UBSan/ASan/TSan (Ch. 42) find the UB, memory errors, and races that manifest as mysterious optimized-code behavior — run the reproduction under them. A bug that only appears at `-O2` and vanishes at `-O0` is very often a sanitizer finding (Ch. 84.2.2). The sanitizers are a *debugging* tool, not just a CI gate.

- **Standard-library debugging aids (name the standard).** `std::source_location` (C++20) tags a log record with file/line/function for the cost of one `lea` of a static (Ch. 81's site IDs); `std::stacktrace` (C++23) captures and, separately, symbolises a backtrace — ~4k cycles to capture and ~40k to symbolise at twelve frames, so it belongs on the terminate/assert path, never in a signal handler (Ch. 88 §88.4.2); `<debugging>` (C++26: `std::breakpoint`, `std::is_debugger_present`) standardises the `int3`-when-attached idiom. Ch. 20 §20.4.4 measures them and gives the toolchain availability and the C++17/20 fallbacks; Appendix H.2–H.3 carry the feature-test macros.

### 5.4.2 Record/replay, reverse debugging, and core-dump analysis

- **`rr` for reverse debugging.** Record the reproduction with `rr record`, then `rr replay` under `gdb`: set a breakpoint at the crash/symptom and **`reverse-continue` / `reverse-step`** backwards to the cause (the session, and the PMU/single-core/shared-memory limits, are §5.3). This is transformative for "how did this pointer become null?" — you run time backwards from the null deref to the write that nulled it. `rr`'s replay is deterministic, so watchpoints and repeated runs behave identically (no heisenbug — §5.5). The gold-standard technique for elusive logic bugs once you can reproduce them.

  **What `rr` cannot record, and what to use instead.** §5.3 named the three limits in a sentence; they decide which bugs in a Part XIII system `rr` can touch at all:

  - **Memory written by something it did not trace.** `rr` records a process's *inputs* — syscall results, signals, the handful of non-deterministic instructions — and replays them; it cannot see a write that arrives without a syscall. A shared-memory ring another process fills (Ch. 54, 86 §86.4.1), a NIC descriptor ring the hardware DMAs into (Ch. 67: ef_vi, DPDK), a `MAP_SHARED` telemetry segment the scraper touches (Ch. 83) — all change under the recorded process and the replay diverges or `rr` refuses (it detects `MAP_SHARED` writable mappings and warns). Recording the *live* consumer of any shared segment is therefore never an option; record the replay harness that feeds the deterministic core from a capture (Ch. 86 §86.2.2, Ch. 89 §89.4.5), which has exactly one input stream and no peers.
  - **Timing.** Every thread of the recordee runs on **one core**, serialised; a Ch. 37 pipeline that overlaps three stages on three cores records at a third of its speed with interleavings it never has in production, so a race that depends on real concurrency may not reproduce (`rr record -h`, chaos mode, randomises scheduling and priorities to shake out races — the right tool for *logic* races, not for timing-dependent tails).
  - **Hardware prerequisites.** Retired-conditional-branch counting (`perf_event_paranoid ≤ 1`, or `CAP_PERFMON`); most VMs do not expose it (none of this chapter's `rr` output was produced on the 4-vCPU VM the other measurements ran on); Zen needs a workaround for its branch-counter quirk, AArch64 support is recent and partial. `rdtsc`/`rdtscp` and `cpuid` are **trapped and emulated** (`PR_SET_TSC`, `arch_prctl(ARCH_SET_CPUID)`), so Ch. 17's stamps cost microseconds under recording and every TSC-based budget check in the recordee fires — turn the Ch. 83 §83.4.4 trigger off for recorded runs.

  ```
     tool                         records                          multi-core timing   shared memory / DMA   cost while recording      replays
     ──────────────────────────── ──────────────────────────────── ─────────────────── ───────────────────── ──────────────────────── ───────────────────
     rr                           one process tree, all syscalls   serialised (1 core) no (peers/DMA unseen) ~1.2–2×; rdtsc trapped   gdb, reverse-exec
     Pernosco (rr + analysis)     an rr trace, uploaded            same as rr          same as rr            same as rr               omniscient DB, web
     Undo LiveRecorder            process; JIT-instrumented        serialised          partial (shared-mem   ~2–5×                    UDB, reverse-exec
                                                                                        plugin for peers)
     the deterministic core       the core's INPUTS only (Ch. 89)  n/a — the core is   yes: the capture IS   ~0 (the capture writer   the core itself, at
     as its own recorder (Ch. 86) via the capture                  single-threaded     the shared-mem/DMA    is already off-path)     full speed; gdb/rr
                                                                                        content                                        on the harness
  ```

  The last row is the book's answer: the architecture of Ch. 86 makes the trading core its own recorder, at zero recording cost, with the capture (Ch. 89) as the trace — and `rr` becomes the tool you point at the *harness* when the replay has already reproduced the bug and you want to run it backwards. The forensic question that remains — *at which event did production first diverge from the replay?* — is Ch. 86 §86.4.6.

- **Deterministic replay through the core (Ch. 86–89) under a debugger.** Replay the captured production session (Ch. 89) through the deterministic core (Ch. 86) *under `gdb`* (or `rr`), reproducing the exact production behavior offline to inspect. Because the core is deterministic, the replay hits the same bug at the same input every time — you can breakpoint the exact message that triggered it. This is `rr`'s benefit at the *application* level, using the architecture you already built.
- **Core-dump post-mortem.** For a production crash, analyze the core (Ch. 88 §88.4.5) in `gdb` with matching debug info: `bt` (backtrace), inspect the corrupted state, correlate the faulting address with your data structures (Ch. 25). Combine with the capture (Ch. 89): the core shows the *end state*, the replay shows *how it got there*. Minidumps / partial cores and `MADV_DONTDUMP` (Ch. 88 §88.4.4) keep the dump cheap enough not to stall survivors; the handler's own crash record (Ch. 88 §88.4.1) usually names the bug before the core is opened.
- **Hardware watchpoints and conditional breakpoints.** In a replay/test (never the live hot path), hardware watchpoints (`watch`) catch *when* a memory location changes — invaluable for "who corrupted this?" (a use-after-free or buffer overflow — Ch. 84). Deterministic under `rr`; use them to pinpoint the write.

### 5.4.3 Production tracing without perturbing the hot path

Sometimes you must observe *production* (the bug won't reproduce offline yet). Do it with the lowest-overhead tools:

- **eBPF / bpftrace (Ch. 66) — the production-safe probe.** Attach a probe to a function, a tracepoint, or a USDT marker to record data (latency, arguments, counts) with low overhead (Ch. 66), *without* stopping the process or recompiling. This is how you observe a live low-latency system: non-stopping, and production-safe when placed with care — kernel tracepoints cost tens of ns per hit, but a uprobe on a user function (and an *enabled* USDT marker, which is implemented as a uprobe: its `nop` is patched to `int3`) traps into the kernel on every hit (~hundreds of ns to a few µs — Ch. 66 §66.3), so such probes belong on boundaries and slow paths, never inside the tick-to-trade inner loop. Trace the suspect path, gather the data offline, and reproduce.
- **Lock-free logging as a debug channel (Ch. 81).** The zero-overhead logger (Ch. 81) *is* a debugging tool: add a binary log record (~5–20 ns — Ch. 81 §81.3) on the suspect path to capture the state at the moment of interest, and decode it offline. Because it's off-path (formatting/IO on the consumer thread), it doesn't perturb timing the way `printf` does — the observer effect (§5.5) minimized. Debug by *recording*, not by *stopping*.
- **`perf` and hardware counters (Ch. 2) for behavior, not logic.** For "why is this slow/stalling" (as opposed to "why is this wrong"), `perf` and the PMU (Ch. 2) show the microarchitectural behavior — a cache miss, a mispredict, a front-end stall (Ch. 7/12/13) — without stopping the process. Debug performance bugs with counters (Ch. 2/93), logic bugs with replay/`rr`. What a `perf record` *costs* the hot thread per sample, and the syscall-free `rdpmc` alternative for per-stage counters, are Ch. 93 §93.4.4; what one tail event *was* is Ch. 90.
- **Always: gather in production, reproduce and fix offline.** Production tracing's job is to gather *enough to reproduce* (the input, the state, the timing signature) so you can then reproduce the bug offline (§5.3) and debug it with heavy tools. Don't try to *fix* on the live system; observe cheaply, reproduce, fix on the replay, verify with the regression gate (Ch. 93).

### 5.4.4 Looking inside a live process without stopping the hot thread

Everything above says "reproduce offline". Sometimes you cannot wait: the strategy is live, something is wrong *now*, and you need to see its state. The book's prohibition was "never attach gdb to a live hot process" (§5.5). The precise rule is narrower and more useful: **never *all-stop* attach**, and never touch the hot thread with `ptrace` at all — there are three ways to look that cost it nothing or next to nothing, measured here on the usual VM (Appendix L.3 caveats: GCC 13.3, Linux 6.18, 4 vCPUs at 2.7 GHz) against a target with a busy-poll hot thread on one core and a housekeeping thread on another:

```
   how you look                                                           hot thread's worst gap during it            notes
   ──────────────────────────────────────────────────────────────────── ──────────────────────────────────────────── ────────────────────────────────────────
   nobody looking (baseline)                                                   67 µs   (VM steal; ~0 on an isolated core)
   gdb -p PID (all-stop attach, `info threads`, detach)                   253,894 µs   (= the 326 ms gdb session)              EVERY thread SIGSTOPped for the session
   ptrace(PTRACE_SEIZE+INTERRUPT) the housekeeping thread only             in the noise band (409 µs; VM)               56 µs for seize+getregs+detach ON THAT THREAD; hot thread untouched
   process_vm_readv of a 64-B seqlock'd snapshot, 1,000,000 times          in the noise band (2.8 ms; VM)               328 ns per read, 999,991 of 1,000,000 consistent; zero stops
```

- **All-stop attach is a quarter-second outage.** `gdb -p` sends `SIGSTOP`-class stops to *every* thread (`PTRACE_ATTACH` per thread), loads symbols, runs its commands and detaches; the hot thread sat for 254 ms. On a Part XIII box the damage propagates: a stopped *producer* starves every shm consumer that spins on its ring (Ch. 54, 86 §86.4.1 — they see a stall, not an error), a stopped *gateway* misses heartbeats and the supervisor's restart policy fires (Ch. 86 §86.4.3), and a stopped feed handler's NIC ring overflows (Ch. 60 §60.5: hardware drops, Ch. 92). This is the number behind the prohibition.
- **Non-stop mode, one thread at a time.** gdb can attach without stopping anything but the thread you ask about: `gdb -q -batch -ex 'set non-stop on' -ex 'set pagination off' -ex "attach $PID" -ex "thread $HK_TID" -ex interrupt -ex bt -ex detach` (`set non-stop on` must precede `attach`; `gdbserver --attach :port $PID` plus `target extended-remote` from another box for the same, with `set scheduler-locking on` so a `step` moves one thread). Under the hood it is the row above: `PTRACE_SEIZE` + `PTRACE_INTERRUPT` on the chosen tid, which stops *that* thread for the ~50 µs of a register read and leaves the others running. The hot thread is still off limits — interrupting *it* is the all-stop row for one thread, and a hot thread's registers mid-loop tell you nothing a flight recorder does not (Ch. 90) — but a wedged housekeeping thread, a stuck scraper, a supervisor in a strange state are all fair game. Yama's `kernel.yama.ptrace_scope` (`1` on most distributions: a process may be traced only by its parent or with `CAP_SYS_PTRACE`; `2`: admin only; `3`: never) is the policy knob — set it to `2` on a trading host and run the inspection as a capability-holding sidecar, not as the user.
- **Out-of-process reads of versioned state — the zero-stop answer.** The Ch. 35 seqlock and the Ch. 54/83 versioned segment headers were designed so a *reader* never blocks the writer. That property does not stop at the process boundary: `process_vm_readv(pid, …)` (or `pread` on `/proc/PID/mem`) copies the writer's memory into the reader without stopping any thread — one syscall, no `ptrace` stop, ~330 ns per 64-byte read here — and the seqlock protocol tells the reader which copies were torn: the measurement rejected 9 of a million snapshots whose `seq` was odd or changed, and kept the rest. A sidecar that knows the segment layout (Ch. 83's header carries the version) can dump the live book's top levels, the strategy's position, the readiness word and the last trace record of every thread with no effect on the hot path at all. For memory that is *not* versioned (an arbitrary struct), `process_vm_readv` still works and still never stops the target — but the copy may be torn, exactly as a seqlock reader's would be without the sequence check, so treat it as a sample, not a truth.
- **Pretty-printers for the structures the book builds.** A gdb Python pretty-printer (`gdb.printing.RegexpCollectionPrettyPrinter`, auto-loaded from `app-gdb.py` next to the binary) turns a Ch. 34 SPSC ring into "head 48213 tail 48209, 4 in flight, slot[48209] = {seq 10022, px 100005}", a Ch. 37 sequence barrier into its gating cursors, and the Ch. 25 book into its top three levels — reading raw memory through `gdb.Value`, so the same printer works on a non-stop attach, on a core (Ch. 88 §88.4.5) and on an `rr` replay. Write them when you write the container; the day you need them is the day you have 254 ms to spend.

### 5.4.5 Finding the corrupting write in a zero-allocation design

Ch. 24 and Ch. 84 §84.4.2 built arenas and pools with generation counters, clear-on-free in debug builds and ASan over the arena in test, and conceded what the design loses: with no per-object guard, a stale pointer writes silently into a neighbouring object and the symptom appears far away. Ch. 42 §42.5 and Ch. 84 §84.3 say sanitizers do not ship. The production forensics that *do* ship, measured on the same VM:

- **A hardware data breakpoint on a live PID — zero cost until the write.** x86 has four debug-address registers; `perf_event_open(PERF_TYPE_BREAKPOINT, bp_type = HW_BREAKPOINT_W, bp_addr = &victim, bp_len = 8, pid = target)` arms one in the target process *from a sidecar*, with `PERF_SAMPLE_IP | PERF_SAMPLE_TID` so the first write delivers the writer's instruction pointer into the sidecar's mmap'd ring. The target's loop ran at **2.70 cycles/op before and 2.70–2.71 after arming** — the debug register is consulted by the address-generation hardware, not by any instruction the target executes — and when the planted bug wrote through a stale pointer, the sidecar reported `WRITE to position_limit by tid 19236 at ip 0x…717`, which `addr2line -e app -f -C -i` resolved to `stale_pointer_write(long*, long)` at `hwbp.cpp:24`: the `mov %rsi,(%rdi)` that did it (the IP is the instruction *after* a data breakpoint fires; `objdump` shows the store one byte back). The same from the shell: `perf record -e mem:0x7f…:w -p $PID -- sleep 60` then `perf script -F tid,ip,sym,srcline`. Limits that matter: four registers per core, so four addresses (or one 8-byte range each) at a time; the breakpoint is per *thread* unless `inherit`/the process-wide `pid` is used and the kernel then multiplexes the four registers across the threads it schedules on that core — a *single* hot thread per core is the easy case, and it is the one the book has; and the address must be known — from the symbol (`nm`/`readelf -s` + `/proc/PID/maps` load base, as above) or from the §5.4.4 sidecar's read of the pool's slot address. This is Ch. 24's "who corrupted this?" answered on the live box, in one write's time.
- **Guard pages around pool slabs.** Lay each slab out as *slab page | `PROT_NONE` page*, and an overflow off the slab's end faults at the write instead of corrupting the neighbour: `SIGSEGV` with `SEGV_ACCERR` and `si_addr` in the guard, caught by Ch. 88's handler with the writer's `rip` — 23,400 cycles (8.7 µs) from the write to a recovered handler in the measurement, which is the cost of *being told*, paid once. The costs of *having* them: the address range doubles (and the TLB reach halves — Ch. 15) and every guard is a VMA, so a 32 MiB pool of 4 KiB slabs is 16,384 VMAs against `vm.max_map_count`'s default 65,530 and a slower `find_vma` on every fault. Measured, random 256-byte object reads over 32 MiB of slabs cost **27.4 cycles packed vs 26.1 guarded** — no penalty, because at that working set *both* layouts miss the STLB on nearly every access (8192 vs 16384 pages against a 2048-entry STLB); the penalty appears when the packed pool fits the STLB (≤ 8 MiB of 4 KiB pages, or the 2 MiB-page equivalent — Ch. 15) and the guarded one no longer does. Use guard pages on the pools whose objects are *handed out by index* (orders, book nodes — Ch. 24 §24.2) where an off-by-one walks off a slab, size the slab so the pool still fits the TLB reach you need, and use 2 MiB slabs with 4 KiB guards when the pool is large (the guard costs one small-page TLB entry per slab, not one per object).
- **Poison and clear-on-free, in production.** Ch. 84 §84.4.2's debug-build `memset` of freed slots to a pattern (`0xdeadbeef`-class values that are *not* valid indices, prices or pointers) is cheap enough to keep on in production for the *order-sized* pools (a 64-byte `memset` is two `vmovdqu` stores — Ch. 29 — on a path that just paid for a cache miss anyway), and it turns a use-after-free from a silent wrong number into a `SEGV_MAPERR` at the poison address or an assertion on an impossible quantity, both of which Ch. 88's record names. Generation-counter handles (Ch. 84 §84.4.2) catch the same bug one layer earlier at one compare per access.
- **Hardened library modes — the shippable middle.** `-D_GLIBCXX_ASSERTIONS` (libstdc++) makes `operator[]` on `vector`/`array`/`span`, `front()`/`back()`, iterator dereferences and a few hundred other preconditions a predictable compare-and-`__builtin_trap`; libc++'s `-D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_FAST` is the equivalent (and the model for C++26's standardised *hardened preconditions*). Measured on a decode-and-apply loop that indexes a `vector<uint16_t>` of offsets, a `span<const uint8_t>` of wire bytes and an `array<Level, 4096>` book — three checked accesses per message — **2.21–2.29 cycles/msg off vs 2.32–2.39 on: +3–8 %**, with the bounds that are provable from the surrounding code (the `& 4095` mask, the loop's own limit) folded away entirely. That is the number behind Ch. 84 §84.3's "on unless measured otherwise" row, and the production alternative to the ASan build Ch. 42 §42.5 forbids: a corrupted index traps at the access with a `SIGILL`/`SIGTRAP` Ch. 88's handler turns into a crash record naming the line. (libc++ was not installed on the measurement VM; the libstdc++ figure is the one measured.)

## 5.5 Pitfalls & anti-patterns: the observer effect and optimized-out state

- **The observer effect / heisenbug (the cardinal pitfall).** Adding a `printf`, a breakpoint, or a heavy probe changes the timing and CPU state (Ch. 43, 81), hiding timing-dependent and race bugs (Ch. 30, 42) — the bug "disappears" the moment you look. Observe with the lowest-overhead tool (lock-free log — Ch. 81, eBPF — Ch. 66), and reproduce offline where you can observe freely (§5.2, §5.4.3).
- **All-stop attaching `gdb` to a live hot process.** `gdb -p` stops *every* thread for the whole session — 254 ms measured (§5.4.4) — violating every latency guarantee, starving every shm consumer of a stopped producer (Ch. 54, 86), tripping the supervisor's heartbeat (Ch. 86 §86.4.3), and deadlocking anything that spins on a lock the stopped thread holds (Ch. 32 §32.5) or a NIC ring nobody else services (Ch. 60, 67). Never all-stop attach, and never `ptrace` the hot thread; look with non-stop mode on a housekeeping thread, with `process_vm_readv` of versioned state, or with a hardware breakpoint armed from a sidecar (§5.4.4–§5.4.5) — and debug the *logic* on a replay (§5.2).
- **Recording the live consumer of a shared segment with `rr`.** The ring another process (or the NIC) writes is invisible to `rr`'s recorder; the replay diverges or `rr` refuses the `MAP_SHARED` mapping. Record the replay harness fed from the capture (§5.4.2's table), never the live process.
- **`printf` debugging on the hot path.** A synchronous `printf`/`std::cout` is ~1–10 µs (format + lock + `write` syscall — Ch. 81 §81.3) — it perturbs timing (heisenbug) *and* violates the latency budget. Use the lock-free binary logger (Ch. 81) as the debug channel instead (§5.4.3).
- **Chasing a `-O2`-only bug at `-O0`.** A bug that vanishes at `-O0` is very likely UB or a race the optimizer exposed (Ch. 21/30/84) — debugging at `-O0` hides it. Debug it *as optimized*, under sanitizers (§5.4.1); the disappearance at `-O0` is a *clue* (it's UB/race), not a debugging strategy.
- **No debug info for production binaries.** Stripping the production binary and keeping no archived debug info → cores and profiles are unsymbolizable (`??` everywhere). Always build with `-g` (codegen-neutral) and **archive the debug info** (split-dwarf / separate `.debug`) matched to each release (§5.4.1).
- **Trusting the source view of optimized code.** Believing `gdb`'s source-line and variable display on an `-O2` binary when the optimizer reordered/eliminated things — the display can be misleading. Cross-check with the asm (Ch. 4) when the source view seems impossible (§5.4.1).
- **Core dumps stalling survivors (Ch. 88).** A synchronous, huge, on-the-hot-disk core dump that freezes the box — hurting the *surviving* processes (Ch. 88 §88.3 measures it). Make the dump small and the handling bounded/off-path (Ch. 88 §88.4.4).
- **Debugging without reproducing.** Guessing at a bug you can't reproduce, adding speculative fixes, "fixing" it by luck (or by perturbation that hides it). Invest in reproduction first (capture-replay, `rr` — §5.3); an unreproduced bug isn't fixed, it's hidden.
- **Fixing on the live system.** Editing/patching production to chase a bug — changing the thing you're debugging under load. Observe in production, reproduce and fix offline, verify with the regression gate (Ch. 93), then deploy (§5.4.3).

## 5.6 Exercises & checklist

**Exercises:**

1. **Debug optimized code.** Build `optbug.cpp` (§5.3) at `-O2 -g`, break in `snap_notional`, and recover `ticks` and `snapped` from the location lists, registers and disassembly; then change `tick` to `int64_t` and `qty` to `int16_t` and see how the location lists move. Fix the bug (floor division) and confirm with both builds. Then take a real `-O2`-only bug of your own and classify it (logic vs UB/race) from whether it survives `-O0` (§5.4.1).
2. **Reverse-debug with `rr`.** Replay §5.3's `rr` session; then record a null-deref / corruption bug of your own, set a hardware watchpoint on the corrupted location and `reverse-continue` from the crash to the write that caused it (§5.4.2). Contrast with forward-only `gdb`, and check `rr record` works at all on your box (`perf_event_paranoid`, VM counters).
3. **Capture-replay reproduction.** Reproduce a "production" bug by replaying a captured session (Ch. 89) through the deterministic core (Ch. 86) under `gdb` — breakpoint the exact triggering message (§5.4.2). Verify determinism (same bug every replay).
4. **Heisenbug.** Create a timing-dependent bug that vanishes when you add a `printf` but persists with a lock-free log record (Ch. 81) — demonstrate the observer effect and the low-overhead workaround (§5.5, §5.4.3).
5. **Production trace → offline fix.** Use bpftrace (Ch. 66) to capture the arguments/state of a suspect path in a running system without stopping it, reproduce the bug offline from the captured data, fix it, and verify with the regression gate (Ch. 93) (§5.4.3).

**Checklist:**

- [ ] Production builds carry **`-g` debug info** (codegen-neutral — the `.text` is byte-identical, §5.3), and the debug info is **archived per release** (split-dwarf / separate `.debug` + debuglink or build ID) for offline symbolization (§5.3, §5.4.1, §5.5).
- [ ] Debugging **reproduces offline first** — via **capture-replay** (Ch. 86–89) or **`rr`** — rather than chasing the bug live (§5.3, §5.5).
- [ ] The **live hot path is never** attached-to, `printf`'d, or breakpointed; observation uses **lock-free logging** (Ch. 81), **eBPF/bpftrace** (Ch. 66), or **hardware counters** (Ch. 2) (§5.4.3, §5.5).
- [ ] **Optimized-code bugs** are debugged **as optimized** (asm/registers — Ch. 4, sanitizers — Ch. 42) when they don't survive `-O0`; the `-O0` disappearance is read as a **UB/race clue** (§5.4.1).
- [ ] **`rr` reverse debugging** is used for elusive logic bugs once reproducible; **core dumps** (async/off-path — Ch. 86) + capture-replay for crashes (§5.4.2).
- [ ] The **observer effect** is respected — the lowest-overhead tool that can see the bug, heavy tools only on the replay (§5.2, §5.5).
- [ ] Fixes are made and verified **offline** (regression gate — Ch. 93), never on the live system (§5.5).
- [ ] Live inspection is **non-stop**: housekeeping threads only via `PTRACE_SEIZE`/gdb non-stop, versioned state via `process_vm_readv`, corruption via a sidecar-armed hardware breakpoint (§5.4.4–§5.4.5); `ptrace_scope` set and the sidecar's capability granted; pretty-printers written for every ring and the book.
- [ ] Production builds ship with `_GLIBCXX_ASSERTIONS`/libc++ hardening on (measured), poison-on-free for the order-sized pools, guard pages where an index can walk off a slab; the ASan/TSan builds stay in CI (Ch. 42).
- [ ] `rr` is pointed at the replay harness, never at the live consumer of a shared segment; the replay's divergence is localised with Ch. 86 §86.4.6's digests before anyone reaches for reverse-execution.

## 5.7 References

- The **`rr`** (rr-project.org) documentation and papers — record/replay and reverse execution, the core technique of §5.4.2.
- The `gdb` documentation on **debugging optimized code**, DWARF location expressions, split debug info, and reverse debugging (§5.4.1–2).
- Ch. 4 (reading asm — debugging at the machine level), Ch. 22 (build flags; sanitizer flags — Appendix D), Ch. 42 (sanitizers — the optimizer-exposed-bug finders), Ch. 66 (eBPF/bpftrace — production tracing), Ch. 81 (lock-free logging — the debug channel), Ch. 86–89 (deterministic replay — the reproduction engine).
- The Linux `core(5)` / `coredump` documentation and minidump tooling — production crash capture (§5.4.2; Ch. 86).

## 5.8 Additional Reading

- Talks on `rr`, time-travel debugging, and debugging optimized C++ (CppCon, the `rr` authors) — the reproduce-then-reverse-debug workflow (§5.4.2).
- Brendan Gregg's material on production debugging with eBPF (Ch. 66 references) — non-perturbing observation of live systems (§5.4.3).
- Writing on heisenbugs, the observer effect, and debugging concurrency (Ch. 42) — why perturbation hides timing bugs (§5.5).
- Ch. 86–89 (*State Machine / Capture-Replay*) — the reproduction substrate; Ch. 41 (*std::execution*) — async control flow whose bugs need these techniques; **Appendix D** — the sanitizer flags (the debug-info flags are in §5.3); **Appendix F** — heisenbug / record-replay / debug-info glossary.

---

*Next: Ch. 6 — System Setup for Low Latency, where we leave the code and tune the machine underneath it: C-states, frequency scaling, hyperthreading, and the speculative-execution mitigations whose cost you can now reason about — building the quiet, deterministic box the rest of the book assumes.*
