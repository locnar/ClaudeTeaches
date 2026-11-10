#!/usr/bin/env python3
"""renumber_history.py — the book's renumbering history as data, and the tool that answers
"this number was written when the book had C chapters: what is it called today?"

This file used to be stale_crossrefs.py, a heuristic that guessed stale references from title
keywords. It was RETIRED on 2026-11-10 (its renumber-chain heuristic had grown to ~270 rows of noise,
and its `selfref` rule flagged every legitimate cross-chapter `§N.M`). The checks are now

    tools/review/check_refs.py    does every reference resolve?           (deterministic; the primary check)
    tools/review/check_hints.py   does it resolve to the right topic?     (hint vs target, with a reviewed baseline)

What survives here is the part that was always exact: STEPS, the ordered list of renumberings
(tools/review/insert_chapter.py appends one line per insertion — keep the marker line), and two helpers
built on it.

usage
    renumber_history.py map N [--chapters C]
        Today's number of the chapter that was "Ch. N" when the book had C chapters on disk
        (C = 76 is the Phase-3 numbering; omit --chapters for the ORIGINAL 64-chapter drafting order,
        which goes through the Phase-3 reorganisation map first).
    renumber_history.py when FILE NEEDLE [N ...]
        Find the commit that first put NEEDLE into FILE (git pickaxe, following renames), report how many
        chapters existed then, and map each N from that numbering to today's. This is how a bare number
        that the insertion tool could not see ("Ch. 6 §6.4.4, 46, 82", a fenced table's chapter column, an
        external book's "Ch. 9" that was renumbered by mistake) is dated and repaired: the text is as old
        as the commit that introduced it, and every later insertion is in STEPS.
    renumber_history.py table [--chapters C]
        The whole old → new map from that numbering.
Exit code: 0, or 2 on bad usage / NEEDLE not found.
"""
import functools
import os
import re
import subprocess
import sys

PHASE3_CHAPTERS = 76    # chapters on disk right after the Phase-3 reorganisation; STEPS[1:] start from there


def phase3(n):
    """The Phase-3 reorganisation map (original drafting order -> twelve-Part order)."""
    if n <= 4: return n
    if n <= 37: return n + 1
    if n <= 41: return n + 2
    if n <= 45: return n + 3
    if n <= 47: return n + 4
    if n == 48: return 53
    if n == 49: return 55
    if n == 50: return 58
    if n <= 53: return n + 10
    if n <= 64: return n + 12
    return None
# Renumbering history, applied in order. ("phase3", None) is the reorganisation above;
# ("insert", N) means "a chapter was inserted after N, so every number > N moved up by one".
# tools/review/insert_chapter.py appends a step here on every insertion — keep the marker line.
STEPS = [
    ("phase3", None),
    ("insert", 45),  # Ch. 46 hunting-residual-noise-isolated-cores
    ("insert", 55),  # Ch. 56 multicast-group-management-dark-feed
    ("insert", 73),  # Ch. 74 zero-overhead-application-telemetry
    ("insert", 78),  # Ch. 79 host-health-telemetry
    ("insert", 79),  # Ch. 80 network-health-telemetry-drop-forensics
    ("insert", 73),  # Ch. 74 in-band-latency-tracing-budget-attribution
    ("insert", 78),  # Ch. 79 crash-handling-core-dumps-post-mortem
    ("insert", 80),  # Ch. 81 latency-spike-forensics-anomaly-triggered-diagnostics
    ("insert", 72),  # Ch. 73 hand-written-inline-assembly
    ("insert", 73),  # Ch. 74 rust-for-the-hot-path
    ("insert", 74),  # Ch. 75 language-interop-abi-boundaries
    ("insert", 75),  # Ch. 76 managed-runtimes-measured
    ("insert", 76),  # Ch. 77 choosing-a-language-per-latency-tier
    ("insert", 37),  # Ch. 38 threading-topologies-hot-loop
    ("insert", 38),  # Ch. 39 flow-control-backpressure-conflation
    ("insert", 43),  # Ch. 44 timers-deadlines-without-jitter
    ("insert", 86),  # Ch. 87 pre-trade-risk-gate-kill-switch
    ("insert", 87),  # Ch. 88 idempotent-order-entry-order-state-recovery
    ("insert", 90),  # Ch. 91 sequencer-total-order-replicated-consumers
    ("insert", 19),  # Ch. 20 compile-time-protocol-decoders
    ("insert", 21),  # Ch. 22 cost-of-modern-idioms-strings-without-allocation
    ("insert", 24),  # Ch. 25 build-systems-build-time-hygiene-hermetic-toolchains
    ("insert", 91),  # Ch. 92 order-entry-gateway-session-layer
    ("insert", 94),  # Ch. 95 line-rate-capture-timing-faithful-replay
    ("insert", 96),  # Ch. 97 failover-without-split-brain-leases-fencing
    ("insert", 100),  # Ch. 101 testing-latency-critical-code
    ("insert", 101),  # Ch. 102 performance-engineering-ci-profile-lifecycle
    ("insert", 102),  # Ch. 103 release-engineering-measuring-changes-in-production
    ("insert", 6),  # Ch. 7 choosing-clocking-hot-path-cpu
    ("insert", 17),  # Ch. 18 chiplets-tiles-numa-within-socket
    ("insert", 53),  # Ch. 54 power-management-frequency-determinism
    ("insert", 57),  # Ch. 58 anatomy-linux-network-datapath
    ("insert", 27),  # Ch. 28 static-analysis-performance-contracts
    ("insert", 31),  # Ch. 32 limit-order-book-engineering
    ("insert", 36),  # Ch. 37 streaming-statistics-incremental-signals
    ("insert", 67),  # Ch. 68 branch-free-parsing-formatting
    ("insert", 8),  # Ch. 9 dram-subsystem-below-l3
    ("insert", 83),  # Ch. 84 userspace-drivers-kernel-modules
    ("insert", 35),  # Ch. 36 fast-transcendentals-approximate-math
    ("insert", 37),  # Ch. 38 fast-hashing-checksums-key-design
    ("insert", 72),  # Ch. 73 multi-venue-book-normalization-symbology-sor
    ("insert", 85),  # Ch. 86 encrypted-web-protocol-venues-cloud
    ("insert", 106),  # Ch. 107 quoting-engine-market-making-mechanics
    ("insert", 116),  # Ch. 117 case-study-exchange-simulator
    # INSERT_STEPS_END
]


def old2new(n):
    """Original 64-chapter drafting number -> today's number (None if the chapter did not exist then)."""
    for kind, arg in STEPS:
        if n is None:
            return None
        if kind == "phase3":
            n = phase3(n)
        elif kind == "insert":
            n = n + 1 if n > arg else n
    return n


def from_count(n, chapters_then):
    """A number written when `chapters_then` chapters existed (>= 76) -> today's number."""
    done = chapters_then - PHASE3_CHAPTERS
    inserts = [arg for kind, arg in STEPS if kind == "insert"]
    if done < 0 or done > len(inserts):
        raise ValueError(f"no numbering with {chapters_then} chapters is recorded (76..{PHASE3_CHAPTERS + len(inserts)})")
    for arg in inserts[done:]:
        if n > arg:
            n += 1
    return n


def _git(root, *args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True).stdout


@functools.lru_cache(None)
def chapters_at(root, commit):
    names = _git(root, "ls-tree", "--name-only", commit, "chapters/").split("\n")
    return sum(1 for x in names if re.match(r"chapters/\d{2,3}-", x) and not x.startswith("chapters/00-"))


def first_commit(root, path, needle):
    out = _git(root, "log", "--follow", "--format=%H", "-S" + needle, "--", path).split()
    return out[-1] if out else None


def main(argv):
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if len(argv) >= 2 and argv[0] in ("map", "table"):
        c = None
        if "--chapters" in argv:
            c = int(argv[argv.index("--chapters") + 1])
        f = (lambda n: old2new(n)) if c is None else (lambda n: from_count(n, c))
        if argv[0] == "map":
            print(f(int(argv[1])))
            return 0
    if argv and argv[0] == "table":
        c = int(argv[argv.index("--chapters") + 1]) if "--chapters" in argv else None
        for n in range(1, (c or 64) + 1):
            print(f"{n}\t{old2new(n) if c is None else from_count(n, c)}")
        return 0
    if len(argv) >= 3 and argv[0] == "when":
        path, needle, nums = argv[1], argv[2], [int(x) for x in argv[3:]]
        commit = first_commit(root, path, needle)
        if not commit:
            print(f"needle not found in the history of {path}")
            return 2
        c = chapters_at(root, commit)
        print(f"introduced by {commit[:10]} ({_git(root, 'log', '-1', '--format=%ad %s', '--date=short', commit).strip()[:90]})")
        print(f"chapters on disk then: {c}")
        for n in nums:
            print(f"  {n} -> {from_count(n, c) if c >= PHASE3_CHAPTERS else old2new(n)}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
