"""Worker protocol + depth placement: points from the fake worker must land on the true walls,
even when lingbot's own camera pose wobbles, and repeated views must not add duplicate points."""
import numpy as np

from fake_worker import FX, H, W, WALLS, result_msg
from gpu_link import VOXEL, GpuLink
from session import path_point, pose_T

K = [FX, FX, W / 2, H / 2]   # the phone's lens for the 518-wide map image (what ARKit reports)


def wall_error(p):
    """Distance from each point to the nearest wall rectangle."""
    best = np.full(len(p), np.inf)
    for axis, val, lo, hi in WALLS:
        i, j = (0, 2) if axis == "x" else (2, 0)
        d = np.abs(p[:, i] - val) + np.clip(lo - p[:, j], 0, None) + np.clip(p[:, j] - hi, 0, None)
        best = np.minimum(best, d)
    return best


def walk(noise):
    link = GpuLink("unused", None, None)
    worst, frames, pts, errs = 0.0, 0, 0, []
    for f in range(450):                                  # one full loop, all corners
        x, z, yaw = path_point(0.08 * f)
        T = pose_T(x, z, yaw)
        link.remember_pose("A", f, T, K)
        out = link.handle_result(result_msg("radar-A", f, 1, f, T, noise=noise))
        if out:
            rid, seq, xyz, rgb = out
            assert rid == "A" and seq == f and len(xyz) == len(rgb)
            e = wall_error(xyz.astype(float))
            errs.append(e)
            worst = max(worst, e.max())
            frames, pts = frames + 1, pts + len(xyz)
    return link, worst, frames, pts, np.percentile(np.concatenate(errs), 95)


# 1. exact lingbot poses: points sit on the walls (float16 depth quantisation is a few mm)
link, worst, frames, pts, _ = walk(noise=0.0)
assert frames > 300 and worst < 0.02, (frames, worst)
# voxel dedup: never more than one point per voxel
keys = link.seen
assert pts == len(keys), (pts, len(keys))

# 2. lingbot pose wobbles by ~3 deg / ~3 cm per frame. Old lingbot-pose placement: p95 15 cm, worst 116 cm.
#    ARKit placement only takes lingbot's (smoothed) scale, so walls stay within a few cm.
_, worst_noisy, frames_n, _, p95_noisy = walk(noise=3.0)
assert frames_n > 300 and p95_noisy < 0.07 and worst_noisy < 0.10, (p95_noisy, worst_noisy)

# 3. the same view again adds (almost) nothing
x, z, yaw = path_point(0.08 * 100)
T = pose_T(x, z, yaw)
link.remember_pose("A", 5000, T, K)
again = link.handle_result(result_msg("radar-A", 5000, 1, 5000, T))
assert again is None or len(again[2]) < 50, len(again[2])

# 4. a new segment (worker re-seeded lingbot) keeps working; frames with no ARKit pose are skipped
link.remember_pose("A", 9999, pose_T(0, 0, 0), K)
link.handle_result(result_msg("radar-A", 9999, 2, 0, pose_T(0, 0, 0)))
assert link.aligners["A"].sim is None and len(link.aligners["A"].buf) == 1 and "A" not in link.scale  # re-learning scale
assert link.handle_result(result_msg("radar-B", 5, 1, 5, pose_T(0, 0, 0))) is None
print(f"link ok: {frames} frames, {pts} points (1 per {VOXEL * 100:.0f} cm voxel), "
      f"worst wall error {worst * 1000:.1f} mm exact; with 3 deg lingbot wobble p95 {p95_noisy * 100:.1f} cm, worst {worst_noisy * 100:.1f} cm")
