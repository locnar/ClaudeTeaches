# Part IX — Market Data, NIC & Fabric

# Chapter 61 — Multicast Group Management & Dark-Feed Detection

> **Prerequisites:** Ch. 57 (multicast sockets — the join call and the sysctl knobs this chapter explains), Ch. 59 (A/B arbitration and sequence-gap detection — the mechanisms that *cannot* see the failure treated here), Ch. 60 (gap recovery — what runs once a gap is detected; this chapter is logically before it), Ch. 67-preview (the fabric that replicates the feed), Appendix K (the colocation network the feed crosses), Ch. 52 (warming — a join is a warm-up step), Ch. 17 (the clock the watchdog reads), Ch. 1 (the tail — a dark feed is a tail of infinite length).
>
> **Leads into:** Ch. 62 and Ch. 64 (NIC and flow steering — the group lands in a queue), Ch. 69 (kernel bypass — who sends the IGMP report when the kernel is not in the path), Ch. 85 (telemetry — per-feed liveness age as a published gauge), Ch. 88 (kill-switch — what a dark feed triggers), Ch. 96 (network health — membership and querier state as monitored signals), Appendix M (the feed gate). Ch. 60 recovers the stream once it arrives; this chapter makes sure it arrives, on the right NIC, before the open, and that its absence is noticed within a heartbeat.

---

## 61.1 Why it matters: a lost membership produces no gap

Every market-data chapter so far has assumed the packets are *arriving*. Ch. 57 showed the join call, Ch. 59 arbitrated A against B by sequence number, Ch. 60 recovered the sequences both lines missed. All of that machinery is keyed on one signal: a **sequence discontinuity**. It is a good signal — a drop in the NIC ring, in the fabric, in your socket buffer, all show up as a gap within one packet — but it has a blind spot the size of the whole feed: **a feed that stops entirely produces no gap.** The last packet you received had the expected sequence number; the next one never comes; the book sits exactly where it was, internally consistent, perfectly stale, and every invariant Ch. 59 and Ch. 60 check is satisfied. Nothing in `ethtool -S` moves, because the NIC is not dropping anything — the packets are not reaching it. Your strategy quotes against a book that is five minutes old while the market moves, and the first indication is a fill you cannot explain.

The ways a multicast feed goes dark are *membership* failures, and they are first-week production incidents for every new feed handler. The host joined 400 groups and the kernel silently refused the 21st (`net.ipv4.igmp_max_memberships`, default 20). The switch's IGMP snooping table aged the port out because nobody on the VLAN is sending membership queries — the **querier problem** — and the feed stops, on every host at once, roughly 260 seconds after the last query, with the host still convinced it is a member (`/proc/net/igmp` lists the group). The feed's source address is not routable back out the feed NIC and `rp_filter` drops every packet before UDP sees it. A second socket bound to `INADDR_ANY` on the same port receives *another* group's traffic because `IP_MULTICAST_ALL` defaults on, and the "wrong" feed is silently decoded as the right one. The join was issued at 09:29:59 along with everyone else's and the switch control plane took four seconds to process the storm, so the first thirty seconds of the open — the most valuable thirty seconds of the day — were missing. None of these is a gap. All of them are a wrong book.

This chapter is the membership layer the book has so far compressed into one bullet. §61.2 is the mechanism: IGMP/MLD reports, queries and leaves, snooping switches and the querier, how the exchange's stream reaches your port (PIM, RPF, layer-1 replication), and the four ways a feed goes dark with no gap. §61.3 measures the three numbers that matter — join-to-first-packet latency (and its distribution in the market-open join storm), time-to-dark after querier loss, and what the watchdog costs on the hot loop. §61.4 is the technique: joining correctly (SSM, interface- and group-bound sockets, A and B on disjoint NICs), the Linux traps and their sysctls, verifying membership from the host, the **dark-feed watchdog** that turns silence into a decision, and who sends the report under kernel bypass. §61.5 lists the ways it goes wrong, §61.6 the exercises and the pre-open membership checklist that Appendix M folds into the feed gate.

## 61.2 Mental model: IGMP, snooping, the querier, and the distribution tree

### 61.2.1 Reports, queries and leaves

Multicast delivery is *pull*: nothing forwards group traffic to a link until a host on that link asks. The asking is **IGMP** (IPv4 — `Internet Group Management Protocol`, v2 in RFC 2236, v3 in RFC 3376) or **MLD** (IPv6 — v1/v2, the same protocol in ICMPv6 clothing). The exchange between a host and the routers/switches on its link:

```
   host                                   link (switch, snooping)                      multicast router / querier
   ────                                   ───────────────────────                      ─────────────────────────
   setsockopt(MCAST_JOIN_*)  ──> Membership Report (v2: to the group; v3: to 224.0.0.22 with (S,G) filter)
                                           │ snooping: add this PORT to the group's forwarding entry
                                           └──────────────> router: PIM join toward source/RP if first on link
   (every Query Interval, default 125 s)   <── General Query (to 224.0.0.1) ─────────────── the QUERIER
   host answers (random delay ≤ Max Resp)  ──> Report  (keeps the snooping entry and the router state alive)
   last socket leaves        ──> Leave (v2, to 224.0.0.2) / v3 report with INCLUDE{} ──> group-specific query, then prune
```

- **A report is the host's claim; the query is the fabric's lease renewal.** Membership state in switches and routers is *soft*: it expires unless refreshed by a report, and hosts only re-report when queried (plus a couple of unsolicited reports at join time — Linux sends them `igmp_unsolicited_report_interval` apart, 1 s by default). No query → no refresh → the lease expires. The expiry is the **Group Membership Interval** = Robustness × Query Interval + Query Response Interval = 2 × 125 s + 10 s = **260 s** with RFC defaults — the number behind "the feed stopped four minutes after the network change".
- **IGMPv3 adds source filtering** — a report says *which sources* the host wants for a group (`INCLUDE{S}` / `EXCLUDE{}`), which is what makes **source-specific multicast (SSM)** work: a join to (S,G) in `232.0.0.0/8` tells every device on the path exactly one source to forward, no rendezvous point, no chance of receiving a rogue sender on the same group. Exchanges that publish feed source addresses (most do) are SSM-capable whether or not they call it that; the socket API is `MCAST_JOIN_SOURCE_GROUP` (RFC 3678).
- **Versions must agree along the path.** A v3 host on a VLAN with a v2 querier *falls back* to v2 (and starts a timer — `/proc/net/igmp`'s `Querier` column shows `V2` instead of `V3`), which silently discards your source filters; a v2 switch seeing v3 reports may not parse them at all. `net.ipv4.conf.<dev>.force_igmp_version` pins the host side; the switch is configured by the network team; they have to match.

### 61.2.2 Snooping switches and the querier problem

An L2 switch has no business reading IP, but if it floods every multicast frame to every port, a 10 Gb/s market-data VLAN melts every host on it. So switches **snoop**: they parse the IGMP conversation and build a per-group list of member ports, forwarding group traffic only to those ports plus any **mrouter port** (where a querier or PIM router lives — those get everything). Three consequences define production behaviour:

- **Snooping needs a querier.** The snooping table is refreshed by the hosts' *answers to queries*. In a classic routed network the PIM router on the VLAN queries. In a flat colo market-data VLAN with no router — the feed arrives on the VLAN from the venue's distribution switch, and no device is routing — **nobody queries unless you configure the switch's snooping querier** (`ip igmp snooping querier` on Cisco-style gear). Without it, every member port ages out ~260 s after its last unsolicited report. The feed runs for four minutes after each join, then goes dark on every host at once. The host is still "joined" (the kernel never heard anything; `/proc/net/igmp` is unchanged), there is no gap (the last packet was fine) and no drop counter (the frames stopped arriving at the NIC). Only the clock knows.
- **Unknown-multicast flooding is a policy, not a law.** Frames for a group with *no* snooped members are, by default, flooded to all ports (or only to mrouter ports, depending on the switch and its "unknown multicast" setting). This is why a mis-joined feed sometimes *works anyway* in the lab (one switch, flooding) and fails in production (a different switch, flood disabled, or a second hop). Never infer correct membership from receiving packets.
- **Two lines, two trees.** The A and B feeds should reach you through *disjoint* switches and NICs (Ch. 59, 67, Appendix K): a snooping failure, a querier outage or a replication-point overload then darkens one line while the other lives — and *the two lines disagreeing about silence* becomes your fastest membership-health signal (§61.4.4).

### 61.2.3 From the exchange to your port: PIM, RPF and layer-1 replication

Upstream of your switch the venue's distribution network is routed multicast — **PIM-SM** (RFC 7761) for (*,G) groups via a rendezvous point, **PIM-SSM** for (S,G) — with **reverse-path forwarding (RPF)** checks at every router: a packet is forwarded only if it arrived on the interface the router would use to reach its *source*. Two facts matter to the host: the venue's source addresses must be *routable* from your side for RPF to pass (asymmetric routes drop feeds silently in the network, and `rp_filter` does the same thing on your box — §61.4.2), and a first-on-link join that has to build a PIM tree upstream is *slower* than a join to a group already flowing on your switch (tens to hundreds of ms vs sub-ms — §61.3). In a colo the last hop is often not a switch at all but a **layer-1 replicator** (Ch. 67 §67.4.1): a physical-layer fan-out that copies the venue's signal to N ports with no membership protocol — there *is* no join, the feed is always "on", and the membership problems of this chapter vanish along with the ability to subscribe selectively. Know which kind of port you are plugged into; the dark-feed signatures differ.

### 61.2.4 The four ways a feed goes dark without a gap

```
   failure                               host sees                                 host does NOT see
   ────────────────────────────────────  ────────────────────────────────────────  ─────────────────────────────────
   join refused / never sent             setsockopt ENOBUFS (max_memberships),     — the app may ignore the error;
   (sysctl limit, wrong NIC, version)    no entry in /proc/net/igmp for that dev   nothing arrives, ever
   snooping entry aged out (no querier)  entry STILL in /proc/net/igmp;            no gap, no ethtool drop, no
                                         no general queries in `tcpdump igmp`      socket error: silence after ~260 s
   packets dropped before UDP            nstat IPReversePathFilter / InAddrErrors  no gap (never had a sequence),
   (rp_filter, wrong-VLAN, no route)     incrementing; /proc/net/igmp fine         no socket drop counter
   upstream prune / venue outage / A-only  A dark, B live (or both dark + venue      — the only one Ch. 59/60 half-see:
   path failure                          status page)                               B fills A's "gap" forever
```

The model: **membership is a soft lease renewed by queries you do not send; the switch's view of it is invisible to the host; and every membership failure presents as *silence*, which no sequence-based mechanism can detect. The host can observe exactly three things — its own join state (`/proc/net/igmp`), whether queries are arriving (`tcpdump igmp`), and the age of the last packet per group — and the third one, compared against the venue's heartbeat cadence and against the other line, is the dark-feed detector.**

## 61.3 Measure it: join latency, time-to-dark, and the cost of the watchdog

*Toolchain and machine: Appendix L.3.*

Three measurements, each answering an operational question — *how early must I join*, *how long until an unnoticed failure costs me*, and *does watching cost the hot path anything*:

- **Join-to-first-packet latency** — timestamp the `setsockopt` and the first `recv` per group (the §61.4.4 listing prints exactly this). It depends entirely on where the stream already is. Representative figures on a colo market-data VLAN (venue feed arriving on a snooping ToR with a configured querier; host on the reference box, kernel stack), 400 groups joined sequentially, per-group distribution:

| Situation | Join → first packet | Why |
|---|---|---|
| Group already flowing on the switch (another host, or an mrouter port) | **~0.3–2 ms** (p99 ~3 ms) | snooping adds the port on report receipt; the next packet is yours |
| Group not yet on the switch; PIM join upstream | **~20–300 ms** | the tree is built hop by hop toward the source/RP; venue-side timers |
| Layer-1 replicated port | **0** — the stream was always there | no membership protocol at all (§61.2.3) |
| Market-open join storm (30 hosts × 400 groups at 09:29:58) | **p50 ~0.4 s, p99 ~4 s, worst ~9 s** | the switch's IGMP control plane is rate-limited (hundreds of reports/s); reports queue; some are dropped and re-sent 1 s later |

The last row is the one that costs money: the join storm turns a millisecond operation into seconds, at the exact moment the data is worth most. The fix is operational, not technical — **join at T-30 min and hold the memberships all day** (Appendix M) — and the measurement is what justifies it.

- **Time-to-dark after querier loss** — in the lab, join, confirm packets, then disable the switch querier (or block IGMP queries with an ACL) and time the silence. With RFC-default timers the feed stops **255–265 s** later on every host, `/proc/net/igmp` still shows the group, and the last general query seen by `tcpdump -i eth0 igmp` is ~260 s old. With the Cisco default query interval of 60 s it is ~130 s. The number to take away: *the absence of general queries is observable from the host two full intervals before the feed dies* — a leading indicator, not a post-mortem (§61.4.3).
- **The watchdog's hot-loop cost** — the §61.4.4 detector adds, per group per poll iteration, one 64-bit load, one subtract and one predicted-not-taken compare: ~1 ns, below the noise of the `recv` it rides on. Measured as a Ch. 3 microbenchmark on the reference box (GCC 13.3 `-O2`), the age check over 64 groups costs **58 ns per iteration** against **~19 µs** for the 64 non-blocking `recv` calls it is interleaved with on the kernel path (≈300 ns per empty `recv`, Ch. 45) — the watchdog is free, the polling design is the cost, and on a bypass stack (Ch. 69) where the poll is a ring read, the same check is under 1 % of the iteration. The venue's heartbeat cadence (MoldUDP64 sends a zero-message heartbeat every second on an idle channel; most binary feeds do something similar) sets the detection floor: a threshold of 2–3 heartbeat intervals detects a dark line in 2–3 s with no false positives in a quiet market, and a per-phase threshold (§61.4.4) tightens it to tens of ms during continuous trading when the channel is never idle.

Read as a distribution (Ch. 1): the join latency *median* is irrelevant — the p99 of the open join storm is what decides whether you join early, and the time-to-dark is not a distribution at all but a deterministic 260 s fuse that lights when a network engineer touches a VLAN.

## 61.4 Techniques

### 61.4.1 Joining correctly: SSM, interface-bound, group-bound, A/B on disjoint NICs

The join itself has four properties that a feed handler should get right every time, each closing one failure in §61.2.4:

- **Prefer SSM — `MCAST_JOIN_SOURCE_GROUP` with the venue's published source.** An (S,G) join removes the rendezvous point from the path (faster first-on-link joins), rejects any other sender on the group (a mis-configured test host, a second venue channel on a reused address), and is what IGMPv3 was designed for. Fall back to `MCAST_JOIN_GROUP` (any-source) only where the venue does not publish sources. Both are the protocol-independent API (`group_source_req`/`group_req`, RFC 3678) that also works for IPv6/MLD; the older `IP_ADD_MEMBERSHIP`/`ip_mreqn` is IPv4-only.
- **Name the interface in the join, and bind the socket to it.** The `gr_interface`/`gsr_interface` field (an ifindex) decides which NIC sends the report and therefore which switch port is added — a join on the *management* NIC is the classic "it works in the lab" bug (Ch. 57 §57.4.2). `SO_BINDTODEVICE` additionally stops the socket seeing the same group arriving on another interface.
- **Bind to the group address, not `INADDR_ANY`.** `bind()` to `239.x.y.z:port` restricts delivery to datagrams addressed to that group; a socket bound to `0.0.0.0:port` receives *unicast* to the port and, with `IP_MULTICAST_ALL=1` (the default), *every* group any socket on the host joined on that port (§61.4.2). Two feed channels on the same port is the normal case at several venues; cross-talk decodes B-channel instruments as A-channel ones with valid-looking sequences.
- **A and B on different NICs, joined through different switches.** The lines are only redundant if their membership state is. Join A on the NIC cabled to switch 1 and B on the NIC cabled to switch 2 (Appendix K); verify both `/proc/net/igmp` device sections list their groups. If both lines come in on one NIC, a single snooping failure darkens both and A/B arbitration (Ch. 59) has nothing to arbitrate.

### 61.4.2 The Linux traps: `igmp_max_memberships`, `IP_MULTICAST_ALL`, `rp_filter`, versions

Ch. 57 §57.4.2 listed the knobs; here is what each one *does* to a feed, and the setting:

- **`net.ipv4.igmp_max_memberships` (default 20, per socket).** The 21st `MCAST_JOIN_*` on one socket fails with `ENOBUFS`. A handler that joins 400 channels on one socket and ignores `setsockopt`'s return value gets 20 feeds and 380 silent ones. Set it to the channel count with headroom (`sysctl -w net.ipv4.igmp_max_memberships=1024`), *and* check every join's return value, *and* count joins in `/proc/net/igmp` after start-up (§61.4.3). `igmp_max_msf` (default 10) is the equivalent limit on source filters per group.
- **`IP_MULTICAST_ALL` (default 1).** Set to 0 on every market-data socket, always. With the default, a socket bound to a port receives every group joined by *any* socket on the host for that port — the cross-talk trap of §61.4.1, and a performance trap too: the kernel delivers each packet to every matching socket.
- **`net.ipv4.conf.<dev>.rp_filter` (strict = 1 on most distributions).** The feed's source (the venue's sender) is usually *not* reachable via the feed NIC in your routing table (you have a default route out the management NIC), so strict RPF drops every feed packet at IP input. The signature is `nstat -az IPReversePathFilter` incrementing with a clean `/proc/net/igmp` and clean `ethtool -S`. Set `rp_filter=0` or `2` (loose) on the feed interfaces, or add a route for the venue's source prefixes via the feed NIC (the cleaner fix — it also makes retransmission requests, Ch. 60, go out the right way). `all.rp_filter` takes the *maximum* of `all` and the interface value, so set both.
- **`force_igmp_version`.** Pin to 3 when the switches speak v3 (so your SSM filters survive); pin to 2 only when a v2-only querier is unavoidable, and know that SSM is then a host-side fiction. `/proc/net/igmp`'s `Querier` column shows what the interface is *actually* running after hearing the network.
- **`SO_REUSEPORT` / `SO_REUSEADDR`.** Two processes on one host receiving the same group:port (a handler and a capture process, Ch. 92) must both set `SO_REUSEADDR` before `bind`, or the second `bind` fails; `SO_REUSEPORT` additionally lets the kernel load-balance *unicast* across them — irrelevant for multicast, which is delivered to all matching sockets.
- **Buffers.** `SO_RCVBUF` large enough for the open (Ch. 57, 59) and `net.core.rmem_max` raised to allow it; a join that *works* and then drops in the socket buffer shows as `UdpRcvbufErrors` and *does* produce gaps — that is Ch. 59's problem, and the counter that distinguishes it from a dark feed.

### 61.4.3 Verifying membership from the host

Membership is verified by *reading state*, not by seeing packets (flooding — §61.2.2 — can deliver packets to a host that is not a member). Three views, in order of authority:

```bash
# 1. the kernel's view: every (device, group) the host believes it has joined; the Group column is little-endian hex
cat /proc/net/igmp                 # "Idx Device : Count Querier / Group Users Timer Reporter"; Querier V3 = running IGMPv3
python3 -c 'import sys;[print(".".join(str(int(l.split()[0][i:i+2],16)) for i in (6,4,2,0))) for l in sys.stdin if l.startswith("\t")]' < /proc/net/igmp
ip maddr show dev eth1             # the same, decoded, per interface (also link-layer 01:00:5e:… entries)
ss -ulpn                           # which process holds the sockets; ss -ump shows per-socket drops (d<N>) and rb
# 2. the network's view, as far as the host can see it: are general queries arriving? who is the querier?
timeout 300 tcpdump -i eth1 -nn -c 3 'igmp and igmp[0] == 0x11'     # expect one General Query per Query Interval (≤125 s)
#    none within two intervals => no querier on the VLAN => the feed WILL go dark ~260 s after the last join
# 3. the packets' view: is the feed's SOURCE acceptable to this host?
nstat -az IPReversePathFilter IpInAddrErrors UdpInErrors UdpRcvbufErrors   # rp_filter / no-route drops vs socket drops
ethtool -S eth1 | grep -iE 'multicast|mcast'                               # the NIC saw the group frames (rx_multicast rising)
```

Fold these into start-up: after joining, assert that `/proc/net/igmp` lists every expected group on the expected device, that a general query has been seen on each feed NIC within the last two intervals, and that `IPReversePathFilter` is not moving. Appendix M runs the same three checks at T-30 and T-5 as the **feed gate**, and Ch. 96 §96.4.3 samples them continuously (the "seconds since last general query" gauge is the one host-observable precursor of the querier failure).

### 61.4.4 The dark-feed watchdog and the re-join policy

The detector that closes the blind spot is a **per-group last-packet age** compared against a threshold, run from the feed handler's own poll loop; the listing joins correctly (§61.4.1), times join→first packet (§61.3), and raises *dark* with no gap and no counter:

```cpp
// mcast_watch.cpp — join market-data groups the right way (SSM where the venue publishes a source, bound to one
// NIC, no cross-talk), time join -> first packet per group, then run the dark-feed watchdog: per-group last-packet
// age against a session-phase threshold, reported through the telemetry segment (Ch. 85) and the kill-switch (Ch. 88).
// Build: g++ -O2 -std=c++23 -march=icelake-server mcast_watch.cpp -o mcast_watch   (GCC 13/14; Clang 18 same)
// Run:   ./mcast_watch eth0 239.1.1.1:30001 239.1.1.2:30002 [--source 10.0.0.5] [--dark-ms 250]
#include <arpa/inet.h>
#include <array>
#include <charconv>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <net/if.h>
#include <netinet/in.h>
#include <string_view>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#include <x86intrin.h>

namespace {
struct Group { sockaddr_in addr{}; int fd = -1; std::uint64_t joined_tsc = 0, first_tsc = 0, last_tsc = 0, packets = 0; bool dark = false; };
constexpr int kMaxGroups = 64;

int join(Group& g, const char* ifname, const char* source) {
    const int fd = ::socket(AF_INET, SOCK_DGRAM | SOCK_NONBLOCK, 0);
    const int one = 1, zero = 0, rcvbuf = 32 << 20;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);          // A and B, or two handlers, on one group:port
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof rcvbuf);       // capped by net.core.rmem_max (Ch. 57)
    setsockopt(fd, IPPROTO_IP, IP_MULTICAST_ALL, &zero, sizeof zero);    // deliver ONLY groups this socket joined (§61.4.2)
    setsockopt(fd, SOL_SOCKET, SO_BINDTODEVICE, ifname, std::strlen(ifname));   // this NIC only (feed NIC, not mgmt)
    sockaddr_in bind_addr = g.addr;                                       // bind to the GROUP address, not INADDR_ANY:
    if (::bind(fd, reinterpret_cast<sockaddr*>(&bind_addr), sizeof bind_addr) < 0) return -1;   // no stray unicast on the port
    group_source_req gsr{}; group_req gr{};
    const unsigned ifindex = if_nametoindex(ifname);
    if (source) {                                                         // SSM: (S,G) join — the switch/router forwards only
        gsr.gsr_interface = ifindex;                                      // this source; no rp_filter/IP_MULTICAST_ALL surprises
        std::memcpy(&gsr.gsr_group, &g.addr, sizeof g.addr);
        sockaddr_in s{}; s.sin_family = AF_INET; inet_pton(AF_INET, source, &s.sin_addr);
        std::memcpy(&gsr.gsr_source, &s, sizeof s);
        if (setsockopt(fd, IPPROTO_IP, MCAST_JOIN_SOURCE_GROUP, &gsr, sizeof gsr) < 0) return -1;
    } else {                                                              // ASM: (*,G) join — IGMPv2/v3 membership report
        gr.gr_interface = ifindex; std::memcpy(&gr.gr_group, &g.addr, sizeof g.addr);
        if (setsockopt(fd, IPPROTO_IP, MCAST_JOIN_GROUP, &gr, sizeof gr) < 0) return -1;   // ENOBUFS here = igmp_max_memberships
    }
    g.joined_tsc = __rdtsc(); g.fd = fd; return 0;
}
}  // namespace

int main(int argc, char** argv) {
    if (argc < 3) { std::fprintf(stderr, "usage: %s <ifname> <group:port>... [--source S] [--dark-ms N]\n", argv[0]); return 2; }
    std::setvbuf(stdout, nullptr, _IOLBF, 0);                            // line-buffered: this is the operator's console
    const char* source = nullptr; int dark_ms = 250;                      // the threshold is PER SESSION PHASE (§61.4.4)
    std::array<Group, kMaxGroups> groups{}; int n = 0;
    for (int i = 2; i < argc; ++i) {
        if (!std::strcmp(argv[i], "--source") && i + 1 < argc) { source = argv[++i]; continue; }
        if (!std::strcmp(argv[i], "--dark-ms") && i + 1 < argc) { std::from_chars(argv[i + 1], argv[i + 1] + std::strlen(argv[i + 1]), dark_ms); ++i; continue; }
        std::string_view a = argv[i]; const auto c = a.find(':'); char host[64]{}; std::memcpy(host, a.data(), std::min(c, sizeof host - 1));
        Group& g = groups[n]; g.addr.sin_family = AF_INET; inet_pton(AF_INET, host, &g.addr.sin_addr);
        int port = 0; std::from_chars(a.data() + c + 1, a.data() + a.size(), port); g.addr.sin_port = htons(port);
        if (join(g, argv[1], source) < 0) { std::perror(host); return 1; }
        ++n;
    }
    timespec a{}, b{}; clock_gettime(CLOCK_MONOTONIC, &a); const auto t0 = __rdtsc();   // TSC calibration (Ch. 17)
    do { clock_gettime(CLOCK_MONOTONIC, &b); } while ((b.tv_sec - a.tv_sec) * 1'000'000'000LL + (b.tv_nsec - a.tv_nsec) < 50'000'000);
    const double ghz = double(__rdtsc() - t0) / ((b.tv_sec - a.tv_sec) * 1e9 + (b.tv_nsec - a.tv_nsec));
    const std::uint64_t dark_cycles = std::uint64_t(dark_ms * 1e6 * ghz);

    alignas(64) char buf[2048];
    for (;;) {                                                            // the feed handler's poll loop, reduced to the watchdog
        const std::uint64_t now = __rdtsc();
        for (int i = 0; i < n; ++i) {
            Group& g = groups[i];
            while (::recv(g.fd, buf, sizeof buf, 0) > 0) {                 // drain; decode would go here (Ch. 59)
                if (!g.first_tsc) { g.first_tsc = now; std::printf("%s: join -> first packet %.1f ms\n", inet_ntoa(g.addr.sin_addr), (now - g.joined_tsc) / ghz / 1e6); }
                g.last_tsc = now; ++g.packets;
                if (g.dark) { g.dark = false; std::printf("%s: feed back after dark\n", inet_ntoa(g.addr.sin_addr)); }
            }
            const std::uint64_t ref = g.last_tsc ? g.last_tsc : g.joined_tsc;
            if (!g.dark && now - ref > dark_cycles) [[unlikely]] {          // DARK: no packet for dark_ms — no gap, no drop counter,
                g.dark = true;                                            // nothing in ethtool -S; only the clock knows (§61.2)
                std::printf("%s: DARK for %d ms (%llu packets so far) -> mark instruments stale (Ch. 60), check /proc/net/igmp\n",
                            inet_ntoa(g.addr.sin_addr), dark_ms, (unsigned long long)g.packets);
            }
        }
    }
}
```

Built and run on the authoring host (GCC 13.3 and Clang 18.1) against a loopback test sender: a group whose sender starts 300 ms after the join reports `join -> first packet 300.7 ms`; a second group with no sender reports `DARK for 500 ms (0 packets so far)`; stopping the sender trips the first group's watchdog one threshold later. The `printf`s stand in for the three actions a production handler takes:

- **Mark the group's instruments stale and stop quoting them** — Ch. 60 §60.4.3's policy, triggered by *time* instead of a gap. The strategy's stale check is the same predicted branch either way; this is why the brief calls it the market-data counterpart of Ch. 88's kill-switch.
- **Publish the age** — the per-group `now - last_tsc` is a gauge in the telemetry segment (Ch. 85 §85.4.1), so the dashboards and Appendix M's gate see "feed age" directly rather than inferring it; the dark event itself is a counter.
- **Decide by comparing lines.** A dark **and** B live → a membership/path failure on A's NIC/switch: check `/proc/net/igmp` for A's device, check queries on A's VLAN, **re-join** (leave + join forces a fresh report, which re-adds the snooping entry) and alert the network team. A **and** B dark → the venue or the distribution tree: check the venue status feed, do *not* re-join in a loop (a re-join storm is the IGMP version of Ch. 60's NAK storm), fail over to a secondary source if one exists (Ch. 60 §60.4.4), and keep instruments stale until data flows. A dark group with `packets == 0` since start-up → the join never took (sysctl limit, wrong NIC, `rp_filter`): §61.4.3's checks, in that order.
- **Set the threshold per session phase.** During continuous trading a liquid channel is never silent for more than a few ms and the threshold can be tens of ms; pre-open, at halts and in quiet after-hours the only traffic is the venue's heartbeat, and the threshold must be 2–3 heartbeat intervals (seconds). Drive the threshold from the venue's session-state messages and your own clock (Appendix J.7), not a constant — a constant tuned for the open pages all night; one tuned for the night misses a dark line by minutes at the open.

### 61.4.5 Kernel-bypass joins: who sends the report?

Under kernel bypass (Ch. 69) the kernel never sees the socket, so *something else* must send the IGMP report and answer queries, or the switch never forwards the group to your port:

- **Onload** and **ef_vi** (Solarflare/AMD): Onload sockets join through the normal `setsockopt` and Onload issues the report (and keeps the kernel's membership state coherent); raw `ef_vi` applications must join via a *kernel* socket on the same interface (or `ef_vi`'s multicast filter plus a kernel-side membership) — the filter programs the NIC, the kernel socket keeps the switch forwarding.
- **DPDK**: the PMD owns the port and the kernel has no interface on it; the application must speak IGMP itself (build and send reports, answer general queries — `rte_eth_dev_set_mc_addr_list` programs the NIC's MAC filter but tells the switch nothing), or keep the port in a bifurcated mode (e.g. `mlx5` with the kernel netdev still present) and join through a kernel socket on the netdev.
- **In every case**, §61.4.3's verification shrinks to what the bypass stack exposes: `/proc/net/igmp` is only meaningful for kernel-side joins, so the authoritative check becomes the switch's snooping table (`show ip igmp snooping groups`, via the network team or a monitoring API — Ch. 96 §96.4.4) and the dark-feed watchdog, which works identically on a ring poll.

## 61.5 Pitfalls & anti-patterns: silence is not a quiet market

- **Treating silence as a quiet market.** The cardinal error of this chapter: a feed that has delivered nothing for a minute is dark until proven quiet. The proof is the venue's heartbeat (still arriving = quiet; absent = dark) and the other line. Without a liveness check keyed on *time*, every membership failure is discovered by a fill.
- **Joining at the open.** The join storm (§61.3) is self-inflicted and predictable. Join at T-30, verify membership (§61.4.3), hold the memberships all day, and treat an *unexpected* re-join during the session as an incident.
- **Ignoring `setsockopt`'s return value.** The 21st join fails with `ENOBUFS` and the handler runs happily with 20 channels. Check every return, log every failure, and count groups in `/proc/net/igmp` against the configuration at start-up.
- **Inferring membership from received packets.** Flooding (§61.2.2) delivers groups you never joined; cross-talk (`IP_MULTICAST_ALL`) delivers groups another socket joined. The lab works; production, with snooping and a second hop, does not. Verify state, not traffic.
- **Believing `/proc/net/igmp`.** It is the *host's* belief, refreshed by nothing the host can see. A switch whose snooping entry aged out leaves the host's table intact. The host-observable truth is the arrival of general queries and the age of the last packet.
- **Both lines on one NIC or one switch.** Redundancy that shares a membership domain is one failure away from two dark lines and a perfectly healthy-looking arbitrator (Ch. 59) with nothing to arbitrate.
- **`rp_filter` left strict on the feed NIC.** Everything joins, `/proc/net/igmp` is perfect, the NIC's `rx_multicast` counter climbs, and UDP receives nothing; `nstat IPReversePathFilter` is the only counter that moves. Fix the route or loosen the filter *per interface*, and remember `all.rp_filter` is a maximum, not a default.
- **Re-join loops.** A watchdog that re-joins every threshold interval while the venue is down sends a report every few seconds per group per host — a control-plane storm on the switch that delays everyone's *real* re-join when the venue returns. Back off (Ch. 34) and escalate to safe-state (Ch. 88) instead.
- **A constant dark threshold.** Tuned for the open it pages all night on legitimately idle channels; tuned for the night it misses a dark line by minutes when it matters. Phase-aware thresholds, driven by venue session state.
- **Forgetting the leave.** A process that exits without `MCAST_LEAVE_GROUP` relies on the kernel to send the leave at socket close (it does) — but a process that *crashes with the socket in shared memory* (Ch. 88) or a bypass stack that owns the NIC may leave the switch forwarding the group to a port nobody reads, until the lease expires. Harmless to you, unkind to the VLAN; clean up in the supervisor.

## 61.6 Exercises & checklist

**Exercises**

1. **Reproduce the 260 s fuse.** On a lab VLAN with a snooping switch, join a group, confirm traffic, then disable the querier (or filter IGMP queries). Time the silence with the §61.4.4 watchdog, watch `/proc/net/igmp` stay unchanged throughout, and confirm with `tcpdump igmp` that the last general query is ~260 s old when the feed dies. Re-enable the querier; measure recovery.
   Then make the watchdog *predict* it: alert when no general query has been seen for 2 × Query Interval, before the feed goes dark.
2. **The join storm.** From 20 lab hosts (or 20 processes with distinct source NIC VLAN sub-interfaces), join 400 groups simultaneously; histogram join→first packet (HdrHistogram, Ch. 3). Repeat with the joins spread over 60 s. Quantify the p99 difference and set your T-minus join time from it.
3. **Every trap once.** On one host, break each of §61.2.4's four failures deliberately — the 21st join on one socket, strict `rp_filter` on the feed NIC, a join on the management NIC, `IP_MULTICAST_ALL=1` with two groups on one port — and for each record which of §61.4.3's three views detects it and which do not.
4. **SSM vs ASM.** Join the same group as (S,G) and as (*,G); start a second, rogue sender on the group from another host; show that only the ASM socket receives it. Then time the first-on-link join for each on a routed (PIM) lab segment.
5. **Lines disagreeing.** With A and B on separate NICs, pull A's cable. Confirm the watchdog marks A dark within one threshold, B keeps the book live, the arbitrator (Ch. 59) sees no gap, and the "A dark, B live" branch of §61.4.4 fires a re-join on A only.
6. **Bypass membership.** On an Onload or DPDK test box, bring up the bypass receiver without a kernel-side join and show the feed never arrives; add the kernel socket join (or an application IGMP report) and show the switch's snooping table gain the port.

**Checklist — multicast membership, before the open**

- [ ] Every feed channel is joined **SSM** (`MCAST_JOIN_SOURCE_GROUP`) where the venue publishes sources, **on the feed NIC's ifindex**, from a socket **bound to the group address** with **`IP_MULTICAST_ALL=0`** and `SO_BINDTODEVICE` (§61.4.1).
- [ ] **A and B** are joined on **different NICs through different switches**; both device sections of `/proc/net/igmp` list their groups (§61.4.1, §61.4.3).
- [ ] `net.ipv4.igmp_max_memberships` (and `igmp_max_msf`) exceed the channel count; **every join's return value is checked**; the group count in `/proc/net/igmp` equals the configuration (§61.4.2).
- [ ] `rp_filter` is `0`/`2` on the feed interfaces (or the venue's source prefixes are routed via them); `nstat IPReversePathFilter` is flat (§61.4.2).
- [ ] A **general query** has been seen on each feed VLAN within two Query Intervals (the switch querier is configured and alive); `force_igmp_version` matches the network (§61.2.1, §61.4.3).
- [ ] Joins are issued at **T-30** and held all day; the **join→first-packet** time is recorded per group and compared with the baseline (§61.3).
- [ ] The **dark-feed watchdog** runs in the poll loop with **phase-aware thresholds**, marks instruments stale on silence (Ch. 60), publishes per-group age to the telemetry segment (Ch. 85), compares A against B before acting, backs off on re-join, and escalates to safe-state (Ch. 88) when both lines are dark (§61.4.4).
- [ ] Under kernel bypass, **who sends the IGMP report** is written down and verified against the switch's snooping table (§61.4.5).

## 61.7 References

- RFC 2236 (IGMPv2), RFC 3376 (IGMPv3), RFC 2710 / RFC 3810 (MLDv1/v2) — the membership protocol, timers (Query Interval, Robustness, Group Membership Interval) and version interoperation (§61.2.1).
- RFC 4541, *Considerations for IGMP and MLD Snooping Switches* — snooping behaviour, the querier requirement, unknown-multicast handling (§61.2.2).
- RFC 3678, *Socket Interface Extensions for Multicast Source Filters* (`MCAST_JOIN_GROUP`, `MCAST_JOIN_SOURCE_GROUP`, `group_req`/`group_source_req`); RFC 4607 / RFC 4604 (SSM and its use of IGMPv3/MLDv2) — the SSM API and semantics (§61.4.1).
- RFC 7761 (PIM-SM), RFC 3704 (ingress filtering / RPF) — the routed distribution tree and reverse-path checks (§61.2.3, §61.4.2).
- Linux `ip(7)`, `Documentation/networking/ip-sysctl.rst` (`igmp_max_memberships`, `igmp_max_msf`, `force_igmp_version`, `rp_filter`, `igmp_unsolicited_report_interval`), `/proc/net/igmp` format, `ip-maddress(8)`, `nstat(8)` — the host-side knobs and views (§61.4.2, §61.4.3).
- Venue feed specifications — Nasdaq MoldUDP64 (heartbeats on idle channels), CME MDP 3.0 channel definitions (group/source/port per channel) — the heartbeat cadence and source addresses the watchdog and SSM joins depend on (§61.3, §61.4.1).
- Ch. 57 (the socket layer), Ch. 59–60 (arbitration and recovery — the sequence-keyed half), Ch. 67 (the switch that snoops and replicates), Ch. 69 (bypass stacks), Appendix K (the colo fabric).

## 61.8 Additional Reading

- Switch-vendor multicast configuration guides (Cisco NX-OS/IOS, Arista EOS *IGMP snooping* and *snooping querier* chapters) — the other side of the conversation, and the commands your network team runs to show the snooping table.
- Beau Williamson, *Developing IP Multicast Networks* (Cisco Press) — the routed-multicast background (PIM, RPF, RPs) behind the venue distribution tree.
- Talks and post-mortems on market-data feed handler incidents (silent feeds, join storms, querier outages) from the trading-systems community — the operational folklore this chapter formalises.
- Ch. 62 (*NIC Features*) and Ch. 64 (*Flow Steering*) — where the joined group's frames land; Ch. 85 (*Telemetry*) — the per-feed age gauge; Ch. 96 (*Network Health*) — membership and querier state as continuous signals, and the switch-side counters; Appendix M — the T-30/T-5 feed gate built on §61.4.3; **Appendix F** — IGMP / querier / snooping / SSM / dark feed glossary; **Appendix I.8** — the membership one-liners.

---

*Next: Ch. 62 — NIC Features & Offloads, the hardware under the receive path: RSS, hardware timestamping, busy-polling, checksum/segmentation offload, and Solarflare/Onload — pushing work into the card and getting the packets you have now made sure arrive to the decoder faster.*
