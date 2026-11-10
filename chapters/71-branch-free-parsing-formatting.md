# Part IX — Market Data, NIC & Fabric

# Chapter 71 — Text Protocols on the Hot Path: Branch-Free Parsing & Formatting

> **Prerequisites:** Ch. 70 (wire handling, the binary overlay, and §70.3's FIX decode measurement — the baseline this chapter beats), Ch. 35 (scaled-integer prices — the target type of every price parse), Ch. 37 (bit tricks — SWAR is bit tricks applied to bytes), Ch. 39 (SIMD — `pcmpeqb`/`movemask`/`pmaddubsw`), Ch. 15 (branch mispredicts — what a per-byte loop pays), Ch. 21 §21.4.2 (`constexpr` tables — the digit-pair table), Ch. 3 (a parse of the *same* message 20 M times measures nothing).
>
> **Leads into:** Ch. 106 §106.4.2 (the gateway's template encoder — §71.4.6 is its arithmetic), Ch. 101 (malformed input as an attack: every parser here rejects at the gate), Ch. 98 (deferred formatting for logs — the opposite discipline from an order encode that cannot be deferred), Ch. 118 §118.3's "order encode → TX" stage. Ch. 70 §70.4.1 is now a pointer to this chapter.

---

## 71.1 Why it matters: FIX is on the order path at most venues, and the encode stage has had no chapter

Ch. 70's verdict stands — the hot *market-data* feed is binary (ITCH/SBE: 2–3 ns per message), text is 30× slower — but text does not go away: **FIX is the order-entry protocol at most venues**, drop copy and many execution reports arrive as FIX, several derivatives and FX feeds are text or FIX/FAST, and every one of those sits on the tick-to-trade path at the *encode* end. Ch. 118 §118.3 budgets "order encode → TX" at ~500 ns p50 with the encode itself a minority; Ch. 106 §106.3 measured a template encoder at 50–95 ns per NewOrderSingle and used it without explaining it. Ch. 70 §70.3 measured `from_chars`-based field scanning at ~60–100 ns per message and told the reader to use "optimised field scanning + branch-free number conversion" without showing any. This chapter shows it, and measures the whole spectrum on one 145-byte NewOrderSingle: integer conversion from `strtoll` to a 16-digit SIMD chain (10 ns → 1.5 ns), decimal prices straight to scaled integers without a `double` (47 ns → 5 ns), `UTCTimestamp` both ways, the tokeniser (which turns out to be the floor, not the number conversion), a layout-pinned parser that halves the message cost, the checksum, and the encode path from `to_chars` to a pre-built template with patched fixed-width fields (44 ns → 27 ns per message). The lesson that organises it: **the per-field overhead — find the delimiter, dispatch on the tag — dominates a text message; the number conversion is nearly free once it is branch-free**, which is why the biggest wins are structural (parse what the layout tells you to expect; encode into a template) and the SWAR/SIMD tricks are what make the fixed-width fields cost nothing at all.

## 71.2 Mental model

### 71.2.1 Where text sits on the hot path, and its budget

Inbound: a FIX execution report or reject on the order session (the fill that updates position before the next decision — Ch. 105), a text market-data feed, a FAST-decoded stream. Outbound: every NewOrderSingle, cancel and replace on a FIX session, which the gateway (Ch. 106) sends synchronously — the encode cannot be deferred to a background thread the way a log line can (Ch. 98), because the bytes *are* the order. The budget for both is the tens of nanoseconds that the binary paths cost, and the chapter's result is that a text message can be brought to ~60 ns inbound and ~27 ns outbound when its layout is known.

### 71.2.2 The cost of a text field: scan, convert, validate — per field, not per byte

A `tag=value<SOH>` field costs: finding `=` and `SOH` (a scan), converting the tag (1–3 digits) and dispatching on it, converting the value, and validating. Written byte-at-a-time each of those is a loop with a data-dependent exit — a mispredict per field at ~15–20 cycles (Ch. 15) — and the §71.3 table says that for a 16-field message this per-field overhead is ~4–5 ns per field *whatever the conversion costs*. Two consequences: the fastest number parser in the world saves ~3 ns per numeric field (six of sixteen fields here), and the structural wins — skipping the scan when the field is fixed-width, skipping the dispatch when the tag order is known, not parsing the seven fields the session layer validates anyway — are worth more than any conversion trick. The conversion tricks still matter, because fixed-width numeric fields (ITCH-style text timestamps, OUCH-like fixed fields, the date half of a `UTCTimestamp`, SBE-text hybrids) are where the scan disappears and the conversion *is* the cost.

### 71.2.3 Word- and vector-parallel digit conversion

Eight ASCII digits are eight bytes; load them as one `uint64_t`, subtract `0x3030…` to turn `'0'..'9'` into `0..9` in every byte, and three multiply-add-mask steps combine neighbouring lanes: `v*10 + (v>>8)` turns byte pairs into two-digit values in 16-bit lanes, `v*100 + (v>>16)` makes four-digit values in 32-bit lanes, `v*10000 + (v>>32)` makes the eight-digit result — **SWAR** (SIMD within a register), 3 multiplies, no branch, no loop (Lemire's and Muła's formulation). Validation is the same trick: `(v & 0xF0F0…) == 0x3030…` and `((v − 0x3030…) + 0x0606…) & 0xF0F0… == 0` together prove every byte was a digit. Sixteen digits fit an SSE register and the same combine runs as `pmaddubsw` (pairs), `pmaddwd` (quads), `packusdw` + `pmaddwd` (octets), then one scalar multiply-add — four vector instructions. A *variable-width* field of up to eight digits becomes fixed-width by shifting the loaded word so the digits are right-aligned (the field length is known from the scan that found the SOH), at the cost of a shift and a masked subtract. These are the "parse 8 digits at once" Ch. 70 §70.4.1 named; §71.4.1 has the code and §71.5 the asm.

### 71.2.4 Decimals, timestamps and checksums as integer problems

A FIX price `187.25` is the integer 18725 with the decimal point two digits from the end; at a fixed scale of 10⁴ it is `18725 × 100 = 1872500` — a digit loop that records where the `.` was and one multiply by a power-of-ten table at the end, never a `double` (Ch. 35: `187.25` is not representable, and `llround(strtod(...) * 1e4)` is slow *and* a rounding bug waiting for a price like `0.07`). A `UTCTimestamp` `20261110-14:30:00.123` is fixed-width: the date is an 8-digit SWAR parse, the time three digit pairs, the milliseconds three digits, and days-from-civil (Hinnant's branch-free algorithm) turns `(y, m, d)` into a day count in a dozen integer operations — and the reverse formats with digit pairs from a `constexpr` table. The FIX checksum (`10=nnn`) is the byte sum modulo 256 — `psadbw` sums sixteen bytes against zero in one instruction. None of this needs a branch per byte.

## 71.3 Measure it: the spectrum on one NewOrderSingle

**Machine, toolchain, flags.** `fixparse.cpp` (every parser and encoder in §71.4 plus the harness: 330 lines) was **run on the author's 4-vCPU KVM guest** (Appendix L.2: TSC 2.70 GHz, Linux 6.18, no isolation), GCC 13.3 `-O2 -std=c++20 -march=native` and Clang 18.1 `-march=x86-64-v3`, pinned, 20 M iterations per row over a ring of **1024 different messages** (sequence, time, ClOrdID, quantity and price vary; an earlier draft parsed one message and both compilers hoisted the parse out of the loop — Ch. 3, and Ch. 70 §70.3's own lesson), with an asm barrier on every output buffer so the bytes written are the result. The message is a 145-byte FIX 4.2 NewOrderSingle (16 fields, 9 of them extracted). VM-measured; run-to-run noise is ±10–15 % on the longer rows, the ratios are stable.

```
   ns per operation                                                     GCC 13.3    Clang 18.1
   (a) integer parsing
   strtoll, '1500' (4 digits)                                              9.6        10.1
   std::from_chars, 4 digits                                               4.7         4.9
   digit loop with [[likely]], 4 digits                                    2.9         2.2
   SWAR up-to-8 (length known from the scan), 4 digits                     1.7         1.4    <- 3x from_chars; the same code for 1-8 digits
   std::from_chars, 8 digits (fixed width)                                 6.8         6.4
   SWAR 8 digits                                                           1.5         1.5    <- 4.5x
   SWAR 8 digits + all_digits8 validation                                  2.0         2.0    <- validation costs 0.5 ns
   std::from_chars, 16 digits                                             11.4        10.8
   SIMD 16 digits (pmaddubsw/pmaddwd chain)                                1.5         1.5    <- 7.5x
   (b) decimal price '187.25' -> int64 at scale 1e4
   strtod + llround(x * 1e4)                                              46.7        46.3    <- and a rounding bug
   std::from_chars<double> + llround(x * 1e4)                             10.7        11.0
   integer digits + decimal position (parse_price_e4)                      4.8         5.6    <- 10x strtod, no double anywhere
   (c) UTCTimestamp
   parse '20261110-14:30:SS.sss' -> ns since epoch                         8.0         9.5
   format ns -> 'YYYYMMDD-HH:MM:SS.sss' (digit pairs, fixed width)         9.9         4.8
   (d) the whole 145-byte message, 9 hot fields extracted
   scalar SOH scan + from_chars  (Ch. 70 §70.3's method)                  81.0        82.8
   SIMD SOH scan per field + from_chars                                   76.5        76.4
   SIMD SOH scan per field + SWAR integers                                76.2        78.9    <- conversion is not where the time is
   one-pass SOH/'=' bitmasks + tzcnt walk + SWAR                         110.8        93.5    <- the mask build costs what the walk saves, at 145 bytes
   layout-pinned: expected tag order, SWAR, generic fallback              58.4        58.6    <- 1.4x: no tag parse, no dispatch, no scan on fixed-width fields
   (e) checksum over 141 bytes
   byte loop                                                              37.6         7.1    <- Clang auto-vectorised it; GCC -O2 did not
   psadbw                                                                 6.7         6.5
   (f) encoding
   to_chars, one u32 (4-7 digits)                                          4.4         6.1
   digit-pair table, one u32 (4-7 digits)                                 10.1         6.3    <- libstdc++'s to_chars already uses digit pairs; the hand-rolled one loses
   fixed-width 8 digits, zero-padded (4 pair lookups, no length)           4.7         3.4
   whole NewOrderSingle from scratch (to_chars, memmove for 9=)           44.3        40.7
   whole NewOrderSingle from a template (fixed-width patches + checksum)  27.4        23.8    <- 1.6x; Ch. 106's encoder
```

Six readings. **Branch-free conversion is 3–7.5× faster than `from_chars`** and `from_chars` is 2× faster than `strtoll` — but note the absolute numbers: the entire gain on a 4-digit field is 3 ns. **The decimal price path is 10× `strtod`** and, more importantly, correct: no `double` is formed. **The message is the tokeniser**: scalar scan, SIMD scan and SWAR conversion all land at 76–81 ns because sixteen `tag=value` fields cost ~5 ns each to find and dispatch; the one-pass bitmask tokeniser (simdjson's idea) *loses* here, because two compares per 16 bytes plus a bit walk cost more than it saves at 145 bytes with short fields — it pays at kilobyte messages. **The layout-pinned parser is the structural win** (58 ns, 1.4×): a venue's NewOrderSingle has a fixed tag order per session, so expect each tag with one 3-byte compare, skip the scan on fixed-width fields (the timestamp), and fall back to the generic parser when the expectation fails. **The checksum row is a codegen lesson** (§71.5): Clang vectorised the byte loop, GCC 13 at `-O2` did not — write the `psadbw` form or check the asm. **The encode path**: `to_chars` is already a digit-pair implementation (the hand-rolled table loses to it), fixed-width fields need no length computation, and a pre-built message template with fixed-width fields patched in place beats building the message by 1.6× — the 27 ns here is the ~24–50 ns Ch. 106 §106.3 measured for its encoder.

## 71.4 Techniques

### 71.4.1 Integers: `from_chars` → digit loop → SWAR-8 → SIMD-16, with validation

```cpp
// fixparse.cpp (excerpt) — integer conversion, fastest last. All read exactly the bytes they are told to; the caller
// guarantees readability (the RX buffer has 16 bytes of slack after the message — §71.6).
static inline u32 parse8_swar(const char* p) {                           // exactly 8 ASCII digits -> value: 3 multiplies, no branch
    u64 v; std::memcpy(&v, p, 8);                                        // little-endian: the first digit is the low byte
    v -= 0x3030303030303030ull;                                          // '0' -> 0 in every byte
    v = (v * 10 + (v >> 8)) & 0x00FF00FF00FF00FFull;                     // pairs:  d0*10 + d1 in each 16-bit lane
    v = (v * 100 + (v >> 16)) & 0x0000FFFF0000FFFFull;                   // quads:  dd*100 + dd
    v = (v * 10000 + (v >> 32)) & 0x00000000FFFFFFFFull;                 // octet
    return u32(v);
}
static inline bool all_digits8(const char* p) {                          // every byte in '0'..'9': two masks, no loop
    u64 v; std::memcpy(&v, p, 8); u64 d = v - 0x3030303030303030ull;
    return ((v & 0xF0F0F0F0F0F0F0F0ull) == 0x3030303030303030ull) && (((d + 0x0606060606060606ull) & 0xF0F0F0F0F0F0F0F0ull) == 0);
}
static inline u32 parse_upto8_swar(const char* p, u32 len) {              // 1..8 digits, len known from the SOH scan: right-align, then as above
    u64 v; std::memcpy(&v, p, 8); v <<= (8 - len) * 8;                   // the unused low bytes become 0 ...
    v -= 0x3030303030303030ull << ((8 - len) * 8);                       // ... and '0' is subtracted only from the real digits
    v = (v * 10 + (v >> 8)) & 0x00FF00FF00FF00FFull; v = (v * 100 + (v >> 16)) & 0x0000FFFF0000FFFFull; v = (v * 10000 + (v >> 32)) & 0xFFFFFFFFull;
    return u32(v);
}
static inline u64 parse16_simd(const char* p) {                          // exactly 16 digits: four vector instructions and a scalar fma
    __m128i v = _mm_sub_epi8(_mm_loadu_si128((const __m128i*)p), _mm_set1_epi8('0'));
    v = _mm_maddubs_epi16(v, _mm_set_epi8(1, 10, 1, 10, 1, 10, 1, 10, 1, 10, 1, 10, 1, 10, 1, 10));   // 8 x (d*10 + d)      u16
    v = _mm_madd_epi16(v, _mm_set_epi16(1, 100, 1, 100, 1, 100, 1, 100));                             // 4 x (dd*100 + dd)   u32
    v = _mm_packus_epi32(v, v);                                                                        // back to u16 lanes (< 10000)
    v = _mm_madd_epi16(v, _mm_set_epi16(1, 10000, 1, 10000, 1, 10000, 1, 10000));                     // 2 x (dddd*10000 + dddd)
    return u64(_mm_cvtsi128_si32(v)) * 100000000ull + u64(_mm_extract_epi32(v, 1));
}
```

- **`std::from_chars`** (C++17) is the correct default: non-allocating, locale-free, no `errno`, 2× `strtoll`, and it *validates* (returns the end pointer and an error code). Use it for every field that is not hot enough to justify the next three bullets.
- **The digit loop** with `[[likely]]` on the continue branch is faster than `from_chars` on short fields (2–3 ns) because `from_chars` handles sign, overflow and bases; it still mispredicts at the exit on variable-width fields.
- **SWAR for 1–8 digits** is the hot-path form: fixed width needs nothing but the load; variable width needs the length, which the delimiter scan already computed. The `all_digits8` check is 0.5 ns and must run on *untrusted* input (Ch. 101) — a non-digit byte makes the arithmetic silently wrong, not loud.
- **SIMD for 9–16 digits** (sequence numbers, ITCH-text timestamps in nanoseconds, large order ids): the `pmaddubsw`/`pmaddwd` chain; AVX2 extends it to 32 digits in one register. Beyond that, a text field is a design error.
- **Sign and overflow**: FIX quantities and prices are unsigned in practice; where a sign is allowed, test the first byte and branch *once* (it is predictable); overflow is a validation failure at the gate (a 9-digit quantity is not an order).

### 71.4.2 Decimal prices straight to scaled integers; rejecting malformed input

```cpp
// "187.25" at scale 1e4 -> 1872500; rejects a second '.', any non-digit, and more fraction digits than the scale allows.
static inline bool parse_price_e4(const char* p, const char* e, i64& out) {
    i64 v = 0; int frac = -1;
    for (; p < e; ++p) { u8 c = u8(*p); if (c == '.') { if (frac >= 0) return false; frac = 0; continue; }
                         u8 d = u8(c - '0'); if (d > 9) return false; v = v * 10 + d; if (frac >= 0) ++frac; }
    if (frac > 4) return false; static constexpr i64 pw[5] = {10000, 1000, 100, 10, 1}; out = v * pw[frac < 0 ? 0 : frac]; return true;
}
```

Integers in, integer out, the scale a compile-time constant of the instrument (Ch. 35 §35.4.1); the only multiply is the final power-of-ten from a table indexed by how many fraction digits arrived. A price with more precision than the scale is *rejected*, not rounded — a venue that sends `187.255` on a 2-decimal instrument has sent something the strategy's tick arithmetic cannot represent, and rounding it silently is how a book ends up a tick off (Ch. 35 §35.6). The ITCH-style **implied decimal** (an integer field with a documented scale) needs no parse at all beyond §71.4.1 and a scale check. The `double` path (`strtod`, `from_chars<double>`) is slower by 2–10× *and* wrong: `187.25` is not a `double`, and `llround(x * 1e4)` is one ulp away from `1872499` for some inputs. `fast_float` is the right library when a feed genuinely carries floating point; it does not belong on a price.

### 71.4.3 `UTCTimestamp` to nanoseconds and back

`YYYYMMDD-HH:MM:SS.sss` (21 bytes, fixed width; FIX 4.4+ allows `.ssssss` and `.sssssssss` — the same code with a longer fraction). Parse: `parse8_swar` on the date, three digit pairs and three digits for the time (`d2(p) = (p[0]−'0')·10 + (p[1]−'0')`), then **days-from-civil** (Hinnant's algorithm: shift the year so March is month 0, compute the era and the day-of-era with four integer divisions by constants — which the compiler turns into multiplies — and no month table), giving `((days·86400 + hh·3600 + mm·60 + ss)·1000 + ms)·10⁶` ns: ~8–10 ns in §71.3, entirely integer, no `struct tm`, no `timegm` (which takes a lock in glibc and is not reentrant-fast). Format: `civil_from_days` (the inverse, also branch-free except one `mp < 10` select) and six `put2()` digit-pair copies into fixed positions — 5–10 ns. Two cautions: the venue's `SendingTime` is the *venue's* clock, compared against the local one only after Ch. 78's PTP offset is known (Appendix J.5's venue-timestamp table says what each venue's timestamp means); and the sequence-number and timestamp fields are the ones a resend (Ch. 106 §106.4.3) must *re-format*, which is why the template encoder keeps them fixed-width.

### 71.4.4 Tag scanning and dispatch: SIMD delimiter find, jump-table switch, bitmask tokenising, layout-pinned parsing

```cpp
// SIMD delimiter find: 16 bytes per compare; movemask + tzcnt gives the offset with no per-byte branch (Ch. 39 §39.4)
static inline const char* find_soh_simd(const char* p, const char* e) {   // requires p..e+15 readable: the RX buffer's slack
    const __m128i soh = _mm_set1_epi8(1);
    for (; p < e; p += 16) { u32 m = u32(_mm_movemask_epi8(_mm_cmpeq_epi8(_mm_loadu_si128((const __m128i*)p), soh))); if (m) return std::min(p + std::countr_zero(m), e); }
    return e;
}
// Generic parse: tag (1-3 digits) -> '=' -> value -> SOH; a dense switch on the tag is a jump table (Ch. 15 §15.4.2)
template <bool SIMD, bool SWAR> static inline Fields parse_msg(const char* p, const char* e) { /* §71.3 rows (d)1-3 */ }
// Layout-pinned parse: a session's NewOrderSingle has a FIXED tag order; expect each tag (one predictable 3-byte compare),
// scan only the values, skip the scan on fixed-width fields, fall back to the generic parser when the expectation fails.
static inline bool expect(const char*& p, const char* lit3) { bool ok = std::memcmp(p, lit3, 3) == 0; p += 3; return ok; }
static inline Fields parse_msg_pinned(const char* p, std::size_t n) {
    const char* e = p + n; Fields f{}; const char* q = p;
    auto skip = [&](const char*& r) { r = find_soh_simd(r, e) + 1; };
    if (!expect(q, "8=F")) [[unlikely]] return parse_msg_masks(p, n); skip(q);                 // 8=FIX.4.2   (validated by the session layer)
    if (!expect(q, "9=1")) [[unlikely]] return parse_msg_masks(p, n); skip(q);                 // 9=nnn
    if (!expect(q, "35=")) [[unlikely]] return parse_msg_masks(p, n); skip(q);                 // 35=D
    if (!expect(q, "49=")) [[unlikely]] return parse_msg_masks(p, n); skip(q);
    if (!expect(q, "56=")) [[unlikely]] return parse_msg_masks(p, n); skip(q);
    if (!expect(q, "34=")) [[unlikely]] return parse_msg_masks(p, n); { const char* d = find_soh_simd(q, e); f.seq = i64(parse_upto8_swar(q, u32(d - q))); q = d + 1; }
    if (!expect(q, "52=")) [[unlikely]] return parse_msg_masks(p, n); f.ts_ns = parse_utc_ts(q); q += 22;     // fixed width: no scan
    if (!expect(q, "11=")) [[unlikely]] return parse_msg_masks(p, n); { const char* d = find_soh_simd(q, e); f.clordid = {q, std::size_t(d - q)}; q = d + 1; }
    /* 21, 55, 54, 38, 40, 44, 59 likewise; 10= is the session layer's */
    return f;
}
```

- **Delimiter find**: `pcmpeqb` + `pmovmskb` + `tzcnt` answers "where is the next SOH within 16 bytes" in ~6 cycles flat (Ch. 39 §39.5 verified the codegen); it needs 16 readable bytes past the message, which the receive buffer must guarantee (§71.6). On fields of 4–10 bytes it is only ~even with the byte loop (§71.3) — the per-field *dispatch* is the larger cost.
- **Dispatch**: a `switch` on a tag that is a small integer compiles to a jump table (one indirect branch, predicted by the BTB on a stable tag order — Ch. 16); a chain of `if (tag == …)` is a mispredict per field. For sparse tags a perfect hash of the tag number into the table index keeps the jump table dense.
- **Bitmask tokenising** (build SOH and `=` position masks for the whole message with two compares per 16 bytes, then walk them with `tzcnt`/`blsr`): the simdjson structure, measured here at 93–111 ns — *slower* on a 145-byte message, because the mask build is paid up front and the walk still parses tags and dispatches. It wins on kilobyte messages (market-data snapshots, long repeating groups) and is the right base for a *validating* parser (every `=` must be preceded by a tag, every field terminated).
- **Layout pinning** is the structural win (58 ns, 1.4×) and the one real FIX engines use: the tag order of a given message type on a given session does not change, so parse by expectation, with the generic parser as the fallback (a venue that reorders is a slow path, not a failure). The fixed-width fields (`52=`, `54=`, `40=`, `59=`) are consumed without any scan. A **schema-generated** version of this — the Ch. 23 approach, with the tag order as a `constexpr` table and the parser unrolled from it — is exercise 4.
- **Parse what you need**: nine of sixteen fields are extracted above; `8=`, `9=`, `35=`, `49=`, `56=`, `21=` and `10=` are the session layer's (Ch. 106), validated once per message there, never converted here.

### 71.4.5 BodyLength and the checksum

`9=` is the byte count from after its own SOH to before `10=`, and `10=` is the byte sum of everything before it modulo 256, three digits zero-padded. On the *inbound* side both are the session layer's framing check (Ch. 106 §106.4.3): `BodyLength` says where the message ends in the TCP stream, so it is read *first* and bounds-checked against the buffer (a `BodyLength` larger than the receive buffer is an attack — Ch. 101), and the checksum is verified before any field is trusted. The byte sum is `psadbw` against zero — sixteen bytes per instruction, 6–7 ns for 141 bytes (§71.3) — with a scalar tail. On the *outbound* side the template knows its own `BodyLength` (fixed-width fields never change it) and computes the checksum once over the finished buffer; building the message *then* patching `9=` with a `memmove` (the from-scratch encoder) is where 10 of its 44 ns go.

### 71.4.6 The encode path: `to_chars`, digit pairs, fixed width, and the template with patched fields

```cpp
static constexpr char DIGIT_PAIRS[201] = "000102…9899";                          // Ch. 21 §21.4.2's constexpr table
static inline void put2(char* o, u32 v) { std::memcpy(o, DIGIT_PAIRS + v * 2, 2); }
static inline void put_fixed8(char* o, u32 v) {                                    // fixed width, zero padded: no length, 4 pair lookups
    put2(o + 6, v % 100); v /= 100; put2(o + 4, v % 100); v /= 100; put2(o + 2, v % 100); v /= 100; put2(o, v % 100); }

struct Template { char buf[160]; std::size_t len; std::size_t off_seq, off_ts, off_clordid, off_qty, off_px, off_cs; };
// built once per (session, message type): every constant field in place, every variable field fixed-width and zero-filled,
// 9= already correct because the body length never changes
static inline std::size_t encode_template(Template& t, const Order& od) {
    put_fixed8(t.buf + t.off_seq, od.seq); format_utc_ts(t.buf + t.off_ts, od.ts_ns); put_fixed8(t.buf + t.off_clordid, od.clordid);
    put_fixed8(t.buf + t.off_qty, od.qty);
    u32 ip = u32(od.px_e4 / 10000), fp = u32(od.px_e4 % 10000) / 100;             // 1872500 -> "0187.25"
    put2(t.buf + t.off_px, ip / 100); put2(t.buf + t.off_px + 2, ip % 100); put2(t.buf + t.off_px + 5, fp);
    u32 cs = checksum_simd(t.buf, t.off_cs - 3); t.buf[t.off_cs] = char('0' + cs / 100); put2(t.buf + t.off_cs + 1, cs % 100);
    return t.len;
}
```

- **`std::to_chars`** is the correct general integer formatter (libstdc++'s is a digit-pair implementation with a `lzcnt`-based length table: 4 ns for 4–7 digits) and the hand-rolled digit-pair loop *loses* to it (§71.3) — the table trick is already inside. Its cost is the **length computation** and the right-to-left write, which **fixed-width** formatting avoids entirely: `put_fixed8` is four table lookups into known positions, 3–5 ns, and — the real reason — it never changes the message length.
- **The template** is the encoder Ch. 106 §106.4.2 uses: constant fields laid out once; variable fields fixed-width (sequence number, ClOrdID, quantity, price with a fixed integer width, the timestamp), so `BodyLength` is a constant and no `memmove` is needed; a *patch* is a handful of `put2`s at known offsets; the checksum is one `psadbw` pass. 27 ns per NewOrderSingle (§71.3) against 44 ns from scratch, and — more important than the 17 ns — a flat cost with no length-dependent branches. Venues accept leading zeros in numeric fields (FIX's `int` is "leading zeros permitted"); a venue that rejects them needs a variable-width ClOrdID, which `to_chars` into a right-aligned slot handles at the cost of a `memmove` of the tail.
- **Price formatting** is the inverse of §71.4.2: integer and fraction parts from the scaled integer by one division and one modulo (the compiler makes them multiplies), digit pairs into fixed positions; never `snprintf("%.2f")` (slow, locale-dependent, a `double` on the way).
- **Everything else is off the hot path**: the template is built when the session logs on (Ch. 106 §106.4.1), the symbol is patched when the instrument changes, and the resend store (Ch. 106 §106.4.3) keeps the *finished bytes*, not the order, so a resend is a `memcpy` plus a re-patched `PossDup` flag and `SendingTime`.

## 71.5 Verify the codegen: the SWAR path, and the loop the compiler did or did not vectorise

`parse8_swar` under GCC 13.3 `-O2 -march=x86-64-v3` (`swar_asm.cpp`, `-S -masm=intel`), the whole function:

```
   movabs  rax, -3472328296227680304      ; -0x3030303030303030
   add     rax, QWORD PTR [rdi]            ; v = load - '0'*8  (one unaligned 8-byte load: the memcpy)
   lea     rdx, [rax+rax*4]                ; v*5
   shr     rax, 8
   lea     rax, [rax+rdx*2]                ; v*10 + (v>>8)      <- the multiply became two lea's
   movabs  rdx, 71777214294589695 ; and rax, rdx            ; & 0x00FF00FF00FF00FF
   lea     rdx, [rax+rax*4] ; shr rax, 16 ; lea rdx, [rdx+rdx*4] ; lea rax, [rax+rdx*4]   ; v*100 + (v>>16)
   movabs  rdx, 281470681808895   ; and rax, rdx            ; & 0x0000FFFF0000FFFF
   imul    rdx, rax, 10000 ; shr rax, 32 ; add eax, edx      ; v*10000 + (v>>32)
   ret
```

Sixteen instructions, one load, zero branches, zero memory stores — the 1.5 ns of §71.3 is the dependency chain's length (the three combine steps serialise; independent fields overlap). Clang emits the same shape with `imul` where GCC chose `lea` pairs. `parse16_simd` under Clang is `vmovdqu`, `vpaddb` (the `-'0'` as an add of `-48`), `vpmaddubsw`, `vpmaddwd`, `vpackusdw`, `vpmaddwd`, `vmovd`/`vpextrd`, one `imul` by 10⁸ and an `add` — eleven instructions. The thing to *check* rather than admire is the checksum: §71.3's byte loop ran at 37.6 ns under GCC and 7.1 ns under Clang because Clang's `-O2` auto-vectorised the sum (`vpsadbw` in its output) and GCC 13's `-O2` did not (`-O3` or `-ftree-vectorize` does; Appendix D.1). A loop whose vectorisation is load-bearing is written with the intrinsic (`checksum_simd`) or guarded by `-fopt-info-vec`/`-Rpass=loop-vectorize` in the build (Appendix D.7) — never assumed.

## 71.6 Pitfalls & anti-patterns

- **`atoi`/`strtol`/`stringstream`/`sscanf` on the hot path.** Locale lookups, `errno`, allocation (`stringstream`), 2–10× `from_chars` — Ch. 70 §70.6 said it; §71.3 measured it.
- **Prices through `double`.** Slow (§71.3) and wrong (Ch. 35): `187.25` is not representable, `x * 1e4` rounds. Integers with a decimal position (§71.4.2), always.
- **Reading past the buffer.** Every SIMD/SWAR load reads 8 or 16 bytes from `p` whether or not the field is that long. The receive buffer owns 16 bytes of slack after the last message, or the parser uses a masked/`maskmov` load at the tail — and the fuzzer (Ch. 101 §101.4.6, ASan) runs *with the slack removed* to prove no path depends on it accidentally.
- **Trusting `BodyLength`.** A `9=` larger than the buffer is a read past it; bounds-check it against the bytes actually received before using it to frame (Ch. 101). A checksum skipped "because TCP already checks" has let a corrupted-in-memory message through.
- **Variable-width SWAR without the length.** `parse_upto8_swar` needs the field length from the scan; applied to a field that runs into the next tag, it silently folds the tag's digits in. Validate (`all_digits8` on the right-aligned word) on untrusted input.
- **The bitmask tokeniser on short messages.** It lost by 15–30 % at 145 bytes (§71.3); measure before adopting a technique from a different message size.
- **Layout pinning without a fallback.** A venue that adds or reorders a field on a Sunday breaks a parser that *asserts* the layout; the pinned parser *expects* it and falls back (§71.4.4) — and logs the fallback count (Ch. 100), because a sustained fallback is a slow path that nobody chose.
- **Hand-rolled digit pairs beating `to_chars` — a belief, not a measurement.** `to_chars` already is one (§71.3); the win is fixed width and the template, not the table.
- **`snprintf`/`std::format` for the order message.** 100+ ns, a `double` on the price path, and a length that changes; the template (§71.4.6) or, for logs, Ch. 98's deferred formatting.
- **Parsing the same message in the benchmark.** Both compilers hoist it (§71.3's preamble; Ch. 3); a ring of different messages and a barrier on the output.

## 71.7 Exercises & checklist

**Exercises**

1. **Your session's message.** Capture one NewOrderSingle and one ExecutionReport from your venue's conformance log; build the pinned parser and the template for each; report §71.3's rows (d) and (f) on your machine, and the fallback rate over a day of replayed drop copy.
2. **Validation at the gate.** Fuzz `parse_msg_pinned` and `parse_price_e4` with libFuzzer + ASan (Ch. 101 §101.4.6) with the buffer slack *removed*; fix every read past the end; then measure what the fixes cost.
3. **`perf stat` the branches.** Run rows (a) and (d) under `perf stat -e branches,branch-misses` on the reference machine: the digit loop should show ~1 mispredict per variable-width field and the SWAR path ~0; the scalar tokeniser ~1 per field. Confirm the per-field cost model of §71.2.2.
4. **Schema-generated parser.** Express the session's tag order as a `constexpr` table and generate `parse_msg_pinned` from it with Ch. 23's technique; confirm the asm matches the hand-written version (§71.5) and that a reordered field falls back.
5. **32-digit AVX2 and `vpermb`.** Extend `parse16_simd` to 32 digits with AVX2, and try the AVX-512 `vpermb`-based right-alignment for variable-width fields; measure against `parse_upto8_swar` at lengths 1–16.
6. **The resend.** Keep finished message bytes in Ch. 106 §106.4.3's resend store; on a `ResendRequest`, patch `PossDup` and `SendingTime` in place and recompute the checksum. How many ns per resent message, and what else in the message *must* change?

**Checklist — text on the hot path**

- [ ] Every integer field is converted with **`from_chars` at least**, **SWAR/SIMD** where it is hot and the width is known; the conversion **validates** untrusted bytes (§71.4.1).
- [ ] Prices go **straight to scaled integers**, excess precision is rejected, no `double` is formed (§71.4.2).
- [ ] Timestamps are parsed and formatted with **integer civil-date arithmetic** and digit pairs; no `struct tm`, no `timegm` (§71.4.3).
- [ ] The hot message types are parsed **by expected layout** with a generic fallback whose rate is counted; only the needed fields are extracted; framing and checksum are the **session layer's** (§71.4.4–§71.4.5).
- [ ] Outbound messages are **templates with fixed-width patched fields** and one `psadbw` checksum; `BodyLength` is a constant (§71.4.6).
- [ ] The receive buffer has **16 bytes of slack** past the message and the fuzzer runs without it (§71.6).
- [ ] The load-bearing loops (checksum, delimiter find) are **intrinsics or asm-verified** vectorised, not assumed (§71.5).

## 71.8 References

- FIX Protocol Ltd, *FIX 4.2 / 4.4 Specification* (message framing, `BodyLength`, `CheckSum`, `UTCTimestamp`, field data types) — what §71.4.3–§71.4.6 implement.
- D. Lemire, *"Number Parsing at a Gigabyte per Second"* (Software: Practice and Experience 51(8), 2021) and the "Quickly parsing eight digits" blog series; W. Muła, *"SIMD-ized parsing of decimal numbers"* — the SWAR and `pmaddubsw`/`pmaddwd` constructions of §71.4.1.
- G. Langdale & D. Lemire, *"Parsing Gigabytes of JSON per Second"* (VLDB Journal 28, 2019) — the bitmask tokeniser §71.4.4 measured and where it pays.
- H. Hinnant, *"chrono-Compatible Low-Level Date Algorithms"* — `days_from_civil`/`civil_from_days`, the branch-free calendar arithmetic of §71.4.3.
- A. Alexandrescu, *"Three Optimization Tips for C++"* (Facebook, 2012) — the digit-pair table, and why `to_chars` implementations use it.
- The libstdc++ `<charconv>` implementation — `to_chars`'s length table and digit pairs (§71.3 row (f)).
- Ch. 70 §70.3 (the baseline), Ch. 106 §106.4.2 (the encoder in its session), Ch. 39 §39.5 (the delimiter-find codegen), Ch. 101 (the gate).

## 71.9 Additional Reading

- The `fast_float` and `simdjson` source — production branch-free parsers, and the engineering around buffer slack and validation.
- J. Anhalt's and R. Ryu's integer/float formatting work (`jeaiii`, Ryu) — the state of the art past `to_chars` when a float *must* be formatted.
- Appendix J.5 — what each venue's timestamp field means, before comparing it to yours.

---

*Next: Ch. 72 — Reliable Multicast & Feed Recovery, the recovery half that Ch. 70's gap detection points at: UDP multicast is lossy, a gap blinds the strategy, and retransmission, snapshots and PGM-style recovery must restore the stream — and the Ch. 33 book — without ever stalling the hot path.*
