# Part I — Foundations & Methodology

# Chapter 5 — Debugging Low-Latency & Optimized Code

> **Prerequisites:** Ch. 2 (perf/PMU — production observation) and Ch. 4 (reading asm — debugging optimized code means reading its codegen). This chapter establishes debugging *methodology* as part of the foundations; the specific machinery it leans on is developed later and forward-referenced here — you don't need it first.
>
> **Connects forward to:** the reproduce-then-debug approach draws on tools built later — the build/debug-info flags (Ch. 22, Appendix D), the sanitizers (Ch. 40), zero-overhead logging as a debug channel (Ch. 71), and — the single best debugging tool — the deterministic state machine and capture/replay (Ch. 74–75). Every "verify the codegen" and async pipeline in the book (e.g. Ch. 39) eventually needs what this chapter teaches. It's the methodology Part I implied: how to *debug* a low-latency system when you can't attach a debugger to the hot path and the optimizer has erased your variables.

---

## 5.1 Why it matters: you can't `printf` your way out of a nanosecond bug

Part I taught you to *measure* — profile, benchmark, read the asm. This chapter is about the other half of the craft: *debugging* — finding out why the system did something wrong. And low-latency systems are uniquely hostile to every debugging technique you learned on ordinary software, because the two properties that make them fast make them nearly undebuggable by conventional means.

First, **the code is optimized past recognition.** You ship `-O2`/`-O3` with LTO and PGO (Ch. 22); the optimizer inlines functions out of existence (Ch. 14), keeps variables in registers or eliminates them entirely, reorders and merges instructions (Ch. 11), and vectorizes loops (Ch. 29). Attach `gdb` to that binary and you get `<optimized out>` where your variable should be, a call stack that doesn't match your source (inlined frames collapsed), and a program counter that jumps around because instructions were reordered. The debugger and the source have diverged — debugging optimized code is a skill of its own (Ch. 4's "read the machine" applied to a live process).

Second, **you cannot perturb the hot path to observe it.** The classic debugging moves — add a `printf`, set a breakpoint, single-step, attach a debugger — all *stop or slow the thread*, and a low-latency thread that's stopped isn't doing the thing you're trying to debug. Worse, they change the timing: the bug is often a *race* or a *timing-dependent* condition (Ch. 30, 40), and the moment you add a `printf` (which takes hundreds of ns and a syscall — Ch. 71) or a breakpoint, the timing shifts and the bug vanishes — a **heisenbug**. Attaching `gdb` to a live hot process stalls it for milliseconds (blowing every latency guarantee) and, if it holds a lock or a NIC ring, can take the whole system down. You cannot debug the hot path *while it's hot*.

The resolution is a different toolkit, built on the same architecture the book already established: **make the system deterministic and captured (Ch. 74–75), then debug the *replay*, not the live system.** Because a deterministic state machine (Ch. 74) fed a captured input stream (Ch. 75) reproduces the exact bug offline, bit-for-bit, you can attach every heavy tool — debugger, sanitizer, record/replay — to the reproduction *without* touching production. This chapter covers debugging optimized code (§5.2 the mental model), reproducing and capturing bugs (§5.3), the techniques — record/replay, core dumps, replay-debugging, production tracing (§5.4), and the pitfalls, chiefly the observer effect (§5.5). It's how you find the bug the profiler can't see and the debugger can't safely touch.

## 5.2 Mental model: optimized code, the observer effect, and reproduce-then-debug

Three ideas frame low-latency debugging:

- **Optimized code doesn't match your source, so debug at the level it runs.** With `-O2`/LTO, the mapping from source lines to instructions is many-to-many and lossy: variables live in registers (and get reused), functions are inlined (their frames vanish), dead code is deleted (including, if you had UB — Ch. 21/72 — code you *thought* would run), and instructions are reordered. To debug it you (a) keep **debug info even in optimized builds** (`-g` doesn't change codegen — §5.4), so the debugger can map what it *can*; (b) read the **asm** (Ch. 4) when the source view lies; and (c) sometimes build a **`-Og`** (optimize-for-debugging) or `-O0` reproduction where the bug still occurs. But beware: some bugs *only* appear at `-O2` (the optimizer exposed a UB — Ch. 21, or a race the reordering made visible — Ch. 30), and vanish at `-O0` — those you must debug *as optimized*, reading registers and asm.
- **The observer effect is the central constraint.** Observing the hot path changes it. A `printf`, a breakpoint, a debugger attach, even a heavyweight `perf` mode perturbs timing and CPU state enough to hide timing-dependent bugs and to violate the latency guarantees you're debugging. The corollary: **observe with the lowest-overhead tool that can see the bug** (a lock-free log record — Ch. 71, an eBPF probe — Ch. 61, a hardware counter — Ch. 2), and do heavy observation *off* the hot path (on a replay). Never debug a heisenbug by adding observation that changes the timing.
- **Reproduce-then-debug (the winning strategy).** Instead of catching the bug live (which you can't safely do), *reproduce it offline* and debug the reproduction with unlimited tooling. The two engines for reproduction: **deterministic replay** (Ch. 74–75 — feed the captured production input through the deterministic core and the bug recurs exactly), and **record/replay debuggers** (`rr` — record the program's execution once, then replay it deterministically forwards *and backwards* as many times as you like). Both turn a fleeting, timing-dependent, production-only bug into a stationary, inspectable, infinitely-repeatable one. This is why the book built determinism (Ch. 74) and capture (Ch. 75): they are, among other things, the debugging substrate.

The mental model: **you don't debug the live hot path — you capture enough to reproduce the bug, then debug the reproduction with heavy tools, reading the code at the level it actually runs (asm/registers) because the optimizer erased the source-level view.**

## 5.3 Measure it: reproducing and capturing the bug

Debugging's "measure it" is *reproduction* — turning a one-off into something you can study. The techniques for capturing enough to reproduce:

- **Capture-driven reproduction (Ch. 75).** If the system captures its input stream (Ch. 75) and the core is deterministic (Ch. 74), a production bug is reproduced by replaying the captured session through the core offline — bit-for-bit (Ch. 74.2.2). This is the highest-value reproduction: it needs no special effort at bug time (the capture already exists for compliance/replay), and it reproduces the *exact* production conditions. Measure your ability to do this by regularly replaying captures and asserting output matches (Ch. 76's regression gate is the same machinery).
- **`rr` record/replay for the reproducible-but-elusive.** For bugs you can trigger in a test or a replay harness but that are timing-dependent, record the execution once with **`rr`** (Mozilla's record-and-replay debugger); `rr` captures enough of the execution (via a low-overhead single-core recording) that it can *replay it deterministically* under `gdb`, including **reverse execution** (step *backwards* from the crash to the cause). This converts "it crashed but I don't know why" into "step back from the crash and watch it happen." `rr` records at some slowdown, so you record in a test/replay environment, not production hot path.
- **Core dumps for the crash you can't reproduce (Ch. 74).** When a production process crashes, a **core dump** captures its final memory state for post-mortem analysis. Capture cores *without stalling survivors* (Ch. 74.4.5 — async/bounded/off-path core handling), symbolize them offline (with the matching debug info — §5.4), and analyze in `gdb`. A core is a single snapshot (not a replay), but for a crash it often shows the smoking gun (a corrupted pointer, a bad index — Ch. 72). Pair with the capture (Ch. 75) to *replay up to* the crash.
- **Deterministic stress to force rare bugs.** Race and timing bugs (Ch. 40) may not appear in one replay; force them with deterministic stress — run the core against adversarial captured inputs (the microburst, the gap, the malformed packet — Ch. 53/72/54), under sanitizers (§5.4), many times. The goal is to make a rare production bug reproduce reliably offline so you can then debug it once.

The lesson: **debugging effort goes into reproduction first.** A bug you can reproduce offline (via capture-replay or `rr`) is a bug you *will* solve with the tools below; a bug you can't reproduce is a guessing game. The book's capture/replay architecture (Ch. 74–75) is what makes most production bugs reproducible — the single biggest debugging investment you can make.

## 5.4 Techniques

### 5.4.1 Debugging optimized and LTO builds

- **Keep debug info in optimized builds.** Compile the production build with `-g` (and consider `-g` + `-fno-omit-frame-pointer` — Ch. 2/22 — for usable stacks); `-g` does **not** change codegen, it only adds a `.debug_*` section the debugger/profiler reads. Ship or archive the debug info (split debug info: `-gsplit-dwarf`, or a separate `.debug` file via `objcopy --only-keep-debug`) so you can symbolize a stripped production binary's cores and profiles offline. A stripped binary with no archived debug info is a binary you can't debug (§5.5).
- **Read `<optimized out>` as "read the asm" (Ch. 4).** When a variable is optimized out, its value is in a register or was eliminated; use `info registers`, `disassemble`, and the source↔asm mapping (Ch. 4) to recover it. `gdb`'s `info scope` / `info address` and reading the DWARF location expressions tell you where a variable *is* at a given PC. This is Ch. 4's skill applied live.
- **Build a debug reproduction — carefully.** A `-Og` or `-O0` build is easier to debug *if the bug survives the optimization change*. Many logic bugs do (debug them at `-Og`); but timing/UB/race bugs often *don't* (§5.2) — they exist *because* of the optimization. Know which kind you have: if the bug vanishes at `-O0`, it's likely a UB or race the optimizer exposed (Ch. 21/30/40) — debug it *as optimized* with sanitizers (below) rather than chasing it at `-O0` where it hides.
- **Sanitizers to catch the optimizer-exposed bugs (Ch. 40, 72).** UBSan/ASan/TSan (Ch. 40) find the UB, memory errors, and races that manifest as mysterious optimized-code behavior — run the reproduction under them. A bug that only appears at `-O2` and vanishes at `-O0` is very often a sanitizer finding (Ch. 72.2.2). The sanitizers are a *debugging* tool, not just a CI gate.

### 5.4.2 Record/replay, reverse debugging, and core-dump analysis

- **`rr` for reverse debugging.** Record the reproduction with `rr record`, then `rr replay` under `gdb`: set a breakpoint at the crash/symptom and **`reverse-continue` / `reverse-step`** backwards to the cause. This is transformative for "how did this pointer become null?" — you run time backwards from the null deref to the write that nulled it. `rr`'s replay is deterministic, so watchpoints and repeated runs behave identically (no heisenbug — §5.5). The gold-standard technique for elusive logic bugs once you can reproduce them.
- **Deterministic replay through the core (Ch. 74–75) under a debugger.** Replay the captured production session (Ch. 75) through the deterministic core (Ch. 74) *under `gdb`* (or `rr`), reproducing the exact production behavior offline to inspect. Because the core is deterministic, the replay hits the same bug at the same input every time — you can breakpoint the exact message that triggered it. This is `rr`'s benefit at the *application* level, using the architecture you already built.
- **Core-dump post-mortem.** For a production crash, analyze the core (Ch. 74.4.5) in `gdb` with matching debug info: `bt` (backtrace), inspect the corrupted state, correlate the faulting address with your data structures (Ch. 25). Combine with the capture (Ch. 75): the core shows the *end state*, the replay shows *how it got there*. Minidumps / partial cores (Ch. 74) keep the capture cheap enough not to stall survivors.
- **Hardware watchpoints and conditional breakpoints.** In a replay/test (never the live hot path), hardware watchpoints (`watch`) catch *when* a memory location changes — invaluable for "who corrupted this?" (a use-after-free or buffer overflow — Ch. 72). Deterministic under `rr`; use them to pinpoint the write.

### 5.4.3 Production tracing without perturbing the hot path

Sometimes you must observe *production* (the bug won't reproduce offline yet). Do it with the lowest-overhead tools:

- **eBPF / bpftrace (Ch. 61) — the production-safe probe.** Attach a probe to a function, a tracepoint, or a USDT marker to record data (latency, arguments, counts) with minimal overhead (Ch. 61), *without* stopping the process or recompiling. This is how you observe a live low-latency system: nanosecond-scale, non-stopping, safe to run in production. Trace the suspect path, gather the data offline, and reproduce.
- **Lock-free logging as a debug channel (Ch. 71).** The zero-overhead logger (Ch. 71) *is* a debugging tool: add a binary log record (a few ns, §Ch. 71) on the suspect path to capture the state at the moment of interest, and decode it offline. Because it's off-path (formatting/IO on the consumer thread), it doesn't perturb timing the way `printf` does — the observer effect (§5.5) minimized. Debug by *recording*, not by *stopping*.
- **`perf` and hardware counters (Ch. 2) for behavior, not logic.** For "why is this slow/stalling" (as opposed to "why is this wrong"), `perf` and the PMU (Ch. 2) show the microarchitectural behavior — a cache miss, a mispredict, a front-end stall (Ch. 7/12/13) — without stopping the process. Debug performance bugs with counters (Ch. 2/76), logic bugs with replay/`rr`.
- **Always: gather in production, reproduce and fix offline.** Production tracing's job is to gather *enough to reproduce* (the input, the state, the timing signature) so you can then reproduce the bug offline (§5.3) and debug it with heavy tools. Don't try to *fix* on the live system; observe cheaply, reproduce, fix on the replay, verify with the regression gate (Ch. 76).

## 5.5 Pitfalls & anti-patterns: the observer effect and optimized-out state

- **The observer effect / heisenbug (the cardinal pitfall).** Adding a `printf`, a breakpoint, or a heavy probe changes the timing and CPU state (Ch. 41, 71), hiding timing-dependent and race bugs (Ch. 30, 40) — the bug "disappears" the moment you look. Observe with the lowest-overhead tool (lock-free log — Ch. 71, eBPF — Ch. 61), and reproduce offline where you can observe freely (§5.2, §5.4.3).
- **Attaching `gdb` to the live hot path.** Stops the thread for milliseconds (violating every latency guarantee) and can deadlock the system if the stopped thread holds a lock or a NIC ring (Ch. 34, 55). Never attach to a live hot process; debug a replay/reproduction (§5.2).
- **`printf` debugging on the hot path.** A `printf`/`std::cout` is hundreds of ns + a syscall (Ch. 71) — it perturbs timing (heisenbug) *and* violates the latency budget. Use the lock-free binary logger (Ch. 71) as the debug channel instead (§5.4.3).
- **Chasing a `-O2`-only bug at `-O0`.** A bug that vanishes at `-O0` is very likely UB or a race the optimizer exposed (Ch. 21/30/72) — debugging at `-O0` hides it. Debug it *as optimized*, under sanitizers (§5.4.1); the disappearance at `-O0` is a *clue* (it's UB/race), not a debugging strategy.
- **No debug info for production binaries.** Stripping the production binary and keeping no archived debug info → cores and profiles are unsymbolizable (`??` everywhere). Always build with `-g` (codegen-neutral) and **archive the debug info** (split-dwarf / separate `.debug`) matched to each release (§5.4.1).
- **Trusting the source view of optimized code.** Believing `gdb`'s source-line and variable display on an `-O2` binary when the optimizer reordered/eliminated things — the display can be misleading. Cross-check with the asm (Ch. 4) when the source view seems impossible (§5.4.1).
- **Core dumps stalling survivors (Ch. 74).** A synchronous, huge, on-the-hot-disk core dump that freezes the box — hurting the *surviving* processes (Ch. 74.4.5). Make core handling async/bounded/off-path (§5.4.2).
- **Debugging without reproducing.** Guessing at a bug you can't reproduce, adding speculative fixes, "fixing" it by luck (or by perturbation that hides it). Invest in reproduction first (capture-replay, `rr` — §5.3); an unreproduced bug isn't fixed, it's hidden.
- **Fixing on the live system.** Editing/patching production to chase a bug — changing the thing you're debugging under load. Observe in production, reproduce and fix offline, verify with the regression gate (Ch. 76), then deploy (§5.4.3).

## 5.6 Exercises & checklist

**Exercises:**

1. **Debug optimized code.** Take a bug in an `-O2` function where the key variable is `<optimized out>`; recover its value via `info registers` + `disassemble` (Ch. 4). Then confirm the bug is or isn't present at `-O0` — and classify it (logic vs UB/race) from whether it survives (§5.4.1).
2. **Reverse-debug with `rr`.** Record a reproduction of a null-deref / corruption bug with `rr`, then `reverse-continue` from the crash to the write that caused it (§5.4.2). Contrast with forward-only `gdb`.
3. **Capture-replay reproduction.** Reproduce a "production" bug by replaying a captured session (Ch. 75) through the deterministic core (Ch. 74) under `gdb` — breakpoint the exact triggering message (§5.4.2). Verify determinism (same bug every replay).
4. **Heisenbug.** Create a timing-dependent bug that vanishes when you add a `printf` but persists with a lock-free log record (Ch. 71) — demonstrate the observer effect and the low-overhead workaround (§5.5, §5.4.3).
5. **Production trace → offline fix.** Use bpftrace (Ch. 61) to capture the arguments/state of a suspect path in a running system without stopping it, reproduce the bug offline from the captured data, fix it, and verify with the regression gate (Ch. 76) (§5.4.3).

**Checklist:**

- [ ] Production builds carry **`-g` debug info** (codegen-neutral), and the debug info is **archived per release** (split-dwarf / separate `.debug`) for offline symbolization (§5.4.1, §5.5).
- [ ] Debugging **reproduces offline first** — via **capture-replay** (Ch. 74–75) or **`rr`** — rather than chasing the bug live (§5.3, §5.5).
- [ ] The **live hot path is never** attached-to, `printf`'d, or breakpointed; observation uses **lock-free logging** (Ch. 71), **eBPF/bpftrace** (Ch. 61), or **hardware counters** (Ch. 2) (§5.4.3, §5.5).
- [ ] **Optimized-code bugs** are debugged **as optimized** (asm/registers — Ch. 4, sanitizers — Ch. 40) when they don't survive `-O0`; the `-O0` disappearance is read as a **UB/race clue** (§5.4.1).
- [ ] **`rr` reverse debugging** is used for elusive logic bugs once reproducible; **core dumps** (async/off-path — Ch. 74) + capture-replay for crashes (§5.4.2).
- [ ] The **observer effect** is respected — the lowest-overhead tool that can see the bug, heavy tools only on the replay (§5.2, §5.5).
- [ ] Fixes are made and verified **offline** (regression gate — Ch. 76), never on the live system (§5.5).

## 5.7 References

- The **`rr`** (rr-project.org) documentation and papers — record/replay and reverse execution, the core technique of §5.4.2.
- The `gdb` documentation on **debugging optimized code**, DWARF location expressions, split debug info, and reverse debugging (§5.4.1–2).
- Ch. 4 (reading asm — debugging at the machine level), Ch. 22 (build/debug-info flags — Appendix D), Ch. 40 (sanitizers — the optimizer-exposed-bug finders), Ch. 61 (eBPF/bpftrace — production tracing), Ch. 71 (lock-free logging — the debug channel), Ch. 74–75 (deterministic replay — the reproduction engine).
- The Linux `core(5)` / `coredump` documentation and minidump tooling — production crash capture (§5.4.2; Ch. 74).

## 5.8 Additional Reading

- Talks on `rr`, time-travel debugging, and debugging optimized C++ (CppCon, the `rr` authors) — the reproduce-then-reverse-debug workflow (§5.4.2).
- Brendan Gregg's material on production debugging with eBPF (Ch. 61 references) — non-perturbing observation of live systems (§5.4.3).
- Writing on heisenbugs, the observer effect, and debugging concurrency (Ch. 40) — why perturbation hides timing bugs (§5.5).
- Ch. 74–75 (*State Machine / Capture-Replay*) — the reproduction substrate; Ch. 39 (*std::execution*) — async control flow whose bugs need these techniques; **Appendix D** — the debug-info and sanitizer flags; **Appendix F** — heisenbug / record-replay / debug-info glossary.

---

*Next: Ch. 6 — System Setup for Low Latency, where we leave the code and tune the machine underneath it: C-states, frequency scaling, hyperthreading, and the speculative-execution mitigations whose cost you can now reason about — building the quiet, deterministic box the rest of the book assumes.*
