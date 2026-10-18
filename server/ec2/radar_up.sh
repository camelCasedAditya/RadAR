#!/usr/bin/env bash
# Runs ON the RunPod pod: brings the whole RadAR backend up, safe to re-run any time.
#   bash /workspace/radar_up.sh [--restart-worker]
# A stopped pod keeps /workspace (lingbot-map, weights, HF cache, our code, saved maps) but loses
# everything pip installed into the container, so step 1 reinstalls only when imports fail.
set -euo pipefail
WS=/workspace
cd $WS

# 1. lingbot + deps (idempotent; ~3 min on a fresh container, seconds otherwise)
if ! python -c "import lingbot_map, flashinfer, fastapi, uvicorn, websockets, PIL" 2>/dev/null; then
  echo "== installing lingbot-map + deps"
  bash $WS/setup_runpod.sh --no-start
fi

# 2. lingbot worker on 127.0.0.1:8010 (with its 3-model warm-up). Skipped when already healthy.
if [ "${1:-}" = "--restart-worker" ] || ! curl -sf --max-time 3 localhost:8010/health >/dev/null; then
  echo "== starting worker"
  bash $WS/start_worker_runpod.sh --depth_stride 3 --reset_frames 20
else
  echo "== worker already healthy: $(curl -s localhost:8010/health)"
fi

# 3. relay + hub on 8888 (Jupyter's port: the only one RunPod proxies on this pod). Always restarted
#    so freshly copied code takes effect.
pkill -f "[j]upyter-lab" 2>/dev/null || true
pkill -f "[r]elay.py --port 8888" 2>/dev/null || true
sleep 1
cd $WS/radar
nohup python server/relay/relay.py --port 8888 --gpu ws://127.0.0.1:8010/stream > $WS/relay.log 2>&1 < /dev/null &
for _ in $(seq 20); do curl -sf --max-time 2 localhost:8888/maps >/dev/null && break; sleep 1; done
echo "== relay: $(curl -s --max-time 2 localhost:8888/maps | head -c 120)"
tail -2 $WS/relay.log
