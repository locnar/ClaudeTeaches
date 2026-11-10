# Part XII — Beyond C++: Languages at the Latency Floor

# Chapter 96 — Choosing a Language per Latency Tier

> **Prerequisites:** Ch. 1 §1.4.2 (the hot/warm/cold latency tiers, qualitative until now), Ch. 3 and Appendix L (the benchmarking discipline this chapter extends across languages), Ch. 21 §21.4.2 (the `constexpr` tick-size table rebuilt here in Rust and Zig), Ch. 92–95 (asm, Rust, the boundary and the managed runtimes — the evidence this chapter decides with), Ch. 107 (the capture every kernel here replays).
>
> **Leads into:** Part XIII (the operations Part, where the control-plane, telemetry and research components live in whatever language this chapter's table puts them in), Ch. 117 (the capstone's tick-to-trade budget, now with a language axis), Appendix B (the summary and tier table that point back here), Appendix L.5 (the cross-language protocol in checklist form).

---

## 96.1 Why it matters: a one-time decision with a per-tick consequence

Language choice is the only decision in this book that is made once, before any measurement exists, and then paid for on every message for the life of the system. It is also the one most often made on folklore: "C++ is fast", "Java is slow", "Rust is as fast as C++", "Python is for research". Each of those is sometimes true and never a number, and Ch. 93–95 have now produced the numbers — a decode-and-book-update kernel, identical in semantics across nine toolchains, replayed from the same capture on the same core. The capstone of Part XII does three things with them. It states the **latency tiers of Ch. 1 §1.4.2 with measured floors on each rung** — what the fastest steady-state and the worst cold-start of each language look like on this kernel, so a component can be placed by its budget rather than its vendor's claim. It writes down the **equal-rigour protocol** without which no cross-language number means anything (same box, same core, same pinned frequency, same replayed capture, warm and cold windows separated, each toolchain's `DoNotOptimize` and asm step), because the single most common cross-language benchmark error is comparing one language's steady state with another's warm-up. And it closes the oldest dangling promise in the book — Ch. 21 §21.4.2's "see Appendix B for `const fn` and `comptime`" — by building the same tick-size table three ways and showing the bytes are identical.

## 96.2 Mental model

### 96.2.1 The tiers, restated with what the language controls

Ch. 1 §1.4.2 divided a trading system into a **hot** tier (the tick-to-trade path: decode, book, strategy, risk, encode — a budget of hundreds of nanoseconds per message, p99.9 measured in microseconds), a **warm** tier (order management, position keeping, market-data fan-out to many consumers — tens of microseconds, occasional milliseconds tolerated), and a **cold** tier (research, backtesting, reporting, UIs, deployment — seconds are fine). The language decides, for each tier, three things: **the steady-state floor** (what the kernel costs once everything is warm — §96.3's `pass 3` column), **the cold-start and mid-session disturbances** (page faults and cold caches for everyone — Ch. 62; plus JIT compilation, deoptimisation and GC for managed runtimes — Ch. 95), and **the controllability of layout, allocation and ordering** (whether you *can* write the Ch. 10/30/41 code the hot tier needs). A language belongs on a tier when its *worst* disturbance fits the tier's tail budget, not when its best steady state does.

### 96.2.2 What the language does not control

Most of this book is language-independent, and the table in §96.3 confirms it: the hardware chapters (Part II), the OS and isolation chapters (Part VII), the network stack and NIC chapters (Parts VIII–X) and the measurement discipline (Part I, Appendix L) are the same job in any language — a cache miss costs the same in Java (Appendix E), `isolcpus` isolates a Go thread as well as a C++ one (if it is `LockOSThread`ed — Ch. 95 §95.4.5), and a kernel-bypass NIC's C SDK (Ch. 82) is called through the same waist from all of them (Ch. 94). What differs is how much of Part II–IV the language *lets you express*: `#[repr(C)]` and `alignas` versus a JVM object header you cannot remove; a `std::array<Level, 65536>` versus a `Level[]` of boxed objects versus a `long[]` SoA (Ch. 95 §95.4.1's answer); `memory_order_release` versus a sequentially-consistent `volatile` versus a `VarHandle` access mode. The floors in §96.3 are what each language reaches *when written in its own hot-path idiom*; written naively, every one of them is slower by an order of magnitude or more.

### 96.2.3 Equal rigour: the protocol

A cross-language number is admissible only if it was produced under one protocol, and the protocol is Appendix L's with four additions:

1. **Same box, same core, same frequency state.** Every toolchain's binary runs under `taskset -c 2` on the same host, turbo and governor fixed (Appendix C), nothing else running — including the previous language's build server (`.NET` leaves MSBuild nodes alive; `dotnet build-server shutdown`).
2. **Same replayed capture, same parity check.** Every implementation reads the same capture file (Ch. 107), performs the same decode and book update, and prints the same **digest** — a fold of every message's result; a mismatch means one implementation did different work and its number is void. §96.3's nine digests are equal.
3. **Warm and cold measured separately, in windows.** Pass 1 is split at 1 k, 10 k, 100 k and 1 M messages, passes 2 and 3 are whole: the first window is the cold start (page faults, cold caches — and for a JIT, the interpreter); the pass-3 column is steady state. Never compare one language's pass 3 to another's pass 1.
4. **Each toolchain's `DoNotOptimize`, and its asm.** The fold into the digest is the sink that keeps every result live; where a loop has no natural sink, the language's black box is: C++ `benchmark::DoNotOptimize` / `asm volatile("" : "+r"(x))` (Ch. 92 §92.2.3); Rust `std::hint::black_box`; Zig `std.mem.doNotOptimizeAway`; Java JMH `Blackhole.consume` (or a `volatile` sink field); .NET BenchmarkDotNet `Consumer` / `GC.KeepAlive`; Go a package-level `sink` variable assigned from the loop; OCaml `Sys.opaque_identity`; Python needs none. And the *verify the codegen* step runs per toolchain: `-S`, `--emit asm`, `zig build-obj -femit-asm`, `-XX:+PrintAssembly`/JITWatch, `DOTNET_JitDisasm`, `go tool objdump`, `ocamlopt -S` (Ch. 95 §95.5) — to confirm the loop was not deleted, hoisted or vectorised into something the others did not get.

### 96.2.4 What this Part measured, and what it did not

The table of §96.3 covers the toolchains installed on the measurement host: GCC, Clang, rustc, Zig, HotSpot, .NET, Go, OCaml 4.14 and CPython. It does **not** measure GraalVM native-image, Azul Prime/C4, Shenandoah, OCaml 5's multicore runtime or a flambda build of OCaml — Ch. 95 describes each from its documentation and says so; their rows in §96.4.2 are placed by the mechanism (AOT or concurrent collector) and are to be filled in on the reference machine before a decision rests on them. Julia sits with Python in the cold tier for the same reason Python does — a JIT with a first-call compilation cost of milliseconds per method and a GC — and its common "two-language problem" answer (write the hot loop in Julia too) is the reimplementation drift Ch. 107 §107.4.6 is designed to avoid; the production decoder bound through Ch. 94's waist serves a Julia notebook as well as a Python one (`ccall`, the same C ABI).

## 96.3 Measure it: one kernel, nine toolchains

*Toolchain and machine.* GCC 13.3 and Clang 18.1 (`-O2 -std=c++23 -march=native`; `gcc -O2 -std=gnu11` for C), rustc 1.97 (`-C opt-level=3 -C target-cpu=native -C panic=abort`), Zig 0.17.1 (`-O ReleaseFast -mcpu=native`, built as a C-ABI static library and timed by the C harness — Ch. 94 §94.4.5), OpenJDK 21.0.12 (`-XX:+UseSerialGC -Xms256m -Xmx256m`), .NET 9.0.3 (Release, TieredPGO), Go 1.24.7, OCaml 4.14.1 (`ocamlopt -unsafe -inline 200`, no flambda), CPython 3.13.17; the 4-vCPU Intel Xeon VM of Appendix L.2 (Linux 6.18, `tsc` clocksource, 2.7 GHz), `taskset -c 2`, median of 5 runs (2 for Python). **Not the reference machine**: absolute numbers will shift on the Xeon Gold 6326; the ratios and the shape of each row are the result.

**The kernel.** Ch. 70's AddOrder decode (32-byte big-endian record: type, side, qty, price in ticks, sequence, order id) fused with Ch. 32 §32.4.5's flat-array book update (two 65,536-level sides indexed by tick, best-bid/ask tracking with re-seek on an emptied touch level), over a 1 M-message synthetic capture whose activity is concentrated within 32 ticks of the touch (Ch. 32's observation), each message's result folded into a 64-bit digest. The C++ source is Ch. 93 §93.3's `kernel.hpp`; the Rust source is in Ch. 93; the Java flyweight is in Ch. 95 §95.4.1; the Zig, C, C#, Go, OCaml and Python versions follow the same structure in each language's hot-path idiom (SoA primitive arrays in Java, `Span<byte>` + `ref` locals in C#, `encoding/binary` + `LockOSThread` in Go, unboxed `int`s and `Bytes.get_int32_be` in OCaml, `struct.unpack_from` and lists in Python).

```
   decode + book update, ns per message (median of 5 runs; every row prints digest 75a4021c2af2c93c)
   language / toolchain            cold [0,1k)   [1k,10k)  [10k,100k)  [100k,1M)   pass 2   pass 3    steady vs C++   cold vs own steady
   C++  GCC 13.3  -O2                     8.9        7.4         7.6        7.3      7.4      7.4      1.0x            1.2x
   C++  Clang 18.1 -O2                   10.1        8.2         8.2        8.3      8.2      8.2      1.1x            1.2x
   C    GCC 13.3  -O2                    11.7        7.2         7.2        7.3      7.2      7.3      1.0x            1.6x
   Rust 1.97  opt-level=3                17.1        7.0         6.7        6.8      6.8      7.3      0.9–1.0x        2.3x   (two 1 MiB Box first-touch)
   Zig  0.17  ReleaseFast                13.5        7.2         7.3        7.3      7.3      7.2      1.0x            1.9x
   Java 21  HotSpot, SerialGC          4471.5      475.4       112.8       21.8     12.1     12.7      1.7x            350x   (interpreter → C1 → C2)
   C# / .NET 9  TieredPGO               489.5      446.9        10.3       10.3      9.6      9.3      1.3x            53x    (tier-0 → tier-1 at ~10k calls)
   Go 1.24                               24.0       18.6        18.7       19.8     18.7     18.7      2.5x            1.3x   (7 bounds checks kept)
   OCaml 4.14  ocamlopt                  15.1       12.4        12.4       12.6     12.5     12.4      1.7x            1.2x   (boxed Int64 fields)
   Python 3.13  CPython                 581.2      500.7       508.1      513.4    532.7    528.0      71x             1.1x
```

Four things the table says that folklore does not. **The AOT languages are one language at steady state**: C, C++ (both compilers), Rust and Zig span 6.8–8.2 ns, and the GCC-vs-Clang gap (11 %) is as large as any gap between them — Ch. 93 §93.5 showed it is an inlining decision, not a language property. **The JITs reach within 1.3–1.7× of C++ — eventually**: .NET at 9.3 ns after ~10,000 messages, Java at 12.7 ns after ~200,000, and both pay the first thousand messages at 50–350× their own steady state; a market-open burst of 10,000 messages lands entirely inside that window unless Ch. 95 §95.4.3's warming has already run. **Go and OCaml are AOT and warm fast but sit at 1.7–2.5×** for reasons visible in their asm (bounds checks and boxing — Ch. 93 §93.5, Ch. 95 §95.4.6), i.e. for costs that can be engineered down, not runtime costs. **Python is 70× slower and has no warm-up** — the right shape for the cold tier, and the wrong one for anything per-message, which is why Ch. 107 §107.4.6 binds the C++ decoder into it rather than reimplementing it (1.9 ns per message through the batched waist, Ch. 94 §94.3). And the cold column is everyone's problem: Rust's 2.3× cold ratio is page faults on its boxed book, not a Rust cost — Ch. 62 applies to every row.

## 96.4 Techniques

### 96.4.1 Running the protocol

The harness is one script per toolchain that: pins (`taskset -c N`), builds with the flags that will ship (never a debug build, never `-O3` for one language and `-O2` for another without saying so — use each language's *release* intent and record it), reads the capture, runs the windowed passes, prints per-window ns/message and the digest, and is invoked by a driver that runs each row *R* times, takes medians, and refuses a row whose digest differs. The §96.3 table is the output of exactly such a driver (`table.py` in the book's scratch tree). Add to it, per Appendix L.2, a tail-latency variant for the rows that matter — per-message `rdtscp`/`nanoTime` stamps into a histogram — because a JIT's deoptimisation (Ch. 95 §95.3) and a GC's safepoint are *tail* events a mean-per-window hides; and run every managed row twice, once from cold and once after the warm-up its production deployment will actually have.

### 96.4.2 The tier table with measured floors

| Tier (Ch. 1 §1.4.2) | Budget per message / tail | Languages whose measured floor fits | Why, and what it costs to stay there | Example components |
|---|---|---|---|---|
| **Hot — wire to wire** | 100s of ns; p99.9 in low µs | C++, C, Rust, Zig (6.8–8.2 ns; cold 1.2–2.3× — Ch. 62 warming) | AOT, layout and allocation under full control, no runtime disturbance; the Part II–IV code is expressible | feed handler, book, strategy, risk gate, order encoder, NIC/bypass driver glue (Ch. 70, 32, 82) |
| **Hot, inherited** | same | Java/.NET **only** garbage-free + warmed + collector proven idle (Ch. 95 §95.3's GCFREE line: 2 ns decode, p99.9 8–15 µs, zero collections) | every allocation is a tail; warm-up must cover every live branch; the JIT may still deoptimise | LMAX/Chronicle/Aeron-style engines; the cost is the discipline, measured continuously |
| **Warm — 10s of µs, ms tolerated** | 10–100 µs; occasional ms | Go, OCaml (12–19 ns kernels; GC STW ≤ 1 ms if allocation is controlled), Java/.NET with concurrent collectors (ZGC/C4/server GC: sub-ms stops, ms-scale allocation-stall tails) | the runtime's worst disturbance fits the tail budget | OMS, position/risk aggregation, market-data fan-out to internal consumers, gateways to slow venues, hot-reload control (Ch. 101) |
| **Cold — seconds** | anything | Python, Julia, any JVM/.NET service, shells | productivity wins; the hot code it needs is *bound in*, not rewritten (Ch. 107 §107.4.6) | research, backtesting, reporting, monitoring dashboards (Ch. 112–113), deployment, UIs |
| **Below software** | 10s–100s of ns, deterministic | HDL / HLS (Ch. 90), P4/DOCA (Ch. 91) | no instruction stream at all | inline feed handling, pre-trade risk in fabric, timestamping |

Two rules apply across rows. **A component is placed by its measured worst case on this protocol, not its best** — Java's 12.7 ns steady state qualifies it for nothing; its 4.5 ms p99.99 under allocation disqualifies it from the hot tier and its 8–15 µs garbage-free p99.9 re-qualifies it, conditionally. **The boundary between tiers is Ch. 94's waist or Ch. 67's shared memory, never a per-message call into a runtime**; the research tier runs the production decoder through the waist (Ch. 107 §107.4.6), and the hot tier publishes to the warm tier through a shared ring (Ch. 67), so that no tier's disturbance crosses into the one below it.

### 96.4.3 `constexpr` vs `const fn` vs `comptime`: one table, three compilers, one `.rodata`

Ch. 21 §21.4.2 built a tick-size-by-band table at compile time so the runtime is one load. The same table in the three AOT languages that have compile-time evaluation:

```cpp
// ticks.cpp — C++20 constexpr (Ch. 21 §21.4.2). Build: g++ -O2 -std=c++20 -c ticks.cpp
constexpr std::int64_t tick_for_band(std::size_t b) { std::int64_t t = 1; for (std::size_t i = 0; i < b / 2; ++i) t *= 10; return (b % 2) ? t * 5 : t; }
static constexpr auto kTickSize = [] { std::array<std::int64_t, 16> t{}; for (std::size_t b = 0; b < 16; ++b) t[b] = tick_for_band(b); return t; }();
extern "C" std::int64_t tick_size(unsigned band) { return kTickSize[band & 15]; }
```
```rust
// ticks.rs — Rust const fn + const item. Build: rustc --edition 2021 -C opt-level=2 --crate-type staticlib --emit obj ticks.rs
const fn tick_for_band(b: usize) -> i64 { let mut t = 1i64; let mut i = 0; while i < b / 2 { t *= 10; i += 1; } if b % 2 == 1 { t * 5 } else { t } }   // const fn: no for-loops/iterators in const context (1.97) — while is fine
const TICK_SIZE: [i64; 16] = { let mut t = [0i64; 16]; let mut b = 0; while b < 16 { t[b] = tick_for_band(b); b += 1; } t };                        // a const block: evaluated by the compiler, never at runtime
#[no_mangle] pub extern "C" fn tick_size_rs(band: u32) -> i64 { TICK_SIZE[(band as usize) & 15] }
```
```zig
// ticks.zig — Zig comptime block. Build: zig build-obj ticks.zig -O ReleaseFast -fomit-frame-pointer
fn tickForBand(b: usize) i64 { var t: i64 = 1; var i: usize = 0; while (i < b / 2) : (i += 1) t *= 10; return if (b % 2 == 1) t * 5 else t; }   // an ORDINARY function: comptime-callable because its inputs are
const tick_size_table: [16]i64 = blk: { var t: [16]i64 = undefined; for (0..16) |b| t[b] = tickForBand(b); break :blk t; };                   // the block runs in the compiler
export fn tick_size_zig(band: u32) i64 { return tick_size_table[band & 15]; }
```

`objdump -s -j .rodata` on the three object files:

```
   ticks_cpp.o (.rodata)            ticks_rs.o (.rodata..Lanon…)      ticks_zig.o (.rodata)
   0000 01000000 00000000 05000000  identical                         identical        <- 1, 5, 10, 50, 100, 500, …, 50 000 000 as 16 little-endian int64s: the same 128 bytes
   …
   tick_size:    and edi,0xf ; lea rax,[rip+table] ; mov rax,[rax+rdi*8] ; ret        (GCC; endbr64 first)
   tick_size_rs: and edi,0xf ; lea rax,[rip+table] ; mov rax,[rax+rdi*8] ; ret        (rustc — byte-identical body)
   tick_size_zig: and edi,0xf ; mov rax,[rdi*8+table] ; ret                           (Zig — absolute addressing in a non-PIC object; 3 instructions)
```

The three mechanisms produce **the same 128 bytes and the same one-load runtime**; the differences are in what the compiler *lets you write*. C++ `constexpr` is a *permission*: a `constexpr` function may also run at runtime, and only `consteval` or a `static constexpr` initializer *forces* compile-time evaluation (Ch. 21 §21.2 — and the check that it happened is Ch. 4's: a fixed-address load, no loop). Rust's `const fn` is a *restriction*: it runs at compile time in a `const` context and at runtime otherwise, but the const subset is smaller (no heap, no trait objects, no `for` over iterators in 1.97 — hence the `while`), which is why const evaluation never silently falls back to runtime: if it cannot be const, it does not compile. Zig's `comptime` is a *mode*: any function can run in the compiler if its arguments are known, types are first-class values, and one mechanism covers `constexpr`, `consteval`, `if constexpr` and templates (`fn Book(comptime levels: usize) type` is the class template). All three obey Ch. 21 §21.5: the table is `.rodata` the data cache must hold, so it is only a win while it stays hot.

### 96.4.4 The scorecard

When the tier is known, the choice within it is a scorecard with measured rows and judged rows, and the measured ones go first:

| Criterion | Measure it with | Weight on the hot tier |
|---|---|---|
| Steady-state floor on *your* kernels | §96.3's protocol | high — but AOT languages tie (§96.3) |
| Worst disturbance (cold, JIT, GC, safepoint) | windows + per-message histogram (Ch. 95 §95.3) | decisive |
| Layout/allocation/ordering control | can the Ch. 10/30/41 code be written? (§96.2.2) | decisive |
| Boundary cost to the rest of the system | Ch. 94 §94.3's ladder | high |
| Correctness tooling for lock-free code | TSan/Miri/Loom, Appendix A ARM run (Ch. 53, 93 §93.6) | high |
| Ecosystem at the floor (NIC SDKs, protocol decoders, allocators) | inventory of C headers to bind (Ch. 94 §94.4.6) | medium |
| Team fluency and hiring | — (judged) | real, and the usual tie-breaker among AOT languages |
| Build/deploy: reproducibility, LTO/PGO, binary size | Ch. 27, Ch. 93 §93.4.4 | medium |

The hot tier's honest conclusion from Part XII: **C++ and Rust are interchangeable on performance**; the choice is the borrow checker's compile-time elimination of Ch. 100's bug classes against the ecosystem's C++-first SDKs and a team's existing fluency; C and Zig are the right waist and the right C compiler; everything else needs the tier above.

### 96.4.5 The polyglot architecture

The system that results has a C++ (or Rust) hot tier — one process per role (Ch. 102) — publishing to shared-memory rings and seqlocks (Ch. 67, 46) that warm-tier processes in Go/Java/.NET/OCaml read in their own loops, with a C waist (Ch. 94) for the rare control call; a capture journal (Ch. 107) that the cold tier reads as columnar data (Ch. 107 §107.4.7) through the production decoder bound into Python (Ch. 107 §107.4.6); and Ch. 98's trace records carried through all of it so the per-tier budgets are attributed, not assumed. The architecture's invariant is the one §96.4.2 stated: no tier's disturbance crosses into the tier below.

### 96.4.6 Parity testing across languages

The digest in §96.3 is the cross-language regression test: every implementation of the kernel, in every language, must fold every message's result to the same 64 bits over the same capture, and a CI job runs them all (the research tier's Python included — through the waist and, for the pure-Python reference, in a nightly job). Ch. 117 §117.4.1's golden-capture gate extends naturally: the C++ core's golden output is also the golden output for the Java risk service's view of the book, and a divergence is a bug in one of them *or* a layout drift at the waist (Ch. 94 §94.4.1), found before the trade.

### 96.4.7 Three worked placements

Read with §96.3 and §96.4.2 together, three components a desk typically argues about settle quickly. **An options-pricing service that re-prices a book of 10,000 instruments on every underlying tick** wants the warm tier: its budget is tens of microseconds per update and milliseconds are survivable, its code is numeric and allocation-heavy by nature, and Java (ZGC, garbage-free inner loop — Ch. 95 §95.4.1), .NET (server GC) or Go all fit — the hot-tier engine publishes the underlying's BBO into a seqlock (Ch. 46) the pricer reads at its own pace (§95.4.7), and the pricer's results go back the same way; no call crosses. **A new venue's binary feed handler** is the hot tier by definition, and the choice is §96.4.4's: C++ if the venue's SDK and the team's existing decoders are C++, Rust if the component is greenfield and the parser is where Ch. 100's bug classes are feared — the floors tie, so the decision is made on the judged rows, and recorded as such. **A backtester that must reproduce the production book exactly** is the cold tier's canonical case, and the wrong answer is a Python reimplementation of the decoder (70× slower — and worse, *different*, Ch. 107 §107.4.6's drift): bind the production `kernel.hpp` through Ch. 94's waist, run it over the capture at 1.9 ns per message, and spend the Python on the research, not the decode.

## 96.5 Verify the codegen: the same bytes

§96.4.3's `objdump -s -j .rodata` is the Ch. 4 step for compile-time evaluation across languages: **the table is 128 identical bytes in `.rodata` in all three object files, and the lookup is a mask, an address and one load**. The things to look for when a compile-time construct *failed* to be compile-time: a loop in the function body (the table being rebuilt at runtime — C++ `constexpr` without a `constexpr` context will do this silently), a call (`tick_for_band` not folded), a `.data` rather than `.rodata` section (a mutable copy), or a table larger than the lookup it replaced. For the §96.3 table itself the per-toolchain asm steps of §96.2.3 confirm what the numbers imply: the AOT kernels are one basic block of `movbe` loads, compares and stores (Ch. 93 §93.5: 84–97 instructions, no calls, no `panic`), the Java C2 output is the same shape after warm-up (JITWatch shows `apply` inlined into the loop with the `VarHandle` accesses reduced to `movbe`s — the flyweight did not allocate), the .NET tier-1 output has its bounds checks hoisted, the Go output keeps 7 checks and 4 `runtime.panic` sites, and the OCaml output has `caml_alloc` sites at the `Int64` reads. The number and the asm agree; when they do not, the asm is right and the harness is wrong.

## 96.6 Pitfalls & anti-patterns: the benchmark that compares warm-up to steady state

- **Comparing windows that are not the same window.** A Java "benchmark" that runs 10 iterations is measuring the interpreter against C++'s steady state (§96.3: 350×); a C++ benchmark that measures the first thousand messages is measuring page faults (1.2–2.3×). The protocol's windows exist to make this impossible.
- **Different work with the same name.** A kernel that skips the BBO re-seek, decodes two fields instead of six, or uses a `HashMap` book in one language and a flat array in another is not the same kernel. The digest catches the first two; the third is the author's honesty (and the asm step's).
- **Unequal optimisation intent.** `-O3 -march=native -flto` against another language's default debug profile; `opt-level=3` against `-O2` without saying so. Record the flags in the table's header (Appendix L.3) and use each language's release intent.
- **No sink.** A loop whose result is not consumed is deleted in C++ and Rust, hoisted by Clang (Ch. 92 §92.5), and *not* deleted in Python — so Python "wins". §96.2.3's `DoNotOptimize` family, and the asm step.
- **One run.** The SPSC ring of Ch. 93 §93.3 gave 23 and 107 Mmsg/s for the same binary. Medians of *R* runs, and a histogram where the tail matters.
- **The box.** Numbers from a 4-vCPU VM (this chapter's) are not the reference machine's; a laptop's are not a server's; a host with another language's build daemon still running is not quiet. State the box; re-run on the target.
- **Extrapolating from a kernel to a system.** §96.3's 7 ns is a decode and a book update; a strategy with a `std::map`, a logger with `std::string`, or an unpinned thread swamps the language difference by an order of magnitude. The language sets the floor; the system decides whether you stand on it.
- **Choosing the tier by the steady state.** §96.4.2's rule: the worst disturbance decides.
- **Rewriting a tier for a language.** Appendix B.5's rule survives the whole Part: a measured, tuned hot path is not improved by a new language; new components, parsers and the research seam are where the choice is open.

## 96.7 Exercises & checklist

1. **Run the table on your box.** Build the nine kernels (the sources are in Ch. 93, 95 and the scratch tree), run the driver pinned, and reproduce §96.3 on your hardware; then on the reference machine. Compare the *ratios*, not the numbers.
2. **Break parity.** Change the Go kernel's cancel to subtract `qty` without the `min` against the level; watch the digest diverge and the driver refuse the row.
3. **Tail per language.** Add a per-message `rdtscp`/`nanoTime` histogram (Appendix L.2) to the C++, Rust, Java and Go kernels; run each from cold with no warm-up and report p50/p99/p99.9/p99.99 over the first 100 k messages — the number §96.4.2's tier placement actually uses.
4. **A fourth compile-time table.** Build the tick table in Java (`static final long[]` initialised in a static block) and look at the C2 output of `tickSize(band)`: is the array load a load, or a load plus a null check plus a bounds check? Compare with §96.4.3's three.
5. **Place one real component.** Take a warm-tier component you own; measure its worst disturbance under its production language with Ch. 95's batch clock; decide, with §96.4.2, whether it is on the right tier — and write down the number that decided it.

Checklist for a cross-language decision:

- [ ] The tier's budget (per-message and tail) is written down before any language is named.
- [ ] Candidates were measured on *the same kernel, box, core, capture and protocol*, warm and cold separately, medians of *R* runs, digests equal.
- [ ] The worst disturbance (cold, JIT, GC, safepoint) of each candidate was measured from the hot thread and compared to the *tail* budget.
- [ ] Each candidate's asm was read: no deleted work, the same inlining intent, bounds checks and allocations counted.
- [ ] The boundary to the rest of the system is a waist or shared memory, costed with Ch. 94's ladder; no per-message runtime crossing.
- [ ] The scorecard's judged rows (team, ecosystem, hiring) were weighed *after* the measured rows, not instead of them.
- [ ] The decision names the chapter, the table and the box it was made on — so it can be re-made when the hardware or the toolchain changes.

## 96.8 References

- Appendix L (the harness this protocol extends) and Ch. 3 — Google Benchmark, HdrHistogram, coordinated omission, warm-up and pinning (§96.2.3, §96.4.1).
- JMH (Java Microbenchmark Harness) documentation and samples — `Blackhole`, warm-up/measurement iterations, forks; BenchmarkDotNet documentation — `Consumer`, `[DisassemblyDiagnoser]`, tiered-JIT handling; Criterion.rs and `std::hint::black_box`; Go `testing.B` and the `-gcflags=-m`/`-S` flags; Zig `std.mem.doNotOptimizeAway`; OCaml `Sys.opaque_identity` and `core_bench` (§96.2.3).
- Ch. 21 §21.4.2 and the C++20 standard `[dcl.constexpr]`/`[expr.const]`; the Rust Reference, "Constant evaluation" and "Const functions"; the Zig language reference, "comptime" (§96.4.3).
- Ch. 92–95 — the measurements this chapter's table and tiers rest on.
- Ch. 1 §1.4.2 — the tiers as first stated; Appendix E — the hardware costs that are the same in every row; Appendix C — the quiet box.

## 96.9 Additional Reading

- Appendix B — the two-page summary of Part XII and the tier table in brief.
- Appendix L.5 — the cross-language protocol as a checklist beside the C++ harness.
- Appendix H — the C++23/26 features whose absence or presence appears in the scorecard's language-mechanics rows.
- Carl Cook, *When a Microsecond Is an Eternity* (CppCon 2017) — a trading firm's hot-path C++ practice, the implicit standard §96.4.2's hot tier is measured against.
- Jane Street, *Why OCaml?* and the OCaml performance posts on the Jane Street tech blog; LMAX, *The LMAX Architecture* (Fowler) — two firms' first-hand accounts of a non-C++ language at or near the hot tier.
- Ch. 107 §107.4.6–§107.4.7 (the research seam and columnar storage: where the cold tier meets the hot one), Ch. 117 (the capstone budget).

---

*Next: Ch. 97 — Zero-Overhead Logging, opening Part XIII (Observability & Operations in Production): async/lock-free loggers, deferred formatting, binary logging, and `std::print`/`std::println` for off-hot-path output — how to get diagnostics out of a zero-allocation hot path without ever touching it.*
