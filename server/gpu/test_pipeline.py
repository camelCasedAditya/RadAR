"""Fake lingbot + aligner over the synthetic corridor loop: aligned points must land on the true walls."""
import sys, pathlib
import numpy as np
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "relay"))
from live_model import FakeReconstructor
from align import Aligner, arkit_Rp
from session import path_point, pose_T

rec, al = FakeReconstructor(), Aligner()
img = np.zeros((378, 518, 3), np.uint8)
errs = []
for i in range(450):                      # 36 m: one full loop, all four corners
    x, z, yaw = path_point(0.08 * i)
    for (f, T), r in rec.push(img, (i, pose_T(x, z, yaw)), pose_T(x, z, yaw)):
        Ra, pa = arkit_Rp(T)
        al.add(r["R"], r["cam"], Ra, pa)
        if al.sim is not None and len(r["xyz"]):
            truth = (r["xyz"] - rec.T) @ rec.R / rec.S
            errs.append(np.abs(al.apply(r["xyz"]) - truth).max())
assert len(errs) > 400 and max(errs) < 1e-3, (len(errs), max(errs))
print(f"pipeline ok: {len(errs)} aligned frames, max error {max(errs):.2e} m")
