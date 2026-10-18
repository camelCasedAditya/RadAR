"""End-to-end check of enhance.py on a synthetic session (no GPU, no S3): one phone, 7 video frames,
a LiDAR patch at frame 3. Every frame but 5 is motion-blurred, so the patch must be re-textured from
frame 5 (within WIN), carrying frame 5's pose and K normalised to the image.
    python test_enhance.py
"""
import io
import json
import pathlib
import struct
import sys
import tempfile

import av
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "relay"))
from enhance import KIND_HD, MAGIC, coverage, enhance, patch_points                         # noqa: E402
from wire import KIND_H264, KIND_PATCH, pack_bin, unpack_bin         # noqa: E402

W, H, S = 518, 392, 3                      # map frame and patch stride, as the live relay
VW, VH = 4 * W, 4 * H                      # "video" bigger than the 1280 texture, so it is downscaled
K = [400.0, 400.0, W / 2, H / 2]
PATCH, SHARP, N = 3, 5, 7


def pose_T(x):                             # column-major ARKit camera-to-world, identity rotation
    return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, 1.4, 0, 1]


def render(f):
    im = Image.new("RGB", (VW, VH), (210, 200, 180))
    d = ImageDraw.Draw(im)
    for u in range(0, VW, 24):
        d.line([(u, 0), (u, VH)], fill=(40, 30, 20), width=4)
    return im if f == SHARP else im.filter(ImageFilter.GaussianBlur(8))


def encode_video(frames):
    """RGB frames -> one Annex-B access unit per frame (libx264, no B-frames, like the phone)."""
    ctx = av.CodecContext.create("libx264", "w")
    ctx.width, ctx.height, ctx.pix_fmt, ctx.framerate = VW, VH, "yuv420p", 30
    ctx.options = {"bf": "0", "tune": "zerolatency", "preset": "veryfast"}
    out = []
    for im in frames:
        fr = av.VideoFrame.from_ndarray(np.asarray(im), format="rgb24").reformat(format="yuv420p")
        out += [bytes(p) for p in ctx.encode(fr)]
    out += [bytes(p) for p in ctx.encode(None)]
    assert len(out) == len(frames), (len(out), len(frames))
    return out


def write_session(path):
    recs = [(0, json.dumps({"t": "state", "responders": {}}))]
    aus = encode_video([render(f) for f in range(N)])
    for f, au in enumerate(aus):
        recs.append((f, json.dumps({"t": "pose", "id": "P", "f": f, "ts": f / 30, "T": pose_T(0.01 * f), "K": K, "w": W, "h": H})))
        recs.append((f, pack_bin(KIND_H264, "P", f, bytes([1 if f == 0 else 0]) + au)))
        if f == PATCH:
            g = np.full((H // S + 1, W // S + 1), 2.0, np.float32)
            hdr = json.dumps({"T": pose_T(0.01 * f), "K": K, "w": W, "h": H, "dw": g.shape[1], "dh": g.shape[0], "s": S}).encode()
            jpg = io.BytesIO(); render(f).resize((W, H)).save(jpg, "JPEG")
            recs.append((f, pack_bin(KIND_PATCH, "P", f, struct.pack("<I", len(hdr)) + hdr + g.astype("<f2").tobytes() + jpg.getvalue())))
    with open(path, "wb") as fh:
        fh.write(MAGIC)
        for t, body in recs:
            b = body.encode() if isinstance(body, str) else body
            fh.write(struct.pack("<IIB", len(b) + 5, t, 0 if isinstance(body, str) else 1) + b)


def main():
    d = pathlib.Path(tempfile.mkdtemp())
    name = "synthetic.radar"
    write_session(d / name)
    meta = enhance(name, local=True, out_dir=d)
    assert meta["status"] == "done" and meta["hd"] and meta["phones"] == ["P"] and meta["patches"] == 1, meta
    assert json.loads((d / f"{name}.json").read_text())["enhanced"]["status"] == "done"
    buf = (d / f"{name}.hd").read_bytes()
    assert buf[:7] == MAGIC
    ln = struct.unpack_from("<I", buf, 7)[0]
    kind, rid, f, payload = unpack_bin(buf[16:11 + ln])
    hl = struct.unpack_from("<I", payload)[0]
    hdr = json.loads(payload[4:4 + hl])
    im = Image.open(io.BytesIO(payload[4 + hl:]))
    assert (kind, rid, f) == (KIND_HD, "P", PATCH), (kind, rid, f)
    assert hdr["sf"] == SHARP, ("sharpest frame not picked", hdr["sf"])
    assert hdr["T"] == pose_T(0.01 * SHARP) and hdr["K"] == [K[0] / W, K[1] / H, 0.5, 0.5], hdr
    assert im.size == (1280, 969), im.size
    g = np.full((H // S + 1, W // S + 1), 2.0, np.float32)
    P = patch_points({"T": pose_T(0), "K": K, "w": W, "h": H, "dw": g.shape[1], "dh": g.shape[0], "s": S}, g)
    assert coverage(P, pose_T(0), K, W, H) == 1.0 and coverage(P, pose_T(1.0), K, W, H) < 0.7   # 1 m to the side sees less
    print(f"enhance ok: patch {f} re-textured from frame {hdr['sf']} at {im.size[0]}x{im.size[1]}")


if __name__ == "__main__":
    main()
