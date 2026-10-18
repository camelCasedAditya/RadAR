# RadAR

Live 3D situational awareness for first responders. Phones stream camera + ARKit pose to a relay on the MacBook; the relay sends frames to a GPU worker on RunPod, which turns them into depth with [lingbot-map](https://github.com/Robbyant/lingbot-map); the relay aligns that to ARKit's metric frame; the hub page shows both responders, their video, and the growing 3D map.

Design: `docs/superpowers/specs/2026-10-17-radar-design.md`. Wire format: `server/relay/protocol.md`.

## Run

**1. GPU worker.** Either option runs the same lingbot worker on `127.0.0.1:8010` on the GPU box; the relay reaches it through an SSH tunnel.

*RunPod (current).* Measured streaming inference: L40S ~52 ms/frame (two phones at 6 map fps), A40 ~112 ms/frame (one phone). On a PyTorch 2.8 / CUDA 12.8 pod with SSH over exposed TCP (add your Mac's key under RunPod Settings → SSH Public Keys first), from the repo root:
```bash
scp -P <port> server/ec2/worker.py server/ec2/setup_runpod.sh server/ec2/start_worker_runpod.sh root@<ip>:/workspace/
ssh -p <port> root@<ip> 'mkdir -p /workspace/rad-ar && mv /workspace/worker.py /workspace/rad-ar/ && bash /workspace/setup_runpod.sh'
```
After a pod restart: `ssh -p <port> root@<ip> bash /workspace/start_worker_runpod.sh`. The start script also runs `warm_worker.py`, which builds all three lingbot models before any phone connects: the worker builds models on demand, and a build while another phone is streaming corrupts that phone's stream for good (confidence drops to the floor), which is what made only one of two phones map. Tunnel: `ssh -N -L 8010:localhost:8010 -p <port> root@<ip>`. The first frames after a fresh install take a few seconds while FlashInfer compiles its kernels (needs `nvcc` on PATH, which the start script sets).

*AWS EC2 (us-east-2, needs G-instance quota).* A `g6e.xlarge` (L40S) runs the lingbot-map worker as a service on `127.0.0.1:8010`. After `aws login`:
```bash
server/ec2/radar-ec2.sh launch    # once: creates the instance (~$1.86/hr while running)
server/ec2/radar-ec2.sh setup     # once: installs lingbot-map + worker (needs server/ec2/worker.py)
server/ec2/radar-ec2.sh start     # later sessions: start the stopped instance
server/ec2/radar-ec2.sh stop      # always stop when done testing
```
`server/ec2/worker.py` is the teammate's worker (same protocol the relay speaks); it's gitignored, get a copy from them.

**2. Tunnel to the worker (MacBook).** Leave this running; it reconnects by itself after network changes:
```bash
server/ec2/radar-ec2.sh tunnel
```

**3. Relay + hub (MacBook).**
```bash
pip install -r server/requirements.txt
python server/relay/relay.py --gpu ws://localhost:8010/stream
```
Open http://localhost:8000. The status strip says "GPU … fps" once the worker is connected. Phones and Mac must be on the same network (a phone hotspot works).

**4. Phones.** Build `RadAR.xcodeproj` to each phone (set your signing team). Enter `ws://<mac-ip>:8000` and a responder ID (`A`, `B`). Both responders stand on the same floor mark facing the same wall and tap Start. The hub shows 30 fps video right away; the 3D cloud appears after about half a metre of walking and trails you by a few seconds (shown as "map lag").

The relay sends each phone's frames to the worker as stream `radar-<id>`, unprojects the returned depth, and aligns it to ARKit's metric frame (`server/relay/gpu_link.py`, `align.py`). `--conf` sets the depth-confidence cut (default 1.5).

## Develop without phones or a GPU
```bash
cd server/relay
python session.py fake ../sessions/fake_corridor.jsonl          # synthetic two-person walk
python fake_worker.py ../sessions/fake_corridor.jsonl           # terminal 1: CPU stand-in for the GPU worker, port 8010
python relay.py --gpu ws://localhost:8010/stream                # terminal 2
python session.py replay ../sessions/fake_corridor.jsonl --loop  # terminal 3
```
`session.py record out.jsonl` captures a real phone session for replay later.

## Tests
```bash
cd server/relay && python wire.py && python align.py && python test_link.py && python test_queue.py && python test_relay.py
```
