"""Spins up relay on :8765 and checks phone -> hub/gpu and gpu -> hub routing."""
import asyncio, json, subprocess, sys, time
import numpy as np, websockets
from wire import KIND_JPEG, KIND_POINTS, pack_bin, pack_points, unpack_bin

U = "ws://127.0.0.1:8765"

async def main():
    hub = await websockets.connect(U + "/hub")
    first = json.loads(await hub.recv())
    assert first["t"] == "state", first
    gpu = await websockets.connect(U + "/gpu/A")
    ph = await websockets.connect(U + "/phone/A")
    await asyncio.sleep(0.1)
    await ph.send(json.dumps({"t": "pose", "f": 1, "ts": 0.0, "T": [1,0,0,0,0,1,0,0,0,0,1,0,1,2,3,1]}))
    await ph.send(pack_bin(KIND_JPEG, "A", 1, b"\xff\xd8jpg"))
    h1 = json.loads(await hub.recv()); h2 = await hub.recv()
    g1 = json.loads(await gpu.recv()); g2 = await gpu.recv()
    assert h1["t"] == "pose" and h1["id"] == "A", h1
    assert g1["t"] == "pose" and unpack_bin(g2)[3] == b"\xff\xd8jpg"
    assert unpack_bin(h2)[:3] == (KIND_JPEG, "A", 1)
    await gpu.send(pack_bin(KIND_POINTS, "A", 1, pack_points(np.zeros((3, 3), np.float32), np.zeros((3, 3), np.uint8))))
    assert unpack_bin(await hub.recv())[0] == KIND_POINTS
    # head message goes to hub only, not gpu
    await ph.send(json.dumps({"t": "head", "yaw": 0.1, "pitch": 0}))
    assert json.loads(await hub.recv())["t"] == "head"
    # late hub gets state + last jpeg
    hub2 = await websockets.connect(U + "/hub")
    s = json.loads(await hub2.recv())
    assert s["responders"]["A"]["pose"]["T"][12] == 1 and "head" in s["responders"]["A"], s
    assert unpack_bin(await hub2.recv())[0] == KIND_JPEG
    print("relay ok")

p = subprocess.Popen([sys.executable, "-c", "import uvicorn, relay; uvicorn.run(relay.app, port=8765, log_level='warning')"])
try:
    time.sleep(1.5)
    asyncio.run(asyncio.wait_for(main(), 10))
finally:
    p.terminate()
