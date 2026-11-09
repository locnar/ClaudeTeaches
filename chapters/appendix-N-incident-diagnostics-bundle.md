# Appendix N — Incident Diagnostics Bundle

> **Consolidates:** Ch. 89 (the forensic method and the frozen rings — this appendix is its checklist), Ch. 87 (the crash record and the core), Ch. 82 (the trace record and the venue-stamp series), Ch. 83 (the telemetry segment), Ch. 90–91 (host and network samples), Ch. 49 (the ftrace ring), Ch. 63 (PTP state and the cross-timestamp anchors), Ch. 85 §85.4.3 (the configuration hash), Ch. 88 (the capture window), Ch. 5 §5.4.1 (build IDs and archived debug info), Appendix M.6 (the host fingerprint), Appendix I (the one-liners the collector runs).

**How to use this:** an incident — a latency spike (Ch. 89), a crash (Ch. 87), a drop (Ch. 91), a venue reject burst (Ch. 58 §58.4.4) — is *diagnosable* only if the evidence from every layer can be laid on one timeline and checked against the right binary, configuration and host. This appendix is the list of what that takes: N.1 the three properties a bundle must have, N.2 provenance, N.3 clocks and anchors, N.4 the evidence per layer (with the chapter that produces each item), N.5 a bounded collection script, N.6 the ten-minute first look, N.7 retention and what to scrub. Each item is a command or a file; the script in N.5 produces a directory with a manifest. Collect within minutes of the trigger, from a housekeeping core, and never by attaching anything to a hot thread (Ch. 5 §5.4.4).

---

## N.1 What a bundle is for: alignable, attributable, checkable

Three properties, each a failure mode of real incident folders:

- **Alignable.** Every file carries timestamps, and the bundle carries the *anchors* that map them onto one axis (N.3): the TSC↔`CLOCK_MONOTONIC` sandwich, the TSC↔PHC cross-timestamp, the `trace_clock` in use, the PTP offset. Without the anchors, a 41 µs window cannot be found in a `perf.data` stamped in `sched_clock` or a journal stamped in `BOOTTIME` (Ch. 89 §89.2.2).
- **Attributable.** Every address, counter and flag is tied to the exact build, configuration and host that produced it (N.2): build IDs for every binary and library in the process, the recorded compiler flags, the configuration hash, the kernel command line, microcode and firmware versions, the host fingerprint. A frame address from a binary whose build ID is not in the bundle resolves to `??`; a counter from a kernel whose `isolcpus` list is unknown cannot be judged.
- **Checkable.** The raw evidence — the frozen rings, the sample windows, the capture slice — is included, not a summary of it, with checksums in a manifest, so a second engineer (or the venue, or a post-incident review a month later) can rerun Ch. 89 §89.4.4's table and reach the verdict independently.

A fourth property is *bounded*: the collector must finish in under a minute, touch no hot core, write to a non-capture disk, and never grow past a size the retention policy (N.7) can hold.

## N.2 Provenance: which build, which configuration, which host

```bash
# binaries and libraries of the process (Ch. 5 §5.4.1): build ID, recorded flags, load bases for ASLR
for f in $(awk '$6 ~ /^\// {print $6}' /proc/$PID/maps | sort -u); do
  printf '%s  ' "$f"; readelf -n "$f" 2>/dev/null | awk '/Build ID/ {print $3}'
done > provenance/build-ids.txt
readelf -p .GCC.command.line /path/to/strategy > provenance/gcc-flags.txt   # needs -frecord-gcc-switches at build (App. D.7)
cp /proc/$PID/maps provenance/maps.txt                                      # load bases: frame addresses → file offsets
cat /proc/$PID/status /proc/$PID/limits > provenance/status.txt             # Cpus_allowed, SigBlk, RLIMIT_CORE, …
# configuration (Ch. 85 §85.4.3): the hash the process published, and the files behind it
cat /dev/shm/telemetry-$SHARD | telemdecode --header > provenance/config-hash.txt
git -C /etc/trading rev-parse HEAD > provenance/config-git.txt 2>/dev/null; tar czf provenance/etc-trading.tgz /etc/trading
# kernel, firmware, microcode, tuning (Appendix C.6 drift detector's inputs)
cat /proc/cmdline /proc/version > provenance/kernel.txt
dmidecode -t bios -q > provenance/bios.txt; grep -m1 microcode /proc/cpuinfo >> provenance/kernel.txt
cat /sys/devices/system/cpu/isolated /sys/devices/system/cpu/nohz_full /sys/kernel/mm/transparent_hugepage/enabled >> provenance/kernel.txt
# the host fingerprint (Appendix M.6) and the most recent readiness-gate report (Appendix M.8)
cp /var/lib/trading/fingerprint-$(date +%F).json provenance/; cp /var/lib/trading/gate-$(date +%F).log provenance/
# NIC: driver, firmware, ring and coalescing settings (Ch. 60, 91)
for i in $IFACES; do ethtool -i $i; ethtool -g $i; ethtool -c $i; ethtool -k $i; done > provenance/nic.txt 2>&1
```

## N.3 Clocks and anchors: the one timeline

```bash
# the anchors the Ch. 90 sampler logs every second — copy the window ±60 s (Ch. 89 §89.4.1's clockmap line)
awk -v a=$((T_REAL-60)) -v b=$((T_REAL+60)) '$1>=a && $1<=b' /var/log/trading/anchors.log > clocks/anchors.log
# PTP: offset, servo state, and whether the clock was STEPPED in the window (Ch. 63; a step is a fingerprint — Ch. 89 §89.2.3)
journalctl -u ptp4l -u phc2sys -u chronyd --since="@$((T_REAL-300))" --until="@$((T_REAL+60))" -o short-precise > clocks/ptp-journal.txt
pmc -u -b 0 'GET CURRENT_DATA_SET' 'GET TIME_STATUS_NP' > clocks/pmc.txt 2>&1
phc_ctl $IFACE cmp > clocks/phc-cmp.txt 2>&1                                 # PHC vs system clock now
# the clocks each ring used (so the analyst knows what to convert): record them, do not assume
cat /sys/kernel/tracing/trace_clock > clocks/trace_clock.txt                 # must read "[x86-tsc]"
perf report -i /inc/perf.data --header-only 2>/dev/null | grep -i clockid > clocks/perf-clockid.txt
cat /sys/devices/system/clocksource/clocksource0/current_clocksource > clocks/clocksource.txt   # "tsc", or the box has a problem (Ch. 17, 90)
```

## N.4 The evidence, per layer

Each row names the file, the producer, and the window to cut. `T0..T1` is the incident window in raw TSC (from the trace record), `T_REAL` its `CLOCK_REALTIME` second (from the anchors).

```
   layer         item                                              produced by                   cut / command
   ───────────── ───────────────────────────────────────────────── ───────────────────────────── ──────────────────────────────────────────────────
   application   the trace record of `corr` and ±1,000 neighbours   Ch. 82 gateway log            blogdecode trace.blog --around corr=$CORR --n 1000
                 the frozen flight recorder(s), raw + decoded       Ch. 89 §89.4.2 drainer        cp /inc/frozen-*.bin; frdecode > app/flightrec.txt
                 binary log window ±1 s, all threads                Ch. 81                        blogdecode --tsc $((T0-2700000000)),$((T1+2700000000))
                 telemetry segment snapshot: histograms, counters,  Ch. 83 §83.4.1                telemdecode /dev/shm/telemetry-$SHARD --all > app/telemetry.txt
                 readiness word, heartbeat/progress
                 per-stage histogram series ±10 min                 Ch. 83 scraper                curl -s scraper:9100/metrics?window=…; or the TSDB export
                 crash record + core location (if a crash)          Ch. 87 §87.4.1, §87.4.4       cp /var/crash/*.$PID.* ; coredumpctl info $PID
                 venue-stamp series for the window (feed latency,   Ch. 82 §82.4.5                blogdecode --fields corr,t_hw_rx,sending_time …
                 wire-out, symmetry check)
   kernel ring   the frozen ftrace ring (per-CPU, x86-tsc)          Ch. 49 §49.4.2, Ch. 89        cat /sys/kernel/tracing/per_cpu/cpu$C/trace > kernel/ftrace-cpu$C.txt
                 perf.data snapshot(s) + perf archive               Ch. 89 §89.4.3 B, Ch. 92      cp /inc/perf.data.*; perf archive /inc/perf.data.*  (symbols!)
                 Intel PT snapshot (if armed)                       Ch. 89 §89.4.3 C              cp /inc/pt.data*   (decode elsewhere)
                 journal / dmesg window                             —                             journalctl -k -o short-precise --since=@$((T_REAL-300)) --until=@$((T_REAL+60))
   host          Ch. 90 sampler records ±10 min (all signals)       Ch. 90 §90.4.1                awk window over /var/log/trading/host-health.log
                 /proc/interrupts, /proc/softirqs — NOW (for the    —                             cp /proc/interrupts /proc/softirqs host/   (deltas come from the sampler)
                 analyst's reference columns)
                 /proc/vmstat, /proc/meminfo, THP, PSI, numastat    Ch. 15, 23, 90                cp /proc/vmstat /proc/meminfo /proc/pressure/* ; numastat -p $PID
                 scheduler: who ran on the hot CPUs ±1 s            Ch. 49 §49.4.2                perf sched timehist -i /inc/perf.data.sched (if armed); else the ftrace ring
                 frequency / thermal / SMI / C-state                Ch. 90 §90.4.2                turbostat --quiet -n 1 -i 1 > host/turbostat.txt (housekeeping cores only!)
                 per-thread: status, sched, stack (NO ptrace)       Ch. 5 §5.4.4                  for t in /proc/$PID/task/*; do cat $t/status $t/sched; done; cat $t/wchan
                 RAS: EDAC, MCE, PCIe AER counters                  Ch. 90 §90.4.3                rasdaemon: ras-mc-ctl --errors; cat /sys/bus/pci/devices/*/aer_dev_*
   network       Ch. 91 sampler records ±10 min                     Ch. 91 §91.4.1                awk window over /var/log/trading/net-health.log
                 ethtool -S / ip -s link NOW (reference)            Ch. 91                        ethtool -S $IFACE > net/ethtool-S.txt; ip -s -s link > net/ip-link.txt
                 drop reasons (if armed)                            Ch. 91 §91.4.2                perf script -i /inc/perf.data.drops; or the bpftrace output file
                 multicast membership / IGMP state                  Ch. 59                        ip maddr; cat /proc/net/igmp
                 bypass-stack counters (if in use)                  Ch. 91 §91.4.5                onload_stackdump lots; exanic-config; dpdk-proc-info --xstats
   capture       the capture slice ±1 s around `corr` (A and B)     Ch. 88 §88.4.1                capslice --from $((T0-…)) --to $((T1+…)) --lines A,B > capture/window.pcapng
                 the journal records (orders/acks) for `corr`       Ch. 88 §88.4.2                jnlgrep --corr $CORR
   tap           the fabric tap's pcapng for the window (if any)    Ch. 65 §65.4.3, Ch. 82 §82.4.6 from the capture box; join by venue seq
```

Two rules about the table. **Everything not already on disk is read from `/proc`, `/sys` or a shm segment by the collector on a housekeeping core** — `turbostat` and MSR reads only on housekeeping cores (Ch. 90 §90.2.4: an MSR read of the hot core IPIs it), no `perf stat -C hot`, no `gdb -p`, no `cat /proc/<hot-tid>/stack` of a *running* hot thread (it is a kernel-side walk and safe, but the thread's `stack` is usually empty for a user-space spinner; its `wchan` is `0`). **The hot thread's own contribution ended when it froze its ring**; the collector never asks it for anything else.

## N.5 The collection script (skeleton)

```bash
#!/bin/bash
# incident-collect.sh — bounded incident bundle collector. Runs on housekeeping cores, writes to a non-capture disk.
# Usage: incident-collect.sh <corr> <T0_tsc> <T1_tsc> <pid> [perf.data...]      Invoked by the drainer/supervisor on a trigger.
set -u
CORR=$1; T0=$2; T1=$3; PID=$4; shift 4
HK=0-1; OUT=/var/incident/$(date -u +%Y%m%dT%H%M%SZ)-$CORR; mkdir -p $OUT/{provenance,clocks,app,kernel,host,net,capture}
exec taskset -c $HK nice -n 10 ionice -c 2 -n 7 timeout 60 bash -c '
  cd '"$OUT"'
  T_REAL=$(anchors2real '"$T0"')                     # TSC → CLOCK_REALTIME second, via the latest anchor line (Ch. 89 §89.4.1)
  echo "corr='"$CORR"' t0='"$T0"' t1='"$T1"' pid='"$PID"' t_real=$T_REAL host=$(hostname) collected=$(date -u +%FT%TZ)" > manifest.txt
  # N.2 provenance, N.3 clocks, N.4 evidence — each group its own line, each tolerant of a missing tool:
  ( set -x; source /usr/local/lib/incident/provenance.sh '"$PID"' ) > provenance/collect.log 2>&1
  ( set -x; source /usr/local/lib/incident/clocks.sh $T_REAL ) > clocks/collect.log 2>&1
  ( set -x; source /usr/local/lib/incident/app.sh '"$CORR $T0 $T1 $PID"' ) > app/collect.log 2>&1
  for c in $(cat /sys/devices/system/cpu/isolated | tr ",-" "  " | xargs seq 2>/dev/null || cat /sys/devices/system/cpu/isolated); do
    cp /sys/kernel/tracing/per_cpu/cpu$c/trace kernel/ftrace-cpu$c.txt 2>/dev/null; done
  journalctl -k -o short-precise --since=@$((T_REAL-300)) --until=@$((T_REAL+60)) > kernel/journal.txt 2>&1
  ( set -x; source /usr/local/lib/incident/host.sh $T_REAL '"$PID"' ) > host/collect.log 2>&1
  ( set -x; source /usr/local/lib/incident/net.sh $T_REAL ) > net/collect.log 2>&1
  ( set -x; source /usr/local/lib/incident/capture.sh '"$CORR $T0 $T1"' ) > capture/collect.log 2>&1
  for f in "$@"; do [ -f "$f" ] && cp "$f" kernel/ && perf archive "$f" >/dev/null 2>&1 && mv "$f".tar.bz2 kernel/ 2>/dev/null; done
  find . -type f ! -name sha256sums.txt -exec sha256sum {} + > sha256sums.txt
  du -sh . >> manifest.txt
' -- "$@"
echo "$OUT"
```

The bounds are the point: `taskset` to the housekeeping cores (never `isolated`), `nice`/`ionice`, `timeout 60` (a partial bundle with a manifest beats a collector that is still running when the next trigger fires), per-group logs so a missing tool breaks one row and not the bundle, a `sha256sums.txt` so the bundle is checkable, and a manifest that names the window, the `corr` and the host so the directory is self-describing. The per-layer `*.sh` files are the N.2–N.4 commands, one group each; keep them in the configuration repository with the rest of Appendix C.6's artefacts so the collector is versioned like the box.

## N.6 The ten-minute first look

In order — the cheap exonerations first (Ch. 89 §89.4.4):

1. `manifest.txt`, `clocks/trace_clock.txt` (`x86-tsc`?), `clocks/anchors.log` (does the window fall inside the anchor coverage? did the residual jump — a step?).
2. `app/flightrec.txt` around `corr`: are *all* records in the window stretched (the core slowed) or is there a *gap* (the thread stopped)? Which site was last before the gap?
3. `kernel/ftrace-cpu$C.txt` in `[T0, T1]`: `sched_switch`? `irq_*`? `tlb_flush`? `ipi_send`? `tick_stop`? Empty?
4. `host/`: the Ch. 90 sample covering the window — any invariant delta (switches, migrations, faults, `CAL`/`TLB`/`LOC` columns, SMI count, frequency, thermal)?
5. `net/`: the Ch. 91 sample — drops, pause frames, membership; `app/` venue series — did `t_hw_rx − SendingTime` jump for this `corr` only?
6. `app/telemetry.txt`: readiness word, heartbeat/progress, the per-stage histograms' interval before/after.
7. `provenance/`: is this the build and configuration you think it is? (`build-ids.txt` against the release manifest; `config-hash.txt` against the deployed version.)
8. Only now: `perf.data` / `pt.data` decode on a housekeeping box, the capture replay (Ch. 88), the core (Ch. 87 §87.4.5).

Write the verdict — mechanism, evidence rows, knob or fix, gate entry — into `manifest.txt` and close the incident with it; an unattributed bundle is kept for the pattern analysis of N.7, not deleted.

## N.7 Retention, pattern analysis and what to scrub

- **Size and retention.** A bundle is tens to a few hundred MB (rings 2–16 MiB each, `perf.data` snapshots 10–100 MB, capture slice a few MB, PT up to the AUX size). Keep every bundle for the regulatory horizon that applies to the *orders* in it (Appendix K.7), and the attributed verdicts indefinitely — the series of verdicts is the input to the drift audit (Ch. 90 §90.4.4) and to the readiness gate's growing fingerprint list (Appendix M.2).
- **Pattern analysis.** The same `manifest.txt` fields across bundles (mechanism, core, hour, venue, build) are a small table; "SMI on host 7 every day at 14:02" or "TLB shootdown from the risk process after every config reload" are visible only across incidents.
- **Scrub before sharing.** Bundles contain session credentials in `provenance/etc-trading.tgz` and possibly in the binary log (Ch. 84 §84.4.6: keep secrets out of logs — verify, do not assume), order and position data in the capture slice and the journal, and client identifiers. A bundle sent to a venue carries the venue-stamp series and the capture slice for *their* packets and nothing else; one sent to a vendor carries the kernel ring and `perf.data` and no capture. Script the redaction; do not do it by hand at 09:45.

## N.8 References

1. Linux kernel documentation: `Documentation/trace/ftrace.rst` (per-CPU `trace` files, `trace_clock`), `Documentation/admin-guide/sysctl/kernel.rst`, `Documentation/ABI/testing/sysfs-devices-system-cpu` (`isolated`, `nohz_full`).
2. `perf-archive(1)`, `perf-report(1)` (`--header-only`), `perf-script(1)`; `journalctl(1)` (`-o short-precise`, `--since=@`); `readelf(1)` (`-n`, `-p .GCC.command.line`); `ethtool(8)`; `turbostat(8)`; `rasdaemon(8)`/`ras-mc-ctl(8)`.
3. linuxptp: `pmc(8)`, `phc_ctl(8)`, `ptp4l(8)`, `phc2sys(8)`.
4. GCC manual, `-frecord-gcc-switches` (Appendix D.7); `debuginfod(8)` and the build-ID conventions of Ch. 5 §5.4.1.
5. Commission Delegated Regulation (EU) 2017/574 (RTS 25) and 2017/589 (RTS 6) — clock synchronisation and the record-keeping obligations that set the retention horizon (Appendix K.7).

## N.9 Additional Reading

- Google SRE, *Site Reliability Engineering*, Ch. 14–15 "Managing Incidents", "Postmortem Culture" — the organisational half this appendix leaves out.
- Gregg, B. *Systems Performance*, 2nd ed., Ch. 2 "Methodologies" — the drill-down and USE methods the first look follows.
- Allspaw, J. "Blameless PostMortems and a Just Culture", Etsy Code as Craft — why the bundle records evidence rather than conclusions.
- Red Hat, *sos report* documentation — the general-purpose ancestor of N.5; what it collects, and why a trading host needs its own bounded version.

---

*This completes Appendix N and the book. The chapters (Parts I–XIII) and Appendices A–N together form the full tutorial series — from the latency mindset (Ch. 1) through the end-to-end tick-to-trade case study (Ch. 92) — with the standing references (ARM port, alternative languages, system tuning, compiler flags, latency numbers, glossary, bibliography, feature matrix, tooling cookbook, market-structure primer, connectivity and physical layer, benchmark harness, the pre-open readiness gate, and the incident diagnostics bundle) that outlive any single chapter.*
