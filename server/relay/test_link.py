"""Worker protocol + unprojection + alignment: points from the fake worker must land on the true walls."""
import numpy as np

from fake_worker import WALLS, result_msg
from gpu_link import GpuLink
from session import path_point, pose_T


def wall_error(p):
    """Distance from each point to the nearest wall rectangle."""
    best = np.full(len(p), np.inf)
    for axis, val, lo, hi in WALLS:
        i, j = (0, 2) if axis == "x" else (2, 0)
        d = np.abs(p[:, i] - val) + np.clip(lo - p[:, j], 0, None) + np.clip(p[:, j] - hi, 0, None)
        best = np.minimum(best, d)
    return best


link = GpuLink("unused", None, None)
worst, frames, pts = 0.0, 0, 0
for f in range(450):                                    # one full loop, all corners
    x, z, yaw = path_point(0.08 * f)
    T = pose_T(x, z, yaw)
    link.remember_pose("A", f, T)
    out = link.handle_result(result_msg("radar-A", f, 1, f, T))
    if out:
        rid, seq, xyz, rgb = out
        assert rid == "A" and seq == f and len(xyz) == len(rgb)
        worst = max(worst, wall_error(xyz.astype(float)).max())
        frames, pts = frames + 1, pts + len(xyz)
assert frames > 400 and pts > 400 * 1000, (frames, pts)
assert worst < 0.02, worst                              # float16 depth quantisation is a few mm

# a new segment (worker re-seeded lingbot) must not break alignment
link.remember_pose("A", 9999, pose_T(0, 0, 0))
link.handle_result(result_msg("radar-A", 9999, 2, 0, pose_T(0, 0, 0)))
assert link.aligners["A"].sim is not None and len(link.aligners["A"].buf) == 1
# frames with no matching ARKit pose are skipped, not crashed on
assert link.handle_result(result_msg("radar-B", 5, 1, 5, pose_T(0, 0, 0))) is None
print(f"link ok: {frames} frames, {pts} points, worst wall error {worst * 1000:.1f} mm")
