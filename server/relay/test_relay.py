"""Runs fake_worker on :8767 and the relay on :8765 (with --gpu), then checks
phone -> hub routing, late-hub state, and phone frames -> GPU worker -> aligned points on the hub."""
import asyncio, json, os, subprocess, sys, tempfile, time
import websockets
from session import fake, path_point, pose_T
from wire import KIND_JPEG, KIND_POINTS, pack_bin, unpack_bin, unpack_points

U = "ws://127.0.0.1:8765"
SESSION = os.path.join(tempfile.mkdtemp(), "session.jsonl")


async def recv_until(ws, pred, timeout=10):
    end = time.time() + timeout
    while True:
        m = await asyncio.wait_for(ws.recv(), end - time.time())
        if pred(m):
            return m


async def main():
    hub = await websockets.connect(U + "/hub", max_size=None)
    assert json.loads(await hub.recv())["t"] == "state"
    ph = await websockets.connect(U + "/phone/A")
    await ph.send(json.dumps({"t": "hello", "name": "A"}))
    assert json.loads(await hub.recv())["t"] == "hello"

    # walk 30 frames; worker answers after its 8-frame scale block, alignment needs 0.5 m of travel
    for f in range(30):
        x, z, yaw = path_point(0.08 * f)   # responder A in session.fake: 0.8 m/s at 10 fps
        await ph.send(json.dumps({"t": "pose", "f": f, "ts": f / 10, "T": pose_T(x, z, yaw)}))
        await ph.send(pack_bin(KIND_JPEG, "A", f, b"\xff\xd8fake"))
    m = await recv_until(hub, lambda m: isinstance(m, bytes) and m[0] == KIND_POINTS)
    kind, rid, f, payload = unpack_bin(m)
    xyz, rgb = unpack_points(payload)
    assert rid == "A" and len(xyz) > 1000, (rid, len(xyz))
    assert abs(xyz[:, 1].min()) < 0.05 and abs(xyz[:, 1].max() - 2.5) < 0.05, (xyz[:, 1].min(), xyz[:, 1].max())

    await ph.send(json.dumps({"t": "head", "yaw": 0.1, "pitch": 0}))
    await recv_until(hub, lambda m: isinstance(m, str) and json.loads(m)["t"] == "head")
    hub2 = await websockets.connect(U + "/hub")
    s = json.loads(await hub2.recv())
    assert "pose" in s["responders"]["A"] and "head" in s["responders"]["A"], s
    assert unpack_bin(await hub2.recv())[0] == KIND_JPEG
    print("relay ok: routing, state, and GPU points path")


fake(SESSION, seconds=5)   # poses the fake worker looks up; same path as above
procs = [subprocess.Popen([sys.executable, "fake_worker.py", SESSION, "--port", "8767"]),
         subprocess.Popen([sys.executable, "relay.py", "--port", "8765", "--gpu", "ws://127.0.0.1:8767/stream"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
try:
    time.sleep(2.5)
    asyncio.run(asyncio.wait_for(main(), 20))
finally:
    for p in procs:
        p.terminate()
