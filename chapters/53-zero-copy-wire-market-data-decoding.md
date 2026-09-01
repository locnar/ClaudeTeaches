# Part IX — Market Data, NIC & Fabric

# Chapter 53 — Zero-Copy Wire Handling & Market-Data Decoding

> **Prerequisites:** Ch. 51 (multicast/A-B feeds — the transport delivering these bytes), Ch. 21 (aliasing / `bit_cast` — overlaying structs on a byte buffer safely), Ch. 9 (struct layout/alignment/endianness), Ch. 25 (the order book this feeds), Ch. 28 (branch-free integer/bit parsing), Ch. 55 (NIC RX rings/timestamping), Ch. 72 (untrusted input — the security framing).
>
> **Leads into:** Ch. 25 (the order book the decoder drives), Ch. 55, 58, 61, 62 (NIC/bypass receive paths), Ch. 72 (treating wire data as hostile — the security chapter builds directly on this), Ch. 75 (capturing the wire). The feed handler is the front door of the tick-to-trade path.

---

## 53.1 Why it matters: parse off the wire without copies

The feed handler is the front door of the tick-to-trade path: bytes arrive from the NIC (Ch. 51, 55), and the feed handler must turn them into order-book updates (Ch. 25) at **nanosecond** speed, on *every* packet, while the market floods it with millions of messages per second at the open. Two things make this hard. First, the naive approach — **copy** the packet into a buffer, copy fields into a parsed struct, copy that into the book — burns the latency budget on memcpy (Ch. 7, 23) before any *work* happens; the discipline is **zero-copy**: parse the message *in place*, directly out of the receive buffer, without copying it anywhere. Second, the data is a **binary wire format** (ITCH, SBE) or a text one (FIX) with its own encoding, endianness, and variable-length fields — and parsing it fast means overlaying fixed-layout structs (Ch. 9, 21) or using branch-free integer/decimal parsing (Ch. 28), not a general-purpose parser.

The latency stakes are extreme because this is the *first* stage and it runs on *every* message — any inefficiency here multiplies across the whole feed and lands in the tick-to-trade tail (Ch. 1) exactly during the high-volume bursts (market open) when speed matters most. A feed handler that takes 500 ns to decode a message instead of 50 ns adds 450 ns to *every* tick — and worse, falls behind during a microburst, building a queue that turns into *latency* (you're processing stale data) or *drops* (Ch. 51). So decode speed isn't just about the average message; it's about **keeping up with the burst** so you never fall behind. This chapter's techniques — zero-copy in-place parsing, branch-free decoding (Ch. 28), scatter/gather receive — are about making per-message decode so cheap that the handler stays ahead of the worst burst.

And then there's the part that makes feed handling uniquely treacherous: **the wire data is untrusted input** (Ch. 72). It comes from outside your process — and even from a "trusted" exchange, packets can be malformed, truncated, corrupted, reordered, duplicated, or dropped (Ch. 51's UDP unreliability). A feed handler that blindly trusts the wire — reading a length field without bounds-checking it, indexing past the buffer, trusting a count — is a buffer-overflow/crash waiting to happen the first time a packet is malformed (a bug, a corruption, or a hostile actor — Ch. 72). So zero-copy parsing must be **bounds-checked at every variable-length field**, and the **A/B feed arbitration and sequence-gap recovery** (handling the dropped/reordered/duplicated packets that *will* happen — Ch. 51) is core feed-handler logic, not an afterthought. This is a flagship chapter: it covers the encodings and microburst mental model (§53.2), measures per-protocol decode latency (§53.3), details branch-free parsing, zero-copy receive, A/B arbitration, and RX-ring/reorder/drop handling (§53.4), verifies the parse-loop codegen (§53.5), and — tying hard to Ch. 72 — warns about unaligned reads and trusting wire input (§53.6).

## 53.2 Mental model

### 53.2.1 ITCH/OUCH/FIX/SBE/FAST encodings

The market-data and order-entry protocols, by encoding strategy — which determines how you parse them:

- **ITCH (Nasdaq) — fixed-layout binary, the fast case.** Each message is a **fixed-size, fixed-offset** binary record (a 1-byte type, then fields at known offsets, big-endian). Decoding is **overlay a struct** (Ch. 9, 21) on the buffer and read fields — essentially free (no parsing, just typed access). The ideal for low latency; most exchange *market-data* feeds are binary-fixed like this (ITCH, CME MDP's SBE, etc.).
- **SBE (Simple Binary Encoding) — fixed binary with a schema.** The FIX community's binary encoding (used by CME and others): fixed-layout binary defined by a *schema*, with fixed and variable-length fields, designed explicitly for **zero-copy, low-latency** decode. Like ITCH, you overlay generated accessors on the buffer — fast. The modern standard for binary market data/order entry.
- **OUCH (Nasdaq) — binary order entry.** The order-*entry* counterpart to ITCH (send orders, get acks/fills) — fixed binary, same overlay approach, on the TCP order path (Ch. 51).
- **FIX (Financial Information eXchange) — tag=value text, the slow case.** A *text* protocol: `8=FIX.4.2|35=D|55=AAPL|...` (tag=value pairs, SOH-delimited). Human-readable, flexible, ubiquitous for *order entry* and some feeds — but **slow to parse** (text scanning, field lookup, string→number conversion — Ch. 28). Where latency matters, FIX is parsed with optimized field-scanning (not a generic parser), or replaced by SBE/binary FIX (FIXP). Branch-free integer/decimal parsing (§53.4.1) is critical for the numbers in FIX.
- **FAST (FIX Adapted for STreaming) — compressed binary.** A bit-packed, template-based compression of FIX for bandwidth-constrained feeds — *complex* to decode (stop-bit-encoded fields, operators, presence maps — bit manipulation, Ch. 28). Lower bandwidth, higher decode CPU; used where bandwidth is the constraint. Decoding is bit-level work.

The decoding-strategy spectrum: **fixed binary (ITCH/SBE/OUCH) → overlay a struct, ~free** (the fast, common case); **text (FIX) → optimized field scanning + branch-free number parsing** (slower, Ch. 28); **compressed (FAST) → bit-level decode** (complex). Most of the latency-critical work is the fixed-binary overlay (§53.2.2).

### 53.2.2 Fixed-layout structs vs bit-fields; endianness

Decoding fixed binary fast — the in-place overlay:

- **Overlay a fixed-layout struct (the zero-copy core).** For a fixed-binary message, define a struct matching the wire layout (exact field sizes, packed, Ch. 9) and access the message as that struct *in place* in the receive buffer — no copy, no parse, just typed field access. The subtlety (Ch. 21): a naive `reinterpret_cast<Msg*>(buf)` and dereference is **strict-aliasing UB** *and* may be **misaligned** (the wire field isn't necessarily aligned in the buffer). Do it safely: `memcpy`/`std::bit_cast` (Ch. 21) the field bytes into a typed local (which the compiler folds to a load — §53.5), or use a carefully-`alignas(1)`/packed accessor. Zero-copy means *no payload copy*, not *no field load*.
- **Endianness (Ch. 28).** Wire formats are usually **big-endian** (network byte order); x86 is little-endian — so multi-byte fields must be **byte-swapped** on read (`std::byteswap` — C++23, or `bswap`/`ntohl` — one instruction, Ch. 28). Forgetting the swap silently reads garbage. Combine the swap with the `memcpy`/`bit_cast` load.
- **Fixed-layout structs vs C bit-fields.** For sub-byte fields (flags, packed values), prefer **explicit shifts/masks** (Ch. 28) over C bit-fields — bit-field layout is implementation-defined and codegen is often poor (Ch. 9, 28). Define the field offsets/masks explicitly and extract with shifts.
- **Variable-length fields — bounds-check every one (Ch. 72).** Many messages have variable-length parts (a count then N entries, a length-prefixed string). You **must** validate the length/count against the *actual buffer size* before reading — a length field that says "200 bytes" in a 50-byte packet is a buffer overflow if trusted (§53.6, Ch. 72). Every variable-length read is bounds-checked against the remaining buffer.
- **Generated decoders (SBE).** SBE (and FIX binary) provide schema-driven *code generators* that emit the overlay accessors (with the swaps and offsets correct) — use them rather than hand-writing offset arithmetic (less error-prone), but understand they produce the same in-place-load codegen (§53.5).

### 53.2.3 Redundant A/B multicast feeds

Market data is unreliable UDP multicast (Ch. 51), so reliability is built at the *feed-handler* level via redundancy and sequencing:

- **Sequence numbers.** Every message (or packet) carries a **sequence number**; the feed handler tracks the expected next sequence per feed. A gap (received seq > expected) means **packets were dropped** (Ch. 51); a duplicate (seq < expected) is ignored. Sequence tracking is how you *detect* loss on an unreliable feed.
- **A/B redundant feeds (Ch. 51).** The exchange sends the *same* sequenced data on **two independent multicast streams** (A and B, often different network paths). The feed handler joins **both** and **arbitrates**: process each sequence number from **whichever feed delivers it first**, and use the *other* feed to **fill a gap** (a packet dropped on A likely arrives on B). This turns two lossy feeds into one near-lossless stream — the standard market-data reliability mechanism (§53.4.3).
- **Gap recovery.** If a sequence is missing from *both* A and B (a real loss), the handler requests **retransmission** (a recovery/replay service the exchange provides, e.g. via TCP) or rebuilds from a **snapshot** (a periodic full book image the exchange multicasts). During recovery, the book is stale — minimize the window (fast detection + fast recovery) and handle the in-flight state carefully.
- **Single logical stream out.** The arbitration layer presents a *single, in-order, gap-free* (post-recovery) message stream to the book builder (Ch. 25) — hiding the A/B/duplicate/reorder complexity. This is the feed handler's core job: unreliable multicast in, clean sequenced stream out.

### 53.2.4 Mechanical sympathy for UDP microbursts: hardware RX rings, software reorder buffers, market-open drops

The hardest operational reality — **microbursts** (Ch. 51): at the open or on a big move, the exchange floods you with thousands of packets in *microseconds*, and if anything in the receive path can't keep up, you **drop** packets (gaps, recovery, stale book):

- **The NIC RX ring (Ch. 55).** The NIC DMA's incoming packets into a **ring of descriptors** in host memory; if the ring fills faster than the host drains it (a burst + a slow handler), the NIC **drops** packets (rx_missed/rx_no_buffer — `ethtool -S`). **Size the RX ring large enough** to absorb the worst microburst (§53.4.4, Ch. 55) — a too-small ring is a prime market-open drop cause.
- **Software reorder buffers.** UDP can **reorder** packets (different network paths, multi-queue NICs — RSS, Ch. 55); the A/B feeds arrive interleaved and out of order. The handler needs a **reorder buffer** (a window indexed by sequence) to reassemble the in-order stream — buffer out-of-order arrivals until the gap fills, with a bounded window (and a timeout → declare a gap → recover). Sizing the reorder window vs the recovery latency is a tuning decision (§53.4.4).
- **Drain fast, drain in batch (Ch. 47, 55).** The handler must drain the RX ring/socket *faster* than the burst fills it — `recvmmsg` (many packets/syscall — Ch. 47), busy-polling (Ch. 41), kernel bypass (Ch. 62). Falling behind during the burst is how the ring overflows. Batch-receive and batch-process (Ch. 37).
- **Detect drops at every layer (Ch. 51).** NIC (`ethtool -S` rx_dropped/missed), socket (UDP drop counters), application (sequence gaps). A market-open drop that goes undetected is a silently-wrong book. Instrument and alert, and *test under microburst load* (replay a captured open — Ch. 75).

The model: **decode is zero-copy in-place parsing of (mostly fixed-binary) wire formats — overlay structs with byte-swaps and bounds checks — fast enough to keep up with microbursts; reliability comes from sequence numbers + A/B feed arbitration + gap recovery; and surviving the market-open microburst needs a big NIC RX ring, fast batched draining, a software reorder buffer, and drop detection at every layer. The wire is untrusted — bounds-check everything (Ch. 72).**

## 53.3 Measure it: decode latency per protocol

Measure per-message decode latency across the encoding spectrum (§53.2.1): fixed-binary overlay (ITCH/SBE) vs text (FIX). The headline: fixed-binary overlay is ~free; text parsing is an order of magnitude more — driving the "binary for the hot feed" conclusion.

```cpp
// decode.cpp — decode latency: fixed-binary overlay vs FIX text parse.
// Build: g++ -O2 -std=c++20 -march=native decode.cpp -o decode
// Run pinned:  taskset -c 2 ./decode
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <charconv>
#include <vector>
#include <chrono>
#include <bit>

// Fixed-binary "ITCH-like" AddOrder: type(1) seq(8) id(8) side(1) qty(4) sym(8) price(8), big-endian.
#pragma pack(push, 1)
struct AddOrder { std::uint8_t type; std::uint64_t seq, id; std::uint8_t side;
                  std::uint32_t qty; char sym[8]; std::uint64_t price; };
#pragma pack(pop)

struct Parsed { std::uint64_t seq, id, price; std::uint32_t qty; char side; };

// zero-copy decode: load fields in place with bswap (Ch.20,27) — no payload copy.
Parsed decode_binary(const std::uint8_t* p) {
    Parsed r;
    std::uint64_t seq; std::memcpy(&seq, p+1, 8);  r.seq = std::byteswap(seq);       // safe load + swap
    std::uint64_t id;  std::memcpy(&id,  p+9, 8);  r.id  = std::byteswap(id);
    r.side = char(p[17]);
    std::uint32_t qty; std::memcpy(&qty, p+18, 4); r.qty = std::byteswap(qty);
    std::uint64_t px;  std::memcpy(&px,  p+30, 8); r.price = std::byteswap(px);
    return r;
}
// FIX-like text decode: scan "55=AAPL|44=12345|38=100|54=1|..." — field scan + from_chars (Ch.27).
Parsed decode_fix(const char* s, std::size_t n) {
    Parsed r{}; const char* end = s + n;
    while (s < end) {
        int tag = 0; s = std::from_chars(s, end, tag).ptr; if (*s=='=') ++s;
        if (tag == 44) { long long px; s = std::from_chars(s, end, px).ptr; r.price = px; }
        else if (tag == 38) { unsigned q; s = std::from_chars(s, end, q).ptr; r.qty = q; }
        else { while (s < end && *s != '\x01') ++s; }     // skip to SOH
        if (s < end) ++s;                                  // past SOH
    }
    return r;
}

int main() {
    constexpr long N = 5'000'000;
    std::vector<std::uint8_t> bin(38); bin[0]=0x41;        // one binary msg
    const char* fix = "55=AAPL\x01" "44=12345\x01" "38=100\x01" "54=1\x01";
    std::size_t fixlen = std::strlen(fix);

    auto t0 = std::chrono::steady_clock::now(); std::uint64_t s1=0;
    for (long i=0;i<N;++i){ auto r=decode_binary(bin.data()); s1+=r.price; }
    auto t1 = std::chrono::steady_clock::now(); std::uint64_t s2=0;
    for (long i=0;i<N;++i){ auto r=decode_fix(fix, fixlen); s2+=r.price; }
    auto t2 = std::chrono::steady_clock::now();
    auto ns=[&](auto a,auto b){return std::chrono::duration_cast<std::chrono::nanoseconds>(b-a).count()/(double)N;};
    std::printf("binary overlay: %.1f ns/msg   FIX text: %.1f ns/msg  (s=%llu,%llu)\n",
                ns(t0,t1), ns(t1,t2), (unsigned long long)s1, (unsigned long long)s2);
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP), `-O2 -march=native`, pinned, turbo off (illustrative):

```
   encoding                         ns / message    note
   fixed-binary overlay (ITCH/SBE)  ~8-15 ns        in-place loads + bswap — ~free (§48.2.2, §48.5)
   FIX text parse (from_chars)      ~80-200 ns      field scan + text→int conversion (Ch.27) — ~10x more
   FAST (bit-unpacking)             ~100-300 ns     stop-bit/presence-map decode (Ch.27) — complex

   binary is ~10x faster to decode → use binary (SBE/ITCH) for the hot feed; FIX where flexibility wins.
```

Read it: **fixed-binary overlay decode is ~free** (~10 ns/msg) — it's just a handful of in-place loads + byte-swaps (§53.5), no parsing, no copying, no allocation. **FIX text parsing is ~10× more** — every field requires scanning to the delimiter and converting text→number (`from_chars` — Ch. 28), even optimized. The conclusion drives real protocol choices: **latency-critical feeds use binary (ITCH/SBE)**; FIX is used where its flexibility/ubiquity matters (order entry to some venues, less-hot paths) and parsed with optimized field-scanning + branch-free number conversion (§53.4.1) when it must be fast. The binary number (~10 ns) is what lets a handler keep up with a microburst (§53.2.4); a 200 ns text parse falls behind. (And note: even the "free" binary decode must **bounds-check** variable-length fields — §53.6 — which this minimal example omits but production must not.) The optimization target: drive the *common* message's decode toward the binary number, and ensure the decode loop vectorizes/branch-minimizes where it can (§53.5).

## 53.4 Techniques

### 53.4.1 `std::from_chars`/`to_chars`; branch-free integer/decimal parsing

Fast number conversion — critical for FIX/text and for formatting outbound (Ch. 27 prices):

- **`std::from_chars`/`std::to_chars` (C++17).** The fast, **non-allocating, locale-independent** number↔text conversion (unlike `atoi`/`stringstream`/`scanf` — slow, allocating, locale-dependent). `from_chars` parses an integer/float from a byte range with no allocation and excellent codegen; `to_chars` formats. Use them for all hot-path number conversion (FIX fields, logging — Ch. 71). They're dramatically faster than the legacy APIs.
- **Branch-free integer parsing (Ch. 28).** For *fixed-width* numeric fields (common in feeds), SIMD/SWAR parsing beats digit-at-a-time: parse 8 digits at once with a few multiplies/shifts (the "parse 8 digits in parallel" trick), or vectorized (Ch. 29) for bulk. `from_chars` is good; hand-tuned fixed-width parsers (or libraries like `fast_float`) are faster for the hottest fields. Branch-free avoids the mispredict (Ch. 13) on variable-length number scanning.
- **Decimal/price parsing to scaled integers (Ch. 27).** Parse a price *directly into a scaled integer* (Ch. 27's fixed-point) — not through `double` (representability error — Ch. 27). A decimal like `123.45` → integer `1234500` at scale 10⁴, parsed with integer arithmetic and the known decimal position. Keep the price integer end to end (Ch. 27).
- **`to_chars` for outbound formatting.** Formatting order fields / log lines (Ch. 71) on the way out: `to_chars` + digit-pair tables (Ch. 18's `constexpr` "00".."99" table) for fast integer formatting, off the hot path where possible.
- **`fast_float`/`fast_double_parser`.** For float parsing (where you must), these libraries are far faster than `strtod`/`from_chars`-float on some implementations — relevant if a feed carries floating-point (most carry scaled integers — Ch. 27).

### 53.4.2 Scatter/gather and zero-copy receive paths

Getting the bytes from NIC to decoder without copies:

- **Parse in place — no copy from the receive buffer.** Decode directly out of the socket/ring receive buffer (§53.2.2) — the overlay-struct/`bit_cast` loads read the buffer in place. Don't `memcpy` the packet to a "work buffer" first (a wasted copy — Ch. 7, 23). The packet's bytes are parsed where they landed.
- **Scatter/gather (`recvmmsg`, `readv` — Ch. 47).** `recvmmsg` receives *many* datagrams in one syscall (Ch. 47 — essential for draining a microburst, §53.2.4); `readv`/scatter-gather splits a message into header+body buffers without a copy. These cut syscalls and copies on the receive path.
- **Kernel-bypass zero-copy receive (Ch. 55, 62).** With kernel bypass (ef_vi/DPDK — Ch. 62) or `AF_XDP` (Ch. 61), packets DMA directly into user-space buffers the application reads — *true* zero-copy from wire to decoder, no kernel copy at all. The ultimate receive path for the hot feed; the decoder overlays its structs on the NIC's DMA buffer.
- **Process the batch through the pipeline (Ch. 37).** A batch of received messages flows as a batch through decode → book → strategy (Ch. 37's batching) — amortizing per-message overhead and self-correcting under burst (Ch. 37.4.2). Decode is the first batched stage.
- **NIC hardware timestamps (Ch. 55, 58).** Capture the NIC's hardware receive timestamp (Ch. 55) per packet for accurate latency measurement (Ch. 17, 58) and sequencing — read it from the packet metadata, not a software clock (Ch. 17).

### 53.4.3 A/B line arbitration; sequence-gap detection and recovery

The reliability core (§53.2.3) — turning unreliable A/B multicast into a clean stream:

- **Sequence tracking per feed.** Maintain the expected-next sequence; for each received message, compare: **in-order** → process + advance; **duplicate** (seq < expected, already seen) → drop; **gap** (seq > expected) → a missing range → buffer the out-of-order message (reorder buffer — §53.4.4) and try to fill from the other feed.
- **A/B arbitration (take-first, fill-gaps).** Join both feeds; process each sequence from whichever arrives first (a per-sequence "have I processed this?" check — a sliding window/bitmap — Ch. 28). A packet missing on A is likely present on B — so the *combination* is near-lossless. The arbitrator dedups across feeds and emits each sequence exactly once, in order.
- **Gap recovery (snapshot + retransmit).** When a sequence is missing from *both* feeds (real loss): request **retransmission** from the exchange's recovery service (often a TCP replay — Ch. 51), or rebuild from the periodic **snapshot** (a full book image the exchange multicasts) — applying buffered incrementals after the snapshot point. During recovery the book is stale/incomplete — flag it, minimize the window (fast detection + fast recovery), and handle strategy behavior on a stale book (don't trade on it).
- **Bounded reorder window + timeout.** Buffer out-of-order messages in a window indexed by sequence; if the gap isn't filled within a timeout/window (neither feed delivers it), declare a real gap and trigger recovery — don't wait forever (that's unbounded latency). The window size vs recovery-latency trade is a tuning decision (§53.4.4).
- **Single clean stream out (§53.2.3).** The arbitration layer hides all of this, emitting one in-order, deduped, gap-recovered stream to the book builder (Ch. 25). Test it hard with injected drops/reorders/duplicates (Ch. 40 fuzzing, Ch. 75 replay).

### 53.4.4 Tuning RX ring sizes, managing UDP reordering in software ring buffers, detecting hardware packet drops

Surviving the microburst (§53.2.4) — the operational tuning:

- **Size the NIC RX ring for the worst microburst (Ch. 55).** `ethtool -G <iface> rx <N>` increases the RX descriptor ring so the NIC can buffer a burst the host hasn't drained yet. Too small → `rx_missed`/`rx_no_buffer` drops at the open. Measure the worst burst (captured open — Ch. 75) and size the ring (and `SO_RCVBUF` — Ch. 51) to absorb it. This is a top market-open drop cause.
- **Software reorder buffer (window).** A buffer indexed by sequence (a ring or window) holding out-of-order arrivals until the gaps fill — reassembling the in-order stream from reordered/A-B-interleaved packets (§53.2.3). Size the window for the expected reorder distance; bound it with a timeout (→ recovery). This is application-level (the NIC delivers packets; you reorder by sequence).
- **Drain fast and in batch (§53.4.2).** The host must drain the RX ring faster than the burst fills it — `recvmmsg`/busy-poll/bypass (Ch. 41, 47, 62). A slow decode (§53.3 — use binary!) or a slow downstream stage backs up the ring → drops. Keep the whole receive→decode pipeline fast enough for the burst.
- **Detect drops at every layer (Ch. 51).** NIC: `ethtool -S` (`rx_dropped`, `rx_missed`, `rx_no_buffer_count`). Socket: UDP `RcvbufErrors`/drops (`/proc/net/snmp`, `ss -u`). Application: sequence gaps (the definitive signal). Instrument all three, alert on any, and **test under replayed market-open load** (Ch. 75) — a drop that only happens at the real open is a production incident you should have caught in test.
- **NIC features for the burst (Ch. 55).** RSS/flow steering (direct the feed to the right queue/core), interrupt coalescing tuning (or busy-poll to avoid interrupts entirely — Ch. 41, 55), and hardware timestamping (Ch. 55). The NIC config (Ch. 55) and this decoding tuning together determine microburst survival.

## 53.5 Verify the codegen: parse loop

The zero-copy overlay's promise — "field access is just a load" — is verifiable, as is whether the parse loop stays branch-light. Snippets are **verified** Clang output (`--target=x86_64-linux-gnu -O2 -march=x86-64-v3`).

**In-place field load + byte-swap → `mov` + `bswap` (no copy).** The `memcpy`-into-local + `byteswap` (Ch. 21, 28) compiles to a single load and a `bswap` — *not* an actual memcpy call, *not* a payload copy:

```asm
; r.seq = byteswap( load 8 bytes at p+1 ):
        mov     rax, qword ptr [rdi + 1]    ; load the wire field IN PLACE (no copy)
        bswap   rax                         ; big-endian → host (Ch.27) — one instruction
        mov     qword ptr [rsi], rax        ; store into the parsed local
```

This is the proof that **zero-copy decode of a fixed-binary field is a load + a swap** — the `memcpy`/`bit_cast` (Ch. 21) folded away, no `call memcpy`, no buffer copy. (A `reinterpret_cast` deref would be UB and possibly a misaligned-access fault — §53.6; the `memcpy` form is safe *and* the same codegen.)

**Bounds check compiles to a cheap compare+branch (predictable).** A `if (offset + len > buf_size) return error;` before a variable-length read is a compare and a (well-predicted, rarely-taken) branch — *cheap*, and the §53.6/Ch. 72 safety is nearly free:

```asm
        lea     rax, [rcx + r8]             ; offset + len
        cmp     rax, rdx                    ; > buf_size ?
        ja      .Lreject                    ; rarely taken (valid packets pass) — predicted (Ch.12)
        ... read the field ...
```

**The dispatch on message type.** The per-message `switch (type)` should be a jump table or a data lookup (Ch. 13–14), not a mispredicting branch chain — verify it (Ch. 14.5):

```asm
        movzx   eax, byte ptr [rdi]         ; message type byte
        jmp     qword ptr [.Ljt + 8*rax]    ; jump table over message types (Ch.13)
```

The verification habits: **confirm field access is a `mov`(+`bswap`), not a `call memcpy` or a misaligned trap** (the zero-copy + safe-load promise — §53.2.2); **confirm bounds checks are cheap predicted branches** (so safety is ~free — §53.6); **confirm the type dispatch is a jump table/lookup** (Ch. 14), not a branch chain; and for bulk fixed-width parsing, **confirm vectorization** (Ch. 29) where it applies. The decode loop is the hottest loop in the system — read its asm and ensure it's loads + swaps + predicted branches, nothing more.

## 53.6 Pitfalls & anti-patterns: unaligned reads, trusting wire input *(ties Ch. 72)*

- **Trusting wire input (the cardinal feed-handler / security sin — Ch. 72).** Reading a length/count field and using it *without bounds-checking against the actual buffer* → buffer over-read/overflow the first time a packet is malformed/truncated/hostile (a crash, or an exploit — Ch. 72). **Every variable-length field is bounds-checked** against the remaining buffer (§53.2.2, §53.5). The wire is untrusted input; this *is* the security boundary (Ch. 72).
- **Unaligned reads via `reinterpret_cast` deref (Ch. 21).** `*reinterpret_cast<uint64_t*>(buf+offset)` is **strict-aliasing UB** (Ch. 21) *and* may be a **misaligned access** (the wire field isn't aligned) — UB/fault on some targets, slow on others. Use `memcpy`/`std::bit_cast` (Ch. 21) — same codegen (§53.5), defined, alignment-safe.
- **Forgetting the byte-swap (endianness).** Reading a big-endian wire field without `byteswap` (Ch. 28) silently yields garbage values (a price of 0x4500000000000000 instead of 0x45). Swap every multi-byte field; test against real wire data.
- **Copying the packet before parsing.** A `memcpy` of the packet into a "work buffer" before decoding wastes the copy (Ch. 7, 23) — defeating zero-copy. Parse in place (§53.4.2).
- **Slow number parsing (`atoi`/`stringstream`/`scanf`).** Legacy conversions are slow, allocating, and locale-dependent — a hot-path disaster for FIX/text. Use `from_chars`/`to_chars` / branch-free parsing (§53.4.1, Ch. 28).
- **Parsing price through `double` (Ch. 27).** Converting a decimal price to `double` introduces representability error (Ch. 27). Parse directly to a **scaled integer** (§53.4.1, Ch. 27).
- **Ignoring drops/gaps (no A/B, no recovery).** Treating UDP multicast as reliable — no sequence tracking, no A/B arbitration, no gap recovery — means silent data loss → a wrong book → wrong trades. A/B + sequence + recovery is *core* logic (§53.4.3), not optional.
- **Undersized RX ring / falling behind the burst.** A small NIC RX ring or a slow decode that can't keep up with the microburst → drops at the open (§53.2.4, §53.4.4) — exactly when it matters. Size the ring, use binary decode, drain in batch, and **test under replayed market-open load** (Ch. 75).
- **C bit-fields for wire layout (Ch. 9, 28).** Implementation-defined layout and poor codegen — wire decoding needs *exact, portable* layout. Use explicit shifts/masks (Ch. 28).
- **Unbounded reorder buffering.** Waiting indefinitely for a missing sequence (no window/timeout) → unbounded latency. Bound the reorder window + timeout → recovery (§53.4.3-4).
- **Not fuzzing the decoder (Ch. 40, 72).** A decoder never fed malformed input *will* mishandle it in production. **Fuzz it (libFuzzer/AFL++ + ASan/UBSan — Ch. 40)** with malformed/truncated/hostile packets — the primary defense for this untrusted-input path (Ch. 72).

## 53.7 Exercises & checklist

**Exercises**

1. **Binary vs FIX decode.** Build `decode.cpp`; compare fixed-binary overlay vs FIX text decode latency. Confirm binary is ~10× faster (§53.3). Disassemble the binary decode (§53.5) — is each field a `mov`+`bswap` (not a memcpy call)? Is the type dispatch a jump table?
2. **The bounds-check.** Add a variable-length field (a count + N entries) to the binary message; decode it *without* a bounds check, then feed a packet whose count exceeds the buffer — reproduce the over-read (ASan — Ch. 40). Add the bounds check; confirm it's a cheap predicted branch (§53.5) and rejects the bad packet (§53.6, Ch. 72).
3. **A/B arbitration.** Build two sequenced UDP streams of the same data; drop random packets from each; write an arbitrator that joins both, dedups, reorders, and emits one clean in-order stream. Inject a gap in *both* and trigger a recovery path. Measure recovery latency (§53.4.3).
4. **Microburst survival.** Replay a captured (or synthetic) market-open burst into your receiver; tune NIC RX ring (`ethtool -G`) + `SO_RCVBUF` + `recvmmsg` until zero drops (`ethtool -S` rx_missed = 0). Measure decode latency under burst (§53.4.4).
5. **Fuzz the decoder.** Write a libFuzzer harness (ASan+UBSan — Ch. 40) for your message decoder; let it find a mishandled malformed packet. Fix and re-fuzz. This is the Ch. 72 untrusted-input defense (§53.6).

**Checklist — zero-copy wire decoding**

- [ ] Latency-critical feeds use **fixed-binary encodings** (ITCH/SBE — ~10× faster decode than FIX text — §53.3); FIX/text uses **`from_chars`/branch-free** parsing (Ch. 28, §53.4.1) and prices parse to **scaled integers** (Ch. 27).
- [ ] Decode is **zero-copy in place** — overlay structs read via **`memcpy`/`bit_cast`** (alignment-safe, defined — Ch. 21) with **byte-swaps** (Ch. 28), **no payload copy**; verified in the asm as `mov`+`bswap` (§53.5).
- [ ] **Every variable-length field is bounds-checked** against the buffer (the untrusted-input boundary — §53.6, Ch. 72); the decoder is **fuzzed** (libFuzzer + ASan/UBSan — Ch. 40).
- [ ] Reliability is built in: **sequence tracking + A/B feed arbitration + gap recovery** (snapshot/retransmit) producing one **clean in-order stream** to the book (Ch. 25) — with a **bounded reorder window + timeout** (§53.4.3).
- [ ] The microburst is survived: **NIC RX ring sized** (`ethtool -G`), **`SO_RCVBUF` sized** (Ch. 51), **batched draining** (`recvmmsg`/busy-poll/bypass — Ch. 47, 62), and **drops detected at NIC/socket/app layers** (§53.4.4) — tested under **replayed market-open load** (Ch. 75).
- [ ] The receive path is **zero-copy** (parse in the RX/DMA buffer; kernel-bypass where ultra-hot — Ch. 62); **NIC hardware timestamps** captured (Ch. 55, 58).
- [ ] Type dispatch is a **jump table/data lookup** (Ch. 14), not a branch chain; bulk fixed-width parsing **vectorizes** where applicable (Ch. 29) — verified (§53.5).
- [ ] **No `reinterpret_cast`-deref unaligned reads, no `atoi`/`stringstream`, no `double` prices, no C bit-fields** for wire layout (§53.6).

## 53.8 References

- The exchange protocol specifications — Nasdaq **ITCH/OUCH** (and MoldUDP64), **FIX** and **FIXP/SBE** (FIX Trading Community), CME **MDP 3.0 / SBE**, and **FAST** — the encodings of §53.2.1.
- D. Lemire et al., *simdjson* and *fast_float* — branch-free/SIMD parsing techniques and number conversion (§53.4.1, ties Ch. 28–29).
- ISO C++ / cppreference — `std::from_chars`/`to_chars`, `std::bit_cast`, `std::byteswap` — the safe, fast primitives (§53.4.1, §53.2.2).
- The exchange feed-handler / A-B-arbitration and gap-recovery documentation (Nasdaq, CME) and "Mechanical Sympathy" feed-handler writeups — reliability and microburst handling (§53.4.3-4).
- Ch. 72 references (secure coding, fuzzing) — treating wire data as untrusted input (§53.6).

## 53.9 Additional Reading

- HFT feed-handler engineering talks and the SBE/simple-binary-encoding project docs — production zero-copy decoding patterns.
- CME/Nasdaq market-data handler and microburst tuning guides — RX ring/reorder/drop handling (§53.4.4).
- Ch. 21 (*Aliasing/`bit_cast`*) — safe in-place field loads; Ch. 28 (*Bit Tricks*) — byte-swap/branch-free parsing; Ch. 25 (*Order Book*) — what the decoder feeds; Ch. 51 (*Sockets/Multicast*) — the A/B transport; Ch. 55 (*NIC*) — RX ring/timestamping/RSS; Ch. 62 (*Kernel Bypass*) — zero-copy receive; Ch. 72 (*Secure Programming*) — untrusted input/fuzzing; Ch. 75 (*Capture/Replay*) — capturing the wire and replaying the open.
- Ch. 54 (*Reliable Multicast & Feed Recovery*) — the recovery deep-dive this chapter's gap detection points at: retransmission, snapshots, and rebuilding the book without stalling the hot path.
- **Appendix E** — decode-latency and RX numbers; **Appendix F** — ITCH/FIX/SBE/A-B/feed glossary.

---

*Next: Ch. 54 — Reliable Multicast & Feed Recovery, the recovery half this chapter's gap detection points at: UDP multicast is lossy, a gap blinds the strategy, and retransmission, snapshots, and PGM-style recovery must restore the stream without ever stalling the hot path.*
