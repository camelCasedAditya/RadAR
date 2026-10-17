"""Client for the lingbot-map worker on the GPU pod (/workspace/rad-ar/worker.py, port 8010, /stream).

That worker only sees images and answers in lingbot's own unscaled frame. This side:
  - sends each phone JPEG as  u32 header_len | JSON {"type":"frame","stream","seq"} | jpeg
  - receives                   u32 header_len | JSON result header | depth f16 | conf f16 | rgb u8
  - unprojects depth with the returned c2w (OpenCV axes) and intrinsics
  - aligns lingbot -> ARKit metric frame with the phone's own pose for the same seq (align.py)
"""
import asyncio
import json
import struct

import numpy as np
import websockets

from align import Aligner, arkit_Rp

STREAM_PREFIX = "radar-"     # keep our stream names apart from anyone else using the worker


def pack(header: dict, payload: bytes = b"") -> bytes:
    h = json.dumps(header).encode()
    return struct.pack("<I", len(h)) + h + payload


def decode_result(msg: bytes):
    (hlen,) = struct.unpack_from("<I", msg, 0)
    h = json.loads(msg[4:4 + hlen])
    n = h["depth_w"] * h["depth_h"]
    body = msg[4 + hlen:]
    shape = (h["depth_h"], h["depth_w"])
    depth = np.frombuffer(body[:2 * n], np.float16).reshape(shape).astype(np.float32)
    conf = np.frombuffer(body[2 * n:4 * n], np.float16).reshape(shape).astype(np.float32)
    rgb = np.frombuffer(body[4 * n:4 * n + 3 * n], np.uint8).reshape(*shape, 3)
    return h, depth, conf, rgb


def unproject(h, depth, conf, rgb, conf_min):
    """-> camera R, position (lingbot frame) and world points + colours for confident pixels."""
    c2w = np.asarray(h["c2w"], float).reshape(3, 4)
    fx, fy, cx, cy = h["intrinsics"]          # already scaled to the depth grid
    v, u = np.mgrid[0:depth.shape[0], 0:depth.shape[1]]
    keep = (conf > conf_min) & (depth > 0)
    z = depth[keep]
    cam = np.stack([(u[keep] - cx) / fx * z, (v[keep] - cy) / fy * z, z], 1)
    return c2w[:, :3], c2w[:, 3], cam @ c2w[:, :3].T + c2w[:, 3], rgb[keep]


class GpuLink:
    def __init__(self, url, on_points, on_status, conf_min=1.5, max_points=20_000):
        self.url, self.on_points, self.on_status = url, on_points, on_status
        self.conf_min, self.max_points = conf_min, max_points
        self.ws = None
        self.poses: dict[str, dict[int, list]] = {}      # rid -> {frame id: ARKit T}
        self.aligners: dict[str, Aligner] = {}
        self.segments: dict[str, int] = {}

    def remember_pose(self, rid, f, T):
        p = self.poses.setdefault(rid, {})
        p[f] = T
        if len(p) > 300:
            p.pop(next(iter(p)))

    async def send_frame(self, rid, f, jpeg):
        if self.ws is None:
            return
        try:
            await self.ws.send(pack({"type": "frame", "stream": STREAM_PREFIX + rid, "seq": f}, jpeg))
        except websockets.ConnectionClosed:
            pass

    async def reset(self, rid):
        """Phone (re)started: its ARKit origin moved, so drop the lingbot sequence and alignment."""
        self.poses.pop(rid, None)
        self.aligners.pop(rid, None)
        if self.ws is not None:
            try:
                await self.ws.send(json.dumps({"type": "reset", "stream": STREAM_PREFIX + rid}))
            except websockets.ConnectionClosed:
                pass

    def handle_result(self, msg):
        h, depth, conf, rgb = decode_result(msg)
        rid = h["stream"].removeprefix(STREAM_PREFIX)
        T = self.poses.get(rid, {}).get(h["seq"])
        if T is None:
            return None
        al = self.aligners.setdefault(rid, Aligner())
        if self.segments.get(rid) != h["segment"]:      # worker restarted lingbot: new frame
            self.segments[rid] = h["segment"]
            al.reset()
        Rl, pl, xyz, col = unproject(h, depth, conf, rgb, self.conf_min)
        Ra, pa = arkit_Rp(T)
        if al.add(Rl, pl, Ra, pa) is None or not len(xyz):
            return None
        if len(xyz) > self.max_points:
            idx = np.random.default_rng(h["seq"]).choice(len(xyz), self.max_points, replace=False)
            xyz, col = xyz[idx], col[idx]
        return rid, h["seq"], al.apply(xyz), col, h.get("infer_ms")

    async def run(self):
        backoff = 1
        while True:
            try:
                async with websockets.connect(self.url, max_size=None, ping_interval=20) as ws:
                    print(f"gpu link up: {self.url}", flush=True)
                    self.ws, backoff = ws, 1
                    self.aligners.clear()
                    async for m in ws:
                        if isinstance(m, str):
                            d = json.loads(m)
                            if d.get("type") == "status":
                                await self.on_status(d)
                            elif d.get("type") == "error":
                                print("gpu error:", d, flush=True)
                            continue
                        out = await asyncio.to_thread(self.handle_result, m)
                        if out:
                            await self.on_points(*out)
            except (OSError, websockets.ConnectionClosed, websockets.InvalidHandshake) as e:
                print(f"gpu link down ({e!r}); retry in {backoff}s", flush=True)
            self.ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)
