# Part VIII — Kernel I/O, Sockets & Zero-Copy

# Chapter 62 — Anatomy of the Linux Network Datapath: Where the Microseconds Go

> **Prerequisites:** Ch. 61 (readiness vs completion, `epoll`, busy-poll — the syscall layer this chapter looks *beneath*), Ch. 52 (syscall and context-switch cost, interrupts), Ch. 54 and Ch. 57 (IRQ affinity and isolated cores — the softirq trap here is their unnamed jitter source), Ch. 59 §59.2 (the noise catalog's `ksoftirqd` row), Ch. 8 (cache lines — an `sk_buff` is several of them), Ch. 20 (TSC timestamps for the attribution).
>
> **Leads into:** Ch. 63–64 (io_uring and the zero-copy fast path — which stages they remove), Ch. 66–67 (socket and TCP tuning — the knobs named here get their mechanism), Ch. 68 (decoding — what the application does with the bytes that arrive at the end of this path), Ch. 72–74 (NIC offloads, DDIO, flow steering — the hardware end of the same path), Ch. 76 (ETF/FQ — the qdiscs that plug into §62.2.3), Ch. 78–79 (XDP and kernel bypass — the hooks that cut the path short, and the decision to leave it entirely), Ch. 110 (network health telemetry — this chapter's counters as a continuous signal). The book quotes "5–20 µs for the kernel stack" and moves on to bypass; this chapter is for the sockets that *stay* on the kernel stack — recovery, drop copy, order replies, control plane — and for the jitter the stack inflicts on cores that never touch it.

---

## 62.1 Why it matters: the stack you did not bypass is still on the box

Every HFT system bypasses the kernel for the market-data feed and the order path (Ch. 79) and keeps the kernel stack for everything else: the retransmission and snapshot channels (Ch. 70), drop copy and order-status (Ch. 101–102), the control plane (Ch. 99), telemetry export (Ch. 96), the venue's FIX session on a less critical venue (Ch. 102). Those sockets ride a path the book has so far described in one line — "NIC IRQ → driver → IP/TCP → socket buffer → `epoll_wait` → `read()` → copy" (Ch. 61 §61.2) — and measured only end to end: ~5–6 µs for a loopback round trip, ~8 µs wire-to-application with the defaults, ~3 µs with busy-poll (Ch. 61 §61.3, Ch. 72 §72.3). Nothing has said which *stage* owns which microsecond, so when the recovery channel's p99.9 doubles after a kernel update there is no place to look.

The second reason is jitter on cores that never open a socket. The kernel stack runs most of its receive work in **softirq** context — on whichever CPU the NIC's interrupt landed, or, when the budget runs out, in that CPU's `ksoftirqd` thread — and **transmit completions** and **RPS** re-steering can land it on a CPU that merely *sent* a packet or was named in a mask. An isolated hot core that sends one UDP datagram to a monitoring process has just volunteered to run the receive path for the reply, the loopback path for the whole message, and a `NET_RX` softirq it did not ask for. Ch. 59's noise catalog has a row for `ksoftirqd/N`; this chapter is the mechanism behind the row.

§62.2 walks the receive path stage by stage with a cost column, then the softirq machinery and its traps, the transmit mirror (qdiscs, BQL, the doorbell), and the two optional taxes — netfilter/conntrack and network namespaces. §62.3 measures: a loopback echo harness that *also* reports which CPUs ran the softirqs, run on the author's VM; a measured conntrack and ruleset cost; and the per-stage attribution on the reference machine with `ftrace` and `bpftrace` anchored to the NIC's hardware timestamp. §62.4 is the technique set — attribution, keeping softirq work off the hot cores, trimming stages, netfilter hygiene, namespaces; §62.5 the pitfalls.

## 62.2 Mental model: the path, the softirq, the mirror, and the two taxes

### 62.2.1 RX stage by stage, with a cost column

```
   stage  what happens                                                     context                 ~cost (µs, kernel path, 6326)
   ─────  ──────────────────────────────────────────────────────────────── ─────────────────────── ──────────────────────────────
    1  NIC DMAs the frame into a pre-posted RX buffer, writes a            hardware                 0.6–1.0 after the wire (PCIe write
       descriptor, raises MSI-X (or not: coalescing — Ch. 72)                                       + DDIO into L3 — Ch. 73, 83)
    2  hardirq handler: ack, napi_schedule(): mark the queue's NAPI         hardirq, IRQ's CPU       0.5–1.5 (interrupt entry/exit;
       instance, raise NET_RX                                                                        + 1–150 µs if the CPU was in C-state — Ch. 58)
    3  NET_RX softirq → net_rx_action(): poll NAPI instances round-robin    softirq, same CPU        0.2–0.5 per poll round
       within netdev_budget (300 pkts) / netdev_budget_usecs (8 ms)
    4  driver poll: read descriptors, build sk_buff, (re)post buffers,       softirq                  0.3–0.8 per packet (skb alloc from
       XDP native hook runs HERE before the skb exists (Ch. 78)                                      per-CPU cache; header pull; checksum offload read)
    5  GRO: coalesce TCP segments of one flow within the poll batch          softirq                  0.1 (miss) / hold until batch end: up to
                                                                                                      the whole poll round for the first segment
    6  netif_receive_skb → generic XDP / tc ingress / RPS decision           softirq                  0.1–0.3 (+ an IPI + backlog hop if RPS)
    7  IP: netfilter PREROUTING, routing lookup (fib), netfilter INPUT,      softirq                  0.3–0.6 (empty hooks) / +0.2–0.5 with
       conntrack if any rule loaded it                                                               conntrack (§62.3 measures it)
    8  UDP/TCP: demux to the socket (hash), checksum verify (or trust        softirq                  0.2–0.4 UDP; 0.5–1.5 TCP (seq/ack,
       offload), TCP state machine                                                                    cwnd, delayed-ACK decision — Ch. 67)
    9  socket receive queue: skb enqueued, sk_data_ready() → wake the        softirq                  0.3 (enqueue) + 1–3 wake-up + switch if the
       waiter (futex/epoll) or nothing if it is busy-polling                                          reader slept (Ch. 52) — the largest item
   10  recvmsg(): copy to user, free skb, return                            process, reader's CPU    0.5–1.0 (syscall + copy of a 1 KiB message)
   ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
   wire → recvmsg return, reader busy-polling, IRQ on the same core:      ~3–4 µs      with a sleeping reader and coalescing on: ~8–20 µs
```

Four observations. The **variable** stages are 2 (a sleeping core — Ch. 58), 5 (GRO holds the first segment until the batch ends), 9 (waking a sleeper — Ch. 52) and 1 (coalescing — Ch. 72): everything that is a *fixed* cost is ~2 µs in total, and everything the book has told you to turn off is in the variable rows. **Stages 2–9 run on the CPU that took the interrupt**, not on the reader's CPU — unless they are the same, which is the point of steering the queue's IRQ to the reader's core (Ch. 54, 74) or of busy-polling, where the reader's own `recv`/`epoll_wait` drives the NAPI poll (Ch. 61 §61.4.1) and stages 3–9 run in *its* context. The `sk_buff` built at stage 4 is ~230 bytes of metadata (four cache lines) plus the data buffer; it is allocated from a per-CPU cache and freed at stage 10 on a *different* CPU, so the free is a remote-cache return (Ch. 19). And **loopback runs the entire receive path inline in the sender's `sendmsg`**, in softirq context on the sender's CPU — which is why §62.3's harness shows `NET_RX` counts on both CPUs of a ping-pong and why "send a UDP datagram to the local monitor" from a hot thread is not a 1 µs operation.

### 62.2.2 Softirqs, `ksoftirqd` and the isolated-core trap

A softirq is deferred work that runs with interrupts enabled, either **on the way out of the hardirq** that raised it (the common case: the NIC's interrupt returns through `irq_exit()` → `do_softirq()`), or **in the per-CPU `ksoftirqd/N` kernel thread** when `do_softirq()` has already spent its budget — `net_rx_action()` stops after `netdev_budget` packets or `netdev_budget_usecs` and, if work remains, increments `time_squeeze` in `/proc/net/softnet_stat` and hands the rest to `ksoftirqd`, which is an ordinary `SCHED_OTHER` thread that now *competes with the application* on that CPU. Three paths put network softirq work on a CPU that nobody meant to:

- **The IRQ landed there.** A NIC queue's MSI-X vector affinity includes the hot core (Ch. 54 §54.4.2 — managed IRQs ignore `smp_affinity`; `isolcpus=managed_irq` is the fix). Every packet on that queue runs stages 2–9 on the hot core.
- **RPS/RFS named it.** `rps_cpus` for a queue includes the hot core: the IRQ CPU hashes the flow, enqueues the skb on the hot core's backlog and sends it an **IPI** (`irq_vectors:call_function_single`), which raises `NET_RX` there. The default is off (`rps_cpus=0`); `tuned`'s `network-throughput` profile and some NIC scripts turn it on.
- **The hot thread sent something.** `sendmsg()` on a kernel socket runs the transmit path in the caller's context (§62.2.3) — and over loopback, the *receive* path too. A TX completion interrupt for that queue lands on the queue's IRQ CPU (XPS decides which TX queue a CPU uses — `xps_cpus`), where `NET_TX` softirq frees the skbs.

The signatures are Ch. 59's: `irq:softirq_entry vec=NET_RX` on the hot core, `sched_switch → ksoftirqd/N`, `/proc/softirqs` `NET_RX` column moving for that CPU, and `softnet_stat` column 3 (`time_squeeze`) incrementing on any CPU whose budget ran out. The knobs are in §62.4.2; the principle is that **network softirq work belongs on the housekeeping core that owns the NIC queue, or in the busy-polling reader's own context — never on a core by accident**. With `threadirqs` (or `PREEMPT_RT`), hardirq handlers run in per-IRQ kernel threads and softirqs in `ksoftirqd`, which can be given RT priorities and pinned — a cleaner model for a housekeeping core that handles many queues, and irrelevant to a hot core that must handle none.

### 62.2.3 The TX mirror: `sendmsg` → qdisc → BQL → doorbell → completion

```
   stage  what happens                                                            context                 ~cost (µs)
   ─────  ───────────────────────────────────────────────────────────────────── ─────────────────────── ───────────────
    1  sendmsg(): copy user data into a fresh skb (or pin pages: MSG_ZEROCOPY —   process, sender's CPU    0.5–1.0
       Ch. 64), UDP/TCP header build (TCP: Nagle/cork decision — Ch. 66)
    2  IP: routing lookup, netfilter OUTPUT/POSTROUTING, neighbour lookup —         process                  0.3–0.6; + 100s of µs to ms if the
       an ARP entry that aged out means the packet WAITS for a resolution                                  neighbour is stale (keep it permanent)
    3  qdisc enqueue/dequeue: noqueue (no queue at all — virtual devices),          process (lock: the      0.1 (noqueue / mq+pfifo_fast) to
       mq + pfifo_fast (per-TX-queue FIFO, the server default), fq / fq_codel       qdisc root lock)        1–3 (fq, fq_codel — timestamps,
       (per-flow pacing: ADDS latency by design), etf (holds until TxTime — Ch. 76)                         flow hash, red-black tree)
    4  BQL (byte queue limits, dql): dequeue only while bytes-in-flight to the      process                  ~0 while under the limit; when the ring
       NIC < limit (auto-tuned from completions) — bounds ring-buffer bloat                                   is "full" the packet waits for a completion
    5  driver ndo_start_xmit: write a TX descriptor, ring the doorbell (an MMIO     process                  0.2–0.5 + the PCIe posted write
       write — Ch. 83); the NIC DMAs the frame and puts it on the wire                                      (NIC: ~0.5–1 µs to the wire)
    6  TX completion: IRQ (or the next NAPI poll) → NET_TX softirq → free skbs,     softirq, TX IRQ's CPU    0.3–0.8 per batch; MSG_ZEROCOPY
       advance BQL, wake a socket blocked on SNDBUF                                                          notifications land here (Ch. 64)
```

The transmit path is cheaper than receive — no interrupt, no wake-up on the critical half — *if* three things hold: the neighbour entry is present (an ARP resolution on the first order after a quiet spell is a millisecond-class stall — pin it with `ip neigh replace … nud permanent`), the qdisc is `mq` with `pfifo_fast` or `noqueue` rather than a pacing qdisc (`fq`/`fq_codel` are the *desktop* default and some distributions set `net.core.default_qdisc = fq_codel`; `fq` is also what `tcp_bbr` and `SO_TXTIME`/ETF need — Ch. 76 uses it deliberately), and the BQL limit is not being hit (a saturated TX ring means the datapath is bandwidth-bound and the latency is queueing; `/sys/class/net/<if>/queues/tx-N/byte_queue_limits/{limit,inflight}` shows the state — on the author's VM's `virtio_net` queue the auto-tuned limit sits at 440 KB). The doorbell is a posted MMIO write (Ch. 83 §83.2) — cheap to issue, but the NIC's DMA read of the descriptor and the frame is the first *non-posted* half of the trip, which is why wire-out is ~1 µs after `sendmsg` returns even on a perfect path.

### 62.2.4 The two taxes: netfilter/conntrack, and namespaces

**Netfilter** is a set of hooks (PREROUTING, INPUT, FORWARD, OUTPUT, POSTROUTING, at the IP layer and again for `arp`/`bridge`) that each packet passes on the way in and out. With *no* tables loaded the hooks are empty function-pointer lists and cost nothing measurable. With *any* ruleset present (`iptables`/`nft`), each hook traverses its chains — and the moment a rule mentions connection state (`ct state`, `-m conntrack`, any NAT), the **conntrack** module is engaged on every packet: a flow-tuple hash lookup, a per-connection entry (created on the first packet, timed out later), and for UDP a "connection" that exists only as a bookkeeping entry. §62.3 measures it at ~0.25 µs per packet per hook on the author's VM — a 10 % tax on the fixed part of the path, and a cache-line footprint (the conntrack entry, its hash bucket) that competes with the application's. A box that has ever had Docker installed has an `iptables-nft` ruleset with NAT, `ct state` rules and a bridge — and therefore conntrack on every packet of every socket, hot or not.

**Network namespaces** are free in themselves (a namespace is a separate set of tables), but *getting a packet between them* is not: the usual `veth` pair is a virtual device whose transmit is the peer's receive — the whole of §62.2.1 stages 3–9 again, in softirq, on the sending CPU, plus a bridge or NAT hop if the container is on a Docker-style bridge network. Measured as an extra ~5–10 µs per crossing on a kernel-stack path, it is the single largest avoidable stage on a containerised host. The alternatives that keep a namespace but skip the second stack pass are `macvlan`/`ipvlan` (a sub-interface of the physical NIC, one stack pass) and, for real isolation, an SR-IOV virtual function handed to the namespace (its own queues, its own IRQs — Ch. 72). The hot box's rule is simpler: hot sockets live in the root namespace, and the Kubernetes question is answered by Ch. 99's process topology, not by a CNI plugin.

## 62.3 Measure it: the loopback echo with softirq attribution, a conntrack tax, and the per-stage trace

**Machine, toolchain, flags.** The echo harness and the netfilter experiment were **run on the author's 4-vCPU KVM guest** (Sapphire Rapids host, `virtio_net`, Linux 6.18, GCC 13.3 and Clang 18.1, `-O2 -std=c++20 -pthread`, `nft` 1.0.9) — loopback does not touch the NIC, so these are real numbers for the *software* stack on a VM. The per-stage attribution is **representative for the reference machine** (Appendix L.3: Xeon Gold 6326, Linux 6.18, a 25 GbE NIC with hardware RX timestamps, `ftrace` and `bpftrace` 0.21) because this guest exposes neither tracefs nor a PTP-capable NIC; the commands are given in full.

**1. The echo harness.** Two pinned threads, UDP over loopback, 48-byte payloads, RTT per message, and — the part Ch. 61 §61.3 did not have — the `NET_RX` softirq count and the `time_squeeze` column per CPU, diffed across the run, so the output says *where the stack ran*:

```cpp
// udp_rtt.cpp — the software stack's round trip with no NIC in the way: a UDP echo over loopback
// between two pinned threads, timed per message, with the NET_RX softirq counts and softnet_stat's
// time_squeeze column diffed across the run so you see WHICH CPU ran the stack and whether the
// softirq budget was ever exhausted.  Modes: block (recv sleeps), busy (SO_BUSY_POLL), spin (MSG_DONTWAIT loop).
// Build: g++ -O2 -std=c++20 -pthread -o udp_rtt udp_rtt.cpp     (clang++ 18 identical)
// Run:   sudo ./udp_rtt block 1 2 200000      # mode, client cpu, server cpu, messages
#include <algorithm>
#include <arpa/inet.h>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <netinet/in.h>
#include <pthread.h>
#include <sched.h>
#include <sstream>
#include <string>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>
#include <vector>

static void pin(int cpu) { cpu_set_t s; CPU_ZERO(&s); CPU_SET(cpu, &s); pthread_setaffinity_np(pthread_self(), sizeof s, &s); }

static std::vector<uint64_t> net_rx_per_cpu() {               // /proc/softirqs NET_RX row
    std::ifstream f("/proc/softirqs"); std::string line; std::vector<uint64_t> v;
    while (std::getline(f, line)) if (line.find("NET_RX:") != std::string::npos) {
        std::istringstream is(line.substr(line.find(':') + 1)); uint64_t x; while (is >> x) v.push_back(x); }
    return v;
}
static std::vector<uint64_t> time_squeeze_per_cpu() {         // /proc/net/softnet_stat column 3 (hex)
    std::ifstream f("/proc/net/softnet_stat"); std::string line; std::vector<uint64_t> v;
    while (std::getline(f, line)) { std::istringstream is(line); std::string a, b, c; is >> a >> b >> c; v.push_back(std::stoull(c, nullptr, 16)); }
    return v;
}
static int udp_socket(uint16_t port, int busy_poll_us) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    sockaddr_in a{}; a.sin_family = AF_INET; a.sin_port = htons(port); a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(fd, (sockaddr*)&a, sizeof a) < 0) { perror("bind"); exit(1); }
    if (busy_poll_us) setsockopt(fd, SOL_SOCKET, SO_BUSY_POLL, &busy_poll_us, sizeof busy_poll_us);   // per-socket busy_read (Ch. 61 §61.4.1)
    return fd;
}

int main(int argc, char** argv) {
    const std::string mode = argc > 1 ? argv[1] : "block";
    const int ccpu = argc > 2 ? atoi(argv[2]) : 1, scpu = argc > 3 ? atoi(argv[3]) : 2;
    const int n = argc > 4 ? atoi(argv[4]) : 200000;
    const int flags = mode == "spin" ? MSG_DONTWAIT : 0, bp = mode == "busy" ? 100 : 0;
    int sfd = udp_socket(9001, bp), cfd = udp_socket(9002, bp);
    sockaddr_in to_s{}; to_s.sin_family = AF_INET; to_s.sin_port = htons(9001); to_s.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    sockaddr_in to_c = to_s; to_c.sin_port = htons(9002);
    auto rx0 = net_rx_per_cpu(), sq0 = time_squeeze_per_cpu();
    std::thread server([&] {
        pin(scpu); char buf[64];
        for (int i = 0; i < n; ++i) {
            ssize_t r; while ((r = recv(sfd, buf, sizeof buf, flags)) < 0 && errno == EAGAIN) {}
            sendto(sfd, buf, r, 0, (sockaddr*)&to_c, sizeof to_c);
        }
    });
    pin(ccpu);
    std::vector<uint32_t> rtt(n); char msg[64] = "tick"; char buf[64];
    for (int i = 0; i < n; ++i) {
        auto t0 = std::chrono::steady_clock::now();
        sendto(cfd, msg, 48, 0, (sockaddr*)&to_s, sizeof to_s);                 // a 48-byte payload: an ITCH-sized message
        while (recv(cfd, buf, sizeof buf, flags) < 0 && errno == EAGAIN) {}
        rtt[i] = uint32_t(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - t0).count());
    }
    server.join();
    auto rx1 = net_rx_per_cpu(), sq1 = time_squeeze_per_cpu();
    std::sort(rtt.begin() + n / 10, rtt.end());                                  // drop the first 10 % as warm-up
    auto pct = [&](double p) { return rtt[n / 10 + size_t(p * (n - n / 10 - 1))] / 1e3; };
    std::printf("%-5s loopback UDP echo RTT (cpu%d <-> cpu%d, %d msgs): p50 %.2f us  p99 %.2f us  p99.9 %.2f us  max %.1f us\n",
                mode.c_str(), ccpu, scpu, n, pct(.5), pct(.99), pct(.999), rtt[n - 1] / 1e3);
    std::printf("      NET_RX softirqs during run:");
    for (size_t c = 0; c < rx0.size(); ++c) std::printf(" cpu%zu=%llu", c, (unsigned long long)(rx1[c] - rx0[c]));
    std::printf("\n      softnet time_squeeze deltas:");
    for (size_t c = 0; c < sq0.size(); ++c) std::printf(" cpu%zu=%llu", c, (unsigned long long)(sq1[c] - sq0[c]));
    std::printf("\n");
}
```

```
   # ./udp_rtt <mode> 1 2 200000   — client on cpu1, server on cpu2
   block loopback UDP echo RTT: p50 24.58 us  p99 36.30 us  p99.9 62.70 us  max 1951.1 us
         NET_RX softirqs during run: cpu0=6 cpu1=200003 cpu2=200000 cpu3=0     time_squeeze deltas: all 0
   busy  loopback UDP echo RTT: p50 24.54 us  p99 37.26 us  p99.9 85.01 us  max 1609.0 us
         NET_RX softirqs during run: cpu0=4 cpu1=200001 cpu2=200000 cpu3=0
   spin  loopback UDP echo RTT: p50  3.23 us  p99  4.29 us  p99.9 19.06 us  max  599.7 us
         NET_RX softirqs during run: cpu0=0 cpu1=200000 cpu2=200000 cpu3=0
```

Read the softirq line first: **exactly one `NET_RX` per message on each CPU** — the sender's `sendmsg` ran the receive path for its own packet, inline, on its own core (§62.2.1's loopback note), and the server's reply did the same on *its* core. Nothing was deferred to `ksoftirqd` (`time_squeeze` 0): the budget is 300 packets per round and these arrive one at a time. Then the latencies: `spin` at 3.2 µs is two `sendmsg` + two stack passes + two `recv` copies — ~0.8 µs per packet per direction for stages 7–10 on this guest, consistent with the fixed-cost rows of §62.2.1. `block` at 24.6 µs is the same work plus **two sleep/wake cycles**, each ~10 µs on a VM (a futex wake in a guest is a hypervisor-assisted IPI; on the bare-metal reference machine the same row is ~5–6 µs — Ch. 61 §61.3). And `busy` (`SO_BUSY_POLL`) equals `block` because busy-polling drives a *NAPI* poll and loopback has no NAPI instance — Ch. 61 §61.3 already warned that busy-poll is inert over `lo`; here the counters prove it. One more run that is not in the table: both threads pinned to the **same** CPU in `spin` mode did not finish in two minutes — each handoff waits a scheduler slice, so a "spin" against a peer on your own core is a ~4 ms round trip. Two spinning threads, two cores (Ch. 54).

**2. The conntrack and ruleset tax**, measured with the `spin` mode and `nft` on the same guest (which, like most distribution images, already carried an empty `iptables-nft` `filter` table — so "baseline" is *hooks present, no conntrack*):

```
   baseline (iptables-nft filter table, no rules touching ct)        p50 3.11 us   p99 4.17 us   p99.9 16.41 us
   + one rule: inet filter input  ct state established,related accept p50 3.63 us   p99 4.54 us   p99.9 18.78 us   nf_conntrack_count = 1
   + 200 non-matching rules (tcp dport N drop) in the same chain      p50 3.52 us   p99 4.32 us   p99.9 16.72 us
   table deleted                                                        p50 3.11 us   p99 4.08 us   p99.9 16.94 us
```

**Engaging conntrack cost 0.52 µs per round trip = ~0.26 µs per packet per INPUT traversal** — the tuple hash, the lookup and (once) the entry creation; the 200 extra rules cost nothing visible, because `nft`'s protocol match fails on the first byte for a UDP packet and the chain is a linear scan of cheap rejects (an `iptables` chain of 200 `-m` matches would not be so kind; a `set` lookup is O(1) either way). The lesson is the one §62.2.4 states: the per-rule cost is small, the per-*packet* conntrack cost is a fixed 5–10 % of the fast path, and it is engaged by a single rule anywhere in the ruleset. Deleting the table restored the baseline exactly.

**3. Per-stage attribution on the reference machine**, anchored to the NIC's hardware RX timestamp (`SO_TIMESTAMPING` with `SOF_TIMESTAMPING_RX_HARDWARE`, PHC synchronised to the system clock — Ch. 75) so the first column is *wire* time, not "when software noticed":

```
   # tracepoints along the path, TSC clock so the app's rdtsc lines up (Ch. 20); restrict to the NIC queue's IRQ CPU
   echo x86-tsc > /sys/kernel/tracing/trace_clock
   trace-cmd record -M 2 -C x86-tsc -e irq:irq_handler_entry -e irq:softirq_entry -e irq:softirq_exit \
       -e napi:napi_poll -e net:netif_receive_skb -e net:napi_gro_receive_entry -e skb:kfree_skb \
       -e sock:inet_sock_set_state -e syscalls:sys_enter_recvmsg -e syscalls:sys_exit_recvmsg -- sleep 10
   # or the same with bpftrace, keyed by the skb pointer so stages of ONE packet chain together:
   bpftrace -e '
     tracepoint:irq:irq_handler_entry /args->irq == 142/ { @t_irq[cpu] = nsecs; }
     tracepoint:napi:napi_poll        { @t_poll[cpu] = nsecs; }
     tracepoint:net:netif_receive_skb { @t_rx[args->skbaddr] = nsecs; @irq2rx = hist(nsecs - @t_irq[cpu]); }
     kprobe:udp_queue_rcv_one_skb     { @t_udp[arg1] = nsecs; }
     kprobe:sock_def_readable         { @rx2ready = hist(nsecs - @t_rx[((struct sock*)arg0)->sk_receive_queue.next]); }
     tracepoint:syscalls:sys_exit_recvmsg /pid == $1/ { @wake2ret = hist(nsecs - @t_ready); }'  $(pidof recovery_handler)
   # function_graph for the whole softirq, to see WHICH callee ate the time on a slow packet:
   echo function_graph > current_tracer; echo net_rx_action > set_graph_function; echo 1 > tracing_on
```

A 1 KiB recovery-channel reply on a quiet 25 GbE link, IRQ steered to the reader's housekeeping core, reader in blocking `recvmsg`, GRO and coalescing off, no netfilter (representative, median / p99 in µs):

```
   stage                                    median    p99     what moved the p99
   ────────────────────────────────────────  ──────  ──────  ─────────────────────────────────────────
   wire (hw RX stamp) → irq_handler_entry      1.1     1.6   PCIe write + MSI-X; C1 exit on the IRQ cpu
   irq_handler_entry → napi_poll               0.4     0.9   irq_exit → do_softirq; another softirq ahead
   napi_poll → netif_receive_skb               0.5     0.8   descriptor read, skb alloc, header pull
   netif_receive_skb → udp_queue_rcv_one_skb   0.6     1.1   IP + fib lookup + empty netfilter hooks
   udp enqueue → sock_def_readable             0.2     0.3
   sock_def_readable → recvmsg returns         2.9     7.4   futex wake + context switch + copy (Ch. 52)
   ──────────────────────────────────────────────────────────
   wire → application                          5.7    11.8   the Ch. 72 §72.3 "GRO/coalescing off" row
   same, reader busy-polling (epoll_wait(0) loop; NAPI poll runs in its context — Ch. 61 §61.4.1):  3.1 / 4.6
   same, with conntrack engaged by one rule:                                                        +0.3 / +0.5
   same, reader in a Docker-bridge namespace (veth + bridge + NAT):                                 +7.8 / +14
```

The table is the chapter's thesis in numbers: the *fixed* stages sum to ~2.8 µs, the wake-up is half the median and two thirds of the p99, and the two taxes add 5 % and 140 % respectively. When the recovery channel's p99.9 doubles, this is where you look — stage by stage, with the same tracepoints, in the same order.

## 62.4 Techniques

### 62.4.1 Attribution: anchor to the wire, trace the path, read the counters first

Before tracing, read the four counters that say whether the path is *healthy* — they cost nothing and are the Ch. 110 telemetry rows for this layer:

```
   grep -E 'NET_RX|NET_TX' /proc/softirqs            # per-CPU: network softirqs must be ZERO on hot cpus, flat on the rest
   cat /proc/net/softnet_stat                        # per-CPU hex: col 1 processed, col 2 dropped (backlog full), col 3 time_squeeze
   nstat -az | grep -E 'UdpRcvbufErrors|UdpInErrors|TcpExtTCPBacklogDrop|TcpExtListenDrops|IpInDiscards'   # socket-level drops
   ethtool -S eth0 | grep -iE 'drop|miss|discard|no_buf'    # NIC/driver drops (Ch. 110 §110.2)
   tc -s qdisc show dev eth0                         # backlog, drops, requeues per TX queue — a nonzero backlog is queueing latency
   cat /sys/class/net/eth0/queues/tx-*/byte_queue_limits/{limit,inflight}
```

Then trace as in §62.3 (3): **anchor** to the hardware RX timestamp (or, for TX, the hardware TX timestamp — Ch. 75 — against the application's `rdtsc` at `sendmsg`), **key by skb** so stages chain per packet, **restrict to the IRQ CPU** so the tracing cost lands where the stack already runs, and prefer tracepoints (`net:*`, `napi:*`, `irq:*`, `skb:*`, `sock:*`, `syscalls:*`) over kprobes — they are stable across kernels and cost ~100–200 ns each when enabled, which is acceptable on a housekeeping core and not on a hot one (Ch. 78 §78.3). `function_graph` is the escalation for the slow packet you have already localised to one softirq.

### 62.4.2 Keeping network softirq work off the isolated cores

```
   # 1. the NIC's queues and their vectors: on housekeeping cores only (Ch. 54 §54.4.2; managed IRQs via the cmdline)
   isolcpus=domain,managed_irq,2-7 nohz_full=2-7                   # kernel cmdline
   ethtool -L eth0 combined 2                                      # as many queues as housekeeping cores that will serve them
   cat /proc/irq/*/effective_affinity_list | sort | uniq -c         # verify: nothing on 2-7
   # 2. RPS/RFS/XPS: off for the hot cores — an RPS mask that names a hot core = IPIs + NET_RX there
   for q in /sys/class/net/eth0/queues/rx-*; do echo 0 > $q/rps_cpus; done
   echo 0 > /proc/sys/net/core/rps_sock_flow_entries
   for q in /sys/class/net/eth0/queues/tx-*; do echo 3 > $q/xps_cpus; done     # TX queues used by cpus 0-1 only
   # 3. the budget: not the knob you think — raise it only on the housekeeping core that is time_squeezing
   sysctl net.core.netdev_budget=600 net.core.netdev_budget_usecs=8000       # default 300 / 8000; watch softnet col 3
   # 4. ksoftirqd and irq threads (threadirqs): pin and prioritise on the housekeeping side
   for p in $(pgrep -f 'ksoftirqd/[01]$'); do chrt -f -p 50 $p; done           # the hot cores' ksoftirqd must simply never run
   # 5. the hot thread sends nothing on a kernel socket from the hot core — hand the bytes to a housekeeping
   #    thread over a ring (Ch. 65) and let IT call sendmsg; loopback runs the RX path in the sender's context
```

Item 5 is the one that surprises: the hot thread's telemetry `sendto()` to a local collector costs the hot core a full loopback receive pass (§62.3 (1)) — ~1.5 µs plus an `sk_buff` allocation and free, in softirq context, on the isolated core. The Ch. 96 design (telemetry over shared memory, exported by a housekeeping process) exists for this reason as much as for the syscall.

### 62.4.3 Trimming stages on the sockets that stay in the kernel

```
   ethtool -K eth0 gro off lro off                                 # stage 5: no batch hold (Ch. 66 §66.3, 67)
   ethtool -C eth0 rx-usecs 0 rx-frames 1 adaptive-rx off tx-usecs 0   # stage 1: an IRQ per packet (Ch. 72)
   sysctl net.core.default_qdisc=pfifo_fast                        # stage TX-3: no pacing qdisc by default (fq only where Ch. 76 needs it)
   tc qdisc replace dev eth0 root mq                               # per-queue pfifo_fast under mq; check with tc -s qdisc
   ip neigh replace 10.1.2.3 lladdr aa:bb:cc:dd:ee:ff dev eth0 nud permanent   # stage TX-2: never ARP on the order path
   sysctl net.ipv4.udp_rmem_min=65536 net.core.rmem_default=4194304     # stage 9: a drop (UdpRcvbufErrors) is the worst latency
   # the reader: busy-poll (Ch. 61 §61.4.1 — epoll_wait(0) loop, SO_BUSY_POLL, or EPIOCSPARAMS) so stage 9 is a check, not a wake
   # TCP recovery/drop-copy sockets: TCP_NODELAY + TCP_QUICKACK before each read (Ch. 66), tcp_slow_start_after_idle=0
```

Each line removes a *variable* row of §62.2.1/§62.2.3; none of them makes the fixed ~2.8 µs smaller — that is what Ch. 63 (fewer syscalls), Ch. 64 (no copy), Ch. 78 (XDP before the skb) and Ch. 79 (no kernel) are for, in that order of invasiveness.

### 62.4.4 Netfilter hygiene: prove it is empty, or exempt the hot sockets

```
   nft list ruleset                                   # must be EMPTY on the hot box (not "only a default accept chain")
   lsmod | grep -E '^nf_|^nft_|^x_tables|^ip_tables|^br_netfilter'   # and the modules unloaded
   cat /proc/sys/net/netfilter/nf_conntrack_count     # if the file exists, conntrack is loaded; if nonzero, it is in use
   modprobe -r nf_conntrack_netlink nf_conntrack      # after removing every rule that references ct (fails while in use)
   # if a ruleset must stay (a firewall mandate): exempt the latency-critical flows from conntrack explicitly
   nft add table inet raw && nft add chain inet raw pre '{ type filter hook prerouting priority raw; }'
   nft add rule inet raw pre udp dport { 20000-20010 } notrack        # market-data / recovery ports: no ct entry, no lookup
   nft add rule inet raw pre tcp dport 31001 notrack                  # the venue session
   # and keep container runtimes off the box: Docker installs NAT + ct state rules + a bridge + br_netfilter on the ROOT namespace
```

`notrack` removes the per-packet lookup for those flows while leaving the policy in place for everything else; it is the right answer when the security team's ruleset cannot go away (Ch. 97 §97.4's threat model wants the box firewalled at the switch and the cross-connect anyway — Appendix K). Verify with §62.3 (2)'s harness against the real socket, not with a reading of the rules.

### 62.4.5 Namespaces: one stack pass, or none

A hot socket lives in the root namespace. Where a namespace is mandated (multi-tenant hosts, a security boundary around a venue session), choose the device type by stack passes: `veth` + bridge = two full passes plus a bridge (§62.3's +7.8 µs); `macvlan`/`ipvlan` in the namespace = one pass (the sub-interface's receive *is* the physical receive, demuxed by MAC/IP); an SR-IOV VF = its own queues and IRQs, zero shared stages, and the ability to steer its IRQ like any NIC (Ch. 72). Measure the chosen one with the §62.3 (1) harness run *across* the boundary (server in the namespace, client outside) before and after — the number, not the diagram, decides.

## 62.5 Pitfalls & anti-patterns: the stage you forgot was running

- **`ksoftirqd/N` on an isolated core.** A managed NIC vector or an RPS mask that names the hot core; the signature is `NET_RX` moving in `/proc/softirqs` for that CPU and `sched_switch → ksoftirqd/N` in Ch. 59's trace. `isolcpus=managed_irq`, `rps_cpus=0`, and check `effective_affinity_list`, not `smp_affinity`.
- **Sending from the hot thread.** One `sendto()` to a local process = the whole loopback RX path in the hot core's softirq context (§62.3 (1): one `NET_RX` per message on the *sender's* CPU). Telemetry and logs leave the hot thread through shared memory (Ch. 94, 96), and a housekeeping thread owns the socket.
- **`time_squeeze` nobody reads.** Column 3 of `softnet_stat` incrementing means `net_rx_action` hit its budget and deferred the rest to `ksoftirqd` — on a housekeeping core that is a capacity warning; on a hot core it is Ch. 59's bug. It is also the one counter `nstat` does not show.
- **Docker was installed once.** `iptables-nft` NAT and `ct state` rules, a bridge, `br_netfilter`: conntrack on every packet of every socket, forever, including after the containers are gone. `nft list ruleset` must be empty, not "default."
- **`fq_codel` as the default qdisc.** Several distributions set `net.core.default_qdisc=fq_codel`; it adds a flow hash, timestamps and CoDel's state to every packet and can *delay* a packet by design. `pfifo_fast` under `mq` on the order NIC; `fq` only where ETF/BBR need it (Ch. 76).
- **GRO on the recovery socket.** The first segment of a batch waits for the poll round to end (§62.2.1 stage 5): ~1–3 µs of hold on a quiet link, more under load. `ethtool -K gro off` on the latency NIC; leave it on for the capture NIC.
- **The stale ARP entry.** The first order after a quiet spell waits for a neighbour resolution (TX stage 2) — a millisecond class event that looks like a venue stall. Permanent neighbour entries for every peer on the order path; alarm on `ip -s neigh` state changes.
- **Raising `netdev_budget` to "fix" latency.** The budget bounds *throughput per softirq round*; raising it on a core that is time-squeezing lets one round run longer — which on a housekeeping core shared with other work is *more* jitter for the others. Add a queue and a core instead.
- **Two spinners, one core.** The §62.3 (1) run that did not finish: a busy-polling reader and its peer on the same CPU hand off once per scheduler slice. Busy-polling threads each own a core (Ch. 54), full stop.
- **Measuring on `lo` and extrapolating to the NIC.** Loopback has no NAPI, no IRQ, no DMA, and runs RX inline in the sender; it measures stages 7–10 and the wake-up only. Busy-poll "does nothing" on `lo` and everything on a NIC (Ch. 61 §61.3). The NIC path's first half needs a hardware timestamp to see.
- **A ruleset "with only accept rules" is not empty.** The hooks are traversed; one `ct`-referencing rule anywhere engages conntrack for every packet (§62.3 (2)). Empty means `nft list ruleset` prints nothing and the modules are not loaded.

## 62.6 Exercises & checklist

**Exercises**

1. Run `udp_rtt` in `spin` mode between two cores, then move the *client* to the server's SMT sibling. How do the RTT and the per-CPU `NET_RX` counts change, and what does that say about where loopback does its work (Ch. 55)?
2. With a real NIC and two hosts (or a NIC in loopback through a switch), reproduce §62.3 (3)'s table with `trace-cmd` or `bpftrace`. Which stage carries the p99.9? Turn GRO and coalescing off one at a time and re-run: attribute each knob to its stage.
3. Add one `ct state` rule, then a `notrack` rule for the harness's ports, and measure both against the empty ruleset. Does `notrack` fully recover the baseline? Now `modprobe -r nf_conntrack` — what has to be removed first, and how would you detect in production that the module came back?
4. Run the server inside a network namespace connected by (a) a `veth` pair and a bridge, (b) `macvlan`, (c) if you have one, an SR-IOV VF. Measure the three with the harness and read `/proc/softirqs` for the extra `NET_RX` passes.
5. Point an RPS mask at an isolated core (`echo 4 > rps_cpus` on the NIC's queue) and watch `/proc/softirqs`, `/proc/interrupts` (`CAL`) and `rtla osnoise` (Ch. 59) on that core while traffic flows. Then write the Ch. 110 telemetry check that would have caught it in a second.

**Checklist**

- [ ] The RX path is known stage by stage for the sockets that stay in the kernel, with a measured per-stage median and p99 anchored to the NIC hardware timestamp (§62.3 (3)), and re-measured after every kernel or driver change.
- [ ] `NET_RX`/`NET_TX` in `/proc/softirqs` are zero on the hot cores for the whole trading day (Ch. 110 samples them); `time_squeeze` is flat on every core.
- [ ] NIC queues and their vectors live on housekeeping cores (`isolcpus=managed_irq`, `effective_affinity_list` verified); `rps_cpus` is 0 (or names housekeeping cores only); `xps_cpus` maps TX queues to housekeeping cores.
- [ ] No hot thread calls `sendmsg`/`sendto` on a kernel socket; bytes leave through a ring to a housekeeping thread that owns the socket.
- [ ] GRO/LRO and interrupt coalescing are off on the latency NIC; the root qdisc is `mq` + `pfifo_fast` (or `noqueue`), `fq`/ETF only where Ch. 76 uses it; BQL `inflight` never approaches `limit`.
- [ ] Every peer on the order and recovery path has a permanent neighbour entry.
- [ ] `nft list ruleset` is empty and the netfilter/conntrack modules are unloaded — or the latency-critical ports are `notrack` and the cost is measured (§62.3 (2)).
- [ ] Hot sockets are in the root namespace; any mandated namespace uses `macvlan`/`ipvlan` or an SR-IOV VF, with the crossing measured.
- [ ] Readers of latency-critical kernel sockets busy-poll (Ch. 61 §61.4.1) or are woken on the core the NIC queue's IRQ runs on; the wake-up stage is measured, not assumed.

## 62.7 References

- Linux kernel documentation: `Documentation/networking/scaling.rst` (RSS, RPS, RFS, aRFS, XPS), `Documentation/networking/napi.rst` (NAPI, busy polling, threaded NAPI, `netdev_budget`), `Documentation/networking/statistics.rst` (`nstat`, `ethtool -S`, `softnet_stat` columns), `Documentation/networking/timestamping.rst` (`SO_TIMESTAMPING`), `Documentation/admin-guide/sysctl/net.rst`; `tc-pfifo_fast(8)`, `tc-mq(8)`, `tc-fq(8)`, `tc-etf(8)`; `nft(8)` and the netfilter project's *nftables wiki* (`notrack`, hook priorities, conntrack).
- Tom Herbert and Eric Dumazet, *Byte Queue Limits* (LWN, 2011; `include/linux/dynamic_queue_limits.h`) — the BQL mechanism and its auto-tuning.
- Joe Damato, *Monitoring and Tuning the Linux Networking Stack: Receiving Data* and *Sending Data* (packagecloud blog, 2016) — the stage-by-stage walkthrough of the RX and TX paths with every counter and knob named; still the best single map of §62.2.1/§62.2.3.
- Brendan Gregg, *BPF Performance Tools* (2019), ch. 10 "Networking" — the tracepoints and tools (`netsize`, `skblife`, `softirqs`, `superping`) that §62.3 (3) builds on; `bpftrace` reference guide for the `tracepoint:net:*`/`napi:*`/`skb:*` arguments.
- Jonathan Corbet, *Software interrupts and realtime* (LWN) and the `threadirqs` / `PREEMPT_RT` softirq documentation — how softirq work moves into threads and what that buys an isolated core.
- Jesper Dangaard Brouer, *Network stack challenges at increasing speeds* (LCA 2015 / netdev) — the per-packet cycle budget at 10–100 GbE and the cost of each stage that motivated XDP (Ch. 78).

## 62.8 Additional Reading

- Ch. 61 (*Linux Native I/O*) — the syscall layer above this path; Ch. 63–64 (io_uring, zero-copy) — removing syscalls and copies from stages 1 and 10; Ch. 72 (*NIC Features & Offloads*), Ch. 73 (DDIO) and Ch. 74 (flow steering) — the hardware end of stages 1–4; Ch. 78 (XDP) and Ch. 79 (kernel bypass) — the two ways to leave the path.
- Ch. 59 (*Hunting Residual Noise*) — the `ksoftirqd`/softirq row of the noise catalog and the tracers that catch it; Ch. 110 (*Network Health Telemetry & Drop Forensics*) — this chapter's counters as a continuous signal and the drop-attribution ladder.
- Christian Benvenuti, *Understanding Linux Network Internals* (O'Reilly, 2005) — dated in detail, still the clearest book-length account of `netif_receive_skb`, the softirq machinery and the qdisc layer.
- Appendix C.3 (the RPS/XPS, backlog and busy-poll knobs now pointed at their mechanism here), Appendix I.8 (the one-liners), Appendix F (BQL, NAPI, softirq, conntrack, `veth`).

---

*Next: Ch. 63 — io_uring Deep Dive, the completion-based evolution of Linux I/O: submission/completion queues that batch operations and slash syscalls, registered buffers and files, and polling modes — the kernel path with stages 1 and 10 of this chapter's tables amortised or removed, and the first step on the ladder from "the kernel stack" toward the zero-copy fast path of Ch. 64.*
