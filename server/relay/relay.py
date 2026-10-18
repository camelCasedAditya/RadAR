"""RadAR relay. Runs on the MacBook. See protocol.md.

    python relay.py                                        # phones + hub only
    python relay.py --gpu ws://localhost:8010/stream       # plus 3D reconstruction (GPU worker via SSH tunnel)
"""
import argparse
import asyncio
import json
import os
import pathlib
import re
import struct
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse
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
    """Session file: everything the relay sends hubs (poses, video, map JPEGs, points, LiDAR patches),
    so a file replays as the live hub saw it, or loads instantly as just the map.
    maps/<start time>.radar = b"RADAR2\n" then records  u32 len | u32 t_ms | u8 type | body
    (type 0 = text JSON, 1 = binary message; len covers t_ms + type + body). A file opens with the
    responder state and each phone's latest video keyframe group, like a late-joining hub gets.
    ponytail: a flat append-only file, not a DB; one file per session, nothing is ever rewritten."""
    MAGIC = b"RADAR2\n"

    def __init__(self):
        MAPS_DIR.mkdir(exist_ok=True)
        self.f = None
        self.store = None                                # S3 copy, see store.py
        up = {}
        if os.environ.get("RADAR_S3_BUCKET"):
            try:
                from store import S3Store
                self.store = S3Store(os.environ["RADAR_S3_BUCKET"])
                up = self.store.listing()                # also proves the credentials work
                print(f"s3: sessions go to s3://{self.store.bucket}/sessions/", flush=True)
            except Exception as e:                       # no creds / no boto3: local files only
                print(f"s3: disabled ({e!r})", flush=True)
                self.store = None
        self.name = None                                 # recording only between start() and stop()
        if self.store:                                   # crashed sessions: upload whole files now
            for m in self.local():
                if m["name"] not in up:
                    self.store.sync_file(MAPS_DIR / m["name"], {k: m[k] for k in ("patches", "mapped", "seconds", "bytes")})

    def status(self):
        return {"t": "recording", "id": "", "name": self.name and self.name[:-6], "since": self.name and self.t0}

    def start(self, label):
        """Begin a named session. The name becomes the file name, so it is reduced to safe characters."""
        self.stop()
        label = re.sub(r"[^A-Za-z0-9 _.-]+", "", label).strip()[:60] or time.strftime("%Y-%m-%d %H%M")
        if (MAPS_DIR / f"{label}.radar").exists():
            label += time.strftime(" %H%M%S")
        self.name = f"{label}.radar"
        self.f = open(MAPS_DIR / self.name, "ab")
        self.t0, self.patches, self.mapped, self.t_ms, self.bytes = time.time(), 0, 0, 0, 0
        if self.store:
            self.store.begin(self.name)
        self.write(self.MAGIC)
        self.append(json.dumps({"t": "state", "responders": state}))
        for frames in gop.values():
            for b in frames:
                self.append(b)
        return self.status()

    def stop(self):
        """End the session; the S3 upload completes in the background. -> what was saved, or None."""
        if not self.f:
            return None
        self.f.close()
        meta = {"name": self.name, "patches": self.patches, "mapped": self.mapped, "seconds": self.t_ms // 1000, "bytes": self.bytes}
        if self.store:
            self.store.finish(meta)
        if not self.mapped:                              # nothing mapped: not worth keeping
            (MAPS_DIR / self.name).unlink(missing_ok=True)
        self.f = self.name = None
        return meta

    def write(self, b):
        self.f.write(b)
        self.f.flush()
        self.bytes += len(b)
        if self.store:
            self.store.write(b)

    def append(self, msg):
        body = msg.encode() if isinstance(msg, str) else msg
        self.t_ms = int((time.time() - self.t0) * 1000)
        if not isinstance(msg, str):
            self.patches += body[0] == KIND_PATCH
            self.mapped += body[0] in (KIND_POINTS, KIND_PATCH)   # a session that mapped nothing is not kept
        self.write(struct.pack("<IIB", len(body) + 5, self.t_ms, 0 if isinstance(msg, str) else 1) + body)

    def local(self):
        out = []
        for p in sorted(MAPS_DIR.glob("*.radar"), reverse=True):
            size = p.stat().st_size
            patches, mapped, t_ms, pos = 0, 0, 0, 0
            with open(p, "rb") as f:
                if f.read(len(self.MAGIC)) != self.MAGIC:
                    continue                             # pre-RADAR2 patch-only file; not listed
                pos = len(self.MAGIC)
                while pos + 4 <= size:
                    ln, t_ms, typ = struct.unpack("<IIB", f.read(9))
                    first = f.read(1)
                    f.seek(ln - 6, 1)
                    pos += 4 + ln
                    patches += typ == 1 and first == bytes([KIND_PATCH])
                    mapped += typ == 1 and first in (bytes([KIND_POINTS]), bytes([KIND_PATCH]))
            if mapped or p.name == self.name:           # skip sessions that mapped nothing (deleted at stop anyway)
                out.append({"name": p.name, "bytes": size, "patches": patches, "mapped": mapped, "seconds": t_ms // 1000, "where": "local"})
        return out

    def listing(self):
        """Local files plus finished sessions in S3; a session in both places is listed once."""
        out = {m["name"]: m for m in self.local()}
        if self.store:
            for name, meta in self.store.listing().items():
                if name in out:
                    out[name]["where"] = "both"
                else:
                    out[name] = {"name": name, **meta, "where": "s3"}
        return sorted(out.values(), key=lambda m: m["name"], reverse=True)


maps = MapFile()


async def send_hubs(msg):
    if maps.f:                           # while recording, everything a hub sees goes in the session file
        maps.append(msg)
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
        maps.stop()                                     # finish the S3 upload of the current session
        if maps.store:
            maps.store.close()

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
                            await send_hubs(pack_bin(KIND_PATCH, rid, f, patch))
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
        await ws.send_text(json.dumps(maps.status()))
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
        return {**maps.status(), "bucket": maps.store.bucket if maps.store else None, "maps": maps.listing()}

    async def tell_hubs_recording():
        for ws in list(hubs):                           # not via send_hubs: must not land in the file
            await ws.send_text(json.dumps(maps.status()))

    @app.post("/maps/start")
    async def start_map(body: dict):
        maps.start(str(body.get("name", "")))
        if link:                                        # fresh map: forget what was mapped before
            link.seen.clear()
            link.keyposes.clear()
        await tell_hubs_recording()
        return maps.status()

    @app.post("/maps/stop")
    async def stop_map():
        saved = maps.stop()
        await tell_hubs_recording()
        return {"saved": saved}

    @app.get("/maps/{name}")
    def get_map(name: str):
        name = pathlib.Path(name).name                  # no path tricks
        p = MAPS_DIR / name
        if p.suffix != ".radar":
            return {"error": "no such map"}
        if p.exists():
            return FileResponse(p, media_type="application/octet-stream")
        if maps.store:
            return RedirectResponse(maps.store.url(name))   # straight from S3, signed for an hour
        return {"error": "no such map"}

    @app.delete("/maps/{name}")
    def delete_map(name: str):
        name = pathlib.Path(name).name
        if name == maps.name:
            return {"error": "that session is still recording"}
        (MAPS_DIR / name).unlink(missing_ok=True)
        if maps.store:
            maps.store.delete(name)
        return {"deleted": name}

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
