"""RadAR relay. Runs on the MacBook. See protocol.md.

    python relay.py                                        # phones + hub only
    python relay.py --gpu ws://localhost:8010/stream       # plus 3D reconstruction (GPU worker via SSH tunnel)
"""
import argparse
import asyncio
import json
import pathlib
import struct
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from gpu_link import GpuLink
from wire import KIND_DEPTH, KIND_H264, KIND_JPEG, KIND_PATCH, KIND_POINTS, pack_bin, pack_points, unpack_bin

HUB_DIR = pathlib.Path(__file__).resolve().parents[2] / "hub"
MAPS_DIR = pathlib.Path(__file__).resolve().parents[1] / "maps"

hubs: set[WebSocket] = set()
state: dict[str, dict] = {}          # id -> {msg type -> latest msg}
last_jpeg: dict[str, bytes] = {}     # id -> latest jpeg binary message
gop: dict[str, list[bytes]] = {}     # id -> latest h264 keyframe + frames since, for late hubs
link: GpuLink | None = None


class MapFile:
    """The map is the sequence of LiDAR patch messages. Each session appends them to
    maps/<start time>.radar as  u32 len | kind-5 message  records; the hub replays a file to show
    a saved map. ponytail: a flat file, not a DB; one file per session, nothing is ever rewritten."""
    def __init__(self):
        MAPS_DIR.mkdir(exist_ok=True)
        self.new()

    def new(self):
        self.name = time.strftime("%Y%m%d-%H%M%S") + ".radar"
        self.f = open(MAPS_DIR / self.name, "ab")
        self.patches = 0

    def append(self, msg: bytes):
        self.f.write(struct.pack("<I", len(msg)) + msg)
        self.f.flush()
        self.patches += 1

    @staticmethod
    def listing():
        out = []
        for p in sorted(MAPS_DIR.glob("*.radar"), reverse=True):
            n, pos, size = 0, 0, p.stat().st_size
            with open(p, "rb") as f:
                while pos + 4 <= size:
                    (ln,) = struct.unpack("<I", f.read(4))
                    f.seek(ln, 1)
                    pos += 4 + ln
                    n += 1
            if n or p.name == maps.name:                # skip empty leftovers from earlier runs
                out.append({"name": p.name, "bytes": size, "patches": n})
        return out


maps = MapFile()


async def send_hubs(msg):
    for ws in list(hubs):
        try:
            await (ws.send_text(msg) if isinstance(msg, str) else ws.send_bytes(msg))
        except Exception:
            hubs.discard(ws)


async def on_points(rid, f, xyz, rgb, lag, queued):
    await send_hubs(pack_bin(KIND_POINTS, rid, f, pack_points(xyz, rgb)))
    await send_hubs(json.dumps({"t": "maplag", "id": rid, "lag": round(lag, 2), "queued": queued}))


async def on_status(d):
    await send_hubs(json.dumps({"t": "gpu", "id": "", **d}))


def make_app(gpu_url=None, conf_min=2.0):
    @asynccontextmanager
    async def lifespan(_app):
        global link
        link = GpuLink(gpu_url, on_points, on_status, conf_min=conf_min)   # LiDAR phones map without a GPU
        task = asyncio.create_task(link.run()) if gpu_url else None
        yield
        if task:
            task.cancel()

    app = FastAPI(lifespan=lifespan)

    @app.websocket("/phone/{rid}")
    async def phone(ws: WebSocket, rid: str):
        await ws.accept()
        try:
            while True:
                m = await ws.receive()
                if m["type"] == "websocket.disconnect":
                    break
                if m.get("text") is not None:
                    d = json.loads(m["text"])
                    d["id"] = rid
                    state.setdefault(rid, {})[d["t"]] = d
                    await send_hubs(json.dumps(d))
                    if link and d["t"] == "hello":
                        await link.reset(rid)
                    elif link and d["t"] == "pose":
                        link.remember_pose(rid, d["f"], d["T"], d.get("K"), d.get("fp"), (d.get("w", 518), d.get("h", 392)))
                elif m.get("bytes") is not None:
                    b = m["bytes"]
                    kind, _, f, payload = unpack_bin(b)
                    if kind == KIND_JPEG:
                        last_jpeg[rid] = b
                        await send_hubs(b)
                        if link:
                            link.enqueue(rid, f, payload)
                    elif kind == KIND_DEPTH:            # LiDAR phone: metric depth -> textured patch, no GPU
                        patch = await asyncio.to_thread(link.handle_lidar, rid, f, payload)
                        if patch:
                            msg = pack_bin(KIND_PATCH, rid, f, patch)
                            maps.append(msg)
                            await send_hubs(msg)
                            await send_hubs(json.dumps({"t": "maplag", "id": rid, "lag": 0, "queued": 0}))
                    elif kind == KIND_H264:
                        if payload[0] & 1:
                            gop[rid] = []
                        if rid in gop and len(gop[rid]) < 120:   # ponytail: caps a stuck GOP at ~4 s of video
                            gop[rid].append(b)
                        await send_hubs(b)
        except WebSocketDisconnect:
            pass

    @app.websocket("/hub")
    async def hub(ws: WebSocket):
        await ws.accept()
        await ws.send_text(json.dumps({"t": "state", "responders": state}))
        for b in last_jpeg.values():
            await ws.send_bytes(b)
        for frames in gop.values():
            for b in frames:
                await ws.send_bytes(b)
        hubs.add(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            hubs.discard(ws)

    @app.get("/maps")
    def list_maps():
        return {"current": maps.name, "maps": MapFile.listing()}

    @app.post("/maps/new")
    async def new_map():
        maps.new()
        if link:
            link.seen.clear()
            link.keyposes.clear()
        await send_hubs(json.dumps({"t": "map_new", "id": "", "name": maps.name}))
        return {"current": maps.name}

    @app.get("/maps/{name}")
    def get_map(name: str):
        p = MAPS_DIR / pathlib.Path(name).name          # no path tricks
        return FileResponse(p, media_type="application/octet-stream") if p.suffix == ".radar" and p.exists() else {"error": "no such map"}

    @app.middleware("http")
    async def no_cache(request, call_next):
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-cache"     # hub edits show up on plain reload
        return resp

    if HUB_DIR.exists():
        app.mount("/", StaticFiles(directory=HUB_DIR, html=True), name="hub")
    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--gpu", help="lingbot worker websocket, e.g. ws://localhost:8010/stream")
    # ponytail: one global confidence cut. Real indoor walks score far lower than the courthouse sample
    # (median ~1-2, p90 ~2.3), so 3 keeps nothing; 2 keeps the solid third now that depth comes at stride 3. Tune by watching for floaters vs holes.
    ap.add_argument("--conf", type=float, default=2.0, help="drop depth pixels below this lingbot confidence")
    a = ap.parse_args()
    uvicorn.run(make_app(a.gpu, a.conf), host="0.0.0.0", port=a.port, ws_max_size=8 * 1024 * 1024)
