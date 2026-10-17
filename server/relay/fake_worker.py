"""CPU stand-in for the GPU pod's lingbot worker, same wire protocol (port 8010, /stream).

It cheats: it looks up each frame's true pose in a session file (session.py fake), ray-casts the
synthetic corridor walls, then reports everything the way lingbot would — OpenCV camera axes, in a
secret scaled/rotated/shifted frame. If the relay's alignment is right, the hub draws the walls
exactly around the walking path.

    python fake_worker.py ../sessions/fake_corridor.jsonl
"""
import argparse
import json
import struct

import numpy as np

F = np.diag([1.0, -1.0, -1.0])                     # ARKit camera axes <-> OpenCV camera axes
S, YAW, TS = 0.37, 0.9, np.array([3.0, -1.0, 2.0])   # lingbot = S * RS @ arkit + TS
RS = np.array([[np.cos(YAW), 0, np.sin(YAW)], [0, 1, 0], [-np.sin(YAW), 0, np.cos(YAW)]])
W, H, STRIDE, FX = 518, 392, 4, 400.0
DW, DH = -(-W // STRIDE), -(-H // STRIDE)
# walls 1 m either side of session.py's 12 x 6 m loop, 0..2.5 m high: (axis, value, lo, hi)
WALLS = [("z", -1, -1, 13), ("z", 1, 1, 11), ("x", 13, -1, 7), ("x", 11, 1, 5),
         ("z", 7, -1, 13), ("z", 5, 1, 11), ("x", -1, -1, 7), ("x", 1, 1, 5)]


def render(T16):
    """ARKit pose -> (lingbot c2w 3x4, intrinsics on depth grid, depth, conf, rgb) as the worker would send."""
    T = np.asarray(T16, float).reshape(4, 4, order="F")
    Ra, pa = T[:3, :3], T[:3, 3]
    fx = FX / STRIDE
    cx, cy = (W / 2) / STRIDE, (H / 2) / STRIDE
    v, u = np.mgrid[0:DH, 0:DW]
    d_cv = np.stack([(u - cx) / fx, (v - cy) / fx, np.ones_like(u, float)], -1)   # depth-1 rays
    d = d_cv @ (Ra @ F).T                                                             # world dirs
    depth = np.full((DH, DW), np.inf)
    hit_y = np.zeros((DH, DW))
    for axis, val, lo, hi in WALLS:
        i, j = (0, 2) if axis == "x" else (2, 0)
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (val - pa[i]) / d[..., i]
        along = pa[j] + t * d[..., j]
        y = pa[1] + t * d[..., 1]
        ok = (t > 0.05) & (along >= lo) & (along <= hi) & (y >= 0) & (y <= 2.5) & (t < depth)
        depth[ok], hit_y[ok] = t[ok], y[ok]
    seen = np.isfinite(depth)
    h = hit_y / 2.5
    rgb = np.stack([120 + 100 * h, 140 + 0 * h, 200 - 100 * h], -1).astype(np.uint8)
    Rl = RS @ Ra @ F
    c2w = np.c_[Rl, S * RS @ pa + TS]
    return c2w, [fx, fx, cx, cy], np.where(seen, depth * S, 0), np.where(seen, 5.0, 0.0), rgb


def result_msg(stream, seq, segment, idx, T16):
    c2w, K, depth, conf, rgb = render(T16)
    header = {"type": "result", "stream": stream, "seq": seq, "segment": segment, "frame_index": idx,
              "c2w": c2w.reshape(-1).tolist(), "intrinsics": K, "model_w": W, "model_h": H,
              "depth_w": DW, "depth_h": DH, "src_w": W, "src_h": H, "infer_ms": 1.0, "keyframe": True}
    h = json.dumps(header).encode()
    return (struct.pack("<I", len(h)) + h + depth.astype(np.float16).tobytes()
            + conf.astype(np.float16).tobytes() + rgb.tobytes())


def make_app(session_path, scale_frames=8):
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    poses = {}
    for line in open(session_path):
        r = json.loads(line)
        if "text" in r:
            d = json.loads(r["text"])
            if d["t"] == "pose":
                poses[(d["id"], d["f"])] = d["T"]
    app = FastAPI()

    @app.websocket("/stream")
    async def stream(ws: WebSocket):
        await ws.accept()
        st = {}   # stream -> {"segment", "buf", "n"}
        try:
            while True:
                m = await ws.receive()
                if m["type"] == "websocket.disconnect":
                    break
                if m.get("text"):
                    d = json.loads(m["text"])
                    if d.get("type") == "reset" and d["stream"] in st:
                        s = st[d["stream"]]
                        s["segment"] += 1; s["buf"] = []; s["n"] = 0
                    continue
                data = m["bytes"]
                (hlen,) = struct.unpack_from("<I", data, 0)
                hd = json.loads(data[4:4 + hlen])
                name, seq = hd["stream"], hd["seq"]
                s = st.setdefault(name, {"segment": 1, "buf": [], "n": 0})
                T = poses.get((name.removeprefix("radar-"), seq))
                if T is None:
                    continue
                s["buf"].append((seq, T))
                if s["n"] < scale_frames and len(s["buf"]) < scale_frames:
                    continue                      # like lingbot: the scale block answers all at once
                for q, Tq in s["buf"]:
                    await ws.send_bytes(result_msg(name, q, s["segment"], s["n"], Tq))
                    s["n"] += 1
                s["buf"] = []
                if seq % 20 == 0:
                    await ws.send_text(json.dumps({"type": "status", "streams": len(st), "gpu": "fake", "mem_gb": 0, "fps": 10.0}))
        except WebSocketDisconnect:
            pass

    return app


if __name__ == "__main__":
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("--port", type=int, default=8010)
    a = ap.parse_args()
    uvicorn.run(make_app(a.session), host="127.0.0.1", port=a.port, ws_max_size=16 * 1024 * 1024)
