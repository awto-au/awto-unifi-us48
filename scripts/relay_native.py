#!/usr/bin/env python3
"""ESPHome native-API switch control (run with awto-terminal's venv, which has aioesphomeapi).

Usage: relay_native.py HOST on|off|state     first switch entity on HOST (port 6053, no encryption)
Waits: connect 5 s (LAN, ms), 1 s after a command for the device to apply it, 2 s for the state report.
Log: tmp/logs/relay_native.log.
"""
import asyncio
import pathlib
import sys
import time

from aioesphomeapi import APIClient

LOG = pathlib.Path(__file__).resolve().parent.parent / "tmp/logs/relay_native.log"


async def run(host, op):
    cli = APIClient(host, 6053, None)
    await asyncio.wait_for(cli.connect(login=True), 5)
    ents, _ = await cli.list_entities_services()
    sw = next(e for e in ents if type(e).__name__ == "SwitchInfo")
    states = []
    cli.subscribe_states(states.append)
    if op in ("on", "off"):
        cli.switch_command(sw.key, op == "on")
        await asyncio.sleep(1)
    await asyncio.sleep(2)
    st = next((s.state for s in reversed(states) if s.key == sw.key), None)
    await cli.disconnect()
    return sw.object_id, st


host, op = sys.argv[1], sys.argv[2]
name, st = asyncio.run(run(host, op))
msg = f"{host} switch {name}: {op} -> state {st}"
LOG.parent.mkdir(parents=True, exist_ok=True)
LOG.open("a").write(time.strftime("%Y-%m-%dT%H:%M:%S%z ") + msg + "\n")
print(msg)
