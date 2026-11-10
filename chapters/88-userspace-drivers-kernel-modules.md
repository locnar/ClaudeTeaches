# Part XI — Heterogeneous Computing & Hardware Acceleration

# Chapter 88 — Userspace Drivers & Kernel Modules: UIO, VFIO and the Device Boundary

> **Prerequisites:** Ch. 87 (PCIe — BARs, posted writes, descriptor rings and doorbells, the IOMMU; this chapter is how user space *gets* the BAR and the DMA mapping Ch. 87 assumed), Ch. 34 (mmap — a BAR is a mapping of device memory), Ch. 17 (huge pages — DMA buffers and the IOTLB), Ch. 54 (interrupts vs polling), Ch. 101 (the IOMMU as a security boundary; capabilities), Ch. 67 (eventfd as a wake-up).
>
> **Leads into:** Ch. 82 (kernel bypass — what DPDK, `ef_vi` and `libexanic` do underneath is this chapter), Ch. 83 (RDMA — the verbs provider is a userspace driver with a kernel control plane), Ch. 91 (FPGA — the host side of a custom accelerator is a VFIO driver), Ch. 92 (SmartNICs), Appendix S (the same VFIO binding used to pass a device *into* a VM), Ch. 103 §103.4.9 (the unit file that re-binds the device at boot).

---

## 88.1 Why it matters: every bypass path stands on a userspace driver

Ch. 87 ended with a rule — *write descriptors to host memory, ring the doorbell with one posted write, poll the completion in host memory* — and a prerequisite it never discharged: "with a mapped device BAR (via a driver or DPDK/VFIO)". Every kernel-bypass stack in Part X (Ch. 82's PMDs and vendor APIs, Ch. 83's verbs) and every accelerator in Part XI (Ch. 91's FPGA, Ch. 92's DPU) is built on that step: a process that owns a PCIe function's registers and can hand it physical addresses to DMA to. Most shops consume it through a vendor library and never see it. You need to see it in three situations: when the device is *yours* (an FPGA you programmed — Ch. 91 — a timing card, a custom NIC); when the vendor's driver is the problem (an `ef_vi` or DPDK PMD doing something you cannot explain in its tail); and when you decide which of the three routes to the device a process should take, because they differ in **latency** (does a DMA go through an IOMMU translation? does a completion arrive as an eventfd read or a cache line?), in **safety** (can a bug in the strategy process scribble over the kernel's memory via the NIC?) and in **operability** (does the box survive the process crashing with the device mid-transfer?).

The chapter is the device boundary from the software side: the three routes to a BAR and a DMA mapping and what each costs (§88.2.1–§88.2.2), interrupt delivery to a process (§88.2.3), the decision between a userspace driver and a kernel module (§88.2.4); the measured cost of an eventfd completion against a polled one (§88.3); binding a device, a minimal complete VFIO driver in C++, the two-tier completion loop, the DMA-memory discipline and a kernel-module skeleton for the cases that need one (§88.4); and the pitfalls that turn "we bypass the kernel" into "we corrupted the kernel" (§88.5).

## 88.2 Mental model

### 88.2.1 Three routes to the device: `resourceN`, UIO, VFIO

A PCIe function is a set of BARs (Ch. 87 §87.2.2), a config space, and (for anything fast) MSI-X vectors. Linux offers user space three ways to reach them:

| Route | How you get the BAR | DMA | Interrupts | IOMMU | Who uses it |
|---|---|---|---|---|---|
| **sysfs `resourceN`** — `mmap` of `/sys/bus/pci/devices/<BDF>/resource0` (`resource0_wc` for write-combining) | one `open` + `mmap`; no driver bound | **none** — you have no physical addresses the device may use (and no way to pin pages) unless you add `/proc/<pid>/pagemap` tricks that break under the IOMMU | none (`/sys/.../irq` is the legacy line; no MSI-X) | not involved: the device DMAs to whatever physical address you give it | register pokes, firmware tools, Ch. 87 §87.3's `mmio_lat`; **never a production data plane** |
| **UIO** (`uio_pci_generic`, `igb_uio`) — a tiny kernel driver that exports the BARs as `/dev/uioN` mmap regions and the interrupt as a blocking `read()` | `mmap(/dev/uio0, offset = N × page)` per BAR | **none in the kernel's eyes**: the process computes physical addresses itself (hugepages + `pagemap`) and gives them to the device — fine *without* an IOMMU, broken or unsafe with one (`igb_uio` needs `iommu=pt`/off) | one legacy/INTx line, delivered as a 4-byte `read()` on the fd; no MSI-X vectors | bypassed | DPDK on old or IOMMU-less boxes; `igb_uio` is out-of-tree and deprecated |
| **VFIO** (`vfio-pci`) — the kernel's framework for *safe* userspace drivers | `ioctl(VFIO_DEVICE_GET_REGION_INFO)` then `mmap` the device fd at the region's offset | **first-class**: `VFIO_IOMMU_MAP_DMA` pins the pages and programs the IOMMU so the device can reach *only* them, at an IOVA you choose | **per-vector MSI-X**, each wired to an eventfd (`VFIO_DEVICE_SET_IRQS`); INTx/MSI too | required (Type1 IOMMU; `vfio-pci` with `enable_unsafe_noiommu_mode=1` exists for IOMMU-less boxes and is what its name says) | DPDK, SPDK, QEMU device passthrough (Appendix S), your FPGA driver (§88.4.2) |

The one-line verdict: **VFIO for anything that DMAs; `resourceN` for register-only tools; UIO only where there is no IOMMU and you accept that a bug in your process is a bug with DMA rights to all of memory.** VFIO's cost is a few microseconds *per setup call* (mapping, binding) and whatever the IOMMU adds *per DMA translation* (§88.2.2) — nothing per doorbell or per completion poll, which are plain memory operations once the mappings exist.

### 88.2.2 IOMMU groups, ACS, and the DMA mapping

The IOMMU (Ch. 87 §87.2.2) translates the addresses a device puts on the bus (**IOVAs**) to physical addresses, and refuses the ones it has no mapping for. VFIO builds on it with three concepts:

- **The IOMMU group** is the smallest set of devices the IOMMU can isolate from each other — every device in a group shares a translation domain, so VFIO hands out *groups*, not devices, and a group is **viable** only when all of its devices are bound to `vfio-pci` (or to nothing). What decides the grouping is the PCIe topology: devices behind a switch or a multi-function card land in one group unless **ACS** (Access Control Services) on every bridge in the path guarantees that peer-to-peer traffic between them is routed up through the IOMMU rather than short-cut inside the switch. A server-class NIC in its own root-port slot is usually alone in its group; a dual-port card may be one group (both ports go to the same process) or two (per-function ACS); consumer-grade topology lumps half the box together. `find /sys/kernel/iommu_groups/ -type l` prints the map; `lspci -vv | grep -A3 ACSCap` shows which bridges support it. There is an out-of-tree "ACS override" patch — it lies to the kernel about isolation, and a trading box does not run it.
- **The DMA map** is the process telling VFIO "these virtual pages may be DMA'd, call them IOVA *X*": `VFIO_IOMMU_MAP_DMA{vaddr, iova, size}` pins the pages (so they cannot be swapped, migrated, or compacted away — the guarantee `mlock` does *not* give, Ch. 34 §34.4.2) and installs the IOMMU page-table entries. The IOVA is yours to choose; most drivers make IOVA = VA (simple; the device sees the same addresses the code does) or IOVA = a dense range starting at 0 (fewer IOMMU page-table levels, smaller IOTLB footprint). Both DPDK modes exist (`--iova-mode va|pa`).
- **The IOTLB** is the device-side TLB: each DMA to an unmapped IOVA page walks the IOMMU page table (~hundreds of ns, overlapped with the DMA). Map DMA memory from **huge pages** (2 MiB or 1 GiB) so the IOMMU can use large entries — the Ch. 17 argument, one level down — and keep the DMA footprint small and hot. The alternative, **`iommu=pt`** (passthrough: identity-map trusted kernel-driver devices, still isolate VFIO ones), is a kernel-wide choice that Ch. 87 §87.5 and Appendix C.2 discuss; with VFIO, the device *always* goes through the IOMMU, which is the point.

### 88.2.3 Interrupts to a process: eventfds, INTx and the two tiers

A device signals completion either by writing a completion word to host memory (the Ch. 87 §87.2.3 pattern) or by raising an interrupt. In user space an interrupt becomes a **file-descriptor event**: UIO delivers its one line as a readable `/dev/uioN` (and requires you to re-enable the line by writing 1 to it — one syscall per interrupt); VFIO wires each MSI-X vector to an **eventfd** you supply (`VFIO_DEVICE_SET_IRQS` with `VFIO_IRQ_SET_DATA_EVENTFD`), so a driver can `epoll` sixty-four vectors in one thread. The path is: device → MSI-X write → the kernel's VFIO interrupt handler → `eventfd_signal` → the sleeping thread is woken → it returns from `read()`/`epoll_wait()` — a hardware IRQ, a kernel wake-up and a context switch (Ch. 54), several microseconds, with the cold-cache aftermath Ch. 54 §54.3 measured. **No hot-path completion is ever consumed this way.** The hot tier polls the completion word in host memory from a dedicated core (Ch. 87 §87.4.2); the eventfd tier exists for the *idle* state of the box — overnight, pre-open, a disarmed strategy — when burning a core to poll an idle device is wasteful, and for error/link/reset vectors that fire once a week. §88.4.3 shows the loop that switches between the two without losing a completion.

### 88.2.4 When to go kernel-side

A userspace driver owns one function exclusively, from one process, with polling as its fast path. Four situations argue for a kernel module instead (or as well):

1. **The device is shared.** Several processes need the same function (one physical NIC feeding a feed handler *and* an order gateway with different queues); a kernel driver can multiplex queues and enforce ownership, which is exactly what the vendor's "VI"/"queue pair" model does (Ch. 82 §82.4.4, Ch. 83): a **kernel control plane** that allocates queues, maps rings and doorbell pages into each process, and a **userspace data plane** per process that never syscalls. SR-IOV virtual functions (Appendix S) are the hardware version of the same split.
2. **The device needs the kernel's subsystems.** A NIC that must also be a `netdev` for the control-plane traffic (IGMP joins — Ch. 74, PTP — Ch. 78, management), a storage device that should be a block device for the journal (Ch. 109), a device that participates in power management or hot-plug.
3. **Interrupts are the design.** A device that interrupts thousands of times a second (not a trading data plane, but a capture card at line rate, Ch. 110) is better served by a kernel handler that batches into a ring the process polls than by eventfd wake-ups.
4. **Crash containment.** A userspace driver that dies mid-DMA leaves the device running: VFIO resets the function when the fd closes (`VFIO_DEVICE_RESET` / FLR on release), which is good; a kernel driver can do an orderly quiesce and keep the link state. Either way the Ch. 103 §103.4.4 kill-switch must not depend on the dead process's device.

The model: **user space reaches a device by mapping its BARs (three routes, VFIO being the safe one), gets DMA rights by pinning and IOMMU-mapping its own memory at IOVAs it chose, and receives interrupts as eventfds it never reads on the hot path. The IOMMU group is the unit of ownership, ACS decides its size, and a kernel module is the control plane for shared, interrupt-heavy or subsystem-integrated devices — with the data plane still polled from user space.**

## 88.3 Measure it: an eventfd completion vs a polled one

*Toolchain and machine: Appendix L.3. The eventfd/polling run below is on the 4-vCPU KVM guest (kernel 6.18) because the guest exposes no VFIO device; the VFIO setup costs are representative for the reference machine with an Intel E810 / a Xilinx Alveo under `vfio-pci`.*

The cost that decides §88.2.3's two tiers is the wake-up. Two pinned threads play device and driver: the "device" signals a completion, the "driver" consumes it, and acknowledges; one-way latency is half the round trip, for the three delivery mechanisms a VFIO driver can choose between:

```cpp
// eventfd_rtt.cpp — what an MSI-X-vector-as-eventfd costs the consumer: a blocking eventfd read vs polling a host-memory word.
// Build: g++ -O2 -std=c++20 -pthread eventfd_rtt.cpp -o eventfd_rtt     Run: ./eventfd_rtt <cpuA> <cpuB>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <sched.h>
#include <sys/epoll.h>
#include <sys/eventfd.h>
#include <thread>
#include <unistd.h>
#include <x86intrin.h>
static void pin(int cpu) { cpu_set_t s; CPU_ZERO(&s); CPU_SET(cpu, &s); sched_setaffinity(0, sizeof s, &s); }
int main(int argc, char** argv) {
    int a = std::atoi(argv[1]), b = std::atoi(argv[2]); const int N = 20000; std::uint64_t v;
    // (1) eventfd ping-pong: thread B is the "device" writing the completion vector; A blocks in read().
    int efd = eventfd(0, EFD_CLOEXEC), back = eventfd(0, EFD_CLOEXEC);
    std::thread dev([&] { pin(b); std::uint64_t w; for (int i = 0; i < N; ++i) { eventfd_write(efd, 1); (void)!read(back, &w, 8); } });
    pin(a); auto t0 = std::chrono::steady_clock::now();
    for (int i = 0; i < N; ++i) { (void)!read(efd, &v, 8); eventfd_write(back, 1); }
    double ev = std::chrono::duration<double, std::nano>(std::chrono::steady_clock::now() - t0).count() / N / 2;
    dev.join();
    // (2) the same with epoll_wait in front of the read (how a driver loop with many vectors is written)
    int ep = epoll_create1(0); epoll_event e{EPOLLIN, {}}; epoll_ctl(ep, EPOLL_CTL_ADD, efd, &e);
    std::thread dev2([&] { pin(b); std::uint64_t w; for (int i = 0; i < N; ++i) { eventfd_write(efd, 1); (void)!read(back, &w, 8); } });
    t0 = std::chrono::steady_clock::now();
    for (int i = 0; i < N; ++i) { epoll_event out; epoll_wait(ep, &out, 1, -1); (void)!read(efd, &v, 8); eventfd_write(back, 1); }
    double ep_ns = std::chrono::duration<double, std::nano>(std::chrono::steady_clock::now() - t0).count() / N / 2;
    dev2.join();
    // (3) polling a completion word in host memory (the Ch. 87 §87.2.3 pattern) between the same two cores
    alignas(64) static std::atomic<std::uint64_t> seq{0}; alignas(64) static std::atomic<std::uint64_t> ack{0};
    std::thread dev3([&] { pin(b); for (std::uint64_t i = 1; i <= N; ++i) { seq.store(i, std::memory_order_release); while (ack.load(std::memory_order_acquire) != i) _mm_pause(); } });
    t0 = std::chrono::steady_clock::now();
    for (std::uint64_t i = 1; i <= N; ++i) { while (seq.load(std::memory_order_acquire) != i) _mm_pause(); ack.store(i, std::memory_order_release); }
    double poll = std::chrono::duration<double, std::nano>(std::chrono::steady_clock::now() - t0).count() / N / 2;
    dev3.join();
    std::printf("cpus %d<->%d, one-way: eventfd read %.0f ns; epoll_wait+read %.0f ns; host-memory poll %.0f ns\n", a, b, ev, ep_ns, poll);
}
```

```
   $ ./eventfd_rtt 2 3            # guest, g++ 13.3 -O2 (clang++ 18.1: same within noise), two runs
   cpus 2<->3, one-way: eventfd read 11629 ns; epoll_wait+read 10978 ns; host-memory poll 203 ns
   cpus 2<->3, one-way: eventfd read 11433 ns; epoll_wait+read 11687 ns; host-memory poll  86 ns
```

- **The eventfd path is ~11 µs one-way in the guest** — a blocked thread's wake-up is a `HLT` exit, a host-side IPI and a VM entry (Appendix S §S.2); on bare metal the same path is ~2–4 µs (a hardware IRQ into the VFIO handler, `eventfd_signal`, a wake-up and a context switch — Ch. 54 §54.3's numbers, plus the cold-cache aftermath). `epoll_wait` in front of it adds nothing measurable; it is the sleep that costs.
- **The polled completion is 86–203 ns** — one cache-line transfer between two cores (Appendix E's HITM row; on the reference machine ~40–100 ns depending on the core pair — Ch. 19 §19.3). A device writing the completion by DMA lands it in L3 (DDIO — Ch. 76) or DRAM, so the real number is a Ch. 76 figure, but it is a *memory read*, two orders of magnitude under the wake-up.
- **The VFIO setup calls are one-time and slow, which is fine:** representative on the reference machine, `VFIO_GROUP_GET_DEVICE_FD` ~50 µs; `VFIO_IOMMU_MAP_DMA` of a 1 GiB hugepage-backed region ~1–2 ms (pinning 512 2 MiB pages and installing their IOMMU entries; ~0.3 s for the same gigabyte in 4 KiB pages — the IOTLB argument in numbers); `VFIO_DEVICE_SET_IRQS` ~20 µs per vector; `VFIO_DEVICE_RESET` (an FLR) 100 ms–1 s. All of it belongs before the readiness gate (Appendix M), none of it in the session.

The lesson is the one Ch. 87 §87.4.2 stated and this measures: **a completion you wait for costs a context switch; a completion you poll for costs a cache line.** The eventfd exists so the box can sleep when nothing is happening — never so the hot path can.

## 88.4 Techniques

### 88.4.1 Binding the device, and checking the group is yours

Before any code: move the function from its kernel driver to `vfio-pci`, and confirm the IOMMU group is viable. Persistently — the Ch. 103 §103.4.9 unit does this at boot, before the trading unit starts:

```
   # the device, its kernel driver, its IOMMU group and the group's other members
   $ lspci -nn -s 3b:00.0                         # vendor:device ids; -vv adds LnkSta (width/speed), NUMA node, ACS
   $ readlink /sys/bus/pci/devices/0000:3b:00.0/driver                       # -> .../ice   (currently a kernel netdev)
   $ readlink /sys/bus/pci/devices/0000:3b:00.0/iommu_group                  # -> .../iommu_groups/42
   $ ls /sys/kernel/iommu_groups/42/devices/                                 # every BDF here must go to vfio-pci (or nothing)
   $ cat /sys/bus/pci/devices/0000:3b:00.0/numa_node                         # the node the driver thread must live on (Ch. 18)
   # bind: driverctl is the persistent, distro-neutral way (writes driver_override + rebinds; survives reboot via udev)
   $ driverctl set-override 0000:3b:00.0 vfio-pci                             # or: dpdk-devbind.py -b vfio-pci 0000:3b:00.0
   $ driverctl list-overrides                                                 # verify at the gate (Appendix M)
   $ ls -l /dev/vfio/                                                         # /dev/vfio/vfio (the container) and /dev/vfio/42 (the group)
   # the process needs: rw on /dev/vfio/42 (a udev rule + a group), RLIMIT_MEMLOCK >= the DMA memory (Ch. 103 §103.4.9: LimitMEMLOCK=)
   # and, if not root, CAP_SYS_RAWIO is NOT needed — that is the point of VFIO; kernel 6.x also wants vfio.enable_unsafe_noiommu_mode=0
```

Three checks that save a morning: the group's *other* members (a dual-port NIC's second port, a card's management function) must also be unbound or the group is not viable (`VFIO_GROUP_GET_STATUS` reports it); the device's `numa_node` decides where the driver thread and its DMA memory go (Ch. 18 §18.4.1, Ch. 19 §19.4.3); and `lspci -vv`'s `LnkSta` must show the width and speed you paid for (Ch. 87 §87.5) — a card negotiated down to x4/Gen3 is a silent bandwidth bug and is also the first thing a VFIO driver cannot fix.

### 88.4.2 A minimal VFIO driver in C++

The whole protocol, for a doorbell/completion device (an FPGA with a ring in host memory, or any NIC at the level under its PMD). Everything device-specific is three register offsets; everything else is VFIO:

```cpp
// vfio_min.cpp — a minimal user-space driver over VFIO: open the IOMMU group, get the device, map BAR0, map a
// hugepage-backed ring for DMA, wire MSI-X vector 0 to an eventfd, ring the doorbell, poll the completion in host memory.
// Build: g++ -O2 -std=c++20 vfio_min.cpp -o vfio_min      (builds under g++ 13.3 and clang++ 18.1 against linux/vfio.h)
// Run (root, after `driverctl set-override 0000:3b:00.0 vfio-pci`): ./vfio_min /dev/vfio/42 0000:3b:00.0
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <linux/vfio.h>
#include <sys/eventfd.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

struct Dev { int container = -1, group = -1, device = -1; volatile std::uint32_t* bar0 = nullptr; std::size_t bar0_len = 0; };
static bool die(const char* what) { std::fprintf(stderr, "%s: %s\n", what, std::strerror(errno)); return false; }

static bool open_device(Dev& d, const char* group_path, const char* bdf) {
    d.container = open("/dev/vfio/vfio", O_RDWR);                       if (d.container < 0) return die("open /dev/vfio/vfio");
    if (ioctl(d.container, VFIO_GET_API_VERSION) != VFIO_API_VERSION)   return die("VFIO_API_VERSION");
    if (!ioctl(d.container, VFIO_CHECK_EXTENSION, VFIO_TYPE1_IOMMU))    return die("no Type1 IOMMU");
    d.group = open(group_path, O_RDWR);                                 if (d.group < 0) return die("open group");
    vfio_group_status st{sizeof st, 0}; ioctl(d.group, VFIO_GROUP_GET_STATUS, &st);
    if (!(st.flags & VFIO_GROUP_FLAGS_VIABLE)) { std::fprintf(stderr, "group not viable: another device in the IOMMU group is still bound to a kernel driver (ACS?)\n"); return false; }
    if (ioctl(d.group, VFIO_GROUP_SET_CONTAINER, &d.container))         return die("SET_CONTAINER");
    if (ioctl(d.container, VFIO_SET_IOMMU, VFIO_TYPE1_IOMMU))           return die("SET_IOMMU");
    d.device = ioctl(d.group, VFIO_GROUP_GET_DEVICE_FD, bdf);            if (d.device < 0) return die("GET_DEVICE_FD");
    // BAR0: ask where it is in the device fd's address space, then mmap it. A write to it is a posted MMIO write (Ch. 87 §87.2.3).
    vfio_region_info ri{}; ri.argsz = sizeof ri; ri.index = VFIO_PCI_BAR0_REGION_INDEX;
    if (ioctl(d.device, VFIO_DEVICE_GET_REGION_INFO, &ri))              return die("GET_REGION_INFO");
    if (!(ri.flags & VFIO_REGION_INFO_FLAG_MMAP)) { std::fprintf(stderr, "BAR0 is not mmap-able (sub-page BAR? use pread/pwrite on the fd)\n"); return false; }
    void* p = mmap(nullptr, ri.size, PROT_READ | PROT_WRITE, MAP_SHARED, d.device, ri.offset);
    if (p == MAP_FAILED)                                                 return die("mmap BAR0");
    d.bar0 = static_cast<volatile std::uint32_t*>(p); d.bar0_len = ri.size;
    return true;
}
// DMA memory: hugepage-backed, pre-faulted, mapped into the IOMMU at the IOVA of our choosing (here IOVA == VA).
static void* map_dma(Dev& d, std::size_t len) {
    void* va = mmap(nullptr, len, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_HUGETLB | MAP_POPULATE, -1, 0);
    if (va == MAP_FAILED) { die("mmap HUGETLB (reserve hugepages: Ch. 17)"); return nullptr; }
    vfio_iommu_type1_dma_map m{}; m.argsz = sizeof m; m.flags = VFIO_DMA_MAP_FLAG_READ | VFIO_DMA_MAP_FLAG_WRITE;
    m.vaddr = reinterpret_cast<std::uint64_t>(va); m.iova = m.vaddr; m.size = len;
    if (ioctl(d.container, VFIO_IOMMU_MAP_DMA, &m)) { die("IOMMU_MAP_DMA"); return nullptr; }   // pins the pages, programs the IOMMU
    return va;
}
// MSI-X: one eventfd per vector. The hot path never reads it (§88.4.3) — it polls host memory; the eventfd is for the idle tier.
static int msix_eventfd(Dev& d, unsigned vector) {
    int efd = eventfd(0, EFD_CLOEXEC);
    alignas(8) char buf[sizeof(vfio_irq_set) + sizeof(int)]; auto* s = reinterpret_cast<vfio_irq_set*>(buf);
    s->argsz = sizeof buf; s->flags = VFIO_IRQ_SET_DATA_EVENTFD | VFIO_IRQ_SET_ACTION_TRIGGER; s->index = VFIO_PCI_MSIX_IRQ_INDEX; s->start = vector; s->count = 1;
    std::memcpy(s->data, &efd, sizeof efd);
    if (ioctl(d.device, VFIO_DEVICE_SET_IRQS, s)) { die("SET_IRQS (MSI-X)"); return -1; }
    return efd;
}
int main(int argc, char** argv) {
    if (argc < 3) { std::fprintf(stderr, "usage: %s /dev/vfio/<group> <BDF>\n", argv[0]); return 2; }
    Dev d; if (!open_device(d, argv[1], argv[2])) return 1;
    constexpr std::size_t RING = 2u << 20;
    auto* ring = static_cast<volatile std::uint64_t*>(map_dma(d, RING)); if (!ring) return 1;
    int efd = msix_eventfd(d, 0); (void)efd;
    // The device-specific part (register offsets are the device's): post a descriptor, ring the doorbell, poll the completion.
    constexpr std::size_t DOORBELL = 0x40 / 4, RING_BASE_LO = 0x10 / 4, RING_BASE_HI = 0x14 / 4;     // placeholders for a real datasheet
    std::uint64_t iova = reinterpret_cast<std::uint64_t>(ring);
    d.bar0[RING_BASE_LO] = std::uint32_t(iova); d.bar0[RING_BASE_HI] = std::uint32_t(iova >> 32);   // posted writes
    ring[0] = 0x1ull;                                 // descriptor 0: "do one thing"
    ring[RING / 8 - 1] = 0;                           // completion word at the end of the ring, device writes it by DMA
    asm volatile("sfence" ::: "memory");              // descriptor must be globally visible before the doorbell (Ch. 87 §87.4.1)
    d.bar0[DOORBELL] = 1;                             // one posted write: ~tens of ns on the CPU side
    while (ring[RING / 8 - 1] == 0) __builtin_ia32_pause();   // poll HOST memory, never the device register (Ch. 87 §87.2.3)
    std::printf("completion observed in host memory\n");
}
```

What the listing establishes, in order: the **container** (an IOMMU domain), the **group** (ownership — viable only if every member is unbound), the **device fd** (regions and IRQs are indexed through it), the **BAR mapping** (uncached by default; a write-combining mapping of a doorbell region needs the device's `VFIO_REGION_INFO_CAP_SPARSE_MMAP`/WC capability or a `resource0_wc`-style sub-mapping — most doorbells are fine UC, Ch. 87 §87.4.1), the **DMA map** (the only place physical memory is granted to the device — note the ring is hugepage-backed *and* pre-faulted *before* mapping; `MAP_DMA` would otherwise fault it in 4 KiB at a time), and the **vector**. The hot loop at the end is the Ch. 87 pattern verbatim: posted writes, `sfence`, doorbell, poll. Teardown is `close()` in reverse order; VFIO unmaps and resets the function.

### 88.4.3 The two-tier completion loop

The hot core polls; the idle box sleeps. The switch between the tiers must not lose a completion that lands between "I checked the ring" and "I went to sleep" — the same lost-wake-up race as a futex-backed queue (Ch. 45 §45.5, Ch. 54 §54.4.2), solved the same way, with the device's interrupt mask as the second check:

```cpp
// Driver loop: poll while armed; when the strategy disarms, enable the MSI-X vector and sleep on its eventfd.
// Device contract (typical): writing 1 to IRQ_ENABLE arms the vector; a completion write *after* arming raises it.
void run(Dev& d, volatile std::uint64_t* cq, int efd, std::atomic<bool>& armed) {
    std::uint64_t next = 1;
    for (;;) {
        if (armed.load(std::memory_order_relaxed)) {                                 // hot tier: a dedicated isolated core (Ch. 56, 59)
            while (cq[next & MASK] != next) { if (!armed.load(std::memory_order_relaxed)) break; __builtin_ia32_pause(); }
            if (cq[next & MASK] == next) { handle(next); ++next; continue; }
        }
        d.bar0[IRQ_ENABLE] = 1;                                                     // idle tier: arm, then re-check (the second look)
        asm volatile("sfence" ::: "memory");
        if (cq[next & MASK] == next) { d.bar0[IRQ_ENABLE] = 0; continue; }           // it landed in the gap: do not sleep
        std::uint64_t n; (void)!read(efd, &n, sizeof n);                             // block (an epoll over all vectors in a real driver)
        d.bar0[IRQ_ENABLE] = 0;                                                     // back to polling; the vector stays wired
    }
}
```

The hot tier never touches `efd` and never writes `IRQ_ENABLE`; the idle tier pays the §88.3 wake-up and a non-posted-free register write per transition. Re-arming is between the poll and the sleep, not after the wake — the order that cannot miss. Pre-open, the readiness gate (Appendix M) flips `armed` and the loop is on its core, polling, before the first packet.

### 88.4.4 DMA memory: pinned, huge, local, and few mappings

The rules for the memory a device may touch (the §88.2.2 mechanics made into a checklist):

- **Allocate from huge pages** (`MAP_HUGETLB`, 1 GiB where the device's DMA footprint allows — Ch. 17 §17.4.1) and **pre-fault** before `MAP_DMA` (`MAP_POPULATE` / `MADV_POPULATE_WRITE` — Ch. 34 §34.4.3). The IOMMU gets large entries; the IOTLB stays warm.
- **Map once, at setup; never unmap in the session.** Each `MAP_DMA`/`UNMAP_DMA` is a page-table change plus an IOTLB flush — the device-side analogue of the TLB shootdown Ch. 34 §34.5 measures. A ring's buffers are a fixed pool mapped as one region, recycled by index (Ch. 31's pool), never allocated per packet.
- **NUMA-local to the device** (`numa_node` from §88.4.1; `numactl --membind` or `mbind` on the region — Ch. 18 §18.4.1). A DMA to the other socket crosses UPI twice (write, then the poll's read).
- **One region, dense IOVAs.** Give the device one contiguous IOVA range (IOVA = VA is simplest; a 0-based dense range shrinks the IOMMU table) rather than many small mappings — fewer entries, fewer IOTLB misses, one `UNMAP` at teardown.
- **`RLIMIT_MEMLOCK` covers it.** VFIO charges pinned pages against the process's locked-memory limit; the unit file sets `LimitMEMLOCK=infinity` (Ch. 103 §103.4.9) or the exact budget — `MAP_DMA` fails with `ENOMEM` otherwise, at startup, where you want it.

### 88.4.5 When the answer is a kernel module

For the §88.2.4 cases, the shape that keeps the hot path in user space is the **control-plane module, data-plane process** split — the design of every vendor stack. The module owns the PCI function (probe/remove, reset, link, interrupts), carves the device into queues, and exposes to each process *only* its queue's doorbell page and its ring memory through a character device's `mmap`; the process polls its ring like §88.4.2 and never calls the module again. The skeleton (not built here — the test guest has no kernel headers — and abbreviated to the parts that decide latency; see LDD3 and `Documentation/driver-api/` for the rest):

```c
// qdev.c — control-plane module: own the function, hand each process one queue's doorbell page + ring via mmap. (kernel 6.x APIs)
static int qdev_probe(struct pci_dev *pdev, const struct pci_device_id *id) {
    pci_enable_device(pdev); pci_set_master(pdev);
    dma_set_mask_and_coherent(&pdev->dev, DMA_BIT_MASK(64));
    q->bar0 = pci_iomap(pdev, 0, 0);                                            // the module keeps the BAR; processes get PAGES of it
    q->ring = dma_alloc_coherent(&pdev->dev, RING_BYTES, &q->ring_dma, GFP_KERNEL);   // IOMMU-mapped by the kernel's DMA API
    pci_alloc_irq_vectors(pdev, 1, NVEC, PCI_IRQ_MSIX);
    request_irq(pci_irq_vector(pdev, 0), qdev_isr, 0, "qdev", q);                // error/link vector only: data completions are polled
    return misc_register(&q->misc);                                             // /dev/qdev0
}
static int qdev_mmap(struct file *f, struct vm_area_struct *vma) {              // offset 0: this queue's doorbell page (UC); 1: the ring
    if (vma->vm_pgoff == 0) { vma->vm_page_prot = pgprot_noncached(vma->vm_page_prot);
        return io_remap_pfn_range(vma, vma->vm_start, (pci_resource_start(q->pdev, 0) + q->db_off) >> PAGE_SHIFT, PAGE_SIZE, vma->vm_page_prot); }
    return dma_mmap_coherent(&q->pdev->dev, vma, q->ring, q->ring_dma, RING_BYTES);   // the ring, cached, shared with the device
}
```

Two properties to insist on: the process's fast path must be **ioctl-free** (every per-message interaction is a store to the mapped doorbell page or a load from the ring), and the module must **not** register a NAPI/netdev receive path for the hot queues (that would put the Ch. 64 softirq machinery back on the data plane). A `netdev` for the *control* queues (IGMP, ARP, PTP) and a polled userspace data plane on the others is exactly the Onload/`ef_vi` and ExaNIC architecture (Ch. 82 §82.4.4) — if you are writing it yourself, you are re-deriving it, and the vendor's design is the reference.

## 88.5 Pitfalls & anti-patterns: a DMA bug is a kernel bug

- **`resourceN` or UIO as a production data plane.** Without an IOMMU mapping the device DMAs to whatever physical address the process computed from `pagemap`; a stale page (migrated, compacted, THP-collapsed — Ch. 34 §34.4.3) or an off-by-one in the ring is the device writing into the kernel, silently, until the box panics at 10:02. VFIO (§88.2.1); `enable_unsafe_noiommu_mode` only on a box with no IOMMU and a documented acceptance.
- **The non-viable group.** `VFIO_GROUP_SET_CONTAINER` fails (or `GET_STATUS` says not viable) because the NIC's second port, or the card's management function, is still bound to its kernel driver — the IOMMU group is the unit, not the BDF (§88.2.2). Unbind the whole group; if the group is half the box, the slot/bridge lacks ACS and the fix is hardware (a different slot or card), not the ACS-override patch.
- **DMA memory that is not pinned the way you think.** `mlock` keeps a page resident; it does not stop the kernel from *moving* it (compaction, NUMA balancing, THP collapse), and a moved page's old physical address is now someone else's memory. Only the DMA map pins (VFIO `MAP_DMA`, `ibv_reg_mr`, `cudaHostRegister`) — and only for the pages it covered (§88.4.4).
- **4 KiB DMA mappings at scale.** A packet pool mapped page by page is a million IOMMU entries, an IOTLB that never hits, and ~0.3 s of `MAP_DMA` per gigabyte at startup; a 1 GiB-backed single region is a handful of entries and ~1 ms (§88.3). The device-side TLB argument is the same as Ch. 17's.
- **Reading the eventfd on the hot path.** An MSI-X completion is a wake-up — ~2–4 µs bare metal, ~11 µs in a guest (§88.3) — plus the cold restart. Poll the completion word from the isolated core; the eventfd is the idle tier (§88.4.3), and the arm/re-check order is what keeps the switch lossless.
- **The doorbell write that is not a doorbell write.** A BAR mapped write-back (a kernel module that forgot `pgprot_noncached`) lets the CPU hold the doorbell in cache; a mapping that is WC without the `sfence` lets the doorbell overtake the descriptor (Ch. 87 §87.4.1, §87.5). UC for doorbells, WC + fence for bulk descriptor pushes.
- **A device reset in the session.** `VFIO_DEVICE_RESET`, a driver unbind, or closing the device fd triggers an FLR — 100 ms to a second of the link down, every queue gone. Open at startup, hold the fds for the process lifetime, and make the restart path (Ch. 103 §103.4.4) re-open rather than reset where the device allows it.
- **Binding that does not survive a reboot or a kernel update.** A `dpdk-devbind` run by hand is gone at the next boot, and the NIC comes up as a kernel `netdev` with `irqbalance` spraying its vectors onto the hot cores (Ch. 56). `driverctl`'s persistent override, or a `vfio-pci.ids=` cmdline entry, and the Appendix M gate checking `driverctl list-overrides`.
- **Lockdown and Secure Boot.** Under kernel lockdown (Ch. 101 §101.4.9) `resourceN` mmap and `/dev/mem` are refused (`iomem=relaxed` is ignored), unsigned modules will not load, and `CAP_SYS_RAWIO` means nothing — VFIO is the route that *does* work under lockdown, because it is safe by construction. Find this out in the lab, not during the kernel upgrade.
- **The driver thread on the wrong node.** The device sits on one socket's root complex (`numa_node`); a driver thread and its ring on the other pay UPI on every descriptor write and every completion poll (Ch. 18 §18.5, Ch. 87 §87.5). Place by `numa_node` — Ch. 19 §19.4.3's rule.

## 88.6 Exercises & checklist

**Exercises**

1. **Map the groups.** On your box, print every IOMMU group and its members, and for each hot-path device find the bridges between it and the root port with `lspci -t` and their `ACSCtl`. Is each NIC/FPGA alone in its group? If not, what would have to move?
2. **The cost of a wake-up.** Build `eventfd_rtt` and run it on an isolated core pair on bare metal. Record the three one-way numbers; then run it with the consumer's core *not* isolated. How much did the eventfd path move; how much did the poll?
3. **Bring up a function under VFIO.** Take an idle NIC port (or an FPGA with a scratch register), bind it with `driverctl`, and extend `vfio_min` to read its vendor/device id from the config region (`VFIO_PCI_CONFIG_REGION_INDEX`, `pread` at offset 0). Then time `MAP_DMA` for 1 GiB backed by 4 KiB, 2 MiB and 1 GiB pages (§88.3).
4. **Lose a completion on purpose.** Remove the re-check after arming in §88.4.3's loop and drive the device (or a simulator thread writing the ring) at a rate that lands completions in the gap. Measure how long the loop sleeps before the next completion rescues it; then restore the re-check and confirm the hang is gone.
5. **The split design.** Read your vendor's bypass library (`ef_vi`, `libexanic`, or a DPDK PMD) and identify its control plane (module, ioctls, queue allocation) and data plane (mapped doorbell, polled ring). Which calls could ever execute on the hot path? Instrument them (`perf trace` on the process — Ch. 54) during a replay to prove none do.

**Checklist — the device boundary from user space**

- [ ] Every device that DMAs is reached through **VFIO** (IOMMU-mapped, pinned by `MAP_DMA`), not `resourceN`/UIO physical-address tricks; `enable_unsafe_noiommu_mode` is off (§88.2.1, §88.5).
- [ ] The device's **IOMMU group is viable** and contains only what the process owns; ACS on the path is verified, no override patch (§88.2.2, §88.4.1).
- [ ] The binding is **persistent** (`driverctl` / `vfio-pci.ids=`) and **verified at the gate**; the device's `numa_node` decides the driver thread's and ring's placement (§88.4.1, Appendix M).
- [ ] DMA memory is **hugepage-backed, pre-faulted, NUMA-local, mapped once as few dense regions**, within `LimitMEMLOCK=`; nothing is mapped or unmapped in the session (§88.4.4).
- [ ] Hot-path completions are **polled in host memory** from an isolated core; MSI-X eventfds serve the **idle tier** with the arm-then-re-check switch (§88.3, §88.4.3).
- [ ] Doorbell pages are **UC** (or WC with `sfence`), descriptors go out as **posted writes**, and no device register is **read** per message (Ch. 87 §87.2.3, §88.5).
- [ ] Shared, interrupt-heavy or netdev-integrated devices use a **control-plane module / data-plane process** split whose per-message path is ioctl-free (§88.2.4, §88.4.5).
- [ ] Device open/map/IRQ setup and any **reset** happen before the readiness gate, never in the session; the restart path re-opens, it does not reset (§88.5).

## 88.7 References

- Linux kernel documentation: `Documentation/driver-api/vfio.rst` (the container/group/device model, the ioctl sequence, Type1 IOMMU), `Documentation/driver-api/vfio-pci-device-specific-driver-acceptance.rst`, `Documentation/driver-api/uio-howto.rst` (UIO and `uio_pci_generic`), `Documentation/core-api/dma-api.rst` and `dma-api-howto.rst` (the kernel-side DMA API §88.4.5 uses), and `Documentation/PCI/pci.rst` — the mechanisms of §88.2 and §88.4.
- The DPDK *Linux Drivers* guide (`vfio-pci`, `uio_pci_generic`, `igb_uio`, IOVA modes, `dpdk-devbind.py`) and the DPDK EAL `vfio` source — the production-grade version of §88.4.2's sequence, including WC mappings and sparse-mmap regions.
- Intel, *Virtualization Technology for Directed I/O (VT-d) Architecture Specification*; AMD, *I/O Virtualization Technology (IOMMU) Specification* — IOMMU domains, IOTLB, large-page support, interrupt remapping (§88.2.2).
- PCI-SIG, *PCI Express Base Specification* — Access Control Services (ACS), Function Level Reset, MSI-X (§88.2.2–§88.2.3).
- J. Corbet, A. Rubini, G. Kroah-Hartman, *Linux Device Drivers*, 3rd ed. (LDD3) and the `driverctl` project — the kernel-module half (§88.4.5) and persistent binding (§88.4.1).
- Solarflare/AMD *Onload* and *ef_vi* user guides; Exablaze/Cisco *libexanic* documentation — the control-plane/data-plane split as shipped (§88.2.4, §88.4.5; Ch. 82 §82.4.4).

## 88.8 Additional Reading

- B. Stephens, *VFIO — "Virtual Function I/O"* (kernel summit and KVM Forum talks by Alex Williamson) — the design rationale: why groups, why ACS, why `noiommu` is unsafe.
- The SPDK *System Configuration* guide and `spdk/lib/env_dpdk` — a second mature VFIO userspace driver base (NVMe), and the clearest example of the IOVA-mode trade-off.
- QEMU's `vfio-pci` documentation — the same binding reused for device passthrough into a guest (Appendix S §S.4).
- Ch. 87 (*PCIe*) — the mechanism this chapter exposes; Ch. 82 (*bypass*) and Ch. 83 (*RDMA*) — the consumers; Ch. 91 (*FPGA*) — the custom device whose host driver is §88.4.2; Ch. 101 (*security*) — the IOMMU as a boundary and capability drop; Ch. 103 §103.4.9 — the unit that binds the device at boot.
- **Appendix I.8** — the `lspci`/`driverctl`/`iommu_groups` one-liners; **Appendix S** — SR-IOV VFs and VFIO passthrough for the adjacent workloads that run in VMs.

---

*Next: Ch. 89 — GPU Computing with CUDA, the first concrete accelerator behind the PCIe boundary this chapter opened: the CUDA execution and memory model, where GPUs pay off in trading (risk, options pricing, Monte-Carlo, ML) — and why the PCIe round-trip keeps them off the tick-to-trade hot path, measured end-to-end including transfer, not just kernel time.*
