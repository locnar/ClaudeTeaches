# Appendix B — Beyond C++: Alternative Languages for Low-Latency

> **Relates to:** Part XII (Ch. 74–78 — the measured treatment this appendix summarises), Ch. 1 (tail latency — a GC pause *is* a tail event), Ch. 20 (the cost of abstractions), Ch. 23–24 (allocation / GC-free memory management), Ch. 30–36 (concurrency / memory model), Ch. 48 (warm-up — JIT warm-up is its runtime form), Ch. 76 (the C ABI boundary), Ch. 86 §86.4.6–§86.4.7 (the research seam and columnar storage). This appendix is the two-page map of the book's language findings; every claim on it is measured in the chapter it points to.

This appendix used to be the book's whole treatment of other languages. It is now the summary: Part XII — **Ch. 74** (inline and hand-written assembly), **Ch. 75** (Rust), **Ch. 76** (interop and the ABI boundary), **Ch. 77** (the managed runtimes, measured) and **Ch. 78** (choosing per tier, and benchmarking across languages with equal rigour) — holds the code, the asm and the numbers. What survives here is the framing, the tier table in brief, and the "what is it called elsewhere" table, each row pointing at its measurement. The framing is unchanged: the one question that matters for the hot path is *what stands between your code and deterministic nanosecond behaviour*, the decisive dividing line is the **garbage collector**, and the discipline is to **match the language to the latency tier** (Ch. 1 §1.4.2) and put the boundary between tiers in the right place (Ch. 76).

## B.1 The findings in one table

| Question | Finding (measured on one box, one kernel, one protocol — Ch. 78 §78.3) | Where |
|---|---|---|
| Is Rust as fast as C++ at the floor? | Yes, to within the spread of two C++ compilers: 6.8–7.3 ns per decode-and-book-update message vs GCC 7.4 and Clang 8.2; SPSC handoff latency indistinguishable; the same instructions for atomics, the same `noalias` the C++ `__restrict__` promises | Ch. 75 §75.3, §75.5 |
| Where does Rust/C++ parity break? | Cold start (boxed allocations first-touched: 17 vs 9 ns for the first 1 k messages), `Vec` growth, panic paths and unwind tables (`panic=abort` does not strip std's), overflow checks as a *build* flag, no `restrict` on raw pointers — and a throughput harness that is bistable in both languages | Ch. 75 §75.3, §75.6 |
| Can inline asm beat the compiler? | An asm statement costs exactly what the intrinsic costs; a hand-written function wins against another out-of-line call (1.69 vs 2.12 ns) and loses to the inlined code (1.25 ns) — intrinsics first, asm for the instructions with no name | Ch. 74 §74.3 |
| What does a cross-language call cost? | A C ABI call from C++/Rust/Zig/OCaml: 2.5–3.0 ns (the inlined floor is 2.48); Java FFM 10.5 ns, JNI 29 ns, Go cgo 50 ns, Python pybind11/nanobind/ctypes 340–474 ns; batched through the waist every caller pays ~1.5–2.6 ns per message | Ch. 76 §76.3 |
| What does a GC do to the hot thread? | One 40-byte object per message: HotSpot stop-the-world collectors pause 8–10 ms (134 ms worst), ZGC ~100 µs stops but a 4.5 ms p99.99 in the thread, .NET workstation GC paused half the run, Go's concurrent collector 5× slower batches; **garbage-free, every runtime runs the same decode at 2 ns per message with no collection** | Ch. 77 §77.3 |
| How long is a JIT slow? | 12.6 µs per message for the first 100 calls, 13 ns at steady state after ~200 k; a 860 µs-per-message deoptimisation window at 10 k; the interpreter alone is 75× the steady state | Ch. 77 §77.3 |
| Are `constexpr`, `const fn` and `comptime` the same thing? | The same 128 bytes of `.rodata` and the same one-load lookup from all three; they differ in what they let you write (permission vs restriction vs mode) | Ch. 78 §78.4.3 |
| Where does Python belong? | 70× the C++ kernel with no warm-up: the cold tier — with the *production* decoder bound in through the waist (1.9 ns per message) rather than reimplemented | Ch. 78 §78.3, Ch. 86 §86.4.6 |

## B.2 The tier table in brief

| Tier (Ch. 1 §1.4.2) | Budget | Languages whose measured floor fits | Condition | Detail |
|---|---|---|---|---|
| **Hot** — wire to wire | 100s of ns; p99.9 in low µs | C++, C, Rust, Zig | AOT, full control of layout/allocation/ordering; Ch. 48 warming for the cold start | Ch. 78 §78.4.2 |
| **Hot, inherited** | same | Java / .NET | garbage-free *and* warmed *and* the collector proven idle over a replayed session | Ch. 77 §77.3–§77.4 |
| **Warm** — 10s of µs, ms tolerated | 10–100 µs; occasional ms | Go, OCaml, Java/.NET with concurrent collectors | the runtime's worst disturbance fits the tail budget; reads the hot tier through shared memory, never called per message | Ch. 77 §77.4.5–§77.4.7, Ch. 76 §76.4.4 |
| **Cold** — seconds | anything | Python, Julia, any JVM/.NET service | the hot code it needs is bound in through the C waist | Ch. 86 §86.4.6, Ch. 76 |
| **Below software** | 10s–100s of ns, deterministic | HDL/HLS, P4/DOCA | no instruction stream | Ch. 72–73 |

The two rules that go with it (Ch. 78 §78.4.2): a component is placed by its **measured worst disturbance**, not its best steady state; and the boundary between tiers is Ch. 76's `extern "C"` waist or Ch. 52's shared memory, never a per-message call into a runtime. The hot-tier verdict after Part XII: **C++ and Rust are interchangeable on performance**; the choice between them is the borrow checker's compile-time elimination of Ch. 82's bug classes against the C++-first vendor SDKs and a team's fluency (Ch. 78 §78.4.4). Rewriting a tuned, measured C++ hot path for its own sake does not pay; new components, parsers and the research seam are where the choice is open (Ch. 75 §75.6).

## B.3 What it is called elsewhere

The hardware, OS and measurement Parts (I–II, VII–XI, Appendix L) are language-independent; the language-mechanics Parts (III–VI) need equivalents, and each row below is measured or shown in the chapter cited:

| This book | Rust | Zig | Java / .NET | Where |
|---|---|---|---|---|
| `constexpr`/`consteval` tables (Ch. 18) | `const fn`, `const` items/blocks, const generics | `comptime` blocks, `comptime` parameters | `static final` arrays (runtime-initialised) | Ch. 78 §78.4.3 |
| Templates / CRTP (Ch. 19); `virtual` (Ch. 14) | traits + generics, monomorphized; `dyn Trait` = fat-pointer vtable | `anytype`, comptime type parameters | interface dispatch, devirtualised by the JIT if monomorphic | Ch. 75 §75.4.5, Ch. 77 §77.6 |
| `std::expected` (Ch. 20) | `Result<T, E>` + `?`; `panic!` for bugs only | error unions `!T` + `try` | exceptions (allocating; off the hot path) | Ch. 75 §75.4.5, Ch. 76 §76.4.3 |
| `__restrict__` (Ch. 21) | `&mut T` → LLVM `noalias`, checked; none for raw pointers | `noalias` on parameters | — | Ch. 75 §75.2.5, §75.5 |
| `std::bit_cast`, wire overlays (Ch. 21, 55) | `from_be_bytes`, `bytemuck::Pod`, `read_unaligned`; `transmute` last | `@bitCast`, `std.mem.readInt`, `extern`/`packed struct` | `VarHandle` byte-array views, `MemorySegment`; `BinaryPrimitives` + `Span<byte>` | Ch. 75 §75.4.1, Ch. 77 §77.4.1, §77.4.4 |
| Arenas, pools, `std::pmr` (Ch. 24) | `MaybeUninit`, `GlobalAlloc`, `bumpalo`/`typed-arena` | explicit `Allocator` parameters | object pools, off-heap `ByteBuffer`/`MemorySegment`, `ArrayPool<T>`, pinned object heap | Ch. 75 §75.2.1, Ch. 77 §77.4.1, §77.4.4 |
| Atomics, orderings (Ch. 30) | `std::sync::atomic` + `Ordering` (same model) | `@atomicLoad`/`@atomicStore`/`@atomicRmw` | `VarHandle` access modes; `Volatile`/`Interlocked` | Ch. 75 §75.2.3, §75.5, Ch. 76 §76.4.4 |
| `alignas`, false-sharing pads (Ch. 9, 33) | `#[repr(C)]`, `#[repr(align(64))]`, `CachePadded` | `extern struct`, `align(64)` | no control over object layout (headers; `[StructLayout]` for structs in .NET) | Ch. 75 §75.2.2 |
| Coroutines (Ch. 39–40) | `async fn` state machines; executor is a library (Tokio, `tokio-uring`, `glommio`, `monoio`) | — | virtual threads / goroutines (stackful, runtime-scheduled) | Ch. 75 §75.4.5, Ch. 77 §77.4.5 |
| Build flags (Appendix D) | `opt-level`, `target-cpu`, `lto`, `codegen-units=1`, `panic=abort`, `overflow-checks`, PGO | `-O ReleaseFast -mcpu=native -fomit-frame-pointer` | `-Xlog:gc*,safepoint`, `-XX:+PrintCompilation`; `TieredPGO`, ReadyToRun, NativeAOT | Ch. 75 §75.4.4, Ch. 77 §77.4.2–§77.4.4 |
| Reading the codegen (Ch. 4) | `--emit asm`, `cargo asm` | `-femit-asm` | `-XX:+PrintAssembly`/JITWatch; `DOTNET_JitDisasm` | Ch. 75 §75.5, Ch. 77 §77.5, Appendix I.11 |
| The C ABI (Ch. 76) | `extern "C"`, `#[repr(C)]`, `bindgen`/`cbindgen`/`cxx` | `extern fn`, `translate-c`, `zig cc` | FFM (`Linker`, `MemorySegment`) / JNI; P/Invoke | Ch. 76 §76.3–§76.4 |

What does *not* transfer: the C++-specific pitfalls (strict aliasing, exception-unwind cost, RTTI, `std::function` — Ch. 20–21) are absent or differently shaped in Rust and Zig and irrelevant to the managed runtimes, whose own pitfalls (GC, safepoints, deoptimisation — Ch. 77 §77.6) C++ does not have. Vendor SDKs arrive as C headers for everyone (Ch. 76 §76.4.6).

## B.4 References

- Part XII — Ch. 74 (asm), Ch. 75 (Rust), Ch. 76 (interop), Ch. 77 (managed runtimes), Ch. 78 (tiers and the cross-language protocol) — each with its own references section; the sources cited there are consolidated in Appendix G.
- *The Rust Programming Language* and *The Rustonomicon*; the Zig language reference; the OpenJDK GC tuning guide, the .NET GC and tiered-compilation documentation, the Go GC guide and the OCaml manual's runtime chapter — the primary documentation behind B.1–B.3.

## B.5 Additional Reading

- **Ch. 78 §78.4.4** (the scorecard) and **§78.7** (the decision checklist) — how to turn this appendix's tables into a recorded decision.
- **Appendix L.5** — the cross-language benchmarking protocol as a checklist; **Appendix I.11** — the per-toolchain asm and runtime-log one-liners; **Appendix F** — GC pause, JIT vs AOT, safepoint, deoptimisation, borrow checker, FFI, hourglass; **Appendix H** — the C++23/26 features in the scorecard's language-mechanics rows; **Appendix A** — the other portability axis (architecture): A.1's weak-memory point applies to every language's atomics.

---

*Next: Appendix C — System Tuning Checklist, a copy-paste-ready, ordered checklist for building a quiet, deterministic box: BIOS/firmware, kernel cmdline, runtime knobs, per-process settings, and a verification pass — each item linking to the chapter that explains why (consolidating Ch. 6, 15, 16, 42–48, 53, 58, 60).*
