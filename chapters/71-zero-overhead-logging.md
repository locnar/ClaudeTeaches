# Part XII — Observability & Operations in Production

# Chapter 71 — Zero-Overhead Logging

> **Prerequisites:** Ch. 34 (lock-free SPSC queues — the producer→consumer ring under an async logger), Ch. 37 (Disruptor — the batching pipeline pattern), Ch. 23–24 (no allocation/syscalls on the hot path — logging must obey both), Ch. 17 (TSC timestamps — cheap per-record time), Ch. 4 (reading codegen — proving the log call is tiny), Ch. 1 (tail latency — a logging stall is a tail event).
>
> **Leads into:** Ch. 75 (capture/persistence — binary logging is the same off-hot-path-writer discipline applied to ticks/orders), Ch. 76 (the case study — logging is how you *see* the tick-to-trade path in production). Opens Part XII — observability and operations: making a low-latency system *visible* without slowing it down.

---

## 71.1 Why it matters: logging must never stall the hot path

Every other chapter of this book worked to shave nanoseconds off the tick-to-trade path. A single careless `log("order sent: {} @ {}", id, price)` on that path can throw all of it away — because the *obvious* way to log does, on the hot path, every single thing this book forbids: it **formats** (parses a format string, converts integers/floats to text — hundreds of ns to microseconds), it may **allocate** (Ch. 23 — `std::string`, stream buffers), it takes a **lock** (Ch. 32 — the logger mutex, contended with other threads), and it does **I/O** (Ch. 47 — a `write()` syscall, or worse a blocking disk/network write that can stall for *milliseconds*). Any one of these is a tail-latency catastrophe (Ch. 1): the median is fine, but the one log call that hit a page fault, a lock convoy, or a flushing disk just produced a p99.9 outlier measured in milliseconds on a path with a microsecond budget.

Yet you *cannot* run a production trading system blind. You need to know what every strategy decided, what every order did, why a position is what it is — for debugging, for compliance, for post-trade analysis (Ch. 75–76). The requirement is absolute on *both* sides: **log everything you need, and spend almost nothing on the hot path doing it.** The resolution is the theme of this chapter and much of Part XII: **do the cheap part on the hot path, defer the expensive part off it.** The hot path does the minimum to *capture* the event — copy a few raw bytes into a lock-free buffer (Ch. 34) and move on, ideally in a handful of nanoseconds. A *separate* thread, on a *non-isolated* core (Ch. 42), does the expensive part — formatting, I/O — where its cost doesn't matter because it's off the path that trades.

This is "zero-overhead" logging: not *zero* cost (capturing the event costs *something*), but **as close to zero as possible on the hot path**, with all the real work moved to where latency is free. The hot-path log call should compile to little more than "store these few values into the ring buffer and bump a pointer" (§71.5 proves it). This chapter shows the deferred-formatting / binary-logging mental model (§71.2), measures what a naive vs a deferred log call actually costs (§71.3), builds the async/lock-free/binary techniques (§71.4), verifies the hot-path call is tiny in the codegen (§71.5), and warns about the ways logging sneaks back onto the hot path (§71.6).

## 71.2 Mental model: deferred formatting; binary logging

The core idea is **separate capture from formatting**, in time and across threads:

```
   HOT PATH (isolated core, Ch.40)                  BACKGROUND (housekeeping core)
   ───────────────────────────────                  ─────────────────────────────
   log(id, price, qty):                             consumer loop:
     • read TSC (Ch.16)         ~cheap                • drain ring buffer (batched, Ch.36)
     • copy raw bytes to ring   ~few ns               • FORMAT now (integers→text, etc.)
     • bump producer sequence                         • write to disk/socket (Ch.44/45)
     └─ return. NO format, NO alloc,                  └─ all expensive work lives HERE,
        NO lock, NO syscall.                             where latency is free.
            │                                                      ▲
            └──────────────  lock-free SPSC ring (Ch.33)  ─────────┘
```

Two design decisions define a zero-overhead logger:

- **Deferred formatting.** The hot path does *not* turn values into text — it stores the *raw values* (the `id`, the `price`, the `qty`, a TSC timestamp, and an identifier for *which* log statement this is) into the buffer. Formatting — the expensive part — happens later, on the consumer thread, or even **offline** in a separate decoder program that never runs in production at all. Text formatting of a single record is *hundreds of ns to microseconds*; storing a few raw 8-byte values is *a few ns*. Deferring formatting is the single biggest win.

- **Binary logging.** Taken to its conclusion, the hot path writes a compact **binary record** — not text — to the buffer (and ultimately to disk). The record is just: a *format-site ID* (which `LOG(...)` statement fired — a small integer, often captured at compile time), the TSC, and the raw argument bytes. The human-readable string ("order sent: 42 @ 100.25") is reconstructed *offline* by a decoder that has the format strings (extracted from the binary at build time) and pairs each site ID with its template. The production process never builds a string. This is how the fastest loggers (e.g. the NanoLog approach) reach **single-digit-nanosecond** hot-path log calls — the hot path stores arguments; *everything* textual is offline.

The enabling structure underneath is the **lock-free SPSC ring buffer** (Ch. 34) — one producer (the hot thread) writes, one consumer (the background thread) reads, no lock (Ch. 32), no allocation (the ring is pre-allocated once at startup, Ch. 23–24). For multiple hot threads, give **each thread its own ring** (thread-local, single-producer — Ch. 34's SPSC, not a contended MPSC) and let the consumer drain them all; per-thread rings keep the producer side lock-free and false-sharing-free (Ch. 33). The consumer **batches** (Ch. 37's Disruptor mechanical sympathy): drain many records per wakeup, format and write them in bulk, amortizing the I/O.

The mental shift: **a log call is not "produce a log line," it's "enqueue an event."** The line is produced elsewhere, later, by someone whose latency doesn't matter.

## 71.3 Measure it: log-call cost on the hot path

Measure the *hot-path cost of the log call itself* — the time the trading thread spends — for three designs, with a microbenchmark (Ch. 3, careful about dead-code elimination and warm-up) timing just the `log(...)` call. Representative numbers (Xeon Gold 6326, GCC 13 `-O2`, single thread; figures pending real runs):

| Logger design | What the hot path does | Hot-path cost per call | Tail behavior |
|---|---|---|---|
| **Naive synchronous** (`fprintf`/`std::ofstream`/`spdlog` default sync) | format + lock + `write()` | **~1-10 µs** (and *much* worse on a syscall/disk stall) | catastrophic — page fault / flushing disk / lock convoy → **ms** outliers |
| **Async, text formatting on hot path** (format to string, enqueue the string) | format + copy string to queue | **~200-800 ns** | better (no I/O on path) but formatting cost + possible allocation still on path |
| **Async, deferred/binary** (store raw args + site ID to SPSC ring) | copy a few values, bump sequence | **~5-20 ns** | flat — no format, no alloc, no lock, no syscall; bounded by the ring write |

The story the table tells (read as a *distribution*, Ch. 1):

- **Naive synchronous logging is a tail-latency weapon pointed at your own foot.** Even when the median `write()` is "fast," the call that triggers a page fault (Ch. 23), hits a flushing buffer, or contends the logger lock (Ch. 32) is a *millisecond* outlier on a microsecond path. One such call per million ruins p99.9. This is the thing to never do on the hot path.
- **Deferring formatting buys ~10-40×; deferring it *and* going binary buys ~100×+** and — critically — makes the cost *flat* (no allocation, no syscall, nothing that can stall). The hot path's cost is just the ring-buffer write (a few stores + a sequence update, Ch. 34), which §71.5 confirms in the asm.
- **The async-but-still-formatting middle row is the common half-measure.** Many teams move I/O off the hot path (good) but still format the string *on* the hot path before enqueuing it (e.g. enqueue a `std::string`) — keeping hundreds of ns and a possible allocation on the path. The full win requires deferring *formatting*, not just I/O.

Also measure the **consumer side** and the **back-pressure behavior** (§71.6): what happens when the hot path produces faster than the consumer drains? The ring fills; your policy (drop, overwrite-oldest, or — never — *block the producer*) is a correctness/observability decision measured under burst load (Ch. 53 microbursts), not an afterthought.

## 71.4 Techniques

### 71.4.1 Async/lock-free loggers

The foundation: a **lock-free SPSC ring per producer thread** (Ch. 34), drained by a dedicated consumer thread pinned to a *housekeeping* core (Ch. 42), never an isolated hot core.

- **Per-thread single-producer rings.** Each hot thread owns its ring (thread-local). Single-producer means the producer side is a plain sequenced write — no CAS contention, no lock (Ch. 32, 34). The consumer reads all rings round-robin. This sidesteps MPSC contention and false sharing (Ch. 33 — pad each ring's head/tail to a cache line).
- **Pre-allocated, fixed-capacity.** The ring is allocated once at startup (Ch. 23–24) — never on the hot path. Fixed capacity forces an explicit full-buffer policy (§71.6) instead of an unbounded allocation that page-faults mid-trade.
- **Consumer batching (Disruptor sympathy, Ch. 37).** The consumer drains as many records as are available per wakeup, formats and writes them in bulk, then sleeps/polls. Batching amortizes formatting and especially I/O (one `write()` for many records, Ch. 47; or io_uring, Ch. 48). Poll with a backoff (Ch. 32) or a brief sleep — the consumer's latency doesn't matter, only that it keeps up on average.
- **Cheap timestamps.** Stamp each record with `rdtsc`/`rdtscp` (Ch. 17), not `clock_gettime` — tens of cycles, no syscall, converted to wall-clock offline using the calibration (Ch. 17). The timestamp is part of the captured event, applied on the hot path, formatted later.

### 71.4.2 Binary logging with off-line formatting

Push deferral to its limit: the hot path writes a **binary record**, and *no* text is produced in production.

- **Compile-time format-site IDs.** Each `LOG("order sent: {} @ {}", id, price)` is assigned a small integer ID at compile time (via a macro + a static registration, or a `consteval` mechanism — Ch. 18). The format string and the argument *types* are recorded in a side table extracted from the binary at build time. The hot path stores only `{site_id, tsc, id, price}` — raw bytes, no string.
- **Type-safe argument capture.** A variadic template (Ch. 19) captures the arguments by value into the record, computing the record size at compile time (`constexpr` — Ch. 18). The hot path memcpy's the packed arguments into the ring — no per-argument formatting, no `std::string`, no `std::format` *at runtime*.
- **Offline decoder.** A separate program reads the binary log + the format-site table and reconstructs human-readable lines on demand — for analysis, compliance, debugging. Because the decoder runs offline, formatting cost is *completely* free; the production process never paid it. This is the NanoLog model and the route to single-digit-ns log calls.
- **Compact on disk (ties Ch. 75).** Binary records are *small* (no text bloat) — cheaper to write, cheaper to store, cheaper to replay. The same binary-record discipline is exactly what Ch. 75 uses to journal every tick and order for deterministic replay; a zero-overhead logger and a capture journal are the same machine pointed at different events.

### 71.4.3 `std::print`/`std::println` for off-hot-path output

Not everything is hot-path. **Setup/teardown, control-plane, periodic status, and the consumer thread's own output** are *off* the hot path (Ch. 1's steady-state-vs-setup distinction) — and there, clarity beats nanoseconds.

- **Use `std::print`/`std::println` (C++23, `<print>`) for off-hot-path output.** It's type-safe (no `printf` format/argument mismatch — a Ch. 72 safety win), faster and leaner than iostreams, and far more readable than stream insertion. For startup banners, config dumps, periodic stats, and the *consumer thread's* formatting-and-writing step, it's the right tool. (Fallback: `fmt::print` for pre-C++23 toolchains; `std::format` to a buffer + one `write`.)
- **Keep it strictly off the hot path.** `std::print` *formats* — it does exactly the expensive work the hot path must avoid. It belongs on the consumer thread and in setup code, *never* in a `LOG` on the trading path. The rule: the hot path *captures* (binary, deferred); everything that *formats* — including `std::print` — runs off the hot path. Naming the two output channels differently in the codebase (`LOG(...)` = hot-path binary capture; `std::println(...)` = off-path human output) keeps the distinction honest.

## 71.5 Verify the codegen: the hot-path log call is a few stores

The whole claim is "the hot-path `LOG` compiles to little more than a ring-buffer write." Prove it (Ch. 4) — put the logger in Godbolt and read the asm for a hot-path `LOG("...: {} @ {}", id, price)`. A correct deferred/binary logger compiles the *hot-path* call to:

```asm
   ; reserve N bytes in the SPSC ring (load producer head, no lock, no branch to a slow path in the common case)
   ; store the compile-time site_id  (an immediate)         mov   [rbx], <site_id_imm>
   ; store the TSC                                            rdtscp / mov [rbx+8], rax
   ; store the raw args (id, price) — a few moves            mov   [rbx+16], rdi
   ;                                                          mov   [rbx+24], rsi
   ; publish: bump the producer sequence (one release store) mov   [head], rcx   ; (release, Ch.29)
```

No call to a formatting routine, no `std::string` constructor, no `malloc`, no `write`. A handful of stores and a sequence update — exactly the Ch. 34 ring-buffer producer, plus a few argument stores. What to confirm in the codegen:

- **No `call` to format/allocate.** If you see a `call` to `std::__cxx11::basic_string`, `operator new`, `std::format`, `vfprintf`, or `write` on the hot-path side, formatting or I/O leaked onto the path — the deferral failed. The hot-path side must be call-free (or call only an inlined, allocation-free reserve).
- **The site ID is an immediate.** The format string's identity is resolved at *compile time* (Ch. 18) — it appears as a constant in the instruction stream, not a runtime string lookup.
- **Arguments are stored raw.** `id` and `price` move straight from their registers into the ring; no per-argument conversion. The record size is a compile-time constant (the reservation is `add` of an immediate).
- **Compare against the naive version.** Put a `fprintf`/`std::ofstream <<` call beside it: the naive version's asm is dominated by `call`s into formatting and I/O. The contrast *is* the lesson — and it's why you read the codegen rather than trust the API name.

If the asm shows the few-stores-and-publish shape, the measurement of §71.3 (single-digit ns) follows; if it shows calls, the logger is not zero-overhead no matter what its README claims.

## 71.6 Pitfalls & anti-patterns: formatting in the hot path; blocking on I/O

- **Formatting on the hot path.** The cardinal sin — building the string (`std::format`, `fmt::format`, `sprintf`, stream insertion, or even `std::to_string`) *on the trading thread*. Hundreds of ns and a possible allocation, every call. Defer formatting (§71.4.2); the hot path stores raw values only.
- **Blocking on I/O.** A synchronous `write()`/`fwrite`/`flush` on the hot path can stall for *milliseconds* on a page fault, a full pipe, a flushing disk, or a slow network sink — a Ch. 1 tail catastrophe. *All* I/O lives on the consumer thread (§71.4.1), off the isolated core (Ch. 42).
- **Allocating on the hot path.** Enqueuing a `std::string`, growing a buffer, or any `new` on the path reintroduces page faults and allocator latency (Ch. 23–24). The ring is pre-allocated and fixed-capacity; records are fixed/bounded size.
- **A contended (locked) logger.** A single global logger behind a mutex (Ch. 32) serializes all threads and produces lock convoys — the one log call that loses the lock race is your p99.9. Use per-thread single-producer rings (Ch. 34), no shared lock.
- **Ignoring back-pressure / full-buffer policy.** When the hot path out-produces the consumer (a microburst, Ch. 53), the ring fills. **Never block the producer** (that puts the consumer's slowness on the hot path — the exact inversion you're avoiding). Choose explicitly: *drop* newest (and count drops), or *overwrite* oldest (lossy but bounded). Measure it under burst (§71.3). Silent blocking-on-full is a hidden hot-path stall.
- **`clock_gettime` per record.** A timestamp syscall on the hot path (Ch. 17) — use `rdtscp` and convert offline. Even the VDSO path is dozens of ns you don't need to spend on the producer.
- **Logging too much / dynamic log levels checked expensively.** Even a few-ns call, made millions of times, adds up; and a runtime log-level check that *isn't* trivially predicted (Ch. 13) costs on every call. Make the level check a single predictable branch (often compiled out for disabled levels, Ch. 18), and be deliberate about hot-path log volume.
- **False sharing between producer and consumer.** The ring's head and tail indices on the same cache line ping-pong between the producing and consuming cores (Ch. 33). Pad them to separate cache lines (`hardware_destructive_interference_size`).
- **Trusting the API name over the codegen.** A logger advertised as "async" or "fast" may still format on the calling thread (the §71.3 middle row). Verify in the asm (§71.5) that the hot-path side is call-free — don't trust the label.

## 71.7 Exercises & checklist

**Exercises:**

1. **Three-way cost measurement.** Benchmark (Ch. 3) the hot-path cost of: (a) `fprintf` to a file, (b) async-but-format-on-path (enqueue a `std::string`), (c) deferred/binary (enqueue raw args). Reproduce the §71.3 spread and, crucially, the *tail* — induce a page fault / disk flush during (a) and capture the millisecond outlier with HdrHistogram.
2. **Build a minimal binary logger.** Implement a `LOG` macro that captures a compile-time site ID + `rdtscp` + raw args into a per-thread SPSC ring (Ch. 34), and an offline decoder that reconstructs the text. Verify in Godbolt (§71.5) that the hot-path call is a few stores with no `call` to formatting/allocation.
3. **Codegen contrast.** Put your binary `LOG` and an `std::ofstream <<` line side by side in Compiler Explorer (Ch. 4). Confirm the binary version has no format/alloc/`write` calls and the stream version is all calls. This contrast is the chapter.
4. **Back-pressure under burst.** Drive the logger with a microburst (Ch. 53) that out-produces the consumer. Implement *drop-and-count* vs *overwrite-oldest* and measure the hot-path cost stays flat under both — and verify neither ever blocks the producer.
5. **Consumer batching.** Measure consumer throughput with per-record vs batched formatting+I/O (Ch. 37, Ch. 48 io_uring). Show batching lets the consumer keep up with a producer it couldn't match one-at-a-time.

**Checklist:**

- [ ] The hot-path log call does **no formatting, no allocation, no lock, no syscall** — it stores raw values (+ site ID + TSC) to a pre-allocated lock-free ring and returns (§71.2, §71.4).
- [ ] Verified in the **codegen** (§71.5) that the hot-path side is **call-free** (no `std::string`/`new`/`format`/`write`) — not trusted from the API name.
- [ ] Formatting and **all I/O** run on a dedicated **consumer thread** pinned to a **housekeeping** core (Ch. 42), batched (Ch. 37), never on an isolated hot core (§71.4.1).
- [ ] Each hot thread has its **own single-producer ring** (Ch. 34); head/tail are **padded** against false sharing (Ch. 33); no shared logger mutex (§71.6).
- [ ] Timestamps use **`rdtscp`** (Ch. 17), converted offline — no `clock_gettime` on the path (§71.6).
- [ ] The **full-buffer policy** is explicit (drop-and-count or overwrite-oldest) and **never blocks the producer**; measured under burst (§71.3, §71.6).
- [ ] Off-hot-path output (setup, status, the consumer's own writes) uses **`std::print`/`std::println`** (C++23) for type-safe, readable formatting — kept strictly off the hot path (§71.4.3).

## 71.8 References

- The **NanoLog** papers and implementation (Stanford) — nanosecond-scale logging via compile-time format extraction and binary records; the canonical deferred/binary-logging design (§71.2, §71.4.2).
- The **LMAX Disruptor** paper (Ch. 37 references) — the batched single-producer/consumer ring that underlies a fast async logger (§71.4.1).
- `std::print`/`std::println` and `<print>` (C++23) and the `{fmt}` library documentation — type-safe off-hot-path formatting (§71.4.3).
- Production low-latency logger implementations and talks (Quill, the binary-logging approaches used in HFT) — real-world async/deferred designs (§71.4).
- Ch. 34 (lock-free SPSC ring), Ch. 37 (Disruptor batching), Ch. 17 (TSC timestamps), Ch. 23–24 (no alloc/syscall on the path) — the pieces this chapter assembles.

## 71.9 Additional Reading

- CppCon talks on low-latency logging and "logging without slowing down" — the deferral and binary-record patterns in practice.
- The NanoLog and Quill design write-ups — compile-time site IDs, type-safe argument capture, offline decoding.
- Ch. 75 (*Capture/Persistence*) — the same binary-record, off-hot-path-writer discipline applied to journaling ticks/orders for replay; Ch. 76 (*Case Study*) — logging as production observability of the tick-to-trade path; Ch. 48 (*io_uring*) — getting the consumer's batched writes to disk efficiently; Ch. 72 (*Secure Programming*) — keeping secrets out of logs and `std::print`'s type safety vs `printf`.
- **Appendix E** — the cost of a syscall / page fault (why I/O can't be on the path); **Appendix F** — SPSC/Disruptor/deferred-formatting glossary.

---

*Next: Ch. 72 — Secure Programming for Low-Latency Systems, treating market-data and order-entry messages as untrusted input: bounds-checking zero-copy parses (Ch. 53), memory safety in arena/zero-allocation designs (Ch. 24), integer safety in price/quantity math (Ch. 27), and the hardening-vs-latency trade-off (stack protector, FORTIFY, CFI, CET) on an isolated box — security as a hot-path concern.*
