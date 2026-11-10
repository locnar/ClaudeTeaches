# Part XII — Beyond C++: Languages at the Latency Floor

# Chapter 94 — Language Interop & ABI Boundaries

> **Prerequisites:** Ch. 4 §4.2.1 (the SysV calling convention — the one contract every language on Linux honours), Ch. 11 (object layout — a struct that crosses a boundary has exactly one layout, and both sides must assert it), Ch. 24 §24.6 (a throw across a C frame is undefined behaviour), Ch. 27 §27.4.2 (`-fvisibility=hidden`, the PLT, `-fno-plt`), Ch. 67 (shared-memory IPC — the alternative to calling at all), Ch. 65, 82 and 83 (`liburing`, `ef_vi`/DPDK and `libibverbs`: the vendor C SDKs the hot path is built on and this chapter wraps), Ch. 93 (the Rust side of the boundary).
>
> **Leads into:** Ch. 95 (the managed runtimes on the far side of the boundary, and why a call *into* one is a different animal from a call into C), Ch. 96 (the polyglot architecture: where the boundary belongs per latency tier), Ch. 107 §107.4.6 (the research seam — the same hourglass, bound into Python), Appendix B (summary).

---

## 94.1 Why it matters: the hot path already crosses a boundary on every packet

Every book-built hot path calls C. `io_uring_get_sqe` and `io_uring_submit` (Ch. 65), `ef_vi_receive_poll` and `exanic_receive_frame` (Ch. 82), `ibv_post_send` and `ibv_poll_cq` (Ch. 83), `rte_eth_rx_burst` (Ch. 82) — all C functions in C headers, called from C++ with no treatment so far of what the call costs, what the C++ around it may and may not do, or how to wrap it so that RAII and `std::span` protect the caller without adding an instruction. The same boundary appears in the other direction the moment the system is polyglot, which every real one is: a Python research notebook that must run the *production* decoder rather than a drifting reimplementation (Ch. 107 §107.4.6), a Java risk service that reads the C++ book, a Rust component beside the C++ core (Ch. 93), a Go control plane that calls into the engine for a config reload (Ch. 101).

Two facts organise the chapter, and both are measured in §94.3. **A C ABI call is a call** — the same `call`/`ret`, the same register contract (Ch. 4 §4.2.1), 2.5–3 ns from C++, Rust, Zig or OCaml on the test box, within a few hundred picoseconds of the inlined C++ code; the cost is the lost inlining, not the language. **A call into or out of a managed runtime is a transition**, and its price is the runtime's bookkeeping: 10 ns through Java's foreign-function API with the safepoint transition disabled, 29 ns through JNI, 50 ns through Go's `cgo`, 340–470 ns through Python's `pybind11`/`nanobind`/`ctypes`. The ladder spans two orders of magnitude, and it dictates the architecture: a per-message call across a runtime boundary is a Ch. 1 tail you added on purpose; batch the work on one side, or share memory (Ch. 67) and call nothing. The rest is the discipline that keeps the boundary from becoming the Ch. 100 bug class — opaque handles, parity asserts on both sides, `noexcept` edges, and the knowledge of which things (exceptions, panics, C++ types, allocators, thread-locals) may never cross.

## 94.2 Mental model

### 94.2.1 The hourglass: one narrow C waist

Both halves of a polyglot system are wide — a C++ engine with templates and RAII, a Python/Java/Rust client with its own types — and the boundary between them is a **narrow waist of plain C**: a handful of `extern "C"` functions taking and returning only things C has (integers, pointers, `struct`s of those), with every C++ object hidden behind an **opaque handle** (`typedef struct book_t book_t;` — a pointer to an incomplete type the caller can hold but never dereference). The waist is the only thing either side depends on; the C++ can change its classes and the client its language without touching the other. Ch. 107 §107.4.6 and §94.3's call ladder both use one such waist, `book_c_api.h`:

```c
/* book_c_api.h — the extern "C" hourglass over the C++ Book (Ch. 96's kernel.hpp): opaque handle, POD-only arguments, nothing throws. */
#pragma once
#include <stdint.h>
#include <stddef.h>
#ifdef __cplusplus
#define BOOK_NOEXCEPT noexcept             /* C++ callers see noexcept; C sees nothing: the contract is the same (§94.4.3) */
extern "C" {
#else
#define BOOK_NOEXCEPT
#endif
typedef struct book_t book_t;                                        /* opaque: callers never see the C++ layout */
book_t*  book_new(void) BOOK_NOEXCEPT;                               /* NULL on failure — errors are values at the waist */
void     book_free(book_t*) BOOK_NOEXCEPT;
uint64_t book_apply_raw(book_t*, const uint8_t* rec32) BOOK_NOEXCEPT;                 /* decode + apply one 32-byte wire record */
uint64_t book_apply_range(book_t*, const uint8_t* buf, size_t first, size_t last, uint64_t h0) BOOK_NOEXCEPT;   /* batch: the loop on the C++ side */
uint32_t book_best_bid(const book_t*) BOOK_NOEXCEPT;
#ifdef __cplusplus
}
#endif
```

```cpp
// book_c_api.cpp — the implementation. Build: g++ -O2 -std=c++23 -march=native -fPIC -fvisibility=hidden -shared book_c_api.cpp -o libbook.so
#include "book_c_api.h"
#include "kernel.hpp"
#define API extern "C" __attribute__((visibility("default")))         // only the waist is exported (Ch. 27 §27.4.2): nm -D shows seven symbols
struct book_t { Book b; };                                            // the opaque type IS the C++ object
API book_t*  book_new(void) noexcept { return new (std::nothrow) book_t; }             // nothrow: nothing can throw across the boundary
API void     book_free(book_t* h) noexcept { delete h; }
API uint64_t book_apply_raw(book_t* h, const uint8_t* rec) noexcept { return h->b.apply(decode(rec)); }
API uint64_t book_apply_range(book_t* h, const uint8_t* buf, size_t first, size_t last, uint64_t acc) noexcept {
    for (size_t i = first; i < last; ++i) acc = fold(acc, h->b.apply(decode(buf + i * 32)));
    return acc;
}
API uint32_t book_best_bid(const book_t* h) noexcept { return h->b.best_bid(); }
```

### 94.2.2 What may not cross

The C ABI defines registers, stack and the layout of C types. Everything else is a *per-language runtime convention* and must stop at the waist: **C++ exceptions** (a throw through a frame without unwind tables is UB — Ch. 24 §24.6; §94.4.3 shows the three things that actually happen); **Rust panics** (an unwind reaching an `extern "C"` frame aborts the process since Rust 1.81); **C++ types** (`std::string`, `std::vector`, `std::span`, references, templates — none has a C layout or a stable ABI across compilers; pass pointer + length); **ownership of allocations** (memory `new`ed on one side is `delete`d on that side: provide `book_free`, never let the caller `free()` it — different allocators, different runtimes); **thread-locals, signal handlers and `errno`** (a callback invoked on a runtime's thread may not see the TLS you expect — Ch. 106 §106.4.1's signal-safety applies); **callbacks into a managed runtime** (a C→Java/Python/Go call attaches a thread, takes a GIL or transitions a safepoint state — §94.6). The waist carries integers, pointers to POD, and error codes. That is all it should carry.

### 94.2.3 The cost model: a call is a call; a transition is not

A direct call through the waist from an AOT language costs what Ch. 4 §4.2.1 says: argument setup, `call`, callee prologue, `ret`, and — because the body is in another object file — the loss of inlining and the register pressure of the ABI's caller-saved set. Through a shared object it additionally goes via the PLT (one `jmp` through the GOT; Ch. 27 §27.4.2), avoidable with `-fno-plt` or static linking. §94.3 puts that at ~0.2–0.4 ns over the inlined code on the test box. A call across a **runtime** boundary pays for whatever that runtime must do to let native code run: Java's FFM must stash the thread's state so a GC can find it (the *safepoint transition*, skippable with `Linker.Option.critical`/`isTrivial` when the callee is short and touches no Java heap); JNI must additionally pin or copy any Java array it hands you (`GetPrimitiveArrayCritical`); Go's `cgo` switches from the goroutine's small stack to a system stack and tells the scheduler the thread is blocked; CPython must convert every argument from a `PyObject`, hold or release the GIL, and box the result. None of that is the callee's cost; it is the price of the caller's runtime, and no amount of C++ on the other side reduces it.

### 94.2.4 Symbols, visibility and the dynamic linker

A shared library's *default* is to export every non-static symbol, including every C++ template instantiation and inline function — thousands of relocations at load time and a public ABI you did not intend. The Ch. 27 §27.4.2 recipe: `-fvisibility=hidden` (nothing exported) plus an explicit `__attribute__((visibility("default")))` on the waist (seven symbols in `libbook.so`), `-fno-semantic-interposition` so the compiler may inline within the library, a **version script** (`LIBBOOK_1.0 { global: book_*; local: *; };`) so the ABI is named and `libabigail`'s `abidiff` can catch an accidental change, and `extern "C"` so the names are unmangled and stable across compilers.

## 94.3 Measure it: the cross-language call ladder

*Toolchain and machine.* GCC 13.3 / Clang 18.1, rustc 1.97, Zig 0.17, OCaml 4.14 (`ocamlopt`), OpenJDK 21.0.12 (FFM via `--enable-preview`; JNI), Go 1.24.7 (`cgo`), CPython 3.13 with `ctypes`, pybind11 3.1 and nanobind 3.1; the 4-vCPU Intel Xeon VM of Appendix L.2 (Linux 6.18, 2.7 GHz, `taskset -c 2`) — not the reference machine. Every rung calls the *same* `libbook.so` (`book_apply_raw`, the §94.2.1 waist) 20 M times over a 1024-record ring after a warm-up, and every rung's digest is the same, so every rung did the same work.

```
   rung (caller → libbook.so)                           ns / call   batch rung (1024 records per call)   ns / msg
   C++ with the Book inlined (no boundary; the floor)        2.48    —
   C++ → C ABI via PLT (.so)                                 2.90    C++ book_apply_range                     1.48
   C++ → C ABI, -fno-plt                                     2.58
   Rust → extern "C" (hand-declared; what bindgen emits)      2.66    Rust                                     1.48
   Zig → extern fn (what translate-c emits)                   2.65
   OCaml → external [@@noalloc] [@untagged] [@unboxed]        3.04    OCaml                                    1.49
   Java 21 FFM downcall, Linker.Option.isTrivial()           10.47    Java FFM                                 2.64
   Java 21 JNI, byte[] via GetPrimitiveArrayCritical         29.41
   Go 1.24 cgo                                               49.59    Go cgo                                   1.75
   Python 3.13 pybind11 (bound method, buffer argument)     340.1     pybind11                                 1.88
   Python 3.13 nanobind                                      346.7     nanobind                                 1.98
   Python 3.13 ctypes                                        473.8     ctypes                                   2.22
```

Read it in three bands. **The AOT band (2.5–3.0 ns)** is one call: C++, Rust, Zig and OCaml are within 0.5 ns of each other and within 0.4 ns of the *inlined* C++ — the boundary costs a `call`/`ret` and a PLT hop, and OCaml's `[@@noalloc]`/`[@unboxed]` annotations are what keep it in this band (without them the runtime boxes the `int64` result and the call pays an allocation). **The managed band (10–50 ns)** is a runtime transition: FFM with the safepoint transition disabled is 4× the AOT call; the default FFM downcall, JNI's array pinning and `cgo`'s stack switch are 10–20×. **The interpreter band (340–474 ns)** is argument conversion and the GIL: two orders of magnitude, and `nanobind` vs `pybind11` vs `ctypes` move it by ±30 %, not by the 100× the architecture needs. The batch column is the architecture: when the *loop* lives on the C++ side, every caller — Python included — pays 1.5–2.6 ns per message, i.e. the kernel's own cost (Ch. 96: 7 ns including the book; here the ring is L1-resident). **Amortise the boundary or do not cross it per message**; Ch. 67's shared-memory ring is the limit of that advice — zero calls.

## 94.4 Techniques

### 94.4.1 Parity asserts on both sides

A struct that crosses the waist — a config block, a shared-memory header, a message — has one layout, and both sides assert it at build time so a reordered field fails the build rather than the trade. The Ch. 11 §11.4 discipline, in every language of this Part:

```cpp
// C++ (and C with _Static_assert): the authority — kernel.hpp's Level
static_assert(sizeof(Level) == 16 && offsetof(Level, count) == 8 && alignof(Level) == 8);
```
```rust
#[repr(C)] pub struct Level { pub qty: i64, pub count: i32, pub pad: i32 }
const _: () = assert!(std::mem::size_of::<Level>() == 16 && std::mem::align_of::<Level>() == 8);   // compile-time; std::mem::offset_of!(Level, count) == 8 on 1.77+
```
```zig
const Level = extern struct { qty: i64, count: i32, pad: i32 };     // extern struct = C layout
comptime { std.debug.assert(@sizeOf(Level) == 16 and @offsetOf(Level, "count") == 8); }
```
```python
class Level(ctypes.Structure): _fields_ = [("qty", ctypes.c_int64), ("count", ctypes.c_int32), ("pad", ctypes.c_int32)]
assert ctypes.sizeof(Level) == 16 and Level.count.offset == 8      # import-time, before any call
```
```java
static final MemoryLayout LEVEL = MemoryLayout.structLayout(JAVA_LONG.withName("qty"), JAVA_INT.withName("count"), JAVA_INT.withName("pad"));
static { assert LEVEL.byteSize() == 16 && LEVEL.byteOffset(PathElement.groupElement("count")) == 8; }   // FFM: the layout is data, checkable
```

The same four numbers everywhere — size, alignment, one interior offset, and (for shared memory) the atomic's size (§94.4.4) — and a test that round-trips one record through the waist and compares the digest (Ch. 96 §96.4.6's parity digest is exactly this across nine languages).

### 94.4.2 Zero-cost RAII wrappers over vendor C SDKs

The vendor SDK gives you `int io_uring_queue_init(unsigned, struct io_uring*, unsigned)` and a matching `_exit`; the C++ caller wants a type that cannot leak, cannot be copied, and whose hot-path methods compile to the raw calls. The pattern, verified in §94.5 to add *no instructions*:

```cpp
// uring_wrap.cpp — a zero-cost RAII wrapper over liburing (Ch. 65). The same shape wraps ef_vi (Ch. 82: ef_vi_alloc/ef_vi_free, ef_vi_receive_poll),
// libibverbs (Ch. 83: ibv_create_qp/ibv_destroy_qp, ibv_poll_cq) and DPDK (rte_eth_rx_burst): setup may throw, the hot path is inline + noexcept.
#include <liburing.h>
#include <span>
#include <system_error>
class Ring {
public:
    explicit Ring(unsigned entries, unsigned flags = 0) {               // SETUP: may throw — this is not the hot path
        if (int rc = io_uring_queue_init(entries, &ring_, flags); rc < 0) throw std::system_error(-rc, std::generic_category(), "io_uring_queue_init");
    }
    ~Ring() noexcept { if (ring_.ring_fd >= 0) io_uring_queue_exit(&ring_); }
    Ring(const Ring&) = delete; Ring& operator=(const Ring&) = delete;    // one owner (Ch. 31's RAII rule): the SDK handle is not copyable
    Ring(Ring&& o) noexcept : ring_(o.ring_) { o.ring_.ring_fd = -1; }
    // HOT PATH: thin, inline, noexcept — the C call IS the body; std::span replaces (ptr, len) pairs at zero cost
    [[nodiscard]] io_uring_sqe* get_sqe() noexcept { return io_uring_get_sqe(&ring_); }
    int submit() noexcept { return io_uring_submit(&ring_); }
    unsigned peek_batch(std::span<io_uring_cqe*> out) noexcept { return io_uring_peek_batch_cqe(&ring_, out.data(), unsigned(out.size())); }
    void advance(unsigned n) noexcept { io_uring_cq_advance(&ring_, n); }
    io_uring* raw() noexcept { return &ring_; }                        // escape hatch for the SDK call you have not wrapped yet
private:
    io_uring ring_{};
};
```

Three rules make it zero-cost. The hot methods are defined in the class (implicitly `inline`), `noexcept`, and *forward without transforming* — no bounds check the SDK does not do, no logging, no allocation; a `std::span` parameter is two registers, exactly what `(ptr, len)` was. Errors on the hot path stay *codes* (`submit()` returns the SDK's `int`; Ch. 24 §24.4's `std::expected<unsigned, int>` is fine too — it is a struct in registers); exceptions are for setup. And the destructor is the only place the SDK's `_exit`/`_free`/`_destroy` is ever called, so the handle cannot leak and cannot double-free — the Ch. 100 §100.4.2 lifetime bugs, closed by construction.

### 94.4.3 Exceptions and panics at the boundary

What happens when a C++ exception is thrown *through* a C frame — say a vendor SDK's event loop calling back into a C++ handler that throws — depends on how the C was compiled, and the §94.3 test harness shows all three outcomes with one callback and three builds of the C file:

```
   C frame compiled with gcc -O2 (default: -fasynchronous-unwind-tables on x86-64)   -> "caught: bad message"   the unwinder walks the C frame: works TODAY, by accident of the default
   C frame compiled with -fno-asynchronous-unwind-tables -fno-unwind-tables            -> terminate called after throwing an instance of 'std::runtime_error'
   C frame compiled with -fexceptions                                                    -> "caught: bad message"   the C frame is explicitly unwind-safe (no cleanups will run in it either way)
```

The first row is why the bug survives review: it works, until the SDK is rebuilt with different flags, or ships as a binary without tables, or is not C at all. The rule is Ch. 24 §24.6's: **the waist is `noexcept` in both directions.** Exported functions catch everything and return a code (`try { … } catch (...) { return -EIO; }` — the `catch (...)` at the edge is the one place the book permits it); callbacks you register with a C SDK are `noexcept` functions (or `extern "C"` ones) that never throw. For Rust the same boundary is harder-edged, and the harness shows it: a `panic!` inside an `extern "C" fn` prints *"panic in a function that cannot unwind … aborting"* and the process dies (Rust ≥ 1.81); declared `extern "C-unwind"`, the panic *can* propagate through a C frame compiled with `-fexceptions` and be caught by a C++ `catch (...)` — whereupon the Rust runtime aborts with *"Rust panics must be rethrown"* because a foreign exception was swallowed. The spelling that works is `std::panic::catch_unwind` at the Rust side of the waist, converting the panic to an error code, with `panic = "abort"` in the hot-path profile so the question never arises (Ch. 93 §93.4.4).

### 94.4.4 Shared memory across languages

Ch. 67's SPSC ring and Ch. 46's seqlock in a shared segment are read by whatever language maps the segment, so the segment's header is a cross-language struct with atomics in it. Two facts make that safe. **C11 `_Atomic uint64_t` and C++ `std::atomic<uint64_t>` have the same size and alignment (8) and are both always lock-free on x86-64** — asserted on both sides in the harness (`_Static_assert(ATOMIC_LLONG_LOCK_FREE == 2)`, `static_assert(std::atomic<uint64_t>::is_always_lock_free)`), and the padded `{head, 56 B, tail, 56 B}` header is 128 bytes with `tail` at offset 64 in C (`_Alignas`/explicit padding), C++ (`alignas(64)`), Rust (`#[repr(C, align(64))]` / `AtomicU64`), Zig (`extern struct` with `align(64)` fields), Java (`MemorySegment` + `VarHandle` with `acquire`/`release` access modes) and Go (`sync/atomic` on a field of an `unsafe.Pointer`-cast struct). **The memory model is the C++20 one everywhere** (Ch. 41; Java's access modes and Go's `atomic` are sequentially consistent unless told otherwise, which is correct and slower). The seqlock reader protocol (Ch. 46 §46.4) is therefore writeable in any of them, and the Appendix A.1 caveat — plain `mov`s on x86, real barriers on ARM — applies to all of them equally.

### 94.4.5 C and Zig idioms worth stealing

C is the waist's language and Zig is the best C compiler you can download, and each has habits the C++ hot path can borrow (with the C++ spelling the book already uses): **`restrict`** on pointer parameters (`__restrict__` — Ch. 26; `&mut` in Rust — Ch. 93); **designated initialisers** (`{ .type = 'A', .side = 'B' }`, C++20 — Ch. 11's self-documenting wire structs); **`_Generic`** for type-dispatched macros (overloading/templates in C++); **flexible array members** (`struct Batch { uint32_t n; Msg m[]; }` — a trailing `std::span` over the same allocation in C++, Ch. 31); **compound literals** for throwaway POD arguments. From Zig: **`extern struct`/`packed struct`** make the C-vs-packed layout choice part of the type (Ch. 11 §11.4); **explicit allocator parameters** (`init(allocator)`) make Ch. 30's "no hidden allocation" a signature-level fact — the Zig kernel of Ch. 96 allocates its book from a `page_allocator` handed in and nothing else can allocate; **`zig translate-c book_c_api.h`** turns the waist into Zig declarations in one command (`pub const struct_book_t = opaque {}; pub extern fn book_apply_raw(…)`), replacing hand-written externs, and **`zig cc`** is a drop-in Clang that cross-compiles the C side of the waist for any target with `-target aarch64-linux-gnu` (Appendix A).

### 94.4.6 Binding generators and where each belongs

| Pair | Tool | Note |
|---|---|---|
| C header → Rust | `bindgen` (C→Rust), `cbindgen` (Rust→C header), `cxx`/`autocxx` for C++ directly | `cxx` generates a safe bridge with shared structs; still a C-ABI waist underneath |
| C header → Zig | `zig translate-c` / `@cImport` (older Zig) | opaque types, `extern fn` — §94.4.5 |
| C/C++ → Python | nanobind (C++17, smallest overhead), pybind11, `ctypes`/`cffi` for pure-C | §94.3: all three are hundreds of ns per call; batch (Ch. 107 §107.4.6 buffer protocol/NumPy) |
| C → Java | `jextract` (FFM, JDK 22+; preview in 21) ; JNI as legacy | FFM `critical`/`isTrivial` for short callees that touch no Java heap |
| C → Go | `cgo` (header in a comment) | ~50 ns/call: keep it off the per-message path |
| C → OCaml | `external` stubs with `[@@noalloc]`/`[@unboxed]`/`[@untagged]`, or `ctypes-foreign` | §94.3: annotated stubs are in the AOT band |
| C ABI stability | `abidiff` (libabigail), version scripts, `-fvisibility=hidden` | §94.2.4 |

## 94.5 Verify the codegen: the wrapper adds nothing; the boundary adds a PLT hop

`g++ -O2 -std=c++23 -S -masm=intel uring_wrap.cpp` — the wrapper's `get_sqe()` and the raw `io_uring_get_sqe()` called from otherwise identical functions:

```asm
_Z11via_wrapperR4Ring:   ; io_uring_sqe* via_wrapper(Ring& r) { return r.get_sqe(); }     _Z5via_cP8io_uring:  ; io_uring_sqe* via_c(io_uring* r) { return io_uring_get_sqe(r); }
        mov     eax, DWORD PTR 68[rdi]                                                             mov     eax, DWORD PTR 68[rdi]
        mov     ecx, DWORD PTR 192[rdi]                                                            mov     ecx, DWORD PTR 192[rdi]
        lea     edx, 1[rax]                                                                        lea     edx, 1[rax]
        mov     rax, QWORD PTR [rdi]                                                               mov     rax, QWORD PTR [rdi]
        mov     eax, DWORD PTR [rax]                                                               mov     eax, DWORD PTR [rax]
        … (identical: liburing's inline sq.sqe_tail/sq.khead arithmetic, 19 instructions) …       …
```

The two bodies are **instruction-for-instruction identical** — the `Ring&` is the `io_uring*` (the struct is the first and only member, offset 0), `get_sqe()` inlined to nothing, and liburing's own `static inline` body is what both compile to. The batch drain (`peek_batch` + `advance` vs the raw pair) is 15 instructions in both, and `diff` of the two bodies with labels normalised is empty. That is the §94.4.2 claim, proven; it holds for any wrapper that forwards without transforming and whose object is layout-identical to the handle.

The boundary itself, from the §94.3 caller: a call to `book_apply_raw` in `libbook.so` is `call book_apply_raw@PLT` → `jmp QWORD PTR [rip+GOT]` → the function; with `-fno-plt` the call site becomes `call QWORD PTR [rip+GOT]` directly (one fewer jump; 2.58 vs 2.90 ns in §94.3). Inside `libbook.so`, `-fvisibility=hidden` lets GCC inline `decode()` and `Book::apply` into `book_apply_raw`, so the exported function *is* the kernel with a `ret` on the end — the only thing the waist cost was the call. And `noexcept` plus `std::nothrow` on the exported functions leave no landing pads to generate: `readelf -SW libbook.so` shows a 4-byte `.gcc_except_table` (the `noexcept` functions' terminate-on-throw markers, nothing else) and a 376-byte `.eh_frame`, where a throwing `new book_t` caught at the edge would have added cleanup pads and `__cxa_begin_catch` calls for every exported function.

## 94.6 Pitfalls & anti-patterns: per-message crossings and silent ABI drift

- **Calling across a runtime per message.** §94.3's ladder: 10–470 ns *per call*, before the callee does anything. The design that survives is the batch column, or Ch. 67's shared memory with the consumer language polling the ring in its own loop (a Java `MemorySegment` reader of a C++-written seqlock is fine; a Java→C call per tick is not).
- **Callbacks into a managed runtime from the hot thread.** A C SDK invoking a Python or Java callback attaches the thread to the runtime, takes the GIL or performs a safepoint transition, and may trigger a GC *on your hot thread* (Ch. 95). Callbacks at the waist stay in C/C++ and hand the event to the runtime through a queue.
- **Letting the layout drift.** A field added to a struct on one side, a `bool` that is 1 byte in C++ and 4 in an old Fortran-style C header, an `enum` whose size the C compiler chose (`-fshort-enums`), `long` on Windows vs Linux — §94.4.1's asserts on *both* sides, plus `abidiff` in CI, are the whole defence.
- **Passing C++ types through the waist.** A `std::string&`, a `std::vector<Msg>`, a `std::span` — none is C; a `std::span` in particular is *two registers today* and an ABI-unstable library type tomorrow. Pointer and length.
- **Exceptions through C frames; panics through anything.** §94.4.3. Works-by-default is the dangerous outcome.
- **Variadic functions and `errno`.** `printf`-style C APIs promote and pass differently per ABI (FFM needs `Linker.Option.firstVariadicArg`); `errno` is thread-local and may be clobbered by the runtime between the call and your read — capture it in the C shim (`int call_and_errno(…, int* err)`).
- **Ownership across the waist.** Memory allocated by `libbook.so` and freed by the caller's `free()` — or by a Rust `Box::from_raw` — is the Ch. 100 §100.4.2 class with a language boundary for cover. Every `_new` has a `_free`, and the handle type makes a direct `free` a type error.
- **Signals and thread identity.** A C SDK that spawns its own threads (DPDK lcores, `ibv` completion threads) calls you on a thread with none of your TLS initialised and none of your Ch. 56 pinning applied; a Go callback may run on *any* OS thread (`runtime.LockOSThread` is per-goroutine). Pin and initialise in the callback's first invocation, or forbid callbacks.
- **Trusting the default visibility.** Without `-fvisibility=hidden` the library exports its template instantiations; two libraries with the same template produce interposition and ODR surprises at load time (Ch. 27 §27.4.2).

## 94.7 Exercises & checklist

1. **Measure your own ladder.** Build `libbook.so` and the §94.3 callers in the languages your firm runs; add the batch rung for each; find the rung at which per-message calling first exceeds your Ch. 1 budget.
2. **Break the parity assert.** Reorder `Level`'s fields in `kernel.hpp` only; confirm the Rust `const _: () = assert!(…)`, the Python `assert` and the Java static initialiser each fail before any call; then remove the asserts and watch the Ch. 96 digest diverge instead.
3. **Wrap a second SDK.** Write the `Ring`-shaped wrapper for `ef_vi` (Ch. 82) or `libibverbs` (Ch. 83) and prove with `-S` that `poll()` is the raw `ef_vi_receive_poll`/`ibv_poll_cq` body — then add one `if (!ok) throw …` to the hot method and watch the landing pad appear.
4. **Three builds of the C frame.** Reproduce §94.4.3's three outcomes; then make the callback `noexcept` and observe that the default build now terminates *at the callback* — the correct, visible failure.
5. **Shared memory in two languages.** Write Ch. 46's seqlock writer in C++ and the reader in Java (FFM + `VarHandle.getAcquire`) or Rust; assert the header layout on both sides; measure read-side cost against the C++ reader.

Checklist for a boundary:

- [ ] The waist is `extern "C"`, POD-only, opaque handles, errors as codes, every function `noexcept`/nothrow; `-fvisibility=hidden` + explicit exports + a version script.
- [ ] Every crossing struct has size/alignment/offset asserts on **both** sides, and a round-trip digest test.
- [ ] No exception, panic, C++ type, allocation ownership, TLS or `errno` assumption crosses; callbacks are `noexcept` and stay native.
- [ ] Hot-path wrappers are inline + `noexcept` + forwarding; verified in `-S` to be identical to the raw call.
- [ ] Managed callers batch (the loop lives on the native side) or read shared memory; the per-call rung is measured and compared to the budget.
- [ ] `abidiff` in CI; the measurement names toolchains for both sides, flags, CPU, kernel.

## 94.8 References

- *System V Application Binary Interface, AMD64 Architecture Processor Supplement* — registers, stack alignment, struct passing/return, variadic rules (§94.2.3; Ch. 4 §4.2.1).
- *Itanium C++ ABI* — why C++ types and mangled names are not a stable waist; name mangling and exception handling ABI (§94.2.2, §94.4.3).
- GCC manual: *Function Attributes* (`visibility`), *Code Gen Options* (`-fvisibility`, `-fno-plt`, `-fno-semantic-interposition`, `-fexceptions`, `-fasynchronous-unwind-tables`) and Drepper, *How to Write Shared Libraries* — the PLT/GOT, visibility and versioning mechanics (§94.2.4, §94.5).
- JEP 442 (FFM API, third preview, JDK 21) and JEP 454 (final, JDK 22) — `Linker`, `MemorySegment`, `Linker.Option.critical`/`isTrivial`; the JNI specification (`GetPrimitiveArrayCritical`) (§94.3).
- *The Rustonomicon*, "FFI" chapter; Rust RFC 2945 (`extern "C-unwind"`) and the Rust 1.81 release notes (abort on unwind through `extern "C"`) (§94.4.3; Ch. 93).
- The `cgo` command documentation and *"cgo is not Go"* (Cheney) — the stack switch and scheduler interaction behind the 50 ns (§94.3).
- The OCaml manual, Ch. 27 *Interfacing C with OCaml* — `[@@noalloc]`, `[@unboxed]`, `[@untagged]` (§94.3).
- nanobind and pybind11 documentation — call overhead, the buffer protocol/`ndarray`, GIL handling (§94.3; Ch. 107 §107.4.6).
- Zig language reference: `extern struct`, `translate-c`, `zig cc`; C11 standard §8.17 (`<stdatomic.h>`) and the C++20 `[atomics]` clause — layout and lock-freedom guarantees (§94.4.4–§94.4.5).
- libabigail (`abidiff`) documentation — ABI diffing of shared libraries in CI (§94.2.4).

## 94.9 Additional Reading

- Ch. 65, 82, 83 (the SDKs this chapter wraps), Ch. 67 (shared memory as the zero-call alternative), Ch. 24 §24.6 (exceptions across C), Ch. 27 §27.4.2 (visibility and the PLT), Ch. 93 (the Rust side), Ch. 95 (why the managed rungs cost what they cost), Ch. 107 §107.4.6 (the Python research seam built on this waist).
- Appendix B (summary and tier table), Appendix D (`-fvisibility`, `-fno-plt`, unwind-table flags), Appendix I.9 (`nm -D`, `objdump`, `readelf` one-liners for checking exports and tables).
- Hyde & Löwy / Microsoft, *"The Hourglass Interfaces for C++ APIs"* (Stefanus Du Toit, CppCon 2014) — the pattern's name and its rationale.

---

*Next: Ch. 95 — Managed Runtimes Near the Hot Path, Measured: what a garbage collector and a JIT actually do to a feed handler's tail — the hot thread's own view of G1, Parallel, Serial, ZGC and Epsilon pauses, the 12 µs-to-13 ns JIT warm-up curve and its deoptimisation dip, .NET's workstation and server collectors, Go's concurrent GC and OCaml's minor heap — and the garbage-free, pre-warmed techniques that make Java run at the C++ kernel's speed until the first allocation.*
