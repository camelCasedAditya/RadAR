"""Enhance a saved RadAR session: sharper photos for the map it already has. The geometry is untouched;
each LiDAR patch gets a new texture cut from the stored 1920x1440 video instead of its 518-wide JPEG,
taken from the sharpest frame within WIN frames of the patch (least motion blur) that still sees the
whole patch, and placed with that frame's own ARKit pose. See docs/superpowers/specs/2026-10-17-enhanced-maps-design.md.

    python enhance.py "Gates Undergrad Commons.radar"      # S3 in, S3 out (RADAR_S3_BUCKET)
    python enhance.py x.radar --local                      # files in server/maps only

Output next to the session: <name>.hd, a RADAR2 file whose binary records are relay messages of kind
KIND_HD (patch f, payload: u32 hlen, JSON {sf, T, K}, JPEG) with K normalised to the image (uv = K
applied to camera coords), and the sidecar <name>.json gains  enhanced: {status, hd, phones, ...}.
"""
import argparse
import io
import json
import os
import pathlib
import struct
import sys
import time

import numpy as np
from PIL import Image

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "relay"))
from wire import KIND_H264, KIND_PATCH, pack_bin, unpack_bin  # noqa: E402

MAPS_DIR = HERE.parent / "maps"
MAGIC = b"RADAR2\n"
KIND_HD = 6
WIN = 5                 # frames either side of the patch frame to search for the sharpest one (1/6 s)
OUT_W = 1280            # texture width; 1280x960 is 2.5x the live patch per axis (GPU memory: ~5 MB a patch)
QUALITY = 85
COVER = 0.9             # a neighbour frame must see this share of the patch (the hub shows the original photo where it does not)


def read_session(path):
    """-> {rid: {"poses": {f: (T, K, w, h)}, "video": [(f, bytes)], "patches": {f: world points (n, 3)}}}"""
    out = {}
    ph = lambda rid: out.setdefault(rid, {"poses": {}, "video": [], "patches": {}})
    buf = pathlib.Path(path).read_bytes()
    assert buf[:7] == MAGIC, "not a RADAR2 session file"
    pos = 7
    while pos + 4 <= len(buf):
        ln, typ = struct.unpack_from("<I", buf, pos)[0], buf[pos + 8]
        body = buf[pos + 9:pos + 4 + ln]
        pos += 4 + ln
        if typ == 0:
            d = json.loads(body)
            if d.get("t") == "pose" and "K" in d:
                ph(d["id"])["poses"][d["f"]] = (d["T"], d["K"], d.get("w", 518), d.get("h", 392))
            continue
        kind, rid, f, payload = unpack_bin(body)
        if kind == KIND_H264:
            ph(rid)["video"].append((f, payload[1:]))
        elif kind == KIND_PATCH:
            hl = struct.unpack_from("<I", payload, 0)[0]
            hdr = json.loads(payload[4:4 + hl])
            depth = np.frombuffer(payload, "<f2", hdr["dw"] * hdr["dh"], offset=4 + hl).reshape(hdr["dh"], hdr["dw"])
            ph(rid)["patches"][f] = patch_points(hdr, depth.astype(np.float32))
    return out


def patch_points(hdr, depth, step=4):
    """The patch's LiDAR samples (every step-th grid point) in world coordinates, as the hub builds them."""
    fx, fy, cx, cy = hdr["K"]
    j, i = np.nonzero(depth[::step, ::step] > 0)
    z = depth[::step, ::step][j, i]
    u, v = i * step * hdr["s"], j * step * hdr["s"]
    cam = np.stack([(u - cx) / fx * z, -(v - cy) / fy * z, -z, np.ones_like(z)])   # ARKit camera axes, -Z forward
    return (np.asarray(hdr["T"], float).reshape(4, 4, order="F") @ cam)[:3].T


def coverage(P, T, K, w, h):
    """Share of world points P that land inside the w x h image of the camera (T, K)."""
    cam = np.linalg.inv(np.asarray(T, float).reshape(4, 4, order="F")) @ np.c_[P, np.ones(len(P))].T
    z = -cam[2]
    with np.errstate(divide="ignore", invalid="ignore"):
        u, v = K[0] * cam[0] / z + K[2], -K[1] * cam[1] / z + K[3]
    return float(((z > 0) & (u >= 0) & (u <= w) & (v >= 0) & (v <= h)).mean()) if len(P) else 0.0


def sharpness(gray):
    """Variance of the Laplacian: high = crisp edges, low = motion blur."""
    g = gray.astype(np.float32)
    lap = 4 * g[1:-1, 1:-1] - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
    return float(lap.var())


def best_frames(video, patches, poses):
    """One pass over the H.264 stream. -> {patch f: (source f, rgb at OUT_W)} where source f is the
    sharpest posed frame within WIN of the patch that sees COVER of it (the patch frame always counts). The phone sends no B-frames, so frames decode in send
    order, one per access unit."""
    import av
    ctx = av.CodecContext.create("h264", "r")
    order = [f for f, _ in video]
    best = {}                                       # patch f -> (score, source f, rgb)
    near = {}                                       # frame f -> patch fs whose window holds it
    for p, P in patches.items():
        for f in range(p - WIN, p + WIN + 1):
            if f == p or (f in poses and coverage(P, *poses[f]) >= COVER):
                near.setdefault(f, []).append(p)
    i = 0

    def take(fr):
        nonlocal i
        f = order[i] if i < len(order) else None
        i += 1
        if f not in near:
            return
        s = sharpness(fr.reformat(width=fr.width // 4, height=fr.height // 4, format="gray").to_ndarray())
        rgb = None
        for p in near[f]:
            if p not in best or s > best[p][0]:
                if rgb is None:
                    w = min(OUT_W, fr.width)
                    rgb = fr.reformat(width=w, height=round(fr.height * w / fr.width), format="rgb24").to_ndarray()
                best[p] = (s, f, rgb)

    for _, au in video:
        for fr in ctx.decode(av.Packet(bytes(au))):
            take(fr)
    for fr in ctx.decode(None):
        take(fr)
    return {p: (f, rgb) for p, (_, f, rgb) in best.items()}


def enhance(name, local=False, out_dir=MAPS_DIR, log=print):
    t0 = time.time()
    out_dir = pathlib.Path(out_dir)
    store = None
    if not local:
        from store import S3Store, PREFIX
        store = S3Store(os.environ["RADAR_S3_BUCKET"])
    src = out_dir / name
    if not src.exists():
        if not store:
            raise FileNotFoundError(src)
        log(f"fetching s3://{store.bucket}/{PREFIX}{name}")
        store.get_file(PREFIX + name, src)
    meta_path = out_dir / f"{name}.json"
    meta = (store.get_json(PREFIX + name + ".json") if store else None) or \
           (json.loads(meta_path.read_text()) if meta_path.exists() else {})

    def save_meta(enh):
        meta["enhanced"] = enh
        meta_path.write_text(json.dumps(meta))
        if store:
            store.put_json(PREFIX + name + ".json", meta)

    save_meta({"status": "running", "since": time.time()})
    try:
        hd = out_dir / f"{name}.hd"
        done, n = [], 0
        with open(hd, "wb") as fh:
            fh.write(MAGIC)
            for rid, ph in read_session(src).items():
                if not ph["patches"]:
                    log(f"{rid}: no LiDAR patches, skipped")
                    continue
                picks = best_frames(ph["video"], ph["patches"], ph["poses"])
                moved = 0
                for p in sorted(picks):
                    sf, rgb = picks[p]
                    T, K, w, h = ph["poses"][sf]
                    hdr = json.dumps({"sf": sf, "T": T, "K": [K[0] / w, K[1] / h, K[2] / w, K[3] / h]}).encode()
                    jpg = io.BytesIO()
                    Image.fromarray(rgb).save(jpg, "JPEG", quality=QUALITY)
                    body = pack_bin(KIND_HD, rid, p, struct.pack("<I", len(hdr)) + hdr + jpg.getvalue())
                    fh.write(struct.pack("<IIB", len(body) + 5, 0, 1) + body)
                    moved += sf != p
                if picks:
                    log(f"{rid}: {len(picks)} of {len(ph['patches'])} patches re-textured at {rgb.shape[1]}x{rgb.shape[0]}, "
                        f"{moved} from a sharper neighbour frame")
                    done.append(rid)
                    n += len(picks)
        if not done:
            raise RuntimeError("needs a LiDAR scan (no patches in this session)")
        if store:
            store.put_file(hd, PREFIX + hd.name)
        save_meta({"status": "done", "hd": True, "phones": done, "patches": n,
                   "bytes": hd.stat().st_size, "seconds": int(time.time() - t0)})
        log(f"done in {time.time() - t0:.0f}s: {hd.name} {hd.stat().st_size / 1e6:.1f} MB")
    except Exception as e:
        save_meta({"status": "failed", "error": f"{type(e).__name__}: {e}"[:300]})
        raise
    return meta["enhanced"]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--local", action="store_true", help="no S3: read and write server/maps only")
    ap.add_argument("--out", default=str(MAPS_DIR))
    a = ap.parse_args()
    enhance(a.name, local=a.local, out_dir=a.out)
