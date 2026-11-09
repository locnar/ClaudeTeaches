# Part I — Foundations & Methodology

# Chapter 5 — Debugging Low-Latency & Optimized Code

> **Prerequisites:** Ch. 2 (perf/PMU — production observation) and Ch. 4 (reading asm — debugging optimized code means reading its codegen). The chapter is self-contained: its worked example (§5.3) needs only `g++ -O2 -g`, gdb and `rr`, and the build flags it discusses are Ch. 22's (forward-referenced, not needed first). The production machinery it leans on later — capture/replay, lock-free logging, eBPF — is developed in Parts X–XII and is forward-referenced here.
>
> **Leads into:** the reproduce-then-debug approach draws on tools built later — the build flags (Ch. 22) and sanitizer flags (Appendix D), the sanitizers (Ch. 40), zero-overhead logging as a debug channel (Ch. 73), and — the single best debugging tool — the deterministic state machine and capture/replay (Ch. 78–80). Every "verify the codegen" and async pipeline in the book (e.g. Ch. 39) eventually needs what this chapter teaches. It's the methodology Part I implied: how to *debug* a low-latency system when you can't attach a debugger to the hot path and the optimizer has erased your variables.

---

## 5.1 Why it matters: you can't `printf` your way out of a nanosecond bug

Part I taught you to *measure* — profile, benchmark, read the asm. This chapter is about the other half of the craft: *debugging* — finding out why the system did something wrong. And low-latency systems are uniquely hostile to every debugging technique you learned on ordinary software, because the two properties that make them fast make them nearly undebuggable by conventional means.

First, **the code is optimized past recognition.** You ship `-O2`/`-O3` with LTO and PGO (Ch. 22); the optimizer inlines functions out of existence (Ch. 14), keeps variables in registers or eliminates them entirely, reorders and merges instructions (Ch. 11), and vectorizes loops (Ch. 29). Attach `gdb` to that binary and you get `<optimized out>` where your variable should be, a call stack that only matches your source if you kept debug info (gdb reconstructs inlined frames from DWARF — strip `-g` and they collapse; tail-called frames vanish regardless), and a program counter that jumps around because instructions were reordered. The debugger and the source have diverged — debugging optimized code is a skill of its own (Ch. 4's "read the machine" applied to a live process).

Second, **you cannot perturb the hot path to observe it.** The classic debugging moves — add a `printf`, set a breakpoint, single-step, attach a debugger — all *stop or slow the thread*, and a low-latency thread that's stopped isn't doing the thing you're trying to debug. Worse, they change the timing: the bug is often a *race* or a *timing-dependent* condition (Ch. 30, 40), and the moment you add a `printf` (~1–10 µs of formatting, locking and a `write` syscall on every flush — every line on a terminal or stderr, every ~4 KiB to a file — Ch. 73 §73.3) or a breakpoint, the timing shifts and the bug vanishes — a **heisenbug**. Attaching `gdb` to a live hot process stalls it for milliseconds (blowing every latency guarantee) and, if it holds a lock or a NIC ring, can take the whole system down. You cannot debug the hot path *while it's hot*.

The resolution is a different toolkit, built on the same architecture the book already established: **make the system deterministic and captured (Ch. 78–80), then debug the *replay*, not the live system.** Because a deterministic state machine (Ch. 78) fed a captured input stream (Ch. 80) reproduces the exact bug offline, bit-for-bit, you can attach every heavy tool — debugger, sanitizer, record/replay — to the reproduction *without* touching production. This chapter covers debugging optimized code (§5.2 the mental model), a worked gdb/`rr`/split-DWARF session on an `-O2` binary with the measured cost of debug info (§5.3), the techniques — record/replay, core dumps, replay-debugging, production tracing (§5.4), and the pitfalls, chiefly the observer effect (§5.5). It's how you find the bug the profiler can't see and the debugger can't safely touch.

## 5.2 Mental model: optimized code, the observer effect, and reproduce-then-debug

Three ideas frame low-latency debugging:

- **Optimized code doesn't match your source, so debug at the level it runs.** With `-O2`/LTO, the mapping from source lines to instructions is many-to-many and lossy: variables live in registers (and get reused), functions are inlined (their frames vanish), dead code is deleted (including, if you had UB — Ch. 21/76 — code you *thought* would run), and instructions are reordered. To debug it you (a) keep **debug info even in optimized builds** (`-g` doesn't change codegen — §5.4), so the debugger can map what it *can*; (b) read the **asm** (Ch. 4) when the source view lies; and (c) sometimes build a **`-Og`** (optimize-for-debugging) or `-O0` reproduction where the bug still occurs. But beware: some bugs *only* appear at `-O2` (the optimizer exposed a UB — Ch. 21, or a race the reordering made visible — Ch. 30), and vanish at `-O0` — those you must debug *as optimized*, reading registers and asm.
- **The observer effect is the central constraint.** Observing the hot path changes it. A `printf`, a breakpoint, a debugger attach, even a heavyweight `perf` mode perturbs timing and CPU state enough to hide timing-dependent bugs and to violate the latency guarantees you're debugging. The corollary: **observe with the lowest-overhead tool that can see the bug** (a lock-free log record — Ch. 73, an eBPF probe — Ch. 63, a hardware counter — Ch. 2), and do heavy observation *off* the hot path (on a replay). Never debug a heisenbug by adding observation that changes the timing.
- **Reproduce-then-debug (the winning strategy).** Instead of catching the bug live (which you can't safely do), *reproduce it offline* and debug the reproduction with unlimited tooling. The two engines for reproduction: **deterministic replay** (Ch. 78–80 — feed the captured production input through the deterministic core and the bug recurs exactly), and **record/replay debuggers** (`rr` — record the program's execution once, then replay it deterministically forwards *and backwards* as many times as you like). Both turn a fleeting, timing-dependent, production-only bug into a stationary, inspectable, infinitely-repeatable one. This is why the book built determinism (Ch. 78) and capture (Ch. 80): they are, among other things, the debugging substrate.

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

Two limits matter for this book's systems. `rr` needs hardware performance counters (it counts retired conditional branches to place events — `kernel.perf_event_paranoid ≤ 1`, and most virtual machines don't expose the counter), and it serializes all threads onto **one core**, so a multi-threaded hot path records at a fraction of its real speed and with different interleavings (`rr record -h`, "chaos mode", randomizes scheduling to shake out races). And `rr` cannot record memory written by *another process* — a shared-memory ring (Ch. 51) or a DMA'd NIC ring (Ch. 64) changes under the recorded process without a syscall, so you record the replay *harness* that feeds the core from a capture (Ch. 78–80), never the live consumer of a shared segment.

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

**Beyond one process: reproduce first.** Everything above assumes you can *run the bug*. In a trading system the bug usually happened once, in production, at 09:31:42, and the measurement that matters is your ability to reproduce it: if the system captures its input stream (Ch. 80) and the core is deterministic (Ch. 78), the production incident is reproduced by replaying the captured session offline, bit-for-bit (Ch. 78.2.2) — and *that* replay is what you point gdb and `rr` at. A crash leaves a **core dump** (symbolized with the archived `.debug` above — Ch. 79 on the fault handler that writes the crash record and on capturing the core without stalling survivors); a race or timing bug (Ch. 40) that doesn't recur in one replay is forced with deterministic stress — adversarial captured inputs (the microburst, the gap, the malformed packet — Ch. 54/55/76), under sanitizers (§5.4.1), many times. **Debugging effort goes into reproduction first**; a bug you can reproduce offline is a bug the tools above will solve.

## 5.4 Techniques

### 5.4.1 Debugging optimized and LTO builds

- **Keep debug info in optimized builds.** Compile the production build with `-g` (and consider `-g` + `-fno-omit-frame-pointer` — Ch. 2/22 — for usable stacks); `-g` does **not** change codegen, it only adds a `.debug_*` section the debugger/profiler reads. Ship or archive the debug info (split debug info: `-gsplit-dwarf`, or a separate `.debug` file via `objcopy --only-keep-debug` + `--add-gnu-debuglink` — the commands, and the measured sizes and the byte-identical `.text`, are §5.3) so you can symbolize a stripped production binary's cores and profiles offline. A stripped binary with no archived debug info is a binary you can't debug (§5.5).
- **Read `<optimized out>` as "read the location list" (Ch. 4).** When a variable is optimized out, its value is in a register for part of the function, is a DWARF *expression* over other registers, or was rewritten into a different computation altogether (§5.3's `snapped`); `info address`/`info scope` show which, and `disassemble` + `info registers` recover the value at the PC you care about. This is Ch. 4's skill applied live.
- **Build a debug reproduction — carefully.** A `-Og` or `-O0` build is easier to debug *if the bug survives the optimization change*. Many logic bugs do (debug them at `-Og`); but timing/UB/race bugs often *don't* (§5.2) — they exist *because* of the optimization. Know which kind you have: if the bug vanishes at `-O0`, it's likely a UB or race the optimizer exposed (Ch. 21/30/40) — debug it *as optimized* with sanitizers (below) rather than chasing it at `-O0` where it hides.
- **Sanitizers to catch the optimizer-exposed bugs (Ch. 40, 76).** UBSan/ASan/TSan (Ch. 40) find the UB, memory errors, and races that manifest as mysterious optimized-code behavior — run the reproduction under them. A bug that only appears at `-O2` and vanishes at `-O0` is very often a sanitizer finding (Ch. 76.2.2). The sanitizers are a *debugging* tool, not just a CI gate.

- **Standard-library debugging aids (name the standard).** `std::source_location` (C++20, GCC 11+/Clang 16+) tags a log record with file/line/function at zero run-time cost — use it in the binary logger's site IDs (Ch. 73). `std::stacktrace` (C++23; libstdc++ 12+, link `-lstdc++_libbacktrace` on GCC 12–13 and `-lstdc++exp` on GCC 14; not in libc++ 18) gives a symbolized backtrace for crash reports and assertion failures — off the hot path, it walks the stack and reads DWARF. `std::breakpoint`/`std::breakpoint_if_debugging` (`<debugging>`, C++26, P2546) standardize the `int3`-when-attached idiom; until a toolchain ships them, `__builtin_debugtrap()` (Clang) or `raise(SIGTRAP)` do the same. See Appendix H for the feature-test macros.

### 5.4.2 Record/replay, reverse debugging, and core-dump analysis

- **`rr` for reverse debugging.** Record the reproduction with `rr record`, then `rr replay` under `gdb`: set a breakpoint at the crash/symptom and **`reverse-continue` / `reverse-step`** backwards to the cause (the session, and the PMU/single-core/shared-memory limits, are §5.3). This is transformative for "how did this pointer become null?" — you run time backwards from the null deref to the write that nulled it. `rr`'s replay is deterministic, so watchpoints and repeated runs behave identically (no heisenbug — §5.5). The gold-standard technique for elusive logic bugs once you can reproduce them.
- **Deterministic replay through the core (Ch. 78–80) under a debugger.** Replay the captured production session (Ch. 80) through the deterministic core (Ch. 78) *under `gdb`* (or `rr`), reproducing the exact production behavior offline to inspect. Because the core is deterministic, the replay hits the same bug at the same input every time — you can breakpoint the exact message that triggered it. This is `rr`'s benefit at the *application* level, using the architecture you already built.
- **Core-dump post-mortem.** For a production crash, analyze the core (Ch. 79 §79.4.5) in `gdb` with matching debug info: `bt` (backtrace), inspect the corrupted state, correlate the faulting address with your data structures (Ch. 25). Combine with the capture (Ch. 80): the core shows the *end state*, the replay shows *how it got there*. Minidumps / partial cores and `MADV_DONTDUMP` (Ch. 79 §79.4.4) keep the dump cheap enough not to stall survivors; the handler's own crash record (Ch. 79 §79.4.1) usually names the bug before the core is opened.
- **Hardware watchpoints and conditional breakpoints.** In a replay/test (never the live hot path), hardware watchpoints (`watch`) catch *when* a memory location changes — invaluable for "who corrupted this?" (a use-after-free or buffer overflow — Ch. 76). Deterministic under `rr`; use them to pinpoint the write.

### 5.4.3 Production tracing without perturbing the hot path

Sometimes you must observe *production* (the bug won't reproduce offline yet). Do it with the lowest-overhead tools:

- **eBPF / bpftrace (Ch. 63) — the production-safe probe.** Attach a probe to a function, a tracepoint, or a USDT marker to record data (latency, arguments, counts) with low overhead (Ch. 63), *without* stopping the process or recompiling. This is how you observe a live low-latency system: non-stopping, and production-safe when placed with care — kernel tracepoints cost tens of ns per hit, but a uprobe on a user function (and an *enabled* USDT marker, which is implemented as a uprobe: its `nop` is patched to `int3`) traps into the kernel on every hit (~hundreds of ns to a few µs — Ch. 63 §63.3), so such probes belong on boundaries and slow paths, never inside the tick-to-trade inner loop. Trace the suspect path, gather the data offline, and reproduce.
- **Lock-free logging as a debug channel (Ch. 73).** The zero-overhead logger (Ch. 73) *is* a debugging tool: add a binary log record (~5–20 ns — Ch. 73 §73.3) on the suspect path to capture the state at the moment of interest, and decode it offline. Because it's off-path (formatting/IO on the consumer thread), it doesn't perturb timing the way `printf` does — the observer effect (§5.5) minimized. Debug by *recording*, not by *stopping*.
- **`perf` and hardware counters (Ch. 2) for behavior, not logic.** For "why is this slow/stalling" (as opposed to "why is this wrong"), `perf` and the PMU (Ch. 2) show the microarchitectural behavior — a cache miss, a mispredict, a front-end stall (Ch. 7/12/13) — without stopping the process. Debug performance bugs with counters (Ch. 2/84), logic bugs with replay/`rr`.
- **Always: gather in production, reproduce and fix offline.** Production tracing's job is to gather *enough to reproduce* (the input, the state, the timing signature) so you can then reproduce the bug offline (§5.3) and debug it with heavy tools. Don't try to *fix* on the live system; observe cheaply, reproduce, fix on the replay, verify with the regression gate (Ch. 84).

## 5.5 Pitfalls & anti-patterns: the observer effect and optimized-out state

- **The observer effect / heisenbug (the cardinal pitfall).** Adding a `printf`, a breakpoint, or a heavy probe changes the timing and CPU state (Ch. 41, 73), hiding timing-dependent and race bugs (Ch. 30, 40) — the bug "disappears" the moment you look. Observe with the lowest-overhead tool (lock-free log — Ch. 73, eBPF — Ch. 63), and reproduce offline where you can observe freely (§5.2, §5.4.3).
- **Attaching `gdb` to the live hot path.** Stops the thread for milliseconds (violating every latency guarantee) and can deadlock the system if the stopped thread holds a spinlock (its holder frozen — Ch. 32 §32.5) or a user-space NIC ring nobody else services (Ch. 57, 64). Never attach to a live hot process; debug a replay/reproduction (§5.2).
- **`printf` debugging on the hot path.** A synchronous `printf`/`std::cout` is ~1–10 µs (format + lock + `write` syscall — Ch. 73 §73.3) — it perturbs timing (heisenbug) *and* violates the latency budget. Use the lock-free binary logger (Ch. 73) as the debug channel instead (§5.4.3).
- **Chasing a `-O2`-only bug at `-O0`.** A bug that vanishes at `-O0` is very likely UB or a race the optimizer exposed (Ch. 21/30/76) — debugging at `-O0` hides it. Debug it *as optimized*, under sanitizers (§5.4.1); the disappearance at `-O0` is a *clue* (it's UB/race), not a debugging strategy.
- **No debug info for production binaries.** Stripping the production binary and keeping no archived debug info → cores and profiles are unsymbolizable (`??` everywhere). Always build with `-g` (codegen-neutral) and **archive the debug info** (split-dwarf / separate `.debug`) matched to each release (§5.4.1).
- **Trusting the source view of optimized code.** Believing `gdb`'s source-line and variable display on an `-O2` binary when the optimizer reordered/eliminated things — the display can be misleading. Cross-check with the asm (Ch. 4) when the source view seems impossible (§5.4.1).
- **Core dumps stalling survivors (Ch. 79).** A synchronous, huge, on-the-hot-disk core dump that freezes the box — hurting the *surviving* processes (Ch. 79 §79.3 measures it). Make the dump small and the handling bounded/off-path (Ch. 79 §79.4.4).
- **Debugging without reproducing.** Guessing at a bug you can't reproduce, adding speculative fixes, "fixing" it by luck (or by perturbation that hides it). Invest in reproduction first (capture-replay, `rr` — §5.3); an unreproduced bug isn't fixed, it's hidden.
- **Fixing on the live system.** Editing/patching production to chase a bug — changing the thing you're debugging under load. Observe in production, reproduce and fix offline, verify with the regression gate (Ch. 84), then deploy (§5.4.3).

## 5.6 Exercises & checklist

**Exercises:**

1. **Debug optimized code.** Build `optbug.cpp` (§5.3) at `-O2 -g`, break in `snap_notional`, and recover `ticks` and `snapped` from the location lists, registers and disassembly; then change `tick` to `int64_t` and `qty` to `int16_t` and see how the location lists move. Fix the bug (floor division) and confirm with both builds. Then take a real `-O2`-only bug of your own and classify it (logic vs UB/race) from whether it survives `-O0` (§5.4.1).
2. **Reverse-debug with `rr`.** Replay §5.3's `rr` session; then record a null-deref / corruption bug of your own, set a hardware watchpoint on the corrupted location and `reverse-continue` from the crash to the write that caused it (§5.4.2). Contrast with forward-only `gdb`, and check `rr record` works at all on your box (`perf_event_paranoid`, VM counters).
3. **Capture-replay reproduction.** Reproduce a "production" bug by replaying a captured session (Ch. 80) through the deterministic core (Ch. 78) under `gdb` — breakpoint the exact triggering message (§5.4.2). Verify determinism (same bug every replay).
4. **Heisenbug.** Create a timing-dependent bug that vanishes when you add a `printf` but persists with a lock-free log record (Ch. 73) — demonstrate the observer effect and the low-overhead workaround (§5.5, §5.4.3).
5. **Production trace → offline fix.** Use bpftrace (Ch. 63) to capture the arguments/state of a suspect path in a running system without stopping it, reproduce the bug offline from the captured data, fix it, and verify with the regression gate (Ch. 84) (§5.4.3).

**Checklist:**

- [ ] Production builds carry **`-g` debug info** (codegen-neutral — the `.text` is byte-identical, §5.3), and the debug info is **archived per release** (split-dwarf / separate `.debug` + debuglink or build ID) for offline symbolization (§5.3, §5.4.1, §5.5).
- [ ] Debugging **reproduces offline first** — via **capture-replay** (Ch. 78–80) or **`rr`** — rather than chasing the bug live (§5.3, §5.5).
- [ ] The **live hot path is never** attached-to, `printf`'d, or breakpointed; observation uses **lock-free logging** (Ch. 73), **eBPF/bpftrace** (Ch. 63), or **hardware counters** (Ch. 2) (§5.4.3, §5.5).
- [ ] **Optimized-code bugs** are debugged **as optimized** (asm/registers — Ch. 4, sanitizers — Ch. 40) when they don't survive `-O0`; the `-O0` disappearance is read as a **UB/race clue** (§5.4.1).
- [ ] **`rr` reverse debugging** is used for elusive logic bugs once reproducible; **core dumps** (async/off-path — Ch. 78) + capture-replay for crashes (§5.4.2).
- [ ] The **observer effect** is respected — the lowest-overhead tool that can see the bug, heavy tools only on the replay (§5.2, §5.5).
- [ ] Fixes are made and verified **offline** (regression gate — Ch. 84), never on the live system (§5.5).

## 5.7 References

- The **`rr`** (rr-project.org) documentation and papers — record/replay and reverse execution, the core technique of §5.4.2.
- The `gdb` documentation on **debugging optimized code**, DWARF location expressions, split debug info, and reverse debugging (§5.4.1–2).
- Ch. 4 (reading asm — debugging at the machine level), Ch. 22 (build flags; sanitizer flags — Appendix D), Ch. 40 (sanitizers — the optimizer-exposed-bug finders), Ch. 63 (eBPF/bpftrace — production tracing), Ch. 73 (lock-free logging — the debug channel), Ch. 78–80 (deterministic replay — the reproduction engine).
- The Linux `core(5)` / `coredump` documentation and minidump tooling — production crash capture (§5.4.2; Ch. 78).

## 5.8 Additional Reading

- Talks on `rr`, time-travel debugging, and debugging optimized C++ (CppCon, the `rr` authors) — the reproduce-then-reverse-debug workflow (§5.4.2).
- Brendan Gregg's material on production debugging with eBPF (Ch. 63 references) — non-perturbing observation of live systems (§5.4.3).
- Writing on heisenbugs, the observer effect, and debugging concurrency (Ch. 40) — why perturbation hides timing bugs (§5.5).
- Ch. 78–80 (*State Machine / Capture-Replay*) — the reproduction substrate; Ch. 39 (*std::execution*) — async control flow whose bugs need these techniques; **Appendix D** — the sanitizer flags (the debug-info flags are in §5.3); **Appendix F** — heisenbug / record-replay / debug-info glossary.

---

*Next: Ch. 6 — System Setup for Low Latency, where we leave the code and tune the machine underneath it: C-states, frequency scaling, hyperthreading, and the speculative-execution mitigations whose cost you can now reason about — building the quiet, deterministic box the rest of the book assumes.*
