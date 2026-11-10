# Part V — Numerics & Data Parallelism

# Chapter 38 — Fast Hashing, Checksums & Key Design for the Hot Path

> **Prerequisites:** Ch. 32 (flat/open-addressing tables — this chapter is what goes *into* the hash slot of §32.3's `FlatMap` and why its multiplier is `0x9E3779B97F4A7C15`), Ch. 37 (multiply, shift, rotate, `pdep`/`pext` — the instructions a hash is made of), Ch. 21 (`constexpr` — the compile-time perfect hash of §38.4.4), Ch. 8 (a probe is a cache line), Ch. 15 (a probe loop is a branch), Ch. 4 (reading asm — §38.5).
>
> **Leads into:** Ch. 39 (SIMD — SwissTable-style group probing and the CRC/PCLMUL instructions), Ch. 33 (the order book's order-ID map), Ch. @MULTIVENUE@ (symbology — per-venue keys to one dense instrument id), Ch. 70 (the 8-byte symbol load off the wire), Ch. 101 (hash flooding from attacker-controlled keys), Ch. 109 (per-record CRC32C in the journal), Ch. 82 (trusting the NIC's checksum in a userspace stack).

---

## 38.1 Why it matters: the fixed cost every message pays before layout matters

Every message that reaches the strategy does at least one key lookup before any decision: symbol → instrument state, order ID → resting order, session → gateway, message type → handler. Ch. 32 taught the dominant lesson — a flat table with contiguous probing beats a node-based map by an order of magnitude because the *probe* is a cache line, not a pointer chase — and then handed you a `0x9E3779B97F4A7C15` multiplier without a word about where it came from, why the code shifts the *high* bits, and what happens if your keys are not what it assumes. This chapter is the other half of the lookup: the hash function, the key, and the checksum, each of which is a fixed cost paid on every message *before* the layout win can apply, and each of which has a failure mode that turns a 4 ns lookup into a 40 ns one or — with the wrong key design — into a table whose probe length is 18,000.

The numbers, measured below: an 8-byte key hashed with a multiply costs ~3 cycles; the same key through `std::hash<std::string>` costs 5 and a heap-allocated `std::string` to get there; `XXH3` costs 8 (16 on the latency path), SipHash 23 (31). A dense exchange-assigned ID needs *no* hash — identity indexing probes zero times — while identity on 8-byte ASCII symbols probes 18,084 times on average because the low bits are the same space character. A compile-time perfect hash for a closed instrument set resolves in 3 cycles with no probe loop at all. CRC32C over a 64-byte record is 32 cycles with one instruction per 8 bytes. And a hash that is *not* keyed, fed with `ClOrdID`s a counterparty chose, can be driven to its worst case on purpose — which is what Ch. 101's "the wire is untrusted" means for a table.

The chapter: what a hash is for and the four properties that matter on the hot path (§38.2.1), the catalogue from identity to SipHash with their mechanisms (§38.2.2), key design — fixed-width symbols as integers, dense IDs as indices (§38.2.3), checksums as a different job (§38.2.4); the shootout and the probe-length experiment (§38.3); the techniques — choosing by key class, the 8-byte symbol key, Fibonacci hashing done right, the compile-time perfect hash, CRC32C for records and the keyed hash for hostile keys (§38.4); the asm (§38.5); and the pitfalls (§38.6). The adjacent topics the brainstorm review listed — bounded-probe tables, filters and sketches, fast division beyond the one range trick used here — are not scheduled; where a technique here touches one, the text says so.

## 38.2 Mental model

### 38.2.1 What a hash is for, and the four properties that matter here

A hash function maps a key to a slot index; a *good* one makes the slot indices of the keys you actually have look uniformly random, so an open-addressing table at load factor α probes about `1/(1−α)` slots on a miss and `(1 + 1/(1−α))/2` on a hit (0.5–1.9 at α = 0.65, which is what §38.3 measures for every decent hash). Four properties decide whether a function belongs on the hot path:

1. **Cost in cycles — in both modes.** *Throughput* (independent lookups, the batch case) and *latency* (the lookup is on the dependency chain of the next instruction — the common case on the decision path: hash → slot → compare → branch). A multiply-shift is 3 cycles either way; a mixer with three multiply-xorshift rounds is 4 of throughput and 10 of latency (§38.3). The table's probe is another 5–60 cycles per cache line (Ch. 8), so a hash that saves 10 cycles of latency but costs one extra probe has lost.
2. **Distribution on *your* keys, not on random ones.** Every hash in §38.3 is fine on random 64-bit keys. The differences appear on *structured* keys: ASCII symbols (high-order bytes are letters, the rest are spaces — the top bits vary, the bottom bits do not), monotone order IDs (stride 1, 4 or 16; the high bits fixed per session), dense exchange IDs (1…N). The question is whether the function moves the entropy from where it is in the key to the bits the table uses.
3. **Which bits the table uses.** A power-of-two table takes `hash & (cap−1)` — the *low* bits — or `hash >> (64 − log2 cap)` — the *high* bits. A multiplicative hash concentrates its mixing in the high bits (carries propagate upward), so it must be used with the shift; a finalizer-style hash (splitmix, wyhash, xxHash) mixes both ends and either works. §38.3 shows Fibonacci hashing with the *low* bits probing 37 slots on symbols against 0.89 with the high bits — the same multiply, a 40× difference.
4. **Keyed or not.** An unkeyed hash is a public function; an adversary who can choose keys (a client's `ClOrdID`, a FIX tag value, an HTTP header on a web venue — Ch. @ENCRYPTED@) can choose ones that collide. A keyed hash (SipHash, or wyhash/xxHash3 with a per-process secret) makes the collision set unguessable. Keying costs nothing in wyhash/xxHash3 (the seed is an extra operand) and everything in SipHash (it is a small block cipher); the question is which tables face a chooser (§38.4.6).

### 38.2.2 The catalogue: identity, multiplicative, finalizers, the modern mixers, SipHash, CRC

| Function | Mechanism (64-bit key) | Cycles thr / lat (§38.3) | Good on | Bad on |
|---|---|---|---|---|
| **identity** | `h = k` | 2.8 / 2.8 (the loop overhead) | dense IDs 1…N (direct index, zero probes); random keys | anything structured: symbols (18,084 probes), strided IDs with a low-bit mask |
| **Fibonacci / multiplicative** | `h = k · 0x9E3779B97F4A7C15`, use the **high** bits | 3.5 / 3.5 | symbols, strided IDs, random — *with the shift* | anything, if you mask the low bits; dense IDs (fine, but identity is free) |
| **multiply-xorshift** | `h = k·φ; h ^= h >> 32` | 2.8 / 4.9 | the same, and tolerant of the low-bit mask | — |
| **splitmix64 / murmur3 finalizer** | three `xorshift-multiply` rounds | 4.2 / 9.8 | everything; the "avalanche" standard (each input bit flips ~half the output bits) | latency-bound lookups (10 cycles) |
| **wyhash64 / rapidhash mixer** | two 64×64→128 `mum` folds | 4.2 / 9.0 | everything; keyed for free; the fastest *string* hash family for short inputs | latency, same as above |
| **XXH3_64bits** (libxxhash) | length-dispatched; for 8 bytes a `mum`-style fold with a secret | 8.4 / 16.6 (8-byte key); 4.2 thr over an 8-char `std::string` | variable-length strings 16 B–16 KiB at ~10 GB/s; keyed | an 8-byte integer key (the length dispatch and call cost dominate) |
| **SipHash-2-4** | 2+4 ARX rounds, 128-bit key | 23 / 31 | hostile keys (the only one above with a cryptographic collision-resistance argument) | any table whose keys you control |
| **CRC32C** (`crc32q`) | polynomial division in hardware, 3-cycle latency per 8 bytes | 3.5 / 3.5 | a *checksum* (burst-error detection); also a usable 32-bit hash on symbols (0.85 probes) | 64-bit output (it is 32 bits); strided order IDs with a low-bit mask (perfect — 0.00 — by accident of linearity, do not rely on it) |

The lesson of the table is that for an 8-byte key **the cheapest adequate hash is a multiply**; everything heavier buys robustness against key structure you may not have, and the robust-by-construction options (SipHash) cost 8× for a reason you only need at one or two tables.

### 38.2.3 Key design: the key is yours to choose before the hash is

The biggest win in this chapter is not a better function; it is a better *key*. Three classes:

- **Dense exchange-assigned IDs** — Nasdaq's `Stock Locate` (a 16-bit index), CME/Eurex `SecurityID`, a venue's instrument index in its definition feed — are already slot numbers. Index an array directly: `table[id]`, zero hash, zero probe, 1.2 cycles (§38.3 of the multi-venue chapter, Ch. @MULTIVENUE@). Translate every venue key to *your* dense internal id once at start-of-day and use that id everywhere downstream (the SoA instrument arrays of Ch. 10, the book array of Ch. 33). The hash lives only at the ingress edge, once per message, and it is a hash of the *venue's* key.
- **Fixed-width symbols** — `"AAPL    "` in ITCH, 6–8 bytes padded with spaces — are **one 64-bit integer**: `std::memcpy(&k, p, 8)` (one `mov`, Ch. 70 §70.4.1), compared with one `cmp`, hashed with one `imul`. Never `std::string`: the allocation, the length, the byte loop, and `std::hash<std::string>` (libstdc++ runs a MurmurHash over the bytes — 5.2 cycles for the hash alone, after the construction). A symbol longer than 8 bytes (OSI option symbols are 21) is two or three 64-bit words, hashed with a two-word mixer, or — better — translated to a dense id at the edge.
- **Monotone order IDs** — your own `slot + generation` handles (Ch. 32 §32.4.4) *are* indices: no map at all. The venue's order IDs on an MBO feed are monotone-ish 64-bit integers with a fixed high part: identity with a low-bit mask works (0.85 probes, §38.3) until the venue's stride changes; Fibonacci with the shift works regardless.

The remaining case — strings of arbitrary length you did not design (FIX `SenderCompID`, a REST symbol like `"BTC-USDT"`, a session name) — is the only one that needs a string hash, and it belongs on the control plane or at the connection edge, hashed once into a dense id.

### 38.2.4 Checksums are a different job

A checksum answers "were these bytes damaged?" — burst errors from a flaky link, a torn write in a journal, a bit flip in DMA — and is judged by its *error-detection guarantees*, not its distribution. CRC32C (the Castagnoli polynomial) detects all burst errors up to 32 bits and all odd numbers of bit errors, is what iSCSI, ext4, Btrfs, SCTP and the Ethernet FCS's cousins use, and has a hardware instruction since Nehalem (`crc32q`, SSE4.2): 3-cycle latency, 1 per cycle throughput, 8 bytes per instruction — **0.26 cycles/byte single-stream** in §38.3, and ~0.1 with three interleaved streams recombined through PCLMUL (Gopal et al.). The TCP/UDP checksum (one's-complement 16-bit sum) and the IP header checksum are the NIC's job (Ch. 75 keeps the offload on) and a userspace stack's job to *verify from the descriptor flags*, not recompute (§38.4.5). A hash is not a checksum (xxHash3 has no burst-error guarantee) and a checksum is not a hash for hostile input (CRC is linear: an attacker can fix up any message to any CRC with 32 free bits — which is also why "CRC as a hash" has a flat probe distribution on strided keys: it is *too* regular).

The model: **a hot-path lookup is a key you designed (dense id → direct index; fixed-width symbol → one 64-bit word), a hash that is one multiply when the key's structure allows it and a short mixer when it does not, and the table's choice of which bits to use matched to the hash's mixing direction. A keyed hash guards the one or two tables that face an adversary; a CRC guards bytes against damage, not keys against choice.**

## 38.3 Measure it: the shootout, the probe lengths, the perfect hash and CRC32C

*Toolchain and machine: Appendix L.3. The run below is on the 4-vCPU KVM guest (GCC 13.3, `-O2 -std=c++20 -march=native`, libxxhash 0.8.2, TSC at 2.7 GHz — divide cycles by 2.7 for ns).*

Four key sets of 42,598 keys (65% of a 2¹⁶-slot table): **symbols** (1–5 uppercase letters, space-padded to 8 bytes as ITCH does), **dense IDs** 1…N, **order IDs** (a fixed 16-bit session prefix in the high bits, stride 4), and **random** 64-bit values. Each hash is timed in throughput and latency mode, then every (hash, key set) pair is inserted into a linear-probing table and the mean and maximum probe length recorded:

```cpp
// hashbench.cpp (excerpt) — the functions under test; the harness is Appendix L's shape. -lxxhash for XXH3.
static inline uint64_t h_fib(uint64_t k)     { return k * 0x9E3779B97F4A7C15ULL; }              // use the TOP bits
static inline uint64_t h_fib_xs(uint64_t k)  { uint64_t h = k * 0x9E3779B97F4A7C15ULL; return h ^ (h >> 32); }
static inline uint64_t h_splitmix(uint64_t k){ k ^= k >> 30; k *= 0xBF58476D1CE4E5B9ULL; k ^= k >> 27; k *= 0x94D049BB133111EBULL; return k ^ (k >> 31); }
static inline uint64_t mum(uint64_t a, uint64_t b) { __uint128_t r = (__uint128_t)a * b; return (uint64_t)r ^ (uint64_t)(r >> 64); }
static inline uint64_t h_wy(uint64_t k)      { return mum(mum(k ^ 0xa0761d6478bd642fULL, 0xe7037ed1a0b428dbULL), 0xe7037ed1a0b428dbULL); }
static inline uint64_t h_crc(uint64_t k)     { return _mm_crc32_u64(0, k); }                     // 32-bit result
static inline uint64_t h_xxh3(uint64_t k)    { return XXH3_64bits(&k, 8); }
// probe-length experiment: linear probing, 2^16 slots, the table's slot from either the high or the low bits
template <class F> Probe probe_stats(const std::vector<uint64_t>& keys, F f, bool top_bits) {
    const int B = 16; std::vector<uint64_t> slots(1ull << B, 0); long total = 0; int mx = 0;
    for (uint64_t k : keys) { uint64_t h = f(k); size_t i = top_bits ? (h >> (64 - B)) : (h & ((1ull << B) - 1));
        int p = 0; while (slots[i]) { i = (i + 1) & ((1ull << B) - 1); ++p; } slots[i] = k; total += p; mx = std::max(mx, p); }
    return { double(total) / keys.size(), mx };
}
```

```
== cost per hash of an 8-byte key (cycles; thr = independent, lat = dependent chain) ==
identity                thr 2.8   lat 2.8     (loop overhead: the floor)
fibonacci (mul)         thr 3.5   lat 3.5
fib + xorshift          thr 2.8   lat 4.9
splitmix64 finalizer    thr 4.2   lat 9.8
crc32c (_mm_crc32_u64)  thr 3.5   lat 3.5
wyhash64 mixer          thr 4.2   lat 9.0
XXH3_64bits(8 B)        thr 8.4   lat 16.6    (length dispatch + call)
SipHash-2-4             thr 23.1  lat 31.5
std::hash<std::string>  thr 5.2             (8-char string; the std::string itself not counted)
XXH3 over the string    thr 4.2

== mean / max probe length, linear probing, 2^16 slots at 65% load ==
                           symbols         dense IDs       order IDs       random
identity (low bits)     18084.09 / 42532   0.00 /    0     0.85 /    2     0.91 /  57
fibonacci (HIGH bits)       0.89 /    38   0.00 /    0     0.50 /    3     0.90 /  72
fibonacci (LOW bits)       37.07 /   345   0.00 /    0     0.85 /    2     0.93 /  61
fib + xorshift (low)        0.93 /    68   1.16 /  139     1.07 /   87     0.93 /  72
splitmix64 (low)            0.92 /    57   0.95 /   73     0.91 /   84     0.92 /  48
crc32c (low)                0.85 /    58   0.61 /    9     0.00 /    0     0.95 /  58
wyhash64 (low)              0.91 /    44   0.93 /   41     0.94 /   50     0.88 /  41
XXH3 (low)                  0.93 /    50   0.94 /   83     0.91 /   46     0.95 /  53
SipHash (low)               0.94 /    59   0.93 /   50     0.90 /   46     0.93 /  56

== compile-time perfect hash (phash.cpp, §38.4.4) ==
CHD, 512 symbols, 1024 slots, 256 buckets:      3.07 cyc/lookup (incl. call), no probe loop
single multiplier, 40 keys, 4096 slots:          2.08 cyc/lookup (incl. call)   (Clang: 1.73)

== CRC32C (crc32q, one stream) ==
   64 B:    32 cyc = 0.50 cyc/B       256 B:   44 cyc = 0.17 cyc/B
 1500 B:   428 cyc = 0.29 cyc/B     65536 B: 17054 cyc = 0.26 cyc/B
```

Read it in three passes. **Cost**: the multiply family is 3–5 cycles and the mixers 9–10 on the latency path; XXH3 is a *string* hash and pays its length dispatch on an 8-byte key; SipHash is 8× everything else. **Distribution on structured keys**: identity on symbols is a catastrophe (every symbol ends in spaces, so the low 16 bits are identical for most of them — the "table" is one 42,000-long chain); Fibonacci with the *high* bits is as good as any mixer on every set, and with the *low* bits is 40× worse on symbols — that is the single most common hash bug in hand-rolled tables, and it is why Ch. 32's `FlatMap` has a `shift`, not a mask; identity on dense IDs is perfect and free, which is why §38.4.1 makes it the first choice; the modern mixers are indistinguishable from random on everything. **The probe is the cost**: at 0.9 mean probes nobody's hash matters and the cache line does; at 37 or 18,084 the hash is the whole latency. The perfect hash removes the probe loop entirely for the closed sets (instruments, message types) — 3 cycles and a compare — and CRC32C's 0.26 cycles/byte is the budget for checksumming a 64-byte journal record (32 cycles) on the capture path.

## 38.4 Techniques

### 38.4.1 Choose by key class, not by benchmark

The decision procedure, in order:

1. **Is the key dense?** (Exchange-assigned index, your own slot handle.) → **Direct index**. No hash, no probe, bounds-checked once (Ch. 101 §101.4.1: the venue's id is wire input).
2. **Is it a fixed-width symbol or a monotone 64-bit ID?** → **Multiply-shift** (Fibonacci, high bits) into an open-addressing table (Ch. 32 §32.4.1), or `fib + xorshift` if the table library insists on masking the low bits (every SwissTable-style library does its own mixing of what you give it; `absl::Hash` and `ankerl::unordered_dense::hash` apply a finalizer, so a plain identity *is* the right `Hash` functor for integer keys there — check the library).
3. **Is the key set closed and known at build time?** (Message types, a venue's instrument universe for the day, a fixed set of sessions.) → **Compile-time perfect hash** (§38.4.4): no probe loop, no load-factor question, no rehash.
4. **Is it a variable-length string?** → **wyhash/rapidhash or XXH3** (both keyed, both ~10 GB/s on long inputs, both near the multiply cost on short ones); translate to a dense id at the edge so the hot path never sees the string again.
5. **Can an adversary choose it?** → **key the hash** (a per-process random seed in wyhash/XXH3; SipHash only if you need the cryptographic argument — §38.4.6).

What never appears on the list: `std::hash` (libstdc++'s integer `std::hash` is the identity, which is step 1's answer only when the keys are dense and your table masks nothing out — `std::unordered_map` uses a prime modulus so it survives, `absl` mixes, a hand-rolled power-of-two table with identity on symbols gets §38.3's 18,084), and `std::string` keys on the hot path.

### 38.4.2 The 8-byte symbol key

Off the wire, a symbol is bytes at an offset in a fixed-layout message (Ch. 70 §70.2.2). Make it a key with one load and compare it with one `cmp`:

```cpp
// sym8.hpp — a fixed-width, space-padded symbol as a 64-bit key (ITCH/OUCH-style 8-byte fields).
struct Sym8 {
    std::uint64_t v;
    static Sym8 from_wire(const std::byte* p) { std::uint64_t k; std::memcpy(&k, p, 8); return {k}; }   // one mov; no byteswap: bytes are the key
    static constexpr Sym8 from_literal(std::string_view s) {                                           // "AAPL" -> "AAPL    " at compile time
        std::uint64_t k = 0; for (std::size_t i = 0; i < 8; ++i) k |= std::uint64_t(i < s.size() ? std::uint8_t(s[i]) : 0x20) << (8 * i); return {k};
    }
    friend bool operator==(Sym8, Sym8) = default;
    struct Hash { std::size_t operator()(Sym8 s) const noexcept { return s.v * 0x9E3779B97F4A7C15ULL; } };   // Fibonacci; the table must take the HIGH bits
    struct HashLowBits { std::size_t operator()(Sym8 s) const noexcept { auto h = s.v * 0x9E3779B97F4A7C15ULL; return h ^ (h >> 32); } };   // for libraries that mask
};
static_assert(sizeof(Sym8) == 8 && std::is_trivially_copyable_v<Sym8>);
```

Two details carry the performance: **no byte swap** — the bytes themselves are the key, and endianness is irrelevant as long as every producer uses `memcpy` the same way (a `bswap` is one cycle but a needless one; the only reason to swap is if you need the key to *sort* alphabetically, which an index does not); and **the padding byte is part of the key** — `"AAPL\0\0\0\0"` and `"AAPL    "` are different keys, so normalise at the edge (`from_wire` trusts the venue's padding; a FIX `Symbol` tag parsed from text goes through `from_literal`). Ch. 25's `Sym8` (strings without allocation) is the same type seen from the string side; this is its hashing side. For 16-byte keys (OSI option symbols, `Sym8` + venue), hash the two words as `mum(a ^ seed, b ^ seed2)` (one 128-bit multiply, 4 cycles) rather than two separate multiplies added — the add loses the high-bit mixing.

### 38.4.3 Fibonacci hashing, done right

The multiplier `0x9E3779B97F4A7C15` is `2⁶⁴/φ` rounded to odd — Knuth's multiplicative hashing with the golden ratio, which spreads consecutive keys as far apart as possible (each successive key's hash lands in the largest gap left by the previous ones, the "three-distance theorem"). It works on any power-of-two table with three rules, all of which §38.3 tested:

- **Take the high bits**: `slot = (k * φ) >> (64 − B)`. A multiply's carries move entropy *up*; the low bits of `k * φ` are a function of the low bits of `k` alone (bit *i* of the product depends only on bits 0…*i* of the key), so a low-bit mask on space-padded symbols sees only the spaces. The shift is one `shr`; the mask is one `and`; the cost is identical and the probe lengths differ 40×.
- **Or fold once** if the table masks: `h ^= h >> 32` (then the low 32 bits contain the high 32's mixing — 2.8/4.9 cycles in §38.3, still fine on every key set, and the slot-mask version costs one more cycle of latency than the shift version).
- **Keep the multiplier odd** (so the map is a bijection on 64-bit integers) and **do not "improve" it** with a second multiply unless you measured a probe-length problem — the second multiply is what splitmix does, at 2× the latency, and §38.3 shows it buys nothing on symbols, IDs or random keys.

For a table whose capacity is *not* a power of two (`std::unordered_map`'s primes), the same multiply followed by Lemire's **fast range** — `slot = ((__uint128_t)h32 * cap) >> 32` — replaces the `%` (a 20–40-cycle `div`) with one multiply and shift and is uniform enough for any mixed hash; it is the one item from the review's fast-division list (D17) that belongs here, because it is the difference between a 4-cycle and a 30-cycle slot computation in any table that is not power-of-two sized.

### 38.4.4 A compile-time perfect hash for the instrument table

For a closed key set known when the binary is built — the message-type dispatch table, a venue's instrument list baked into a daily build, the session roster — a **perfect hash** has no collisions by construction: lookup is hash, one load, one compare, no loop. Two constructions, both `constexpr` (Ch. 21), both verified in §38.3 and §38.5:

- **One multiplier, searched** — for small sets (≤ ~64 keys in a table of 4N² slots the birthday bound makes a collision-free multiplier likely): walk a splitmix sequence of odd multipliers until `(k · m) >> (64 − B)` is injective over the set. For 40 message types in 4,096 slots the *first* candidate, φ itself, works; lookup is `imul; shr; cmp` — **2.1 cycles including the call**.
- **Hash-and-displace (CHD)** — for hundreds to tens of thousands of keys at ~2 slots per key: a first hash `h1` assigns each key to one of `N/2` buckets; buckets are processed largest first, and for each the construction searches a per-bucket seed `d` such that `h2(k, d)` places all its keys into free slots. Lookup is `h2(k, disp[h1(k)])`: two multiplies, two loads (the 16-bit seed, then the slot), one compare — **3.1 cycles including the call** for 512 symbols in 1,024 slots, and the construction for 512 keys takes GCC a few seconds at `-fconstexpr-ops-limit=2000000000` (the keys are an `#include`d generated array; the build is the time to spend, Ch. 28).

```cpp
// phash.cpp (excerpt) — CHD built at compile time; keys are 8-byte Sym8 values.
template <size_t N, int B> struct CHD {
    static constexpr uint32_t M = 1u << B, NB = M / 4;                    // slots, buckets
    std::array<uint64_t, M> table; std::array<uint16_t, M> ids; std::array<uint16_t, NB> disp;
    static constexpr uint32_t h1(uint64_t k)             { return uint32_t((k * 0x9E3779B97F4A7C15ULL) >> (64 - std::countr_zero(NB))); }
    static constexpr uint32_t h2(uint64_t k, uint32_t d) { return uint32_t((k * (0xC2B2AE3D27D4EB4FULL + 2 * uint64_t(d) * 0x9E3779B97F4A7C15ULL)) >> (64 - B)); }
    constexpr CHD(const std::array<uint64_t, N>& keys) : table{}, ids{}, disp{} {
        std::array<uint16_t, NB> bsize{}; for (auto k : keys) bsize[h1(k)]++;
        std::array<uint16_t, NB> order{}; for (uint32_t b = 0; b < NB; ++b) order[b] = uint16_t(b);
        std::sort(order.begin(), order.end(), [&](uint16_t a, uint16_t c) { return bsize[a] > bsize[c]; });   // biggest buckets first
        std::array<bool, M> used{};
        for (uint32_t b : order) { if (!bsize[b]) break;
            for (uint32_t d = 0; ; ++d) {                                     // search the bucket's seed
                bool ok = true; std::array<uint32_t, 64> taken{}; int nt = 0;
                for (size_t i = 0; i < N && ok; ++i) if (h1(keys[i]) == b) { uint32_t s = h2(keys[i], d);
                    if (used[s]) ok = false; else { for (int t = 0; t < nt; ++t) if (taken[t] == s) ok = false; taken[nt++] = s; } }
                if (ok) { disp[b] = uint16_t(d); for (size_t i = 0; i < N; ++i) if (h1(keys[i]) == b) { uint32_t s = h2(keys[i], d); used[s] = true; table[s] = keys[i]; ids[s] = uint16_t(i); } break; }
                if (d == 65535) throw "CHD: no seed found — grow B";          // a constexpr failure is a compile error
            } }
    }
    uint32_t find(uint64_t k) const { uint32_t s = h2(k, disp[h1(k)]); return table[s] == k ? ids[s] : UINT32_MAX; }
};
constexpr CHD<512, 10> PH(KEYS);                     // KEYS: the generated, deduplicated, sorted instrument list
```

The miss path matters as much as the hit: a key not in the set lands on *some* slot, and the `table[s] == k` compare is what rejects it (the `UINT32_MAX` sentinel; the caller bounds-checks, Ch. 101). Rebuilding when the set changes is a *build*, which is the right cadence for a daily instrument universe and the wrong one for intraday additions — those go to a Ch. 32 table beside the perfect one, or to the hot-reloaded reference data of Ch. 102.

### 38.4.5 CRC32C for records and frames; what to trust from the NIC

Where bytes can be damaged — the capture journal (Ch. 109 §109.4.2), a shared-memory record a crashed writer may have torn (Ch. 67), a custom reliable-UDP frame (Ch. 85 §85.4.4) — stamp a CRC32C and verify it on read:

```cpp
// crc32c.hpp — hardware CRC32C over a record; 8 bytes per instruction, tail bytes one at a time.
inline std::uint32_t crc32c(const void* data, std::size_t n, std::uint32_t seed = 0) {
    const auto* p = static_cast<const std::uint8_t*>(data); std::uint64_t c = ~std::uint64_t(seed);
    for (; n >= 8; n -= 8, p += 8) { std::uint64_t w; std::memcpy(&w, p, 8); c = _mm_crc32_u64(c, w); }   // crc32q: 3-cycle latency, 1/cycle thr
    for (; n; --n, ++p) c = _mm_crc32_u8(std::uint32_t(c), *p);
    return ~std::uint32_t(c);
}
```

Single-stream `crc32q` is latency-bound at 3 cycles per 8 bytes (0.375 cycles/byte ideal; 0.26 measured in §38.3 because the loads overlap): a 64-byte record costs 32 cycles, a 1,500-byte frame 430. For bulk (a 64 KiB journal block), three independent streams over three chunks interleave to ~0.1 cycles/byte and are recombined with a PCLMUL-based shift (Gopal et al.'s "slicing-by-3", as in the Linux kernel's `crc32c-intel` and `zlib-ng`); on the *capture* path (Ch. 109 §109.4.3) the record CRC is computed by the drain thread on the housekeeping core, never by the writer on the hot core.

What *not* to compute: the TCP/UDP/IP checksums. A kernel-bypass stack (Ch. 82 §82.4.2) reads the RX descriptor's checksum-OK flags (`ef_vi`'s `EF_EVENT_RX_CSUM`-style bits, DPDK's `RTE_MBUF_F_RX_L4_CKSUM_GOOD`/`BAD`/`UNKNOWN`) and **treats `UNKNOWN` as a frame to verify in software, not as good** — the NIC computes the checksum on every frame it can parse, and a frame it could not parse (an unexpected encapsulation, a VLAN tag the filter did not strip, a truncated header) is exactly the one Ch. 101 says to distrust. On TX, the NIC fills the checksum when the offload is on (Ch. 75 §75.2); a userspace stack that forgets to set the descriptor's checksum-offload bits sends frames the receiver drops silently — the first thing to check when a bypass session "connects but never gets a reply".

### 38.4.6 Hash flooding: when the adversary chooses the keys

A table keyed by something a counterparty chooses — a client's `ClOrdID` in a broker's order gateway (Ch. 106), a FIX `SenderCompID`, the symbol string a REST client sends to a web venue you *operate* (Ch. @ENCRYPTED@) — can be flooded: with an unkeyed hash, the attacker computes a few thousand keys that share a slot, sends them, and every subsequent lookup in that table walks a chain (or probe sequence) thousands long — §38.3's identity-on-symbols row, induced on purpose, and a denial-of-service that costs the attacker nothing. The 2011 "hash DoS" against web frameworks is the same attack on the same data structure.

The defence is a **keyed hash with a per-process secret**: wyhash and XXH3 take a seed for free (`XXH3_64bits_withSeed(p, n, secret)`; wyhash's `secret[4]`), which makes the collision set unguessable without the secret — sufficient against a remote chooser who cannot observe timing at the resolution needed to recover it. **SipHash** is the answer when the argument must be cryptographic (the attacker can observe many outputs, or the table's secret has a long life); it costs 23 cycles (§38.3), which is fine for a session-establishment table and wrong for the per-message instrument lookup. The rule: **tables keyed by wire-chosen strings at a trust boundary use a seeded hash; tables keyed by dense ids or by symbols from a venue you trust use the multiply.** Rotate the seed per process start (`getrandom(2)`, Ch. 101 §101.4.5), bound the table (a client that opens 10⁶ distinct `ClOrdID`s is throttled before the table grows — Ch. 105 §105.4.2's in-flight table is fixed-capacity for this reason too), and *measure* the probe-length distribution of every wire-keyed table in the §38.3 harness with adversarial inputs as part of the Ch. 101 §101.4.8 fuzz pass.

## 38.5 Verify the codegen: one multiply, the shift, and the perfect-hash lookup

Snippets are **verified** GCC 13.3 output (`-O2 -std=c++20 -march=native`, Appendix L.3); Clang 18.1 is the same shape.

**Fibonacci slot, high bits**: `(k * 0x9E3779B97F4A7C15) >> 48` for a 2¹⁶ table is `movabs $0x9e3779b97f4a7c15, %rax; imul %rdi, %rax; shr $48, %rax` — one 3-cycle `imul`, one `shr`. The low-bits version is `imul; and $0xffff` — the same cost and the 40× worse table; the asm will not tell you which is right, the probe-length histogram will.

**The CHD lookup** (`lookup(uint64_t k)` from §38.4.4, `noinline` for the listing):

```asm
_Z6lookupm:
        movabsq  $-7046029254386353131, %rax     # 0x9E3779B97F4A7C15 (h1 multiplier)
        imulq    %rdi, %rax
        leaq     _ZL2PH(%rip), %rdx              # the constexpr CHD object in .rodata
        movabsq  $4354685564936845354, %rcx      # 2 * 0x9E3779B97F4A7C15 (seed stride)
        shrq     $56, %rax                       # h1: top 8 bits -> bucket
        movzwl   10240(%rdx,%rax,2), %eax        # disp[bucket]  (16-bit seed)
        imulq    %rcx, %rax                      # seed * stride
        movabsq  $-4417276706812531889, %rcx     # 0xC2B2AE3D27D4EB4F (h2 base multiplier)
        addq     %rcx, %rax
        imulq    %rdi, %rax                      # h2(k, d)
        movl     $-1, %ecx                       # UINT32_MAX for the miss
        shrq     $54, %rax                       # top 10 bits -> slot
        cmpq     (%rdx,%rax,8), %rdi             # table[slot] == k ?
        jne      .L1
        movzwl   8192(%rdx,%rax,2), %ecx         # ids[slot]
.L1:    movl     %ecx, %eax
        ret
```

Three `imul`, two loads from the `.rodata` object (the seed array at offset 10240, the key table at 0, the id table at 8192), one compare and one well-predicted branch: the 3.1 cycles §38.3 measured. There is no loop — that is the point — and the whole object (8 KiB keys + 2 KiB ids + 512 B seeds) is in `.rodata`, pre-faulted with the code (Ch. 61). **The thing to look for in the negative**: a `call` to a hash function that did not inline (`XXH3_64bits` is a library call unless you use the inlined header variant `XXH_INLINE_ALL`), or a `div`/`idiv` for a slot computation (a `%` by a non-power-of-two capacity — replace with Lemire's fast range, §38.4.3).

**`crc32q`**: the loop of §38.4.5 is `crc32q (%rdi), %rax; add $8, %rdi; cmp; jne` — one instruction per 8 bytes with a 3-cycle loop-carried dependency through `%rax`, which is why the single-stream number is latency-bound and the three-stream version exists.

## 38.6 Pitfalls & anti-patterns

- **Masking the low bits of a multiplicative hash.** The §38.3 row that says 37 probes instead of 0.89. Shift, or fold once; and when you pass a hash functor to a library, know whether it masks (§38.4.3).
- **`std::string` symbols on the hot path.** An allocation, a length and a byte loop per message, then a 5-cycle hash of what should have been one 8-byte load (§38.4.2). Ch. 25's cost model says the same from the string side.
- **Identity on structured keys in a power-of-two table.** Fine for dense IDs, 18,084 probes for space-padded symbols (§38.3). The key's structure, not its type, decides.
- **A heavier hash as a substitute for a better key.** splitmix/wyhash at 10 cycles of latency on a lookup that could have been a direct index at 1 (§38.4.1, Ch. @MULTIVENUE@). Translate to dense ids at the edge.
- **`XXH3_64bits` on 8-byte keys through the library call.** 8.4/16.6 cycles against 3.5 for a multiply (§38.3): the length dispatch and the call dominate. Inline it (`XXH_INLINE_ALL`) for strings; use the multiply for integers.
- **Unkeyed hash on wire-chosen keys at a trust boundary.** Floodable (§38.4.6). Seed it, bound the table, fuzz the probe lengths.
- **SipHash everywhere "to be safe."** 8× the cost on tables no adversary can reach. Reserve it for the one or two that face a chooser and need the cryptographic argument (§38.4.6).
- **Treating `CKSUM_UNKNOWN` as good.** The NIC could not parse the frame; the software must verify it or drop it (§38.4.5). And a CRC is not a hash for hostile input — it is linear.
- **`%` by a non-power-of-two capacity.** A 20–40-cycle `div` on every lookup; Lemire's multiply-shift range or a power-of-two capacity with the shift (§38.4.3).
- **Rebuilding the perfect hash at runtime "because the set might change."** Then it is not a perfect hash; it is a slower open-addressing table. The closed set is a *build* input; intraday additions live in a separate ordinary table (§38.4.4).
- **Computing the journal CRC on the hot writer.** 32 cycles per record on the core that should be publishing the next tick; the drain thread on the housekeeping core computes it (§38.4.5, Ch. 109 §109.4.3).

## 38.7 Exercises & checklist

**Exercises**

1. **Reproduce the probe table.** Build `hashbench.cpp`; add your own venue's symbol list and order-ID pattern as key sets; confirm which hashes are structure-sensitive on *your* keys. Then change the table to take the low bits and watch Fibonacci collapse.
2. **Find the entropy.** For your symbol set, print the per-bit entropy of the 64-bit key (which bit positions vary); explain the identity row of §38.3 from it, and predict which bits a mask must include.
3. **Perfect hash the message types.** Take your feed's message-type bytes (ITCH: `A`, `D`, `E`, `F`, `P`, `U`, `X`, …); search a single multiplier at compile time for a 256-slot table; replace a `switch` dispatch with `handlers[ph.find(type)]` and compare the asm (Ch. 16 §16.4.3's jump-table discussion) and the cycles.
4. **Perfect hash the universe.** Generate your daily instrument list as a `constexpr` array (a build step from the definition feed), build the CHD, measure lookup vs the Ch. 32 flat table on the same keys, and time the compile. Decide the key count at which the build time stops being worth it.
5. **Flood it.** Write an attacker that generates `ClOrdID` strings colliding under an unkeyed Fibonacci slot (invert the multiply — it is a bijection) and feed them to a gateway table; measure the probe length and the per-message latency. Seed the hash; repeat; then try SipHash and compare the cost per message.
6. **CRC on the drain.** Add a CRC32C per record to a Ch. 109 journal writer, first on the hot writer, then on the drain thread; measure the hot thread's p99.9 both ways. Then implement the three-stream interleave and measure cycles/byte on 64 KiB blocks.

**Checklist — hashing, checksums and keys**

- [ ] Every hot-path key is **dense id → direct index** or **fixed-width → one 64-bit word**; strings are hashed once at the edge into a dense id (§38.2.3, §38.4.1).
- [ ] Multiplicative hashes use the **high bits** (shift) or one fold; the table library's masking behaviour is known; no `%` by a non-power-of-two capacity (§38.4.3).
- [ ] The **probe-length histogram** of every production table has been measured on *production-shaped* keys, not random ones (§38.3).
- [ ] Closed key sets (message types, the daily instrument universe) use a **compile-time perfect hash**; intraday additions go to a separate table (§38.4.4).
- [ ] Tables keyed by **wire-chosen strings at a trust boundary** use a **seeded** hash, are bounded, and are fuzzed with adversarial keys; SipHash only where the cryptographic argument is needed (§38.4.6).
- [ ] Journal records and custom frames carry **CRC32C**, computed off the hot core; NIC checksum flags are trusted only when they say *good*, and `UNKNOWN` is verified in software (§38.4.5).
- [ ] The hash **inlined** (no `call` in the lookup), the slot is `imul`+`shr`, and the perfect-hash lookup has no loop — confirmed in asm (§38.5).

## 38.8 References

- D. E. Knuth, *The Art of Computer Programming*, Vol. 3, §6.4 — multiplicative hashing and the golden ratio (Fibonacci hashing), the three-distance theorem (§38.4.3).
- J.-P. Aumasson and D. J. Bernstein, "SipHash: a fast short-input PRF" (INDOCRYPT 2012); S. Crosby and D. Wallach, "Denial of service via algorithmic complexity attacks" (USENIX Security 2003) — hash flooding and its defence (§38.4.6).
- Y. Collet, *xxHash* (`XXH3` design notes, `XXH_INLINE_ALL`); Wang Yi, *wyhash*; N. Vigna, *rapidhash* — the modern mixer family and their seeding (§38.2.2, §38.4.1).
- D. Belazzougui, F. Botelho, M. Dietzfelbinger, "Hash, displace, and compress" (ESA 2009) — the CHD construction of §38.4.4; R. Pagh, "Hash and displace: efficient evaluation of minimal perfect hash functions" (WADS 1999).
- D. Lemire, "Fast random integer generation in an interval" (ACM TOMACS 2019) and "A fast alternative to the modulo reduction" — the multiply-shift range of §38.4.3.
- V. Gopal et al., "Fast CRC computation for iSCSI polynomial using CRC32 instruction" (Intel white paper, 2011) — three-stream interleaved `crc32q` with PCLMUL recombination (§38.4.5); Intel *SDM* Vol. 2, `CRC32` and `PCLMULQDQ`.
- A. Appleby, *MurmurHash3* and the `fmix64` finalizer; S. Vigna and D. Blackman, *splitmix64* — the finalizer rows of §38.2.2.
- The DPDK `rte_mbuf` offload-flag documentation (`RTE_MBUF_F_RX_L4_CKSUM_*`) and AMD/Solarflare *ef_vi* user guide (RX event checksum bits) — §38.4.5's descriptor flags.

## 38.9 Additional Reading

- M. Kulukundis, "Designing a Fast, Efficient, Cache-friendly Hash Table, Step by Step" (CppCon 2017) — SwissTable, and what its own mixing expects of your `Hash` functor; M. Ankerl, *unordered_dense* and the hash-map benchmark series.
- Ch. 32 (*Containers*) — the table the hash feeds; Ch. 33 (*LOB*) — the order-ID map; Ch. 25 (*Strings Without Allocation*) — `Sym8` from the string side; Ch. 70 (*Decoding*) — the 8-byte load; Ch. 101 (*Security*) — the trust boundary; Ch. 109 (*Capture*) — the journal CRC.
- **Appendix I.9** — `objdump` on the lookup; **Appendix E** — the hash and CRC rows; **Appendix O.8** — the layout-and-caches catalogue this chapter's probe lengths belong to.
- The brainstorm review's unscheduled data-structure items (bounded-probe and cuckoo tables, Bloom/cuckoo filters and sketches, fast division beyond the range trick) are adjacent to this chapter and not covered by it.

---

*Next: Ch. 39 — SIMD & Vectorization: auto-vectorization, intrinsics, SSE/AVX/AVX-512, data layout for vectorization, and when SIMD pays off — the width that turns Ch. 36's polynomial into a chain re-price and this chapter's group probing into one compare.*
