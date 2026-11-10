# Part XII — Beyond C++: Languages at the Latency Floor

# Chapter 94 — Rust for the Hot Path

> **Prerequisites:** Ch. 93 (reading and arguing with the emitted asm — every parity claim here is settled that way), Ch. 30–31 (the zero-allocation discipline Rust's ownership model re-expresses), Ch. 11 and 44 (layout, alignment and false sharing — `#[repr(C)]`, `#[repr(align(64))]`), Ch. 41 (the memory model Rust shares with C++, down to the instruction), Ch. 26 (aliasing — `&mut` is the `restrict` you do not have to promise), Ch. 45 (the SPSC ring), Ch. 70 (the decoder) and Ch. 32 §32.4.5 (the book update) — the three kernels this chapter ports and measures.
>
> **Leads into:** Ch. 95 (the C ABI boundary — how a Rust component and the C++ core call each other, and what a `panic` may not do), Ch. 96 (the languages on the other side of the GC line, measured the same way), Ch. 97 (the cross-language table these numbers feed, and `const fn` beside `constexpr` and `comptime`), Appendix B (the summary that now points here).

---

## 94.1 Why it matters: a second language at the floor

Appendix B has said since the first draft that Rust is "a credible hot-path language" — no GC, ahead-of-time LLVM codegen, the same memory model as C++, and a borrow checker that rejects at compile time the use-after-free, data-race and aliased-`&mut` bugs that Ch. 101 §101.4.2 spends a whole section defending against. What the appendix could not say, because nothing had been measured, is what that costs on *this book's* kernels. Rust adds things C++ does not have: a bounds check on every slice index the optimizer cannot prove safe, an overflow check that is a build flag, a `panic` path with its own unwinding machinery, generics that are monomorphized the way templates are but spelled through traits. Each is either free, elided, or a cost you must budget — and "zero-cost" is a claim this book does not accept without a Godbolt diff and a number (Ch. 4, Appendix L).

So this chapter does what Appendix B promised: Ch. 70's AddOrder decode and Ch. 32's flat-array book update (fused into the one kernel Ch. 97 runs in nine languages), and Ch. 45's SPSC ring, written in idiomatic Rust, compiled with rustc 1.97 (LLVM 20), and measured beside GCC 13.3 and Clang 18.1 on the same box, the same capture and the same core. The result is parity on the decode-and-apply kernel to within the compilers' own spread (6.8 vs 7.4 vs 8.2 ns per message), an SPSC handoff latency that is indistinguishable, and three honest places where parity *breaks* — none of them where the folklore says. Along the way it earns the Appendix B table: which C++ pattern becomes which Rust spelling, and which of the book's `unsafe`-by-default habits Rust forces you to name.

## 94.2 Mental model

### 94.2.1 Ownership vs the zero-allocation discipline

Ch. 30–31's rule — *no allocation, no syscall on the steady-state hot path; allocate everything at setup* — is a discipline in C++ and a type-system fact in Rust only partially: Rust will not allocate behind your back (no hidden copies, no implicit conversions that heap-allocate), but `Vec::push`, `String`, `Box::new` and `format!` allocate exactly as `std::vector::push_back` does, and the borrow checker does not care. What ownership *does* give the hot path is **deterministic destruction without a GC and without reference counting**: a value has one owner, borrows are checked, and when the owner goes out of scope the destructor runs — RAII, enforced. The Ch. 31 patterns map directly: a pool is a `Vec<T>` or `Box<[T; N]>` filled at startup and indexed afterwards (indices, not references, because a `&mut` into the pool would lock the whole pool); an arena is `bumpalo`/`typed-arena` or a `Vec<MaybeUninit<T>>` with a cursor; uninitialised storage is `MaybeUninit<T>` (the spelling for "trust me, I will write it before I read it" — Ch. 31 §31.4); a custom global allocator is a `#[global_allocator]` implementing `GlobalAlloc` (the stable half of the allocator API; the per-container `Allocator` parameter is still unstable in 1.97). The idiom shift is that *the compiler knows which function owns what*, so "who frees this" — the Ch. 101 bug class — is not a convention.

### 94.2.2 Layout you control: `repr`, `align`, padding

Rust's default struct layout is *unspecified*: the compiler reorders fields to minimise padding (Ch. 11 would approve) and may change the order between compilations. Anything whose layout matters — a wire overlay, a shared-memory segment (Ch. 67), a struct handed across the C ABI (Ch. 95) — gets `#[repr(C)]`, which is exactly the C/Itanium layout of Ch. 11 §11.2.1, and the same `static_assert` discipline is `const _: () = assert!(std::mem::size_of::<Level>() == 16);` (evaluated at compile time; fails the build). `#[repr(align(64))]` is `alignas(64)`; `crossbeam_utils::CachePadded<T>` is a `#[repr(align(128))]`-or-64 newtype depending on target — the Ch. 44 false-sharing pad as a library type. `#[repr(C, packed)]` is `#pragma pack(1)` with the same misaligned-access caveats (taking a reference to a packed field is a compile error, which is Rust being right about Ch. 11 §11.5). `#[repr(u8)]` on an enum fixes its size for the wire.

### 94.2.3 Atomics and the memory model: the same model, the same instructions

`std::sync::atomic::{AtomicU64, Ordering}` *is* the C++20 memory model — `Relaxed`, `Acquire`, `Release`, `AcqRel`, `SeqCst` with the C++ definitions, `fence()` for `std::atomic_thread_fence`, `compiler_fence()` for the Ch. 41 §41.4.2 compiler barrier (the one Ch. 93 §93.2.3 showed as an empty `asm volatile` with a memory clobber). There is no `memory_order_consume`. §94.5 shows the emitted instructions are the same `mov`/`mov`/`xchg`/`lock xadd`/`lock cmpxchg` as libstdc++ produces, so every reasoning step of Ch. 41–46 transfers verbatim — including the Appendix A.1 porting hazard: a `Relaxed` where `Acquire` was needed compiles, runs on x86-TSO, and breaks on ARM, exactly as in C++. The borrow checker proves the *absence of data races on non-atomic data* (`Send`/`Sync`); it proves nothing about your choice of `Ordering`.

### 94.2.4 Bounds checks: where they are elided and where they stay

Every `slice[i]` is a compare-and-branch to a panic unless LLVM can prove `i < len`. The proofs it can do are mechanical and worth knowing by heart: an index masked to the array's power-of-two size *when the array's length is in its type* (`[Level; 65536]`, `levels[(px as usize) & 65535]` → no check); an iterator (`for x in slice`, `.iter().sum()` → the iterator owns the bound, no per-element check); a fixed-size view (`&[u8; 32]` → every constant index into it is proven); a range check the loop condition already implies. What it cannot elide: indexing a `&[u64]` *slice* with a masked index (the slice's length is a runtime value — it might be shorter than 65536), indexing with a count that may exceed the length (then it hoists *one* check before the loop, §94.5), anything through a pointer it cannot track. `get_unchecked` removes the check and is `unsafe` — the last resort, after the asm has shown the check survived and the measurement has shown it matters (Appendix B.1's caution, now with the §94.5 diffs behind it).

### 94.2.5 `&mut` is `noalias`

Ch. 26 asked you to *promise* `__restrict__` and warned that the compiler could not check you. In Rust the promise is the type system: a `&mut T` is the only live reference to that `T`, so rustc emits LLVM `noalias` on every `&mut` function argument, and the vectorizer needs no runtime overlap check (§94.5: the C++ `axpy` carries `lea/sub/cmp/ja` before its vector loop; the Rust one does not; the `__restrict__` C++ matches Rust). Raw pointers (`*mut T`) carry no such guarantee — there is no `restrict` for them — which is the one aliasing control C++ has that Rust lacks (§94.6).

### 94.2.6 `unsafe` at the hardware boundary

The book's hot path does four things the borrow checker cannot see through: it overlays a struct on wire bytes (Ch. 70), it hands slots between threads in a ring it has proven single-producer/single-consumer by protocol (Ch. 45), it maps and touches device or shared memory (Ch. 34, 67, 87), and it calls C SDKs (Ch. 95). Each is an `unsafe` block — a *scoped* assertion that you have discharged an obligation the compiler could not. The discipline Appendix B.1 stated is now concrete: keep `unsafe` to the one line that needs it, wrap it in a safe function whose signature *makes the invariant a type* (the ring's `push(&self, v)` is safe to call; the `unsafe impl Sync` is the one place the SPSC proof lives), and prefer the safe spelling where one exists (`u64::from_be_bytes` over `transmute`; `bytemuck::Pod` over a pointer cast). The Rust kernel of §94.3 contains exactly one `unsafe`: viewing the 32 MB capture as `&[[u8; 32]]`.

## 94.3 Measure it: the book's kernels, Rust beside GCC and Clang

*Toolchain and machine.* rustc 1.97.0 (LLVM 20; `-C opt-level=3 -C target-cpu=native -C panic=abort`, edition 2021), GCC 13.3 and Clang 18.1 (`-O2 -std=c++23 -march=native`), on the 4-vCPU Intel Xeon VM of Appendix L.2 (Linux 6.18, 2.7 GHz TSC, `taskset -c 2`) — the Ch. 97 protocol: same box, same core, same 1 M-message capture, cold and warm windows separated, five runs, medians. Not the reference machine; the comparisons are the result.

**Kernel 1 — decode + book update** (Ch. 70's `decode()` fused with Ch. 32 §32.4.5's flat-array `apply()`, over a replayed 1 M-message capture; the C++ source is Ch. 97's `kernel.hpp`). The Rust version, complete and idiomatic — no `unsafe` in the kernel itself:

```rust
// kernel.rs — decode + flat-book update in Rust (Ch. 97's kernel, Rust side). Build: rustc --edition 2021 -C opt-level=3 -C target-cpu=native -C panic=abort kernel.rs
#[derive(Clone, Copy)]
pub struct Msg { pub seq: u64, pub id: u64, pub qty: u32, pub px: u32, pub typ: u8, pub side: u8 }

#[inline] fn be32(p: &[u8]) -> u32 { u32::from_be_bytes([p[0], p[1], p[2], p[3]]) }          // safe punning (Ch. 26): a load + bswap, no transmute
#[inline] fn be64(p: &[u8]) -> u64 { u64::from_be_bytes(p[..8].try_into().unwrap()) }       // the unwrap is on a length LLVM proves: no branch survives

#[inline]
pub fn decode(rec: &[u8; 32]) -> Msg {                                                        // &[u8; 32]: the length is in the type → every index below is proven in range
    Msg { seq: be64(&rec[16..24]), id: be64(&rec[24..32]), qty: be32(&rec[4..8]), px: be32(&rec[8..12]), typ: rec[0], side: rec[1] }
}

#[derive(Clone, Copy, Default)] #[repr(C)]
pub struct Level { pub qty: i64, pub count: i32, pub pad: i32 }                               // repr(C): byte-for-byte the C++ Level (Ch. 11)
const LEVELS: usize = 65536;

pub struct Book { bids: Box<[Level; LEVELS]>, asks: Box<[Level; LEVELS]>, best_bid: u32, best_ask: u32 }   // two allocations, at setup (Ch. 30)

impl Book {
    #[inline]
    pub fn apply(&mut self, m: &Msg) -> u64 {
        let idx = (m.px as usize) & (LEVELS - 1);                                            // masked index into a [Level; 65536]: bounds check provably redundant
        let is_bid = m.side == b'B';
        let side = if is_bid { &mut self.bids } else { &mut self.asks };
        let l = &mut side[idx];
        if m.typ == b'A' {
            l.qty += m.qty as i64; l.count += 1;
            if is_bid { if m.px > self.best_bid { self.best_bid = m.px; } } else if m.px < self.best_ask { self.best_ask = m.px; }
        } else {
            let take = (m.qty as i64).min(l.qty);
            l.qty -= take; if l.count > 0 { l.count -= 1; }
            if l.qty == 0 {                                                                   // BBO re-seek (Ch. 32 §32.4.5): walk toward the touch until a non-empty level
                if is_bid && m.px == self.best_bid { while self.best_bid > 0 && self.bids[self.best_bid as usize & (LEVELS - 1)].qty == 0 { self.best_bid -= 1; } }
                if !is_bid && m.px == self.best_ask { while (self.best_ask as usize) < LEVELS - 1 && self.asks[self.best_ask as usize & (LEVELS - 1)].qty == 0 { self.best_ask += 1; } }
            }
        }
        ((self.best_bid as u64) << 32 | self.best_ask as u64) ^ self.bids[self.best_bid as usize & (LEVELS - 1)].qty as u64   // Ch. 97's parity digest term
    }
}
// main(): read the capture; the ONE unsafe — view the Vec<u8> as &[[u8; 32]] (Ch. 70's struct-over-bytes overlay, length-checked once) — then the Ch. 97 windows.
```

```
   decode + apply, ns/message (median of 5; digest 75a4021c2af2c93c in all three)
                               cold [0,1k)  [1k,10k)  [10k,100k)  [100k,1M)   pass 2   pass 3
   Rust 1.97  opt-level=3           17.1        7.0        6.7        6.8       6.8      7.3
   C++ GCC 13.3  -O2                 8.9        7.4        7.6        7.3       7.4      7.4
   C++ Clang 18.1 -O2               10.1        8.2        8.2        8.3       8.2      8.2
```

Steady state: **Rust 6.8–7.3 ns, GCC 7.4, Clang 8.2** — the Rust/GCC difference is smaller than the GCC/Clang difference, i.e. within the spread of two C++ compilers on the same source. §94.5 explains the small Rust edge (84 instructions in the inlined body vs GCC's 97; Clang chose not to inline `apply` at all). The cold window is where Rust is *slower*: 17 ns for the first thousand messages vs 9–10 ns — two 1 MiB `Box<[Level; 65536]>` allocations being first-touched (Ch. 30 §30.2's page faults) plus `Vec::into_boxed_slice` zero-fill, where the C++ `new Level[kLevels]()` had already faulted its pages during construction. Same mechanism, different placement of the setup cost — and a reminder that Ch. 62's warming applies to Rust's `Box` as much as to C++'s `new`.

**Kernel 2 — the SPSC ring** (Ch. 45 §45.3's listing, padded head/tail, acquire/release, ported line for line — `UnsafeCell<T>` slots, `#[repr(align(64))]` pads, `unsafe impl Sync`). Throughput first, and the honest finding is about the *measurement*, not the language:

```
   unbatched ring, 200 M messages, cores 1→3, alternating runs
   C++ (GCC)  :  74, 102, 101, 108, 100, 151, 88, 103 Mmsg/s        Rust: 28, 30, 35, 31, 45, 38, 79, 30 Mmsg/s
   C++ with the consumer's locals kept in registers (as Rust's are): 107, then 23 Mmsg/s   <- bistable
   Rust with one extra store per pop (as the C++ harness had):        37, then 90 Mmsg/s   <- bistable
   batched ring (each end caches the other's index — Ch. 45 §45.3): C++ 197, 180, 78;  Rust 51, 39, 110 Mmsg/s
```

The emitted hot loops are the same five instructions (§94.5), both threads were verified on their cores, and placement of the ring (static, boxed, inline buffer, every offset within a page) changed nothing systematically — yet a given binary lands in one of **two regimes**: lock-step, where the consumer catches the producer every message and each handoff costs a cross-core line transfer (~30 ns → ~30 Mmsg/s), or lagged, where the consumer trails by a few slots, the index lines move less often and the transfers overlap (~10 ns → ~100 Mmsg/s). Which regime a run falls into is decided by thread start-up timing and by incidental details of the harness — the C++ consumer lambda's two extra stack stores happened to slow it into the lagged regime more often; remove them and C++ flips too. A *throughput* number for an unbatched SPSC ring is therefore not a property of the code, in either language, and Ch. 45 §45.3's remark that latency is the quantity a tick-to-trade path cares about is the right instrument:

```
   ping-pong round trip (A pushes to ring 1, B echoes on ring 2; 2 M round trips, first 10% dropped; TSC ticks at 2.7 GHz, 0.37 ns/tick)
   C++  : p50 2390 / 1912 / 2240   p99 2900 / 2686 / 2950   p99.9 4284 / 6588 / 3588
   Rust : p50 2444 / 1838 / 1998   p99 2662 / 2370 / 2466   p99.9 3236 / 2618 / 3114
```

Round-trip p50 ≈ 700–900 ns on this VM (two handoffs, each a cross-vCPU line transfer plus polling detection — Appendix E's bare-metal figure is a quarter of that), and **Rust and C++ are indistinguishable, run to run**. That is the parity result for the SPSC ring; the throughput bistability is the Ch. 3 lesson it came wrapped in.

**Kernel 3 — the costs that are build flags, not language.** The same `kernel.rs`, three ways:

```
   -C panic=abort   : .text 276,136 B   .eh_frame 22,148 B   .gcc_except_table 10,984 B      pass 3: 7.3 ns/msg
   -C panic=unwind  : .text 277,800 B   .eh_frame 22,496 B   .gcc_except_table 11,240 B      pass 3: 7.1 ns/msg   <- the unwinding tables barely move:
                                                                                                  the precompiled std already carries them (-Zbuild-std, nightly, removes them)
   -C overflow-checks=on (abort): decode+apply body 93 instructions (+9), 6 `jo`→trap sites;   pass 3: 7.5 ns/msg   <- predicted-not-taken: in the noise here
```

## 94.4 Techniques

### 94.4.1 The wire overlay without `transmute`

Ch. 70 §70.2.2's zero-copy decode is a *load plus a byte-swap* per field, and the Rust spelling that compiles to exactly that (§94.5) is `u32::from_be_bytes(rec[4..8].try_into().unwrap())` — safe code, the `try_into` on a 4-byte sub-slice of a `&[u8; 32]` proven at compile time, no `unsafe`, no `transmute`. For a whole-struct overlay (`#[repr(C, packed)] struct AddOrderWire { … }` over the bytes) the choice is `bytemuck::from_bytes::<T>(&rec[..])` with `T: Pod` — the validity rules (no padding, every bit pattern valid) checked once at the type — or, at the lowest level, `std::ptr::read_unaligned(rec.as_ptr() as *const T)` inside `unsafe`, which is Ch. 26's `memcpy`-into-local made explicit. `std::mem::transmute` is the reinterpret cast: it checks sizes only, producing an invalid `bool` or enum from wire bytes is UB exactly as in C++, and nothing in this book's decoders needs it. The one view-cast the kernel does need — the capture buffer as records — is `std::slice::from_raw_parts(buf.as_ptr() as *const [u8; 32], buf.len() / 32)`: `unsafe`, length-checked once, and the only such line in the program.

### 94.4.2 Indexing that compiles to a load

In order of preference, each verified in §94.5: put the length in the type (`&[u8; 32]`, `Box<[Level; 65536]>`) so constant and masked indices are proven; iterate instead of indexing (`for (i, x) in a.iter().enumerate()` — the iterator owns the bound; `chunks_exact(32)` for records); hoist the one check yourself (`let a = &a[..n];` before the loop makes the loop's `a[i]` provably in range and gives you one `panic` site at the top); and only then `get_unchecked`, in an `unsafe` block with a comment naming the invariant. The SoA book of Ch. 10 fits the first rule perfectly: parallel fixed arrays indexed by one masked tick.

### 94.4.3 The SPSC ring: where `unsafe` lives and why it is small

```rust
#[repr(align(64))] struct Line<T>(T);                                 // the Ch. 44 pad as a type (crossbeam::CachePadded does the same, 128 B on some targets)
struct SpscRing<T: Copy> { buf: Box<[UnsafeCell<T>; CAP]>, head: Line<AtomicUsize>, tail: Line<AtomicUsize> }
unsafe impl<T: Copy + Send> Sync for SpscRing<T> {}                   // THE assertion: the push/pop protocol below is the proof; the compiler cannot see it
impl<T: Copy> SpscRing<T> {
    #[inline] fn push(&self, v: T) -> bool {
        let h = self.head.0.load(Ordering::Relaxed);
        if h.wrapping_sub(self.tail.0.load(Ordering::Acquire)) == CAP { return false; }
        unsafe { *self.buf[h & (CAP - 1)].get() = v; }                 // the slot write: single producer owns [tail, head) — that is the discharged obligation
        self.head.0.store(h.wrapping_add(1), Ordering::Release); true  // publish (Ch. 41): a plain mov on x86, stlr on ARM
    }
    // pop(&self, v: &mut T) -> bool mirrors Ch. 45 §45.3 exactly; the batched variant caches the opposite index in a Line<Cell<usize>> owned by one side
}
```

`UnsafeCell` is the one primitive that permits mutation through a shared reference — `std::atomic`, `Cell`, `Mutex` are all built on it — and `&self` methods on the ring are the *honest* signature: both threads hold a `&SpscRing`. The `unsafe impl Sync` is the sentence "this type is safe to share across threads because of the protocol", and it is the only place a reviewer has to think; everything else is checked. Compare Ch. 45's C++, where the same proof exists only in the comments.

### 94.4.4 The build profile for a hot path

| Appendix D (C++) | Cargo profile / rustc flag | Note |
|---|---|---|
| `-O2`/`-O3 -march=native` | `opt-level = 3`, `-C target-cpu=native` (or `=icelake-server`) | Rust's release default is `opt-level=3`; `2` is closer to `-O2` and usually identical on these kernels |
| `-flto` | `lto = "fat"` (or `"thin"`), `codegen-units = 1` | the single codegen unit matters more than LTO for small crates: it restores cross-function inlining |
| `-fno-exceptions` | `panic = "abort"` (+ `-Zbuild-std -Zbuild-std-features=panic_immediate_abort` on nightly to strip std's tables) | measured above: without rebuilding std the tables stay |
| `-fwrapv` / UBSan integer | `overflow-checks = false` (release default) / `= true` | a *build mode*, not a type property: audit which one production ships (§94.6) |
| `-fprofile-generate/-use` | `-C profile-generate=/tmp/pgo` then `-C profile-use=merged.profdata` (`llvm-profdata merge`) | the Ch. 27 PGO step, same LLVM machinery as Clang |
| `-fno-omit-frame-pointer` | `-C force-frame-pointers=yes` | for the Ch. 27 §27.4.4 unwinders and `perf` |
| `-fvisibility=hidden` | the default for a `cdylib`; `#[no_mangle] pub extern "C"` exports | Ch. 95 |
| `-S` / Godbolt | `--emit asm`, `cargo asm` (cargo-show-asm), `cargo rustc -- --emit asm`; Godbolt has rustc | §94.5 |

### 94.4.5 What maps to what: Part III in Rust

The Appendix B table, now with the chapters behind each row: Ch. 21's `constexpr`/`consteval` is `const fn` and `const` items (a strict subset — no heap, no trait objects; Ch. 97 §97.4.3 builds the same tick table in both and shows identical `.rodata`); a non-type template parameter is a const generic `const N: usize`; Ch. 22's templates/CRTP are **traits with generics**, monomorphized per concrete type and inlined exactly like a template instantiation (`fn apply<B: Book>(b: &mut B)`), while `dyn Trait` is Ch. 16's indirect call as a fat pointer (data + vtable) rather than an in-object vptr — same BTB exposure, same un-inlinable call, and the same rule: not on the hot path; Ch. 24's `std::expected` is `Result<T, E>` with `?`, no unwinding on the success path, and `panic!` is for bugs, never control flow; Ch. 26's `__restrict__` is `&mut` (§94.2.5) and `std::bit_cast` is `from_bits`/`from_ne_bytes`; Ch. 51's coroutines are `async fn` state machines with a *library* executor (Tokio, or a pinned single-threaded `io_uring` runtime — `tokio-uring`, `glommio`, `monoio` — for the Ch. 56 reasons). Ch. 39's SIMD is `std::arch::x86_64` intrinsics (stable, behind `#[target_feature]`/`is_x86_feature_detected!`), with `std::simd` still nightly-only in 1.97.

## 94.5 Verify the codegen: side by side

All excerpts are rustc 1.97 `-C opt-level=3 -C target-cpu=x86-64-v3 --emit asm` (AT&T syntax, as rustc emits) beside GCC 13.3 / Clang 18.1 `-O2 -march=x86-64-v3 -S -masm=intel`; labels and directives stripped. The Rust functions are declared `#[no_mangle] pub extern "C"` so their symbols are readable.

**A masked index into a sized array has no check; into a slice it keeps one.**

```asm
level_masked:  (levels: &[u64; 65536], px: u32)        level_cxx:  (const uint64_t* levels, uint32_t px)
        movzwl  %si, %eax                                      movzx   esi, si
        movq    (%rdi,%rax,8), %rax                            mov     rax, QWORD PTR [rdi+rsi*8]
        retq                                                   ret
level_slice:   (levels: &[u64], px: u32)                 <- the slice's length (rsi) is a runtime value: the check stays
        movq    %rdi, %rax
        movzwl  %dx, %edi
        cmpq    %rdi, %rsi
        jbe     .LBB5_2                 ; -> core::panicking::panic_bounds_check
        movq    (%rax,%rdi,8), %rax
        retq
level_unchecked: identical to level_masked (get_unchecked)
```

**`sum_indexed(a, n)` hoists one check; `sum_iter` has none.** With `for i in 0..n { s += a[i] }` the optimizer emits a single `cmpq %rax, %rsi / jbe panic` (is `n-1 < len`?) *before* the vectorized loop, then a 16-wide `vpaddq` body — one predicted branch per call, not per element. `a.iter().sum()` has no compare at all. The Go version of the same kernel, for contrast, keeps 7 of its bounds checks (`-gcflags=-d=ssa/check_bce/debug=1`) and 4 `runtime.panic` call sites in `apply`.

**Atomics: the same instructions as `std::atomic`.**

```asm
load_acquire      movq (%rdi), %rax                     mov  rax, QWORD PTR [rdi]
store_release     movq %rsi, (%rdi)                     mov  QWORD PTR [rdi], rsi
store_seqcst      xchgq %rsi, (%rdi)                    xchg rsi, QWORD PTR [rdi]
fetch_add_acqrel  movl $1,%eax ; lock xaddq %rax,(%rdi) mov eax,1 ; lock xadd QWORD PTR [rdi], rax
compare_exchange  movq %rsi,%rax ; lock cmpxchgq %rdx,(%rdi) ; sete %al      mov rax,rsi ; lock cmpxchg QWORD PTR [rdi], rdx ; sete al
```

**`&mut` vs `__restrict__` vs may-alias.** `axpy(y: &mut [f64], x: &[f64], a: f64)` at `opt-level=3` is a 4- and 16-wide FMA loop with *no* overlap test — its prologue is `cmp/cmovb` for `min(len)` and the width dispatch. The C++ `axpy(double* y, const double* x, n, a)` at `-O3` carries `lea rax,[rsi+8] / sub rdi,rax / cmp rdi,16 / ja .vector` — the runtime alias check Ch. 26 §26.5 described — and a scalar fallback for the overlapping case (73 lines vs 51); `axpy_restrict` drops the check and matches Rust's shape. (At `-O2`, GCC 13 vectorizes neither C++ version; Rust's `opt-level=3` is the fair comparison to `-O3` here.) The raw-pointer Rust version `axpy_raw(*mut f64, *const f64, …)` is where the guarantee ends — LLVM must assume aliasing again.

**The decode + apply body.** The Rust `rs_decode_apply(&mut Book, &[u8; 32])` is **84 instructions, fully inlined, with no call and no `panic_bounds_check`** — the decode's four `movbe` loads flow straight into the book update. GCC's `cxx_decode_apply` is 97 instructions, also fully inlined (it spends more on the `Msg` temporary and on `vpshufb` byte swaps where Rust used scalar `movbe`). Clang emitted a 15-instruction decode that stores the `Msg` to the stack and *calls* `Book::apply` — the inlining decision, not the language, is the 8.2-vs-7.4 ns in §94.3. The habit is Ch. 4's: count instructions, find the calls, find the branches to `panic`, and compare *like with like* — the same box, flags of equal intent, and the compiler's inlining decisions read, not assumed.

## 94.6 Pitfalls & anti-patterns: where parity breaks, and the one that is not Rust's fault

- **`Vec` growth on the hot path.** `vec.push()` past capacity reallocates and copies — Ch. 30's `malloc` in disguise, with the same tail. `Vec::with_capacity` at setup and `debug_assert!(v.len() < v.capacity())` before the push, or a fixed `[T; N]`/`Box<[T; N]>` with your own cursor. The compiler will not stop you.
- **Overflow checks are a *profile* decision.** Debug builds trap on `u32` overflow; release builds wrap. A price computation that is "correct" in CI and wraps in production — or traps in production because someone shipped `overflow-checks = true` for safety — is a Ch. 35/101 §101.2 hazard in a new coat. Choose explicitly (`wrapping_add`, `checked_mul`, `saturating_sub` say what you mean regardless of profile) and read the §94.3 cost (6 trap sites, noise-level here, measurable on a tighter loop).
- **Panics are unwinding, and tables, and a path.** Every `unwrap`, `expect`, `[]` and arithmetic trap is a call to a cold panic path; `panic = "abort"` makes it a trap but, as §94.3 measured, does not shrink the shipped binary's `.eh_frame` unless std is rebuilt. On the hot path, write code with *no* panic site (§94.4.2's hoisting makes the one that remains visible), and treat a `panic` crossing a C ABI as the Ch. 95 §95.4.3 abort it is.
- **No `restrict` for raw pointers.** Where you have dropped to `*mut T` — the NIC ring, a shared-memory segment — the aliasing guarantee is gone and so is the vectorization it enabled. Re-borrow into a `&mut [T]` slice as early as possible (`std::slice::from_raw_parts_mut`, one `unsafe`) so the rest of the function is checked and `noalias` again.
- **`Ordering` is yours to get wrong.** `Relaxed` everywhere compiles and passes on x86 (§94.2.3). Appendix A.1's ARM test and Ch. 53's TSan (`-Zsanitizer=thread` on nightly, or Miri for the model) are the same gates as for C++, and Loom exists for exhaustively exploring interleavings of small lock-free code — a tool C++ lacks.
- **The ecosystem gap is in the SDKs.** `ef_vi`, DPDK, `libibverbs`, exchange protocol decoders arrive as C headers (Ch. 75, 82, 83); `bindgen` makes them callable, but the zero-cost RAII wrappers of Ch. 95 §95.4.2 are yours to write, and crate quality in this niche varies. Budget for it.
- **Reading a throughput number as a property of the language.** §94.3's SPSC bistability: the same instructions gave 23 and 107 Mmsg/s. When two languages differ by 3×, suspect the harness before the compiler — pin, read the asm, measure latency, and run the batched variant — and only then write "Rust is slower" or "faster".
- **Rewriting what works.** Appendix B.5's rule stands: a tuned, measured C++ hot path is not improved by being Rust. The chapter's numbers say the languages are at parity; the borrow checker's return is on *new* components and on the parser/boundary code where Ch. 101's bug classes live.

## 94.7 Exercises & checklist

1. **Port Ch. 70's FIX text parser** (`decode_fix`) to Rust with `str::from_utf8` avoided (bytes only) and `atoi`-style parsing; count the bounds-check branches in `--emit asm` before and after hoisting `let s = &s[..n]`.
2. **Make the SPSC ring deterministic.** Implement the batched ring in both languages (cache the opposite index) and *then* add the Ch. 45 ping-pong harness; report latency percentiles, not throughput. Confirm with `--emit asm` that `Acquire`/`Release` are plain `mov`s.
3. **Break parity on purpose.** Replace `Box<[Level; LEVELS]>` with `Vec<Level>` in `kernel.rs`; find the bounds check that reappears in `apply`; measure it; then recover it with `&levels[..LEVELS]` at function entry.
4. **ARM.** Cross-compile `kernel.rs` and `spsc.rs` for `aarch64-unknown-linux-gnu` and read the `ldar`/`stlr` the orderings produce (Appendix A.1); then change one `Acquire` to `Relaxed` and observe the `ldr`.
5. **Rebuild std.** On nightly, build with `-Zbuild-std -Zbuild-std-features=panic_immediate_abort` and re-measure `.eh_frame`/`.gcc_except_table`; compare to §94.3.

Checklist for a Rust hot-path component:

- [ ] Steady state allocates nothing: `Vec`/`Box`/`String` growth audited; pools and arenas sized at setup (Ch. 30–31).
- [ ] Every wire/shared/FFI struct is `#[repr(C)]` with a `const _: () = assert!(size_of…)` beside it (Ch. 11, 95).
- [ ] No `panic` site on the hot path, or exactly one hoisted check at function entry, verified in `--emit asm` (`panic_bounds_check` absent).
- [ ] `unsafe` blocks are one line each, wrapped in a safe function whose signature carries the invariant; `unsafe impl Send/Sync` has the protocol written beside it.
- [ ] `Ordering`s reviewed against Ch. 41 and tested under TSan/Miri/Loom and on ARM.
- [ ] Release profile chosen deliberately: `opt-level`, `target-cpu`, `codegen-units = 1`, `lto`, `panic`, `overflow-checks` — and the integer semantics spelled (`wrapping_*`/`checked_*`) so the profile cannot change them.
- [ ] Codegen read beside the C++ it replaces — same box, same flags of intent, inlining decisions compared, not assumed.

## 94.8 References

- *The Rust Programming Language* (Klabnik & Nichols) and *The Rustonomicon* — ownership, borrowing, `unsafe`, `UnsafeCell`, `Send`/`Sync`, the aliasing model behind `noalias` (§94.2, §94.4.3).
- *The Rust Reference*, "Type layout" and "Behavior considered undefined" — `repr(C)`/`repr(align)`/`repr(packed)` and the validity rules `transmute` can violate (§94.2.2, §94.4.1).
- *The Rust Performance Book* (Nethercote) — bounds-check elision patterns, allocation avoidance, profile settings (§94.2.4, §94.4.2, §94.4.4).
- `std::sync::atomic` documentation and Mara Bos, *Rust Atomics and Locks* (O'Reilly, 2023) — the memory model as Rust states it, with the x86/ARM codegen the book's Ch. 41–46 reason about (§94.2.3, §94.5).
- LLVM Language Reference, `noalias` parameter attribute — what rustc emits for `&mut` and what the vectorizer does with it (§94.2.5, §94.5).
- `crossbeam-utils` (`CachePadded`), `bytemuck` (`Pod`/`Zeroable`), `bumpalo` documentation — the library spellings of Ch. 44, 70 and 31 patterns (§94.2.2, §94.4.1).
- rustc book, "Codegen options" (`opt-level`, `target-cpu`, `lto`, `codegen-units`, `panic`, `overflow-checks`, `profile-generate/use`, `force-frame-pointers`) and the `cargo` profiles reference (§94.4.4).
- Loom and Miri documentation — model-checking and interpreting small lock-free code under the Rust memory model (§94.6; Ch. 53).

## 94.9 Additional Reading

- *Rust for Rustaceans* (Gjengset) — the `unsafe` abstraction chapter and the FFI chapter, the engineering of the boundary this chapter's `unsafe impl Sync` sits on.
- Godbolt / Compiler Explorer with rustc, and `cargo-show-asm` — the Ch. 4 workflow for Rust (§94.5).
- The Crossbeam project's SPSC/MPMC queue sources — production versions of §94.4.3 with the batching of Ch. 45 §45.3.
- Appendix B (the summary and tier table), Ch. 95 (calling the C++ core and vendor SDKs from Rust, and what a `panic` may not do), Ch. 97 (this kernel in nine languages, and `const fn` beside `constexpr`/`comptime`), Appendix A.1 (the orderings on ARM), Appendix H (the C++23/26 features Rust already has or lacks).

---

*Next: Ch. 95 — Language Interop & ABI Boundaries: the `extern "C"` hourglass around the C++ core, opaque handles and parity asserts on both sides, the measured cost ladder of a cross-language call from a C++ inline to Python's `ctypes` (2.5 ns to 474 ns), zero-cost RAII wrappers over `liburing`, `ef_vi` and verbs proven identical in asm, and what exceptions, panics and `_Atomic`s may and may not do at the boundary.*
