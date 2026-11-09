# Part IX — Market Data, NIC & Fabric

# Chapter 64 — Precise & Scheduled Transmission

> **Prerequisites:** Ch. 63 (PTP / hardware timestamping — a scheduled send needs a disciplined clock), Ch. 57 (microbursts — the thing precise pacing avoids creating), Ch. 60 (NIC features — the PHC and TX hardware timestamping this builds on), Ch. 55 (socket options), Ch. 17 (the timestamp counter).
>
> **Leads into:** Ch. 70 (transports that pace their own sends), Ch. 69 (fabric congestion that pacing eases). The send-side dimension the book has ignored: not *what* to send, but *exactly when*.

---

## 64.1 Why it matters: "send now" is not the only option

Every send path in the book so far answers one question — *how fast can I get this packet onto the wire?* — and the answer is always "now, as fast as possible." But there is a second question the hot path often needs and the sockets API historically couldn't answer: *at exactly what time should this packet leave?* Precise, scheduled transmission — telling the NIC "put this packet on the wire at nanosecond T" — is a capability that changes several things a trading system cares about, and it's an area the book hasn't touched at all.

Three motivations. First, **avoiding self-inflicted microbursts** (Ch. 57). When you send a batch of orders or messages "as fast as possible," you create a burst — packets back-to-back at line rate — that can overwhelm a downstream switch buffer (Ch. 65), your own NIC's queue, or the receiver, adding queuing jitter to your *own* traffic. **Pacing** the sends (spreading them over time to a smooth rate) trades a microsecond of deliberate spacing for the removal of burst-induced jitter — often a net latency win at the tail (Ch. 1). Second, **coordinated timing**: sending to multiple venues at a precisely coordinated instant, or releasing an order at an exact time relative to a market event, requires *scheduling* the send, not racing it. Third, **determinism**: a system that sends at scheduled times has a *predictable* egress pattern, which is easier to reason about, measure (Ch. 94), and keep jitter-free than one that bursts whenever work is ready.

The enabling technology arrived in the Linux kernel and NIC hardware as the **`SO_TXTIME` / ETF (Earliest TxTime First) / hardware LAUNCHTIME** stack, built on the **EDT (Earliest Departure Time)** model and the **TSN (Time-Sensitive Networking)** standards. You attach a transmit timestamp to a packet, and the qdisc + NIC hold it until that time, then put it on the wire — with hardware precision disciplined by PTP (Ch. 63). This is a genuinely different send model: the packet's *departure time* is a first-class property. This chapter explains the scheduled-transmission model (§64.2), measures send-time accuracy and pacing precision (§64.3), details `SO_TXTIME`/ETF/LAUNCHTIME, pacing, and TSN (§64.4), and warns about the clock discipline and deadline pitfalls (§64.5). It's the "send at exactly when, not just now" chapter.

## 64.2 Mental model: EDT, `SO_TXTIME`, qdiscs, and hardware LAUNCHTIME

The core shift is that a packet carries a **departure time**, and the egress path honors it:

```
   "send now":         app -> socket -> qdisc -> NIC -> wire   (as fast as possible)
   scheduled send:     app -> [attach TxTime T via SO_TXTIME] -> ETF qdisc (holds until ~T)
                                                              -> NIC hardware LAUNCHTIME (puts on wire AT T)
                              ▲ T is on the PTP-disciplined clock (Ch. 63)
```

The layers, from the EDT model down to the wire:

- **The EDT (Earliest Departure Time) model.** Modern Linux egress is built around each packet having an *earliest departure time* — a timestamp saying "don't send before this." Pacing, rate-limiting, and scheduling all express themselves as setting each packet's EDT. It replaced the older token-bucket mental model: instead of "how many tokens do I have," it's "when is this packet allowed to leave." Both pacing (a smooth rate) and precise scheduling (an exact time) are EDT settings.
- **`SO_TXTIME` — the socket API.** You set `SO_TXTIME` on the socket and attach a per-packet transmit time (an `SCM_TXTIME` control message carrying a nanosecond timestamp on **`CLOCK_TAI`** — the only clock the ETF qdisc accepts; the PHC itself cannot be named, so `CLOCK_TAI` must be disciplined to the PHC via `phc2sys` (Ch. 63) because the NIC compares the launch time against *its* clock). That timestamp is the packet's requested departure time.
- **ETF (Earliest TxTime First) qdisc — the software scheduler.** The ETF qdisc holds each packet until (near) its TxTime, releasing it to the NIC just in time, in departure-time order. It can operate in *software* (the kernel releases at the right time) or hand off to *hardware* offload. A delta parameter controls how early it hands packets to the NIC.
- **Hardware LAUNCHTIME — the NIC.** NICs with TSN/LAUNCHTIME support take the departure time and put the packet on the wire at *that hardware time* — tens-of-nanosecond precision (32 ns launch-time granularity on Intel i210), immune to software jitter (Ch. 43) between the qdisc and the wire. This is the precise, deterministic layer: the NIC's own clock (PTP-disciplined, Ch. 63) gates transmission.
- **TSN (Time-Sensitive Networking).** The IEEE 802.1 standards family — the **time-aware shaper (802.1Qbv)** and related — that makes Ethernet deliver on scheduled, deterministic time windows. LAUNCHTIME and the whole scheduled-transmission stack are the endpoint side of TSN; the fabric side (Ch. 65) can honor time-aware scheduling too.

The mental model: **a scheduled send is a packet with a departure timestamp that the qdisc and NIC honor, gated by a PTP-disciplined clock — turning "send" from an immediate action into a time-addressed one.** Pacing is the special case where you set a stream of departure times at a steady interval; precise scheduling is setting a specific one. Everything depends on the clock (Ch. 63): a departure time is meaningless without an accurate, disciplined clock to measure it against.

## 64.3 Measure it: send-time accuracy and pacing precision

*Toolchain and machine: Appendix L.3.*

The measurements are about *accuracy* — how close the actual on-wire time is to the requested time — and require hardware timestamps (Ch. 60, 63) to observe the true wire time.

- **Send-time accuracy.** Request a departure time T (via `SO_TXTIME`), capture the actual on-wire timestamp (NIC hardware TX timestamp, Ch. 60, 63), and measure the error (actual − requested). Compare software ETF vs hardware LAUNCHTIME:

| Mechanism | Send-time accuracy (error vs requested) | Notes |
|---|---|---|
| Best-effort "send now" | N/A (no target); jitter from software + qdisc | the baseline — no time control |
| **ETF qdisc (software)** | ~microseconds | software release timing; subject to some host jitter (Ch. 43) |
| **Hardware LAUNCHTIME** | ~tens of nanoseconds | NIC hardware gates the wire; near-immune to software jitter |

  The lesson: **hardware LAUNCHTIME is the precise option** (tens of ns), software ETF is looser (microseconds) but needs no special NIC. Choose by how precise the schedule must be.
- **Pacing precision and its effect on the tail.** Pace a stream of sends at a target rate (setting evenly-spaced departure times) and measure (a) the inter-packet spacing accuracy on the wire and (b) the effect on downstream jitter — a paced stream should show a *flatter* latency distribution at the receiver/switch (Ch. 65) than a bursted one, because it doesn't overflow buffers (Ch. 57). Quantify the trade: pacing adds a small, deliberate delay per packet but removes burst-induced queuing jitter.
- **The single-message cost.** Measure the latency of *one* scheduled send vs one immediate send — scheduling adds a small fixed cost (attaching the timestamp, the qdisc). For the single latency-critical order where you want it out *now*, that cost is pure overhead (§64.5); scheduling pays off for *coordinated* or *paced* sends, not the race-it-out single order.

The lessons: measure accuracy with hardware timestamps (software timing can't see the true wire time); hardware LAUNCHTIME gives tens-of-nanosecond scheduling, software ETF microsecond; pacing flattens the downstream tail by not bursting; and scheduling has a per-send cost that makes it a *coordination/pacing* tool, not something to put on the race-it-out single-order path (§64.5). All of it depends on a disciplined clock (Ch. 63) — the accuracy is only as good as the clock the times are measured against.

## 64.4 Techniques

### 64.4.1 `SO_TXTIME` + ETF + hardware LAUNCHTIME

- **Set up the ETF qdisc and clock.** ETF is a *leaf* under an `mqprio` parent, one per hardware TX queue; the socket reaches it through `SO_PRIORITY` → traffic class → queue. Configure it tied to the PTP-disciplined clock (Ch. 63 — `CLOCK_TAI` synced to the PHC; the TAI-UTC offset must be set, §63.4.1): the `delta` sets how early the qdisc hands packets to the NIC; `offload` enables hardware LAUNCHTIME where the NIC supports it. The complete setup and a scheduled send:

```cpp
// txtime.cpp — one scheduled UDP send: SO_TXTIME on CLOCK_TAI, an SCM_TXTIME per packet, error-queue read-back.
// Build: g++ -O2 -std=c++20 txtime.cpp -o txtime        (Linux >= 4.19 UAPI headers)
// Setup once (root) — the tc-etf(8) layout: mqprio parent, SO_PRIORITY 3 -> traffic class 0 -> queue 0 -> ETF leaf:
//   tc qdisc replace dev eth0 parent root handle 100 mqprio num_tc 3 map 2 2 1 0 2 2 2 2 2 2 2 2 2 2 2 2 queues 1@0 1@1 2@2 hw 0
//   tc qdisc replace dev eth0 parent 100:1 etf clockid CLOCK_TAI delta 200000 offload   # drop 'offload' without LAUNCHTIME
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <ctime>
#include <unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <linux/net_tstamp.h>   // sock_txtime, SOF_TXTIME_*
#include <linux/errqueue.h>     // sock_extended_err, SO_EE_ORIGIN_TXTIME, SO_EE_CODE_TXTIME_*

int main() {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    int prio = 3;                                                   // -> TC 0 -> the ETF queue (see map above)
    setsockopt(fd, SOL_SOCKET, SO_PRIORITY, &prio, sizeof prio);
    sock_txtime cfg{}; cfg.clockid = CLOCK_TAI;                     // the ONLY clock the ETF qdisc accepts
    cfg.flags = SOF_TXTIME_REPORT_ERRORS;                           // | SOF_TXTIME_DEADLINE_MODE for "send BY T"
    if (setsockopt(fd, SOL_SOCKET, SO_TXTIME, &cfg, sizeof cfg) < 0) { perror("SO_TXTIME"); return 1; }

    sockaddr_in dst{}; dst.sin_family = AF_INET; dst.sin_port = htons(9000); inet_pton(AF_INET, "10.0.0.2", &dst.sin_addr);
    timespec now{}; clock_gettime(CLOCK_TAI, &now);                 // CLOCK_TAI disciplined to the PHC by phc2sys (Ch. 63)
    std::uint64_t txtime = std::uint64_t(now.tv_sec) * 1'000'000'000ull + now.tv_nsec + 500'000;   // launch at now + 500 us

    char payload[64] = "order";
    iovec iov{payload, sizeof payload};
    alignas(cmsghdr) char cbuf[CMSG_SPACE(sizeof txtime)];
    msghdr msg{}; msg.msg_name = &dst; msg.msg_namelen = sizeof dst; msg.msg_iov = &iov; msg.msg_iovlen = 1;
    msg.msg_control = cbuf; msg.msg_controllen = sizeof cbuf;
    cmsghdr* c = CMSG_FIRSTHDR(&msg); c->cmsg_level = SOL_SOCKET; c->cmsg_type = SCM_TXTIME; c->cmsg_len = CMSG_LEN(sizeof txtime);
    std::memcpy(CMSG_DATA(c), &txtime, sizeof txtime);
    if (sendmsg(fd, &msg, 0) < 0) { perror("sendmsg"); return 1; }

    usleep(2000);                                                   // off the hot path: did the launch time hold?
    char ebuf[256], ectrl[512]; iovec eiov{ebuf, sizeof ebuf};
    msghdr emsg{}; emsg.msg_iov = &eiov; emsg.msg_iovlen = 1; emsg.msg_control = ectrl; emsg.msg_controllen = sizeof ectrl;
    if (recvmsg(fd, &emsg, MSG_ERRQUEUE | MSG_DONTWAIT) >= 0)
        for (cmsghdr* e = CMSG_FIRSTHDR(&emsg); e; e = CMSG_NXTHDR(&emsg, e))
            if (e->cmsg_level == SOL_IP && e->cmsg_type == IP_RECVERR) {
                auto* se = reinterpret_cast<sock_extended_err*>(CMSG_DATA(e));
                if (se->ee_origin == SO_EE_ORIGIN_TXTIME)
                    std::printf("txtime error: %s\n", se->ee_code == SO_EE_CODE_TXTIME_MISSED
                                ? "MISSED (ECANCELED at dequeue)" : "INVALID_PARAM (EINVAL at enqueue)");
            }
    return 0;
}
```

  **Which NICs offload.** In-tree ETF offload (`TC_SETUP_QDISC_ETF`) exists for Intel i210 (`igb`, queues 0–1, 32 ns units — `igb` rejects other controllers), i225/i226 (`igc`), Intel E830 (`ice`, 2025+) and embedded TSN MACs (`stmmac`, NXP `enetc`) — all 1–2.5 GbE parts. The 10/25/100 GbE NICs on trading hosts (Solarflare/AMD, Mellanox ConnectX, Intel E810) have no LAUNCHTIME: `offload` fails and you get *software* ETF with its microsecond accuracy (§64.3), or the vendor's own pacing (`mlx5` `tx_port_ts`/`SO_TXTIME` on ConnectX-6 Dx+, DPDK `RTE_ETH_TX_OFFLOAD_SEND_ON_TIMESTAMP`). Check `ethtool -T` and `dmesg` after `tc qdisc replace`, and measure (§64.3) before designing around hardware precision.
- **Attach a departure time per packet.** Set `SO_TXTIME` on the socket; for each send, attach a `SCM_TXTIME` control message with the target nanosecond timestamp. The qdisc/NIC hold the packet until (near) that time. The timestamp must be on the *same clock* the qdisc is configured for (Ch. 63) — a mismatch makes the qdisc drop the packet at enqueue (§64.5).
- **Choose software vs hardware.** Hardware LAUNCHTIME (offload) for tens-of-nanosecond precision on a supporting NIC; software ETF where the NIC lacks it and microsecond precision suffices (§64.3). Verify the NIC's LAUNCHTIME support and PTP discipline before relying on hardware precision.
- **Handle the deadline.** If a packet's departure time has already passed when it reaches the qdisc (you scheduled too tight, or the host was late — Ch. 43), the ETF qdisc *drops* it rather than sending late — at dequeue with `ECANCELED` / `SO_EE_CODE_TXTIME_MISSED`, or at enqueue with `EINVAL` / `SO_EE_CODE_TXTIME_INVALID_PARAM` for a bad clock or a time already past. Two knobs change this: `SOF_TXTIME_REPORT_ERRORS` delivers each drop on the socket error queue (`recvmsg(MSG_ERRQUEUE)`, `SO_EE_ORIGIN_TXTIME` — the listing above), and `SOF_TXTIME_DEADLINE_MODE` (set on *both* socket and qdisc, `deadline_mode`) turns "send *at* T" into "send *by* T". Schedule with enough lead time, and drain the error queue off the hot path (§64.5).

### 64.4.2 Pacing: `SO_MAX_PACING_RATE`, FQ, and the EDT model

- **Rate pacing with `SO_MAX_PACING_RATE`.** For a smooth egress rate (rather than exact times), `SO_MAX_PACING_RATE` on the socket, honored by the **FQ (Fair Queue) qdisc** via the EDT model, spaces packets to the target rate — no bursting. Use it to smooth bulk egress (capture upload — Ch. 90, snapshot/replay streams — Ch. 58) so they don't create microbursts that jitter the hot path's own traffic.
- **Pace to avoid self-inflicted bursts (Ch. 57).** When you have a batch to send (many orders, a snapshot), pacing it to a rate the downstream fabric (Ch. 65) and receiver can absorb smoothly *removes* the burst-induced queuing jitter (§64.3) — often a tail-latency win even though each individual packet leaves slightly later. This is the counter-intuitive lesson: sending *slower* (paced) can give a *flatter tail* than bursting.
- **EDT as the unifying model.** Both pacing (steady departure times) and scheduling (specific departure times) are EDT settings (§64.2) — the same mechanism, different time patterns. Understanding egress as "set each packet's earliest departure time" unifies rate-limiting, pacing, and precise scheduling into one model.

### 64.4.3 TSN, time-aware shaping, and coordinated sends

- **Coordinated multi-venue / multi-order sends.** To release orders to several venues at a precisely coordinated instant (or in a precise relative order), schedule each send's departure time (§64.4.1) rather than racing them out sequentially — the NIC releases them at the planned times, giving deterministic, coordinated egress that "send now" can't. Useful for strategies that must hit multiple venues simultaneously or in a controlled sequence.
- **Time-aware shaping (802.1Qbv, Ch. 65).** In a TSN fabric, the network can reserve *time windows* for classes of traffic (the time-aware shaper; Linux's endpoint implementation is the `taprio` qdisc, which can sit above ETF), so scheduled traffic gets deterministic, contention-free transmission windows end-to-end. The endpoint's LAUNCHTIME (§64.4.1) and the fabric's time-aware shaping (Ch. 65) together give scheduled, deterministic delivery across the network — the full TSN picture.
- **Deterministic egress for measurability.** A scheduled-send system has a *predictable* egress pattern, which makes the tick-to-trade measurement (Ch. 94) cleaner (you know when packets left) and the system's behavior easier to reason about and keep jitter-free. Determinism (Ch. 1, 86) extends to *when you transmit*, not just what you compute.

## 64.5 Pitfalls & anti-patterns: clock discipline and missed deadlines

- **Scheduling without a disciplined clock (the cardinal error).** A departure time is meaningless without an accurate, PTP-disciplined clock (Ch. 63) to measure it against — an undisciplined or drifting clock sends at the wrong wall-clock time, defeating coordination and making "nanosecond precision" a fiction. Scheduled transmission *requires* the Ch. 63 clock infrastructure; it's a precondition, not an add-on (§64.2).
- **Clock-ID mismatch.** The `SO_TXTIME` timestamp's clock must match the ETF qdisc's configured clock (`CLOCK_TAI` typically) and the NIC's PHC (Ch. 63). A mismatch (e.g. the socket's `sock_txtime.clockid` set to `CLOCK_MONOTONIC` while the qdisc uses `CLOCK_TAI`) makes the ETF qdisc drop the packet at enqueue — `EINVAL`, `SO_EE_CODE_TXTIME_INVALID_PARAM` on the error queue when `SOF_TXTIME_REPORT_ERRORS` is set — so nothing leaves the NIC (unless the qdisc was created with `skip_sock_check`, in which case a `CLOCK_MONOTONIC` value is read as a TAI instant decades in the past and dropped as expired). Align all three (§64.4.1).
- **Missed deadlines / scheduling too tight.** If a packet's departure time has passed by the time it reaches the qdisc (scheduled with too little lead, or host context-switch/scheduling jitter — Ch. 43 — delayed it), ETF drops it (`ECANCELED`/`SO_EE_CODE_TXTIME_MISSED`) — it sends late only when socket *and* qdisc are in deadline mode (§64.4.1). Schedule with adequate lead time and read the error queue; don't schedule departure times so tight that normal host jitter blows them (§64.4.1).
- **Pacing the single latency-critical order.** Applying pacing/scheduling to the *one* order you want out *now* — adding deliberate delay to the thing you're racing (§64.3). Scheduling/pacing is for *coordination* and *bulk smoothing*, not the single race-it-out order; the hot order path still sends immediately (bypass — Ch. 67, or inline — Ch. 74).
- **Assuming software ETF is nanosecond-precise.** Software ETF release timing is subject to host scheduling jitter (Ch. 43) — microsecond, not nanosecond, precision (§64.3). For tens-of-nanosecond scheduling you need hardware LAUNCHTIME; don't rely on software ETF for tight coordination.
- **NIC/driver support assumed.** Hardware LAUNCHTIME, ETF offload, and TSN features vary by NIC and driver; verify support (and PTP discipline) on your hardware before designing around tens-of-nanosecond scheduling (§64.4.1 — most 10/25/100 GbE trading NICs have none) — the Appendix A portability discipline applied to TSN.
- **Pacing rate mismatched to the fabric.** Pacing to a rate the downstream fabric (Ch. 65) or receiver *still* can't absorb doesn't remove the burst problem — it just moves it. Pace to a rate matched to the bottleneck's absorption (§64.4.2), measured (Ch. 65).
- **Forgetting pacing is a tail trade.** Pacing adds per-packet delay to remove burst jitter — a *tail* win but a per-packet-latency cost. On a path where per-message latency dominates and there's no burst problem, pacing is pure overhead. Apply it where bursts actually cause downstream jitter (§64.4.2).

## 64.6 Exercises & checklist

**Exercises:**

1. **Send-time accuracy.** Schedule sends at target times via `SO_TXTIME` + ETF (software) and via hardware LAUNCHTIME; measure the error against the actual NIC TX timestamp (Ch. 63) — reproduce the microsecond-vs-nanosecond gap (§64.3).
2. **Pacing flattens the tail.** Send a batch bursted vs paced (`SO_MAX_PACING_RATE`/FQ); measure the downstream latency distribution (at a switch/receiver, Ch. 65) — show pacing removes burst-induced jitter (§64.3, §64.4.2).
3. **Missed deadline.** Schedule a departure time too tight (less than host jitter) and observe ETF dropping/late-sending; add lead time and handle the case (§64.4.1, §64.5).
4. **Clock mismatch.** Schedule on the wrong clock ID and observe the enqueue drop on the error queue (`SO_EE_CODE_TXTIME_INVALID_PARAM`); align `SO_TXTIME`, qdisc, and PHC clocks (Ch. 63) and confirm accuracy (§64.5).
5. **Coordinated sends.** Schedule sends to two destinations at a coordinated instant; verify (hardware timestamps) they leave at the planned relative times, vs racing them sequentially (§64.4.3).

**Checklist:**

- [ ] Scheduled transmission runs on a **PTP-disciplined clock** (Ch. 63); the `SO_TXTIME`, ETF qdisc, and NIC PHC **clock IDs all match** (§64.5).
- [ ] **Hardware LAUNCHTIME** is used where tens-of-nanosecond precision is required (and the NIC actually offloads it); **software ETF** only where microsecond suffices (§64.3, §64.4.1).
- [ ] Departure times are scheduled with **adequate lead time**; missed-deadline (drop/late) behavior is **handled explicitly** (§64.4.1, §64.5).
- [ ] **Pacing** (`SO_MAX_PACING_RATE`/FQ, EDT) smooths **bulk egress** (capture/replay — Ch. 90/58) to avoid self-inflicted microbursts (Ch. 57); paced to a rate the **fabric can absorb** (§64.4.2).
- [ ] Scheduling/pacing is **not applied to the single race-it-out order** — that still sends immediately (bypass — Ch. 67) (§64.5).
- [ ] Pacing is applied where **bursts cause measured downstream jitter** (a tail trade), not blanket (§64.4.2, §64.5).
- [ ] **NIC/driver LAUNCHTIME/ETF/TSN support** is verified on the actual hardware (§64.4.1, §64.5).
- [ ] Where end-to-end determinism is needed, endpoint LAUNCHTIME is combined with **fabric time-aware shaping** (802.1Qbv, Ch. 65) (§64.4.3).

## 64.7 References

- The Linux kernel documentation on **`SO_TXTIME`**, the **ETF qdisc** (`tc-etf(8)`), the **FQ qdisc**, and the **EDT model** (Van Jacobson's "EDT" netdev talks) — the mechanisms of §64.4.
- The **TSN / IEEE 802.1Qbv** (time-aware shaper) and 802.1AS (timing) standards — the deterministic-networking framework (§64.2, §64.4.3).
- NIC/driver documentation for **hardware LAUNCHTIME** (Intel igb/igc, and TSN-capable NICs) — hardware scheduled transmission (§64.4.1).
- Ch. 63 (PTP — the clock discipline scheduled transmission requires), Ch. 57 (microbursts — what pacing avoids), Ch. 60 (NIC TX timestamping), Ch. 65 (fabric time-aware shaping), Ch. 17 (timestamp counter).

## 64.8 Additional Reading

- Netdev and TSN community talks on `SO_TXTIME`, EDT, and time-aware shaping — the design and deployment of scheduled transmission (§64.2, §64.4).
- Van Jacobson's EDT / "bufferbloat and pacing" material — why the departure-time model replaced token buckets (§64.2, §64.4.2).
- Ch. 70 (*Transport Beyond TCP*) — transports that pace their own sends; Ch. 69 (*Lossless Ethernet*) — congestion that pacing eases; Ch. 63 (*PTP*) — the clock; Ch. 65 (*Fabric*) — end-to-end time-aware delivery; **Appendix F** — EDT / LAUNCHTIME / TSN glossary.

---

*Next: Ch. 65 — Network Fabric & Switching, the hop between your NIC and the exchange: store-and-forward vs cut-through vs layer-1 switches, per-hop latency in nanoseconds, and why the switch on the tick-to-trade path is as much a latency decision as any line of code.*
