"""Live, frame-at-a-time lingbot-map.

lingbot's own `inference_streaming` needs the whole video up front. This replays its loop
(lingbot_map/models/gct_stream.py, inference_streaming) one frame at a time:
  1. buffer `scale` frames, run them as one bidirectional block
  2. then one causal forward per frame, KV cache kept only for keyframes
  3. when the cache nears its trained range, wipe it and re-seed from the last `scale` frames

push() returns a list of (tag, result) because the scale step emits several frames at once.
result = {"cam": (3,) camera position, "R": (3,3) camera-to-world rotation (OpenCV axes), both in lingbot frame,
          "xyz": (N,3) world points, "rgb": (N,3) uint8, "conf": (N,), "reseed": bool}
`reseed` True means lingbot's frame just changed: reset the Sim(3) aligner.
"""
from collections import deque

import numpy as np

STRIDE = 4  # keep every 4th pixel each way: 518x392 -> ~12.7k candidate points per frame


class LiveReconstructor:
    def __init__(self, model_path, scale=8, keyframe_interval=6, max_keyframes=300, use_sdpa=False):
        import torch
        from lingbot_map.models.gct_stream import GCTStream
        self.torch = torch
        dev = "cuda"
        m = GCTStream(img_size=518, patch_size=14, enable_3d_rope=True, max_frame_num=1024,
                      kv_cache_sliding_window=64, kv_cache_scale_frames=scale,
                      kv_cache_cross_frame_special=True, kv_cache_include_scale_frames=True,
                      use_sdpa=use_sdpa, camera_num_iterations=4)
        ck = torch.load(model_path, map_location=dev, weights_only=False)
        m.load_state_dict(ck.get("model", ck), strict=False)
        m = m.to(dev).eval()
        self.dtype = torch.bfloat16 if torch.cuda.get_device_capability()[0] >= 8 else torch.float16
        m.aggregator = m.aggregator.to(dtype=self.dtype)   # same trick as demo.py
        self.m, self.dev = m, dev
        self.scale, self.kfi, self.max_kf = scale, keyframe_interval, max_keyframes
        self.recent = deque(maxlen=scale)   # (tensor, rgb, tag)
        self.pending = []
        self.n_stream = 0                   # frames processed since last scale block
        self.n_kf = 0
        self.seeded = False

    def _forward(self, batch, per_block):
        t = self.torch
        with t.no_grad(), t.amp.autocast("cuda", dtype=self.dtype):
            t.compiler.cudagraph_mark_step_begin()
            return self.m(batch.unsqueeze(0).to(self.dev), num_frame_for_scale=self.scale,
                          num_frame_per_block=per_block, causal_inference=True)

    def _results(self, out, frames, reseed):
        from lingbot_map.utils.pose_enc import pose_encoding_to_extri_intri
        ext = pose_encoding_to_extri_intri(out["pose_enc"].float(), build_intrinsics=False)[0][0].cpu().numpy()
        res = []
        for i, (_, rgb, tag) in enumerate(frames):
            pts = out["world_points"][0, i, ::STRIDE, ::STRIDE].float().cpu().numpy().reshape(-1, 3)
            conf = out["world_points_conf"][0, i, ::STRIDE, ::STRIDE].float().cpu().numpy().reshape(-1)
            res.append((tag, {"cam": ext[i, :3, 3], "R": ext[i, :3, :3], "xyz": pts, "rgb": rgb[::STRIDE, ::STRIDE].reshape(-1, 3),
                              "conf": conf, "reseed": reseed}))
        return res

    def _scale_block(self, frames, reseed):
        self.m.clean_kv_cache()
        out = self._forward(self.torch.stack([f[0] for f in frames]), per_block=len(frames))
        self.seeded, self.n_stream, self.n_kf = True, 0, 0
        return self._results(out, frames, reseed)

    def push(self, rgb: np.ndarray, tag):
        """rgb: (H, 518, 3) uint8, H a multiple of 14, same H every frame."""
        x = self.torch.from_numpy(rgb).permute(2, 0, 1).float() / 255.0
        f = (x, rgb, tag)
        self.recent.append(f)
        if not self.seeded:
            self.pending.append(f)
            if len(self.pending) < self.scale:
                return []
            frames, self.pending = self.pending, []
            return self._scale_block(frames, reseed=False)
        if self.n_kf >= self.max_kf:
            # ponytail: hard re-seed loses long-range memory; lingbot's windowed mode keeps overlap if drift matters.
            return self._scale_block(list(self.recent), reseed=True)[-1:]
        is_kf = self.n_stream % self.kfi == 0
        self.n_stream += 1
        self.n_kf += is_kf
        if not is_kf:
            self.m._set_skip_append(True)
        out = self._forward(x.unsqueeze(0), per_block=1)
        if not is_kf:
            self.m._set_skip_append(False)
        return self._results(out, [f], reseed=False)

    def restart(self):
        """Forget everything (e.g. phone reconnected)."""
        self.m.clean_kv_cache()
        self.recent.clear()
        self.pending = []
        self.seeded = False


class FakeReconstructor:
    """CPU stand-in for testing the pipeline without a GPU.

    Pretends to be lingbot: emits the synthetic corridor walls (server/relay/session.py)
    seen from the given ARKit pose, but expressed in a secret scaled/rotated/shifted
    frame. If the worker's alignment is right, the hub draws walls exactly on the path.
    """
    S, YAW, T = 0.37, 0.9, np.array([3.0, -1.0, 2.0])   # lingbot = S*R*arkit + T

    def __init__(self, scale=8):
        c, s = np.cos(self.YAW), np.sin(self.YAW)
        self.R = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
        self.scale, self.pending, self.seeded = scale, [], False
        # walls 1 m either side of the 12 x 6 m loop, 0..2.5 m high, 5 cm grid
        rng = np.random.default_rng(1)
        pts = []
        for x0, z0, x1, z1 in [(-1, -1, 13, -1), (1, 1, 11, 1), (13, -1, 13, 7), (11, 1, 11, 5),
                               (-1, 7, 13, 7), (1, 5, 11, 5), (-1, -1, -1, 7), (1, 1, 1, 5)]:
            n = int(np.hypot(x1 - x0, z1 - z0) * 400)
            u = rng.random(n)
            pts.append(np.c_[x0 + (x1 - x0) * u, rng.random(n) * 2.5, z0 + (z1 - z0) * u])
        self.walls = np.concatenate(pts)
        h = self.walls[:, 1:2] / 2.5
        self.cols = (np.c_[120 + 100 * h, 140 + 0 * h, 200 - 100 * h]).astype(np.uint8)

    def to_lingbot(self, p):
        return self.S * p @ self.R.T + self.T

    def push(self, rgb, tag, arkit_T=None):
        T = np.asarray(arkit_T, float).reshape(4, 4, order="F")
        pos, fwd = T[:3, 3], -T[:3, 2]
        rel = self.walls - pos
        d = np.linalg.norm(rel, axis=1)
        vis = (d < 6) & ((rel @ fwd) > d * np.cos(np.radians(35)))
        F = np.diag([1.0, -1.0, -1.0])   # ARKit camera axes -> OpenCV camera axes
        res = {"cam": self.to_lingbot(pos), "R": self.R @ T[:3, :3] @ F, "xyz": self.to_lingbot(self.walls[vis]), "rgb": self.cols[vis],
               "conf": np.full(vis.sum(), 5.0), "reseed": False}
        if not self.seeded:
            self.pending.append((tag, res))
            if len(self.pending) < self.scale:
                return []
            self.seeded = True
            out, self.pending = self.pending, []
            return out
        return [(tag, res)]

    def restart(self):
        self.pending, self.seeded = [], False


if __name__ == "__main__":
    import argparse
    import time
    ap = argparse.ArgumentParser(description="Feed a video through LiveReconstructor and report per-frame output.")
    ap.add_argument("--model_path", required=True)
    ap.add_argument("--video", required=True)
    ap.add_argument("--fps", type=float, default=10)
    ap.add_argument("--use_sdpa", action="store_true")
    ap.add_argument("--save", help="write accumulated points to this .npz")
    a = ap.parse_args()
    import cv2
    from PIL import Image
    cap = cv2.VideoCapture(a.video)
    step = max(1, round((cap.get(cv2.CAP_PROP_FPS) or 30) / a.fps))
    rec = LiveReconstructor(a.model_path, use_sdpa=a.use_sdpa)
    allx, allc, i, total = [], [], 0, 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if i % step == 0:
            im = np.asarray(Image.fromarray(fr[:, :, ::-1]).resize((518, 378), Image.BICUBIC))
            t0 = time.time()
            for tag, r in rec.push(im, i):
                keep = r["conf"] > 1.5
                total += keep.sum()
                allx.append(r["xyz"][keep]); allc.append(r["rgb"][keep])
                print(f"frame {tag}: {keep.sum()} pts, cam {np.round(r['cam'], 3)}, {1000 * (time.time() - t0):.0f} ms, total {total}")
        i += 1
    if a.save:
        np.savez(a.save, xyz=np.concatenate(allx), rgb=np.concatenate(allc))
