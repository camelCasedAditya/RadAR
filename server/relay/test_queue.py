"""GPU link queue against a slow fake worker (70 ms/frame, drops when behind like the real one):
a 20-frame burst plus two phones at 5 map-fps must all be processed, none dropped, in order, with lag reported."""
import asyncio, json, os, subprocess, sys, tempfile, time

from gpu_link import GpuLink
from session import fake

SESSION = os.path.join(tempfile.mkdtemp(), "session.jsonl")
fake(SESSION, seconds=10)
poses = {}
for line in open(SESSION):
    r = json.loads(line)
    if "text" in r and json.loads(r["text"])["t"] == "pose":
        d = json.loads(r["text"]); poses[(d["id"], d["f"])] = d["T"]


async def main():
    got, status = {"A": [], "B": []}, {}

    async def on_points(rid, f, xyz, rgb, lag, queued):
        got[rid].append((f, lag))

    async def on_status(d):
        status.update(d)

    link = GpuLink("ws://127.0.0.1:8769/stream", on_points, on_status, queue_cap=60)   # big cap: test no-drop
    for (rid, f), T in poses.items():
        link.remember_pose(rid, f, T)
    task = asyncio.create_task(link.run())
    while link.ws is None:
        await asyncio.sleep(0.05)

    sent = {"A": [], "B": []}
    for f in range(0, 40, 2):                    # burst: 20 frames at once for A
        link.enqueue("A", f, b"jpg"); sent["A"].append(f)
    for f in range(40, 100, 2):                  # then both phones at 5 map-fps for 6 s
        link.enqueue("A", f, b"jpg"); sent["A"].append(f)
        link.enqueue("B", f, b"jpg"); sent["B"].append(f)
        await asyncio.sleep(0.2)
    end = time.time() + 15
    while time.time() < end and status.get("processed", 0) < len(sent["A"]) + len(sent["B"]):
        await asyncio.sleep(0.2)
    await asyncio.sleep(0.6)
    task.cancel()

    total = len(sent["A"]) + len(sent["B"])
    assert status.get("dropped") == 0, status
    assert status.get("processed") == total, (status, total)
    for rid in "AB":
        seqs = [f for f, _ in got[rid]]
        assert seqs == sorted(seqs) and len(seqs) > 0.8 * len(sent[rid]), (rid, seqs)  # first ~8 wait for alignment travel
    lags = [l for r in got.values() for _, l in r]
    # default cap bounds lag: a 40-frame burst keeps only the newest 15 queued
    capped = GpuLink("unused", None, None)
    for f in range(40):
        capped.enqueue("A", f, b"jpg")
    assert [f for f, _, _ in capped.queue["A"]] == list(range(25, 40)), list(capped.queue["A"])
    print(f"queue ok: {total} frames, 0 dropped by worker, in order, cap keeps newest 15; map lag median {sorted(lags)[len(lags)//2]:.2f}s max {max(lags):.2f}s")


p = subprocess.Popen([sys.executable, "fake_worker.py", SESSION, "--port", "8769", "--delay", "0.07"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    time.sleep(2)
    asyncio.run(asyncio.wait_for(main(), 40))
finally:
    p.terminate()
