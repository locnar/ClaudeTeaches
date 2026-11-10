# Appendix M — Pre-Open Readiness Gate & Market-Open Playbook

> **Consolidates:** Ch. 61 (the signed-off quiet core and its baseline), Ch. 62 (warming as a go-live precondition), Ch. 72 §72.4.4 and Ch. 74 (feed liveness, membership, the feed gate), Ch. 78 (PTP offset and servo state), Ch. 100 (the telemetry segment that carries the readiness word), Ch. 103 §103.4.4 (kill-switch, gateway failover, warm restart), Ch. 114–115 (host and network invariants), Appendix C (the knobs this gate re-verifies), Appendix E and Appendix L (the fingerprint micro-suite's reference numbers and harness), Appendix J.3 and Appendix K.4 (the session layer and venue mandates). This is the *operational* companion to Appendix C: C says how to build a quiet, correct box; M says how to prove, every trading day, before the first order, that it still is one — and how the process itself knows whether it is allowed to trade.

**How to use this:** the gate is a *timed, scripted procedure with pass/fail criteria*, not a checklist read by a human. M.1 defines the in-process readiness state machine that the gate drives and the order path reads. M.2–M.5 are the gates themselves in time order — T-60 (host attestation), T-30 (clock, feed, network), T-5 (session, positions, warm-up, kill-switch), T-0 and intraday (arm, continuous re-verification, re-arm after a restart, disarm) — each item **a command with an expected value**. M.6 is the host fingerprint micro-suite that compares the box against Appendix E and its own history. M.7 is configuration as code and drift detection (the full treatment is Appendix C.6). M.8 is the gate runner as a script skeleton. The book's five dangling references to "the readiness gate/check" (Ch. 58, Ch. 103, Ch. 120, Appendix C) resolve here. The gate takes about ninety seconds of machine time, runs unattended, and its output — PASS, or the first FAIL with the command, the expected value and the observed one — is the only thing a human reads at 09:25.

---

## M.1 The readiness state machine

A trading process is in exactly one of seven states, published as a single 32-bit word in the header of the telemetry segment (Ch. 100 §100.4.1, `Segment::readiness`), written only by the process itself (or its supervisor on its behalf when it is dead), and read by the gate runner, the supervisor (Ch. 103) and the dashboards:

```
   cold ──> warming ──> syncing ──> reconciled ──> armed ──> trading ──> disarmed
    │          │           │            │            │          │            ▲
    │          │           │            │            │          └── close / kill-switch / operator ──┘
    │          │           │            │            └── first real order allowed (T-0)
    │          │           │            └── positions, limits, working orders agree with venue + drop-copy
    │          │           └── book rebuilt from snapshot, no open gaps, both lines live, PTP locked, session logged on
    │          └── pages pre-faulted, caches/TLB/predictors primed, shadow traffic running, NIC path warm (Ch. 62)
    └── process started; segment mapped; nothing trusted yet
   any state ──(gate FAIL, invariant delta, feed dark, session dead)──> disarmed   (never back to armed without re-running the gates)
```

- **The order path reads one word.** The gateway's send function checks `readiness == trading` with a single load and a predicted-taken branch (Ch. 15) — the same shape as Ch. 72's per-instrument stale flag — and refuses otherwise. Nothing else on the hot path knows about states; everything that *changes* the state runs off the hot path (the gate runner, the control plane — Ch. 103 §103.4.1, the supervisor).

```cpp
// readiness.hpp — the one hot-path touch point of the gate (Ch. 100's Segment::readiness is this word)
enum class Readiness : std::uint32_t { cold, warming, syncing, reconciled, armed, trading, disarmed };
inline bool may_send(const std::atomic<std::uint32_t>& r) noexcept {           // one load + one predicted branch
    return r.load(std::memory_order_acquire) == static_cast<std::uint32_t>(Readiness::trading);
}
// off the hot path, the gate runner / control plane advances it:  seg.readiness.store(uint32_t(Readiness::armed), release);
// and ANY failing invariant (Ch. 114/115 sampler, dark feed, dead session, kill-switch) stores `disarmed` — from a housekeeping core.
```

- **Transitions are monotonic until `disarmed`.** A process advances only when the gate for that state passes; any later failure drops it to `disarmed`, and the only way back is to re-run the gates from the failed one forward (M.5). There is no `armed`-to-`armed` "retry": the state is a claim with evidence behind it, and the evidence has a timestamp.
- **`armed` vs `trading`.** `armed` means every gate passed and the process *may* trade at T-0; `trading` is set by the operator or the scheduler at the open (or at re-arm after a restart) and is what the order path reads. The split exists so that the whole fleet can be verified `armed` at T-5 with the order path still closed, and opened together.
- **The supervisor owns the word when the process does not.** On a crash the supervisor (Ch. 103 §103.4.3) writes `disarmed` into the dead process's slot before restarting it, so a dashboard never shows a dead process as `trading` — and the restarted process starts at `cold` with the same gates ahead of it (M.5).

## M.2 T-60: host attestation

Sixty minutes before the open, on every box: *is this the machine we signed off, configured the way we signed it off?* Each line is the command, then the expected value; a mismatch is a FAIL that blocks `warming`. The baseline is the fingerprint recorded at sign-off (Ch. 61 §61.4.3, Ch. 114 §114.4.4) and stored with the release (M.7).

```bash
# --- provenance: anything here differing from the baseline is a change nobody declared
uname -r                                                       == baseline kernel release
cat /proc/cmdline | sha256sum                                  == baseline cmdline hash (isolcpus, nohz_full, rcu_nocbs, irqaffinity, hugepages, mitigations=, tsc=, nmi_watchdog=0 ...)
cat /sys/devices/system/cpu/cpu0/microcode/version             == baseline microcode
dmidecode -s bios-version; dmidecode -s bios-release-date      == baseline BIOS
ethtool -i <feednic> | grep -E 'driver|version|firmware'       == baseline driver + NIC firmware (per NIC)
tuned-adm active 2>/dev/null; systemctl is-active irqbalance   == "No current active profile"/baseline profile; irqbalance: inactive
# --- isolation (Ch. 56, 59, 61): the cores we think are isolated are, and nothing else lives there
cat /sys/devices/system/cpu/isolated                           == <hot cpus>
cat /sys/devices/system/cpu/nohz_full                          == <hot cpus>
cat /sys/devices/virtual/workqueue/cpumask                     == <housekeeping mask>
cat /proc/sys/kernel/watchdog_cpumask                          == <housekeeping cpus>
for i in /proc/irq/[0-9]*; do cat $i/effective_affinity_list; done | sort -u   == subsets of <housekeeping cpus> only (managed vectors included)
ps -eLo psr,cls,comm | awk '$1 ~ /^(<hot cpus>)$/'             == empty (nothing is running on a hot cpu yet)
# --- memory, clock, frequency (Ch. 6, 17, 20): the knobs that drift most
grep -E 'HugePages_(Total|Free)' /proc/meminfo                 == reserved count; Free == Total (nothing mapped yet)
cat /sys/kernel/mm/transparent_hugepage/enabled                == [never]  (or [madvise] by design — Appendix C.3)
swapon --show                                                  == empty
cat /sys/devices/system/clocksource/clocksource0/current_clocksource   == tsc
grep -c -E 'constant_tsc|nonstop_tsc' /proc/cpuinfo            == 2 × cpu count
cpupower frequency-info -p | grep -o 'performance'             == performance ; cat /sys/devices/system/cpu/intel_pstate/no_turbo == 1 (per Appendix L.3 policy)
cpupower idle-info | grep -A1 'C6' | grep -c 'DISABLED'        == 1 (deep C-states off — Appendix C.1/C.2)
cat /sys/devices/system/cpu/vulnerabilities/* | sort -u        == the baseline mitigation set (Ch. 6, 101)
sudo rdmsr -p 0 0x34                                           == baseline SMI count (unchanged since last night's value: no SMI storm overnight — Ch. 6 §6.4.4)
# --- cache partitioning (Ch. 58)
cat /sys/fs/resctrl/hotpath/schemata; cat /sys/fs/resctrl/hotpath/cpus_list   == baseline CBM masks and hot cpus
# --- NIC and PCIe (Ch. 75, 77, 87, 115)
ethtool -g <feednic> | awk '/^Current/,0'                      == baseline ring sizes (RX 4096 ...)
ethtool -c <feednic> | grep -E 'Adaptive|rx-usecs'              == Adaptive RX: off ; rx-usecs: 0
ethtool -k <feednic> | grep -E 'generic-receive-offload|large-receive-offload'   == off / off
ethtool -l <feednic> | awk '/^Current/,0' | grep Combined       == baseline queue count
ethtool -a <feednic> | grep -E 'RX|TX'                          == off / off (flow control — Ch. 115 §115.2.3; on only by lossless-fabric design, Ch. 84)
ethtool <feednic> | grep -E 'Speed|Duplex|Link detected'        == baseline speed, Full, yes
cat /sys/bus/pci/devices/<nic bdf>/current_link_{speed,width}  == baseline (e.g. 16.0 GT/s PCIe, 16) — Ch. 87
cat /sys/bus/pci/devices/<nic bdf>/aer_dev_correctable | awk '{s+=$2} END{print s}'   == baseline (no overnight growth)
cat /sys/class/net/<feednic>/carrier_changes                   == baseline (no overnight flaps)
# --- hardware health (Ch. 114 §114.4.3)
cat /sys/devices/system/edac/mc/mc*/ce_count | paste -sd+ | bc  == baseline (no overnight growth); ue_count == 0
sensors 2>/dev/null | grep -E 'Package|Core' | awk '{print $NF}'   # or ipmitool sdr: temperatures inside the vendor's normal band
```

Then start the Ch. 114 sampler (`hostwatch`) and the Ch. 115 sampler (`netwatch`) on a housekeeping core, pointed at the hot CPUs, and record "quiet since" = now. Every later gate requires that *no invariant delta* has been reported since this timestamp.

## M.3 T-30: clock, feed and network gates

Thirty minutes out: the processes start, map their segments, publish `cold`, and advance to `warming` and `syncing` while the gate runner checks the inputs they depend on.

```bash
# --- clock gate (Ch. 78): the fleet shares a time, and the box's own clock is sane
pmc -u -b 0 'GET TIME_STATUS_NP' | grep -E 'master_offset|gmPresent'   == |master_offset| < <budget, e.g. 100 ns> ; gmPresent true
journalctl -u ptp4l --since -10min | grep -o 'servo [a-z0-9]*' | tail -1   == servo s2 (locked); s0/s1 == FAIL (Ch. 78 §78.4.1)
pmc -u -b 0 'GET GRANDMASTER_SETTINGS_NP' | grep clockClass   == baseline clockClass (6 = GNSS-locked; 7/52 = holdover — FAIL after the holdover budget)
journalctl -u phc2sys --since -10min | tail -1                  == offset within budget; no "clockcheck" steps
cat /sys/devices/system/clocksource/clocksource0/current_clocksource   == tsc (again — it can be demoted at any time)
# --- feed gate (Ch. 72 §72.4.4, Ch. 74): joined, forwarded, flowing, rebuilt
grep -c . /proc/net/igmp                                        == 2 + <expected groups> per feed interface section (Ch. 74 §74.4.3)
timeout 260 tcpdump -i <feednic> -nn -c 1 'igmp and igmp[0]==0x11'   == a General Query seen within one Query Interval (a querier exists)
nstat -az IPReversePathFilter                                    == unchanged since T-60
telemetry: per channel book_age_ns (Ch. 100 §100.4.2)             == < phase threshold on BOTH lines for every channel (both lines live)
telemetry: per channel A/B skew                                 == within baseline ± tolerance (no path change overnight — §72.4.4)
telemetry: gaps post-A/B since start                            == 0 ; snapshot rebuild complete for every instrument (Ch. 72 §72.4.2)
telemetry: invariant violations (crossed/locked/collar)         == 0 since rebuild
# --- network gate (Ch. 115): nothing has dropped anywhere since T-60
netwatch log since T-60                                          == no NIC/driver, NAPI, STACK or LINK lines (ring, FIFO, squeeze, socket, rp_filter, CRC, flaps all zero)
ethtool -S <feednic> | grep -E 'fec_uncorrectable|rx_crc'        == unchanged since T-60
ethtool -m <feednic> | grep -E 'Rx|Tx' | grep -i power           == inside the transceiver's warning thresholds (DOM — Ch. 115 §115.4.4)
# --- host gate (Ch. 114): the box is still the quiet box
hostwatch log since T-60                                         == only "ok" lines: no LOC/RES/CAL/TLB/NMI/PMI deltas on hot cpus, no nonvoluntary switches, no faults after warm-up, no throttle, no clocksource change
sudo rdmsr -p 0 0x34                                             == same as T-60 (no SMI in the last half hour)
```

A process passes `syncing` only when its own channels pass the feed gate; a box passes T-30 only when every process on it has, and the clock, network and host gates are green. A FAIL here is a *fix-and-re-run*, with thirty minutes to do it — which is the point of running this gate at T-30 and not at T-5.

## M.4 T-5: session, positions, warm-up and the kill-switch

Five minutes out, the checks that need the venue:

```bash
# --- session gate (Appendix J.3, Ch. 72 §72.4.4, Ch. 103 §103.4.4)
telemetry: session state per venue                              == logged on; inbound/outbound sequence numbers == venue's (no ResendRequest outstanding)
telemetry: heartbeats received in the last 5 × HeartBtInt       == 5 ± 1 ; TestRequests sent == 0 ; TestRequests received == 0
gateway: send TestRequest, measure Heartbeat round-trip (TSC)    == within the venue's ack-latency baseline (a warm session; Ch. 62 §62.4.2)
telemetry: oldest unacknowledged order age                      == 0 (nothing in flight) or < 2 × ack p99.9
# --- positions and limits
risk: positions per instrument (internal) vs venue start-of-day / drop-copy (Appendix K.4)   == equal, to the share
risk: working orders (internal) vs venue open-order report                                    == equal (usually: none)
risk: pre-trade limits loaded, version == released config version (Ch. 102 §102.4.3)             == match ; kill-switch thresholds loaded
# --- warm-up gate (Ch. 62)
telemetry: minflt per hot thread since warming began            == 0 for the last 60 s (every page is resident — Ch. 34)
telemetry: shadow-traffic wire-to-wire p99.9 over the last 60 s  == within the Ch. 120 §120.3 budget (the path is warm, not just mapped)
telemetry: per hot thread cpu                                    == the configured isolated cpu (it is where it should be)
hostwatch: hot-thread nonvoluntary switches since warming        == 0
# --- the kill-switch, dry-run (Ch. 103 §103.4.4; SEC 15c3-5 / MiFID II RTS 6 mandate it — Appendix K.4)
control plane: `kill-switch --dry-run <shard>`                   == gateway walks the (empty) working-order set and reports the cancel path reachable within <budget>
control plane: `disarm <shard>`; then `arm <shard>`              == readiness word transitions observed in the segment within <budget>; order path refuses while disarmed
```

Pass → the process publishes `reconciled`, then `armed`. The operator sees one line per process: `armed @ 09:25:12, quiet since 08:31:04, feeds live, PTP s2 -43 ns, positions reconciled`. Anything else is a FAIL line with the command, the expected value and the observed one.

## M.5 T-0 and intraday: arm, re-verify, re-arm, disarm

- **T-0.** The scheduler (or operator) sets `trading` for every `armed` process at the venue's open time — never earlier (an order into a closed book is a reject at best). The first minute of the open is a different SLO regime (Ch. 100 §100.4.4); the gate's job ended at `armed`.
- **Continuous re-verification.** The Ch. 114 and Ch. 115 samplers run all day. Their invariant classes — hot-thread switches/faults/migrations, the IPI rows, throttling, SMIs, clocksource, drops at any layer, feed age, PTP offset — are not re-checked by a gate; a *delta* is an incident, and the supervisor's policy decides whether it is a ticket (an SMI at 11:00, the strategy keeps trading, someone looks) or a `disarmed` (feed dark on both lines, session dead, clocksource demoted, `nonvoluntary_ctxt_switches` on an order-path thread). Write the mapping down per signal; it is the one table the on-call engineer reads during the session.
- **Re-arm after an intraday restart.** A process that crashed and was restarted by the supervisor (Ch. 103 §103.4.3) starts at `cold` and must pass the *same* gates — compressed, not skipped: provenance (unchanged since T-60 — skip), host and network invariants (the samplers' logs since the restart: seconds), feed gate (snapshot rebuild and both lines live — the recovery path of Ch. 72 §72.4.2, measured in Ch. 103 §103.3), session gate (logon with sequence recovery), positions (reconcile against the venue — mandatory; the dead process's working orders were already cancelled by the kill-switch), warm-up (shadow traffic until the first-message latency is within budget — Ch. 62). Time-box it: a re-arm that cannot complete inside the budget (say 30 s) stays `disarmed` and the shard sits out; a half-reconciled process that trades is worse than a shard that does not.
- **Disarm.** At the close, or on an operator command, or on a kill-switch: `disarmed`, working orders cancelled (Ch. 103 §103.4.4), memberships *kept* (the overnight feed and the venue's post-close messages are data too — Ch. 74 §74.5), samplers left running. The overnight SMI/EDAC/AER/carrier counts are exactly what T-60 compares against tomorrow.

## M.6 The host fingerprint micro-suite

Attestation (M.2) proves the *configuration* is unchanged; it does not prove the *machine* behaves as it did. A sixty-second micro-suite, run at T-60 after the attestation and compared against two references — Appendix E's table for the reference machine and *this host's own history* — catches what configuration cannot: a DIMM running in a degraded mode, a NIC whose link downtrained, a core whose turbo-off setting did not take, a BIOS update that re-enabled a prefetcher. Each test is Appendix L's harness pinned to one isolated core (Appendix L.3), each result a number with a pass band:

```
   test (Appendix L harness, pinned, 60 s total)             reference (App. E, Xeon Gold 6326 @ 2.9 GHz)   pass band (vs this host's baseline)
   pointer-chase latency ladder: L1 / L2 / L3 / DRAM (Ch. 8)   1.7 ns / ~5 ns / ~15–25 ns / ~80–100 ns       each within ±10 % of baseline
   DRAM NUMA-remote vs local (Ch. 18; dual-socket boxes)       ~1.5–2× local                                  within ±10 %
   rdtsc / fenced rdtscp cost (Ch. 20)                         ~6 ns / ~12–25 ns                              within ±10 %
   null syscall (Ch. 54; mitigations= as deployed)             ~50–100 ns (off) / ~150–400 ns (on)            within ±10 %; a band change = mitigation drift
   uncontended atomic RMW; cross-core ping-pong (Ch. 41, 44)   ~5–10 ns; ~40–100 ns per transfer              within ±15 %
   clock_gettime via vDSO (Ch. 6 §6.4.4, 20)                   ~18–20 ns                                      > 100 ns = clocksource is not tsc: FAIL
   cyclictest -m -p 80 -a <hot> -i 200 -D 60  (Ch. 59)          max ≤ ~2–5 µs                                  max ≤ baseline max + 2 µs; a 10× max = FAIL
   gapdetect --seconds 60 (Ch. 61 §61.3)                        worst gap ≤ a few µs                           worst ≤ baseline worst + 2 µs
   effective core frequency (TSC cycles per fixed work unit)    2.9 GHz = base                                 within ±1 % (turbo off, no throttle)
   NIC loopback / A-B skew baseline (Ch. 70, 75; if available)  baseline skew distribution                      median within ±5 µs of baseline
```

The bands are deliberately loose — this is a smoke test for a changed machine, not a benchmark — and the comparison that matters is against the host's *own* history (a box that was 92 ns to DRAM for a year and is 118 ns this morning has a story, even though 118 ns is inside Appendix E's range). Record every run; the time series of the micro-suite is the host's health record, and a slow drift in it is a failing component.

## M.7 Configuration as code and drift detection (summary)

Appendix C.6 carries the checklist; the principle in one paragraph: **the box's configuration is a versioned artefact released with the software, applied by tooling, and audited against its declaration.** Appendix C's knobs become a configuration-management role (Ansible/Salt/systemd unit tree) checked into the same repository as the trading system and tagged with the same release; the firmware/microcode/driver inventory is part of it; the fingerprint (M.2) is *generated from* the declaration, so the attestation compares the live box to what the release says it should be — not to what somebody remembers. Drift is then a `diff` with two possible resolutions: re-apply the declaration (the box drifted) or change the declaration through a release (the intent changed), with a fresh Ch. 61 sign-off and baseline in the latter case. A fleet of boxes shares the role and differs only in a per-host variables file (hot CPU list, NIC names, NUMA node); a host that fails the gate is compared against its siblings first.

## M.8 The gate runner (skeleton)

The whole appendix as a program: a shell skeleton whose `check` function records a command, an expected value and the observed one, and whose exit status is the gate's verdict. Fill the `expected` values from the baseline file; add the telemetry reads through the Ch. 100 scraper's export.

```bash
#!/usr/bin/env bash
# gate.sh — readiness gate runner: gate.sh <T-60|T-30|T-5|rearm> <host-baseline.env>
# Every check: a command, an expected value, the observed value. First FAIL is printed with all three; exit 1.
set -u; source "$2"; FAILS=0
check() {  # check "<label>" "<command>" "<expected>"      (expected may be a regex: prefix with ~)
  local label=$1 cmd=$2 exp=$3 obs
  obs=$(bash -c "$cmd" 2>/dev/null | tr -d '\n')
  if [[ $exp == ~* ]]; then [[ $obs =~ ${exp#\~} ]] && ok=1 || ok=0; else [[ $obs == "$exp" ]] && ok=1 || ok=0; fi
  if (( ok )); then printf 'PASS  %-44s %s\n' "$label" "$obs"
  else printf 'FAIL  %-44s expected [%s] observed [%s]\n      %s\n' "$label" "$exp" "$obs" "$cmd"; FAILS=$((FAILS+1)); fi
}
case $1 in
T-60)
  check "kernel release"        "uname -r"                                               "$BASE_KERNEL"
  check "cmdline hash"          "sha256sum < /proc/cmdline | cut -c1-16"                 "$BASE_CMDLINE_SHA"
  check "microcode"             "cat /sys/devices/system/cpu/cpu0/microcode/version"     "$BASE_MICROCODE"
  check "isolated cpus"         "cat /sys/devices/system/cpu/isolated"                   "$HOT_CPUS"
  check "nohz_full cpus"        "cat /sys/devices/system/cpu/nohz_full"                  "$HOT_CPUS"
  check "workqueue cpumask"     "cat /sys/devices/virtual/workqueue/cpumask"             "$HK_MASK"
  check "irqbalance inactive"   "systemctl is-active irqbalance"                         "inactive"
  check "clocksource"           "cat /sys/devices/system/clocksource/clocksource0/current_clocksource" "tsc"
  check "THP"                   "cat /sys/kernel/mm/transparent_hugepage/enabled"        "~\[never\]|\[madvise\]"
  check "governor"              "cat /sys/devices/system/cpu/cpu${HOT_CPU0}/cpufreq/scaling_governor" "performance"
  check "no_turbo"              "cat /sys/devices/system/cpu/intel_pstate/no_turbo"      "1"
  check "SMI count unchanged"   "rdmsr -p 0 0x34"                                        "$BASE_SMI"
  check "feed NIC ring"         "ethtool -g $FEEDNIC | awk '/^Current/,0' | awk '/^RX:/{print \$2}'" "$BASE_RX_RING"
  check "feed NIC coalescing"   "ethtool -c $FEEDNIC | awk '/^rx-usecs:/{print \$2}'"     "0"
  check "feed NIC flow control" "ethtool -a $FEEDNIC | awk '/^RX:/{print \$2}'"          "off"
  check "PCIe link width"       "cat /sys/bus/pci/devices/$NIC_BDF/current_link_width"   "$BASE_PCIE_WIDTH"
  check "EDAC CE count"         "cat /sys/devices/system/edac/mc/mc*/ce_count | paste -sd+ | bc" "$BASE_EDAC_CE"
  check "no stray tasks on hot" "ps -eLo psr= | sort -n | uniq | tr '\n' ' ' | grep -c -E '\b($HOT_CPU_RE)\b'" "0" ;;
T-30)
  check "PTP servo locked"      "journalctl -u ptp4l --since -10min | grep -o 'servo s[0-9]' | tail -1" "servo s2"
  check "PTP offset (ns)"       "pmc -u -b 0 'GET TIME_STATUS_NP' | awk '/master_offset/{print (\$2<0?-\$2:\$2)}'" "~^([0-9]|[1-9][0-9])\$"
  check "IGMP groups/feed NIC"  "awk -v d=$FEEDNIC '\$2==d{c++} END{print c+0}' /proc/net/igmp" "1"
  check "general query seen"    "timeout 130 tcpdump -i $FEEDNIC -nn -c 1 'igmp and igmp[0]==0x11' >/dev/null 2>&1 && echo yes" "yes"
  check "rp_filter drops"       "nstat -az IPReversePathFilter | awk '{print \$2}'"       "$BASE_RPF"
  check "host sampler quiet"    "grep -vc ' ok' $HOSTWATCH_LOG"                           "0"
  check "net sampler quiet"     "grep -vc ' ok' $NETWATCH_LOG"                            "0"
  check "feeds live, no gaps"   "curl -s $SCRAPER/feeds | jq -r '[.channels[] | select(.age_ns > .threshold_ns or .gaps > 0)] | length'" "0" ;;
T-5)
  check "sessions logged on"    "curl -s $SCRAPER/sessions | jq -r '[.[] | select(.state != \"logged_on\" or .resend_outstanding)] | length'" "0"
  check "positions reconciled"  "curl -s $SCRAPER/risk | jq -r '.position_mismatches + .working_order_mismatches'" "0"
  check "warm: faults 60s"      "curl -s $SCRAPER/host | jq -r '[.threads[] | .minflt_60s] | add'" "0"
  check "warm: shadow p99.9"    "curl -s $SCRAPER/shadow | jq -r '.p999_ns <= .budget_ns'"  "true"
  check "kill-switch dry run"   "$CONTROL kill-switch --dry-run --all | tail -1"          "~^ok"
  check "all shards armed"      "curl -s $SCRAPER/readiness | jq -r '[.[] | select(. != \"armed\")] | length'" "0" ;;
esac
(( FAILS == 0 )) && echo "GATE $1 PASS" || { echo "GATE $1 FAIL ($FAILS)"; exit 1; }
```

The runner is deliberately dull: no logic beyond compare-and-report, no thresholds computed at runtime (they come from the baseline file the release generated — M.7), and its log is the day's attestation record (Ch. 109's compliance archive is the right home for it).

## M.9 References

- Ch. 61 §61.4.3 and Ch. 114 §114.4.4 (the sign-off baseline and the drift audit), Ch. 62 (warming as a precondition), Ch. 72 §72.4.4 and Ch. 74 §74.4.3 (the feed gate's checks), Ch. 78 §78.4.1 (PTP servo states and offset), Ch. 100 (the segment and the `readiness` word), Ch. 103 §103.3–§103.4.4 (restart timing, kill-switch, gateway failover), Ch. 114–115 (the invariants the gate requires to be quiet), Appendix C (every knob re-verified here), Appendix E and L (the micro-suite's references and harness), Appendix J.3 and K.4 (session layer, venue mandates).
- SEC Rule 15c3-5 (Market Access Rule) and ESMA MiFID II RTS 6 (Articles 5–7, 12–13: annual self-assessment, pre-trade controls, kill functionality, real-time monitoring) — the regulatory requirements a readiness gate and kill-switch test satisfy (M.4).
- `linuxptp` documentation (`pmc` management messages `TIME_STATUS_NP`, `GRANDMASTER_SETTINGS_NP`; `ptp4l` servo states) — the clock gate (M.3).
- Ansible/Salt documentation and the systemd unit/`sysctl.d`/`tuned` profile mechanisms — configuration as code (M.7, Appendix C.6).

## M.10 Additional Reading

- Beyer et al., *Site Reliability Engineering* (the chapters on release engineering and on managing incidents) and *The Site Reliability Workbook* — the production-readiness-review idea this appendix specialises to a trading day.
- John Allspaw and the "blameless post-mortem" literature; the aviation checklist literature (Gawande, *The Checklist Manifesto*) — why a gate is a script with expected values rather than a list a human reads.
- Vendor "day-in-the-life" operations guides from exchange connectivity providers and the trading-systems community's market-open runbooks — the operational folklore M.2–M.5 formalise.
- **Appendix C** (the knobs), **Appendix I** (the one-liners behind every check), **Appendix F** (readiness / attestation / drift / kill-switch glossary).

---

*Next: Appendix N — Incident Diagnostics Bundle, the other end of the operating day: when the gate passed and a spike, a crash or a drop happened anyway, the checklist and bounded collector that gather provenance, clock anchors and every layer's frozen evidence into one alignable, attributable, checkable bundle (Ch. 108, 113).*
