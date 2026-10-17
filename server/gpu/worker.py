"""GPU worker: one process per responder. Connects OUT to the relay (works behind RunPod NAT).

    python worker.py --relay wss://xxxx.ngrok-free.app --responder A --model_path lingbot-map.pt
    python worker.py --relay ws://127.0.0.1:8000 --responder A --fake      # CPU test, no GPU
"""
import argparse
import asyncio
import io
import json
import pathlib
import sys
import time

import numpy as np
import websockets
from PIL import Image

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "relay"))
from wire import KIND_JPEG, KIND_POINTS, pack_bin, pack_points, unpack_bin  # noqa: E402

from align import Aligner, arkit_Rp  # noqa: E402

CONF_MIN = 1.5      # lingbot's demo default (conf is >= 1, not 0..1)
MAX_PTS = 20_000


def process(rec, aligner, rid, f, jpeg, pose, fake):
    rgb = np.asarray(Image.open(io.BytesIO(jpeg)).convert("RGB"))
    h, wd = rgb.shape[:2]
    if wd != 518 or h % 14:     # lingbot "crop" sizing: width 518, height rounded to a multiple of 14
        rgb = np.asarray(Image.fromarray(rgb).resize((518, round(h * 518 / wd / 14) * 14), Image.BICUBIC))
    out = rec.push(rgb, (f, pose), pose["T"]) if fake else rec.push(rgb, (f, pose))
    msgs = []
    for (tf, tpose), r in out:
        if r["reseed"]:
            aligner.reset()
        Ra, pa = arkit_Rp(tpose["T"])
        aligner.add(r["R"], r["cam"], Ra, pa)
        if aligner.sim is None:
            continue    # not enough travel yet to know scale
        keep = r["conf"] > CONF_MIN
        xyz, col = r["xyz"][keep], r["rgb"][keep]
        if len(xyz) > MAX_PTS:
            idx = np.random.default_rng(tf).choice(len(xyz), MAX_PTS, replace=False)
            xyz, col = xyz[idx], col[idx]
        msgs.append(pack_bin(KIND_POINTS, rid, tf, pack_points(aligner.apply(xyz), col)))
    return msgs


async def run(a, rec):
    url = f"{a.relay.rstrip('/')}/gpu/{a.responder}"
    backoff = 1
    while True:
        try:
            async with websockets.connect(url, max_size=None, ping_interval=20) as ws:
                print("connected", url, flush=True)
                backoff = 1
                rec.restart()
                aligner = Aligner()
                latest = {"job": None}   # newest unprocessed (jpeg, pose); older ones dropped
                poses = {}
                wake = asyncio.Event()

                async def reader():
                    async for m in ws:
                        if isinstance(m, str):
                            d = json.loads(m)
                            if d["t"] == "pose":
                                poses[d["f"]] = d
                                if len(poses) > 50:
                                    poses.pop(next(iter(poses)))
                        else:
                            kind, _, f, payload = unpack_bin(m)
                            if kind == KIND_JPEG and f in poses:
                                latest["job"] = (f, payload, poses[f])
                                wake.set()

                async def compute():
                    n, t0 = 0, time.time()
                    while True:
                        await wake.wait()
                        wake.clear()
                        f, jpeg, pose = latest["job"]
                        msgs = await asyncio.to_thread(process, rec, aligner, a.responder, f, jpeg, pose, a.fake)
                        for m in msgs:
                            await ws.send(m)
                        n += 1
                        if time.time() - t0 > 5:
                            print(f"{a.responder}: {n / (time.time() - t0):.1f} fps processed", flush=True)
                            n, t0 = 0, time.time()

                await asyncio.gather(reader(), compute())
        except (OSError, websockets.ConnectionClosed) as e:
            print(f"disconnected ({e}); retry in {backoff}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--relay", required=True)
    ap.add_argument("--responder", required=True)
    ap.add_argument("--model_path")
    ap.add_argument("--use_sdpa", action="store_true")
    ap.add_argument("--fake", action="store_true", help="CPU fake model for pipeline tests")
    a = ap.parse_args()
    if a.fake:
        from live_model import FakeReconstructor
        rec = FakeReconstructor()
    else:
        from live_model import LiveReconstructor
        rec = LiveReconstructor(a.model_path, use_sdpa=a.use_sdpa)
    asyncio.run(run(a, rec))
