#!/usr/bin/env python3
"""Run code on a2 (unit 1 Cortex-A9) from its SRAM: load program, CoreSight halt, inject `mrc` + `mov pc,r0`, restart.

Usage: US48_HOST=lizard python3 scripts/a2_force_pc.py [program.bin]   (board at uboot-2; default tmp/bootrom/a2_test.bin)
Build the program: arm-linux-musleabi-as/ld -Ttext=0x1b000000 tools/a2_test.S, objcopy -O binary.
Host reaches unit 1 AXI via PAXB IMAP0_7 (BAR0+0x2c1c) + 4 KiB window at BAR0+0x7000; restored on exit.
Only unit 1 is touched (a1, the host A9, never). Undo: pulse CRU_CONTROL.ARM_RST_N (unit 1 0x1803e000 bit 4).
Log: tmp/logs/a2_force_pc.log. Details: docs/sram.md, issue #53.
"""
import pathlib,sys,io,time
sys.path.insert(0,str(pathlib.Path(__file__).parent))
import console,re,struct
REPO=pathlib.Path(__file__).resolve().parent.parent
BIN=pathlib.Path(sys.argv[1]) if len(sys.argv)>1 else REPO/'tmp/bootrom/a2_test.bin'
LOG=REPO/'tmp/logs/a2_force_pc.log'; LOG.parent.mkdir(parents=True,exist_ok=True)
_p=print
def print(*a,**k):
    _p(*a,**k); LOG.open('a').write(time.strftime('%Y-%m-%dT%H:%M:%S%z ')+' '.join(str(x) for x in a)+'\n')
W=0x08007000; IM=0x08002c1c
prog=struct.unpack('<9I',open(BIN,'rb').read())
def cmd(s,c,w=0.3):
    s.send_line(c); return s.read_idle(w,3.0).decode(errors='replace')
def rd(s,a,n=1):
    r=cmd(s,'md.l 0x%x %x'%(W+a,n)); o=[]
    for l in r.splitlines():
        m=re.match(r'\s*0800[0-9a-f]{4}:((?: [0-9a-f]{8})+)',l)
        if m: o+=m.group(1).split()
    return [int(x,16) for x in o[:n]]
def wr(s,off,v): cmd(s,'mw.l 0x%x 0x%08x'%(W+off,v),0.15)
def page(s,axi): cmd(s,'mw.l 0x%x %#x'%(IM,axi|1),0.2)
DSCR,ITR,DTRRX,DRCR=0x88,0x84,0x80,0x90
def itr(s,ins):
    wr(s,ITR,ins)
    for _ in range(10):
        if rd(s,DSCR)[0]&(1<<24): return True
    return False
with console.Session() as s:
    s.write(b"\x03\r\n"); s.read_idle(1.0,3.0)
    try:
        page(s,0x1b000000)
        for i,w in enumerate(prog): wr(s,4*i,w)
        wr(s,0x100,0); wr(s,0x104,0)
        print('program ok:',rd(s,0,9)==list(prog))
        page(s,0x19010000)
        wr(s,0xfb0,0xC5ACCE55); print('LSR after unlock',hex(rd(s,0xfb4)[0]))
        d=rd(s,DSCR)[0]; print('DSCR',hex(d)); wr(s,DSCR,d|(1<<14)); print('DSCR HDBGen',hex(rd(s,DSCR)[0]))
        wr(s,DRCR,1)
        halted=False
        for k in range(10):
            d=rd(s,DSCR)[0]
            if d&1: halted=True; break
        print('halted:',halted,'DSCR',hex(d))
        if not halted: raise SystemExit('halt did not take; restoring')
        print('PCSR/halted PC sample',hex(rd(s,0x84)[0]) if False else '(skipped)')
        wr(s,DSCR,d|(1<<13)|(1<<14))                       # ITRen
        wr(s,DTRRX,0x1b000000)
        print('mrc r0<-DTRRX done:',itr(s,0xee100e15))
        print('mov pc,r0 done:',itr(s,0xe1a0f000))
        d=rd(s,DSCR)[0]; wr(s,DSCR,d&~(1<<13))             # ITRen off
        wr(s,DRCR,2|4)                                     # restart, clear sticky
        for k in range(10):
            d=rd(s,DSCR)[0]
            if d&2 and not d&1: break
        print('restarted: DSCR',hex(d))
        page(s,0x1b000000)
        for k in range(3): print('SRAM marker/counter',[hex(x) for x in rd(s,0x100,2)])
        page(s,0x19010000); print('PCSR now',hex(rd(s,0x84)[0]),hex(rd(s,0x84)[0]))
    finally:
        cmd(s,'mw.l 0x%x 0x18032001'%IM); print('IMAP0_7 restored')
