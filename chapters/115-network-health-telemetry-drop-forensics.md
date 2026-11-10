# Part XIII — Observability & Operations in Production

# Chapter 115 — Network Health Telemetry & Drop Forensics

> **Prerequisites:** Ch. 63 (the kernel receive path — ring, NAPI, softirq, socket), Ch. 68 (socket buffers, `TCP_NODELAY`, the UDP knobs), Ch. 70 §70.2.4 and §70.4.4 (RX rings, microbursts, "detect drops at every layer"), Ch. 74 (membership — the drop that produces no counter), Ch. 75 and Ch. 77 (NIC queues, RSS, steering), Ch. 80 (the fabric), Ch. 82 (bypass stacks and the tooling they take away), Ch. 84 (PFC/ECN on lossless fabrics), Ch. 87 (PCIe — the path behind "FIFO" errors), Ch. 100 and Ch. 114 (the scraper, the invariant alert class, the sampler's footprint).
>
> **Leads into:** Ch. 120 (the case study — a drop is the first thing its incident procedure rules out), Appendix M (the feed and link gates read these counters), Appendix I.8 (the one-liners). Four chapters say "monitor `ethtool -S` and alert"; this one says what each counter means, which layer dropped the packet, how to find the exact line of kernel code that freed it, and how to sample all of it without touching the hot core.

---

## 115.1 Why it matters: a silent drop at the open is a wrong book and a lost race

Every packet that dies between the exchange's switch and your decoder dies *somewhere*, and the somewhere is the whole diagnosis. A packet dropped because the NIC's host ring was full means your poll loop was too slow for the burst — a software problem, fixed with a bigger ring, busy-polling or steering (Ch. 70, 75, 77). The same packet dropped because the NIC's *internal* FIFO overflowed means the DMA path could not drain into memory — a PCIe link that downtrained, an IOMMU, a NUMA-remote ring (Ch. 18, 87) — and no amount of ring sizing will help. Dropped in the socket buffer, it means the consumer thread fell behind (Ch. 68); dropped by `rp_filter`, it means the feed's source is unroutable (Ch. 74); dropped at the PHY with a CRC error, it means an optic is dying (Ch. 80, Appendix K). Each is a different team, a different fix and a different urgency, and they all look identical from the application: a sequence gap (Ch. 70) or, worse, nothing at all (Ch. 74). Ch. 68, 70, 75 and Appendix I.8 each tell you to "monitor `ethtool -S` and alert on drops"; none says which of the two hundred counters `ethtool -S` prints on a modern NIC means what, which ones are the *same* drop counted twice, or what to do when the counters are clean and the packets are still missing.

The second half of the problem is the **order path**, where the transport is TCP and a "drop" is not a gap but a *stall*: a retransmission timeout on the order session is a 200 ms hole (Ch. 69) during which every order you send sits in the socket buffer, and the counter that records it (`TCPTimeouts`) is one line in `/proc/net/netstat` that nobody reads. Per-socket `TCP_INFO` — the live RTT to the venue, retransmits, the time the connection spent limited by the receive window or by your own send buffer — is the venue session's health in twelve integers, available from a housekeeping thread at 1 Hz.

This chapter is the counter taxonomy the book has deferred, plus the forensic step beyond counters. §115.2 arranges the receive path's counters by **the layer that dropped** — PHY/MAC, NIC buffer and PCIe, driver/NAPI, IP, UDP, TCP, bypass stacks, application — and names the per-driver dialects (`ixgbe`/`i40e`/`ice`, `mlx5`, `sfc`), the duplicates, and the one drop no counter records. §115.3 measures the market-open replay in which each layer saturates in turn and shows which counter lights first, then the cost and footprint of the pollers themselves. §115.4 builds the sampler (the `ethtool -S` ioctl, `softnet_stat`, `nstat`'s sources, `TCP_INFO`), the **drop forensics** workflow (`dropwatch`, the `skb:kfree_skb` tracepoint with its drop reason, `perf`/`bpftrace` one-liners that name the function that freed the packet), the link and fabric signals (pause frames, FEC, optics, flaps, switch telemetry, IGMP state), and the bypass-stack counters that replace the kernel's when the kernel is not in the path; §115.5 and §115.6 close with pitfalls, exercises and the checklist that feeds Appendix M's link and feed gates.

## 115.2 Mental model: the counter hierarchy, by the layer that dropped

### 115.2.1 The receive path and its counters

A packet entering the host crosses eight places where it can be lost or delayed, and each place has its own counters, kept by a different piece of software (or firmware), exposed through a different interface:

```
   layer                  where it dies                            counter (and where)                                    what it means / who fixes it
   ─────────────────────  ───────────────────────────────────────  ───────────────────────────────────────────────────── ────────────────────────────────────────────
   1 PHY / MAC            bad symbol, CRC, runt/giant, FEC fail    ethtool -S: rx_crc_errors, rx_length_errors,            the LINK: optic, cable, FEC mode, switch
                                                                   rx_frame_errors, rx_pcs_symbol_err_phy (mlx5),          port (Ch. 80, Appendix K) — network team
                                                                   fec_corrected/uncorrectable (ice/mlx5 names vary);
                                                                   /sys/class/net/X/statistics/rx_crc_errors
   2a NIC port buffer     NIC's internal packet FIFO overflows:    rx_fifo_errors / rx_over_errors (kernel generic);        the DMA path could not drain: PCIe link
      (before the ring)   DMA to host could not keep up            mlx5 rx_discards_phy, rx_buffer_passed_thres_phy;        downtrained, IOMMU, remote NUMA ring,
                                                                   ixgbe rx_missed_errors* (see dialects); sfc              memory-bandwidth contention (Ch. 18, 76, 87)
                                                                   port_rx_nodesc_drops
   2b NIC host ring       no free RX descriptor: the host had not  rx_missed_errors (kernel generic, i40e/ice/ixgbe);       the HOST did not poll fast enough: ring
      (descriptors)       returned buffers fast enough             mlx5 rx_out_of_buffer, per-queue rxN_out_of_buffer;      depth, NAPI/busy-poll cadence, steering to
                                                                   sfc rx_nodesc_drop_cnt; per-queue rx-N.rx_dropped        a busy core (Ch. 70, 75, 77)
   3 driver / NAPI        backlog queue full; poll budget spent     /proc/net/softnet_stat col 2 dropped (netdev_max_backlog),  dropped = RPS/backlog overflow; time_squeeze
                          with packets still in the ring            col 3 time_squeeze (netdev_budget / _usecs exhausted),    = LATENCY not loss: NAPI deferred to
                                                                   col 11 flow_limit_count                                 ksoftirqd; precursor of 2b (Ch. 64)
   4 IP                   no route / not for us / rp_filter /       nstat: IpInDiscards, IpInAddrErrors, IpInHdrErrors,       config: rp_filter (Ch. 74), routes, MTU/
                          bad header / reassembly                   IpReasmFails, TcpExt:IPReversePathFilter                 fragments
   5 UDP                  socket buffer full; no socket; checksum   nstat: UdpRcvbufErrors, UdpNoPorts, UdpInCsumErrors,     RcvbufErrors = the CONSUMER is slow (Ch. 68);
                                                                   UdpInErrors (the sum); per socket ss -ump d<N>,          NoPorts = traffic with no bound socket
                                                                   /proc/net/udp drops column
   6 TCP (order session)  retransmit / RTO / receive queue drop     nstat: TcpRetransSegs, TcpExt:TCPTimeouts,               an RTO is a 200 ms+ stall on the ORDER path
                                                                   TCPSpuriousRTOs, TCPLossProbes, TCPRcvQDrop,             (Ch. 69); per socket: getsockopt(TCP_INFO) /
                                                                   TCPBacklogDrop, TCPOFODrop; ss -tim / TCP_INFO:           ss -ti — rtt, retrans, notsent_bytes,
                                                                   tcpi_rtt, tcpi_retrans, tcpi_total_retrans, tcpi_lost,   rwnd_limited, sndbuf_limited, busy_time
                                                                   tcpi_notsent_bytes, tcpi_rwnd_limited, tcpi_sndbuf_limited
   7 bypass stack         PMD ring full; mempool empty; HW discard   DPDK rte_eth_stats: imissed (= 2b), rx_nombuf (software: DPDK/Onload/ef_vi counters REPLACE 2b–5
      (kernel not in path) (Ch. 82)                                 mbufs exhausted), ierrors (= 1), q_errors[]; xstats     when the NIC leaves the kernel; the kernel's
                                                                   rx_out_of_buffer/rx_phy_discard_packets; Onload           counters read ZERO and prove nothing
                                                                   onload_stackdump stats rx_discard_*, ef_vi stats
   8 application          sequence gap (post-A/B); FEED AGE           Ch. 70's gap counters; Ch. 74's per-channel age; the      the drop that produced NO counter below:
                          (no counter anywhere below)               recorded-vs-arrived reconciliation (Ch. 100 §100.2.4)       membership lost, upstream prune, venue
```

Three facts to read the table with. **The layers are ordered, and a packet dies at the first one that cannot accept it** — so a burst that overflows the socket buffer (layer 5) while layers 1–3 are clean says "the consumer is slow", while a burst that fills the ring (2b) *and* squeezes NAPI (3) says "the kernel path is too slow for this rate" and points at busy-poll or bypass. **The same drop is often counted at two layers with different names** — a ring-full drop appears as the driver's `rx_missed_errors`, the generic `rx_dropped` (on some drivers) and the per-queue `rx-N.rx_dropped`; `UdpInErrors` includes `UdpRcvbufErrors`; do not add them. And **layer 8's "feed age" is the only signal for the drop that happens *before* layer 1** — a packet that never reached your port (membership, upstream prune, venue) moves nothing in the table, which is why Ch. 74's watchdog is part of network health and not a separate concern.

### 115.2.2 Driver dialects: the same drop, five names

`ethtool -S` prints whatever the driver author chose to export; the generic `/sys/class/net/<if>/statistics/` and `ip -s link` fields are the kernel's attempt at a common vocabulary, filled in by each driver differently. The ones that matter on a trading box:

- **Intel `ixgbe` (10 GbE)**: `rx_missed_errors` is the MAC's *missed packet count* (MPC) — the NIC's **port FIFO** overflowed because the host ring had no descriptors *or* the DMA path stalled; it does not distinguish 2a from 2b. `rx_no_buffer_count` (RNBC) is the ring-empty count — 2b specifically. `rx_fifo_errors` mirrors MPC in the generic stats. Per-queue `rx_queue_N_packets/bytes`, no per-queue drops.
- **Intel `i40e` / `ice` (25/100 GbE)**: `rx_dropped` and `port.rx_dropped` (ring full → 2b, per port); `rx_discards`/`port.rx_discards`; `rx_missed_errors` in the generic stats; per-queue `rx-N.rx_dropped` (so you can see *which* queue — and therefore which core — is slow); link-level `port.rx_crc_errors`, `port.rx_length_errors`, `port.fec_corrected_blocks`/`fec_uncorrectable_blocks` (ice).
- **NVIDIA/Mellanox `mlx5`**: the cleanest separation. `rx_out_of_buffer` (no WQE posted — the **host ring** was empty: 2b, per queue as `rxN_out_of_buffer`), `rx_discards_phy` (dropped at the **port** because the NIC's internal buffer was full: 2a — PCIe/DMA backpressure, or every queue full), `rx_buffer_passed_thres_phy` (buffer high-water, the precursor), `rx_crc_errors_phy`, `rx_symbol_err_phy`, `rx_pcs_symbol_err_phy`, `rx_corrected_bits_phy` (FEC), `rx_pause_ctrl_phy`/`tx_pause_ctrl_phy` and per-priority `rx_prioN_pause` (PFC, Ch. 84), `rx_pci_signal_integrity`, `tx_pci_signal_integrity`, `outbound_pci_stalled_rd/wr` (the PCIe path itself — Ch. 87). `ethtool -S` on `mlx5` issues **firmware commands** and takes ~1–3 ms; poll it from a housekeeping core at 1 Hz, never faster.
- **Solarflare/AMD `sfc`**: `rx_nodesc_drop_cnt` (ring empty, 2b), `port_rx_nodesc_drops` (port view of the same), `port_rx_dp_di_dropped_packets` (datapath drops), `port_rx_pause`/`port_tx_pause`, `port_rx_bad` (CRC/frame), per-queue `rx-N.rx_packets`; under Onload/ef_vi the kernel counters are irrelevant (layer 7).
- **The generic fields** (`ip -s -s link show`, `/sys/class/net/X/statistics/`): `rx_dropped` (driver-reported drops, meaning varies), `rx_missed_errors` (ring/port overflow), `rx_fifo_errors` (FIFO overflow — often *equal* to `rx_missed_errors`, the same event), `rx_over_errors` (receiver overrun), `rx_crc_errors`, `rx_length_errors`, `rx_frame_errors`, and `rx_errors` (a sum). Trust them only after reading the driver's mapping once (`drivers/net/ethernet/<vendor>/*ethtool*.c` is short and definitive).

The rule for a new NIC: *run one deliberately-overflowed burst with the consumer stopped (ring full), one with a PCIe link forced to a lower width (port FIFO), and one with a bad patch cable (CRC), and record which counters move for each.* That table — not the vendor's documentation — is what the sampler's "meaning" column should say (§115.3).

### 115.2.3 Delay is not loss, and loss is not always a counter

- **`time_squeeze` is latency.** NAPI processes up to `netdev_budget` (300) packets or `netdev_budget_usecs` (2,000 µs) per softirq round; if work remains, it yields and **defers the rest to `ksoftirqd`**, a `SCHED_OTHER` thread that runs when the scheduler allows (Ch. 59 — on a shared core, milliseconds later). The packets are not lost — the ring still holds them — but every one of them is late, and if the ring fills before `ksoftirqd` runs, `time_squeeze` turns into `rx_missed_errors`. A rising `time_squeeze` on the market-data path is the warning that precedes the first drop, and the reason a kernel-path feed handler busy-polls (Ch. 63 §63.4.2) or leaves the kernel (Ch. 82).
- **Pause frames move the drop.** With Ethernet flow control enabled (`ethtool -a`), a NIC whose buffers fill sends **PAUSE** (`tx_pause`), and the switch stops transmitting to you — the packets queue in the *switch* instead of dropping in your NIC. Your counters stay clean while the switch's egress queue, and then its other ports (head-of-line blocking, Ch. 84), absorb your slowness. `tx_pause` increments are therefore a *your-host-is-slow* signal disguised as health; `rx_pause` means the switch is telling *you* to stop. Market-data hosts usually run flow control **off** so that drops happen where they can be seen; lossless RDMA fabrics (Ch. 84) run PFC *on* by design and monitor it as a congestion signal.
- **The kernel-bypass blind spot.** When the NIC is owned by DPDK, or the socket by Onload, layers 2b–5 are not in the kernel: `/proc/net/*`, `ss`, `tcpdump` and the generic statistics read zero and *prove nothing* (Ch. 82 §82.5). The counters exist — `rte_eth_stats_get`/`rte_eth_xstats_get`, `onload_stackdump stats`, `ef_vi` stats — and the sampler must read them from the bypass stack's own API, on a housekeeping core, with the same layer tags.
- **The drop with no counter.** A lost IGMP membership, an upstream prune, a venue outage, a cable moved to the wrong switch port: nothing arrives, nothing is counted. Feed age (Ch. 74) and the recorded-vs-arrived reconciliation (Ch. 100 §100.2.4) are the only detectors, and they belong on the same dashboard as `rx_missed_errors`.

The model: **drops have an address — the layer that could not accept the packet — and each address has a counter with a driver-specific name, a meaning, and an owner; the layers fill in order, so the first counter to move names the bottleneck; `time_squeeze` and pause frames are delay signals that precede loss; bypass stacks carry their own counters; and the drop that moves nothing is detected by time.**

## 115.3 Measure it: the open, layer by layer; and the cost of looking

*Toolchain and machine: Appendix L.3.*

**The market-open replay.** Replay a captured open (Ch. 109) at increasing speed into a kernel-path feed handler on the reference box (Intel E810 `ice` driver, 4096-descriptor RX ring, the handler pinned to isolated CPU 5 with the NIC queue steered to it, `SO_RCVBUF` 32 MiB, `netdev_budget` default), with the §115.4.1 sampler on CPU 0, and record which counters move at each rate. Representative:

```
   replay rate         consumer state             first counters to move (per second)                           app sees
   ───────────────────  ─────────────────────────  ────────────────────────────────────────────────────────────  ──────────────────────────────
   1× (real time)       keeps up                   none                                                           flat p99.9
   3×                   keeps up, NAPI strained    softnet time_squeeze +40/s on CPU 5 (NAPI budget exhausted)     p99.9 +8 µs — delay, no gaps
   5×                   decode falls behind        Udp:RcvbufErrors +12k/s                                         gaps (post-A/B) begin: the SOCKET
                                                                                                                  overflowed; NIC and ring clean
   8×                   ksoftirqd cannot keep up   rx-5.rx_dropped +31k/s, rx_missed_errors +31k/s (same drops),    gaps; the RING overflowed before
                                                   time_squeeze +900/s; Udp:RcvbufErrors stops rising             packets reached the socket
   8×, PCIe forced x4   DMA path too slow          port.rx_dropped/rx_fifo_errors +; rx_missed_errors flat;         gaps with a QUIET host — the
   (link downtrained)                              ice: port.rx_discards; mlx5 would show rx_discards_phy          PCIe path (Ch. 87, 114)
   8×, busy-poll on     keeps up (no NAPI)         none until 12×: then rx_missed_errors only (ring), no squeeze    flat to 12× — the kernel softirq
   (Ch. 63 §63.4.2)                                                                                                 path was the bottleneck
   8×, bad patch cable  —                          rx_crc_errors +; FEC uncorrectable +; kernel/stack counters 0    gaps + A/B skew (one line's CRCs)
```

Each row is a different fix, and the counters tell them apart before the application can: the socket fills first when the *consumer* is slow (fix: Ch. 68/70 — a faster decoder, a bigger buffer buys time, not throughput); the ring fills when the *kernel's* receive path is slow (fix: busy-poll, steering, bypass — Ch. 63, 77, 82); the port FIFO fills when the *DMA path* is slow (fix: the PCIe slot, the IOMMU, the NUMA node — Ch. 18, 87); the PHY counts when the *link* is bad (fix: the optic). `time_squeeze` precedes the ring-full row at every rate — it is the early warning, and the reason the sampler tags it as delay rather than loss.

**The cost of looking.** Every poller runs code somewhere, and three of them can touch the data path:

```
   poller (1 Hz, housekeeping CPU 0)              cost per sample                      footprint on the data path / hot core
   ethtool -S via SIOCETHTOOL (ice, 400 counters)  ~180 µs; takes rtnl_lock             none on the hot core; rtnl_lock serialises other
   ethtool -S (mlx5: firmware query)               ~1.5–3 ms                            netlink config ops (irrelevant mid-session)
   /sys/class/net/X/statistics/* (10 files)        ~40 µs                               none (cached values the driver updates itself)
   /proc/net/softnet_stat                          ~20 µs                               none (per-CPU counters, read remotely)
   /proc/net/snmp + /proc/net/netstat (nstat)      ~110 µs                              none (SNMP MIB counters are per-CPU sums)
   getsockopt(TCP_INFO) on the order socket        ~2 µs                                takes the socket lock for ~100–200 ns: a send()
                                                                                        on the hot thread at that instant waits that long
   ss -tim (inet_diag netlink) for all sockets     ~300 µs for 50 sockets               the same brief socket lock per socket
   dropwatch / skb:kfree_skb tracepoint, enabled   ~0 while nothing drops; ~300 ns per  ARMING it is a text_poke cross-call to every CPU
                                                   DROPPED skb (the drop path only)      (Ch. 61 §61.5): arm before the session
   perf record -e skb:kfree_skb -a (sampling)      per-drop record + buffer flushes      -a includes the hot CPUs: use -C <housekeeping>
```

The first five rows are free on the hot core — counters the kernel already maintains, read from a housekeeping CPU, with the one caveat that `ethtool -S` on a firmware-backed NIC is milliseconds of *housekeeping* time and must not be polled at 100 Hz. `TCP_INFO` is the one poller that can touch the hot thread: its socket-lock hold is ~150 ns and a `send()` racing it waits that long — at 1 Hz, an acceptable, measured, once-a-second ~150 ns tail contribution; at 1 kHz, a design error. The tracepoint rows are the §115.4.2 forensics: free while armed, expensive to *arm*, which is why they are armed before the open and left on.

## 115.4 Techniques

### 115.4.1 The sampler: every counter, by layer, from a housekeeping core

The sampler reads all of §115.2.1's kernel-side layers once a second — `ethtool -S` through the same `SIOCETHTOOL` ioctl the tool uses (so the per-driver names come straight from the driver), the generic statistics, link flaps, `softnet_stat` per CPU, and the SNMP/netstat MIBs — diffs, and tags each delta with its layer and meaning:

```cpp
// netwatch.cpp — the network-health sampler: once a second, from a housekeeping core, read every counter in the receive
// path by LAYER — NIC/driver (ethtool -S via the SIOCETHTOOL ioctl, /sys/class/net statistics, carrier changes), NAPI
// (/proc/net/softnet_stat per CPU), IP/UDP/TCP (/proc/net/snmp, /proc/net/netstat) — diff against the previous sample,
// and print each delta tagged with the layer that dropped and what it means.  (Ch. 115 §115.4.1)
// Build: g++ -O2 -std=c++23 netwatch.cpp -o netwatch   (GCC 13/14; Clang 18 same)
// Run:   taskset -c 0 ./netwatch eth1 [--cpus 2-7] [--period-ms 1000]
#include <algorithm>
#include <charconv>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <linux/ethtool.h>
#include <linux/sockios.h>
#include <map>
#include <net/if.h>
#include <regex>
#include <sstream>
#include <string>
#include <string_view>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>
#include <vector>

namespace {
using u64 = std::uint64_t;
std::string slurp(const std::string& p) { std::ifstream f(p); std::stringstream ss; ss << f.rdbuf(); return ss.str(); }
u64 to_u64(std::string_view s) { u64 v = 0; std::from_chars(s.data(), s.data() + s.size(), v); return v; }
std::vector<int> parse_list(std::string_view s) {
    std::vector<int> out;
    while (!s.empty()) { auto c = s.find(','); auto tok = s.substr(0, c); s = c == s.npos ? "" : s.substr(c + 1);
        int a = 0, b = 0; auto d = tok.find('-'); std::from_chars(tok.data(), tok.data() + (d == tok.npos ? tok.size() : d), a);
        if (d == tok.npos) b = a; else std::from_chars(tok.data() + d + 1, tok.data() + tok.size(), b);
        for (int i = a; i <= b; ++i) out.push_back(i); }
    return out;
}

// --- layer 1-2: the driver's own counters, exactly what `ethtool -S` prints. The ioctl runs DRIVER code (and on some
// NICs a firmware query) under rtnl_lock — fine from a housekeeping core at 1 Hz, never from a hot thread (§115.3).
struct Ethtool {
    int fd; ifreq ifr{}; std::vector<std::string> names;
    explicit Ethtool(const char* ifname) : fd(::socket(AF_INET, SOCK_DGRAM, 0)) {
        std::strncpy(ifr.ifr_name, ifname, IFNAMSIZ - 1);
        ethtool_sset_info info{}; info.cmd = ETHTOOL_GSSET_INFO; info.sset_mask = 1ull << ETH_SS_STATS;
        ifr.ifr_data = reinterpret_cast<char*>(&info);
        if (ioctl(fd, SIOCETHTOOL, &ifr) < 0 || !(info.sset_mask & (1ull << ETH_SS_STATS))) return;
        const unsigned n = info.data[0];
        std::vector<char> buf(sizeof(ethtool_gstrings) + n * ETH_GSTRING_LEN);
        auto* gs = reinterpret_cast<ethtool_gstrings*>(buf.data()); gs->cmd = ETHTOOL_GSTRINGS; gs->string_set = ETH_SS_STATS; gs->len = n;
        ifr.ifr_data = buf.data();
        if (ioctl(fd, SIOCETHTOOL, &ifr) < 0) return;
        for (unsigned i = 0; i < n; ++i) names.emplace_back(reinterpret_cast<char*>(gs->data) + i * ETH_GSTRING_LEN);
    }
    std::vector<u64> read() {
        std::vector<char> buf(sizeof(ethtool_stats) + names.size() * sizeof(u64));
        auto* st = reinterpret_cast<ethtool_stats*>(buf.data()); st->cmd = ETHTOOL_GSTATS; st->n_stats = names.size();
        ifr.ifr_data = buf.data();
        if (ioctl(fd, SIOCETHTOOL, &ifr) < 0) return {};
        return {st->data, st->data + names.size()};
    }
};
// --- layer 3: /proc/net/softnet_stat, one hex row per CPU: processed dropped time_squeeze ... received_rps flow_limit backlog_len cpu
struct Softnet { u64 processed = 0, dropped = 0, time_squeeze = 0, flow_limit = 0, backlog = 0; };
std::vector<Softnet> softnet() {
    std::vector<Softnet> v; std::istringstream in(slurp("/proc/net/softnet_stat")); std::string line;
    while (std::getline(in, line)) { std::istringstream ls(line); std::vector<u64> f; std::string t;
        while (ls >> t) { u64 x = 0; std::from_chars(t.data(), t.data() + t.size(), x, 16); f.push_back(x); }
        if (f.size() >= 3) v.push_back({f[0], f[1], f[2], f.size() > 10 ? f[10] : 0, f.size() > 11 ? f[11] : 0}); }
    return v;
}
// --- layers 4-7: /proc/net/snmp and /proc/net/netstat ("Proto: names" / "Proto: values" line pairs)
std::map<std::string, u64> snmp() {
    std::map<std::string, u64> m;
    for (const char* path : {"/proc/net/snmp", "/proc/net/netstat"}) {
        std::istringstream in(slurp(path)); std::string h, v;
        while (std::getline(in, h) && std::getline(in, v)) {
            std::istringstream hs(h), vs(v); std::string proto, name, val; hs >> proto; vs >> proto;
            while (hs >> name && vs >> val) m[proto + name] = to_u64(val);         // e.g. "Udp:RcvbufErrors", "TcpExt:TCPTimeouts"
        }
    }
    return m;
}
const std::regex kInteresting("drop|miss|err|fifo|discard|pause|fec|out_of_buffer|nodesc|nombuf|overrun|squeeze|crc|symbol|flap|link_down", std::regex::icase);
}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) { std::fprintf(stderr, "usage: %s <ifname> [--cpus 2-7] [--period-ms N]\n", argv[0]); return 2; }
    std::vector<int> cpus; int period_ms = 1000;
    for (int i = 2; i + 1 < argc; i += 2) { if (!std::strcmp(argv[i], "--cpus")) cpus = parse_list(argv[i + 1]); if (!std::strcmp(argv[i], "--period-ms")) period_ms = int(to_u64(argv[i + 1])); }
    const std::string ifn = argv[1], sysstat = "/sys/class/net/" + ifn + "/statistics/";
    static const char* kernel_stats[] = {"rx_dropped", "rx_missed_errors", "rx_fifo_errors", "rx_over_errors", "rx_crc_errors", "rx_length_errors", "rx_frame_errors", "rx_errors", "tx_dropped", "tx_errors"};
    static const char* stack_keys[] = {"Ip:InDiscards", "Ip:InAddrErrors", "Ip:InHdrErrors", "Udp:InErrors", "Udp:RcvbufErrors", "Udp:NoPorts", "Udp:InCsumErrors",
                                       "Tcp:RetransSegs", "Tcp:InErrs", "TcpExt:TCPTimeouts", "TcpExt:TCPSpuriousRTOs", "TcpExt:TCPLossProbes", "TcpExt:TCPRcvQDrop",
                                       "TcpExt:TCPBacklogDrop", "TcpExt:TCPOFODrop", "TcpExt:IPReversePathFilter", "TcpExt:TCPMemoryPressures"};
    Ethtool et(ifn.c_str());
    std::vector<unsigned> watch; for (unsigned i = 0; i < et.names.size(); ++i) if (std::regex_search(et.names[i], kInteresting)) watch.push_back(i);
    auto et_prev = et.read(); auto sn_prev = softnet(); auto st_prev = snmp();
    std::map<std::string, u64> ks_prev; for (auto k : kernel_stats) ks_prev[k] = to_u64(slurp(sysstat + k));
    u64 carrier_prev = to_u64(slurp("/sys/class/net/" + ifn + "/carrier_changes"));
    std::setvbuf(stdout, nullptr, _IOLBF, 0);
    std::printf("%s: %zu ethtool counters (%zu watched), %zu softnet rows\n", ifn.c_str(), et.names.size(), watch.size(), sn_prev.size());
    for (int n = 1;; ++n) {
        usleep(period_ms * 1000);
        auto etn = et.read();
        for (unsigned i : watch) if (i < etn.size() && etn[i] != et_prev[i]) std::printf("[%d] NIC/driver  %-40s +%llu\n", n, et.names[i].c_str(), (unsigned long long)(etn[i] - et_prev[i]));
        et_prev = std::move(etn);
        for (auto k : kernel_stats) { const u64 v = to_u64(slurp(sysstat + k)); if (v != ks_prev[k]) {
            const char* why = !std::strcmp(k, "rx_missed_errors") ? "<- host RING FULL: poll/NAPI too slow (software side)"
                            : !std::strcmp(k, "rx_fifo_errors") || !std::strcmp(k, "rx_over_errors") ? "<- NIC FIFO/PCIe backpressure (Ch. 87): the DMA path, not the host"
                            : !std::strcmp(k, "rx_crc_errors") || !std::strcmp(k, "rx_frame_errors") || !std::strcmp(k, "rx_length_errors") ? "<- LINK: cable/optic/FEC (§115.4.4)" : "";
            std::printf("[%d] NIC/kernel  %-40s +%llu %s\n", n, k, (unsigned long long)(v - ks_prev[k]), why); ks_prev[k] = v; } }
        const u64 cc = to_u64(slurp("/sys/class/net/" + ifn + "/carrier_changes")); if (cc != carrier_prev) { std::printf("[%d] LINK FLAP: carrier_changes +%llu\n", n, (unsigned long long)(cc - carrier_prev)); carrier_prev = cc; }
        auto sn = softnet();
        for (size_t c = 0; c < sn.size() && c < sn_prev.size(); ++c) {
            const bool hot = std::find(cpus.begin(), cpus.end(), int(c)) != cpus.end();
            if (sn[c].dropped != sn_prev[c].dropped) std::printf("[%d] NAPI cpu%zu  softnet dropped      +%llu <- backlog queue overflow (netdev_max_backlog)%s\n", n, c, (unsigned long long)(sn[c].dropped - sn_prev[c].dropped), hot ? "  ON A HOT CPU" : "");
            if (sn[c].time_squeeze != sn_prev[c].time_squeeze) std::printf("[%d] NAPI cpu%zu  time_squeeze         +%llu <- poll ran out of netdev_budget(_usecs): deferred to ksoftirqd%s\n", n, c, (unsigned long long)(sn[c].time_squeeze - sn_prev[c].time_squeeze), hot ? "  ON A HOT CPU" : "");
            if (hot && sn[c].processed != sn_prev[c].processed) std::printf("[%d] NAPI cpu%zu  processed            +%llu <- the KERNEL stack is receiving on an isolated CPU (steering? Ch. 77)\n", n, c, (unsigned long long)(sn[c].processed - sn_prev[c].processed));
        }
        sn_prev = std::move(sn);
        auto st = snmp();
        for (auto k : stack_keys) { auto a = st.find(k), b = st_prev.find(k); if (a != st.end() && b != st_prev.end() && a->second != b->second) {
            const char* why = !std::strcmp(k, "Udp:RcvbufErrors") ? "<- SOCKET buffer full: the consumer is slow (SO_RCVBUF, Ch. 68)"
                            : !std::strcmp(k, "TcpExt:TCPTimeouts") ? "<- RTO fired on a TCP session: a 200 ms+ stall on the ORDER path (Ch. 69)"
                            : !std::strcmp(k, "TcpExt:IPReversePathFilter") ? "<- rp_filter dropping the feed source (Ch. 74)"
                            : !std::strcmp(k, "Udp:NoPorts") ? "<- traffic for a port no socket is bound to (a join without a socket?)" : "";
            std::printf("[%d] STACK       %-40s +%llu %s\n", n, k, (unsigned long long)(a->second - b->second), why); } }
        st_prev = std::move(st);
        if (n % 10 == 0) std::printf("[%d] ok\n", n);
    }
}
```

Built with GCC 13.3 and Clang 18.1. On the authoring host (a VM with a `virtio_net` interface — 20 ethtool counters, 6 of them matching the watch list) a burst of 20,000 1 kB datagrams into a socket with a 2 KiB `SO_RCVBUF`, plus 50 datagrams to an unbound port, produced in the first second:

```
   [1] STACK       Udp:InErrors        +19999
   [1] STACK       Udp:RcvbufErrors    +19999 <- SOCKET buffer full: the consumer is slow (SO_RCVBUF, Ch. 68)
   [1] STACK       Udp:NoPorts         +50    <- traffic for a port no socket is bound to (a join without a socket?)
```

— layer 5, with layers 1–3 silent, which is the diagnosis: the consumer, not the NIC. In production the `printf`s are stores into a `NetStats` block of the Ch. 100 segment (per NIC: the watched `ethtool` counters by *your* layer mapping from §115.2.2, the generic stats, carrier changes; per CPU: `softnet` rows; host-wide: the MIB keys), exported beside the application's gap counters so "`rx_missed_errors +31k`" and "post-A/B gaps +400" share a timestamp — and the "any nonzero delta" alert class (Ch. 100 §100.4.4) applies to every drop counter in the table, with the meaning column attached to the ticket. Two additions the listing omits for length: the **per-socket `TCP_INFO`** poll for the order sessions (below), and the **bypass-stack counters** (§115.4.5) when the kernel is not in the path.

**`TCP_INFO` for the order session.** From the gateway's housekeeping thread, once a second per venue socket:

```cpp
tcp_info ti{}; socklen_t len = sizeof ti;
if (getsockopt(fd, IPPROTO_TCP, TCP_INFO, &ti, &len) == 0) {            // ~2 µs; holds the socket lock ~150 ns (§115.3)
    publish(venue, "rtt_us", ti.tcpi_rtt, "rttvar_us", ti.tcpi_rttvar,     // the venue round-trip the kernel measures
            "retrans", ti.tcpi_retrans, "total_retrans", ti.tcpi_total_retrans, "lost", ti.tcpi_lost,
            "notsent_bytes", ti.tcpi_notsent_bytes,                        // > 0 with an idle app = data STUCK behind cwnd/rwnd
            "rwnd_limited_us", ti.tcpi_rwnd_limited, "sndbuf_limited_us", ti.tcpi_sndbuf_limited, "busy_us", ti.tcpi_busy_time,
            "snd_cwnd", ti.tcpi_snd_cwnd, "reordering", ti.tcpi_reordering, "delivery_rate", ti.tcpi_delivery_rate);
}
```

`tcpi_rtt` is the live round-trip to the venue — its drift is the first sign of a congested path or a re-routed cross-connect (Appendix K); `tcpi_total_retrans` moving on an order session is one lost order message and one RTO-class stall; `tcpi_notsent_bytes > 0` while the strategy is idle means the kernel is holding data it cannot send (congestion window collapsed after idle — Ch. 68 §68.2's slow-start-after-idle, or the venue's receive window closed — `tcpi_rwnd_limited` rising); `tcpi_sndbuf_limited` rising means *your* buffer is the cap. The same fields are what `ss -tim` prints; the `getsockopt` is cheaper and avoids walking every socket on the host.

### 115.4.2 Drop forensics: naming the function that freed the packet

Counters say *which layer*; sometimes you need *which line*. Every packet the kernel discards is freed through `kfree_skb_reason()`, which fires the `skb:kfree_skb` tracepoint with the socket buffer's address, the location (the function that dropped it) and, since Linux 5.17, an enumerated **drop reason** (`SKB_DROP_REASON_*`: `SOCKET_RCVBUFF`, `IP_RPFILTER`, `UDP_CSUM`, `NO_SOCKET`, `TCP_OFO_DROP`, `NETFILTER_DROP`, `DEV_READY`, ... — `/sys/kernel/tracing/events/skb/kfree_skb/format` lists them). Three ways to read it, in increasing precision:

```bash
# 1. dropwatch: a per-location histogram of kernel drops, live (kas = kernel address symbolised)
sudo dropwatch -l kas            # then "start"; prints "N drops at udp_queue_rcv_one_skb+2a3 (0xffff...) [software]" per second
# 2. the tracepoint, with the reason, through perf — housekeeping CPUs only; arm BEFORE the session (text_poke IPIs, Ch. 61)
sudo perf record -C 0-1 -e skb:kfree_skb -g -- sleep 60 && sudo perf script | head -40
sudo perf trace --no-syscalls -C 0-1 -e 'skb:kfree_skb' --filter 'reason != 2'   # 2 = SKB_DROP_REASON_NOT_SPECIFIED noise varies by kernel
# 3. bpftrace: count by reason and by the dropping function, with the stack when you need it (Ch. 81)
sudo bpftrace -e 'tracepoint:skb:kfree_skb { @[args->reason, ksym(args->location)] = count(); }
                  interval:s:1 { print(@); clear(@); }'
sudo bpftrace -e 'tracepoint:skb:kfree_skb /args->reason == 7/ { @[kstack] = count(); }'   # e.g. SOCKET_RCVBUFF: who was receiving?
# the kernel's own per-reason counter view (6.x): drop_monitor via netlink, summarised by `nstat -az` and `perf trace` as above
```

Reading the output against §115.2.1: `udp_queue_rcv_one_skb` + `SOCKET_RCVBUFF` is layer 5 (the consumer); `ip_rcv_finish_core` + `IP_RPFILTER` is Ch. 74's `rp_filter`; `__netif_receive_skb_core` + `NO_SOCKET`/`NETFILTER_DROP` is a firewall or a missing listener; `tcp_data_queue` + `TCP_OFO_DROP`/`TCP_RCVQ_DROP` is the order session's receive queue overflowing (the gateway is not reading — Ch. 103's liveness). Drops that never reach `kfree_skb` — the NIC ring (2b) and port FIFO (2a) — are *hardware* drops and appear only in `ethtool -S`; that is the dividing line between the counter layer and the tracepoint layer, and both are needed. The tracepoint costs nothing while packets are not being dropped (the hook is only on the drop path) and ~300 ns per dropped skb when they are; **enabling** it patches kernel text and IPIs every CPU, so it is armed before the open and left armed (Ch. 61 §61.5), with `-C <housekeeping>` so the perf buffers never live on a hot core. XDP (Ch. 81 §81.4.2) adds its own layer *before* the stack: an `XDP_DROP` program's drops are counted by the program (a BPF map) and by `ethtool -S` `rx_xdp_drop` on drivers that export it — not by `kfree_skb`, because no skb was ever allocated.

### 115.4.3 Fabric and membership signals the host can see

The host sees the fabric only through what arrives, but three fabric conditions leave host-side traces that belong in the same sampler:

- **Multicast membership and the querier.** Ch. 74 §74.4.3's three views, sampled: the group count per feed interface in `/proc/net/igmp` (must equal the configuration), **seconds since the last general query** on each feed VLAN (a packet-capture-derived gauge: a pinned `tcpdump`/AF_PACKET reader on a housekeeping core matching `igmp[0] == 0x11`, or a tiny eBPF program counting them — Ch. 81), and `IPReversePathFilter`. The query-age gauge is the only *leading* indicator of the ~260 s dark feed; alert at 2 × Query Interval, long before the feed stops.
- **Per-line arrival skew and silence.** Ch. 70's A/B arbitration statistics — how often A wins, the A−B arrival-time distribution, per-line packet counts — are *fabric* signals: a line whose skew grows by 20 µs over an hour has a path change (a re-routed cross-connect, a switch failing over — Appendix K); a line that goes silent while its twin lives is a membership or port fault on that path (Ch. 74 §74.4.4). They come from the application (Ch. 100 §100.4.2) and are read beside the NIC counters.
- **Pause frames as a fabric message.** `rx_pause`/`rx_prioN_pause` (the switch asking *you* to stop) means the switch's egress buffer toward you is full — a congested uplink, an oversubscribed replication point (Ch. 80 §80.4.2) — and is the one case where a host counter reports a *switch* condition directly.

### 115.4.4 Link, PHY, optics and switch telemetry

Below the packet layer the link has its own health, and its degradation is gradual — which means it is predictable if watched:

- **Error and FEC counters.** `rx_crc_errors`, `rx_length_errors`, `rx_frame_errors`; `ethtool --show-fec <if>` for the FEC mode (RS-FEC at 25/100 GbE); and the per-driver FEC counters (`fec_corrected_blocks`/`fec_uncorrectable_blocks` on `ice`, `rx_corrected_bits_phy`/`rx_err_lane_N_phy`/`rx_pcs_symbol_err_phy` on `mlx5`). Corrected FEC blocks are free individually and *rising* as an optic ages; uncorrectable blocks are CRC errors by another name. Trend the corrected rate; alert on the first uncorrectable one.
- **Optics DOM.** `ethtool -m <if>` reads the transceiver's digital diagnostics: RX/TX optical power, laser bias, temperature, voltage, with the vendor's alarm and warning thresholds. RX power drifting toward the warning threshold is a cleaning or a replacement, scheduled rather than suffered. The read is an I²C transaction through the NIC firmware (slow — tens of ms; once a minute, housekeeping core).
- **Link flaps and speed.** `/sys/class/net/<if>/carrier_changes` (and `carrier_up_count`/`carrier_down_count`), `speed`, `duplex`, `ethtool <if>` for the negotiated speed — a 100 GbE link that re-negotiated to 25 GbE after a flap is a fabric event that shows up as `time_squeeze` under load. The sampler reports every carrier change; one is an incident at 09:29.
- **Switch-side telemetry.** The counters you cannot see from the host — per-port egress discards, queue depth and microburst detection (Arista LANZ, Cisco Nexus queue-drop telemetry, Broadcom/Mellanox buffer statistics), PFC pause counts and ECN marks per queue (Ch. 84 §84.3), IGMP snooping tables (Ch. 74), and in-band network telemetry (INT) where deployed — are pulled by the network team's collector (SNMP, gNMI/streaming telemetry, the vendor API) and should land in the *same* time-series store as the host counters, with the same timestamps (PTP — Ch. 78). An egress discard on the switch port facing you is a drop that *your* counters will never show: it is layer 0 of §115.2.1's table.

### 115.4.5 When the kernel is not in the path: bypass-stack counters

Under kernel bypass (Ch. 82) layers 2b–5 of the table move into the user-space stack, and so must the sampler:

- **DPDK.** `rte_eth_stats_get()` per port: `ipackets`/`ibytes`, **`imissed`** (the NIC dropped because the PMD's RX ring was full — layer 2b, hardware-counted), **`rx_nombuf`** (the PMD could not refill the ring because the mbuf mempool was empty — a *software* exhaustion: the consumer holds too many buffers, Ch. 31), `ierrors` (malformed/CRC — layer 1), `q_errors[]` per queue; and `rte_eth_xstats_get()` for the driver's full `ethtool -S`-equivalent list (`rx_out_of_buffer`, `rx_phy_discard_packets`, FEC and pause counters on `mlx5`/`ice` PMDs). The sampler is a housekeeping lcore calling these at 1 Hz and writing the Ch. 100 segment; the data-plane lcores are never touched (the stats are read from device registers or a PMD-maintained per-queue struct — check which: a register read is a PCIe round-trip on the *housekeeping* core, Ch. 87, fine at 1 Hz).
- **Onload.** `onload_stackdump stats` / `onload_stackdump lots` for every stack: `rx_discard_*` (by reason — `csum_bad`, `mcast_mismatch`, `crc_bad`, `trunc`, `rights`, `inner_csum_bad`), `rx_no_buf`/`rx_nodesc` (ring/buffer exhaustion), `rx_drop_*`, per-socket `rx_drops`; `onload_stackdump` reads shared memory the stack maintains and costs nothing on the data path.
- **ef_vi / exanic.** `ef_vi_stats_query()` (`rx_discard_*` by class) and the adapter-level `ethtool -S` still works for the *port* counters (layers 1–2a) because the kernel still owns the netdev; `exanic-config <dev>` for ExaNIC port counters and hardware drops.
- **The invariant.** Whatever the stack, the sampler's output keeps the §115.2.1 layer tags — "ring full (2b)", "mempool empty (software)", "PHY" — so an incident reads the same on a kernel-path box and a bypass box, and the kernel counters on a bypass box are reported as *not applicable* rather than as zero.

## 115.5 Pitfalls & anti-patterns

- **"Monitor `ethtool -S` and alert on drops" without the layer.** Two hundred counters, several of them the same event, alerting as a lump. Map every watched counter to its layer and owner once (§115.2.2's three-fault experiment), and alert with the meaning attached.
- **Adding counters that count the same drop.** `rx_missed_errors` + `rx_fifo_errors` + `rx_dropped` + per-queue `rx-N.rx_dropped` summed into one "drops" metric triples the number and hides which layer it was. Pick one per layer; keep the others as corroboration.
- **Mistaking `time_squeeze` for loss, or ignoring it as "just latency".** It is latency — on the market-data path, latency *is* the loss — and it is the precursor of the ring-full drop (§115.3). Alert on its first appearance during the session; fix with busy-poll/steering before the drops start.
- **Trusting zero counters on a bypass box.** `/proc/net/snmp` and `ss` report nothing because nothing goes through them. Read the bypass stack's counters (§115.4.5), and make the dashboard say "N/A", not "0".
- **Polling drivers fast.** `ethtool -S` on `mlx5` is a firmware command (ms); at 100 Hz it is 20 % of a housekeeping core and a queue of `rtnl_lock` holders. 1 Hz, housekeeping core.
- **`TCP_INFO`/`ss -ti` at high rate on the order socket.** Each poll holds the socket lock ~150 ns; a `send()` racing it waits. Once a second is a measured, bounded cost; a 1 kHz "live RTT" display is a self-inflicted tail (§115.3).
- **Arming tracepoints during the session.** `perf record -e skb:kfree_skb`, `dropwatch start`, a `bpftrace` probe — each patches kernel text and IPIs every CPU including the hot ones (Ch. 61 §61.5). Arm before the open; use `-C <housekeeping>`; leave armed.
- **Flow control on, drops hidden.** Ethernet PAUSE moves your drops into the switch's buffers where your counters cannot see them and other hosts pay for them. Off on market-data NICs (unless the fabric is designed lossless and monitored as such — Ch. 84); treat `tx_pause > 0` as "this host is too slow".
- **Reading only the host.** The switch port facing you drops in its egress queue and your counters stay clean. The network team's per-port telemetry on the same time axis is not optional.
- **Diagnosing the application first.** A tail incident investigated in the decoder for an hour while `rx_missed_errors` had been climbing since 09:30:02. The incident procedure reads the layer table top-down (link → NIC → NAPI → stack → socket → application) before anyone opens a profiler (Ch. 120).
- **Forgetting the drop with no counter.** Feed age (Ch. 74) and recorded-vs-arrived reconciliation (Ch. 100 §100.2.4) belong on the network-health dashboard, or a lost membership will be "no network problem — all counters clean".

## 115.6 Exercises & checklist

**Exercises**

1. **The three-fault experiment.** On your NIC, induce (a) ring-full drops (stop the consumer during a replayed burst), (b) port-FIFO drops (force the PCIe link to a lower width in the BIOS or with `setpci`, then burst), (c) link errors (a marginal patch cable or `ethtool --set-fec off` on a link that needs it). Record which `ethtool -S`, generic and `softnet` counters move for each, and write your driver's row of §115.2.2.
2. **The open, layer by layer.** Replay a captured open (Ch. 109) at 1×, 3×, 5×, 8× into a kernel-path handler with `netwatch` running; reproduce §115.3's progression (`time_squeeze` → `RcvbufErrors` or ring drops). Then enable busy-poll (Ch. 63) and find the new first-failing layer and rate.
3. **Forensics.** With the socket buffer deliberately small, run `dropwatch -l kas`, `perf record -C 0-1 -e skb:kfree_skb -g` and the `bpftrace` reason histogram during a burst; confirm all three name `udp_queue_rcv_one_skb`/`SOCKET_RCVBUFF`. Then set `rp_filter=1` on the feed NIC and watch the reason change to `IP_RPFILTER`.
4. **`TCP_INFO`.** Poll a venue (or test-server) session at 1 Hz; induce a lost segment with `tc netem loss 0.1%` and watch `tcpi_total_retrans`, `TCPTimeouts` and `tcpi_rtt`; then let the connection idle 2 s and send — observe `tcpi_notsent_bytes`/`tcpi_snd_cwnd` with and without `tcp_slow_start_after_idle=0` (Ch. 68).
5. **Poller footprint.** Measure `ethtool -S` latency on your NIC (`strace -T`), `TCP_INFO`'s socket-lock hold (a hot thread doing `send()` in a loop with the poller at 1 Hz, 100 Hz, 1 kHz — HdrHistogram the sends), and the cost of arming `skb:kfree_skb` (`/proc/interrupts` `CAL` on the hot cores before/after `perf record` starts). Reproduce §115.3's second table.
6. **Bypass.** On a DPDK or Onload test box, run the same burst and show the kernel counters at zero while `imissed`/`rx_nombuf` or `rx_discard_*`/`rx_no_buf` count; extend `netwatch` (or its production equivalent) to read them with the same layer tags.
7. **Querier age.** Build the "seconds since last general query" gauge (an AF_PACKET or eBPF reader on a housekeeping core); disable the lab switch's querier and show the gauge crossing 2 × Query Interval ~130 s before the feed goes dark (Ch. 74 §74.3).

**Checklist — network health**

- [ ] Every watched counter is **mapped to a layer and an owner** by *measurement* on this NIC/driver (ring full vs port FIFO vs link), and duplicates are not summed (§115.2.2, §115.5).
- [ ] A sampler on a **housekeeping core** reads at **1 Hz**: `ethtool -S` (ioctl), generic statistics, carrier changes, `softnet_stat` per CPU (flagging any receive on an isolated CPU), the IP/UDP/TCP MIBs, and **`TCP_INFO`** for each order session — and publishes into the Ch. 100 segment beside the application's gap and feed-age counters (§115.4.1).
- [ ] **`time_squeeze`** is alerted as a delay signal and precursor; **ring-full**, **port-FIFO**, **socket-buffer**, **RTO** and **link-error** deltas are "any nonzero delta" tickets with the meaning attached (§115.2.3, §115.3).
- [ ] **`skb:kfree_skb` forensics** (`dropwatch`/`perf`/`bpftrace` by reason and location) are armed **before** the session on **housekeeping CPUs only** (§115.4.2).
- [ ] **Membership and querier age**, **A/B skew and per-line silence**, and **pause frames** are on the network dashboard (§115.4.3); **FEC/CRC trends, optics DOM, flaps and negotiated speed** are sampled, and **switch-side telemetry** shares the time axis (§115.4.4).
- [ ] On bypass boxes the **stack's own counters** are read with the same layer tags and the kernel's are shown as N/A (§115.4.5).
- [ ] Flow control is **off** on market-data NICs (or deliberately on and monitored on lossless fabrics — Ch. 84); `tx_pause > 0` is treated as "this host is slow" (§115.2.3).
- [ ] The incident procedure reads the **layer table top-down** before profiling the application; the **feed-age** and **recorded-vs-arrived** gauges are checked when every counter is clean (§115.5).

## 115.7 References

- Linux kernel documentation and sources: `Documentation/networking/statistics.rst` (the generic `rtnl_link_stats64` fields and their intended meanings), `Documentation/networking/scaling.rst` (RPS/`netdev_max_backlog`, `softnet_stat`), `Documentation/admin-guide/sysctl/net.rst` (`netdev_budget`, `netdev_budget_usecs`), `include/net/dropreason-core.h` (the `SKB_DROP_REASON_*` enumeration), `net/core/drop_monitor.c`, `Documentation/networking/snmp_counter.rst` (every `/proc/net/snmp` and `netstat` counter explained), `include/uapi/linux/tcp.h` (`struct tcp_info`), `include/uapi/linux/ethtool.h` (`ETHTOOL_GSSET_INFO`/`GSTRINGS`/`GSTATS`) — the sources of §115.2.1 and §115.4.1.
- Driver statistics sources: `drivers/net/ethernet/intel/ixgbe/ixgbe_ethtool.c`, `intel/ice/ice_ethtool.c`, `mellanox/mlx5/core/en_stats.c`, `sfc/ethtool_common.c` — the definitive mapping of each driver's counter names (§115.2.2); NVIDIA's *Understanding mlx5 ethtool Counters* and Intel's *E810 ethtool statistics* notes.
- `ethtool(8)` (`-S`, `-a`, `-m`, `--show-fec`, `-g`, `-c`), `nstat(8)`, `ss(8)` (`-t -i -m`), `dropwatch(1)`, `perf-trace(1)`/`perf-record(1)` with tracepoints, the `bpftrace` reference guide (Ch. 81) — the readers of §115.4.
- DPDK Programmer's Guide (`rte_eth_stats`, `rte_eth_xstats`), AMD/Solarflare *Onload User Guide* (`onload_stackdump`), `ef_vi` API reference — the bypass counters of §115.4.5.
- Ch. 63 (the receive path), Ch. 68–75, 77 (sockets, decoding, recovery, membership, NIC, steering), Ch. 80 and Ch. 84 (fabric, PFC/ECN), Ch. 82 (bypass), Ch. 87 (PCIe), Ch. 100 and Ch. 114 (the pipeline and the sampler discipline).

## 115.8 Additional Reading

- Brendan Gregg, *Systems Performance* (2nd ed.), the networking chapter — the layer-by-layer methodology and the `netstat`/`ss`/`nstat`/tracing toolkit this chapter specialises; his *BPF Performance Tools* networking chapter for the eBPF versions.
- Packagecloud's *Monitoring and Tuning the Linux Networking Stack: Receiving Data* — the long-form walk through NAPI, `softnet_stat` and the SNMP counters.
- Arista and Cisco documentation on microburst detection and queue telemetry (LANZ, streaming telemetry), and the P4/INT literature — what the switch can tell you that the host cannot (§115.4.4).
- Ch. 120 (*Case Study*) — the incident procedure that reads this chapter's table first; Ch. 74 (*Multicast Membership*) — the drop with no counter; Appendix M — the link and feed gates; **Appendix I.8** — the one-liners; **Appendix E** — the kernel-stack vs bypass RX floor under these counters; **Appendix F** — `time_squeeze` / `rx_missed` / FEC / PAUSE / drop-reason glossary.

---

*Next: Ch. 116 — Testing Latency-Critical Code: Unit, Property-Based & Deterministic Simulation Testing: before a build may touch any of the above, the book's first test code — table-driven tests of `apply(state, input)`, property-based tests with a shrinker that cut a 200-input failure to two, a deterministic simulation that drove the gateway through 5 000 seeded fault schedules in 120 ms and found a resend bug at seed 1, golden masters cut from captures, and test doubles as policies that compile to the production instructions.*
