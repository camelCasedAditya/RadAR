# RadAR

Live 3D situational awareness for first responders. Phones stream camera + ARKit pose to a relay on the MacBook; a GPU worker on RunPod turns the video into a point cloud with [lingbot-map](https://github.com/Robbyant/lingbot-map) and aligns it to ARKit's metric frame; the hub page shows both responders, their video, and the growing 3D map.

Design: `docs/superpowers/specs/2026-10-17-radar-design.md`. Wire format: `server/relay/protocol.md`.

## Run

**1. Relay + hub (MacBook)**
```bash
pip install -r server/requirements.txt
python server/relay/relay.py
```
Open http://localhost:8000. Phones and Mac must be on the same network (a phone hotspot works).

**2. Tunnel for the GPU worker**
```bash
ngrok http 8000
```

**3. GPU worker (RunPod, 24 GB+ GPU, CUDA 12.8)**
```bash
git clone https://github.com/Robbyant/lingbot-map && cd lingbot-map && pip install -e .   # per its README
# download lingbot-map.pt from Hugging Face, then from this repo's server/gpu:
pip install -r ../server/requirements.txt
python worker.py --relay wss://<ngrok-host> --responder A --model_path /path/lingbot-map.pt
python worker.py --relay wss://<ngrok-host> --responder B --model_path /path/lingbot-map.pt
```
First check lingbot runs at all: `python demo.py --model_path lingbot-map.pt --image_folder example/courthouse`. Add `--use_sdpa` to the worker if FlashInfer isn't installed.

**4. Phones**
Build `RadAR.xcodeproj` to each phone (set your signing team). Enter `ws://<mac-ip>:8000` and a responder ID (`A`, `B`). Both responders stand on the same floor mark facing the same wall and tap Start.

## Develop without phones or a GPU
```bash
cd server/relay
python session.py fake ../sessions/fake_corridor.jsonl     # synthetic two-person walk
python session.py replay ../sessions/fake_corridor.jsonl --loop
cd ../gpu && python worker.py --relay ws://127.0.0.1:8000 --responder A --fake
```
`session.py record out.jsonl` captures a real phone session for replay later.

## Tests
```bash
cd server/relay && python wire.py && python test_relay.py
cd ../gpu && python align.py && python test_pipeline.py
```
