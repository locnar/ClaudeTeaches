# Part III — Compile-Time & Language Mechanics

# Chapter 23 — Compile-Time Protocol Decoders: From Schema to Code

> **Prerequisites:** Ch. 21 (compile-time mechanics — `constexpr`/`consteval`, the template cost model; this chapter is the worked example §21.1 promised), Ch. 22 (templates as a design tool — type-based dispatch, `if constexpr`, the §22.4.6 concepts), Ch. 26 (aliasing and the sanctioned punning channels — every accessor generated here is a `memcpy` into a typed local), Ch. 37 (`std::byteswap`/`bswap`/`movbe`), Ch. 70 (the hand-written zero-copy decoder this chapter must match instruction for instruction).
>
> **Leads into:** Ch. 24 (the cost of abstractions — where the generated accessors' return types meet the calling convention, §24.4.5), Ch. 25 (`string_view` fields and symbols-as-integers, which the `Chars` encoding hands you), Ch. 28 (what the `consteval` parse costs the build and how to keep that cost bounded), Ch. 70 (the decode path proper: line arbitration, gaps, microbursts — this chapter only generates the field accessors it uses) and Ch. 106 (binary journal records are the same schema → layout problem, captured together with the schema version).

---

## 23.1 Why it matters: the venue document is the source of truth, and it changes

Every feed handler in this book starts the same way: a venue publishes a PDF that says *AddOrder is 38 bytes: type at 0, sequence at 1 (8 bytes, big-endian), order id at 9, side at 17, quantity at 18 (4 bytes), symbol at 22 (8 chars), price at 30 (8 bytes, 4 implied decimals)*, and a developer transcribes it into a `#pragma pack(1)` struct and a decoder that `memcpy`s each field into a typed local and byte-swaps it (Ch. 70 §70.3). The transcription is where the bugs live. Nasdaq's ITCH 5.0 has fifty-odd message types; CME's SBE schemas have hundreds of fields across dozens of templates and *ship as XML* precisely so machines can read them; Eurex's EOBI, ASX's, B3's — every venue has its own, every one changes a few times a year, and the change note that moves `price` from offset 30 to 31 arrives as a line in a document, not as a compiler error. A feed handler that decodes the wrong offset does not crash. It trades on the wrong price.

The compile-time answer is to stop transcribing. Write the venue's field list *once*, as data — a schema string, a `constexpr` table — and have the compiler derive the offsets, the sizes, the byte-swap and the typed accessor for every field, `static_assert` the result against the numbers in the venue document, and emit *exactly* the load-plus-swap the hand-written decoder would have. Then a venue change is one edit to one string; a typo in a field name is a compile error that names the field; a layout mismatch is a `static_assert` that fails on the build server, not a position that is wrong at 09:30:00.000. Ch. 21 §21.1 promised "a SBE/protocol decoder whose field layout is resolved at compile time"; Ch. 70 used an external code generator and moved on. This chapter builds the decoder in the language, on shipping C++20 — class-type non-type template parameters (P0732, GCC 10 / Clang 12 — Appendix H.3), `consteval` parsing, `if constexpr` accessor generation — and proves two things the hot path cares about: the generated accessor is the same `movbe` as the hand-written one (§23.5), and the whole message decodes in the same 2.5 ns (§23.3). The cost moves entirely into the build, and §23.3 measures that too.

---

## 23.2 Mental model: the schema is data; the decoder is a function of it

### 23.2.1 Three representations of one message

A wire message has three descriptions that must agree: the **venue document** (prose and tables), the **schema** (a machine-readable field list — SBE XML, a FAST template, or the one-line string this chapter uses) and the **code** (a struct, or accessors). Today's practice keeps all three by hand and reconciles them in code review. The compile-time decoder collapses the last two: the code *is derived from* the schema inside the compiler, so the only reconciliation left is schema ↔ document — and that is what `static_assert`s against the document's published offsets and lengths are for (§23.4.3). When a venue ships SBE XML, a 200-line Python script turns the XML into the schema string at build time (or you parse the XML in `consteval` — possible, not worth it); the point is that nothing between the schema and the hot path is typed by a human.

### 23.2.2 Class-type NTTPs and `fixed_string`: a string literal as a type-level value

Since C++20 a template can take a *class object* as a non-type template parameter, provided the class is **structural** — all public members, themselves structural (ints, enums, arrays of those, other structural classes), with a defaulted `operator==`. A `fixed_string<N>` wrapping a `char v[N]` qualifies, so `Message<"type:u8 seq:u64be price:u64be">` and `m.get<"price">()` are legal: the string literal becomes part of the *type*, two `Message`s with different schemas are different types, and `"price"` is an argument the compiler can compare against the field table at compile time. This is the mechanism that lets a field be addressed by *name* without a runtime string compare, a hash or an enum you keep in sync by hand. (Before C++20 the idiom needed `template <char...>` packs and a GNU string-literal operator template; the C++20 form is portable to GCC 10+ and Clang 12+ — Appendix H.3.)

### 23.2.3 `consteval` parsing and the field table

A `consteval` function *must* run at compile time: calling it where the result cannot be a constant is an error. The schema parser is `consteval`: it walks the string, splits tokens, maps `u64be` to an encoding enum and a size, accumulates offsets, rejects duplicate names and unknown types by `throw`ing — and a `throw` in a constant evaluation is a *compile error whose message is the thrown string*. The result is a `std::array<FieldDesc, N>` that becomes a `static constexpr` member of `Layout<Schema>`: a table of `{name, encoding, offset, size}` that exists only in the compiler's constant evaluator unless something ODR-uses it at runtime (nothing here does — §23.5 shows the table leaves no `.rodata` behind). The accessor `get<"price">()` indexes that table at compile time (`index_of` is also `consteval`, and a misspelt name throws), then `if constexpr` selects the body for the field's encoding: a `memcpy` into a `uint64_t` and a `byteswap`, or a byte load, or a `string_view` over the bytes. Each `if constexpr` branch not taken is discarded — the generated function has *one* body, the same one you would write by hand.

### 23.2.4 What C++26 static reflection would change (unverified here)

P2996 (*Reflection for C++26*, adopted into the C++26 working draft in 2025) adds `^^T` to reflect a type, `std::meta::nonstatic_data_members_of` to enumerate its members, `offset_of`/`size_of`/`type_of` queries and `[: r :]` to splice a reflection back into code. With it the schema can be an ordinary `struct WireAddOrder { be<u64> seq; … }` and the field table *derived from the struct*, closing the remaining gap (today the struct and the schema string are two descriptions). **No compiler we build with implements it**: GCC 13.3 rejects `-std=c++26` outright, Clang 18.1 has no `-freflection`, and the only implementation is Bloomberg's experimental `clang-p2996` fork. Everything below stands on C++20; §23.4.6 sketches the reflection form and labels it as such, and Appendix H carries the row.

---

## 23.3 Measure it: decode latency identical, build time proportional to message types

*Measured on the 4-vCPU Intel Xeon VM of Appendix L.2 (TSC 2.7 GHz, Linux 6.18, no isolation), GCC 13.3 and Clang 18.1, `-O2 -std=c++23 -march=x86-64-v3`; numbers are VM-measured and labelled as such — Appendix L.3 for the reference machine. The listing is complete and compiles as shown; the header it includes is §23.4.1–§23.4.2.*

```cpp
// decode_bench.cpp — generated (this chapter) vs hand-written (Ch. 70 §70.3) decoder of one ITCH-like AddOrder.
// g++ -O2 -std=c++23 -march=x86-64-v3 decode_bench.cpp -o decode_bench && ./decode_bench
#include "ct_decoder.hpp"                                   // §23.4.1–§23.4.2
#include <chrono>
#include <cstdio>
#include <vector>

// ---- the venue's document, transcribed ONCE -----------------------------------------------------
using AddOrderMsg = ct::Message<"type:u8 seq:u64be id:u64be side:u8 qty:u32be sym:c8 price:u64be">;
using AddOrderL   = AddOrderMsg::L;

// ---- Ch. 70's hand-written overlay/decoder, unchanged --------------------------------------------
#pragma pack(push, 1)
struct AddOrder { std::uint8_t type; std::uint64_t seq, id; std::uint8_t side;
                  std::uint32_t qty; char sym[8]; std::uint64_t price; };
#pragma pack(pop)
struct Parsed { std::uint64_t seq, id, price; std::uint32_t qty; char side; };

// The venue's published offsets, checked against BOTH descriptions at compile time (§23.4.3):
static_assert(AddOrderL::size == 38 && sizeof(AddOrder) == 38);
static_assert(AddOrderL::field<"seq">.offset   == 1  && offsetof(AddOrder, seq)   == 1);
static_assert(AddOrderL::field<"qty">.offset   == 18 && offsetof(AddOrder, qty)   == 18);
static_assert(AddOrderL::field<"sym">.offset   == 22 && offsetof(AddOrder, sym)   == 22);
static_assert(AddOrderL::field<"price">.offset == 30 && offsetof(AddOrder, price) == 30);

Parsed decode_hand(const std::uint8_t* p) {                 // Ch. 70: memcpy into a typed local + byteswap
    Parsed r;
    std::uint64_t seq; std::memcpy(&seq, p+1, 8);  r.seq = std::byteswap(seq);
    std::uint64_t id;  std::memcpy(&id,  p+9, 8);  r.id  = std::byteswap(id);
    r.side = char(p[17]);
    std::uint32_t qty; std::memcpy(&qty, p+18, 4); r.qty = std::byteswap(qty);
    std::uint64_t px;  std::memcpy(&px,  p+30, 8); r.price = std::byteswap(px);
    return r;
}
Parsed decode_gen(const std::uint8_t* p) {                  // this chapter: every offset/size/swap derived
    AddOrderMsg m{p};
    return { m.get<"seq">(), m.get<"id">(), m.get<"price">(), m.get<"qty">(), char(m.get<"side">()) };
}
std::uint64_t price_hand(const std::uint8_t* p) { std::uint64_t px; std::memcpy(&px, p+30, 8); return std::byteswap(px); }
std::uint64_t price_gen (const std::uint8_t* p) { return AddOrderMsg{p}.get<"price">(); }     // for the §23.5 diff

int main() {
    const std::size_t N = 1u << 20, REPS = 20;
    std::vector<std::uint8_t> buf(N * 38);
    for (std::size_t i = 0; i < N; ++i) { auto* m = buf.data() + i*38; m[0] = 'A'; m[17] = (i & 1) ? 'B' : 'S';
        for (int k = 1; k < 17; ++k) m[k] = std::uint8_t(i >> (k & 7)); for (int k = 18; k < 38; ++k) m[k] = std::uint8_t((i*7) >> (k & 7)); }
    using F = Parsed(*)(const std::uint8_t*);
    volatile F fns[2] = { decode_hand, decode_gen };        // volatile: keep both out of line and un-merged
    for (int which = 0; which < 2; ++which) {
        F f = fns[which]; std::uint64_t acc = 0;
        auto t0 = std::chrono::steady_clock::now();
        for (std::size_t r = 0; r < REPS; ++r)
            for (std::size_t i = 0; i < N; ++i) { Parsed p = f(buf.data() + i*38); acc += p.seq ^ p.id ^ p.price ^ p.qty ^ p.side; }
        auto t1 = std::chrono::steady_clock::now();
        std::printf("%-12s %.3f ns/msg  (acc=%llx)\n", which ? "generated" : "hand-written",
                    std::chrono::duration<double, std::nano>(t1 - t0).count() / double(N*REPS), (unsigned long long)acc);
    }
}
```

**Run time (VM-measured, two runs each; 1 M messages × 20, 38-byte stride, L2-resident):**

```
                     GCC 13.3            Clang 18.1
hand-written         2.54 – 2.67 ns/msg  2.48 – 2.74 ns/msg
generated            2.50 – 2.51 ns/msg  2.40 – 2.50 ns/msg
```

The two are the same function to the machine (§23.5 shows the identical instruction multiset); the ±0.1 ns is VM run-to-run noise, not a difference between them. There is no runtime trace of the schema string, the field table or the `consteval` parser.

**Build time** — the cost moved here. A translation unit with *N* message types, each with seven fields and a decoder that reads five of them, generated vs hand-written (`-O2 -c`, best of three):

```
                      N = 1          N = 16          N = 64
GCC 13.3   hand        151 ms          171 ms          263 ms
           generated   151 ms          236 ms          506 ms       ≈ +3.8 ms per message type
Clang 18.1 hand        197 ms          210 ms          261 ms
           generated   202 ms          305 ms          621 ms       ≈ +5.7 ms per message type
```

`clang -ftime-trace` on the 16-type TU puts the whole file at 296 ms: front end 272 ms (of which `InstantiateFunction` 56 ms — the `get<Name>()` instantiations at 2.9–3.5 ms *each*, one per distinct `(schema, field)` pair — and `ParseClass` 31 ms), back end 22 ms. The generated decoder is a front-end cost: roughly **4–6 ms per message type**, linear, and paid once per translation unit that instantiates it. For a venue with 60 message types that is a quarter-second in *one* TU — fine; it becomes a problem only if forty TUs each re-instantiate the same decoders, which is a build-structure decision Ch. 28 takes up (`extern template` on the `Message` instantiations, one decoder TU per venue). The `-ftime-trace` workflow itself is Ch. 28 §28.3.

---

## 23.4 Techniques

### 23.4.1 From schema string to `constexpr` field table

The whole header is ~120 lines; the parts that matter are the structural NTTP, the `consteval` parser and the table.

```cpp
// ct_decoder.hpp — schema string → field table → overlay accessors. C++20 (std::byteswap is C++23; fallback below).
#include <algorithm>
#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string_view>
namespace ct {

template <std::size_t N>
struct fixed_string {                                        // structural: usable as a template argument (P0732)
    char v[N]{};
    constexpr fixed_string(const char (&s)[N]) { std::copy_n(s, N, v); }
    constexpr std::string_view sv() const { return {v, N - 1}; }
    constexpr bool operator==(const fixed_string&) const = default;
};

enum class Enc : std::uint8_t { U8, U16BE, U32BE, U64BE, U16LE, U32LE, U64LE, Chars };
struct FieldDesc { std::string_view name; Enc enc; std::size_t offset, size; };

constexpr std::size_t enc_size(Enc e, std::size_t n) {
    switch (e) { case Enc::U8: return 1; case Enc::U16BE: case Enc::U16LE: return 2;
                 case Enc::U32BE: case Enc::U32LE: return 4; case Enc::U64BE: case Enc::U64LE: return 8;
                 case Enc::Chars: return n; }
    return 0;
}
consteval std::size_t count_fields(std::string_view s) {
    std::size_t n = 0; bool in = false;
    for (char c : s) { if (c == ' ') in = false; else if (!in) { in = true; ++n; } }
    return n;
}
consteval std::uint64_t parse_uint(std::string_view s) {
    std::uint64_t v = 0;
    for (char c : s) { if (c < '0' || c > '9') throw "digit expected"; v = v * 10 + (c - '0'); }
    return v;
}
consteval FieldDesc parse_field(std::string_view tok, std::size_t offset) {   // "price:u64be", "sym:c8"
    auto colon = tok.find(':');
    if (colon == std::string_view::npos) throw "field needs name:type";
    std::string_view name = tok.substr(0, colon), ty = tok.substr(colon + 1);
    Enc e{}; std::size_t n = 0;
    if      (ty == "u8")    e = Enc::U8;
    else if (ty == "u16be") e = Enc::U16BE;  else if (ty == "u32be") e = Enc::U32BE;  else if (ty == "u64be") e = Enc::U64BE;
    else if (ty == "u16le") e = Enc::U16LE;  else if (ty == "u32le") e = Enc::U32LE;  else if (ty == "u64le") e = Enc::U64LE;
    else if (ty.starts_with("c")) { e = Enc::Chars; n = parse_uint(ty.substr(1)); }
    else throw "unknown field type";                      // a throw in consteval = a compile error carrying this text
    return {name, e, offset, enc_size(e, n)};
}

template <fixed_string Schema>
struct Layout {
    static constexpr std::size_t nfields = count_fields(Schema.sv());
    static consteval std::array<FieldDesc, nfields> parse() {
        std::array<FieldDesc, nfields> out{};
        std::string_view s = Schema.sv(); std::size_t off = 0, i = 0;
        while (!s.empty()) {
            auto sp = s.find(' '); std::string_view tok = s.substr(0, sp);
            if (!tok.empty()) { out[i] = parse_field(tok, off); off += out[i].size; ++i; }
            s = (sp == std::string_view::npos) ? std::string_view{} : s.substr(sp + 1);
        }
        for (std::size_t a = 0; a < nfields; ++a)           // duplicate names are a compile error
            for (std::size_t b = a + 1; b < nfields; ++b) if (out[a].name == out[b].name) throw "duplicate field name";
        return out;
    }
    static constexpr std::array<FieldDesc, nfields> fields = parse();
    static constexpr std::size_t size = fields.back().offset + fields.back().size;
    static consteval std::size_t index_of(std::string_view name) {
        for (std::size_t i = 0; i < nfields; ++i) if (fields[i].name == name) return i;
        throw "no such field";                               // get<"pirce">() fails HERE, naming the field
    }
    template <fixed_string Name> static constexpr FieldDesc field = fields[index_of(Name.sv())];
};
} // namespace ct
```

Design points: offsets are *derived*, never written (a field inserted in the middle shifts everything after it, exactly as the venue's document does); the schema is one string per message type and lives next to the version it came from; and every error the parser can detect is detected at the `static constexpr` initialisation — before a single byte is decoded. The parser is deliberately a toy grammar (`name:type` tokens): a production one adds implied-decimal scales, enumerations and presence flags to `FieldDesc` with the same shape.

### 23.4.2 Generating the overlay accessors

```cpp
namespace ct {
template <Enc E> struct enc_type;                            // encoding → C++ type
template <> struct enc_type<Enc::U8>    { using type = std::uint8_t;  };
template <> struct enc_type<Enc::U16BE> { using type = std::uint16_t; };  template <> struct enc_type<Enc::U16LE> { using type = std::uint16_t; };
template <> struct enc_type<Enc::U32BE> { using type = std::uint32_t; };  template <> struct enc_type<Enc::U32LE> { using type = std::uint32_t; };
template <> struct enc_type<Enc::U64BE> { using type = std::uint64_t; };  template <> struct enc_type<Enc::U64LE> { using type = std::uint64_t; };
template <> struct enc_type<Enc::Chars> { using type = std::string_view; };

template <class T> constexpr T bswap(T v) {                  // C++23 std::byteswap, or the builtins on C++20 (Ch. 37)
#if defined(__cpp_lib_byteswap)
    return std::byteswap(v);
#else
    if constexpr (sizeof(T) == 2) return __builtin_bswap16(v); else if constexpr (sizeof(T) == 4) return __builtin_bswap32(v); else return __builtin_bswap64(v);
#endif
}

template <fixed_string Schema>
struct Message {
    using L = Layout<Schema>;
    const std::uint8_t* p;                                   // the receive buffer — NOT copied, NOT reinterpret_cast (Ch. 26)
    template <fixed_string Name>
    [[gnu::always_inline]] constexpr auto get() const {
        constexpr FieldDesc f = L::template field<Name>;     // offset, size and encoding are compile-time constants here
        using T = typename enc_type<f.enc>::type;
        if constexpr (f.enc == Enc::Chars) return std::string_view(reinterpret_cast<const char*>(p + f.offset), f.size);
        else if constexpr (f.enc == Enc::U8) return p[f.offset];
        else {
            T v; std::memcpy(&v, p + f.offset, sizeof v);   // defined for any alignment; folds to one load (§23.5)
            if constexpr (f.enc == Enc::U16BE || f.enc == Enc::U32BE || f.enc == Enc::U64BE) return bswap(v);
            else return v;
        }
    }
};
} // namespace ct
```

Three things make this zero-cost rather than merely convenient. The field descriptor is a `constexpr` *object* inside the function, so `f.offset` is an immediate in the address mode, not a load from a table. The `memcpy` of a constant size into a local is the Ch. 26 sanctioned pun, and both compilers fold it to a single load — there is no payload copy and no aliasing UB, which is also why this works on a `std::byte` receive buffer that a `reinterpret_cast<const AddOrder*>` overlay would not legally allow (Ch. 26 §26.4.2 for the C++23 `std::start_lifetime_as` form of the struct overlay). And `if constexpr` leaves one body per instantiation: the big-endian branch *is* the function for `price`, the `string_view` branch *is* the function for `sym`. The `always_inline` is belt-and-braces; at `-O2` both compilers inline a one-instruction accessor anyway.

Return types follow the encoding, so `get<"price">()` is a `std::uint64_t` and `get<"sym">()` a `std::string_view` over the buffer (the lifetime rule of Ch. 25 §25.4.4 applies: it is a view *into the datagram*). The natural extension — a `Price` strong type (Ch. 22 §22.4.4) for fields tagged with an implied-decimal scale — adds an entry to `FieldDesc` and one more `if constexpr`, and the §22.5 proof says it still rides in a register.

### 23.4.3 `static_assert` against the venue document and the schema version

The schema string replaces the struct; the `static_assert`s replace the code review. Three kinds, all free:

- **Against the published numbers.** `static_assert(AddOrderL::field<"price">.offset == 30)` and `static_assert(AddOrderL::size == 38)` transcribe the two numbers the venue document states for each field — and a transcription error in *either* the schema or the assertion fails the build. When the venue moves `price` to 31, the first build after the schema edit fails every assertion downstream until each is re-read against the new document. That is the workflow: the document's numbers live in `static_assert`s, the structure lives in the schema, and they check each other.
- **Against the legacy struct during migration.** While the hand-written `#pragma pack(1)` struct still exists (Ch. 70), `static_assert(AddOrderL::field<"qty">.offset == offsetof(AddOrder, qty))` proves the two descriptions agree for every field, so the migration from struct to schema cannot silently change a layout. Delete the struct when the last consumer is gone.
- **Against the schema version.** Venues version their templates (SBE's `schemaId`/`version`/`blockLength` in the message header; ITCH's version in the feed name). Put the expected version in the schema (`Message<"v5.0 …">` or a second NTTP), make the decoder's first act a runtime check of the header's version word against the compile-time constant, and reject the session, not the message, on mismatch. For SBE specifically, `static_assert(Layout::size == 38 /* blockLength from the XML */)` catches the root-block drift, and the *runtime* `blockLength` from each header must be ≥ the compile-time one before any field past the known block is read (§23.6).

### 23.4.4 Message-type dispatch and repeating groups: what stays at runtime

The compile-time decoder resolves everything that is *fixed* in the schema. Two things are not, and the design must say where they go:

- **Which message type arrived.** The type byte is runtime data. The idiomatic generated dispatcher is a `switch` over the type byte whose cases are `Message<SchemaN>` instantiations — a jump table (Ch. 16 §16.4.4) with one predictable indirect branch, the same cost as the hand-written handler. A variadic `MessageSet<AddOrderSchema, ExecSchema, …>` can generate the `switch` from the list (fold expression over `if (type == S::type_byte)`), which keeps the type byte beside the schema it belongs to. Do not route this through `std::variant`: the variant *adds* a tag you already have on the wire.
- **Repeating groups and variable-length tails.** SBE groups (`noInGroup` × `blockLength` entries) and FIX/FAST variable-length fields make offsets *data-dependent*; no compile-time scheme removes that arithmetic. The generated code handles the fixed root block and hands the group a `(base pointer, count, entry blockLength)` read at runtime; the group's *entry* is itself a fixed-layout `Message<GroupEntrySchema>` positioned at `base + i * entry_len`. The runtime `entry_len` — not the compile-time `Layout::size` — is what you stride by, because a newer schema version may have appended fields to the entry (SBE's forward-compatibility rule). This is the one place a compile-time offset would be a bug.

### 23.4.5 Keeping the build-time cost bounded

§23.3 priced a message type at 4–6 ms of front-end time per instantiating TU. Three habits keep that from compounding: instantiate each venue's `Message<…>` set in *one* decoder translation unit and expose a small non-template interface (or `extern template` the instantiations — Ch. 28 §28.4.2); keep the `consteval` parser's complexity linear in the schema length (the duplicate-name check above is O(n²) in field count, which is fine at 60 fields and would not be at 6,000 — SBE schemas with thousands of fields want a sorted check); and watch `-ftime-trace`'s `InstantiateFunction` rows in CI the way Ch. 116 watches a latency percentile — a decoder header that goes from 300 ms to 3 s is a regression even though no runtime number moved.

### 23.4.6 The C++26 reflection form (unverified: no shipping compiler implements it)

With P2996 the schema can be the struct itself and the field table derived from it:

```cpp
// C++26 static reflection, P2996 — NOT compiled by GCC 13.3 or Clang 18.1; shown for orientation only.
template <class T> struct be { T raw; };                      // tag type: "this field is big-endian on the wire"
struct WireAddOrder { std::uint8_t type; be<std::uint64_t> seq, id; std::uint8_t side; be<std::uint32_t> qty; char sym[8]; be<std::uint64_t> price; };

template <class W> consteval auto field_table() {             // enumerate members, derive offset/size/encoding
    constexpr auto ms = std::meta::nonstatic_data_members_of(^^W, std::meta::access_context::unchecked());
    std::array<FieldDesc, ms.size()> out{}; std::size_t i = 0;
    template for (constexpr auto m : ms)                      // expansion statement (P1306)
        out[i++] = { std::meta::identifier_of(m), encoding_of(std::meta::type_of(m)), std::meta::offset_of(m).bytes, std::meta::size_of(m) };
    return out;
}
```

The accessor generation (§23.4.2) is unchanged — it consumes `FieldDesc`s and does not care where they came from — which is the argument for building the C++20 version now: the table is the interface, and reflection only changes how the table is produced. Gate any adoption on `__cpp_reflection` (Appendix H) and keep the string-schema path until GCC and Clang both ship it.

---

## 23.5 Verify the codegen: the generated accessor is the hand-written instruction

The claim "the compile-time decoder costs nothing at runtime" is an asm claim. **Verified** Clang 18.1 and GCC 13.3 output for the §23.3 listing (`-O2 -std=c++23 -march=x86-64-v3 -S -masm=intel`):

```asm
; price_hand(const uint8_t*)  — Ch. 70's memcpy + byteswap          ; price_gen(const uint8_t*) — Message::get<"price">()
        movbe   rax, qword ptr [rdi + 30]                            ;         movbe   rax, qword ptr [rdi + 30]
        ret                                                          ;         ret
; GCC 13.3 emits the same two instructions for both (plus endbr64).  The schema string, the field table,
; index_of(), the consteval parser and the if constexpr chain left NOTHING: offset 30 is an immediate.
```

**The whole decoder.** `decode_hand` and `decode_gen` compile to the *same multiset of instructions*; Clang's bodies differ only in order (the generated version reads fields in the order the `Parsed` initialiser names them) and the label of one constant:

```asm
; decode_hand / decode_gen (Clang 18.1) — identical instructions, two reordered:
        mov     rax, rdi
        vmovdqu xmm0, xmmword ptr [rsi + 1]             ; seq and id: one 16-byte load…
        vpshufb xmm0, xmm0, xmmword ptr [rip + .LCPI]   ; …byte-swapped together (Ch. 70 §70.5)
        vmovdqu xmmword ptr [rdi], xmm0
        movzx   ecx, byte ptr [rsi + 17]                ; side
        mov     byte ptr [rdi + 28], cl
        mov     ecx, dword ptr [rsi + 18]               ; qty
        movbe   dword ptr [rdi + 24], ecx               ;   byte-swapping store
        mov     rcx, qword ptr [rsi + 30]               ; price
        movbe   qword ptr [rdi + 16], rcx
        ret
```

`diff <(sort hand.s) <(sort gen.s)` is empty for both compilers — which is the test to automate: generate the asm of the hand-written reference decoder once, and fail CI if the generated decoder's sorted instruction list diverges. **The table is not in the binary**: `nm -C decode_bench | grep -i fields` finds no `Layout<…>::fields` symbol at `-O2`; nothing ODR-uses it at runtime. (At `-O0` it is emitted, as every `static constexpr` is; the rule is Ch. 4's: read `-O2`.)

**The compile-error path is also a deliverable.** `m.get<"pirce">()` fails as:

```
ct_decoder.hpp:98: error: expression '<throw-expression>' is not a constant expression     (GCC 13.3, 81 lines total)
        throw "no such field";
ct_decoder.hpp:101: error: constexpr variable 'field<fixed_string<6>{"pirce"}>' must be initialized by a constant expression   (Clang 18.1, 59 lines)
```

Both name the misspelt field in the template argument; neither is short. Ch. 22 §22.4.6 compares the diagnostics of the four dispatch mechanisms; the `consteval`-throw idiom is the one that lets *you* choose the message text, and a `static_assert` with a custom message at the call site shortens it further.

---

## 23.6 Pitfalls & anti-patterns: compile-time layout, runtime data

- **A compile-time offset for a runtime quantity.** The root block of an SBE message is fixed; a repeating group's entry stride (`blockLength`) and count are *in the header*. Striding by `Layout::size` instead of the header's `blockLength` works until the venue appends a field to the group entry — forward-compatible by the SBE rules, silently misaligned by your decoder (§23.4.4). Compile-time for the fixed part, header values for the variable part, always.
- **Trusting `blockLength` without bounding it.** The converse: a header that *claims* a longer block than the datagram holds. The `static_assert` proves the schema; only a runtime `offset + size <= remaining` check proves the packet (Ch. 70 §70.6, Ch. 99). The generated accessor does not bounds-check — the caller checks the message length once against `Layout::size` before any `get<>()`.
- **Instantiating the same decoder in forty TUs.** 4–6 ms per message type per TU (§23.3) is invisible once and a minute of wall time across a large binary. One decoder TU per venue, `extern template` for the rest (§23.4.5, Ch. 28).
- **The schema string as the *only* description.** A schema without the `static_assert`s against the document's numbers has merely moved the transcription error; the schema *and* the asserted offsets must both be re-read when the venue publishes a change (§23.4.3). Keep the document version in the schema's name.
- **`reinterpret_cast` back into the overlay for "speed".** The `memcpy`-into-local form *is* the load (§23.5). A `reinterpret_cast<const uint64_t*>(p + 30)` dereference is strict-aliasing UB and a potentially misaligned access (Ch. 26 §26.6, Ch. 70 §70.6) for zero gain.
- **A `string_view` field that outlives the buffer.** `get<"sym">()` views the receive buffer; copy it into a `Sym8`/`uint64` key (Ch. 25 §25.4.4) before the buffer is recycled — the ring slot will be overwritten by the next datagram.
- **Non-structural NTTP types.** A `fixed_string` with a private member, a `std::string_view` member, or a user-provided `operator==` is not structural and the template will not compile — the error messages for this are poor on both compilers; keep the NTTP type to public `char[N]` plus a defaulted comparison.
- **Reflection-first design on today's toolchains.** No shipping GCC or Clang implements P2996 (§23.2.4); a decoder that *requires* it does not build. Build on the field table; let reflection replace the parser later.

---

## 23.7 Exercises & checklist

**Exercises**

1. **Add implied decimals.** Extend `FieldDesc` with a `scale` (e.g. `price:u64be/4`) and make `get<"price">()` return a `Price` strong type (Ch. 22 §22.4.4) carrying ticks. Confirm in the asm that the return is still one `movbe` into `rax`.
2. **Generate the dispatcher.** Write `MessageSet<S1, S2, …>` with a `dispatch(type_byte, p, handler)` that expands to a `switch`. Compare its asm and `br_misp_retired.indirect` (Ch. 16 §16.3) against a hand-written `switch` over the same types.
3. **Break the document.** Change `price:u64be` to `price:u32be` in the schema and leave the `static_assert`s. Read the failure. Then change the assertion instead and read *that* failure. Which one would you rather see at 06:00 on a venue-upgrade day?
4. **Parse real SBE.** Write a 100-line script that turns a CME SBE XML template into the schema string of §23.4.1 (root block only), generate the decoder, and `static_assert` `Layout::size == blockLength` from the XML.
5. **Price the build.** Reproduce the §23.3 build-time table with `-ftime-trace` (Ch. 28 §28.3) on your own venue's message set, then apply `extern template` to the `Message<…>` instantiations and measure the per-TU saving.
6. **Reflection, if you can.** On a `clang-p2996` build, derive the field table from the §23.4.6 struct and `static_assert` it equal to the string-schema table for the same message. Note what you had to change in the accessor layer (it should be nothing).

**Checklist — compile-time decoders**

- [ ] Each venue message type is described **once**, in a schema the compiler reads; offsets and sizes are **derived**, never typed by hand.
- [ ] The venue document's published offsets and total length are `static_assert`ed against the derived layout, and the schema names the document version.
- [ ] The generated field accessor compiles to the **same instruction** as the hand-written `memcpy` + `byteswap` (`movbe`/`bswap`), verified by an asm diff in CI (§23.5).
- [ ] Repeating-group strides and counts come from the **message header at runtime**; only the fixed root block uses compile-time offsets (§23.4.4).
- [ ] The message length is checked once against `Layout::size` before any accessor runs; no accessor is called on an unchecked datagram.
- [ ] Decoder instantiations live in one TU per venue (`extern template` elsewhere), and the decoder header's `-ftime-trace` cost is tracked (Ch. 28).
- [ ] Nothing in the build depends on C++26 reflection; its use is gated on `__cpp_reflection` (Appendix H).

---

## 23.8 References

- ISO C++20, [temp.param] and P0732R2, *Class Types in Non-Type Template Parameters* (J. Maurer) — the structural-type rule behind `fixed_string` as a template argument (§23.2.2); P1907R1 for the final C++20 wording.
- ISO C++20, [dcl.constexpr] `consteval` (P1073R3, *Immediate functions*) and [expr.const] — why a `throw` in a constant evaluation is a compile-time error (§23.2.3).
- P2996 (*Reflection for C++26*, B. Revzin, W. Childers, P. Dimov, A. Katz, D. Vandevoorde, D. Stone) and P1306 (*Expansion statements*) — the reflection form sketched in §23.4.6; implementation status per Appendix H.
- FIX Trading Community, *Simple Binary Encoding (SBE) Technical Specification* — `blockLength`, `schemaId`/`version`, repeating groups and the forward-compatibility rules that keep group strides at runtime (§23.4.3–§23.4.4).
- Nasdaq, *TotalView-ITCH 5.0 Specification* — the fixed-layout, big-endian message family the examples model (Appendix J.4 for the field-level quick reference).
- Real Logic, *simple-binary-encoding* (reference SBE code generator) — what an external generator emits, for comparison with the in-language approach.

## 23.9 Additional Reading

- Jason Turner, *"constexpr ALL the Things!"* (C++Now 2017) and Ben Deane, *"Using Types Effectively"* — the compile-time-parsing style the `consteval` schema parser follows.
- Hana Dusíková, *"Compile Time Regular Expressions"* (CppCon 2018) — the canonical C++17/20 example of parsing a string literal into code at compile time, and the NTTP idioms that preceded P0732.
- Daveed Vandevoorde and Barry Revzin, *"Reflection in C++26"* (CppCon 2024) — the P2996 design and the `^^`/`[: :]` syntax.
- Ch. 21 (*Compile-Time Mechanics*) — the cost model this chapter instantiates; Ch. 70 (*Zero-Copy Wire Handling & Market-Data Decoding*) — the hand-written decoder and the receive path it feeds; Ch. 28 (*Build Systems & Build-Time Hygiene*) — pricing and bounding the build cost measured in §23.3; **Appendix H** — availability of class-type NTTPs, `std::byteswap`, `consteval` and reflection; **Appendix J** — the venue protocols.

---

*Next: Ch. 24 — The Cost of Abstractions, the counterweight to Ch. 22 and this chapter: the abstractions that are not free — exception unwinding, RTTI, the diagnostics library — and, new to this edition, what the calling convention does to a `Price`, an `expected<Fill, Err>` or a `unique_ptr` passed by value, and what a function-local `static` or a `thread_local` quietly costs on every call.*
