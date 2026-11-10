# Part IX — Market Data, NIC & Fabric

# Chapter 70 — Zero-Copy Wire Handling & Market-Data Decoding

> **Prerequisites:** Ch. 68 (multicast/A-B feeds — the transport delivering these bytes), Ch. 26 (aliasing / `bit_cast` — overlaying structs on a byte buffer safely), Ch. 11 (struct layout/alignment/packing), Ch. 32 (the order book this feeds), Ch. 37 (endianness/byteswap and shift/mask bit extraction), Ch. 75-preview (NIC RX rings/timestamping), Ch. 100-preview (untrusted input — the security framing).
>
> **Leads into:** Ch. 72 (reliable multicast & feed recovery — retransmission/snapshot recovery for the gaps A/B cannot fill), Ch. 32 (the order book the decoder drives), Ch. 75, 78, 81, 82 (NIC/bypass receive paths), Ch. 100 (treating wire data as hostile — the security chapter builds directly on this), Ch. 107 (capturing the wire). The feed handler is the front door of the tick-to-trade path.

---

## 70.1 Why it matters: parse off the wire without copies

The feed handler is the front door of the tick-to-trade path: bytes arrive from the NIC (Ch. 68, 75), and the feed handler must turn them into order-book updates (Ch. 32) at **nanosecond** speed, on *every* packet, while the market floods it with millions of messages per second at the open. Two things make this hard. First, the naive approach — **copy** the packet into a buffer, copy fields into a parsed struct, copy that into the book — burns the latency budget on memcpy (Ch. 8, 30) before any *work* happens; the discipline is **zero-copy**: parse the message *in place*, directly out of the receive buffer, without copying it anywhere. Second, the data is a **binary wire format** (ITCH, SBE) or a text one (FIX) with its own encoding, endianness, and variable-length fields — and parsing it fast means overlaying fixed-layout structs (Ch. 11, 26) or using branch-free integer/decimal parsing (Ch. 37), not a general-purpose parser.

The latency stakes are extreme because this is the *first* stage and it runs on *every* message — any inefficiency here multiplies across the whole feed and lands in the tick-to-trade tail (Ch. 1) exactly during the high-volume bursts (market open) when speed matters most. A feed handler that takes 500 ns to decode a message instead of 50 ns adds 450 ns to *every* tick — and worse, falls behind during a microburst, building a queue that turns into *latency* (you're processing stale data) or *drops* (Ch. 68). So decode speed isn't just about the average message; it's about **keeping up with the burst** so you never fall behind. This chapter's techniques — zero-copy in-place parsing, branch-free decoding (Ch. 37), scatter/gather receive — are about making per-message decode so cheap that the handler stays ahead of the worst burst.

And then there's the part that makes feed handling uniquely treacherous: **the wire data is untrusted input** (Ch. 100). It comes from outside your process — and even from a "trusted" exchange, packets can be malformed, truncated, corrupted, reordered, duplicated, or dropped (Ch. 68's UDP unreliability). A feed handler that blindly trusts the wire — reading a length field without bounds-checking it, indexing past the buffer, trusting a count — is a buffer-overflow/crash waiting to happen the first time a packet is malformed (a bug, a corruption, or a hostile actor — Ch. 100). So zero-copy parsing must be **bounds-checked at every variable-length field**, and the **A/B feed arbitration and sequence-gap recovery** (handling the dropped/reordered/duplicated packets that *will* happen — Ch. 68) is core feed-handler logic, not an afterthought. This is a flagship chapter: it covers the encodings and microburst mental model (§70.2), measures per-protocol decode latency (§70.3), details branch-free parsing, zero-copy receive, A/B arbitration, and RX-ring/reorder/drop handling (§70.4), verifies the parse-loop codegen (§70.5), and — tying hard to Ch. 100 — warns about unaligned reads and trusting wire input (§70.6).

## 70.2 Mental model

### 70.2.1 ITCH/OUCH/FIX/SBE/FAST encodings

The market-data and order-entry protocols, by encoding strategy — which determines how you parse them (the per-venue framing, channels, snapshot services and sessions these encodings ride on — MoldUDP64, MDP 3.0 channels, EOBI/EMDI, PITCH units, the SIP — are tabulated in Appendix J.6):

- **ITCH (Nasdaq) — fixed-layout binary, the fast case.** Each message is a **fixed-size, fixed-offset** binary record (a 1-byte type, then fields at known offsets, big-endian). Decoding is **overlay a struct** (Ch. 11, 26) on the buffer and read fields — essentially free (no parsing, just typed access). The ideal for low latency; most exchange *market-data* feeds are binary-fixed like this (ITCH, CME MDP's SBE, etc.).
- **SBE (Simple Binary Encoding) — fixed binary with a schema.** The FIX community's binary encoding (used by CME and others): fixed-layout binary defined by a *schema*, with fixed and variable-length fields, designed explicitly for **zero-copy, low-latency** decode. Like ITCH, you overlay generated accessors on the buffer — fast. The modern standard for binary market data/order entry.
- **OUCH (Nasdaq) — binary order entry.** The order-*entry* counterpart to ITCH (send orders, get acks/fills) — fixed binary, same overlay approach, on the TCP order path (Ch. 68).
- **FIX (Financial Information eXchange) — tag=value text, the slow case.** A *text* protocol: `8=FIX.4.2|35=D|55=AAPL|...` (tag=value pairs, SOH-delimited). Human-readable, flexible, ubiquitous for *order entry* and some feeds — but **slow to parse** (text scanning, field lookup, string→number conversion — Ch. 37). Where latency matters, FIX is parsed with optimized field-scanning (not a generic parser), or replaced by SBE (the FIX community's binary encoding), typically carried over the FIXP session layer (FIXP is session establishment/sequencing/recovery, not an encoding). Branch-free integer/decimal parsing (§70.4.1) is critical for the numbers in FIX.
- **FAST (FIX Adapted for STreaming) — compressed binary.** A bit-packed, template-based compression of FIX for bandwidth-constrained feeds — *complex* to decode (stop-bit-encoded fields, operators, presence maps — bit manipulation, Ch. 37). Lower bandwidth, higher decode CPU; used where bandwidth is the constraint. Decoding is bit-level work.

The decoding-strategy spectrum: **fixed binary (ITCH/SBE/OUCH) → overlay a struct, ~free** (the fast, common case); **text (FIX) → optimized field scanning + branch-free number parsing** (slower, Ch. 37); **compressed (FAST) → bit-level decode** (complex). Most of the latency-critical work is the fixed-binary overlay (§70.2.2).

### 70.2.2 Fixed-layout structs vs bit-fields; endianness

Decoding fixed binary fast — the in-place overlay:

- **Overlay a fixed-layout struct (the zero-copy core).** For a fixed-binary message, define a struct matching the wire layout (exact field sizes, packed, Ch. 11) and access the message as that struct *in place* in the receive buffer — no copy, no parse, just typed field access. The subtlety (Ch. 26): a naive `reinterpret_cast<Msg*>(buf)` and dereference is **strict-aliasing UB** *and* may be **misaligned** (the wire field isn't necessarily aligned in the buffer). Do it safely, one of two ways: `memcpy`/`std::bit_cast` (Ch. 26) the field bytes into a typed local (which the compiler folds to a load — §70.5), or create the struct over the bytes with C++23's `std::start_lifetime_as<Msg>(p)` — or its C++20 `memmove`+`launder` polyfill, both zero instructions (Ch. 26 §26.4.2, §26.5) — on a `#pragma pack(1)` overlay struct (alignment 1, so any datagram offset is fine; `alignas` cannot *weaken* an alignment, so an unpacked struct over an unaligned datagram is not an option). Ch. 23 generates the per-field form from the venue schema. Zero-copy means *no payload copy*, not *no field load*.
- **Endianness (Ch. 37).** ITCH/OUCH/MoldUDP64 and most legacy binary feeds are **big-endian** (network byte order); x86 is little-endian — so their multi-byte fields must be **byte-swapped** on read (`std::byteswap` — C++23, or `bswap`/`ntohl` — one instruction, Ch. 37). SBE, by contrast, defaults to `byteOrder="littleEndian"` (CME MDP 3.0 and iLink 3 are explicitly little-endian) precisely so an x86 overlay load is swap-free — the schema's `byteOrder` attribute / the generated accessors decide, so swap only where the protocol says so. Forgetting a required swap silently reads garbage. Combine the swap with the `memcpy`/`bit_cast` load.
- **Fixed-layout structs vs C bit-fields.** For sub-byte fields (flags, packed values), prefer **explicit shifts/masks** (Ch. 37) over C bit-fields — bit-field layout is implementation-defined and codegen is often poor (Ch. 11, 37). Define the field offsets/masks explicitly and extract with shifts.
- **Variable-length fields — bounds-check every one (Ch. 100).** Many messages have variable-length parts (a count then N entries, a length-prefixed string). You **must** validate the length/count against the *actual buffer size* before reading — a length field that says "200 bytes" in a 50-byte packet is a buffer overflow if trusted (§70.6, Ch. 100). Every variable-length read is bounds-checked against the remaining buffer.
- **Generated decoders (SBE).** SBE (and FIX binary) provide schema-driven *code generators* that emit the overlay accessors (with the swaps and offsets correct) — use them rather than hand-writing offset arithmetic (less error-prone), but understand they produce the same in-place-load codegen (§70.5).

### 70.2.3 Redundant A/B multicast feeds

Market data is unreliable UDP multicast (Ch. 68), so reliability is built at the *feed-handler* level via redundancy and sequencing:

- **Sequence numbers.** Every message (or packet) carries a **sequence number**; the feed handler tracks the expected next sequence per feed. A gap (received seq > expected) means **packets were dropped** (Ch. 68); a duplicate (seq < expected) is ignored. Sequence tracking is how you *detect* loss on an unreliable feed.
- **A/B redundant feeds (Ch. 68).** The exchange sends the *same* sequenced data on **two independent multicast streams** (A and B, often different network paths). The feed handler joins **both** and **arbitrates**: process each sequence number from **whichever feed delivers it first**, and use the *other* feed to **fill a gap** (a packet dropped on A likely arrives on B). This turns two lossy feeds into one near-lossless stream — the standard market-data reliability mechanism (§70.4.3).
- **Gap recovery is Ch. 72's subject.** When a sequence is missing from *both* A and B (a real loss), the decoder's job ends: it flags the gap, keeps the contiguous prefix flowing, and hands the gap off the hot path to the recovery machinery — retransmission request, snapshot rebuild, trading through the gap — which Ch. 72 develops in full.
- **Single logical stream out.** The arbitration layer presents a *single, in-order, gap-free* (once Ch. 72's recovery has filled it) message stream to the book builder (Ch. 32) — hiding the A/B/duplicate/reorder complexity. This is the feed handler's core job: unreliable multicast in, clean sequenced stream out.

### 70.2.4 Mechanical sympathy for UDP microbursts: hardware RX rings, software reorder buffers, market-open drops

The hardest operational reality — **microbursts** (Ch. 68): at the open or on a big move, the exchange floods you with thousands of packets in *microseconds*, and if anything in the receive path can't keep up, you **drop** packets (gaps, recovery, stale book). Two mechanisms are all the model needs. First, the NIC DMAs incoming packets into a **ring of RX descriptors** in host memory (Ch. 75) and **drops** (`rx_missed`/`rx_no_buffer` in `ethtool -S`) when the ring fills faster than the host drains it — so survival is a race between the burst rate and the drain rate, decided by ring depth, how many packets each drain pulls (`recvmmsg`, busy-poll, bypass — Ch. 63, 54, 82) and how fast decode runs (§70.3). Second, UDP can arrive **reordered** when the network uses multiple paths, and the A/B lines are independent streams that interleave (RSS keeps one flow on one queue, so a multi-queue NIC is *not* a reordering cause — steer the feed to the decoder's queue for locality, Ch. 75, 77), so a *missing* sequence is not yet a *lost* one: the handler holds out-of-order arrivals in a bounded, sequence-indexed **reorder window** and declares a gap only when the window or its timeout expires. The operational checklist — ring sizing, batched draining, drop counters at every layer, testing under a replayed open — is **§70.4.4**; what happens once a gap is declared is **Ch. 72**.

The model: **decode is zero-copy in-place parsing of (mostly fixed-binary) wire formats — overlay structs with byte-swaps and bounds checks — fast enough to keep up with microbursts; reliability comes from sequence numbers + A/B feed arbitration + gap recovery; and surviving the market-open microburst needs a big NIC RX ring, fast batched draining, a software reorder buffer, and drop detection at every layer. The wire is untrusted — bounds-check everything (Ch. 100).**

## 70.3 Measure it: decode latency per protocol

Measure per-message decode latency across the encoding spectrum (§70.2.1): fixed-binary overlay (ITCH/SBE) vs text (FIX). The headline: fixed-binary overlay is ~free; text parsing is an order of magnitude more — driving the "binary for the hot feed" conclusion.

```cpp
// decode.cpp — decode latency: fixed-binary overlay vs FIX text parse.
// Build: g++ -O2 -std=c++23 -march=native decode.cpp -o decode
//   (std::byteswap is C++23 <bit>: GCC 12+/libstdc++ 12, Clang 14+/libc++ 14 -- Appendix H;
//    on C++20 toolchains substitute __builtin_bswap64/__builtin_bswap32.)
// Run pinned:  taskset -c 2 ./decode
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <charconv>
#include <string>
#include <vector>
#include <chrono>
#include <bit>

// Fixed-binary "ITCH-like" AddOrder: type(1) seq(8) id(8) side(1) qty(4) sym(8) price(8), big-endian.
#pragma pack(push, 1)
struct AddOrder { std::uint8_t type; std::uint64_t seq, id; std::uint8_t side;
                  std::uint32_t qty; char sym[8]; std::uint64_t price; };
#pragma pack(pop)

struct Parsed { std::uint64_t seq, id, price; std::uint32_t qty; char side; };

// zero-copy decode: load fields in place with bswap (Ch. 26, 37) — no payload copy.
Parsed decode_binary(const std::uint8_t* p) {
    Parsed r;
    std::uint64_t seq; std::memcpy(&seq, p+1, 8);  r.seq = std::byteswap(seq);       // safe load + swap
    std::uint64_t id;  std::memcpy(&id,  p+9, 8);  r.id  = std::byteswap(id);
    r.side = char(p[17]);
    std::uint32_t qty; std::memcpy(&qty, p+18, 4); r.qty = std::byteswap(qty);
    std::uint64_t px;  std::memcpy(&px,  p+30, 8); r.price = std::byteswap(px);
    return r;
}
// FIX-like text decode: scan "55=AAPL|44=12345|38=100|54=1|..." — field scan + from_chars (§70.4.1).
// Every dereference is guarded by `s < end`: a truncated datagram must not read past the buffer (the bounds-check pitfall below).
Parsed decode_fix(const char* s, std::size_t n) {
    Parsed r{}; const char* end = s + n;
    while (s < end) {
        int tag = 0; s = std::from_chars(s, end, tag).ptr; if (s < end && *s=='=') ++s;
        if (tag == 44) { long long px = 0; s = std::from_chars(s, end, px).ptr; r.price = px; }
        else if (tag == 38) { unsigned q = 0; s = std::from_chars(s, end, q).ptr; r.qty = q; }
        else { while (s < end && *s != '\x01') ++s; }     // skip to SOH
        if (s < end) ++s;                                  // past SOH
    }
    return r;
}

int main() {
    constexpr long N = 5'000'000;
    // A ring of 1024 DIFFERENT messages (64 KiB binary / ~40 KiB text, L1d/L2-resident): decoding the
    // same message 5 M times lets the compiler hoist the decode out of the loop (GCC 13 did, and
    // printed 0.0 ns/msg); the asm barrier makes each decoded result a used value (Appendix L).
    constexpr std::size_t R = 1024;
    std::vector<std::uint8_t> bin(R * sizeof(AddOrder));
    std::vector<std::string> fix(R);
    for (std::size_t i = 0; i < R; ++i) {
        AddOrder m{}; m.type = 0x41; m.price = std::byteswap(std::uint64_t(12345 + i)); m.qty = std::byteswap(std::uint32_t(100 + i));
        std::memcpy(&bin[i * sizeof(AddOrder)], &m, sizeof m);
        fix[i] = "55=AAPL\x01" "44=" + std::to_string(12345 + i) + "\x01" "38=" + std::to_string(100 + i) + "\x01" "54=1\x01";
    }

    auto t0 = std::chrono::steady_clock::now(); std::uint64_t s1=0;
    for (long i=0;i<N;++i){ auto r=decode_binary(&bin[(i & (R-1)) * sizeof(AddOrder)]);
                      s1+=r.seq ^ r.id ^ r.price ^ r.qty ^ std::uint8_t(r.side); asm volatile("" : "+r"(s1)); }   // EVERY field is used
    auto t1 = std::chrono::steady_clock::now(); std::uint64_t s2=0;
    for (long i=0;i<N;++i){ const auto& f = fix[i & (R-1)]; auto r=decode_fix(f.data(), f.size());
                      s2+=r.price ^ r.qty; asm volatile("" : "+r"(s2)); }
    auto t2 = std::chrono::steady_clock::now();
    auto ns=[&](auto a,auto b){return std::chrono::duration_cast<std::chrono::nanoseconds>(b-a).count()/(double)N;};
    std::printf("binary overlay: %.1f ns/msg   FIX text: %.1f ns/msg  (s=%llu,%llu)\n",
                ns(t0,t1), ns(t1,t2), (unsigned long long)s1, (unsigned long long)s2);
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP, 2.9 GHz), GCC 13 `-O2 -std=c++23 -march=native`, Linux 6.18, pinned, turbo off (illustrative). The loop decodes a *different* message each iteration and consumes *every* decoded field — an earlier draft decoded one invariant message and used one field, and GCC hoisted the whole decode out of the loop (`imul rax, rax, 5000000`, 0.0 ns/msg):

```
   encoding                         ns / message    note
   fixed-binary overlay (ITCH/SBE)  ~2-3 ns         4 in-place loads + movbe/bswap — ~free (§70.2.2, §70.5); throughput-bound, messages overlap
   FIX text parse (from_chars)      ~60-100 ns      4 fields: delimiter scan + text→int conversion (§70.4.1) — ~30x more
   FAST (bit-unpacking)             not measured here — a stop-bit/presence-map decoder is more code than this listing; expect FIX-like cost (§70.2.1)

   binary is >10x faster to decode → use binary (SBE/ITCH) for the hot feed; FIX where flexibility wins.
```

Read it: **fixed-binary overlay decode is ~free** (~2-3 ns/msg — a few cycles, and independent messages overlap in the out-of-order window) — it's just a handful of in-place loads + byte-swaps (§70.5), no parsing, no copying, no allocation. **FIX text parsing is ~30× more** — every field requires scanning to the delimiter and converting text→number (`from_chars` — §70.4.1), even optimized. The conclusion drives real protocol choices: **latency-critical feeds use binary (ITCH/SBE)**; FIX is used where its flexibility/ubiquity matters (order entry to some venues, less-hot paths) and parsed with optimized field-scanning + branch-free number conversion (Ch. 71) when it must be fast. The binary number (a few ns) is what lets a handler keep up with a microburst (§70.2.4); a ~100 ns text parse falls behind. (And note: even the "free" binary decode must **bounds-check** variable-length fields and the message length itself — §70.6 — which this minimal example omits for the binary path, while the text parser guards every dereference with `s < end`, as production must.) The optimization target: drive the *common* message's decode toward the binary number, and ensure the decode loop vectorizes/branch-minimizes where it can (§70.5).

## 70.4 Techniques

### 70.4.1 `std::from_chars`/`to_chars`; branch-free integer/decimal parsing (now Ch. 71)

Fast number conversion — the text half of decoding, and the whole of order *encoding* — is **Ch. 71 — Text Protocols on the Hot Path: Branch-Free Parsing & Formatting**, which measures the full spectrum on one FIX NewOrderSingle: `strtoll` → `std::from_chars` → a `[[likely]]` digit loop → 8-digit SWAR → 16-digit `pmaddubsw`/`pmaddwd` (10 ns → 1.5 ns per field), decimal prices straight to scaled integers without a `double` (47 ns → 5 ns), `UTCTimestamp` both ways with integer civil-date arithmetic, SIMD delimiter scanning, jump-table and layout-pinned dispatch (the structural 1.4× that the number tricks alone do not give), the `psadbw` checksum, and the encode path from `to_chars` to a template with fixed-width patched fields (44 → 27 ns per message). The rules this chapter keeps: `from_chars`/`to_chars` at minimum, never `atoi`/`stringstream`; prices never through `double` (Ch. 35); `fast_float` only where a feed genuinely carries floating point.

### 70.4.2 Scatter/gather and zero-copy receive paths

Getting the bytes from NIC to decoder without copies:

- **Parse in place — no copy from the receive buffer.** Decode directly out of the socket/ring receive buffer (§70.2.2) — the overlay-struct/`bit_cast` loads read the buffer in place. Don't `memcpy` the packet to a "work buffer" first (a wasted copy — Ch. 8, 30). The packet's bytes are parsed where they landed.
- **Scatter/gather (`recvmmsg`, `readv` — Ch. 63).** `recvmmsg` receives *many* datagrams in one syscall (Ch. 63 — essential for draining a microburst, §70.2.4); `readv`/scatter-gather splits a message into header+body buffers without a copy. These cut syscalls and copies on the receive path.
- **Kernel-bypass zero-copy receive (Ch. 75, 82).** With kernel bypass (ef_vi/DPDK — Ch. 82) or `AF_XDP` (Ch. 81), packets DMA directly into user-space buffers the application reads — *true* zero-copy from wire to decoder, no kernel copy at all. The ultimate receive path for the hot feed; the decoder overlays its structs on the NIC's DMA buffer.
- **Process the batch through the pipeline (Ch. 48).** A batch of received messages flows as a batch through decode → book → strategy (Ch. 48's batching) — amortizing per-message overhead and self-correcting under burst (Ch. 48.4.2). Decode is the first batched stage.
- **NIC hardware timestamps (Ch. 75, 78).** Capture the NIC's hardware receive timestamp (Ch. 75) per packet for accurate latency measurement (Ch. 20, 78) and sequencing — read it from the packet metadata, not a software clock (Ch. 20).

### 70.4.3 A/B line arbitration; sequence-gap detection (recovery → Ch. 72)

The reliability core (§70.2.3) — turning unreliable A/B multicast into a clean stream:

- **Sequence tracking per feed.** Maintain the expected-next sequence; for each received message, compare: **in-order** → process + advance; **duplicate** (seq < expected, already seen) → drop; **gap** (seq > expected) → a missing range → buffer the out-of-order message (reorder buffer — §70.4.4) and try to fill from the other feed.
- **A/B arbitration (take-first, fill-gaps).** Join both feeds; process each sequence from whichever arrives first (a per-sequence "have I processed this?" check — a sliding window/bitmap — Ch. 37). A packet missing on A is likely present on B — so the *combination* is near-lossless. The arbitrator dedups across feeds and emits each sequence exactly once, in order.
- **Bounded reorder window; then hand off (Ch. 72).** Buffer out-of-order messages in a window indexed by sequence (the mechanics — sizing for the reorder distance, the timeout — are §70.4.4); when the window or its timeout expires with the sequence still missing from *both* feeds, declare a gap and **hand it to the recovery subsystem — Ch. 72** (retransmission for small recent gaps, snapshot rebuild for large ones, and what the strategy does on a stale book meanwhile — §72.4.2–§72.4.3). The hot path's job ends at detection: flag the gap, keep delivering the contiguous prefix, never block on recovery.
- **Single clean stream out (§70.2.3).** The arbitration layer hides all of this, emitting one in-order, deduped stream — gap-free once Ch. 72's recovery has spliced the missing sequences back in — to the book builder (Ch. 32). Test it hard with injected drops/reorders/duplicates (Ch. 53 fuzzing, Ch. 107 replay).

### 70.4.4 Tuning RX ring sizes, managing UDP reordering in software ring buffers, detecting hardware packet drops

Surviving the microburst (§70.2.4) — the operational tuning:

- **Size the NIC RX ring for the worst microburst (Ch. 75).** `ethtool -G <iface> rx <N>` increases the RX descriptor ring so the NIC can buffer a burst the host hasn't drained yet. Too small → `rx_missed`/`rx_no_buffer` drops at the open. Measure the worst burst (captured open — Ch. 107) and size the ring (and `SO_RCVBUF` — Ch. 68) to absorb it. This is a top market-open drop cause.
- **Software reorder buffer (window).** A buffer indexed by sequence (a ring or window) holding out-of-order arrivals until the gaps fill — reassembling the in-order stream from reordered/A-B-interleaved packets (§70.2.3). Back the window's byte storage with a double-mapped `memfd` ring (Ch. 34 §34.4.3) so a datagram that straddles the ring's end is still one contiguous span for the decoder. Size the window for the expected reorder distance; bound it with a timeout (→ recovery). This is application-level (the NIC delivers packets; you reorder by sequence).
- **Drain fast and in batch (§70.4.2).** The host must drain the RX ring faster than the burst fills it — `recvmmsg`/busy-poll/bypass (Ch. 54, 63, 82). A slow decode (§70.3 — use binary!) or a slow downstream stage backs up the ring → drops. Keep the whole receive→decode pipeline fast enough for the burst.
- **Detect drops at every layer (Ch. 68).** NIC: `ethtool -S` (`rx_dropped`, `rx_missed`, `rx_no_buffer_count`). Socket: UDP `RcvbufErrors`/drops (`/proc/net/snmp`, `ss -u`). Application: sequence gaps (the definitive signal). Instrument all three, alert on any — and remember that a feed which has *stopped* (a lapsed multicast membership — Ch. 74) trips none of them: add per-channel last-packet age as the fourth signal. Ch. 113 is the full counter taxonomy by layer (and the forensics for when the counters are clean). **Test under replayed market-open load** (Ch. 108) — a drop that only happens at the real open is a production incident you should have caught in test.
- **NIC features for the burst (Ch. 75).** RSS/flow steering (direct the feed to the right queue/core), interrupt coalescing tuning (or busy-poll to avoid interrupts entirely — Ch. 54, 75), and hardware timestamping (Ch. 75). The NIC config (Ch. 75) and this decoding tuning together determine microburst survival.

## 70.5 Verify the codegen: parse loop

The zero-copy overlay's promise — "field access is just a load" — is verifiable, as is whether the parse loop stays branch-light. Snippets are **verified** Clang 18.1 output (Appendix L.3; `--target=x86_64-linux-gnu -O2 -march=x86-64-v3`).

**In-place field load + byte-swap → a load + `bswap`, or a single `movbe` (no copy).** The `memcpy`-into-local + `byteswap` (Ch. 26, 37) compiles to a load and a swap — *not* an actual memcpy call, *not* a payload copy — and since x86-64-v3 includes MOVBE, the swap is fused into the load or the store itself. `Parsed` is returned through a hidden pointer (sret), so in `decode_binary` `rdi` is the result and `rsi` is `p`:

```asm
; r.price = byteswap( load 8 bytes at p+30 ):     (-march=x86-64-v3 — MOVBE available)
        mov     rcx, qword ptr [rsi + 30]   ; load the wire field IN PLACE (no copy)
        movbe   qword ptr [rdi + 16], rcx   ; byte-swapping STORE into r.price — the swap is fused
; a bare accessor  uint64_t load_be64(const uint8_t* p)  is ONE instruction:
        movbe   rax, qword ptr [rdi + 1]    ; byte-swapping LOAD: big-endian → host (Ch.37)
; without MOVBE (-march=x86-64-v2) the same source is the two-instruction form:
        mov     rcx, qword ptr [rsi + 30]
        bswap   rcx                         ; big-endian → host — one instruction
        mov     qword ptr [rdi + 16], rcx
```

(Clang goes further on the adjacent `seq`/`id` fields: one 16-byte `vmovdqu` + `vpshufb` swaps both at once.) This is the proof that **zero-copy decode of a fixed-binary field is a load + a swap — or one `movbe`** — the `memcpy`/`bit_cast` (Ch. 26) folded away, no `call memcpy`, no buffer copy. (A `reinterpret_cast` deref would be UB and possibly a misaligned-access fault — §70.6; the `memcpy` form is safe *and* the same codegen.)

**Bounds check compiles to a cheap compare+branch (predictable).** A `if (offset + len > buf_size) return error;` before a variable-length read is a compare and a (well-predicted, rarely-taken) branch — *cheap*, and the §70.6/Ch. 100 safety is nearly free:

```asm
        lea     rax, [rcx + r8]             ; offset + len
        cmp     rax, rdx                    ; > buf_size ?
        ja      .Lreject                    ; rarely taken (valid packets pass) — predicted (Ch.15)
        ... read the field ...
```

**The dispatch on message type.** The per-message `switch (type)` should be a jump table or a data lookup (Ch. 15–16), not a mispredicting branch chain — verify it (Ch. 16.5):

```asm
        movzx   eax, byte ptr [rdi]         ; message type byte
        jmp     qword ptr [.Ljt + 8*rax]    ; jump table over message types (Ch.16)
```

The verification habits: **confirm field access is a `mov`(+`bswap`) or a `movbe`, not a `call memcpy` or a misaligned trap** (the zero-copy + safe-load promise — §70.2.2); **confirm bounds checks are cheap predicted branches** (so safety is ~free — §70.6); **confirm the type dispatch is a jump table/lookup** (Ch. 16), not a branch chain; and for bulk fixed-width parsing, **confirm vectorization** (Ch. 39) where it applies. The decode loop is the hottest loop in the system — read its asm and ensure it's loads + swaps + predicted branches, nothing more.

## 70.6 Pitfalls & anti-patterns: unaligned reads, trusting wire input *(ties Ch. 100)*

- **Trusting wire input (the cardinal feed-handler / security sin — Ch. 100).** Reading a length/count field and using it *without bounds-checking against the actual buffer* → buffer over-read/overflow the first time a packet is malformed/truncated/hostile (a crash, or an exploit — Ch. 100). **Every variable-length field is bounds-checked** against the remaining buffer (§70.2.2, §70.5). The wire is untrusted input; this *is* the security boundary (Ch. 100).
- **Unaligned reads via `reinterpret_cast` deref (Ch. 26).** `*reinterpret_cast<uint64_t*>(buf+offset)` is **strict-aliasing UB** (Ch. 26) *and* may be a **misaligned access** (the wire field isn't aligned) — UB/fault on some targets, slow on others. Use `memcpy`/`std::bit_cast` for fields, or `std::start_lifetime_as` (C++23; polyfill in Ch. 26 §26.4.2) on a packed overlay struct — same codegen (§70.5), defined, alignment-safe.
- **Forgetting the byte-swap (endianness).** Reading a big-endian wire field without `byteswap` (Ch. 37) silently yields garbage values (a price of 0x4500000000000000 instead of 0x45). Swap every multi-byte field; test against real wire data.
- **Copying the packet before parsing.** A `memcpy` of the packet into a "work buffer" before decoding wastes the copy (Ch. 8, 30) — defeating zero-copy. Parse in place (§70.4.2).
- **Slow number parsing (`atoi`/`stringstream`/`scanf`).** Legacy conversions are slow, allocating, and locale-dependent — a hot-path disaster for FIX/text. Use `from_chars`/`to_chars` / branch-free parsing (§70.4.1, Ch. 37).
- **Parsing price through `double` (Ch. 35).** Converting a decimal price to `double` introduces representability error (Ch. 35). Parse directly to a **scaled integer** (§70.4.1, Ch. 35).
- **Ignoring drops/gaps (no A/B, no recovery).** Treating UDP multicast as reliable — no sequence tracking, no A/B arbitration, no gap recovery — means silent data loss → a wrong book → wrong trades. A/B + sequence + recovery is *core* logic (§70.4.3), not optional.
- **Undersized RX ring / falling behind the burst.** A small NIC RX ring or a slow decode that can't keep up with the microburst → drops at the open (§70.2.4, §70.4.4) — exactly when it matters. Size the ring, use binary decode, drain in batch, and **test under replayed market-open load** (Ch. 108).
- **C bit-fields for wire layout (Ch. 11, 37).** Implementation-defined layout and poor codegen — wire decoding needs *exact, portable* layout. Use explicit shifts/masks (Ch. 37).
- **Unbounded reorder buffering.** Waiting indefinitely for a missing sequence (no window/timeout) → unbounded latency. Bound the reorder window + timeout → recovery (§70.4.3-4).
- **Not fuzzing the decoder (Ch. 53, 100).** A decoder never fed malformed input *will* mishandle it in production. **Fuzz it (libFuzzer/AFL++ + ASan/UBSan — Ch. 53)** with malformed/truncated/hostile packets — the primary defense for this untrusted-input path (Ch. 100).

## 70.7 Exercises & checklist

**Exercises**

1. **Binary vs FIX decode.** Build `decode.cpp`; compare fixed-binary overlay vs FIX text decode latency. Confirm binary is >10× faster (§70.3). Disassemble the binary decode (§70.5) — is each field a `mov`+`bswap` or a `movbe` (not a memcpy call)? Is the type dispatch a jump table?
2. **The bounds-check.** Add a variable-length field (a count + N entries) to the binary message; decode it *without* a bounds check, then feed a packet whose count exceeds the buffer — reproduce the over-read (ASan — Ch. 53). Add the bounds check; confirm it's a cheap predicted branch (§70.5) and rejects the bad packet (§70.6, Ch. 100).
3. **A/B arbitration.** Build two sequenced UDP streams of the same data; drop random packets from each; write an arbitrator that joins both, dedups, reorders, and emits one clean in-order stream. Inject a gap in *both* and trigger a recovery path. Measure recovery latency (§70.4.3).
4. **Microburst survival.** Replay a captured (or synthetic) market-open burst into your receiver; tune NIC RX ring (`ethtool -G`) + `SO_RCVBUF` + `recvmmsg` until zero drops (`ethtool -S` rx_missed = 0). Measure decode latency under burst (§70.4.4).
5. **Fuzz the decoder.** Write a libFuzzer harness (ASan+UBSan — Ch. 53) for your message decoder; let it find a mishandled malformed packet. Fix and re-fuzz. This is the Ch. 100 untrusted-input defense (§70.6).

**Checklist — zero-copy wire decoding**

- [ ] Latency-critical feeds use **fixed-binary encodings** (ITCH/SBE — >10× faster decode than FIX text — §70.3); FIX/text uses **`from_chars`/branch-free** parsing (Ch. 37, §70.4.1) and prices parse to **scaled integers** (Ch. 35).
- [ ] Decode is **zero-copy in place** — overlay structs read via **`memcpy`/`bit_cast`** (alignment-safe, defined — Ch. 26) with **byte-swaps** (Ch. 37), **no payload copy**; verified in the asm as `mov`+`bswap` or `movbe` (§70.5).
- [ ] **Every variable-length field is bounds-checked** against the buffer (the untrusted-input boundary — §70.6, Ch. 100); the decoder is **fuzzed** (libFuzzer + ASan/UBSan — Ch. 53).
- [ ] Reliability is built in: **sequence tracking + A/B feed arbitration** on the hot path, **gap recovery** (snapshot/retransmit) handed off to Ch. 72's machinery, producing one **clean in-order stream** to the book (Ch. 32) — with a **bounded reorder window + timeout** (§70.4.3).
- [ ] The microburst is survived: **NIC RX ring sized** (`ethtool -G`), **`SO_RCVBUF` sized** (Ch. 68), **batched draining** (`recvmmsg`/busy-poll/bypass — Ch. 63, 82), and **drops detected at NIC/socket/app layers** (§70.4.4) — tested under **replayed market-open load** (Ch. 108).
- [ ] The receive path is **zero-copy** (parse in the RX/DMA buffer; kernel-bypass where ultra-hot — Ch. 82); **NIC hardware timestamps** captured (Ch. 75, 78).
- [ ] Type dispatch is a **jump table/data lookup** (Ch. 16), not a branch chain; bulk fixed-width parsing **vectorizes** where applicable (Ch. 39) — verified (§70.5).
- [ ] **No `reinterpret_cast`-deref unaligned reads, no `atoi`/`stringstream`, no `double` prices, no C bit-fields** for wire layout (§70.6).

## 70.8 References

- The exchange protocol specifications — Nasdaq **ITCH/OUCH** (and MoldUDP64), **FIX** and **FIXP/SBE** (FIX Trading Community), CME **MDP 3.0 / SBE**, and **FAST** — the encodings of §70.2.1.
- D. Lemire et al., *simdjson* and *fast_float* — branch-free/SIMD parsing techniques and number conversion (§70.4.1, ties Ch. 37–39).
- ISO C++ / cppreference — `std::from_chars`/`to_chars`, `std::bit_cast`, `std::byteswap` — the safe, fast primitives (§70.4.1, §70.2.2).
- The exchange feed-handler / A-B-arbitration and gap-recovery documentation (Nasdaq, CME) and "Mechanical Sympathy" feed-handler writeups — reliability and microburst handling (§70.4.3-4).
- Ch. 100 references (secure coding, fuzzing) — treating wire data as untrusted input (§70.6).

## 70.9 Additional Reading

- HFT feed-handler engineering talks and the SBE/simple-binary-encoding project docs — production zero-copy decoding patterns.
- CME/Nasdaq market-data handler and microburst tuning guides — RX ring/reorder/drop handling (§70.4.4).
- Ch. 26 (*Aliasing/`bit_cast`*) — safe in-place field loads; Ch. 37 (*Bit Tricks*) — byte-swap/branch-free parsing; Ch. 32 (*Order Book*) — what the decoder feeds; Ch. 68 (*Sockets/Multicast*) — the A/B transport; Ch. 75 (*NIC*) — RX ring/timestamping/RSS; Ch. 82 (*Kernel Bypass*) — zero-copy receive; Ch. 100 (*Secure Programming*) — untrusted input/fuzzing; Ch. 107 (*Capture/Replay*) — capturing the wire and replaying the open.
- Ch. 72 (*Reliable Multicast & Feed Recovery*) — the recovery deep-dive this chapter's gap detection points at: retransmission, snapshots, and rebuilding the book without stalling the hot path.
- **Appendix E** — kernel-stack vs bypass RX numbers; **Appendix F** — ITCH/FIX/SBE/A-B/feed glossary.

---

*Next: Ch. 71 — Text Protocols on the Hot Path: Branch-Free Parsing & Formatting. The binary overlay above is ~free; the text that remains on the hot path — FIX order entry, execution reports, text feeds — is not, and §70.4.1's pointer is cashed there: the integer-parse spectrum from `strtoll` to SIMD measured on one NewOrderSingle, decimals straight to scaled integers, timestamps, tag scanning and layout-pinned dispatch, and the encode path that builds the order message into a template.*
