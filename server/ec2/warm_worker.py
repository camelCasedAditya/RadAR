"""Build the worker's model pool BEFORE any phone connects. Run on the pod after the worker is healthy.

The worker builds a lingbot model on demand when a new stream appears. Building one while another
stream is live corrupts the live stream (its confidence drops to the floor for good) and the new model
starts out broken too. Models that already exist and sit in the pool work fine side by side. So: create
`--streams` throwaway streams one after another now, each with enough frames to force its build, then let
them go idle. Phones then always pick a ready model from the pool and no build ever happens mid-stream.
"""
import argparse, asyncio, glob, io, json, struct
import websockets
from PIL import Image

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="ws://127.0.0.1:8010/stream")
ap.add_argument("--streams", type=int, default=3, help="= the worker's --max_streams")
ap.add_argument("--images", default="/workspace/lingbot-map/example/courthouse")
a = ap.parse_args()

def pack(h, payload=b""):
    h = json.dumps(h).encode(); return struct.pack("<I", len(h)) + h + payload

paths = sorted(glob.glob(a.images + "/*"))[:12]
frames = []
for p in paths:
    im = Image.open(p).convert("RGB"); im = im.resize((518, round(im.height * 518 / im.width / 14) * 14))
    b = io.BytesIO(); im.save(b, "JPEG", quality=60); frames.append(b.getvalue())

async def main():
    async with websockets.connect(a.url, max_size=None, ping_timeout=None) as ws:
        for s in range(a.streams):
            name = f"radar-warm{s}"
            await ws.send(json.dumps({"type": "reset", "stream": name}))
            for i, j in enumerate(frames):     # paced: the worker keeps only 2 pending frames once streaming
                await ws.send(pack({"type": "frame", "stream": name, "seq": i}, j))
                await asyncio.sleep(0.2)
            got = 0
            while got < 9:                     # 8 scale frames + 1 streaming frame = the model is built and working
                m = await asyncio.wait_for(ws.recv(), 600)
                got += not isinstance(m, str)
            print(f"warm stream {s + 1}/{a.streams} built", flush=True)
asyncio.run(main())
