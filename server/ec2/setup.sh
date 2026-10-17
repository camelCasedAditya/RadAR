#!/usr/bin/env bash
# Runs ON the EC2 instance (Deep Learning Base OSS Nvidia Driver AMI, Ubuntu 22.04).
# Installs lingbot-map + the worker and registers it as a service that starts on every boot.
# Idempotent: safe to re-run. Called by radar-ec2.sh setup.
set -euo pipefail

HOME_DIR=/home/ubuntu
VENV=$HOME_DIR/venv
cd $HOME_DIR

if [ ! -d lingbot-map ]; then
  git clone --depth 1 https://github.com/Robbyant/lingbot-map
fi
if [ ! -d "$VENV" ]; then
  sudo apt-get update -qq && sudo apt-get install -y -qq python3.10-venv
  python3.10 -m venv "$VENV"
fi
. "$VENV/bin/activate"
pip install -q --upgrade pip
pip install -q torch==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu128
pip install -q -e "lingbot-map[vis]"
pip install -q flashinfer-python huggingface_hub fastapi "uvicorn[standard]" "matplotlib<3.9"
mkdir -p weights
[ -f weights/lingbot-map.pt ] || hf download robbyant/lingbot-map lingbot-map.pt --local-dir weights

python -c "import torch, lingbot_map, flashinfer; print('setup ok', torch.__version__, torch.cuda.get_device_name(0))"

sudo tee /etc/systemd/system/radar-worker.service >/dev/null <<EOF
[Unit]
Description=RadAR lingbot-map worker (ws://localhost:8010/stream)
After=network-online.target

[Service]
User=ubuntu
WorkingDirectory=$HOME_DIR/rad-ar
Environment=PATH=$VENV/bin:/usr/bin:/bin
ExecStart=$VENV/bin/python worker.py --model_path $HOME_DIR/weights/lingbot-map.pt --host 127.0.0.1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now radar-worker
sudo systemctl restart radar-worker
echo "worker service started; health: curl localhost:8010/health"
