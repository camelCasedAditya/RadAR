# RadAR

Live 3D situational awareness for first responders. Phones stream camera + ARKit pose over HTTPS to a relay on an AWS EC2 GPU box; the relay sends frames to the lingbot-map worker on the same box, which turns them into depth with [lingbot-map](https://github.com/Robbyant/lingbot-map); the relay aligns that to ARKit's metric frame; the hub page shows both responders, their video, and the growing 3D map.

Design: `docs/superpowers/specs/2026-10-17-radar-design.md`. Wire format: `server/relay/protocol.md`.

## Run (AWS EC2, current)

Everything lives in AWS us-east-2: a `radar-live` g6e.xlarge (L40S) runs the lingbot worker, the relay and the hub behind Caddy with an automatic Let's Encrypt certificate on its Elastic IP (`https://18-227-138-16.sslip.io`); a separate `radar-enhance` g6e.xlarge runs enhance jobs on demand (see **Enhanced maps**); session files live in S3. The boxes authenticate to S3 and each other through instance roles, so no access keys are stored anywhere. Admin access is through AWS Systems Manager (no SSH port). After `aws login`:
```bash
server/aws/live.sh status    # state, hub + phone URLs, relay health
server/aws/live.sh up        # start the stopped box (hub is up ~3 min later: boot, worker, 3-model warm-up)
server/aws/live.sh down      # stop it when done (disk, weights and venv are kept; ~$1.86/hr while running)
server/aws/live.sh deploy    # upload server/ + hub/ + worker.py to S3 and restart the services (seconds)
server/aws/live.sh logs      # worker, relay and caddy logs
```
Hub: `https://18-227-138-16.sslip.io`. Phones: scan the hub's Connect QR, or enter `wss://18-227-138-16.sslip.io` and a responder ID. The box was created by the AWS MCP from `server/aws/setup_live.sh` (first-boot user-data: venv with torch 2.8 cu128, lingbot-map, FlashInfer, the weights, Caddy, two systemd services that pull the latest code from `s3://<bucket>/code/live.tgz` at every boot). `server/ec2/worker.py` is the teammate's worker; it's gitignored, get a copy from them, `deploy` ships it. Ids and the hostname are in `server/aws/live.env`.

### Previous hosting (kept for reference)

**1. GPU worker.** Either option runs the same lingbot worker on `127.0.0.1:8010` on the GPU box; the relay reaches it through an SSH tunnel.

*RunPod.* Measured streaming inference: L40S ~52 ms/frame (two phones at 6 map fps), A40 ~112 ms/frame (one phone). On a PyTorch 2.8 / CUDA 12.8 pod with SSH over exposed TCP (add your Mac's key under RunPod Settings → SSH Public Keys first), from the repo root:
```bash
scp -P <port> server/ec2/worker.py server/ec2/setup_runpod.sh server/ec2/start_worker_runpod.sh root@<ip>:/workspace/
ssh -p <port> root@<ip> 'mkdir -p /workspace/rad-ar && mv /workspace/worker.py /workspace/rad-ar/ && bash /workspace/setup_runpod.sh'
```
After a pod restart: `ssh -p <port> root@<ip> bash /workspace/start_worker_runpod.sh --depth_stride 3` (stride 3 = 173x131 depth per frame, ~160 kB; stride 2 is 4x the bytes and too much for a hotspot tunnel). The start script also runs `warm_worker.py`, which builds all three lingbot models before any phone connects: the worker builds models on demand, and a build while another phone is streaming corrupts that phone's stream for good (confidence drops to the floor), which is what made only one of two phones map. Tunnel: `ssh -N -L 8010:localhost:8010 -p <port> root@<ip>`. The first frames after a fresh install take a few seconds while FlashInfer compiles its kernels (needs `nvcc` on PATH, which the start script sets).

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

**Pause and resume the pod (one command).** Stopping the pod in RunPod keeps `/workspace` (lingbot-map, weights, our code, saved maps) and the proxy URL, but wipes the pip installs and all running processes. After Start, read the new SSH ip:port from RunPod → Connect and run:
```bash
POD_IP=<ip> POD_PORT=<port> server/ec2/pod.sh up
```
That copies the current relay, hub and worker scripts, reinstalls lingbot only if its imports fail (~3 min on a fresh container), starts the worker with its 3-model warm-up if it is not already healthy, restarts the relay on 8888, and prints the hub and phone URLs. Run it again any time to deploy code changes (seconds). `pod.sh status` and `pod.sh logs` show health; `pod.sh up --restart-worker` forces a worker restart. Use Stop, never Terminate: Terminate deletes `/workspace`. A stopped pod can fail to find a free L40S on Start; the pod id and URL stay the same once it does.

**Alternative: everything on the pod, no hotspot.** The relay and hub can run on the pod itself next to the worker, reachable through RunPod's HTTP proxy from any network (phones on cellular work). RunPod exposes only Jupyter's port 8888 on this pod, so Jupyter is stopped and the relay takes its port:
```bash
ssh -p <port> root@<ip> 'mkdir -p /workspace/radar/server/relay /workspace/radar/hub'
scp -P <port> server/relay/*.py root@<ip>:/workspace/radar/server/relay/ && scp -P <port> hub/* root@<ip>:/workspace/radar/hub/
ssh -p <port> root@<ip> 'pkill -f "[j]upyter-lab"; cd /workspace/radar && nohup python server/relay/relay.py --port 8888 --gpu ws://127.0.0.1:8010/stream > /workspace/relay.log 2>&1 < /dev/null &'
```
Hub: `https://<pod-id>-8888.proxy.runpod.net`. Phones: `wss://<pod-id>-8888.proxy.runpod.net`. No tunnel and no Mac relay needed. Costs the round trip to the pod in map lag, and each phone uploads ~3 Mbit/s video plus ~8 Mbit/s LiDAR depth, so two phones on cellular may need the depth compressed. The pod runs a copy of the files, not a git checkout: re-copy and restart (`pkill -f "[r]elay.py --port 8888"`) after edits.

**4. Phones.** Build `RadAR.xcodeproj` to each phone (set your signing team). Enter `ws://<mac-ip>:8000` and a responder ID (`A`, `B`). Both responders stand on the same floor mark facing the same wall and tap Start. The hub shows 30 fps video right away; the 3D cloud appears after about half a metre of walking and trails you by a few seconds (shown as "map lag").

**LiDAR phones** (iPhone Pro) skip the GPU: ARKit's metric depth goes to the relay, which upsamples it to the map image and sends the hub a textured *patch* (the photo draped over its depth) whenever the camera has moved 25 cm or turned 15°. The hub's "Surface" button switches between the photo patches and their vertices as dots. Phones without LiDAR still map through lingbot as points.

**Recording maps.** Nothing is saved until someone presses **● Record new map** on the hub and names it. From then until **■ Stop recording**, everything the hub receives (poses, 30 fps video, map JPEGs, lingbot points, LiDAR photo patches) is appended to `server/maps/<name>.radar`, roughly 25 MB per phone-minute of video plus the map; every open hub shows a red recording indicator with the name and a timer, and the view is cleared when a recording starts. Stop closes the file and sends it to S3; a recording that mapped nothing is discarded. Each responder's movement path (one point per 5 cm walked, drawn on the floor in their colour) is kept for the whole session, live and in the file. **Past maps** lists saved sessions with duration and size; click a name to load its map (points, patches and the walked paths) into the view at once, ▶ replays the whole session on its timeline with video and markers, ↓ downloads the file, ✕ deletes it everywhere. `server/relay/protocol.md` has the format.

**S3 copy.** With `RADAR_S3_BUCKET` set (plus AWS credentials and `AWS_DEFAULT_REGION`), the relay streams every recording to `s3://<bucket>/sessions/<name>` in 8 MB parts as it records, finishes it with a `<name>.json` sidecar when the session ends, and uploads any local file that never made it up at the next start. The Maps panel lists local and S3 sessions together (☁), and Load/Replay of an S3-only session fetches straight from S3 through a one-hour presigned link, so sessions outlive the pod. Sessions that mapped nothing are not uploaded. The bucket is `radar-dubhacks-784917519323` in us-east-2, private, encrypted, with a CORS rule for GET and a rule that aborts stale multipart uploads after 2 days. The pod uses the IAM user `radar-pod` (access limited to that bucket) via `/workspace/radar-s3.env`, which `radar_up.sh` sources; recreate that file with `aws iam create-access-key --user-name radar-pod` if the pod is ever rebuilt. On the Mac, `eval "$(aws configure export-credentials --format env)"` then `RADAR_S3_BUCKET=... AWS_DEFAULT_REGION=us-east-2 python server/relay/relay.py ...` does the same.

**Enhanced maps.** A saved map's stored video is 1920×1440 at 30 fps with a pose per frame, far more detail than the live 518-wide patch photos. **✦ Enhance** on a past map queues an offline job (`server/enhance/enhance.py`) that keeps the map exactly as it is and re-textures every LiDAR patch: for each patch it picks the sharpest video frame within 5 frames of the patch (least motion blur), downsizes it to 1280 wide, and stores it with that frame's pose in `<name>.hd` next to the session in S3. The row shows queued/running and then **✦ enhanced**; loading that map streams the sharp photos in after the map and adds an **Original / Enhanced** toggle (the hub projects each patch's vertices into the chosen frame's camera). The job is CPU-only; it runs on the EC2 enhancer box (`server/aws/`) that the relay starts on demand via `RADAR_ENHANCE_INSTANCE` and that stops itself after three idle minutes; `server/aws/enhancer.sh deploy|up|down|status|logs` manages it from the Mac. Without S3 the relay runs the same job locally. Design: `docs/superpowers/specs/2026-10-17-enhanced-maps-design.md`.

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
cd server/enhance && python test_enhance.py     # needs: pip install av pillow
```
