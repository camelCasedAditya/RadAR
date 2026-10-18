"""Record, replay, or synthesize phone sessions.

    python session.py record out.jsonl          # connect as hub, save phone traffic
    python session.py replay in.jsonl [--loop]  # play back into /phone/{id} at real time
    python session.py fake out.jsonl            # two responders walking a synthetic corridor

File format: one JSON object per line, {"dt": seconds since start, "id": .., "text": ..} or {"dt", "id", "bin": base64}.
"""
import argparse
import asyncio
import base64
import io
import json
import math
import time

import websockets

from wire import KIND_JPEG, pack_bin, unpack_bin

RELAY_MSGS = {"uwb_check", "gpu", "maplag", "state", "recording"}   # relay-originated; not phone traffic


async def record(path, url):
    t0 = time.time()
    f = open(path, "w", buffering=1)
    async with websockets.connect(url + "/hub", max_size=None) as ws:
        await ws.recv()  # initial state
        async for m in ws:
            if isinstance(m, str):
                d = json.loads(m)
                if d["t"] in RELAY_MSGS or not d.get("id"):
                    continue
                f.write(json.dumps({"dt": time.time() - t0, "id": d["id"], "text": m}) + "\n")
            else:
                kind, rid, _, _ = unpack_bin(m)
                if kind == KIND_JPEG:
                    f.write(json.dumps({"dt": time.time() - t0, "id": rid, "bin": base64.b64encode(m).decode()}) + "\n")


async def replay(path, url, loop):
    rows = [r for r in (json.loads(l) for l in open(path)) if r.get("id")
            and not ("text" in r and json.loads(r["text"])["t"] in RELAY_MSGS)]   # phone traffic only
    ids = sorted({r["id"] for r in rows})
    socks = {i: await websockets.connect(f"{url}/phone/{i}", max_size=None) for i in ids}
    while True:
        t0 = time.time()
        for r in rows:
            wait = r["dt"] - (time.time() - t0)
            if wait > 0:
                await asyncio.sleep(wait)
            ws = socks[r["id"]]
            await ws.send(r["text"] if "text" in r else base64.b64decode(r["bin"]))
        if not loop:
            break


# ---------- synthetic session ----------
# Corridor loop: rectangle 12 m x 6 m, walls 1 m out from the walking path.
PATH_W, PATH_D = 12.0, 6.0


def path_point(s):
    """Point at arc length s along the rectangle loop, plus heading (radians, 0 = -Z)."""
    per = 2 * (PATH_W + PATH_D)
    s %= per
    legs = [(PATH_W, (1, 0)), (PATH_D, (0, 1)), (PATH_W, (-1, 0)), (PATH_D, (0, -1))]
    x, z = 0.0, 0.0
    for length, (dx, dz) in legs:
        if s <= length:
            return x + dx * s, z + dz * s, math.atan2(-dx, -dz)
        x, z, s = x + dx * length, z + dz * length, s - length
    return x, z, 0.0


def pose_T(x, z, yaw, y=1.4):
    """Column-major 4x4 camera-to-world, ARKit axes (camera looks along -Z, +Y up)."""
    c, s = math.cos(yaw), math.sin(yaw)
    # rotation about Y by yaw
    return [c, 0, -s, 0,  0, 1, 0, 0,  s, 0, c, 0,  x, y, z, 1]


def fake_jpeg(label, hue):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (518, 378), (int(60 + 60 * hue), 50, int(120 - 60 * hue)))
    d = ImageDraw.Draw(im)
    d.text((20, 20), label, fill=(255, 255, 255))
    for i in range(0, 518, 37):
        d.line([(i, 0), (i, 378)], fill=(90, 90, 90))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=60)
    return buf.getvalue()


def fake(path, seconds=60, fps=10):
    K = [400.0, 400.0, 259.0, 189.0]
    with open(path, "w") as f:
        for i in range(int(seconds * fps)):
            t = i / fps
            for rid, speed, offset, hue in (("A", 0.8, 0.0, 0.0), ("B", 0.6, 18.0, 1.0)):
                x, z, yaw = path_point(offset + speed * t)
                pose = {"t": "pose", "id": rid, "f": i, "ts": t, "T": pose_T(x, z, yaw), "K": K, "w": 518, "h": 378}
                f.write(json.dumps({"dt": t, "id": rid, "text": json.dumps(pose)}) + "\n")
                if i % 2 == 0:  # keep the fixture small: jpeg at 5 fps
                    jpg = pack_bin(KIND_JPEG, rid, i, fake_jpeg(f"{rid} t={t:.1f}s", hue))
                    f.write(json.dumps({"dt": t, "id": rid, "bin": base64.b64encode(jpg).decode()}) + "\n")
                head = {"t": "head", "id": rid, "yaw": 0.6 * math.sin(t * 0.7 + hue), "pitch": 0.15 * math.sin(t * 0.3)}
                f.write(json.dumps({"dt": t, "id": rid, "text": json.dumps(head)}) + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["record", "replay", "fake"])
    ap.add_argument("path")
    ap.add_argument("--url", default="ws://127.0.0.1:8000")
    ap.add_argument("--loop", action="store_true")
    a = ap.parse_args()
    if a.cmd == "record":
        asyncio.run(record(a.path, a.url))
    elif a.cmd == "replay":
        asyncio.run(replay(a.path, a.url, a.loop))
    else:
        fake(a.path)
