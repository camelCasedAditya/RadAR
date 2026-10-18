"""Binary framing shared by relay, GPU worker and tools. See protocol.md."""
import struct

import numpy as np

KIND_JPEG = 1
KIND_POINTS = 2
KIND_H264 = 3     # payload: flags u8 (bit 0 = keyframe), then Annex-B H.264 access unit
KIND_DEPTH = 4    # payload: w u16, h u16, depth f32[w*h] metres, conf u8[w*h] (ARKit 0/1/2), then the map JPEG


def pack_bin(kind: int, rid: str, f: int, payload: bytes) -> bytes:
    idb = rid.encode()
    return bytes([kind, len(idb)]) + idb + struct.pack("<I", f) + payload


def unpack_bin(msg: bytes):
    kind, idlen = msg[0], msg[1]
    rid = msg[2:2 + idlen].decode()
    (f,) = struct.unpack_from("<I", msg, 2 + idlen)
    return kind, rid, f, msg[6 + idlen:]


def pack_points(xyz: np.ndarray, rgb: np.ndarray) -> bytes:
    n = len(xyz)
    return (struct.pack("<I", n)
            + np.ascontiguousarray(xyz, dtype="<f4").tobytes()
            + np.ascontiguousarray(rgb, dtype=np.uint8).tobytes())


def unpack_points(payload: bytes):
    (n,) = struct.unpack_from("<I", payload, 0)
    xyz = np.frombuffer(payload, dtype="<f4", count=n * 3, offset=4).reshape(n, 3)
    rgb = np.frombuffer(payload, dtype=np.uint8, count=n * 3, offset=4 + n * 12).reshape(n, 3)
    return xyz, rgb


if __name__ == "__main__":
    xyz = np.random.rand(5, 3).astype(np.float32)
    rgb = (np.random.rand(5, 3) * 255).astype(np.uint8)
    m = pack_bin(KIND_POINTS, "A", 42, pack_points(xyz, rgb))
    kind, rid, f, payload = unpack_bin(m)
    x2, c2 = unpack_points(payload)
    assert (kind, rid, f) == (KIND_POINTS, "A", 42)
    assert np.array_equal(xyz, x2) and np.array_equal(rgb, c2)
    kind, rid, f, payload = unpack_bin(pack_bin(KIND_JPEG, "B", 7, b"\xff\xd8"))
    assert (kind, rid, f, payload) == (KIND_JPEG, "B", 7, b"\xff\xd8")
    print("wire ok")
