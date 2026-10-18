"""Runs fake_worker on :8767 and the relay on :8765 (with --gpu), then checks
phone -> hub routing, late-hub state, and phone frames -> GPU worker -> aligned points on the hub."""
import asyncio, json, os, subprocess, sys, tempfile, time
import websockets
from session import fake, path_point, pose_T
from wire import KIND_AUDIO, KIND_JPEG, KIND_POINTS, pack_bin, unpack_bin, unpack_points

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
    assert json.loads(await hub.recv()) == {"t": "recording", "id": "", "name": None, "since": None}   # not recording yet
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
    assert json.loads(await hub2.recv())["t"] == "recording"
    assert unpack_bin(await hub2.recv())[0] == KIND_JPEG

    # Voice call: phone audio reaches the other phone and the hubs, never the talker; hub audio reaches
    # every phone and the other hub, not itself. hr / talk ride the generic JSON path.
    pb = await websockets.connect(U + "/phone/B")
    await pb.send(json.dumps({"t": "hello", "name": "B"}))
    await asyncio.sleep(0.2)
    pcm = bytes(1280)
    await ph.send(json.dumps({"t": "hr", "bpm": 92}))
    assert json.loads(await recv_until(hub, lambda m: isinstance(m, str) and json.loads(m)["t"] == "hr"))["bpm"] == 92
    await ph.send(pack_bin(KIND_AUDIO, "A", 1, pcm))
    assert unpack_bin(await asyncio.wait_for(pb.recv(), 5))[:2] == (KIND_AUDIO, "A")
    await recv_until(hub, lambda m: isinstance(m, bytes) and m[0] == KIND_AUDIO)
    await hub.send(json.dumps({"t": "talk", "on": True}))
    await hub.send(pack_bin(KIND_AUDIO, "hub", 1, pcm))
    for p in (ph, pb):
        assert unpack_bin(await asyncio.wait_for(p.recv(), 5))[:2] == (KIND_AUDIO, "hub")
    t = await recv_until(hub2, lambda m: isinstance(m, str) and json.loads(m)["t"] == "talk")
    assert json.loads(t) == {"t": "talk", "id": "hub", "on": True}, t
    await recv_until(hub2, lambda m: isinstance(m, bytes) and m[0] == KIND_AUDIO)
    try:                                       # the talker never hears itself
        m = await asyncio.wait_for(ph.recv(), 0.5)
        assert False, ("phone A got", m[:8])
    except asyncio.TimeoutError:
        pass
    # peer positions: a pose from A reaches B as {"t": "peer", "id": "A", "p": [x, y, z]} (the relay sends 5 Hz)
    await asyncio.sleep(0.25)
    await ph.send(json.dumps({"t": "pose", "f": 99, "ts": 9.9, "T": pose_T(1.0, 2.0, 0)}))
    peer = json.loads(await recv_until(pb, lambda m: isinstance(m, str) and json.loads(m)["t"] == "peer"))
    assert peer["id"] == "A" and peer["p"] == [1.0, 1.4, 2.0], peer
    await pb.close()

    # Enhance (local mode, no S3): unknown map -> 409; a recorded map is queued, the background job
    # runs enhance.py, and this points-only session ends "failed: needs a LiDAR scan" via the sidecar.
    import urllib.request, urllib.error, urllib.parse
    H = "http://127.0.0.1:8765"
    def post(path):
        path = urllib.parse.quote(path)
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(H + path, method="POST", data=b"{}", headers={"Content-Type": "application/json"})))
        except urllib.error.HTTPError as e:
            return {"status": e.code, **json.load(e)}
    assert post("/maps/nope.radar/enhance")["status"] == 409
    post("/maps/start")
    for f in range(30, 45):
        x, z, yaw = path_point(0.08 * f)
        await ph.send(json.dumps({"t": "pose", "f": f, "ts": f / 10, "T": pose_T(x, z, yaw)}))
        await ph.send(pack_bin(KIND_JPEG, "A", f, b"\xff\xd8fake"))
    await recv_until(hub, lambda m: isinstance(m, bytes) and m[0] == KIND_POINTS)
    name = post("/maps/stop")["saved"]["name"]
    r = post(f"/maps/{name}/enhance")
    assert r.get("queued") == name and r["enhancer"] == "local", r
    async def wait_enhanced():
        for _ in range(150):
            m = next(m for m in json.load(urllib.request.urlopen(H + "/maps"))["maps"] if m["name"] == name)
            if m.get("enhanced", {}).get("status") in ("done", "failed"):
                return m
            await asyncio.sleep(0.2)
        raise AssertionError("enhance job never finished")
    m = await wait_enhanced()
    assert m["enhanced"]["status"] == "failed" and "LiDAR" in m["enhanced"]["error"], m
    assert post(f"/maps/{name}/enhance").get("queued") == name           # failed -> may retry
    await wait_enhanced()
    urllib.request.urlopen(urllib.request.Request(H + urllib.parse.quote(f"/maps/{name}"), method="DELETE"))
    print("relay ok: routing, state, GPU points path, voice call, enhance queue")


fake(SESSION, seconds=5)   # poses the fake worker looks up; same path as above
procs = [subprocess.Popen([sys.executable, "fake_worker.py", SESSION, "--port", "8767"]),
         subprocess.Popen([sys.executable, "relay.py", "--port", "8765", "--gpu", "ws://127.0.0.1:8767/stream"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
try:
    time.sleep(2.5)
    asyncio.run(asyncio.wait_for(main(), 60))
finally:
    for p in procs:
        p.terminate()
