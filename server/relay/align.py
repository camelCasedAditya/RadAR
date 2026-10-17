"""Align lingbot-map's scale-free frame to ARKit's metric frame with Sim(3).

Both systems see the same camera every frame:
  lingbot: R_l (3x3, OpenCV camera axes: x right, y down, z forward), position p_l, arbitrary scale
  ARKit:   R_a (3x3, ARKit camera axes: x right, y up, z backward),  position p_a, metres
Camera axes differ by F = diag(1, -1, -1), so the frame rotation is  R = R_a F R_l^T  for every frame.
Rotation is averaged from orientations (works even when walking a straight line, where
position-only alignment cannot resolve roll about the walking direction). Scale and
translation then come from positions, which needs some travel.

Result maps lingbot -> ARKit:  p_a ≈ s R p_l + t
"""
from collections import deque

import numpy as np

F = np.diag([1.0, -1.0, -1.0])


def project_so3(M):
    U, _, Vt = np.linalg.svd(M)
    D = np.diag([1, 1, np.sign(np.linalg.det(U @ Vt))])
    return U @ D @ Vt


def solve(Rl, pl, Ra, pa):
    """Rl, Ra: (N,3,3); pl, pa: (N,3). Returns (s, R, t)."""
    R = project_so3(np.einsum("nij,jk,nlk->il", Ra, F, Rl))   # mean of R_a F R_l^T
    xl, xa = pl - pl.mean(0), pa - pa.mean(0)
    s = np.sum(xa * (xl @ R.T)) / np.sum(xl ** 2)
    t = pa.mean(0) - s * R @ pl.mean(0)
    return s, R, t


class Aligner:
    # ponytail: plain least squares over a fixed window; add RANSAC if lingbot poses spike.
    def __init__(self, window=30, min_travel=0.5):
        self.buf = deque(maxlen=window)
        self.min_travel = min_travel
        self.sim = None  # last good (s, R, t)

    def reset(self):
        """lingbot's frame changed (KV re-seed): old pairs are invalid. Keep last sim so points keep flowing."""
        self.buf.clear()

    def add(self, Rl, pl, Ra, pa):
        self.buf.append((np.asarray(Rl, float), np.asarray(pl, float), np.asarray(Ra, float), np.asarray(pa, float)))
        if len(self.buf) >= 3:
            Rl_, pl_, Ra_, pa_ = (np.array(x) for x in zip(*self.buf))
            if np.linalg.norm(pa_.max(0) - pa_.min(0)) >= self.min_travel and np.ptp(pl_, 0).max() > 1e-6:
                self.sim = solve(Rl_, pl_, Ra_, pa_)
        return self.sim

    def apply(self, pts):
        s, R, t = self.sim
        return (s * pts @ R.T + t).astype(np.float32)


def arkit_Rp(T16):
    """ARKit column-major 4x4 list -> (R, p)."""
    T = np.asarray(T16, float).reshape(4, 4, order="F")
    return T[:3, :3], T[:3, 3]


if __name__ == "__main__":
    rng = np.random.default_rng(0)
    q, _ = np.linalg.qr(rng.normal(size=(3, 3)))
    Rf = q * np.sign(np.linalg.det(q))          # true lingbot->arkit rotation
    s, t = 2.7, np.array([1.0, -2.0, 0.5])

    def yaw(a):
        c, n = np.cos(a), np.sin(a)
        return np.array([[c, 0, n], [0, 1, 0], [-n, 0, c]])

    a = Aligner()
    # standing still: no alignment
    for _ in range(5):
        assert a.add(np.eye(3), rng.normal(size=3) * 1e-3, np.eye(3), [0, 0, 0.01]) is None
    # walk a STRAIGHT line along x with constant heading: the degenerate case for position-only alignment
    for i in range(30):
        pa = np.array([0.1 * i, 1.4, 0.0])
        Ra = yaw(-np.pi / 2)
        Rl = Rf.T @ Ra @ F                      # what lingbot would report for this camera
        pl = Rf.T @ (pa - t) / s
        a.add(Rl, pl, Ra, pa)
    s2, R2, t2 = a.sim
    assert abs(s2 - s) < 1e-9 and np.allclose(R2, Rf) and np.allclose(t2, t), (s2, R2, t2)
    off = rng.normal(size=(50, 3)) * 5          # points well off the walked line
    assert np.allclose(a.apply((off - t) @ Rf / s), off, atol=1e-4)
    a.reset()
    assert a.sim is not None
    print("align ok")
