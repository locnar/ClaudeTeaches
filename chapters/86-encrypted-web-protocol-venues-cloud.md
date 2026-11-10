# Part X — Kernel Bypass, RDMA & Transport

# Chapter 86 — Encrypted & Web-Protocol Venues and Trading in the Cloud: TLS/kTLS, WebSocket, JSON and REST on a Latency Budget

> **Prerequisites:** Ch. 68–69 (the kernel TCP path and its tuning — every protocol here rides it), Ch. 70–71 (the zero-copy overlay discipline and branch-free text parsing — what JSON decoding must approach), Ch. 62 (warming connections), Ch. 101 (the wire as untrusted input; secrets and signing keys), Ch. 105–106 (order identity, idempotent submission, the session machine), Ch. 85 (purpose-built transports — the fast end of the spectrum this chapter closes), Appendix S (VMs — the mechanics behind §86.4.6).
>
> **Leads into:** Ch. 87 (PCIe, opening Part XI — the boundary a cloud NIC's virtual function still crosses), Ch. 92 (SmartNICs/DPUs — TLS offload as the hardware answer to §86.2.1), Ch. 104 (the risk gate in front of a REST order), Ch. 119 (the capstone's tier table). Closes **Part X**.

---

## 86.1 Why it matters: the venues you cannot colocate with

Part IX and the first five chapters of Part X describe a world the book chose: binary feeds on multicast, a cross-connect in the venue's building, a NIC that stamps packets in hardware, a session protocol designed for a matching engine. A growing share of low-latency work happens in a different world — crypto exchanges, FX ECNs and broker APIs reached over the public internet or a cloud region, speaking **TLS** over TCP, **WebSocket** for market data, **JSON** for everything and **REST** (with rate limits) for orders — and the techniques transfer only partly. The latency tier is different by three orders of magnitude: a crypto venue's round trip from the nearest cloud region is 0.5–5 ms, its matching engine's own jitter is in the hundreds of microseconds, and the race is decided by the mandatory protocol stack and your distance from the venue's load balancer, not by a DDIO window. What *is* the same: the race is still a race, the tail is still what loses money, and every microsecond you spend decrypting a record, parsing a frame or allocating a JSON DOM is a microsecond someone else did not.

Measured below (§86.3): one TLS 1.3 record of a 1,400-byte market-data message costs **~2,500 cycles (0.9 µs) through OpenSSL's record layer** — of which the AES-GCM itself is 700 — and a 16 KiB record 9,500; a WebSocket frame parse with unmasking is 52 cycles; decoding a 154-byte JSON book update into the internal record is **96 ns with simdjson On-Demand, 203 ns with a hand-rolled scanner, and 1.8 µs with a DOM library**; a full TLS 1.3 handshake burns ~400 µs of CPU across both sides and a resumption ~280. Against a venue RTT of a millisecond those numbers look small until they are multiplied by a feed of 20,000 messages/s on one connection, or placed on the order path where 1 µs *is* the race margin against a competitor in the same availability zone.

The chapter: what a TLS record costs and where, and what kTLS changes (§86.2.1); handshakes, resumption and why 0-RTT is forbidden for orders (§86.2.2); WebSocket framing (§86.2.3); JSON and REST as wire formats (§86.2.4); the tier and what transfers from the bare-metal chapters (§86.2.5); the measurements (§86.3); the techniques — the connection, record handling without copies, the frame parser, simdjson into the overlay discipline, REST order entry with rate limits and signing, and trading from a cloud host (§86.4); and the pitfalls (§86.5). It stays honest about the floor: this chapter makes the software side of a ~1 ms path as flat as it can be; it cannot make the path short.

## 86.2 Mental model

### 86.2.1 The TLS record: what it costs, where, and what kTLS moves

TLS 1.3 (RFC 8446) frames application data into **records** of up to 16 KiB: a 5-byte header, the ciphertext, and a 16-byte AEAD tag. Sending a message is: copy it into the library's record buffer, encrypt-and-authenticate (AES-GCM or ChaCha20-Poly1305) into the output buffer, write the output buffer to the socket (a syscall, Ch. 68); receiving is the mirror — read into the library's buffer, verify-and-decrypt in place, copy out to the caller (or hand back a pointer, §86.4.2). The costs, measured in §86.3 through a memory BIO pair so that *only* the record layer is timed:

- **The AEAD is the floor and it is small**: AES-128-GCM with the key scheduled once is ~430 cycles for a 64-byte record (fixed cost: the GHASH setup, the tag) rising to ~700 for 1,400 bytes and 2,800 for 16 KiB — ~0.17 cycles/byte at length, which is what `vaesenc`/`vpclmulqdq` on AVX-512 deliver. ChaCha20-Poly1305 is 3–4× that on a CPU with AES-NI (it exists for CPUs without); AES-256 costs ~10% over AES-128. **Pick AES-128-GCM** unless policy says otherwise.
- **The record layer is 3× the AEAD**: ~2,000 cycles for the 64-byte record end to end through OpenSSL 3.0's `SSL_write`/`SSL_read` — buffer management, the record header, the two copies, the provider dispatch, the state checks. The ratio shrinks with record size (2,500 vs 700 at 1,400 bytes) but the *fixed* ~1,500 cycles per record is a cost per *message* for a feed that sends one small message per record, which most WebSocket venues do.
- **The copies and the syscalls are the rest**: a `recv` into the library's buffer and a copy out; a `send` from the library's buffer. **kTLS** (Linux's `tls` upper-layer protocol: `setsockopt(fd, SOL_TCP, TCP_ULP, "tls")` after the handshake, then `TLS_TX`/`TLS_RX` with the negotiated keys) moves the record layer *into the kernel*: `send()` on the socket encrypts in the TCP transmit path, `recv()` decrypts in the receive path, the application sees plaintext bytes through the ordinary socket API, and `sendfile`/`splice` work on an encrypted connection. It removes the user-space copies and the library's per-record overhead; the AEAD still runs, now in softirq context on *whichever core processes the socket's receive* (Ch. 64 §64.2.2 — which must not be the hot core), and the NIC can offload it entirely on hardware that supports `tls-hw-rx-offload` (Mellanox ConnectX-6 Dx+, Chelsio; Ch. 92). The guest this chapter measures on has no `tls` ULP built (`/proc/sys/net/ipv4/tcp_available_ulp` lists only `mptcp`), so kTLS is cited, not measured: published numbers from its authors and from Netflix/Facebook deployments put the saving at the two copies plus ~30–40% of the per-record CPU for small records, with the latency benefit being that the plaintext arrives in the socket buffer without a user-space round trip.

### 86.2.2 Handshakes: 1-RTT, resumption, and why 0-RTT is forbidden for orders

A full TLS 1.3 handshake is **one round trip** (ClientHello with key share → ServerHello, certificate, Finished → client Finished, then data) plus the CPU: an ECDHE key agreement on each side (X25519 or P-256, ~50–100 µs each), a certificate signature verification on the client, a signature on the server — **~400 µs of CPU across both sides** in §86.3, on top of the network RTT. **Resumption** (a pre-shared key from a `NewSessionTicket` the server sent after the previous handshake) skips the certificate work and, by default (`psk_dhe_ke`), keeps the ECDHE for forward secrecy — ~280 µs in §86.3 — still one RTT. **0-RTT** lets the client send application data *in its first flight*, before the server has replied: zero added RTT, and **replayable by construction** — an attacker (or a retrying load balancer) can deliver the same early-data record twice, and the server cannot tell. For a market-data subscription that is harmless; for an order it is a duplicate order, and venues that accept early data for order endpoints are doing something the RFC tells them not to. **Never send an order as 0-RTT data**, and treat a venue that accepts it as a bug report, not a feature. The practical pattern (§86.4.1) is: establish and *keep* the connections before the session — the handshake is a once-per-day cost, the RTT it hides is the thing you cannot buy back — and resume on reconnect.

### 86.2.3 WebSocket: a framing layer, not a transport

WebSocket (RFC 6455) is an HTTP/1.1 connection upgraded to a bidirectional message stream: a 2–14-byte frame header (FIN bit, opcode, length in 1/3/9 bytes, an optional 4-byte **masking key**), then the payload. Messages can be *fragmented* across frames (FIN = 0 until the last), **client-to-server frames must be masked** (XOR with the 4-byte key — a browser-security measure that costs you a pass over every byte you send, and that the venue's server pays on receipt), server-to-client frames are not, and control frames (ping/pong/close, ≤ 125 bytes) can be interleaved between fragments. The handler must therefore: parse the header branch-free (three length encodings), unmask client-side sends 8 bytes at a time (§86.4.3: 52 cycles for a 162-byte frame including the parse), reassemble fragmented messages into a contiguous buffer *only when a venue actually fragments* (most never do for market data; check), answer pings off the hot path within the venue's timeout (typically 10–60 s — a ping is liveness, Ch. 106 §106.4.4), and never let a close frame or a TCP FIN pass unnoticed (the venue's "disconnect" is often just silence, Ch. 69 §69.4.4). The TLS record and the WebSocket frame are **not aligned**: one record may hold several frames, one frame may span records; the parser works on the decrypted *stream*, with a frame-boundary state across reads (§86.4.3).

### 86.2.4 JSON and REST as wire formats: the parse is the decoder, the request is the session

JSON is a text encoding with no schema and no fixed layout: every field is found by scanning, every number is a decimal string, and the venue's `"b":"25.35190000"` price is a *string* (deliberately — the venue is telling you not to use a `double`; Ch. 35 §35.2.1). Three ways to turn it into the 48-byte internal record (§86.3): a **DOM parser** (nlohmann, RapidJSON's DOM) builds a tree with an allocation per node — 1.8 µs; a **hand-rolled scanner** that `memmem`s for the five keys you need — 203 ns and fragile (a key that appears inside a string value, a reordered field, an escaped quote, all break it); and **simdjson On-Demand**, which validates UTF-8 and structure with SIMD (Ch. 39), then walks fields forward in document order without materializing a tree — **96 ns**, with the robustness of a real parser. On-Demand's model matches Ch. 70's overlay discipline exactly: the document is the buffer, the fields are views into it, nothing is copied until you convert it (`dec_to_e8` on the price string, §86.4.4), and the *order* in which you ask for fields should be the order the venue emits them (a backward reference re-scans). Two things stay unavoidable: the **padding** simdjson requires at the end of its input (64 bytes — size the receive buffer for it) and the **schema drift** a venue can introduce any day (a new field, a renamed one; a parser that fails loudly on a missing field and *ignores* unknown ones is the contract — Ch. 101 §101.4.1).

**REST** is the order path on most such venues: an HTTPS request per order (`POST /api/v3/order` with a signed body), a response with the venue's order id, and a **rate limit** (requests per second per key, per IP, per endpoint, with weighted costs and a `429` or a ban when exceeded — Appendix K.4's throttle, enforced by an HTTP gateway). Each request on a **kept-alive** connection is: build the body, sign it (HMAC-SHA256 over the query string, ~1–2 µs for a 200-byte body), one TLS record out, one in, parse the response. HTTP/2 multiplexes requests on one connection and removes head-of-line blocking at the HTTP layer (not at TCP's — Ch. 85 §85.2); HTTP/1.1 pipelining is not honoured by most gateways. The order-entry discipline of Ch. 105 — a client order id you chose, journaled before send; idempotent resubmission; the unknown-state matrix on a timeout — applies *verbatim*, with a slower clock: a REST timeout is 1–5 s, a venue "order status" query is the reconciliation path, and the cancel-on-disconnect that a FIX session gives you is replaced by whatever the venue's API offers (often nothing: a dead-man's switch endpoint you must arm, or a mass cancel you must send). FIX-over-TLS (a FIX session inside a TLS tunnel, offered by some crypto venues and most FX ECNs) is the kinder case: Ch. 106's machine, with §86.2.1's record cost added per message.

### 86.2.5 The tier, and what transfers

Place this chapter's venues honestly on the book's tier table (Ch. 97 §97.2, Ch. 119 §119.2): **100 µs–10 ms**, dominated by (1) the physical distance to the venue's load balancer (a cloud region's intra-AZ RTT is 50–200 µs; cross-AZ 0.5–1 ms; cross-region tens of ms — Appendix E), (2) the venue's own gateway, which is itself a software stack with its own tail, and (3) the mandatory protocol layers of §86.2.1–§86.2.4. What transfers from Parts VIII–IX: **busy-polling** the socket (Ch. 68 §68.4.1, `SO_BUSY_POLL` or a userspace `epoll_wait(0)` loop) instead of sleeping in `epoll_wait` — it removes the wake-up latency (Ch. 54) that is still 5–20 µs even in a cloud VM; **warming** the connection, the TLS state and the parser (Ch. 62); the **zero-copy parse discipline** (On-Demand JSON *is* it); **`TCP_NODELAY`** and the send-path tuning (Ch. 69 §69.4.1 — autocorking is still a thing); the **order-entry discipline** of Ch. 104–106; and **measurement**: the tick-to-trade ledger (Ch. 99) with a timestamp at `recv` return and at `send` entry, which in this tier are the only stamps you own. What does not: multicast and A/B lines (the venue sends one TCP stream per subscription — redundancy is two connections from two hosts, Ch. 112's failover applies), cross-connects and layer-1 fan-out, NIC hardware timestamps on the *venue* side (you have your own NIC's, if the cloud exposes a PHC — §86.4.6), kernel bypass (a virtual function is passable, Appendix S.5, but the venue's gateway is still 500 µs away), and PTP to the venue's clock (the venue's `E`/`T` timestamps are on *its* clock, offset unknown; use them for ordering within the venue's stream, never across venues — Ch. 73 §73.2.4).

The model: **an encrypted web-protocol venue is a kernel-TCP connection with three mandatory layers on top — a TLS record (~2,000 cycles fixed + 0.17 cycles/byte, or less with kTLS), a WebSocket frame (tens of cycles), and a JSON document (~100 ns with a SIMD On-Demand parser, 20× that with a DOM) — kept warm and resumed, never 0-RTT for orders, busy-polled, parsed in place into the same fixed records the binary feeds produce, and placed on a tier where the floor is the region's RTT to the venue's load balancer.**

## 86.3 Measure it: TLS records by cipher and size, the handshake, a WebSocket frame, and a JSON book update

*Toolchain and machine: Appendix L.3. The run below is on the 4-vCPU KVM guest (GCC 13.3, `-O2 -std=c++20 -march=native`, OpenSSL 3.0.13, simdjson 3.10.1 single-header with the `icelake` implementation selected at runtime, nlohmann-json 3.11.3, TSC at 2.7 GHz — divide cycles by 2.7 for ns). The guest kernel exposes no `tls` ULP, so kTLS is cited (§86.2.1), not measured.*

Two harnesses. `tls.cpp` connects an OpenSSL client and server through a **memory BIO pair** — no socket, no kernel — so that one `SSL_write` on the client plus the `SSL_read` that consumes it on the server is exactly the record layer's cost; then times the raw EVP AEAD with the key scheduled once (what a connection does) per record size; then a full handshake against a PSK resumption, both sides' CPU. `jsonws.cpp` builds a 162-byte WebSocket frame around a 154-byte `bookTicker`-style JSON message, parses and unmasks it, and decodes the JSON into the internal record three ways:

```cpp
// jsonws.cpp (excerpt) — WebSocket frame parse with in-place 8-byte unmasking, and the simdjson On-Demand decode.
static inline bool ws_parse(uint8_t* p, size_t n, const uint8_t** payload, size_t* len) {
    if (n < 2) return false; uint8_t op = p[0] & 0x0f; if (op != 1 && op != 2) return false;   // text/binary only here; control frames off-path
    bool masked = p[1] & 0x80; size_t L = p[1] & 0x7f, h = 2;
    if (L == 126)      { if (n < 4)  return false; L = size_t(p[2]) << 8 | p[3]; h = 4; }
    else if (L == 127) { if (n < 10) return false; L = 0; for (int i = 0; i < 8; ++i) L = L << 8 | p[2 + i]; h = 10; }
    if (masked) { if (n < h + 4) return false; uint32_t k; std::memcpy(&k, p + h, 4); h += 4; if (n < h + L) return false;
        uint64_t k8 = uint64_t(k) | uint64_t(k) << 32; uint8_t* d = p + h; size_t i = 0;
        for (; i + 8 <= L; i += 8) { uint64_t w; std::memcpy(&w, d + i, 8); w ^= k8; std::memcpy(d + i, &w, 8); }   // vectorizes to vpxor
        for (; i < L; ++i) d[i] ^= reinterpret_cast<const uint8_t*>(&k)[i & 3];
    } else if (n < h + L) return false;
    *payload = p + h; *len = L; return true;
}
static bool decode_simdjson(simdjson::ondemand::parser& parser, simdjson::padded_string& buf, BookUpdate& u) {
    auto doc = parser.iterate(buf);
    for (auto field : doc.get_object()) {                                   // forward-only, in document order — no DOM
        std::string_view k = field.unescaped_key();
        if      (k == "b") { std::string_view v = field.value().get_string(); u.bid_px  = dec_to_e8(v.data(), v.size()); }   // price strings -> scaled int (Ch. 35)
        else if (k == "B") { std::string_view v = field.value().get_string(); u.bid_qty = dec_to_e8(v.data(), v.size()); }
        else if (k == "a") { std::string_view v = field.value().get_string(); u.ask_px  = dec_to_e8(v.data(), v.size()); }
        else if (k == "A") { std::string_view v = field.value().get_string(); u.ask_qty = dec_to_e8(v.data(), v.size()); }
        else if (k == "E") { u.ts = field.value().get_uint64(); }
    }
    return true;
}
```

```
== TLS 1.3 record cost through a memory BIO pair: client SSL_write + server SSL_read (cycles per record) ==
cipher                           64 B     256 B    1400 B   16 KiB
TLS_AES_128_GCM_SHA256           2038     2106     2496     9534      (0.75 / 0.78 / 0.92 / 3.5 us)
TLS_AES_256_GCM_SHA384           2050     2148     2566    10396
TLS_CHACHA20_POLY1305_SHA256     5300     5610     7680    27858
   handshake CPU, both sides, no network:  full 411 us   PSK resumption (psk_dhe_ke) 281 us
== raw AEAD, EVP, key scheduled once, per record seal + 16-byte tag (cycles) ==
AES-128-GCM                       432      488      704     2816      (~0.17 cyc/B at length)
AES-256-GCM                       468      526      844     3476
ChaCha20-Poly1305                1592     1682     3038    13062
memcpy of 1400 B (for scale)       46
== WebSocket frame, 162 B (154 B JSON payload) ==
parse, unmasked (server->client)                 34 cyc  (13 ns)
parse + unmask 8 B/iter (client->server)         52 cyc  (19 ns)
== JSON bookTicker (154 B) -> 48-byte BookUpdate ==
simdjson On-Demand                              260 cyc  (96 ns)
hand-rolled memmem key scanner                  548 cyc  (203 ns)
nlohmann::json DOM                             4824 cyc  (1.8 us)
```

Read it against the tier. **The record layer, not the cipher, is the cost**: AES-GCM is 430–700 cycles and the record is 2,000–2,500, so the lever is fewer records (coalesce sends; ask the venue for batched updates where it offers them), a leaner record layer (BoringSSL's and wolfSSL's are smaller; kTLS removes the copies and the library — §86.4.2), or hardware offload (Ch. 92) — not a different cipher, where the only decision is *not* ChaCha20 on an AES-NI CPU. **The handshake is a once-per-connection 400 µs you pay off-session** (§86.4.1), and resumption still costs an ECDHE unless you deliberately drop forward secrecy. **The JSON decode is the one place you choose a 20× factor**: 96 ns with On-Demand is within 2× of a binary SBE decode (Ch. 70 §70.3), the DOM is a 1.8 µs allocation storm per message — on a 20,000 msg/s feed that is 36 ms of CPU per second and a tail nobody can budget. And the WebSocket frame is noise (19 ns) *provided* fragmentation and control frames are handled off the hot path.

## 86.4 Techniques

### 86.4.1 The connection: TLS 1.3, AES-128-GCM, resumed, established before the session, and never 0-RTT for orders

- **Negotiate what you measured**: TLS 1.3 only (`SSL_CTX_set_min_proto_version(ctx, TLS1_3_VERSION)`), `TLS_AES_128_GCM_SHA256` first in the cipher list, X25519 as the key share (it is the fastest group and what every venue offers). Verify the venue's certificate against a pinned CA bundle you ship (Ch. 101 §101.4.7), not the system store.
- **Establish before the session, keep alive, resume on reconnect.** All connections — the market-data WebSockets *and* the REST/FIX order sessions — are up, handshaken and warm (Ch. 62 §62.4.2: a periodic ping or a cheap authenticated request so the TCP window, the TLS state and the venue's LB entry stay hot) before the readiness gate (Appendix M). Keep the session ticket (`SSL_CTX_sess_set_new_cb` — TLS 1.3 tickets arrive *after* the handshake and are single-use in OpenSSL 3; take the fresh one each time) so a reconnect is one RTT of resumption, not a full handshake plus a certificate chain.
- **Disable early data on every client context** (`SSL_CTX_set_max_early_data(ctx, 0)` is the default — assert it) and refuse to design around a venue that accepts it for orders (§86.2.2).
- **Two connections from two hosts** is the A/B line of this tier (Ch. 112's failover for the order session; a second market-data subscription for the feed, arbitrated by the venue's own sequence/update ids — most venues supply a monotone `u`/`U` update id that makes Ch. 72's gap detection applicable).

### 86.4.2 Record handling without copies: read into your ring, and when kTLS pays

OpenSSL's default `SSL_read` is a copy out of the library's record buffer; the shape that avoids it:

```cpp
// tls_rx.hpp — receive records into the application's ring, decrypt in place where the library allows it.
// OpenSSL 3: SSL_read_ex into a large application buffer (one syscall + one record decrypt per call, no extra copy of the plaintext
// beyond the library's internal in-place decrypt), with SSL_MODE_RELEASE_BUFFERS off so the record buffer is never freed/realloc'd per record.
struct TlsRx {
    SSL* ssl; std::uint8_t* ring; std::size_t cap, head = 0, tail = 0;             // the ring the WebSocket/JSON parsers consume (padded by 64 B for simdjson)
    void setup() { SSL_set_read_ahead(ssl, 1); SSL_clear_mode(ssl, SSL_MODE_RELEASE_BUFFERS); }   // read ahead: one recv may hold several records
    std::size_t pump() {                                                           // called from the busy-poll loop; never blocks (socket is O_NONBLOCK)
        std::size_t got = 0;
        for (;;) { std::size_t n = 0; int rc = SSL_read_ex(ssl, ring + tail, cap - tail, &n);
            if (rc == 1) { tail += n; got += n; if (tail == cap) break; continue; }
            int err = SSL_get_error(ssl, rc); if (err == SSL_ERROR_WANT_READ) break;          // drained
            if (err == SSL_ERROR_ZERO_RETURN) { on_close(); break; } on_error(err); break; }
        return got;                                                                 // the frame parser (§86.4.3) now walks [head, tail)
    }
};
```

The rules around it: the socket is non-blocking and *busy-polled* from the hot loop (`SSL_read_ex` returning `WANT_READ` is the "nothing yet" path, a few hundred ns — the Ch. 68 `recvmsg` cost plus the library's checks); **read-ahead on**, so one `recv` syscall pulls everything TCP has and the records are decrypted one per `SSL_read_ex` call without further syscalls; the ring is sized for the largest burst the venue sends (a snapshot can be a 1 MiB JSON document — Ch. 50's flow-control question in a new costume) and padded for simdjson; and the **allocation-free assertion** of Ch. 101 §101.4.2 is checked against the library — OpenSSL allocates on the handshake and on a renegotiation/key update, not per record, *if* `SSL_MODE_RELEASE_BUFFERS` is clear; BoringSSL and wolfSSL (`--enable-sp`, static buffers) are the choice when that guarantee must hold by construction.

**kTLS** pays when the receive path can run on a *different* core than the hot loop (the AEAD moves into the softirq on the socket's RX core — steer the flow there, Ch. 77 — and the hot loop reads plaintext from the socket buffer with a plain `recv`), when sends are large or `sendfile`-shaped, or when the NIC offloads the record (`ethtool -K eth0 tls-hw-rx-offload on` on a ConnectX-6 Dx: the AEAD disappears from the CPU entirely and the plaintext is DMA'd). Enable it after the handshake: `setsockopt(fd, SOL_TCP, TCP_ULP, "tls", 4)`, then `setsockopt(fd, SOL_TLS, TLS_RX, &crypto_info, …)`/`TLS_TX` with the keys and IVs extracted from the library (`SSL_get_key_update_type`… — OpenSSL 3 does this itself with `SSL_OP_ENABLE_KTLS` when the kernel supports it; check `SSL_get_rbio` / `BIO_get_ktls_recv`). What it does *not* change: the venue's record size and rate, the handshake, and the fact that the socket is still a kernel socket with Ch. 64's datapath under it.

### 86.4.3 The WebSocket frame parser on the hot path

The §86.3 parser is the fast path; the surrounding discipline:

- **Stream, not datagram.** Frames arrive in a TCP stream of decrypted bytes with no relation to record or `recv` boundaries. The parser keeps `(head, tail)` into the ring and a *frame-in-progress* state (header parsed, bytes remaining); on a short read it returns and resumes on the next `pump()` — never copies a partial frame to "wait for the rest" except when a message is *fragmented* (FIN = 0), in which case the fragments are appended into a per-connection reassembly buffer sized for the venue's largest message (and the venue's documentation says whether it fragments at all — most market-data streams never do).
- **Unmask only what you send.** Server-to-client frames are unmasked (34 cycles to parse); client-to-server frames you *build* must be masked — the 8-byte XOR loop (vectorized by GCC/Clang to `vpxor` over 32 bytes) is the cost, ~20 cycles for a 160-byte subscription message, and it is on the send path of *orders* on venues that take orders over WebSocket. Generate the mask key from a cheap PRNG (Appendix L.5: splitmix), not `getrandom`.
- **Control frames off-path.** Ping → pong *must* be answered (the venue's timeout is liveness, §86.2.3), but by the housekeeping side of the loop, not inline with a market-data burst; close → the Ch. 69 §69.4.4 "the gateway is gone" path, immediately; text frames with the venue's own heartbeat/`"ping"` JSON (some venues do both) → the same.
- **Validate the header before trusting the length** (Ch. 101 §101.4.1): a 63-bit length in the 127 form from a hostile or confused server is a buffer-size attack; cap it at the ring's capacity and treat an excess as a protocol error.

### 86.4.4 JSON fast paths: simdjson On-Demand into the overlay discipline

The §86.3 decoder is the template. The rules that keep it at 96 ns and correct:

1. **On-Demand, not DOM**; iterate fields in document order and convert each in place; `dec_to_e8` (a digit loop — or Ch. 71 §71.4.2's SWAR version for 8+ digit strings) turns the venue's decimal *string* into the scaled integer the book uses; `get_uint64` for integer timestamps; `get_string` returns a view into the padded buffer (zero copy; invalid only after the next `iterate`).
2. **Fail loudly on a missing required field, ignore unknown fields**, and count both (Ch. 100 §100.4.2's per-feed counters): the venue will add fields without notice; it will also, one day, rename one.
3. **Pad the buffer** (`SIMDJSON_PADDING` = 64 bytes after the end of the document) — the ring of §86.4.2 reserves it — and give the parser the *exact* document length (the WebSocket frame's payload length), not a NUL-terminated guess.
4. **One `parser` per thread, reused**; its internal buffers are allocated on first use and grow to the largest document seen — warm it with the largest message the venue can send (a full snapshot) before the open, so no allocation happens in session (Ch. 101 §101.4.2's allocation-after-warm-up alarm catches a regression).
5. **Validate what the parser does not**: simdjson guarantees the document is valid JSON and UTF-8; it does not know that `"b"` must be a positive decimal with ≤ 8 fractional digits or that `"u"` must exceed the last update id. Those are Ch. 101 §101.4.1's bounds and the Ch. 72 gap check, applied to the decoded record.
6. **Numbers**: never `get_double` on a price (Ch. 35); for venues that send prices as JSON numbers rather than strings, use `raw_json_token()` and parse the decimal text yourself into the scaled integer — simdjson's number parser would round it to binary first.

When the venue offers a **binary** alternative (SBE-over-WebSocket, Protobuf, a proprietary binary feed for the "pro" tier), take it: Ch. 70's overlay decode at 40–60 ns replaces the 96 ns parse *and* the UTF-8 validation.

### 86.4.5 REST order entry: rate limits, signing, idempotency and FIX-over-TLS

- **The rate limit is a term in the decision, not a rejection to handle.** Model the venue's limits (per endpoint weight, per second/minute window, per key and per IP) as token buckets in the risk gate (Ch. 104 §104.4.3 — the same TSC bucket, slower refill), refuse at the gate, and never let a `429` reach the wire: on most venues a `429` escalates to a temporary ban, which is a kill-switch event you inflicted on yourself.
- **Sign cheaply and once.** HMAC-SHA256 over the request string is ~1–2 µs with OpenSSL's EVP (SHA-NI on the reference box); precompute everything that does not change per order (the key, the endpoint, the static parameters) and sign only the variable tail; the timestamp the venue requires in the signature must be *your* clock within the venue's window (typically ±5 s — not a PTP problem, a sanity one).
- **The order id discipline of Ch. 105 applies verbatim**: your `newClientOrderId` is the slot+generation handle, journaled before send; a timeout (the venue's gateway has a 1–5 s tail) puts the order in the unknown-state matrix, resolved by the venue's order-status endpoint (its reconciliation path) — never by resending without the same client id.
- **Keep-alive, HTTP/2 where offered, one connection per purpose**: one for orders, one for cancels (so a burst of new orders never queues a cancel behind it — the cancel-first discipline of Ch. 107), one for queries. Pre-establish all three.
- **Cancel-on-disconnect does not exist unless you arm it.** Venues that offer a dead-man's switch (`/api/v3/cancelAllAfter`-style: "cancel everything if you do not hear from me within N seconds") get it armed at the open and refreshed from the housekeeping loop; venues that do not get a mass-cancel request from the *supervisor* (Ch. 103 §103.4.3) when the gateway process dies, which means the supervisor holds its own connection and key.
- **FIX-over-TLS** is Ch. 106's machine with §86.2.1's record under it and a slower venue: the `TestRequest` cadence is seconds, `tcp_retries2`/`TCP_USER_TIMEOUT` (Ch. 69 §69.4.4) still decide how long a dead gateway looks alive, and the resend store (Ch. 106 §106.4.3) is the recovery path a REST venue's status endpoint only approximates.

### 86.4.6 Trading from a cloud host: virtualized NICs, placement, time, and the floor you cannot buy

A cloud instance is Appendix S's VM operated by someone else: you do not choose the host, cannot pin host cores, and see the hypervisor only through its effects. What you *can* do, in order of leverage:

- **Placement.** Put the instance in the venue's region and, where the venue runs in the same cloud, in the same **availability zone** (many crypto venues publish which AWS region/AZ their matching engine or gateway lives in); use a **cluster placement group** so your own processes' hosts share a low-latency network partition. Intra-AZ RTT is 50–200 µs; cross-AZ 0.5–1 ms; cross-region 10–100 ms (Appendix E). No software change competes with this line.
- **The NIC.** Choose an instance type with an **SR-IOV / ENA-class** adapter exposed as a PCIe function (AWS Nitro's ENA; Azure Accelerated Networking's Mellanox VF; GCP's gVNIC) rather than a paravirtual `virtio-net` — the difference is Appendix S.5's ~8 vs ~25 µs one-way. On ENA: enable **Low-Latency Queues** (`ena` driver `llq` mode, where available), set interrupt moderation to zero (`ethtool -C eth0 rx-usecs 0 tx-usecs 0` — ENA's adaptive moderation adds tens of µs by default), size the queues (`ethtool -L`) to the vCPU count, and steer the venue flow to one queue on the busy-polling core (Ch. 77 — ENA supports a limited `ethtool -N` set). **EFA** (AWS's SRD transport, OS-bypass via libfabric) is for *host-to-host* traffic within the placement group (your feed handler to your strategy on another instance, Ch. 85's problem), not for reaching a venue. DPDK on ENA exists (`net_ena` PMD) and removes the kernel from the receive path — but the venue is still 500 µs away, so measure whether the 5–10 µs it saves is worth losing the kernel's TCP (you need a userspace TCP stack to speak TLS to the venue — Ch. 82 §82.4.2).
- **Time.** AWS exposes a **PTP hardware clock** on Nitro instances (`/dev/ptp0`, disciplined by the Nitro card to Amazon Time Sync, sub-100 µs to UTC and ~µs-level between instances); Azure and GCP offer `chrony` sources of similar quality. Discipline the guest's clock from it (Ch. 78 §78.4.1 with the cloud PHC as the source) and read the TSC for intervals (Appendix S.3: `tsc` clocksource, not `kvm-clock`). None of it synchronises you to the *venue's* clock — its timestamps are on its clock, usable for ordering its own stream only (§86.2.5).
- **Jitter you did not cause.** Steal time (`/proc/stat`'s `steal` column, Appendix S.2), EPT misses from a noisy neighbour, the hypervisor's timer, live migration. Measure with `cyclictest` *in the instance* (Appendix S.6; Ch. 61's noise hunt applies with the kernel's view only), pick instance types with dedicated or bare-metal tenancy (`.metal` instances give you the host — then Appendix C applies verbatim and §86.4.6 reduces to §86.4.1–§86.4.5), and treat a p99.9 you cannot explain as a reason to move the instance, not to tune it.
- **The floor you cannot buy.** The venue's gateway is a load balancer in front of a software stack with its own queue, its own TLS termination and its own tail; your fastest possible path is one RTT to it plus its processing, and that number — measured as the venue's ack time distribution, per endpoint, per time of day (Ch. 99 §99.4.5 with the venue's `T` and `E` stamps) — is the budget everything above is optimized *within*. A shop that needs to be faster than that moves to the venue's colocation offering if one exists (several crypto venues now sell proximity in their cloud region or a bare-metal cage — Appendix K.1's economics, cloud edition), and otherwise accepts that it is competing on the software side of a fixed floor.

## 86.5 Pitfalls & anti-patterns

- **A DOM JSON parser on the feed.** 1.8 µs and an allocation per node per message (§86.3); 20× the On-Demand parse, and a tail no budget covers. simdjson On-Demand into fixed records (§86.4.4).
- **A hand-rolled `memmem` scanner "because it's simpler."** Slower than simdjson (203 vs 96 ns) and wrong on a key inside a string value, a reordered field or an escape (§86.2.4).
- **Prices through `double`.** The venue sent a string on purpose (§86.4.4 rule 6; Ch. 35).
- **0-RTT for orders.** Replayable by construction (§86.2.2). Assert `max_early_data == 0`.
- **Handshaking in session.** 400 µs of CPU and an RTT plus a certificate chain on the first order of the day; establish and warm before the gate, resume on reconnect (§86.4.1).
- **ChaCha20 on an AES-NI CPU.** 3–4× the record cost (§86.3) for no security gain here.
- **`SSL_MODE_RELEASE_BUFFERS` on.** Frees and reallocates the record buffer per record — allocation on the hot path (§86.4.2).
- **Sleeping in `epoll_wait`.** The wake-up adds 5–20 µs in a VM to a path whose only lever is software; busy-poll the socket (§86.2.5).
- **Pings answered inline with the burst, or not at all.** The venue's timeout disconnects you mid-burst; a disconnect you did not notice leaves orders resting with no cancel-on-disconnect (§86.4.3, §86.4.5).
- **Letting a `429` reach the wire.** The venue escalates to a ban; the rate limit is a term in the gate (§86.4.5).
- **`virtio-net` and default interrupt moderation on the cloud NIC.** 25 µs instead of 8 one-way, plus tens of µs of coalescing (§86.4.6, Appendix S.5).
- **Comparing the venue's timestamps to yours.** Its clock, unknown offset; order within its stream only (§86.2.5).
- **Tuning the instance instead of moving it.** The AZ is the latency; the rest is a few µs on top of 500 (§86.4.6).

## 86.6 Exercises & checklist

**Exercises**

1. **Record cost by library.** Rebuild `tls.cpp` against BoringSSL and wolfSSL (static buffers); compare the 64 B and 1,400 B record costs with OpenSSL 3.0's; then enable kTLS on a kernel that has it (`CONFIG_TLS`) over a real loopback socket and measure `recv` latency and CPU on the hot core with the flow steered to another core.
2. **Coalescing.** Feed 20,000 small JSON messages/s through one TLS connection as one record each, then batched 10 per record; measure CPU per message on the receiver and the added latency per message from batching. Find the venue's own batching knobs.
3. **Parser shootout on your venue.** Capture a day of your venue's WebSocket stream; decode it with simdjson On-Demand, DOM, and a schema-generated decoder (if the venue publishes a schema); measure ns/message and the allocation count; then inject a renamed field and a key-inside-a-string and show which decoders notice.
4. **The REST path.** Instrument an order's path — gate, sign, `SSL_write`, response `SSL_read`, parse — with Ch. 99's stamps; plot the venue's ack time by hour; compute how much of the median is yours.
5. **Cloud placement.** From the same instance type in the venue's AZ, a neighbouring AZ and another region, measure RTT and ack-time distributions to the venue's REST and WebSocket endpoints; then compare `virtio` vs ENA, moderation on vs off, `epoll_wait` vs busy-poll, on the AZ-local instance. Rank the levers.
6. **Dead-man's switch drill.** Arm the venue's cancel-all-after (or your supervisor's mass cancel), kill the gateway process mid-session, and measure the time until the venue reports no open orders.

**Checklist — encrypted and web-protocol venues**

- [ ] TLS 1.3 only, **AES-128-GCM**, X25519, pinned CA; **early data disabled and asserted**; tickets captured and **resumed** on reconnect (§86.4.1).
- [ ] All connections **established, handshaken and warmed before the gate**; two from two hosts for the order session (§86.4.1).
- [ ] Records are read **non-blocking from the busy-poll loop with read-ahead** into the application's padded ring; `RELEASE_BUFFERS` off; no allocation after warm-up (§86.4.2).
- [ ] **kTLS** or NIC TLS offload where the RX core is not the hot core; measured, not assumed (§86.2.1, §86.4.2).
- [ ] The WebSocket parser is a **stream parser** with frame state; control frames and fragmentation handled off-path; lengths validated against the ring (§86.4.3).
- [ ] JSON is decoded with **simdjson On-Demand** in document order into fixed records; prices from strings to scaled integers; missing fields fail loudly, unknown ones are counted (§86.4.4).
- [ ] Rate limits are **token buckets in the risk gate**; signing is precomputed; client order ids are journaled before send; the dead-man's switch is **armed** (§86.4.5).
- [ ] The instance is in the **venue's AZ**, on an **SR-IOV/ENA** adapter with moderation off and the flow steered, disciplined from the **cloud PHC**, with `cyclictest` run inside it (§86.4.6).
- [ ] The venue's **ack-time distribution** is measured per endpoint and hour, and the tier is stated honestly in the budget (§86.2.5, §86.4.6).

## 86.7 References

- E. Rescorla, *RFC 8446: The Transport Layer Security (TLS) Protocol Version 1.3* — records, handshake, resumption, and §8's warning on 0-RTT replay (§86.2.1–§86.2.2); *RFC 5116* (AEAD), *RFC 8439* (ChaCha20-Poly1305).
- I. Fette and A. Melnikov, *RFC 6455: The WebSocket Protocol* — framing, masking, fragmentation and control frames (§86.2.3, §86.4.3).
- D. Watson, *Kernel TLS* (Netdev 1.2, 2016) and the Linux `Documentation/networking/tls.rst` / `tls-offload.rst` — kTLS and NIC record offload (§86.2.1, §86.4.2).
- G. Langdale and D. Lemire, "Parsing gigabytes of JSON per second," *VLDB Journal* 28 (2019); J. Keiser and D. Lemire, "On-demand JSON: a better way to parse documents?" *Software: Practice and Experience* 54 (2024) — the parser of §86.4.4.
- The OpenSSL 3.0 manual pages — `SSL_read_ex`, `SSL_set_read_ahead`, `SSL_MODE_RELEASE_BUFFERS`, `SSL_CTX_sess_set_new_cb`, `SSL_OP_ENABLE_KTLS`, `SSL_CTX_set_max_early_data` (§86.4.1–§86.4.2).
- AWS, *Elastic Network Adapter* and *Enhanced networking* documentation (LLQ, interrupt moderation, placement groups); *Amazon Time Sync Service* and the Nitro PTP hardware clock; Azure *Accelerated Networking*; GCP *gVNIC* — §86.4.6's knobs.
- The venues' own API specifications (rate-limit schemes, signing, dead-man's-switch endpoints, update-id semantics) — read the current one; they change quarterly (§86.4.5).

## 86.8 Additional Reading

- D. J. Bernstein and T. Lange, "SafeCurves"; Cloudflare's TLS 1.3 and 0-RTT engineering posts — the 0-RTT replay discussion in practice (§86.2.2).
- Ch. 68–69 (the TCP path under all of this); Ch. 70–71 (the decode discipline this chapter approximates); Ch. 85 (the fast transports at the other end of Part X); Ch. 92 (TLS offload on a DPU); Ch. 105–106 (order identity, the session machine, liveness); Ch. 112 (failover with two connections); **Appendix S** (the VM mechanics under a cloud instance); **Appendix K.4** (throttles) and **K.1** (the economics of being close).

---

*Next: Ch. 87 — PCIe & the Host–Device Boundary, opening Part XI (Heterogeneous Computing & Hardware Acceleration): every accelerator and modern NIC — the cloud VF this chapter steered a flow to included — sits behind PCIe, and the round-trip across that boundary is the irreducible floor under every offload decision in the rest of the book.*
