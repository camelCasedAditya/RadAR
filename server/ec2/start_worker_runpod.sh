#!/usr/bin/env bash
# (Re)start the lingbot worker on a RunPod pod after setup_runpod.sh. Logs to /workspace/worker.log.
# Extra args go to worker.py; use --depth_stride 3 (default 4 is blocky, 2 is too many bytes for the tunnel).
pkill -f "[p]ython /workspace/rad-ar/worker.py" 2>/dev/null; sleep 1
export PATH=/usr/local/cuda/bin:$PATH       # FlashInfer compiles its kernels with nvcc on first use
cd /workspace
nohup python /workspace/rad-ar/worker.py --model_path /workspace/weights/lingbot-map.pt --host 127.0.0.1 "$@" > /workspace/worker.log 2>&1 < /dev/null &
for _ in $(seq 90); do
  if curl -sf localhost:8010/health | grep -q '"gpu"'; then
    curl -s localhost:8010/health; echo
    # Build every model now; a build during live streaming corrupts the live stream (see warm_worker.py).
    PATH=/usr/local/cuda/bin:$PATH python /workspace/warm_worker.py --streams 3 && curl -s localhost:8010/health && echo
    exit 0
  fi
  sleep 2
done
echo "worker did not come up"; tail -20 /workspace/worker.log; exit 1
