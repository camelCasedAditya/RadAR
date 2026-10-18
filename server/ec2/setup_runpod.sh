#!/usr/bin/env bash
# Runs ON a RunPod pod (PyTorch 2.8 / CUDA 12.8 template). Installs lingbot-map + weights into /workspace
# and starts the worker on 127.0.0.1:8010. Idempotent; re-run after a pod restart to start the worker again.
set -euo pipefail
WS=/workspace
export PIP_BREAK_SYSTEM_PACKAGES=1 HF_HOME=$WS/.cache/huggingface
cd $WS
[ -d lingbot-map ] || git clone --depth 1 https://github.com/Robbyant/lingbot-map
pip install -q -e "lingbot-map[vis]"
pip install -q flashinfer-python huggingface_hub fastapi "uvicorn[standard]" "matplotlib<3.9"
mkdir -p weights
[ -f weights/lingbot-map.pt ] || hf download robbyant/lingbot-map lingbot-map.pt --local-dir weights
python -c "import torch, lingbot_map, flashinfer; print('setup ok', torch.__version__, torch.cuda.get_device_name(0))"
[ "${1:-}" = --no-start ] || exec bash "$(dirname "$0")/start_worker_runpod.sh"
