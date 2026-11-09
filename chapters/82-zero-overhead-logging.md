# Part XIII — Observability & Operations in Production

# Chapter 82 — Zero-Overhead Logging

> **Prerequisites:** Ch. 35 (lock-free SPSC queues — the producer→consumer ring under an async logger), Ch. 38 (Disruptor — the batching pipeline pattern), Ch. 24–25 (no allocation/syscalls on the hot path — logging must obey both), Ch. 17 (TSC timestamps — cheap per-record time), Ch. 4 (reading codegen — proving the log call is tiny), Ch. 1 (tail latency — a logging stall is a tail event).
>
> **Leads into:** Ch. 84 (telemetry — the same capture-here/format-there discipline applied to counters and histograms), Ch. 91 (capture/persistence — binary logging is the same off-hot-path-writer discipline applied to ticks/orders), Ch. 96 (the case study — logging is how you *see* the tick-to-trade path in production). Opens Part XIII — observability and operations: making a low-latency system *visible* without slowing it down.

---

## 82.1 Why it matters: logging must never stall the hot path

Every other chapter of this book worked to shave nanoseconds off the tick-to-trade path. A single careless `log("order sent: {} @ {}", id, price)` on that path can throw all of it away — because the *obvious* way to log does, on the hot path, every single thing this book forbids: it **formats** (parses a format string, converts integers/floats to text — hundreds of ns to microseconds), it may **allocate** (Ch. 24 — `std::string`, stream buffers), it takes a **lock** (Ch. 33 — the logger mutex, contended with other threads), and it does **I/O** (Ch. 52 — a `write()` syscall, or worse a blocking disk/network write that can stall for *milliseconds*). Any one of these is a tail-latency catastrophe (Ch. 1): the median is fine, but the one log call that hit a page fault, a lock convoy, or a flushing disk just produced a p99.9 outlier measured in milliseconds on a path with a microsecond budget.

Yet you *cannot* run a production trading system blind. You need to know what every strategy decided, what every order did, why a position is what it is — for debugging, for compliance, for post-trade analysis (Ch. 91–96). The requirement is absolute on *both* sides: **log everything you need, and spend almost nothing on the hot path doing it.** The resolution is the theme of this chapter and much of Part XIII: **do the cheap part on the hot path, defer the expensive part off it.** The hot path does the minimum to *capture* the event — copy a few raw bytes into a lock-free buffer (Ch. 35) and move on, ideally in a handful of nanoseconds. A *separate* thread, on a *non-isolated* core (Ch. 46), does the expensive part — formatting, I/O — where its cost doesn't matter because it's off the path that trades.

This is "zero-overhead" logging: not *zero* cost (capturing the event costs *something*), but **as close to zero as possible on the hot path**, with all the real work moved to where latency is free. The hot-path log call should compile to little more than "store these few values into the ring buffer and bump a pointer" (§82.5 proves it). This chapter shows the deferred-formatting / binary-logging mental model (§82.2), measures what a naive vs a deferred log call actually costs (§82.3), builds the async/lock-free/binary techniques (§82.4), verifies the hot-path call is tiny in the codegen (§82.5), and warns about the ways logging sneaks back onto the hot path (§82.6).

## 82.2 Mental model: deferred formatting; binary logging

The core idea is **separate capture from formatting**, in time and across threads:

```
   HOT PATH (isolated core, Ch.46)                  BACKGROUND (housekeeping core)
   ───────────────────────────────                  ─────────────────────────────
   log(id, price, qty):                             consumer loop:
     • read TSC (Ch.17)         ~cheap                • drain ring buffer (batched, Ch.38)
     • copy raw bytes to ring   ~few ns               • FORMAT now (integers→text, etc.)
     • bump producer sequence                         • write to disk/socket (Ch.52/53)
     └─ return. NO format, NO alloc,                  └─ all expensive work lives HERE,
        NO lock, NO syscall.                             where latency is free.
            │                                                      ▲
            └──────────────  lock-free SPSC ring (Ch.35)  ─────────┘
```

Two design decisions define a zero-overhead logger:

- **Deferred formatting.** The hot path does *not* turn values into text — it stores the *raw values* (the `id`, the `price`, the `qty`, a TSC timestamp, and an identifier for *which* log statement this is) into the buffer. Formatting — the expensive part — happens later, on the consumer thread, or even **offline** in a separate decoder program that never runs in production at all. Text formatting of a single record is *hundreds of ns to microseconds*; storing a few raw 8-byte values is *a few ns*. Deferring formatting is the single biggest win.

- **Binary logging.** Taken to its conclusion, the hot path writes a compact **binary record** — not text — to the buffer (and ultimately to disk). The record is just: a *format-site ID* (which `LOG(...)` statement fired — a small integer, often captured at compile time), the TSC, and the raw argument bytes. The human-readable string ("order sent: 42 @ 100.25") is reconstructed *offline* by a decoder that has the format strings (extracted from the binary at build time) and pairs each site ID with its template. The production process never builds a string. This is how the fastest loggers (e.g. the NanoLog approach) reach **single-digit-nanosecond** hot-path log calls — the hot path stores arguments; *everything* textual is offline.

The enabling structure underneath is the **lock-free SPSC ring buffer** (Ch. 35) — one producer (the hot thread) writes, one consumer (the background thread) reads, no lock (Ch. 33), no allocation (the ring is pre-allocated once at startup, Ch. 24–25). For multiple hot threads, give **each thread its own ring** (thread-local, single-producer — Ch. 35's SPSC, not a contended MPSC) and let the consumer drain them all; per-thread rings keep the producer side lock-free and false-sharing-free (Ch. 34). The consumer **batches** (Ch. 38's Disruptor mechanical sympathy): drain many records per wakeup, format and write them in bulk, amortizing the I/O.

The mental shift: **a log call is not "produce a log line," it's "enqueue an event."** The line is produced elsewhere, later, by someone whose latency doesn't matter.

## 82.3 Measure it: log-call cost on the hot path

*Toolchain and machine: Appendix L.3.*

Measure the *hot-path cost of the log call itself* — the time the trading thread spends — for three designs, with a microbenchmark (Ch. 3, careful about dead-code elimination and warm-up) timing just the `log(...)` call. Representative numbers (Xeon Gold 6326, GCC 13 `-O2`, single thread; figures pending real runs):

| Logger design | What the hot path does | Hot-path cost per call | Tail behavior |
|---|---|---|---|
| **Naive synchronous** (`fprintf`/`std::ofstream`/`spdlog` default sync) | format + lock + copy into the stdio/streambuf buffer; the `write()` lands on whichever later call fills the buffer (or every call with `fflush`/`std::endl`/`flush_on`) | **~0.2-1 µs** median buffered; **~1-10 µs** when the `write()` fires (every call if flushed per record) | catastrophic — the flush call hits a page fault / flushing disk / lock convoy → **ms** outliers, on an unpredictable call |
| **Async, text formatting on hot path** (format to string, enqueue the string) | format + copy string to queue | **~200-800 ns** | better (no I/O on path) but formatting cost + possible allocation still on path |
| **Async, deferred/binary** (store raw args + site ID to SPSC ring) | copy a few values, bump sequence | **~5-20 ns** (≈7 ns median in NanoLog's measurements) | flat — no format, no alloc, no lock, no syscall; bounded by the ring write |

The story the table tells (read as a *distribution*, Ch. 1):

- **Naive synchronous logging is a tail-latency weapon pointed at your own foot.** Even when the median `write()` is "fast," the call that triggers a page fault (Ch. 24), hits a flushing buffer, or contends the logger lock (Ch. 33) is a *millisecond* outlier on a microsecond path. One such call per million ruins p99.9. This is the thing to never do on the hot path.
- **Moving I/O off the path buys roughly an order of magnitude at the median; deferring the *formatting* too buys another ~10-100×.** Row 2 (async, still formatting) removes the `write()` but leaves hundreds of ns and a possible allocation on the path; row 3 (binary records) removes those as well and — critically — makes the cost *flat* (no allocation, no syscall, nothing that can stall). The hot path's cost is just the ring-buffer write (a few stores + a sequence update, Ch. 35), which §82.5 confirms in the asm.
- **The async-but-still-formatting middle row is the common half-measure.** Many teams move I/O off the hot path (good) but still format the string *on* the hot path before enqueuing it (e.g. enqueue a `std::string`) — keeping hundreds of ns and a possible allocation on the path. The full win requires deferring *formatting*, not just I/O.

Also measure the **consumer side** and the **back-pressure behavior** (§82.6): what happens when the hot path produces faster than the consumer drains? The ring fills; your policy (drop, overwrite-oldest, or — never — *block the producer*) is a correctness/observability decision measured under burst load (Ch. 58 microbursts), not an afterthought.

## 82.4 Techniques

### 82.4.1 Async/lock-free loggers

The foundation: a **lock-free SPSC ring per producer thread** (Ch. 35), drained by a dedicated consumer thread pinned to a *housekeeping* core (Ch. 46), never an isolated hot core.

- **Surviving the crash.** The records still sitting in the rings when the process dies (SIGSEGV, `abort`, a kill) are the most valuable of all — the last microseconds before the fault — and a heap-allocated ring dies with the process. Either drain the rings from a crash handler (the NanoLog/Quill approach: a `SIGSEGV`/`SIGABRT` handler that flushes what it can before re-raising — fragile, since the handler runs on a corrupted process) or, better, place the rings in a **shared-memory segment** (`memfd_create`/`shm_open` — Ch. 55) that the consumer thread *or a separate process* maps: the records survive the producer's death and a hot restart (Ch. 87), and the reader is out of the blast radius.
- **Per-thread single-producer rings.** Each hot thread owns its ring (thread-local). Single-producer means the producer side is a plain sequenced write — no CAS contention, no lock (Ch. 33, 35). The consumer reads all rings round-robin. This sidesteps MPSC contention and false sharing (Ch. 34 — pad each ring's head/tail to a cache line).
- **Pre-allocated, fixed-capacity.** The ring is allocated once at startup (Ch. 24–25) — never on the hot path. Fixed capacity forces an explicit full-buffer policy (§82.6) instead of an unbounded allocation that page-faults mid-trade.
- **Consumer batching (Disruptor sympathy, Ch. 38).** The consumer drains as many records as are available per wakeup, formats and writes them in bulk, then sleeps/polls. Batching amortizes formatting and especially I/O (one `write()` for many records, Ch. 52; or io_uring, Ch. 53). Poll with a backoff (Ch. 33) or a brief sleep — the consumer's latency doesn't matter, only that it keeps up on average.
- **Cheap timestamps.** Stamp each record with `rdtsc`/`rdtscp` (Ch. 17), not `clock_gettime` — tens of cycles, no syscall, converted to wall-clock offline using the calibration (Ch. 17). The timestamp is part of the captured event, applied on the hot path, formatted later.

### 82.4.2 Binary logging with offline formatting

Push deferral to its limit: the hot path writes a **binary record**, and *no* text is produced in production.

- **Compile-time format-site IDs.** Each `LOG("order sent: {} @ {}", id, price)` is assigned a small integer ID at compile time (via a macro + a static registration, or a `consteval` mechanism — Ch. 18), and the site's file/line/function come from a defaulted `std::source_location::current()` parameter — the C++20 default, one `lea` of a static per call site and correct inside templates and lambdas (Ch. 21 §21.4.4 measures it); the `__FILE__`/`__LINE__` macro is the C++17 fallback. The format string and the argument *types* are recorded in a side table extracted from the binary at build time. The hot path stores only `{site_id, tsc, id, price}` — raw bytes, no string.
- **Type-safe argument capture.** A variadic template (Ch. 19) captures the arguments by value into the record, computing the record size at compile time (`constexpr` — Ch. 18). The hot path memcpy's the packed arguments into the ring — no per-argument formatting, no `std::string`, no `std::format` *at runtime*.
- **Offline decoder.** A separate program reads the binary log + the format-site table and reconstructs human-readable lines on demand — for analysis, compliance, debugging. Because the decoder runs offline, formatting cost is *completely* free; the production process never paid it. This is the NanoLog model and the route to single-digit-ns (median) log calls.
- **The hot-path half, in code.** Everything above in forty lines — the record, the reserve, the raw capture, the publish — is what §82.5 disassembles:

```cpp
// binlog.cpp — the hot-path half of a deferred/binary logger: fixed record, SPSC reserve, raw-argument capture.
// Build: g++ -O2 -std=c++23 -march=icelake-server -fno-stack-protector -c binlog.cpp   (GCC 13/14; Clang 18 same shape;
//        without -fno-stack-protector Ubuntu's default adds a canary load/compare around the rdtscp out-parameter — Ch. 85)
#include <atomic>
#include <cstdint>
#include <cstring>
#include <new>
#include <x86intrin.h>

struct alignas(64) Ring {                                    // one per producer thread (§82.4.1)
    static constexpr std::size_t N = 1 << 16;               // bytes, power of two
    alignas(64) /* hardware_destructive_interference_size; Clang 18 lacks the constant */ std::atomic<std::uint64_t> head{0};   // producer-owned
    alignas(64) /* hardware_destructive_interference_size; Clang 18 lacks the constant */ std::atomic<std::uint64_t> tail{0};   // consumer-owned
    alignas(64) std::uint8_t buf[N];
};
inline thread_local Ring* tls_ring;                          // set up at thread start, off the hot path

struct Hdr { std::uint32_t site_id; std::uint32_t len; std::uint64_t tsc; };   // 16 bytes, then the raw args

template <class... Args>
[[gnu::always_inline]] inline bool log_record(std::uint32_t site_id, const Args&... args) {
    static_assert((std::is_trivially_copyable_v<Args> && ...), "capture by raw bytes only");
    constexpr std::uint32_t len = sizeof(Hdr) + (0 + ... + sizeof(Args));   // compile-time record size
    Ring& r = *tls_ring;
    const std::uint64_t h = r.head.load(std::memory_order_relaxed);
    if (h - r.tail.load(std::memory_order_acquire) > Ring::N - len) [[unlikely]] return false;   // full: drop + count (§82.6)
    std::uint8_t* p = r.buf + (h & (Ring::N - 1));          // (records never straddle the end: N is a multiple of every len)
    unsigned aux;
    Hdr hd{site_id, len, __rdtscp(&aux)};                   // the TSC, not clock_gettime (Ch. 17)
    std::memcpy(p, &hd, sizeof hd); p += sizeof hd;
    ((std::memcpy(p, &args, sizeof args), p += sizeof args), ...);   // raw bytes, no formatting
    r.head.store(h + len, std::memory_order_release);        // publish (Ch. 31)
    return true;
}
// LOG(): the site id is __COUNTER__ at compile time; the format string + arg types go to a side table the
// build extracts (a static registration keyed by the id) — the decoder (§82.4.2) uses it offline.
#define LOG(fmt, ...) log_record(__COUNTER__, __VA_ARGS__)

void on_fill(std::uint64_t order_id, std::int64_t price_ticks, std::uint32_t qty) {
    LOG("fill: order {} @ {} x {}", order_id, price_ticks, qty);
}
```

- **Compact on disk (ties Ch. 91).** Binary records are *small* (no text bloat) — cheaper to write, cheaper to store, cheaper to replay. The same binary-record discipline is exactly what Ch. 91 uses to journal every tick and order for deterministic replay; a zero-overhead logger and a capture journal are the same machine pointed at different events.

### 82.4.3 `std::print`/`std::println` for off-hot-path output

Not everything is hot-path. **Setup/teardown, control-plane, periodic status, and the consumer thread's own output** are *off* the hot path (Ch. 1's steady-state-vs-setup distinction) — and there, clarity beats nanoseconds.

- **Use `std::print`/`std::println` (C++23, `<print>`) for off-hot-path output.** It's type-safe (no `printf` format/argument mismatch — a format-string bug class closed at compile time), leaner than iostreams (no locale machinery or sentry per call) and far more readable than stream insertion; libstdc++ 14 / libc++ 18 — Appendix H. For startup banners, config dumps, periodic stats, and the *consumer thread's* formatting-and-writing step, it's the right tool. (Fallback: `fmt::print` for pre-C++23 toolchains; `std::format` to a buffer + one `write`.)
- **Keep it strictly off the hot path.** `std::print` *formats* — it does exactly the expensive work the hot path must avoid. It belongs on the consumer thread and in setup code, *never* in a `LOG` on the trading path. The rule: the hot path *captures* (binary, deferred); everything that *formats* — including `std::print` — runs off the hot path. Naming the two output channels differently in the codebase (`LOG(...)` = hot-path binary capture; `std::println(...)` = off-path human output) keeps the distinction honest.

## 82.5 Verify the codegen: the hot-path log call is a few stores

The whole claim is "the hot-path `LOG` compiles to little more than a ring-buffer write." Prove it (Ch. 4) — the §82.4.2 listing's `on_fill`, GCC 13.3 `-O2 -std=c++23 -march=icelake-server -fno-stack-protector`, `objdump -d -M intel` (Clang 18 produces the same shape):

```asm
on_fill(unsigned long, long, unsigned int):
        mov     r8, QWORD PTR fs:0                  ; tls_ring
        mov     r11, rsi                            ; price_ticks
        mov     r10d, edx                           ; qty
        mov     rsi, QWORD PTR [r8]                 ; head (relaxed load)
        mov     rdx, QWORD PTR [r8+64]              ; tail (acquire load — a plain mov on x86-TSO, Ch. 31)
        mov     rax, rsi
        sub     rax, rdx
        cmp     rax, 0xffdc                         ; N - len, an immediate: the record size is compile-time
        ja      .Lfull                              ; the only branch — predicted not-taken
        push    rbx
        movzx   eax, si                             ; h & (N-1)
        mov     rbx, QWORD PTR [rip+site_hdr]       ; {site_id, len} — a 64-bit constant
        vmovq   xmm0, rdi                           ; order_id
        lea     r9, [r8+rax+128]                    ; &buf[h & (N-1)]
        rdtscp                                      ; TSC -> EDX:EAX, TSC_AUX -> ECX
        shl     rdx, 32
        mov     DWORD PTR [rsp-4], ecx              ; the aux out-parameter (dead store)
        vpinsrq xmm0, xmm0, r11, 1                  ; {order_id, price_ticks} packed into one 16-byte store
        add     rsi, 36                             ; h + len
        or      rax, rdx                            ; the full 64-bit TSC (rdtscp splits it: RAX alone is only the low half)
        mov     QWORD PTR [r9], rbx                 ; hdr.site_id, hdr.len
        mov     QWORD PTR [r9+8], rax               ; hdr.tsc
        mov     DWORD PTR [r9+32], r10d             ; qty
        vmovdqu XMMWORD PTR [r9+16], xmm0           ; order_id, price_ticks
        mov     QWORD PTR [r8], rsi                 ; publish: head.store(release) — a plain mov on x86 (Ch. 31)
        pop     rbx
.Lfull: ret
```

No call to a formatting routine, no `std::string` constructor, no `malloc`, no `write`. Twenty-six instructions, one predictable branch, five stores and a release — exactly the Ch. 35 ring-buffer producer, plus the argument stores. (Two things worth noticing: `rdtscp` returns the TSC split across EDX:EAX, so the `shl`/`or` pair is the compiler assembling the 64-bit stamp — a hand-written "`mov [rbx+8], rax`" would store only the low 32 bits; and with Ubuntu's default `-fstack-protector-strong` the `aux` out-parameter makes GCC add a canary load and compare around the call — the Ch. 85 hardening-vs-latency trade in miniature.) What to confirm in the codegen:

- **No `call` to format/allocate.** If you see a `call` to `std::__cxx11::basic_string`, `operator new`, `std::format`, `vfprintf`, or `write` on the hot-path side, formatting or I/O leaked onto the path — the deferral failed. The hot-path side must be call-free (or call only an inlined, allocation-free reserve).
- **The site ID is an immediate.** The format string's identity is resolved at *compile time* (Ch. 18) — it appears as a constant in the instruction stream, not a runtime string lookup.
- **Arguments are stored raw.** `id` and `price` move straight from their registers into the ring; no per-argument conversion. The record size is a compile-time constant (the reservation is `add` of an immediate).
- **Compare against the naive version.** Put a `fprintf`/`std::ofstream <<` call beside it: the naive version's asm is dominated by `call`s into formatting and I/O. The contrast *is* the lesson — and it's why you read the codegen rather than trust the API name.

If the asm shows the few-stores-and-publish shape, the measurement of §82.3 (single-digit ns at the median) follows; if it shows calls, the logger is not zero-overhead no matter what its README claims.

## 82.6 Pitfalls & anti-patterns: formatting in the hot path; blocking on I/O

- **Formatting on the hot path.** The cardinal sin — building the string (`std::format`, `fmt::format`, `sprintf`, stream insertion, or even `std::to_string`) *on the trading thread*. Hundreds of ns and a possible allocation, every call. Defer formatting (§82.4.2); the hot path stores raw values only.
- **Blocking on I/O.** A synchronous `write()`/`fwrite`/`flush` on the hot path can stall for *milliseconds* on a page fault, a full pipe, a flushing disk, or a slow network sink — a Ch. 1 tail catastrophe. *All* I/O lives on the consumer thread (§82.4.1), off the isolated core (Ch. 46).
- **Allocating on the hot path.** Enqueuing a `std::string`, growing a buffer, or any `new` on the path reintroduces page faults and allocator latency (Ch. 24–25). The ring is pre-allocated and fixed-capacity; records are fixed/bounded size.
- **A contended (locked) logger.** A single global logger behind a mutex (Ch. 33) serializes all threads and produces lock convoys — the one log call that loses the lock race is your p99.9. Use per-thread single-producer rings (Ch. 35), no shared lock.
- **Ignoring back-pressure / full-buffer policy.** When the hot path out-produces the consumer (a microburst, Ch. 58), the ring fills. **Never block the producer** (that puts the consumer's slowness on the hot path — the exact inversion you're avoiding). Choose explicitly: *drop* newest (and count drops), or *overwrite* oldest (lossy but bounded). Overwrite-oldest is not a free variant of the SPSC ring: the oldest slot may be the one the consumer is reading, so it needs per-slot sequence numbers for the consumer to detect and discard a slot overwritten under it (the Disruptor's scheme — Ch. 35, 38); with a plain head/tail ring, drop-newest-and-count is the only race-free policy. Measure it under burst (§82.3). Silent blocking-on-full is a hidden hot-path stall.
- **`clock_gettime` per record.** With the `tsc` clocksource it is a vDSO call, not a syscall (≈18 ns — Ch. 17; a real syscall only when the clocksource is not vDSO-capable, e.g. `hpet` or some VMs) — but that is still ~3× a raw `rdtscp` plus a conversion per record. Store the raw TSC and convert offline (Ch. 17).
- **Logging too much / dynamic log levels checked expensively.** Even a few-ns call, made millions of times, adds up; and a runtime log-level check that *isn't* trivially predicted (Ch. 13) costs on every call. Make the level check a single predictable branch (often compiled out for disabled levels, Ch. 18), and be deliberate about hot-path log volume.
- **False sharing between producer and consumer.** The ring's head and tail indices on the same cache line ping-pong between the producing and consuming cores (Ch. 34). Pad them to separate cache lines (`hardware_destructive_interference_size`).
- **Trusting the API name over the codegen.** A logger advertised as "async" or "fast" may still format on the calling thread (the §82.3 middle row). Verify in the asm (§82.5) that the hot-path side is call-free — don't trust the label.

## 82.7 Exercises & checklist

**Exercises:**

1. **Three-way cost measurement.** Benchmark (Ch. 3) the hot-path cost of: (a) `fprintf` to a file, (b) async-but-format-on-path (enqueue a `std::string`), (c) deferred/binary (enqueue raw args). Reproduce the §82.3 spread and, crucially, the *tail* — induce a page fault / disk flush during (a) and capture the millisecond outlier with HdrHistogram.
2. **Build a minimal binary logger.** Implement a `LOG` macro that captures a compile-time site ID + `rdtscp` + raw args into a per-thread SPSC ring (Ch. 35), and an offline decoder that reconstructs the text. Verify in Godbolt (§82.5) that the hot-path call is a few stores with no `call` to formatting/allocation.
3. **Codegen contrast.** Put your binary `LOG` and an `std::ofstream <<` line side by side in Compiler Explorer (Ch. 4). Confirm the binary version has no format/alloc/`write` calls and the stream version is all calls. This contrast is the chapter.
4. **Back-pressure under burst.** Drive the logger with a microburst (Ch. 58) that out-produces the consumer. Implement *drop-and-count* vs *overwrite-oldest* and measure the hot-path cost stays flat under both — and verify neither ever blocks the producer.
5. **Consumer batching.** Measure consumer throughput with per-record vs batched formatting+I/O (Ch. 38, Ch. 53 io_uring). Show batching lets the consumer keep up with a producer it couldn't match one-at-a-time.

**Checklist:**

- [ ] The hot-path log call does **no formatting, no allocation, no lock, no syscall** — it stores raw values (+ site ID + TSC) to a pre-allocated lock-free ring and returns (§82.2, §82.4).
- [ ] Verified in the **codegen** (§82.5) that the hot-path side is **call-free** (no `std::string`/`new`/`format`/`write`) — not trusted from the API name.
- [ ] Formatting and **all I/O** run on a dedicated **consumer thread** pinned to a **housekeeping** core (Ch. 46), batched (Ch. 38), never on an isolated hot core (§82.4.1).
- [ ] Each hot thread has its **own single-producer ring** (Ch. 35); head/tail are **padded** against false sharing (Ch. 34); no shared logger mutex (§82.6).
- [ ] Timestamps use **`rdtscp`** (Ch. 17), converted offline — no `clock_gettime` on the path (§82.6).
- [ ] The **full-buffer policy** is explicit (drop-and-count or overwrite-oldest) and **never blocks the producer**; measured under burst (§82.3, §82.6).
- [ ] Off-hot-path output (setup, status, the consumer's own writes) uses **`std::print`/`std::println`** (C++23) for type-safe, readable formatting — kept strictly off the hot path (§82.4.3).

## 82.8 References

- The **NanoLog** papers and implementation (Stanford) — nanosecond-scale logging via compile-time format extraction and binary records; the canonical deferred/binary-logging design (§82.2, §82.4.2).
- The **LMAX Disruptor** paper (Ch. 38 references) — the batched single-producer/consumer ring that underlies a fast async logger (§82.4.1).
- `std::print`/`std::println` and `<print>` (C++23) and the `{fmt}` library documentation — type-safe off-hot-path formatting (§82.4.3).
- Production low-latency logger implementations and talks (Quill, the binary-logging approaches used in HFT) — real-world async/deferred designs (§82.4).
- Ch. 35 (lock-free SPSC ring), Ch. 38 (Disruptor batching), Ch. 17 (TSC timestamps), Ch. 24–25 (no alloc/syscall on the path) — the pieces this chapter assembles.

## 82.9 Additional Reading

- CppCon talks on low-latency logging and "logging without slowing down" — the deferral and binary-record patterns in practice.
- The NanoLog and Quill design write-ups — compile-time site IDs, type-safe argument capture, offline decoding.
- Ch. 91 (*Capture/Persistence*) — the same binary-record, off-hot-path-writer discipline applied to journaling ticks/orders for replay; Ch. 96 (*Case Study*) — logging as production observability of the tick-to-trade path; Ch. 53 (*io_uring*) — getting the consumer's batched writes to disk efficiently; Ch. 85 (*Secure Programming*) — keeping secrets out of logs (§85.4.5).
- **Appendix E** — the cost of a syscall / page fault (why I/O can't be on the path); **Appendix F** — SPSC/Disruptor glossary.

---

*Next: Ch. 83 — In-Band Latency Tracing & Budget Attribution, where the binary record this chapter writes gains a passenger: a fixed trace header — the venue sequence number or `ClOrdID` as correlation ID plus a vector of raw-TSC stage stamps — carried in every ring slot across the feed handler, strategy and gateway processes, so the tick-to-trade budget can be attributed per stage, per message, in production, and then split between you, the wire and the matching engine using the timestamps the venue itself puts on the wire.*
