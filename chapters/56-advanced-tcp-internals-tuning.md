# Part VIII — Kernel I/O, Sockets & Zero-Copy

# Chapter 56 — Advanced TCP Internals & Tuning

> **Prerequisites:** Ch. 51 (native I/O, epoll, busy-poll), Ch. 55 (socket optimization — `TCP_NODELAY`/`TCP_QUICKACK`, buffer sizing and the 40 ms Nagle/delayed-ACK measurement; this chapter goes *under* those knobs), Ch. 50 (warming — a cold TCP connection is slow on the first message), Ch. 60 (NIC offloads — GRO/GSO/TSO live here), Ch. 43 (syscalls/context switches on the I/O path).
>
> **Leads into:** Ch. 67 (userspace TCP over bypass — where this chapter's knobs have their `EF_*` equivalents), Ch. 70 (transport beyond TCP), Appendix C (the quiet-box checklist these TCP `sysctl`s sit beside). The transport deep-dive Ch. 55 opened: order entry and feed recovery (Ch. 58) usually ride TCP, and TCP's defaults are tuned for throughput and fairness — the opposite of what the order path wants.

---

## 56.1 Why it matters: TCP's defaults optimize for the wrong thing

Order entry — the messages that actually send your orders to the exchange — is almost always **TCP**, because an order must not be silently lost the way a market-data multicast packet can be (Ch. 58); you need the reliability, ordering, and flow control TCP provides. The feed-recovery channels of Ch. 58 (retransmission, snapshot) are usually TCP too. So the order path, the most latency-critical output you have (Ch. 93), rides a protocol whose entire design was tuned for **bulk throughput and network fairness across the public internet** — heuristics that actively *add latency* to the single small, latency-critical message an order actually is. TCP wants to batch, to wait for more data, to back off politely, to acknowledge lazily — and every one of those instincts is wrong for "send this 100-byte order *now*, on a clean, dedicated, colocated link (Appendix K)."

Ch. 55 dealt with the socket-level knobs — `TCP_NODELAY`, `TCP_QUICKACK`, buffer sizing, `tcp_slow_start_after_idle` — and measured the 40 ms Nagle/delayed-ACK stall they exist to prevent (§55.3). This chapter opens the stack underneath them: the **send path** a small write actually travels and the second coalescer (autocorking) that `TCP_NODELAY` does *not* switch off; the **delayed-ACK timer** (`ato`, pingpong mode, the quickack counter) and therefore *why* `TCP_QUICKACK` is not sticky; the **congestion window** on a cold or idle connection — and why CUBIC and BBR behave differently after a quiet spell; **loss recovery** (SACK, RACK-TLP, the 200 ms RTO floor) — what it costs when a lone order is lost on a link that never congests; **pacing**; the receive-side **offloads** (GRO/LRO) that batch incoming segments; and **buffer autotuning**. And it shows you how to *see* all of this from userspace through `TCP_INFO` — the stack's own view of a connection — instead of inferring it from latency plots.

Understanding TCP's internals is what lets you turn off the throughput heuristics that hurt and keep the reliability that helps — to make TCP behave, on a dedicated colo link, almost like a reliable datagram: send immediately, acknowledge immediately, don't batch, don't ramp, don't back off. This chapter covers the send path, the ACK clock, and congestion/loss control (§56.2), measures the connection's internal state across a burst and an idle gap plus the offload latency (§56.3), gives the tuning techniques (§56.4), and warns about the interactions (§56.5). It's the "make TCP stop helping" chapter for the order path.

## 56.2 Mental model: the send path, the ACK clock, and congestion control

A small TCP write travels a longer road than it looks:

```
   send():  copy into socket send buffer -> [Nagle? autocork?] -> [cwnd? pacing?] -> [TSO/GSO] -> qdisc -> NIC -> wire
   recv:    NIC -> [GRO/LRO coalesce?] -> kernel -> [delayed ACK: ato / pingpong / quick] -> socket recv buffer -> read()
```

The latency-relevant mechanisms, in the order a write meets them:

- **Two coalescers on the send path, not one.** `tcp_sendmsg` copies the write into an `skb` on the write queue and calls `tcp_push`, where two independent checks can *hold* it. The first is **Nagle** (`tcp_nagle_check`: hold a sub-MSS segment while unacknowledged data is outstanding) — `TCP_NODELAY` disables it, Ch. 55 §55.2 has the model and §55.3 the 40 ms measurement. The second is **autocorking** (`net.ipv4.tcp_autocorking`, default on since Linux 3.14): if a previous `skb` of this flow is *still sitting in the qdisc or the NIC's TX queue* — not yet handed to the wire — a following small write is held and merged into it, until the TX-completion callback releases the socket. `TCP_NODELAY` does **not** turn autocorking off; only the `sysctl` does. The hold is bounded by the NIC's transmit-completion latency (a few µs on a kernel-stack NIC, more with interrupt coalescing — Ch. 60), so it is a microsecond tax, not Nagle's millisecond one — but it is a tax on exactly the back-to-back small writes an order gateway produces.
- **The delayed-ACK timer, from the inside.** The receiver keeps an adaptive **ACK timeout, `ato`** (`icsk_ack.ato`), estimated from the inter-arrival time of incoming segments and clamped to **[40 ms, 200 ms]** (`TCP_ATO_MIN`/`TCP_DELACK_MIN` = HZ/25, `TCP_DELACK_MAX` = HZ/5). When a segment arrives and the stack decides to *delay* its ACK, the timer is armed for `ato` — that is the 40 ms Ch. 55 measured (`tcpi_ato` in §56.3 reads exactly 40 000 µs once a flow is established). Whether an ACK is delayed at all depends on two pieces of per-socket state: **pingpong mode** (`icsk_ack.pingpong`, entered when the socket *sends* data shortly after receiving — the stack's definition of an "interactive" session, where ACKs are expected to piggyback on replies) and the **quickack counter** (`icsk_ack.quick`, seeded at connection start — up to 16 immediate ACKs — and decremented on every ACK sent). `TCP_QUICKACK` *leaves* pingpong mode and flushes a pending ACK; the stack **re-enters** pingpong mode the next time this socket sends within `ato` of a receive (`tcp_event_data_sent`) — which is precisely what a request/response server does. That is the mechanism behind Ch. 55's "not sticky" warning: the option is not a mode, it is a nudge. The route attribute `quickack 1` (`ip route change … quickack 1`) *is* a mode — it disables delayed ACKs for every socket on that route.
- **Slow start & congestion control.** TCP begins each connection with a small **congestion window (cwnd)** — `initcwnd`, 10 segments by default (RFC 6928) — and *ramps up*: slow start doubles cwnd per RTT until it reaches `ssthresh`, then grows it linearly (congestion avoidance). After an idle period longer than the RTO, `tcp_slow_start_after_idle` (default on) **resets cwnd to `initcwnd`** (`tcp_cwnd_restart`), so a connection that was quiet between trades sends the next burst through a cold window (Ch. 50's cold-start problem, in the transport). One internals detail with practical weight: that idle restart is applied only to controllers that use the kernel's generic cwnd logic — **CUBIC** (the default) and Reno are reset, **BBR** (which installs its own `cong_control` hook) is exempt (§56.3 shows both). On a clean, dedicated colo link that never truly congests, a loss-based controller's *back-off* on a spurious event is latency the link didn't earn; BBR's model-based rate estimate reacts differently. Which is right for *your* link is empirical (§56.4.2).
- **Loss recovery on a link that doesn't lose.** SACK (RFC 2018) and **RACK-TLP** (`tcp_recovery=1`, the default) recover most losses from the ACK stream within about an RTT — *when there is other traffic in flight to generate ACKs*. A lone order with nothing behind it has no such help: the **tail-loss probe** fires after `2 × SRTT` *plus* the RTO floor when only one segment is in flight (it has to wait out the peer's delayed-ACK window), and the **RTO** itself is clamped to a minimum of **200 ms** (`TCP_RTO_MIN`), whatever the measured RTT — on a 4 µs loopback or a 30 µs colo link alike. Per-route `rto_min` (`ip route change … rto_min 5ms`) lowers both. The practical point: on the order path a single lost segment costs *hundreds of milliseconds* by default, so the link must simply not lose (Ch. 65, 69), and the recovery timers should be sized to the link you actually have, not to the internet.
- **Pacing.** Since Linux 4.13 the stack paces every flow (`sk_pacing_rate`, internal pacing or the `fq` qdisc): instead of releasing a cwnd's worth of segments back-to-back, it spreads them across the RTT (at 200 % of the estimated rate in slow start, `tcp_pacing_ss_ratio`, 120 % after, `tcp_pacing_ca_ratio`). BBR *requires* pacing. For bulk transfers this smooths bursts and avoids queue build-up; for a burst of orders you want on the wire *now*, a pacing timer is a latency you never asked for — `SO_MAX_PACING_RATE` and the pacing ratios are the knobs, and `tcpi_pacing_rate` the readout.
- **Segmentation offloads (GRO/GSO/TSO/LRO).** To save CPU, the stack and NIC **batch** segments. On transmit, **TSO** hands one large buffer to the NIC to segment in hardware; **GSO** is the software fallback that carries the large `skb` through the stack and segments it just before the driver (the CPU still does the work). On receive, **GRO** merges full-size segments *that arrived in the same NAPI poll* and hands them up together at the end of that poll — bounded by the poll batch, and never holding a sub-MSS or `PSH` segment (so a small order reply is not delayed by it); a packet is held *across* polls only with hardware **LRO**, or when `gro_flush_timeout` + `napi_defer_hard_irqs` are set to keep the NIC in polled mode (Ch. 51, 60). The latency cost is therefore "everything in this poll is delivered when the poll ends" — microseconds of batching under load, and jitter that depends on what else arrived — not a timer. On the RX hot path even that is the wrong trade (§56.4.2).
- **Buffer autotuning, `TCP_NOTSENT_LOWAT`.** The kernel autotunes send and receive buffers (`tcp_moderate_rcvbuf`, `tcp_wmem`/`tcp_rmem`) and lets unsent data queue in the send buffer; large buffers add queuing latency (bufferbloat, Ch. 65) on the send path — the order sits behind whatever bulk was written before it. `TCP_NOTSENT_LOWAT` caps the unsent backlog.
- **`TCP_INFO` — the stack's view, from userspace.** Every mechanism above leaves a trace in `struct tcp_info` (`getsockopt(fd, IPPROTO_TCP, TCP_INFO, …)`, the same data `ss -ti` prints): `tcpi_snd_cwnd`/`tcpi_snd_ssthresh` (the window), `tcpi_rtt`/`tcpi_min_rtt` (µs), `tcpi_ato` (the delayed-ACK timeout), `tcpi_total_retrans`, `tcpi_notsent_bytes`, `tcpi_pacing_rate`/`tcpi_delivery_rate`, `tcpi_busy_time`/`tcpi_rwnd_limited`/`tcpi_sndbuf_limited` (where the connection spent its time), and `tcpi_options` (SACK, timestamps). Read it before and after the event you care about and the stack tells you what it did.

The mental model: **TCP has a dozen throughput/fairness heuristics, and on a dedicated colo link every one of them is a latency tax you want to remove** — send now (no Nagle, no autocork), ACK now (no delayed ACK — by route, not by nudge), don't ramp (warm the connection, raise `initcwnd`, pick a controller that keeps its window), don't wait 200 ms for a lost segment, don't pace a burst of orders, don't coalesce on RX, don't over-buffer — and verify each one through `TCP_INFO`.

## 56.3 Measure it: the connection's internal state across a burst and an idle gap

*Toolchain and machine: Appendix L.3 (this listing was last rebuilt and run with GCC 13.3 on Linux 6.18).*

- **The Nagle/delayed-ACK stall is Ch. 55's measurement.** Ch. 55 §55.3 runs `nodelay.cpp` in its three modes (two writes per request with Nagle on → ~40 ms on every exchange; one write → no stall; `TCP_NODELAY` → ~10-25 µs) and reads the result; it is not repeated here. What this chapter adds is the *internal* evidence: `tcpi_ato` below reads 40 000 µs on an established flow — the timer that produced that 40 ms.
- **Watch cwnd through `TCP_INFO`: fresh, after a burst, after idle.** The harness dumps the stack's own state three times — on a fresh connection, after an 8 MiB burst has grown the window and been fully acknowledged, and after a one-second idle gap (longer than the 200 ms RTO floor, so `tcp_slow_start_after_idle` is armed) followed by one small message. The congestion controller is chosen per socket with `TCP_CONGESTION`, so the same binary shows CUBIC and BBR:

```cpp
// tcpinfo.cpp — read the stack's own view of a connection: TCP_INFO on a fresh socket, after a burst, after idle.
// Build: g++ -O2 -std=c++20 tcpinfo.cpp -o tcpinfo
// Run:   ./tcpinfo cubic                                                (default sysctls, CUBIC on this socket)
//        ./tcpinfo bbr                                                  (BBR: exempt from the idle restart)
//        sysctl -w net.ipv4.tcp_slow_start_after_idle=0 && ./tcpinfo cubic   (the grown cwnd survives the idle gap)
#include <cstddef>
#include <cstdio>
#include <cstring>
#include <vector>
#include <arpa/inet.h>
#include <linux/tcp.h>       // the full Linux struct tcp_info (not <netinet/tcp.h>'s subset)
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

static void xfer(ssize_t (*op)(int, void*, size_t), int fd, void* p, size_t n) {   // full read/write
    for (size_t done = 0; done < n;) { ssize_t k = op(fd, (char*)p + done, n - done); if (k <= 0) _exit(1); done += size_t(k); }
}
static ssize_t rd(int fd, void* p, size_t n) { return read(fd, p, n); }
static ssize_t wr(int fd, void* p, size_t n) { return write(fd, p, n); }

static void dump(int fd, const char* when) {
    tcp_info ti{}; socklen_t len = sizeof ti;
    if (getsockopt(fd, IPPROTO_TCP, TCP_INFO, &ti, &len)) { std::perror("TCP_INFO"); return; }
    std::printf("%-24s cwnd=%4u  ssthresh=%-10u rtt=%5u us  min_rtt=%4u us  ato=%6u us  retrans=%u  opts=%s%s\n",
                when, ti.tcpi_snd_cwnd, ti.tcpi_snd_ssthresh, ti.tcpi_rtt, ti.tcpi_min_rtt, ti.tcpi_ato,
                ti.tcpi_total_retrans, (ti.tcpi_options & TCPI_OPT_SACK) ? "SACK " : "",
                (ti.tcpi_options & TCPI_OPT_TIMESTAMPS) ? "TS" : "");
}

int main(int argc, char** argv) {
    int ls = socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in a{}; a.sin_family = AF_INET; a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    int one = 1; setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    if (bind(ls, (sockaddr*)&a, sizeof a) || listen(ls, 1)) { std::perror("bind/listen"); return 1; }
    socklen_t al = sizeof a; getsockname(ls, (sockaddr*)&a, &al);

    constexpr size_t BULK = 8u << 20;                  // an 8 MiB burst grows cwnd well past initcwnd
    if (fork() == 0) {                                 // ---- server: sink the burst, then echo two 16-byte pings
        int c = accept(ls, nullptr, nullptr);
        std::vector<char> buf(BULK);
        xfer(rd, c, buf.data(), BULK);
        char p[16];
        for (int i = 0; i < 2; ++i) { xfer(rd, c, p, 16); xfer(wr, c, p, 16); }
        _exit(0);
    }
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (connect(fd, (sockaddr*)&a, sizeof a)) { std::perror("connect"); return 1; }
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);   // Ch. 55's knob; this chapter looks underneath it
    if (argc > 1) setsockopt(fd, IPPROTO_TCP, TCP_CONGESTION, argv[1], socklen_t(std::strlen(argv[1])));  // cubic / bbr
    dump(fd, "fresh connection");

    std::vector<char> bulk(BULK, 'x');
    xfer(wr, fd, bulk.data(), BULK);
    char ping[16] = {};
    xfer(wr, fd, ping, 16); xfer(rd, fd, ping, 16);    // one exchange: every byte of the burst is now ACKed
    dump(fd, "after 8 MiB burst");

    sleep(1);                                          // idle > RTO (200 ms minimum on Linux): the idle-reset trigger
    xfer(wr, fd, ping, 16); xfer(rd, fd, ping, 16);
    dump(fd, "after 1 s idle + 1 msg");
    wait(nullptr);
    return 0;
}
```

Representative output — reference machine (Appendix L.3), loopback, default `sysctl`s (`tcp_slow_start_after_idle=1`). Loopback's 64 KiB MTU makes each "segment" huge, so the window numbers are small; the *shape* is what carries over to a real link:

```
   $ ./tcpinfo cubic
   fresh connection         cwnd=  10  ssthresh=2147483647 rtt=   20 us  min_rtt=  20 us  ato=     0 us  retrans=0  opts=SACK TS
   after 8 MiB burst        cwnd=  19  ssthresh=16         rtt=  175 us  min_rtt=   4 us  ato= 40000 us  retrans=0  opts=SACK TS
   after 1 s idle + 1 msg   cwnd=  10  ssthresh=16         rtt=  180 us  min_rtt=   4 us  ato= 40000 us  retrans=0  opts=SACK TS
                             ^^^^ back to initcwnd: tcp_slow_start_after_idle reset the window during the quiet second
   $ ./tcpinfo bbr
   fresh connection         cwnd=  10  ssthresh=2147483647 rtt=   21 us  min_rtt=  21 us  ato=     0 us  retrans=0  opts=SACK TS
   after 8 MiB burst        cwnd=  16  ssthresh=15         rtt=   61 us  min_rtt=   4 us  ato= 40000 us  retrans=0  opts=SACK TS
   after 1 s idle + 1 msg   cwnd=  16  ssthresh=15         rtt=   66 us  min_rtt=   4 us  ato= 40000 us  retrans=0  opts=SACK TS
                             ^^^^ unchanged: BBR supplies its own cong_control and is exempt from the idle restart
   $ sysctl -w net.ipv4.tcp_slow_start_after_idle=0; ./tcpinfo cubic      -> the third line keeps cwnd=19
```

Read it: three internals are visible that no latency plot shows directly. (1) **`ato` goes from 0 to 40 000 µs** once the flow is established — the delayed-ACK timer of §56.2, the exact 40 ms Ch. 55 measured, sitting armed on a loopback connection whose RTT is 4 µs. (2) **CUBIC's cwnd collapses from 19 back to `initcwnd`=10 after one quiet second**, while `ssthresh` (16) remembers the old ceiling — the next burst of orders after a lull is metered by a cold window until slow start refills it; with `tcp_slow_start_after_idle=0` (or a warm connection, Ch. 50) the window survives. (3) **BBR keeps its window** across the same gap because the idle restart only applies to controllers using the generic cwnd code — a difference between controllers that matters more on a bursty order path than their congestion-avoidance curves do. On a real 1 500-byte-MTU link the segment counts are ~40× larger and the burst has to be sized accordingly, but the three effects are identical.

- **GRO/LRO receive latency.** Measure RX latency (packet-on-wire to `read()` returns, hardware timestamps — Ch. 60, 63) with GRO on vs off (`ethtool -K <if> gro off`, `lro off`), under *load* — a lone segment shows no difference, because GRO only merges what arrived in the same poll. GRO on: segments that share a poll are delivered together when the poll ends (latency and jitter proportional to the batch). GRO off: each segment is delivered as soon as it is processed — lower latency, more CPU. On the latency-critical RX path (feed recovery — Ch. 58, drop-copy), GRO off usually wins; measure the trade.
- **Congestion-control behavior on a clean link.** On a dedicated colo link (no real congestion), measure whether the congestion controller ever backs off spuriously (e.g. on a single reordering event misread as loss) — `tcpi_total_retrans` and `tcpi_snd_cwnd` sampled around the event tell you. CUBIC vs BBR behave differently; measure which holds a flatter window and send rate on *your* link.
- **Autocorking.** Send pairs of small writes back-to-back with `TCP_NODELAY` set and `tcp_autocorking` on vs off, with hardware TX timestamps (Ch. 60/63) on both segments; the gap between them is the autocork hold — microseconds, bounded by TX completion, and gone with the `sysctl`.

The lessons: the default stack carries a **40 ms timer**, a **cold-window reset**, a **200 ms recovery floor** and a **pacing timer** on a link whose RTT is tens of microseconds; the knobs that remove them are nearly free; and `TCP_INFO` lets you *verify* each one is gone rather than hoping. And measure the *distribution* (Ch. 1, 3), because TCP's latency problems are almost all in the tail.

## 56.4 Techniques

### 56.4.1 Send-path internals: autocorking, the ACK clock, the cold window, pacing

The socket-level knobs — `TCP_NODELAY` on both ends, `TCP_QUICKACK` as belt-and-braces, `tcp_slow_start_after_idle=0`, buffer sizing — are Ch. 55 §55.4.1; apply them first. What follows are the levers *under* them:

- **Turn off autocorking on the order path.** `sysctl -w net.ipv4.tcp_autocorking=0`. `TCP_NODELAY` disables Nagle but not the autocork hold (§56.2); a gateway that emits header-then-body or several orders back-to-back is exactly the pattern autocorking merges. The sysctl is global (one more reason the order gateway lives on its own box or namespace — Ch. 86); where you *want* coalescing on a bulk socket, use `TCP_CORK` explicitly.
- **Make quick ACKs a route property, not a socket nudge.** `TCP_QUICKACK` is undone the next time the socket replies within `ato` of a receive (§56.2). For a session whose every ACK matters — the exchange's order-entry gateway address, the retransmission server (Ch. 58) — set it on the route: `ip route change <dst> via <gw> quickack 1`. Verify with `tcpi_ato` (it should stay 0) or `ss -ti`.
- **Size the cold window and keep it.** Raise `initcwnd` on the route (`ip route change <dst> … initcwnd 40`) so the first burst after connect isn't metered by ten segments; set `tcp_slow_start_after_idle=0` (Ch. 55) so an idle connection keeps its window — or run a controller that keeps it anyway (BBR, §56.3); set `tcp_no_metrics_save=1` so a stale cached `ssthresh` from yesterday's loss doesn't cap today's window. And still warm the connection (Ch. 50): a grown window is necessary, warm caches and an established ARP/route are the rest.
- **Shrink the recovery timers to the link.** `ip route change <dst> … rto_min 5ms` lowers both the RTO floor and the tail-loss-probe wait for that route (default 200 ms — §56.2); `tcp_thin_linear_timeouts=1` keeps the RTO from doubling exponentially on a thin (few-segments-in-flight) stream like order entry. Do this only on a link you have measured to be clean (§56.3): a too-small `rto_min` on a lossy path makes spurious retransmissions.
- **Don't pace the order burst.** `setsockopt(SO_MAX_PACING_RATE)` set very high (or unlimited) on the order socket, and check `tcpi_pacing_rate`; if the box runs `fq`, know that its timer granularity is the release granularity of your segments. Pacing stays on for bulk flows — it is per-socket.
- **`TCP_NOTSENT_LOWAT`** — cap how much unsent data the kernel queues in the send buffer, reducing send-path queuing latency (bufferbloat) for a writer that produces faster than the link drains. Keeps the order at the front of a short queue, not behind buffered bulk.

### 56.4.2 Offloads, congestion control, and buffer tuning

- **Disable GRO/LRO on the latency-critical RX path** (Ch. 60). Receive coalescing delivers a poll's worth of segments together (§56.2–§56.3); on the order-reply / drop-copy / feed-recovery RX path, turn GRO off (`ethtool -K <if> gro off`, LRO off) so each segment is delivered as soon as it is processed. Keep it on for bulk/throughput paths where CPU matters more than latency — decide per interface/path (as with DDIO, Ch. 61, the same latency-vs-efficiency trade).
- **TSO/GSO on transmit — usually keep, but know the trade.** Transmit segmentation offload saves CPU and is generally fine for latency (the NIC segments a large buffer), but for a single tiny order there's nothing to segment — TSO neither helps nor hurts the one-order case. Keep it on for throughput paths; it's not the latency problem GRO is.
- **Choose the congestion control for a clean link — and know BBR's own taxes.** On a dedicated colo link that doesn't truly congest, no controller matters *until a loss occurs*: CUBIC never backs off without one, and RACK/DSACK undo already repairs most spurious retransmits. CUBIC does reset its window after every idle gap (§56.3); **BBR** (model-based, rate-estimating) keeps its window across idle — but BBR's defining mechanism is **pacing** (it deliberately spreads a burst of orders across an RTT-scaled interval), and its **ProbeRTT** phase collapses cwnd to 4 segments for ~200 ms roughly every 10 s — both latency negatives for bursty order entry. So the order-path question is less "CUBIC or BBR" than "`rto_min`, `initcwnd`, `tcp_slow_start_after_idle=0` and pacing off" (§56.4.1), with the controller chosen by measurement on *your* link (§56.3). Set via `tcp_congestion_control` (`sysctl`) or per-socket `TCP_CONGESTION` (as `tcpinfo.cpp` does), and keep SACK on (it helps recovery without hurting the common case).
- **Buffers: enough, not more.** Size send/receive buffers (Ch. 55 §55.4.1) large enough not to stall but not so large they add queuing; if autotuning (`tcp_moderate_rcvbuf`) introduces jitter on a fixed-rate session, pin the sizes with `SO_SNDBUF`/`SO_RCVBUF` (which disables autotuning for that socket). `tcpi_rwnd_limited`/`tcpi_sndbuf_limited` tell you whether a buffer ever was the bottleneck.

### 56.4.3 Bypassing the stack where TCP itself is the tax

When even a tuned kernel TCP stack is too slow or too jittery, move TCP off the kernel:

- **Busy-poll the socket (Ch. 51).** `SO_BUSY_POLL` / `epoll` busy-polling avoids the interrupt/wakeup/context-switch latency (Ch. 43) of the default readiness path — the kernel TCP stack, but polled instead of interrupt-driven. A middle ground: keep kernel TCP's reliability, remove its scheduling latency.
- **Userspace TCP stacks (Ch. 67).** For the lowest latency, run TCP in **userspace** over kernel bypass — a DPDK-based stack, Solarflare/AMD **Onload**'s socket-compatible stack, F-Stack and the like; Ch. 67 §67.4.2 and §67.4.4 own the mechanics, the API comparison and the latency floors. This is how the tick-to-trade order path often sends TCP orders — kernel-bypass TCP, not kernel TCP — and most of this chapter's kernel knobs have an equivalent there (Onload's `EF_*` environment variables cover spinning, delayed-ACK and Nagle behavior).
- **Kernel-bypass reliable transport (Ch. 68).** Where the venue supports it, RDMA (Ch. 68) or a vendor's reliable transport can replace TCP entirely on intra-datacenter links — but for exchange order entry, userspace TCP (Onload) is the common answer because the exchange speaks TCP.
- **Know when the stack is the floor.** Measure the kernel-TCP order path (busy-polled, fully tuned) against a userspace-TCP path; the gap (often ~microseconds — Ch. 51 vs 62) tells you whether the kernel stack is your floor. If it is, and the budget demands it, bypass (Ch. 67) — the same decision as the rest of Part VIII.

## 56.5 Pitfalls & anti-patterns: autocorking, spurious recovery and offload latency

- **Stopping at `TCP_NODELAY`.** Nagle is Ch. 55's bug and `TCP_NODELAY` fixes it; it does not fix autocorking (§56.2), the cold window, the 200 ms recovery floor or pacing. A gateway that "has NODELAY everywhere" and still shows a few microseconds between back-to-back orders, or a slow first burst after a lull, is hitting the layer below — read `TCP_INFO` (§56.3) before blaming the network.
- **GRO/LRO adding hidden RX latency.** Leaving receive coalescing on for a latency-critical RX path (order replies, feed recovery — Ch. 58) — the receiver holds segments to batch them, adding variable latency (§56.3). Disable GRO/LRO on the hot RX path (§56.4.2).
- **Cold connection on the first burst (slow start).** A single order is one segment and always fits the 10-segment initial/restart window (Ch. 55's socket-tuning slow-start model) — it is the first *burst* (a mass-quote update, a cancel-all, a recovery stream) over a freshly-established or long-idle connection that is metered by a small cwnd (§56.2, §56.3), slow exactly when it matters (the open). Warm connections (Ch. 50), disable slow-start-after-idle, raise `initcwnd`, or run a controller that keeps its window (§56.4.1–§56.4.2).
- **Congestion control backing off on a clean link.** A loss-based controller (CUBIC) over-reacting to a spurious reordering on a dedicated link that never congests — throttling for no reason. Measure and choose the controller (§56.4.2); on a clean link, spurious back-off is pure loss.
- **Trusting the 200 ms floor is "only on loss".** It is — and a single lost order on a link you believed lossless then sits for a fifth of a second (§56.2). Either make the link lossless (Ch. 65, 69) *and* shrink `rto_min` to the measured link (§56.4.1), or accept that the order path's p99.99 contains an RTO.
- **Lowering `rto_min` on a path you haven't measured.** The opposite error: a 5 ms RTO floor on a route that occasionally takes 8 ms produces spurious retransmissions and a controller that keeps backing off. Measure the RTT distribution first (`tcpi_rtt`/`tcpi_min_rtt`, Ch. 3) and size the timers to its tail.
- **Over-large buffers (bufferbloat on the send side).** Huge send buffers let data queue behind the order, adding latency (§56.2, Ch. 65). Cap unsent data (`TCP_NOTSENT_LOWAT`) and size buffers deliberately (§56.4.2).
- **Tuning TCP but ignoring the scheduling path.** A perfectly-tuned TCP socket still pays interrupt/wakeup/context-switch latency (Ch. 43) on the default readiness path — busy-poll (Ch. 51) or bypass (Ch. 67) if that latency matters (§56.4.3). TCP option tuning and the delivery path are separate levers.
- **Assuming kernel TCP is the only option.** Treating the kernel stack as fixed when userspace TCP over bypass (Onload/DPDK, Ch. 67) can remove microseconds — the order path often *should* be kernel-bypass TCP (§56.4.3). Measure the kernel floor before accepting it.
- **Tuning globally and breaking other traffic.** Applying latency `sysctl`s system-wide when only the order path needs them — some settings (disabling GRO or autocorking, buffer caps, pacing limits) cost throughput for bulk traffic (capture — Ch. 89, backups). Apply per-socket / per-route / per-interface where possible (§56.4.1–§56.4.2).

## 56.6 Exercises & checklist

**Exercises**

1. **Read the stack's mind.** Run `tcpinfo.cpp` with `cubic` and `bbr`, then again with `tcp_slow_start_after_idle=0`; explain each change in `cwnd`, `ssthresh` and `ato` with the congestion-control and ACK-clock model of §56.2. Then run it against a real peer across the colo link and compare the window sizes with the loopback run of §56.3.
2. **Cold vs warm connection.** Measure the latency of a 50-order burst on a freshly-established / idle-reset connection vs a warmed one (Ch. 50) — and confirm a *single* order shows no difference; raise `initcwnd` on the route and quantify the improvement, reading `tcpi_snd_cwnd` before and after (§56.3, §56.4.1).
3. **Autocorking.** With `TCP_NODELAY` set, send header+body as two writes and hardware-timestamp both segments (Ch. 60/63) with `tcp_autocorking` on vs off; measure the hold (§56.2, §56.4.1).
4. **GRO latency.** Measure RX latency (hardware timestamps, Ch. 60, 63) with GRO on vs off on a request-reply path — show GRO adds latency/jitter and turning it off removes it (§56.3, §56.4.2).
5. **CUBIC vs BBR on a clean link.** On a dedicated link with an injected spurious reordering, compare CUBIC and BBR window stability (`tcpi_snd_cwnd`, `tcpi_total_retrans` — §56.4.2); then drop one segment with `tc netem` and time the recovery with the default and a per-route `rto_min` (§56.4.1).
6. **Kernel vs userspace TCP.** Measure a fully-tuned busy-polled kernel-TCP order round-trip vs a userspace-TCP (Onload/DPDK, Ch. 67) path; quantify the kernel-stack floor and decide whether to bypass (§56.4.3).

**Checklist — advanced TCP tuning**

- [ ] Ch. 55's knobs are applied (`TCP_NODELAY` both ends, buffers sized, `tcp_slow_start_after_idle=0`) **and** **`tcp_autocorking=0`** on the order-path box; quick ACKs are a **route attribute** (`quickack 1`) for the sessions that need them (§56.4.1).
- [ ] The cold window is handled: **`initcwnd`** raised on the route, `tcp_no_metrics_save=1`, connections **warmed** (Ch. 50), and the **congestion controller** chosen by measurement on the actual link — knowing CUBIC resets after idle and BBR does not (§56.3, §56.4.2).
- [ ] Recovery timers match the link: **`rto_min`** per route and `tcp_thin_linear_timeouts` where the RTT distribution justifies it; the link itself is kept lossless (Ch. 65, 69) (§56.4.1, §56.5).
- [ ] **Pacing** is unlimited on the order socket and left on for bulk (§56.4.1); send-path queuing is bounded (**`TCP_NOTSENT_LOWAT`**, deliberate buffer sizing) — no bufferbloat (§56.4.2, §56.5).
- [ ] **GRO/LRO disabled on the latency-critical RX path** (kept on for bulk/throughput paths) (§56.4.2).
- [ ] Every one of the above is **verified through `TCP_INFO`/`ss -ti`** (`ato`, `cwnd`, `pacing_rate`, `retrans`), not assumed (§56.3).
- [ ] The **delivery path** is addressed too — **busy-poll** (Ch. 51) or **userspace TCP over bypass** (Onload/DPDK, Ch. 67) where kernel scheduling latency matters (§56.4.3).
- [ ] Latency settings are applied **per-socket / per-route / per-interface** where possible, not globally breaking bulk traffic (§56.5); the **kernel-TCP floor is measured** against a bypass path, and the bypass decision (Ch. 67) is made on data (§56.4.3).

## 56.7 References

- *TCP/IP Illustrated* (Stevens/Fall) and the relevant RFCs — Nagle (RFC 896), delayed ACK (RFC 1122), slow start / congestion avoidance (RFC 5681), initial window (RFC 6928), SACK (RFC 2018), RACK-TLP (RFC 8985), CUBIC (RFC 9438, obsoleting RFC 8312), BBR — the mechanisms of §56.2.
- The Linux kernel networking documentation (`Documentation/networking/ip-sysctl.rst` — `tcp_autocorking`, `tcp_slow_start_after_idle`, `tcp_no_metrics_save`, `tcp_recovery`, `tcp_pacing_*_ratio`, `tcp_thin_linear_timeouts`), `tcp(7)` / `socket(7)` (`TCP_NODELAY`, `TCP_QUICKACK`, `TCP_NOTSENT_LOWAT`, `TCP_CONGESTION`, `TCP_INFO`, `SO_MAX_PACING_RATE`) and `ip-route(8)` (`initcwnd`, `rto_min`, `quickack`) — the knobs of §56.4; Appendix C.
- The kernel sources behind §56.2 — `net/ipv4/tcp.c` (`tcp_push`/`tcp_should_autocork`), `tcp_output.c` (`tcp_cwnd_restart`, `tcp_event_data_sent`, `tcp_schedule_loss_probe`), `tcp_input.c` (`tcp_incr_quickack`, delayed-ACK scheduling) and `include/net/tcp.h` (`TCP_RTO_MIN`, `TCP_DELACK_MIN/MAX`, `TCP_ATO_MIN`).
- `ethtool` documentation — GRO/GSO/TSO/LRO offload control (§56.4.2; Ch. 60); `ss(8)` — the `-ti` readout of `tcp_info`.
- Solarflare **Onload** and DPDK userspace-TCP documentation — kernel-bypass TCP for the order path (§56.4.3; Ch. 67).
- Ch. 51 (native I/O / busy-poll), Ch. 55 (socket/TCP tuning — the knobs this chapter explains), Ch. 50 (warming), Ch. 60 (offloads), Ch. 67 (bypass), Ch. 58 (the recovery channel this transports).

## 56.8 Additional Reading

- Eric Dumazet's LWN and netdev writeups on TCP autocorking, TCP Small Queues and internal pacing — the send-path internals of §56.2.
- BBR vs CUBIC analyses and the datacenter-TCP (DCTCP) literature — congestion control on controlled links (§56.4.2); the RACK-TLP paper and RFC 8985 — why a lone segment's loss is expensive (§56.2).
- Onload / kernel-bypass TCP performance studies — the userspace-TCP order path (§56.4.3).
- Ch. 55 (*Socket Optimization*) — the knobs and the 40 ms measurement; Ch. 58 (*Reliable Multicast*) — the recovery channels that ride this TCP; Ch. 70 (*Transport Beyond TCP*) — when tuning isn't enough and TCP's *shape* is wrong: Aeron, eRPC, Homa, and custom reliable UDP; Ch. 67 (*Kernel Bypass*) — userspace TCP; Ch. 50 (*Warming*) — warm connections; **Appendix C** — the system checklist these TCP `sysctl`s extend; **Appendix E** — the syscall and NIC RX rows (the 40 ms stall and the 200 ms recovery floor are Ch. 55's and this chapter's own figures); **Appendix F** — the Nagle / delayed-ACK / GRO glossary entries.

---

*Next: Ch. 57 — Zero-Copy Wire Handling & Market-Data Decoding, opening Part IX and the heart of the feed handler: parsing ITCH/OUCH/FIX/SBE/FAST off the wire without copies, branch-free integer/decimal parsing, and A/B feed arbitration with sequence-gap detection.*
