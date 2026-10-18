"""Client for the lingbot-map worker on the GPU pod (/workspace/rad-ar/worker.py, port 8010, /stream).

That worker only sees images and answers in lingbot's own unscaled frame. This side:
  - queues each phone map JPEG in order, keeps only a few in flight (the worker drops when it is
    behind, so we never let it fall behind), and sends  u32 header_len | JSON {"type":"frame","stream","seq"} | jpeg
  - receives                   u32 header_len | JSON result header | depth f16 | conf f16 | rgb u8
  - uses lingbot only for DEPTH: places each frame with the phone's own ARKit pose and lens
    intrinsics (accurate to ~1 cm), and lingbot's pose only to estimate its depth scale (align.py),
    smoothed so the scale does not jitter frame to frame
  - drops low-confidence and far depth, and keeps one point per voxel across all phones
"""
import asyncio
import json
import struct
import time
from collections import deque

import io

import numpy as np
import websockets
from PIL import Image

from align import F, Aligner, arkit_Rp

STREAM_PREFIX = "radar-"     # keep our stream names apart from anyone else using the worker
IN_FLIGHT = 2                # per stream: the worker keeps only 2 pending frames and drops the rest
SCALE_FRAMES = 8             # worker answers nothing until it has this many after a (re)start
SCALE_WAIT = 1.0             # s with NO answers at all before we assume it is collecting scale frames
LOST_AFTER = 5.0             # s before an unanswered frame is written off
# ponytail: fixed quality knobs; tune on site (watch the hub for floaters vs holes).
SCALE_SMOOTH = 0.05          # EMA weight of each new lingbot->metres scale estimate within a segment
MAX_DEPTH = 5.0              # m; lingbot depth is least reliable far away
VOXEL = 0.04                 # m; one point kept per cube, shared by all phones
MIN_BRIGHTNESS = 12          # mean grey level below which a map frame is treated as a dead camera


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


def unproject(h, depth, conf, rgb, conf_min, K=None):
    """-> lingbot camera R, position (lingbot frame), camera-space points (OpenCV axes, lingbot units)
    and colours for confident pixels. K: [fx, fy, cx, cy] of the phone's 518-wide map image; when
    given it replaces lingbot's predicted intrinsics."""
    c2w = np.asarray(h["c2w"], float).reshape(3, 4)
    if K is not None:
        st = h["model_w"] / h["depth_w"]
        fx, fy, cx, cy = (k / st for k in K)
    else:
        fx, fy, cx, cy = h["intrinsics"]      # already scaled to the depth grid
    v, u = np.mgrid[0:depth.shape[0], 0:depth.shape[1]]
    keep = (conf > conf_min) & (depth > 0)
    z = depth[keep]
    cam = np.stack([(u[keep] - cx) / fx * z, (v[keep] - cy) / fy * z, z], 1)
    return c2w[:, :3], c2w[:, 3], cam, rgb[keep]


def is_dark(jpeg: bytes) -> bool:
    """A camera that has not started yet sends pure black frames. If lingbot's first 8 frames are
    black its whole sequence is anchored on nothing and every later frame scores ~0 confidence."""
    try:
        im = Image.open(io.BytesIO(jpeg))
        im.draft("L", (im.width // 8, im.height // 8))
        return np.asarray(im.convert("L").resize((32, 24))).mean() < MIN_BRIGHTNESS
    except Exception:
        return False        # not decodable here; let the worker decide


def voxel_keys(pts, size=VOXEL):
    q = np.floor(pts / size).astype(np.int64) + (1 << 20)        # +-40 km at 4 cm
    return (q[:, 0] << 42) | (q[:, 1] << 21) | q[:, 2]


class GpuLink:
    def __init__(self, url, on_points, on_status, conf_min=1.5, max_points=20_000, queue_cap=15):
        self.url, self.on_points, self.on_status = url, on_points, on_status
        self.conf_min, self.max_points = conf_min, max_points
        self.ws = None
        # ponytail: fixed cap; at ~6 map fps per phone 60 frames is ~10 s of lag before oldest frames drop.
        self.queue: dict[str, deque] = {}               # rid -> (f, jpeg, t_enqueued), oldest first
        self.inflight: dict[str, dict[int, tuple]] = {}  # rid -> {f: (t_enqueued, t_sent)}
        self.last_answer: dict[str, float] = {}          # rid -> time of the last result from the worker
        self.last_nodepth_log: dict[str, float] = {}
        self.wake = asyncio.Event()
        self.queue_cap = queue_cap
        self.stats: dict[str, dict[str, int]] = {}      # rid -> counters, printed every 5 s
        self.last_print = 0.0
        self.poses: dict[str, dict[int, tuple]] = {}     # rid -> {frame id: (ARKit T, K or None)}
        self.scale: dict[str, float] = {}                # rid -> smoothed lingbot->metres scale
        self.seen: set[int] = set()                      # occupied voxels, all phones
        self.aligners: dict[str, Aligner] = {}
        self.segments: dict[str, int] = {}

    def remember_pose(self, rid, f, T, K=None):
        p = self.poses.setdefault(rid, {})
        p[f] = (T, K)
        if len(p) > 300:
            p.pop(next(iter(p)))

    def count(self, rid, key, n=1):
        st = self.stats.setdefault(rid, {"in": 0, "sent": 0, "back": 0, "pts": 0, "skip_nopose": 0, "skip_scale": 0, "new_segment": 0,
                                                "no_depth": 0, "short_travel": 0, "all_seen": 0, "dropped_by_worker": 0, "dark": 0})
        st[key] += n

    def print_stats(self):
        now = time.time()
        if now - self.last_print < 5 or not self.stats:
            return
        self.last_print = now
        for rid, st in self.stats.items():
            if not any(st.values()):
                continue
            print(f"map {rid}: {st['in']} in, {st['sent']} sent, {st['back']} back, {st['pts']} new pts "
                  f"(no pose {st['skip_nopose']}, learning scale {st['skip_scale']}: short travel {st['short_travel']}, "
                  f"no depth {st['no_depth']}, new segments {st['new_segment']}; already mapped {st['all_seen']}; "
                  f"dropped by worker {st['dropped_by_worker']}; dark frames skipped {st['dark']}); "
                  f"queued {len(self.queue.get(rid, ()))}, in flight {len(self.inflight.get(rid, {}))}, "
                  f"link {'up' if self.ws else 'down'}", flush=True)
            for k in st:
                st[k] = 0

    def enqueue(self, rid, f, jpeg):
        self.count(rid, "in")
        if is_dark(jpeg):
            self.count(rid, "dark")
            return
        q = self.queue.setdefault(rid, deque(maxlen=self.queue_cap))
        q.append((f, jpeg, time.time()))
        self.wake.set()

    def window(self, rid, now):
        """How many frames may be at the worker. Normally IN_FLIGHT (it drops beyond that). While it
        collects its 8 scale frames it answers nothing, so if frames are in flight and nothing has
        come back for SCALE_WAIT, widen to 8. Ordinary lag keeps answers flowing, so it never widens then."""
        fl = self.inflight.setdefault(rid, {})
        for f in [f for f, (_, ts) in fl.items() if now - ts > LOST_AFTER]:
            del fl[f]
        oldest_sent = min((ts for _, ts in fl.values()), default=now)
        silent_since = max(self.last_answer.get(rid, 0.0), oldest_sent)
        return SCALE_FRAMES if fl and now - silent_since > SCALE_WAIT else IN_FLIGHT

    async def pump(self):
        """Send queued frames whenever a stream has room in its window."""
        while True:
            try:
                await asyncio.wait_for(self.wake.wait(), 0.25)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            self.print_stats()
            if self.ws is None:
                continue
            now = time.time()
            for rid, q in self.queue.items():
                fl = self.inflight.setdefault(rid, {})
                while q and len(fl) < self.window(rid, now):
                    f, jpeg, t_enq = q.popleft()
                    fl[f] = (t_enq, now)
                    self.count(rid, "sent")
                    try:
                        await self.ws.send(pack({"type": "frame", "stream": STREAM_PREFIX + rid, "seq": f}, jpeg))
                    except websockets.ConnectionClosed:
                        break

    def answered(self, rid, seq):
        """Free the window slot; returns seconds from capture to now (map lag).
        The worker answers in order and silently drops older frames when it is behind, so anything
        older than an answered seq is gone too: free those slots now instead of waiting LOST_AFTER."""
        fl = self.inflight.get(rid, {})
        self.last_answer[rid] = time.time()
        t = fl.pop(seq, None)
        for old in [f for f in fl if f < seq]:
            del fl[old]
            self.count(rid, "dropped_by_worker")
        self.wake.set()
        return time.time() - t[0] if t else None

    async def reset(self, rid):
        """Phone (re)started: its ARKit origin moved, so drop the lingbot sequence and alignment."""
        self.poses.pop(rid, None)
        self.aligners.pop(rid, None)
        self.scale.pop(rid, None)
        self.queue.pop(rid, None)
        self.inflight.pop(rid, None)
        self.last_answer.pop(rid, None)
        if self.ws is not None:
            try:
                await self.ws.send(json.dumps({"type": "reset", "stream": STREAM_PREFIX + rid}))
            except websockets.ConnectionClosed:
                pass

    def handle_result(self, msg):
        h, depth, conf, rgb = decode_result(msg)
        rid = h["stream"].removeprefix(STREAM_PREFIX)
        self.count(rid, "back")
        entry = self.poses.get(rid, {}).get(h["seq"])
        if entry is None:
            self.count(rid, "skip_nopose")
            return None
        T, K = entry
        if self.segments.get(rid) != h["segment"]:      # worker restarted lingbot: new depth scale,
            self.segments[rid] = h["segment"]           # so the old scale is wrong; re-learn it
            self.count(rid, "new_segment")
            self.aligners[rid] = Aligner()
            self.scale.pop(rid, None)
        al = self.aligners.setdefault(rid, Aligner())
        Rl, pl, cam, col = unproject(h, depth, conf, rgb, self.conf_min, K)
        Ra, pa = arkit_Rp(T)
        sim = al.add(Rl, pl, Ra, pa)
        if sim is None or not len(cam):
            self.count(rid, "skip_scale")
            self.count(rid, "no_depth" if not len(cam) else "short_travel")
            if not len(cam) and time.time() - self.last_nodepth_log.get(rid, 0) > 5:
                self.last_nodepth_log[rid] = time.time()
                print(f"map {rid}: seq {h['seq']} had no usable depth: conf median {float(np.nanmedian(conf)):.2f} "
                      f"max {float(np.nanmax(conf)):.2f}, depth median {float(np.nanmedian(depth)):.2f}, "
                      f"nan {bool(np.isnan(conf).any() or np.isnan(depth).any())}, segment {h['segment']} frame {h['frame_index']}", flush=True)
            return None
        prev = self.scale.get(rid)
        s = sim[0] if prev is None else (1 - SCALE_SMOOTH) * prev + SCALE_SMOOTH * sim[0]
        self.scale[rid] = s
        cam = cam * s                                   # metres, OpenCV camera axes
        near = cam[:, 2] < MAX_DEPTH
        xyz = (cam[near] @ (Ra @ F).T + pa).astype(np.float32)   # placed with ARKit's own pose
        col = col[near]
        keys = voxel_keys(xyz)
        _, first = np.unique(keys, return_index=True)   # one point per voxel within the frame
        fresh = np.array([i for i in first if keys[i] not in self.seen], dtype=np.int64)
        if not len(fresh):
            self.count(rid, "all_seen")
            return None
        self.seen.update(keys[fresh].tolist())
        xyz, col = xyz[fresh], col[fresh]
        if len(xyz) > self.max_points:
            idx = np.random.default_rng(h["seq"]).choice(len(xyz), self.max_points, replace=False)
            xyz, col = xyz[idx], col[idx]
        self.count(rid, "pts", len(xyz))
        return rid, h["seq"], xyz, col

    async def run(self):
        pump = asyncio.create_task(self.pump())
        try:
            await self._connect_loop()
        finally:
            pump.cancel()

    async def _connect_loop(self):
        backoff = 1
        while True:
            try:
                async with websockets.connect(self.url, max_size=None, ping_interval=20, ping_timeout=60) as ws:  # pongs queue behind frames on the tunnel
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
                                self.inflight.pop(d.get("stream", "").removeprefix(STREAM_PREFIX), None)
                            continue
                        (hlen,) = struct.unpack_from("<I", m, 0)
                        h = json.loads(m[4:4 + hlen])
                        rid = h["stream"].removeprefix(STREAM_PREFIX)
                        lag = self.answered(rid, h["seq"])
                        try:
                            out = await asyncio.to_thread(self.handle_result, m)
                        except Exception as e:      # one bad frame must not stop mapping
                            print(f"map {rid}: frame {h['seq']} failed: {e!r}", flush=True)
                            continue
                        if out and lag is not None:
                            await self.on_points(*out, lag, len(self.queue.get(rid, ())))
            except (OSError, websockets.ConnectionClosed, websockets.InvalidHandshake) as e:
                print(f"gpu link down ({e!r}); retry in {backoff}s", flush=True)
            self.ws = None
            self.inflight.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)
