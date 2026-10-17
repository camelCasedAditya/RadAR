"""RadAR relay. Runs on the MacBook. See protocol.md.

    python relay.py            # serves http://0.0.0.0:8000, hub at /
"""
import json
import pathlib

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from wire import KIND_JPEG, unpack_bin

HUB_DIR = pathlib.Path(__file__).resolve().parents[2] / "hub"

app = FastAPI()
hubs: set[WebSocket] = set()
gpus: dict[str, WebSocket] = {}
state: dict[str, dict] = {}          # id -> {msg type -> latest msg}
last_jpeg: dict[str, bytes] = {}     # id -> latest jpeg binary message


async def send_hubs(msg):
    for ws in list(hubs):
        try:
            await (ws.send_text(msg) if isinstance(msg, str) else ws.send_bytes(msg))
        except Exception:
            hubs.discard(ws)


async def send_gpu(rid, msg):
    ws = gpus.get(rid)
    if not ws:
        return
    try:
        await (ws.send_text(msg) if isinstance(msg, str) else ws.send_bytes(msg))
    except Exception:
        gpus.pop(rid, None)


@app.websocket("/phone/{rid}")
async def phone(ws: WebSocket, rid: str):
    await ws.accept()
    try:
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect":
                break
            if m.get("text") is not None:
                d = json.loads(m["text"])
                d["id"] = rid
                state.setdefault(rid, {})[d["t"]] = d
                txt = json.dumps(d)
                await send_hubs(txt)
                if d["t"] == "pose":
                    await send_gpu(rid, txt)
            elif m.get("bytes") is not None:
                b = m["bytes"]
                if unpack_bin(b)[0] == KIND_JPEG:
                    last_jpeg[rid] = b
                    await send_hubs(b)
                    await send_gpu(rid, b)
    except WebSocketDisconnect:
        pass


@app.websocket("/gpu/{rid}")
async def gpu(ws: WebSocket, rid: str):
    await ws.accept()
    gpus[rid] = ws
    try:
        while True:
            await send_hubs(await ws.receive_bytes())
    except WebSocketDisconnect:
        pass
    finally:
        if gpus.get(rid) is ws:
            gpus.pop(rid)


@app.websocket("/hub")
async def hub(ws: WebSocket):
    await ws.accept()
    await ws.send_text(json.dumps({"t": "state", "responders": state}))
    for b in last_jpeg.values():
        await ws.send_bytes(b)
    hubs.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        hubs.discard(ws)


if HUB_DIR.exists():
    app.mount("/", StaticFiles(directory=HUB_DIR, html=True), name="hub")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, ws_max_size=8 * 1024 * 1024)
