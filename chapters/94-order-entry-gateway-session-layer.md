# Part XIII — Observability & Operations in Production

# Chapter 94 — The Order-Entry Gateway & Session Layer

> **Prerequisites:** Ch. 93 (ClOrdIDs, the in-flight table, journal-before-send and the unknown-state matrix — the *order* state this chapter's *session* state sits beneath), Ch. 91 §91.2.1 and §91.4.4 (the gateway role: it owns the venue session and executes the kill), Ch. 60–61 (socket tuning and TCP's send path, autocorking and RTO — the transport the session rides on; §61.4.4 is this chapter's failure-detection companion), Ch. 62 §62.4.1 (`std::to_chars` and the outbound encode), Ch. 49 §49.4.2 (deadline timers — heartbeats and TestRequests are timers, not sleeps), Ch. 31 (an `mmap`'d page as the persisted sequence journal), Ch. 21 §21.4.5 (type-state for the session), Ch. 55 §55.4.2 (pre-established, warm connections), Appendix J.3 (the session-layer paragraph this chapter expands) and J.5 (OUCH/FIX field names), Appendix K.4 (gateway ports and throttles) and K.6 (redundant gateways).
>
> **Leads into:** Ch. 95 (what the crash handler preserves: the sequence page and the resend store), Ch. 99 (failover without split-brain — *which* gateway may hold the session, enforced with leases and fencing tokens; this chapter is the mechanism that takeover drives), Ch. 105 (venue conformance and certification sessions as a release step), Ch. 106 §106.4.5 (venue timestamps on acks join the post-trade latency analysis), Appendix M.4 (the T-5 "session logon and sequence reconciliation" gate). Ch. 93 made an order impossible to confuse with another; this chapter is the component that carries it to the venue and keeps the *session* — not just the socket — alive and in sequence: the protocol state machine, the persisted sequence numbers, the encode fast path, the throttle, and how long it really takes to learn the venue is gone.

---

## 94.1 Why it matters: the session is the only thing that can cancel your orders

Every other component in Part XIII can crash and be restarted (Ch. 91, 95); the **order-entry gateway** cannot be *wrong* even briefly, because it is the one process that talks to the venue, and the venue's view of the firm's position is defined by what arrived on that connection. Three facts make the session layer a latency *and* a correctness topic. First, the gateway is on the tick-to-trade path: Ch. 106 §106.3 budgets "order encode → TX" at ~500 ns of software, and a general-purpose FIX engine that builds a `std::string` per tag, formats timestamps with `strftime`, allocates a message object and takes a mutex spends that budget several times over (§94.3 measures a template-plus-`to_chars` encode at **50–95 ns** per 145-byte NewOrderSingle). Second, the session is a **state machine with persisted state** — `MsgSeqNum` in both directions — and a gateway that restarts with the wrong numbers either gets its Logon rejected (sequence too low) or, worse, has the venue silently accept a *resent* order as a new one; Ch. 93's never-resend rule only works if the session layer can *prove* what the venue received, which is what `ResendRequest`/`SequenceReset-GapFill` and `PossDupFlag` are for. Third, and least appreciated: **TCP does not tell you the venue is gone.** §94.3 shows the Linux defaults keep a connection to a dead peer "established" for ~15 minutes (`tcp_retries2 = 15`); a gateway that trusts the socket will keep writing orders into a black hole, and the venue's cancel-on-disconnect (Appendix K.4) — the firm's last safety net — only fires when the *venue's* side notices. The session layer's heartbeats and `TestRequest`, plus `TCP_USER_TIMEOUT`, turn fifteen minutes into two seconds; Ch. 99 turns two seconds into a safe failover.

## 94.2 Mental model

### 94.2.1 Four protocols, one state machine

```
   FIX session (4.2/4.4/FIXT)           SoupBinTCP (under OUCH)          CME iLink 3 / FIXP               Cboe BOE
   ────────────────────────────         ─────────────────────────        ──────────────────────────       ───────────────────────
   Logon(A) / Logout(5)                 Login Request / Accepted /       Negotiate → Establish →          Login Request / Response
   Heartbeat(0) / TestRequest(1)          Rejected, Logout Request         Sequence / Terminate            Client/Server Heartbeat
   MsgSeqNum(34) both directions,       sequenced server→client          UUID + sequence numbers           sequence numbers,
   ResendRequest(2) 7=/16=,               messages; client asks for      both ways; Retransmit Request    replay on login
   SequenceReset(4) GapFill 123=Y,        "resume from seq N" at login   (bounded window), NotApplied     (bounded window)
   PossDupFlag(43), OrigSendingTime      client→unsequenced + heartbeats  for client gaps
   text tag=value, checksum(10)         binary, length-prefixed          SBE binary (Ch. 62)              binary, length-prefixed
```

They differ in encoding and vocabulary and agree on the shape: a **session** is established with a handshake that *agrees on sequence numbers*, kept alive by **heartbeats at a negotiated interval** (one missed heartbeat → a `TestRequest`, which the peer must answer; a second miss → the session is dead at the application layer whatever TCP says), and **recovered** after a disconnect by one side telling the other "I last saw your N; resend from N+1" — answered either with the original messages marked *possible duplicate* (so an order the venue already has is recognised and ignored, by `ClOrdID` — Ch. 93 §93.2.4) or with a *gap fill* that says "N+1..M were administrative, skip them". The binary venues bound the resend window (iLink 3 and BOE replay only a few thousand messages; SoupBinTCP resumes the *server's* sequenced stream and makes the *client's* messages unsequenced, so the client proves delivery by correlating acks — Ch. 93 §93.2.4); FIX leaves it unbounded in the spec and bounded by each venue's rules. The gateway therefore implements one **deterministic state machine** (Ch. 91 §91.2.2 applies to sessions as much as to books) with a per-venue codec in front of it:

```
   Disconnected ──connect()+send Logon──► LogonSent ──Logon(A) in, seq == expected──► Established ◄──────────────────────────────┐
        ▲                                     │ Logon in, seq > expected                   │ HeartBtInt elapsed w/o TX → send Heartbeat   │
        │                                     ▼                                            │ HeartBtInt×1.2 w/o RX → send TestRequest     │ Heartbeat(112=id) in
        │  recv()==0 / TestRequest unanswered │ send ResendRequest(7=expected,16=0)         │ HeartBtInt×2.4 w/o RX → Disconnected        │
        │  / TCP_USER_TIMEOUT / Logout in     ▼                                            ▼                                              │
        └──────────────────────────── ResendPending ──PossDup resends + GapFill applied──► Established ───ResendRequest in──► resend(from,to)┘
                                                                                                                              (store → PossDup; admin runs → GapFill)
```

Every arrow is a function of `(state, message-or-timer)` and nothing else; the venue in §94.3 is a second instance of the same machine with the roles swapped, which is how the session layer is tested without a venue (Ch. 103 §103.4.3 puts a fault-injecting venue double behind it).

### 94.2.2 Sequence numbers are state, and state must survive the process

`MsgSeqNum` is incremented for *every* message, administrative or not, in each direction, and a session's numbers persist across disconnects until a scheduled reset (daily, at the venue's chosen time). The gateway therefore holds two counters — `next_out` (the number the next sent message will carry) and `next_in` (the number expected next from the venue) — and they are **durable state written before the message that consumes them is sent** (Ch. 93 §93.4.1's journal-before-send, applied to the session): a restart that reads them back logs on with the right numbers, and the venue's Logon reply tells it, by comparing *its* `MsgSeqNum` with `next_in`, whether anything was missed. The persisted page is tiny (§94.4.1: 32 bytes in an `mmap`'d file — Ch. 31) and the write is a store to mapped memory, not a syscall, so it costs nothing on the hot path. What also has to survive is the **resend store**: the last few thousand *application* messages by sequence number, so a `ResendRequest` can be answered with the originals plus `43=Y`/`122=OrigSendingTime`. Both live in shared memory for the same reason Ch. 93's in-flight table does — the crash handler (Ch. 95) preserves the segment and the restarted gateway, or a standby one (Ch. 99), reattaches to it.

### 94.2.3 Liveness has layers, and each one is a clock

```
   layer                   mechanism                                     time to detect a dead venue (Linux 6.18 defaults → tuned)
   ───────────────────     ───────────────────────────────────────────   ──────────────────────────────────────────────────────────────
   TCP, data in flight     RTO retransmits, exponential backoff          tcp_retries2 = 15 → ~15 min (§94.3: 8 retransmits by 27 s)   → TCP_USER_TIMEOUT = 2 s
   TCP, idle               SO_KEEPALIVE                                  off (2 h idle) → 1 s idle / 1 s intvl / 3 probes ≈ 4 s
   TCP, peer process died  FIN or RST arrives                            tens of µs (§94.3: recv() returns 0 in ~25 µs) — only if the HOST is alive
   session                 Heartbeat / TestRequest                       HeartBtInt (venue-set, 1–30 s) → TestRequest at 1.2×, dead at 2.4×
   application             acks per order (Ch. 93 §93.4.2 ageing)        cancel-if-no-ack ~ms: the fastest, and only for orders actually sent
```

The layers detect *different* deaths. A venue process that exits gives the kernel a FIN and the gateway knows in microseconds; a venue host that loses power, a cut cross-connect or a failed switch gives the gateway *nothing*, and only a timer can notice — TCP's retransmission timer if data is in flight, keepalive if not, the session heartbeat regardless. The gateway's design rule follows: **every liveness decision is a deadline on the Ch. 49 timer wheel, and the socket's own timers are set so TCP gives up before the session layer would** (`TCP_USER_TIMEOUT` ≈ one heartbeat interval), so that "dead" is reported *once*, by one layer, to one place: the Ch. 91 supervisor and the Ch. 99 failover protocol. §61.4.4 owns the socket knobs and the measured backoff curve; this chapter sets them.

### 94.2.4 The encode fast path and the throttle

An order message is *mostly constant*: `BeginString`, `SenderCompID`/`TargetCompID`, `Symbol`, `OrdType`, `TimeInForce`, the venue's tag order. What varies per order is `MsgSeqNum`, `SendingTime`, `ClOrdID`, `Side`, `OrderQty`, `Price` — six integers. The fast path is a **template frame** with the variable fields written by `std::to_chars` (Ch. 62 §62.4.1) at known offsets or appended in order, `BodyLength` computed from the pointer difference, the checksum as one pass over the bytes, and `SendingTime` as integer arithmetic on a cached date rather than a `strftime` call. The binary protocols are cheaper still — fixed offsets, little-endian integers, no checksum — and Ch. 22's compile-time decoders run in reverse as encoders. On the other side sits the venue's **throttle** (Appendix K.4): a message budget per session per second, enforced by rejecting or disconnecting; the gateway keeps a TSC token bucket (the same one Ch. 92 §92.4.1 uses for the rate limit) *in front of* the socket so a mass requote never trips the venue's limit, and a mass *cancel* uses the venue's single `OrderMassCancelRequest` where one exists (Ch. 92 §92.4.4).

## 94.3 Measure it: encode latency, resync time, and how long TCP takes to notice

*Toolchain and machine: Appendix L.3. Built with GCC 13.3 and Clang 18.1 (`-O2 -std=c++20 -march=native -pthread`), run on the 4-vCPU Intel Xeon VM of Ch. 43 §43.3 (TSC 2.70 GHz, Linux 6.18, no isolation). The loopback figures include two kernel TCP stacks and a thread wake-up and are an upper bound for a real NIC path in the µs terms, a lower bound in the wire terms; the encode figures are per-core and transfer with the usual ~0.8× clock ratio. Labelled VM-measured.*

**The harness** (`fixsess.cpp`, ~300 lines): a `Session` implementing §94.2.1's machine with a 32-byte persisted `SeqJournal` in `/dev/shm`, a 4096-entry resend store and the §94.4.2 encoder; a loopback **venue** thread running the mirror image (Logon, `ExecutionReport` per order, `TestRequest`/`Heartbeat`, `ResendRequest` when a Logon arrives with a higher sequence than expected), with one injected fault: after acking the 1000th order it drops the connection *and forgets the last seven messages it received* — a venue that crashed before persisting them. A second program (`tcpdetect.cpp`) installs an `iptables` rule that silently drops every packet the "venue" sends back and times how long until the gateway's socket reports an error, under default timers, `TCP_USER_TIMEOUT` and `SO_KEEPALIVE`.

```
encode NewOrderSingle (145 bytes, 15 tags, checksum)         GCC 13.3:  p50  95 ns   p99 114–126 ns   p99.9 147–168 ns
  (rdtsc pair around one encode, 2^20 encodes)                Clang 18.1: p50  50 ns   p99  68 ns       p99.9  98 ns
  frame: 8=FIX.4.4|9=122|35=D|49=GW|56=VENUE|34=1049575|52=20261109-22:13:20.001|11=4785074604081218|21=1|55=ESZ6|54=1|38=100|40=2|44=4500.25|59=3|10=176|

initial logon (connect + Logon + Logon reply, loopback)      337–537 µs   (TCP handshake, the venue thread's wake-up, two parses)
1000 orders, ack RTT over loopback                            p50 17–25 µs   p99 36–54 µs
venue drops the connection                                    recv() == 0 seen 22–29 µs after the last ack; journal: next_out 1002, next_in 1002
reconnect + Logon                                             49–229 µs
  + venue's ResendRequest(7=995) → 7 PossDup resends + 1 GapFill → 7 acks   +60–75 µs  ⇒ 123–291 µs from connect() to "every order the venue lost is back"
idle 3 s at HeartBtInt = 1 s                                  4 administrative messages sent, session Established throughout
second process start (journal persisted)                      starts at epoch 2, next_out 1007, next_in 1013 — logs on with the venue's numbers, no resend

how long until TCP reports the dead venue (server→client packets dropped; server host otherwise alive):
  defaults (tcp_retries2 = 15)       retransmits at 0.2, 0.4, 0.8, 1.6, 3.3, 6.5, 13, 26 s (backoff 0→7, RTO 204 ms → 26 s) … NOT detected in 45 s; by the kernel's formula ≈ 924 s
  TCP_USER_TIMEOUT = 2000 ms         ETIMEDOUT at 2.25 s (after the 4th retransmit)
  SO_KEEPALIVE idle 1 s / intvl 1 s / cnt 3 (no data in flight)   ETIMEDOUT at 4.08 s
```

Reading it:

- **Encoding a FIX order is ~50–100 ns, not the microseconds a general-purpose engine spends.** The variable work is six `to_chars` calls and a 130-byte checksum pass; GCC's version is slower than Clang's because it does not vectorise the checksum loop at `-O2` (Ch. 34; `-O3` or a manual 8-lane sum closes most of the gap). Against Ch. 106 §106.3's ~500 ns "encode → TX" budget this leaves the TX path (Ch. 72) as the cost, which is where it should be. A binary protocol encode (fixed offsets, no checksum) is ~10–20 ns on the same code shape.
- **Resynchronisation is microseconds when the resend store is in memory.** The whole reconnect — handshake, Logon exchange, the venue's `ResendRequest`, seven resends with `43=Y`, one `GapFill` for the administrative message in the range, seven acks — completes in 123–291 µs on loopback, and the resync part is only 60–75 µs of it. The thing that makes it this fast is §94.2.2: the store is indexed by sequence number, the journal says exactly where both sides are, and nothing is read from disk. What this does *not* measure is the venue's side — real venues answer a Logon in milliseconds and some throttle resends; the gateway's share of a takeover is the part it controls.
- **The venue's FIN is seen in ~25 µs; the venue's silence is seen in 15 minutes by default.** Eight retransmissions in 27 s with the RTO doubling each time is the whole story: the kernel will keep trying until the *sum* of backoffs reaches `tcp_retries2`'s ≈ 924 s, and the gateway's `send()` keeps succeeding into the socket buffer the whole time. `TCP_USER_TIMEOUT` caps the time unacknowledged data may sit at 2 s here (2.25 s measured — the check runs at the next RTO expiry after the deadline); keepalive covers the *idle* session at idle + intvl × cnt ≈ 4 s. Both are set per socket in §94.4.4, and the session heartbeat sits above them.
- **Heartbeats are cheap and the resend store is not optional.** Four administrative messages in three idle seconds consumed four sequence numbers, which is why the store must be able to *gap-fill* a range (`35=4`, `123=Y`, `36=next`) rather than resend heartbeats — and why a `SequenceReset` without `GapFill` (a hard reset, `123=N`) is a manual, disconnect-the-strategy operation, never something the machine does on its own.

## 94.4 Techniques

### 94.4.1 The persisted session state: one page, written before the send

```cpp
// seq_journal.hpp — 32 bytes in an mmap'd file (Ch. 31): the session's durable state. Written before the message that uses the number is sent
// (Ch. 93 §93.4.1), read at startup and by the standby (Ch. 99). Stores to MAP_SHARED memory; no syscall on the hot path.
struct alignas(64) SeqJournal { std::uint64_t magic; std::uint64_t epoch; std::uint64_t next_out; std::uint64_t next_in; };
static SeqJournal* open_journal(const char* path) {
    int fd = ::open(path, O_RDWR | O_CREAT, 0600); if (::ftruncate(fd, 4096) != 0) std::abort();
    auto* j = static_cast<SeqJournal*>(::mmap(nullptr, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0)); ::close(fd);
    if (j->magic != 0x4649585345535331ull) { j->magic = 0x4649585345535331ull; j->epoch = 0; j->next_out = 1; j->next_in = 1; }   // first start of the day
    return j;
}
std::uint64_t Session::alloc_seq() { std::uint64_t s = j->next_out; j->next_out = s + 1; return s; }   // the store is written before send(): a crash between the two is "sent" to the recovery logic
```

- **`next_in` advances only after the message is applied**, so a crash mid-message makes the venue resend it (PossDup) rather than lose it; the Ch. 93 correlation path discards the duplicate by `ClOrdID`.
- **`epoch` counts connections**; the resend store records which epoch sent each message, so a `ResendRequest` that spans a reconnect is answered correctly and a diagnostic (Ch. 88) can show "orders sent in epoch 3 and acked in epoch 4".
- **The daily reset** (venues reset sequence numbers at a fixed time, or on the first Logon of the day with `141=Y`) is a *scheduled* event in the sequencer's stream (Ch. 98) so every replica agrees when the numbers restarted; a gateway that resets on its own initiative has desynchronised the firm from itself.
- **Place the page where the crash handler and the standby can see it**: a `/dev/shm` file or the gateway's shared segment (Ch. 59; the segment-epoch discipline of Ch. 91 §91.4.8 applies), never the process heap.

### 94.4.2 The encode fast path: template, `to_chars`, one checksum pass

```cpp
// fix_encode.hpp — body first (35= … last field), then the 8=/9= prefix written BACKWARDS from the body start, then 10=. One pass for the checksum.
template <class F> std::uint32_t Encoder::frame(Frame& f, char msg_type, std::uint64_t seq, std::uint64_t ts_utc_ns, F&& body) {
    char* b = f.buf + 20; char* p = b;                                   // room for "8=FIX.4.4|9=nnnnn|" in front
    *p++ = '3'; *p++ = '5'; *p++ = '='; *p++ = msg_type; *p++ = SOH;
    p = tag(p, "49=", sender, sender_len); p = tag(p, "56=", target, target_len);
    p = tagnum(p, "34=", seq); p = sendtime(p, ts_utc_ns);               // 52= from a cached date + integer arithmetic: no strftime, no locale, no allocation
    p = body(p);                                                         // the per-type fields: 11=, 54=, 38=, 44= via to_chars; constants via memcpy of literals
    std::uint32_t blen = std::uint32_t(p - b); char* q = b; char tmp[8]; auto r = std::to_chars(tmp, tmp + 8, blen);
    q -= 1; *q = SOH; q -= (r.ptr - tmp); std::memcpy(q, tmp, r.ptr - tmp); q -= 2; q[0] = '9'; q[1] = '='; q -= 10; std::memcpy(q, "8=FIX.4.4\x01", 10);
    std::uint32_t sum = 0; for (char* c = q; c < p; ++c) sum += std::uint8_t(*c); sum &= 0xff;     // the only loop; vectorises under -O3 / Clang -O2
    p[0] = '1'; p[1] = '0'; p[2] = '='; p[3] = char('0' + sum / 100); p[4] = char('0' + (sum / 10) % 10); p[5] = char('0' + sum % 10); p[6] = SOH;
    f.len = std::uint32_t(p + 7 - q); if (q != f.buf) std::memmove(f.buf, q, f.len); return f.len;
}
// Price as a scaled integer (Ch. 32): 450025 ticks of 0.01 → "4500.25" — integer division and two digit stores, never a double
std::memcpy(p, "44=", 3); p += 3; p = std::to_chars(p, p + 20, o.px_ticks / 100).ptr; *p++ = '.'; unsigned c = unsigned(o.px_ticks % 100); *p++ = char('0' + c / 10); *p++ = char('0' + c % 10); *p++ = SOH;
```

- **Pre-size the frame per message type** and keep it in the gateway's slot for that order (Ch. 93's in-flight table has a `Frame` per slot), so a cancel or a resend never rebuilds from fields — it patches `34=`/`52=` and re-checksums.
- **For binary venues** (OUCH, iLink 3 SBE, BOE) the "encode" is a `std::bit_cast` of a packed struct with `std::byteswap` where the wire is big-endian (Ch. 62 §62.4.2, Ch. 22) — ~10 ns — and the session framing is a length prefix; keep the same `Session` machine and swap the codec policy (Ch. 21 §21.4.3).
- **`SendingTime` to the millisecond is what FIX requires**; venues that accept microseconds (`52=….ssssss`) get them from the same arithmetic. The *measurement* timestamp — when the frame left — is the NIC's hardware TX stamp (Ch. 65, 68), not tag 52.

### 94.4.3 The resend store and `ResendRequest` handling

```cpp
// resend.hpp — answer "resend 7=from 16=to" from the store: application messages re-sent with 43=Y/122=orig time, runs of admin messages collapsed into one GapFill.
struct SentRec { std::uint64_t seq, ts; OrderFields o; bool app; };   // 4096-entry ring indexed by seq & 4095; lives in the gateway's shm segment
void Session::resend(std::uint64_t from, std::uint64_t to) {
    if (to == 0) to = j->next_out - 1;                                     // 16=0 means "to infinity": everything up to the last sent
    std::uint64_t gap_start = 0;
    auto flush_gap = [&](std::uint64_t upto) { if (!gap_start) return; send_gapfill(gap_start, /*36=NewSeqNo*/ upto); gap_start = 0; };
    for (std::uint64_t s = from; s <= to; ++s) {
        const SentRec& r = store[s & 4095];
        if (r.seq != s || !r.app) { if (!gap_start) gap_start = s; continue; }  // evicted or administrative → part of a gap-fill run
        flush_gap(s); enc.new_order(f, s, now_ns(), r.o, /*possdup*/ true, r.ts); send_frame();
    }
    flush_gap(to + 1);
}
```

- **Never resend what Ch. 93's matrix forbids.** An IOC, a message the strategy has since cancelled, or an order whose slot has been reused is *gap-filled*, not resent, even though the store has it — the resend path consults the in-flight table's state before choosing. The venue sees a `GapFill` and the strategy's intent is preserved.
- **Bound the store by the venue's rules**, not by memory: iLink 3 and BOE replay windows are small and the venue will `NotApplied`/reject a request beyond them; a store larger than the window only costs lines.
- **Inbound gaps**: a message with `34=` above `next_in` is queued (a few slots), a `ResendRequest(7=next_in, 16=0)` goes out, and queued messages are applied once the gap closes; a message *below* `next_in` without `43=Y` is a protocol error (the venue reset without telling you) and the right response is Logout + alert, never silent acceptance.

### 94.4.4 Liveness: heartbeats, `TestRequest`, and setting TCP's timers below the session's

```cpp
// liveness — all three layers set in one place, so "dead" is reported once. Values for a HeartBtInt = 1 s session on a colo cross-connect.
unsigned ut_ms = 2000;                 ::setsockopt(fd, IPPROTO_TCP, TCP_USER_TIMEOUT, &ut_ms, sizeof ut_ms);      // data in flight unacked for 2 s → ETIMEDOUT (§61.4.4)
int on = 1, idle = 1, intvl = 1, cnt = 3; ::setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &on, sizeof on);
::setsockopt(fd, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof idle); ::setsockopt(fd, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof intvl); ::setsockopt(fd, IPPROTO_TCP, TCP_KEEPCNT, &cnt, sizeof cnt);
// session timers as Ch. 49 deadlines, evaluated in the gateway's hot loop (Ch. 43) — never a sleeping thread:
if (st == Established && now - last_tx > hb)         send_admin('0');                                  // keep OUR heartbeat going out
if (st == Established && now - last_rx > hb * 12/10 && !test_req_out) { send_admin('1', "112=TR1\x01", 8); test_req_out = true; }
if (st == Established && now - last_rx > hb * 24/10) { st = Disconnected; supervisor.report(SessionDead{venue, epoch}); }   // Ch. 91 §91.4.3 / Ch. 99
```

- **Order the deadlines**: `TCP_USER_TIMEOUT` ≈ `HeartBtInt` so the socket fails about when the first `TestRequest` would go unanswered; the application-layer verdict at 2.4× is the backstop for a *venue process* that keeps TCP alive but has stopped processing (a wedged venue gateway does exactly this — the kernel ACKs, the application does not).
- **The venue heartbeats you too.** Missing *your* heartbeat makes the venue `TestRequest` you and then disconnect — and apply cancel-on-disconnect to your orders. A gateway stalled by a 1.5 s page-fault storm or a `SIGSTOP`'d debugger (Ch. 5) has just had its book cancelled; the hot-loop placement of the heartbeat (and Ch. 91 §91.4.3's liveness slot) is what makes that impossible in normal operation.
- **Cancel-on-disconnect is configured, not assumed**: it is a per-session venue setting (Appendix K.4), sometimes with a delay; the gateway records which it is, because Ch. 99's failover protocol depends on whether the venue will cancel the dead gateway's orders or the new one must.

### 94.4.5 Throttles, batching and the order of operations on send

- **Token bucket in front of the socket** (Ch. 92 §92.4.1's TSC bucket, refilled at the venue's limit minus a margin): a requote burst that would exceed it is *conflated* (Ch. 44 §44.4.3 — the latest quote per instrument wins) rather than queued, because a queued quote is stale by the time it is sent.
- **One `send()` per message, `TCP_NODELAY`, no corking** on the order session (Ch. 60 §60.4.1, Ch. 61 §61.4.1 — autocorking and Nagle both trade latency for segments); batching is for the *recovery* stream and mass cancels, where a single `writev` of several frames is the right call.
- **Order of operations per send**: gate decision (Ch. 92) → `alloc_seq()` (journal) → in-flight table slot stamped *Sent* with the frame (Ch. 93) → `send()` → hardware TX timestamp collected off the hot path (Ch. 65). The journal and table writes precede the bytes; nothing after `send()` is allowed to fail in a way the recovery logic cannot see.

### 94.4.6 Reconnect and takeover: who owns the numbers

A reconnect by the *same* gateway is §94.3's path — the journal is local, the store is local, 100–300 µs plus the venue's Logon time. A takeover by a *standby* gateway (Ch. 99) needs the same two things: the standby reads the shared `SeqJournal` and resend store (it has been a replica of the in-flight table all along — Ch. 98 §98.2.3), logs on with `next_out`/`next_in`, and answers the venue's `ResendRequest` from the store. The protocol questions — *when* may it log on (the venue allows one session per `SenderCompID`; a Logon while the old one is alive is rejected or, worse, bumps the old one), *who decides* the old gateway is dead, and how to make a zombie gateway's late `send()` harmless — are Ch. 99's leases and fencing tokens. This chapter's contribution is that the state the standby needs is already in shared memory, written before every send, so takeover is a Logon, not a rebuild.

## 94.5 Pitfalls & anti-patterns

- **Trusting the socket.** `send()` succeeding means bytes entered a buffer; with default timers the kernel will hold a dead connection open for ~15 minutes (§94.3). Set `TCP_USER_TIMEOUT` and keepalive, and treat the session heartbeat as the authority.
- **Sequence numbers in process memory.** A gateway that restarts at `34=1` while the venue expects 1007 is rejected at Logon (or, at venues that accept a low sequence with a warning, makes every subsequent message ambiguous). Persist both counters before every send (§94.4.1).
- **Resending what should be gap-filled.** A resent IOC that the venue *did* receive is a duplicate it may execute if the `ClOrdID` check is lax; a resent order the strategy has cancelled is a position. The resend path reads the in-flight table (§94.4.3) and Ch. 93 §93.2.3's matrix, not just the store.
- **A general-purpose FIX engine on the hot path.** Per-message allocation, `std::string` tags, `strftime`, a mutex around the session, a thread per connection: each is a µs; together they are Ch. 106 §106.3's entire software budget. Use the engine for the *admin* session and certification, the template encoder for orders — or one machine for both, as here.
- **Heartbeats from a sleeping thread.** A heartbeat thread that oversleeps under load (Ch. 48–49) lets the venue's `TestRequest` go unanswered → disconnect → cancel-on-disconnect. The heartbeat is a deadline in the hot loop.
- **Resetting sequence numbers to "fix" a mismatch.** `SequenceReset-Reset` (`123=N`) discards the venue's ability to tell you what it received; after one, every in-flight order is in the unknown state. It is an operator action after reconciliation (Ch. 93 §93.4.5), not an automatic recovery.
- **Conflating the message timestamp with the measurement timestamp.** `52=SendingTime` is formatted to the millisecond from a clock read before the encode; the latency ledger (Ch. 87) uses the NIC's TX stamp. A FIX engine that *measures* with tag 52 is reporting its own formatting cost.

## 94.6 Exercises & checklist

1. **The encode under both compilers.** Build `fixsess.cpp` with GCC and Clang, read the checksum loop's asm (Ch. 4) and explain the 2× gap; make GCC's version match with `-O3` or a manual 8-lane sum, and verify that the price field never goes through a floating-point conversion.
2. **Resend with the matrix.** Extend `resend()` to consult an in-flight table: mark three of the seven "forgotten" orders as IOC / cancelled and show the venue receives four `PossDup` orders and gap-fills around the rest.
3. **TCP timers vs the session.** Run `tcpdetect` with `TCP_USER_TIMEOUT` of 500 ms, 2 s and 10 s, then with a `HeartBtInt` of 1 s on the FIX session, and record which layer reports first in each combination; argue for one ordering.
4. **Binary codec policy.** Add a SoupBinTCP/OUCH codec policy to `Session` (length-prefixed frames, unsequenced client messages, a server sequence resumed at login) and measure the encode against the FIX one.
5. **Takeover rehearsal (with Ch. 99).** Start a second `Session` process on the same journal and store, kill the first mid-stream, and time the Logon + resync of the second; record what the venue double's `ResendRequest` asked for.

Checklist:

- [ ] `next_out`/`next_in` live in a shared, persisted page and are **written before the send** (§94.4.1); the resend store is in the same segment.
- [ ] Order encode is a template + `to_chars` + one checksum pass, **measured** at ≤ 100 ns (§94.3); no allocation, no `strftime`, no mutex.
- [ ] `TCP_USER_TIMEOUT` ≈ `HeartBtInt`, keepalive on the idle session, heartbeats/`TestRequest` as hot-loop deadlines; the dead-venue verdict is reported **once** to the supervisor (§94.4.4).
- [ ] Resends consult the in-flight table; IOCs and cancelled orders are gap-filled (§94.4.3); a hard `SequenceReset` requires an operator.
- [ ] The venue's cancel-on-disconnect setting and resend-window size are recorded per session (§94.4.4, §94.4.3) — Ch. 99 reads them.
- [ ] A token bucket at the venue's throttle sits before the socket; requotes conflate rather than queue (§94.4.5).
- [ ] Time-to-detect a silent venue and time-to-resync after a reconnect are **measured numbers** in the readiness gate (Appendix M.4), not assumptions.

## 94.7 References

- FIX Trading Community, *FIX Session Protocol* (FIX 4.4 Vol. 2; FIXT 1.1) — Logon/Logout, Heartbeat/TestRequest, `MsgSeqNum`, `ResendRequest`/`SequenceReset`, `PossDupFlag`/`OrigSendingTime`, the recovery rules this chapter's machine implements.
- Nasdaq, *SoupBinTCP 4.0* and *OUCH 5.0* specifications — sequenced server stream, unsequenced client messages, login with a requested sequence number.
- CME Group, *iLink 3 Binary Order Entry* — Negotiate/Establish/Sequence/Terminate, retransmit requests and the bounded replay window; *FIXP* (FIX Performance Session Layer).
- Cboe, *Binary Order Entry (BOE)* specification — login/replay semantics and the heartbeat rules.
- `tcp(7)` — `tcp_retries2`, `TCP_USER_TIMEOUT`, `SO_KEEPALIVE`/`TCP_KEEPIDLE`/`TCP_KEEPINTVL`/`TCP_KEEPCNT`; RFC 6298 (RTO computation) and RFC 5482 (TCP User Timeout Option).
- `std::to_chars` — C++17 `<charconv>`; Ch. 62 §62.4.1.
- Ch. 93 (order identity and recovery), Ch. 61 §61.4.4 (failure detection timing), Ch. 99 (takeover protocol), Appendix J.3/J.5 and K.4/K.6.

## 94.8 Additional Reading

- Venue conformance and certification guides (any venue's "certification test script") — the scripted session scenarios (gap on logon, resend across a reconnect, heartbeat miss) a gateway must pass before going live; Ch. 105 §105.4.6 treats them as a release step.
- Open-source FIX engines (QuickFIX, Fix8, OnixS documentation) — read their session state machines and resend logic for completeness, and their per-message allocation paths for what *not* to do on the hot path.
- Ch. 103 §103.4.3 — the fault-injecting venue double that exercises this chapter's machine with drops, reorders, late acks and bogus sequence numbers under many seeds.

---

*Next: Ch. 95 — Crash Handling, Core Dumps & Post-Mortem Diagnostics: when the gateway (or anything else) dies mid-send, the fifty microseconds between the faulting instruction and the core file — the kill-switch handoff, preserving this chapter's sequence page and resend store, and capturing the state without stalling the survivors.*
