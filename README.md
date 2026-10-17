# RadAR

Live 3D situational awareness for first responders. Phones stream camera + ARKit pose to a relay on the MacBook; the relay sends frames to a GPU worker on RunPod, which turns them into depth with [lingbot-map](https://github.com/Robbyant/lingbot-map); the relay aligns that to ARKit's metric frame; the hub page shows both responders, their video, and the growing 3D map.

Design: `docs/superpowers/specs/2026-10-17-radar-design.md`. Wire format: `server/relay/protocol.md`.

## Run

**1. GPU worker (AWS EC2, us-east-2).** A `g6e.xlarge` (L40S) runs the lingbot-map worker as a service on `127.0.0.1:8010`. After `aws login`:
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
