#!/usr/bin/env python3
"""BLE bridge for the Sperax RM-01 / Freepi walking pad (WiLink WLT6200 module).

stdout: one JSON state object per line.
stdin:  "start <km/h>", "speed <km/h>", "stop".

Commands from https://github.com/nathanabrewer/sperax-rm01/blob/main/PROTOCOL.md;
byte stuffing and the 0x19 telemetry layout were decoded from a real walk.
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import date, timedelta

from bleak import BleakClient, BleakScanner

NAME = os.environ.get("WALKING_PAD_NAME", "SPERAX_RM01")
NOTIFY = "0000fff1-0000-1000-8000-00805f9b34fb"
WRITE = "0000fff2-0000-1000-8000-00805f9b34fb"
MIN_KMH, MAX_KMH = 0.2, 4.0  # the pad's own pace range
CACHE = os.path.expanduser("~/.cache/walking-pad")
LOG = f"{CACHE}/frames.log"
ADDRESS = f"{CACHE}/address"
HISTORY = os.path.expanduser("~/.local/share/walking-pad/history.json")
TRACKED = ("steps", "distanceKm", "seconds")

# 0x19 state byte. 0x11-0x13 is the 3-2-1 countdown before the belt moves.
STATES = {0x00: "idle", 0x02: "idle", 0x10: "running", 0x0F: "stopping"}


def crc16(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA327 if crc & 1 else crc >> 1
    return crc


# Frames are F5 <len> 00 <cmd> <data> <crc lo> <crc hi> FA. Any byte >= F0 between
# the length and the trailer goes on the wire as F0 0x, so F5/FA only ever mark
# frame edges. The length byte counts wire bytes; the CRC covers the unstuffed
# frame with its unstuffed length.
def frame(*data):
    body = bytes([0xF5, len(data) + 6, 0x00, *data])
    crc = crc16(body)
    inner = body[2:] + bytes([crc & 0xFF, crc >> 8])
    wire = b"".join(bytes([0xF0, b & 0x0F]) if b >= 0xF0 else bytes([b]) for b in inner)
    return bytes([0xF5, len(wire) + 3]) + wire + b"\xFA"


def unstuff(wire):
    """Wire frame -> (cmd, data), or None if it doesn't check out."""
    inner, i = [], 2
    while i < len(wire) - 1:
        if wire[i] == 0xF0 and i + 1 < len(wire) - 1:
            inner.append(0xF0 | wire[i + 1])
            i += 2
        else:
            inner.append(wire[i])
            i += 1
    if len(inner) < 4 or crc16(bytes([0xF5, len(inner) + 3, *inner[:-2]])) != inner[-2] | inner[-1] << 8:
        return None
    return inner[1], bytes(inner[2:-2])


def split_frames(buf):
    """Notifications can cut a frame in two; return (complete frames, leftover)."""
    frames = []
    while (end := buf.find(0xFA)) != -1:
        start = buf.rfind(0xF5, 0, end)
        if start != -1:
            frames.append(buf[start:end + 1])
        buf = buf[end + 1:]
    # Only a partial frame (from its F5) is worth keeping, and a real one is
    # well under 512 bytes even fully stuffed; drop anything else so a pad
    # that never sends FA can't grow the buffer.
    start = buf.rfind(0xF5)
    return frames, buf[start:] if start != -1 and len(buf) - start < 512 else b""


def parse(cmd, d):
    """0x19 telemetry, big-endian: d2 state, d5-6 seconds, d7-8 distance (10 m),
    d9-10 kcal, d11-12 steps, d13 speed (0.1 km/h)."""
    if cmd != 0x19 or len(d) < 14:
        return {}
    state = STATES.get(d[2], "countdown" if 0x11 <= d[2] <= 0x13 else f"0x{d[2]:02x}")
    fields = {"state": state, "countdown": d[2] - 0x10 if state == "countdown" else 0}
    # Once stopped the pad zeroes the session; keep showing the last one until
    # the next start rather than flashing back to 0.
    if state == "idle" and not any(d[5:14]):
        return fields | {"speedKmh": 0}
    return fields | {
        "seconds": d[5] << 8 | d[6],
        "distanceKm": (d[7] << 8 | d[8]) / 100,
        "calories": d[9] << 8 | d[10],
        "steps": d[11] << 8 | d[12],
        "speedKmh": d[13] / 10,
    }


class Tally:
    """Per-day totals built from the pad's per-session counters, which reset on
    every start, so today keeps adding up across on/off cycles."""

    def __init__(self, path):
        self.path = path
        try:
            with open(path) as f:
                saved = json.load(f)
        except (OSError, ValueError):
            saved = {}
        self.days = saved.get("days", {})
        # The session counters last seen, saved too so a helper restart mid-walk
        # picks up where it left off instead of dropping the gap.
        self.last = saved.get("last", {})

    def add(self, fields):
        """Credit new progress to today; True if anything changed."""
        today = self.days.setdefault(date.today().isoformat(), dict.fromkeys(TRACKED, 0))
        changed = False
        for key in TRACKED:
            if key not in fields:
                continue
            value = fields[key]
            # A counter going backwards is a fresh session starting from 0.
            prev = self.last.get(key, value)
            delta = value - prev if value >= prev else value
            self.last[key] = value
            if delta:
                today[key] = round(today[key] + delta, 2)
                changed = True
        if changed:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path + ".tmp", "w") as f:
                json.dump({"days": self.days, "last": self.last}, f)
            os.replace(self.path + ".tmp", self.path)
        return changed

    def recent(self, n=7):
        days = [(date.today() - timedelta(days=i)).isoformat() for i in reversed(range(n))]
        return [{"date": d, **self.days.get(d, dict.fromkeys(TRACKED, 0))} for d in days]


def kmh(arg):
    return round(max(MIN_KMH, min(MAX_KMH, float(arg))) * 10)


async def stdin_lines():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    return reader


def emit(state):
    print(json.dumps(state), flush=True)


async def session(commands, log, tally):
    state = {"connected": False, "history": tally.recent()}
    emit(state)
    dev = await BleakScanner.find_device_by_name(NAME, timeout=10)
    if dev:
        open(ADDRESS, "w").write(dev.address)
    else:
        # A pad still linked to BlueZ from a killed helper stops advertising; free it.
        if os.path.exists(ADDRESS):
            subprocess.run(["bluetoothctl", "disconnect", open(ADDRESS).read().strip()],
                           capture_output=True, timeout=10)
        return
    gone = asyncio.Event()
    buf = b""
    # The pad ignores the speed in a start command and ramps to its default, so a
    # requested speed waits here until the belt is actually running.
    want_speed = None

    def on_notify(_, data):
        nonlocal buf
        log.write(f"{time.time():.1f} < {data.hex(' ')}\n")
        frames, buf = split_frames(buf + bytes(data))
        for f in frames:
            fields = parse(*(unstuff(f) or (None, b"")))
            if tally.add(fields):
                fields["history"] = tally.recent()
            if fields and any(state.get(k) != v for k, v in fields.items()):
                state.update(fields)
                emit(state)

    async def send(*d):
        f = frame(*d)
        log.write(f"{time.time():.1f} > {f.hex(' ')}\n")
        await client.write_gatt_char(WRITE, f, response=False)

    async with BleakClient(dev, disconnected_callback=lambda _: gone.set()) as client:
        await client.start_notify(NOTIFY, on_notify)
        state["connected"] = True
        emit(state)
        while not gone.is_set():
            while not commands.empty():
                cmd, _, arg = commands.get_nowait().partition(" ")
                if cmd == "start":
                    await send(0x00)
                    await asyncio.sleep(0.3)
                    await send(0x15, 0x01, kmh(arg), 0x00)
                    want_speed = kmh(arg)
                elif cmd == "speed":
                    want_speed = kmh(arg)
                elif cmd == "stop":
                    want_speed = None
                    # The reference app repeats stop; one write can get lost.
                    await send(0x15, 0, 0, 0)
                    await asyncio.sleep(0.5)
                    await send(0x15, 0, 0, 0)
            if want_speed is not None and state.get("state") == "running":
                await send(0x15, 0x01, want_speed, 0x00)
                want_speed = None
            # 0x00 doubles as keepalive: the pad drops idle BLE links.
            await send(0x00)
            await asyncio.sleep(0.3)
            await send(0x19)
            await asyncio.sleep(0.7)


async def main():
    os.makedirs(CACHE, exist_ok=True)
    commands = asyncio.Queue()
    reader = await stdin_lines()

    async def pump():
        while line := await reader.readline():
            commands.put_nowait(line.decode().strip())
        os._exit(0)  # shell went away; don't linger holding the pad's only BLE slot

    asyncio.create_task(pump())
    tally = Tally(HISTORY)
    with open(LOG, "a", buffering=1) as log:
        while True:
            try:
                await session(commands, log, tally)
            except Exception as e:
                print(f"session error: {e}", file=sys.stderr, flush=True)
            await asyncio.sleep(3)


def selftest():
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "history.json")
    t = Tally(path)
    for steps in [0, 5, 28, 28]:  # walk, stop (session kept on screen)
        t.add({"steps": steps})
    t.add({})  # idle frames carry no counters
    for steps in [0, 4, 10]:  # second walk: pad restarted its counter
        t.add({"steps": steps})
    t = Tally(path)  # helper restart mid-walk: steps walked meanwhile still count
    t.add({"steps": 15})
    assert t.recent()[-1]["steps"] == 43 and len(t.recent()) == 7

    # Captured from a real pad: a stuffed status frame split across two
    # notifications, then the telemetry at the end of a 1:57 / 98-step walk.
    got, rest = split_frames(bytes.fromhex("121314f509000e00f00c9a"))
    assert got == [] and rest == bytes.fromhex("f509000e00f00c9a")
    assert split_frames(b"\x00" * 4096) == ([], b"")
    assert split_frames(b"\xf5" + b"\x00" * 4096) == ([], b"")
    got, _ = split_frames(rest + bytes.fromhex("fa" "f519001900000200010075000300040062000000008ff00afa"))
    assert unstuff(got[0]) == (0x0E, b"\x00")
    t = parse(*unstuff(got[1]))
    assert (t["state"], t["seconds"], t["distanceKm"], t["steps"], t["speedKmh"]) == ("idle", 117, 0.03, 98, 0)
    # Mid-walk at 3.0 km/h, then slowing to a stop: steps must hold at 14.
    for tail, kmh_ in [("0e 1e", 3.0), ("0e 1a", 2.6), ("0e 02", 0.2)]:
        d = bytes.fromhex("00 00 10 00 01 00 0c 00 01 00 01 00" + tail + "00 00 00")
        assert (parse(0x19, d)["steps"], parse(0x19, d)["speedKmh"]) == (14, kmh_)
    assert parse(0x19, bytes(17)) == {"state": "idle", "countdown": 0, "speedKmh": 0}
    # Outgoing: the pad answered this one with "ready", and stuffing round-trips
    # (unstuffed, the 0x19 query's CRC carries a bare FA).
    assert frame(0x00) == bytes.fromhex("f5070000d2b6fa")
    for cmd in [(0x19,), (0x15, 0x01, 45, 0x00)]:
        assert 0xFA not in frame(*cmd)[1:-1] and unstuff(frame(*cmd)) == (cmd[0], bytes(cmd[1:]))


if __name__ == "__main__":
    selftest()
    if sys.argv[1:] != ["test"]:
        asyncio.run(main())
