// SPDX-License-Identifier: GPL-2.0-or-later
//! a2ctl: host-side control of a2 (US-48 unit 1, BCM53346 Cortex-A9) from a1 over PCIe.
//!
//! Path to unit 1's AXI bus: PAXB inbound window IMAP0_7 (BAR0+0x2c1c) selects a 4 KiB AXI page, visible at
//! BAR0+0x7000 (OpenMDK shbde_iproc.c). a2 start: CoreSight halt + ITR injection (jump to the SRAM stub).
//! Stub protocol: tools/a2_stub.S (mailbox at 0x1b000100). Prior Python prototype: scripts/a2_loader.py.
//!
//! Timeouts: the stub answers in microseconds; ack wait is 50 ms (a PCIe store+load is ~1 us, payload time
//! is the caller's: pass --wait-ms). On expiry the error prints the mailbox. Debug halt/ITR waits: 1000
//! register polls (~1 ms) before giving up.

use std::fs::OpenOptions;
use std::os::unix::io::AsRawFd;
use std::time::{Duration, Instant};

const BAR0_DEFAULT: &str = "/sys/bus/pci/devices/0000:01:00.0/resource0";
const BAR0_LEN: usize = 0x8000;
const IMAP0_7: usize = 0x2c1c;
const IMAP0_7_DEFAULT: u32 = 0x1803_2001;
const WIN: usize = 0x7000;
const SRAM: u32 = 0x1b00_0000;
const MBOX: u32 = 0x1b00_0100;
const PAYLOAD: u32 = 0x1b00_0200;
const MAGIC: u32 = 0x57b1_0ade;
const CRU_CONTROL: u32 = 0x1803_e000;
const DBG: u32 = 0x1901_0000;
// debug registers (offsets in the 0x19010000 page)
const DTRRX: u32 = 0x80;
const ITR: u32 = 0x84;
const DSCR: u32 = 0x88;
const DRCR: u32 = 0x90;
const LAR: u32 = 0xfb0;
// mailbox word offsets
const M_CMD: u32 = 0;
const M_ARG: u32 = 4;
const M_RES: u32 = 16;
const M_SEQ: u32 = 20;
const M_HB: u32 = 24;
const M_MAGIC: u32 = 28;

struct Bar {
    base: *mut u32,
    page: Option<u32>,
}

impl Bar {
    fn open(path: &str) -> Result<Bar, String> {
        let f = OpenOptions::new().read(true).write(true).open(path).map_err(|e| format!("{path}: {e}"))?;
        let p = unsafe {
            libc::mmap(std::ptr::null_mut(), BAR0_LEN, libc::PROT_READ | libc::PROT_WRITE, libc::MAP_SHARED, f.as_raw_fd(), 0)
        };
        if p == libc::MAP_FAILED {
            return Err(format!("mmap {path}: {}", std::io::Error::last_os_error()));
        }
        Ok(Bar { base: p as *mut u32, page: None })
    }
    fn rd32(&self, off: usize) -> u32 {
        unsafe { std::ptr::read_volatile(self.base.add(off / 4)) }
    }
    fn wr32(&self, off: usize, v: u32) {
        unsafe { std::ptr::write_volatile(self.base.add(off / 4), v) }
    }
    /// Point the 4 KiB window at AXI page containing `axi`; readback orders the posted write.
    fn page(&mut self, axi: u32) {
        let p = axi & !0xfff;
        if self.page != Some(p) {
            self.wr32(IMAP0_7, p | 1);
            let _ = self.rd32(IMAP0_7);
            self.page = Some(p);
        }
    }
    fn axi_rd(&mut self, axi: u32) -> u32 {
        self.page(axi);
        self.rd32(WIN + (axi & 0xfff) as usize)
    }
    fn axi_wr(&mut self, axi: u32, v: u32) {
        self.page(axi);
        self.wr32(WIN + (axi & 0xfff) as usize, v);
        let _ = self.rd32(WIN + (axi & 0xfff) as usize); // flush posted write
    }
    fn restore(&mut self) {
        self.wr32(IMAP0_7, IMAP0_7_DEFAULT);
        let _ = self.rd32(IMAP0_7);
        self.page = None;
    }
}

fn poll<F: FnMut() -> bool>(mut f: F, n: u32) -> bool {
    (0..n).any(|_| f())
}

fn reset(b: &mut Bar) {
    let c = b.axi_rd(CRU_CONTROL);
    b.axi_wr(CRU_CONTROL, c & !0x10); // ARM_RST_N = 0
    b.axi_wr(CRU_CONTROL, c | 0x10);
    println!("CRU_CONTROL {:#x} -> pulsed -> {:#x}", c, b.axi_rd(CRU_CONTROL));
}

fn load(b: &mut Bar, data: &[u8], addr: u32) {
    for (i, w) in data.chunks(4).enumerate() {
        let mut x = [0u8; 4];
        x[..w.len()].copy_from_slice(w);
        b.axi_wr(addr + 4 * i as u32, u32::from_le_bytes(x));
    }
    println!("loaded {} bytes at {:#x}", data.len(), addr);
}

fn start(b: &mut Bar, stub: &[u8]) -> Result<(), String> {
    load(b, stub, SRAM);
    for off in (0..0x20).step_by(4) {
        b.axi_wr(MBOX + off, 0);
    }
    b.axi_wr(DBG + LAR, 0xC5AC_CE55);
    let d = b.axi_rd(DBG + DSCR);
    b.axi_wr(DBG + DSCR, d | 1 << 14); // HDBGen
    b.axi_wr(DBG + DRCR, 1); // halt request
    if !poll(|| b.axi_rd(DBG + DSCR) & 1 != 0, 1000) {
        return Err(format!("a2 did not halt (DSCR {:#x}); try `reset` first", b.axi_rd(DBG + DSCR)));
    }
    let d = b.axi_rd(DBG + DSCR);
    b.axi_wr(DBG + DSCR, d | 1 << 13 | 1 << 14); // ITRen
    b.axi_wr(DBG + DTRRX, SRAM);
    let mut ok = true;
    for ins in [0xee10_0e15u32, 0xe1a0_f000] {
        // mrc p14,0,r0,c0,c5,0 ; mov pc,r0
        b.axi_wr(DBG + ITR, ins);
        ok &= poll(|| b.axi_rd(DBG + DSCR) & (1 << 24) != 0, 1000);
    }
    let d = b.axi_rd(DBG + DSCR);
    b.axi_wr(DBG + DSCR, d & !(1 << 13));
    b.axi_wr(DBG + DRCR, 2 | 4); // restart + clear sticky
    if !ok {
        return Err("ITR injection incomplete; a2 left halted: run `reset`".into());
    }
    if !poll(|| b.axi_rd(MBOX + M_MAGIC) == MAGIC, 1000) {
        return Err("stub did not report ready".into());
    }
    println!("stub ready, heartbeat {}", b.axi_rd(MBOX + M_HB));
    Ok(())
}

fn command(b: &mut Bar, cmd: u32, a: [u32; 3], wait: Duration) -> Result<u32, String> {
    let seq = b.axi_rd(MBOX + M_SEQ);
    for (i, v) in a.iter().enumerate() {
        b.axi_wr(MBOX + M_ARG + 4 * i as u32, *v);
    }
    b.axi_wr(MBOX + M_CMD, cmd); // cmd last
    let t = Instant::now();
    while t.elapsed() < wait {
        if b.axi_rd(MBOX + M_SEQ) != seq {
            return Ok(b.axi_rd(MBOX + M_RES));
        }
    }
    let m: Vec<String> = (0..8).map(|i| format!("{:#x}", b.axi_rd(MBOX + 4 * i))).collect();
    Err(format!("a2 did not ack cmd {cmd} within {wait:?}: mailbox {m:?}"))
}


/// iProc CCB I2C controller at `base` (unit 1 AXI), SMBus "quick" probe of every 7-bit address (protocol from
/// mainline i2c-bcm-iproc.c). One quick write at 100 kHz is ~0.2 ms; 5 ms backstop per address (25x).
fn i2c_scan(b: &mut Bar, base: u32) -> String {
    const CFG: u32 = 0x00;
    const FIFO: u32 = 0x0c;
    const CMD: u32 = 0x30;
    const IE: u32 = 0x38;
    const IS: u32 = 0x3c;
    const TX: u32 = 0x40;
    b.axi_wr(base + CFG, 1 << 31); // reset
    let t = Instant::now();
    while t.elapsed() < Duration::from_micros(200) {}
    b.axi_wr(base + CFG, 1 << 30); // out of reset, enabled
    b.axi_wr(base + IE, 0);
    let mut acks = Vec::new();
    let mut counts = [0u32; 8];
    let mut timeouts = 0u32;
    for a in 0x03u32..=0x77 {
        b.axi_wr(base + FIFO, 3 << 30); // flush rx+tx
        b.axi_wr(base + TX, a << 1);
        b.axi_wr(base + CMD, 1 << 31); // start, protocol quick (0)
        let t = Instant::now();
        let mut cmd = b.axi_rd(base + CMD);
        while cmd & (1 << 31) != 0 && t.elapsed() < Duration::from_millis(5) {
            cmd = b.axi_rd(base + CMD);
        }
        if cmd & (1 << 31) != 0 {
            timeouts += 1;
            b.axi_wr(base + CFG, 1 << 31);
            b.axi_wr(base + CFG, 1 << 30);
            continue;
        }
        let st = ((cmd >> 25) & 7) as usize;
        counts[st] += 1;
        if st == 0 {
            acks.push(a);
        }
        let is = b.axi_rd(base + IS);
        b.axi_wr(base + IS, is);
    }
    format!(
        "i2c {base:#x}: ACK at {:02x?}; status counts ok/arb/nack_addr/nack_data/timeout/under/full = {:?}, busy-timeouts {timeouts}",
        acks, &counts[..7]
    )
}

fn num(s: &str) -> Result<u32, String> {
    let r = if let Some(h) = s.strip_prefix("0x") { u32::from_str_radix(h, 16) } else { s.parse() };
    r.map_err(|e| format!("bad number {s}: {e}"))
}

const USAGE: &str = "usage: a2ctl [--bar0 PATH] [--wait-ms N] [--stub FILE] <cmd>\n\
  reset | start | status | read ADDR [N] | write ADDR VAL | load FILE [ADDR] | call ADDR [A B] | run FILE [A B]\n\
  axi-read ADDR [N] | axi-write ADDR VAL | i2c-scan CTRL_BASE | bar-read OFF [N]   (host path to unit 1, a2 not involved)";

fn run() -> Result<(), String> {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let mut bar0 = BAR0_DEFAULT.to_string();
    let mut wait = Duration::from_millis(50);
    let mut stub = "tmp/a2/a2_stub.bin".to_string();
    while args.first().map_or(false, |a| a.starts_with("--")) {
        let k = args.remove(0);
        let v = if args.is_empty() { return Err(USAGE.into()) } else { args.remove(0) };
        match k.as_str() {
            "--bar0" => bar0 = v,
            "--wait-ms" => wait = Duration::from_millis(num(&v)? as u64),
            "--stub" => stub = v,
            _ => return Err(USAGE.into()),
        }
    }
    if args.is_empty() {
        return Err(USAGE.into());
    }
    let cmd = args.remove(0);
    let mut b = Bar::open(&bar0)?;
    let r = (|| -> Result<(), String> {
        let a = |i: usize| args.get(i).map(|s| num(s)).transpose();
        match cmd.as_str() {
            "reset" => reset(&mut b),
            "start" => start(&mut b, &std::fs::read(&stub).map_err(|e| format!("{stub}: {e}"))?)?,
            "status" => {
                let (m, h1) = (b.axi_rd(MBOX + M_MAGIC), b.axi_rd(MBOX + M_HB));
                let t = Instant::now();
                while t.elapsed() < Duration::from_millis(10) {}
                let h2 = b.axi_rd(MBOX + M_HB);
                println!("magic {m:#x} seq {} heartbeat {h1} -> {h2} {}", b.axi_rd(MBOX + M_SEQ), if h2 != h1 { "(moving)" } else { "(STOPPED)" });
            }
            "read" => {
                let (addr, n) = (a(0)?.ok_or(USAGE)?, a(1)?.unwrap_or(1));
                for i in 0..n {
                    println!("{:08x}: {:08x}", addr + 4 * i, command(&mut b, 2, [addr + 4 * i, 0, 0], wait)?);
                }
            }
            "write" => println!("rc {}", command(&mut b, 3, [a(0)?.ok_or(USAGE)?, a(1)?.ok_or(USAGE)?, 0], wait)?),
            "load" => {
                let f = args.first().ok_or(USAGE)?.clone();
                load(&mut b, &std::fs::read(&f).map_err(|e| format!("{f}: {e}"))?, a(1)?.unwrap_or(PAYLOAD));
            }
            "call" => {
                let addr = a(0)?.ok_or(USAGE)?;
                println!("call {addr:#x} -> r0 = {:#x}", command(&mut b, 1, [addr, a(1)?.unwrap_or(0), a(2)?.unwrap_or(0)], wait)?);
            }
            "run" => {
                let f = args.first().ok_or(USAGE)?.clone();
                load(&mut b, &std::fs::read(&f).map_err(|e| format!("{f}: {e}"))?, PAYLOAD);
                println!("call {PAYLOAD:#x} -> r0 = {:#x}", command(&mut b, 1, [PAYLOAD, a(1)?.unwrap_or(0), a(2)?.unwrap_or(0)], wait)?);
            }
            "axi-read" => {
                let (addr, n) = (a(0)?.ok_or(USAGE)?, a(1)?.unwrap_or(1));
                for i in 0..n {
                    println!("{:08x}: {:08x}", addr + 4 * i, b.axi_rd(addr + 4 * i));
                }
            }
            "axi-write" => b.axi_wr(a(0)?.ok_or(USAGE)?, a(1)?.ok_or(USAGE)?),
            "i2c-scan" => println!("{}", i2c_scan(&mut b, a(0)?.ok_or(USAGE)?)),
            "bar-read" => {
                let (off, n) = (a(0)?.ok_or(USAGE)? as usize, a(1)?.unwrap_or(1));
                for i in 0..n as usize {
                    println!("bar0+{:#x}: {:08x}", off + 4 * i, b.rd32(off + 4 * i));
                }
            }
            _ => return Err(USAGE.into()),
        }
        Ok(())
    })();
    b.restore();
    r
}

fn main() {
    if let Err(e) = run() {
        eprintln!("a2ctl: {e}");
        std::process::exit(1);
    }
}
