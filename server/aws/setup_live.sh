#!/usr/bin/env bash
# Runs ONCE on the EC2 live box (Deep Learning Base AMI, Ubuntu 22.04, NVIDIA driver + CUDA present)
# as root via user-data at first boot. Installs the lingbot-map worker, the relay + hub, and Caddy for
# HTTPS; registers systemd services that pull the latest code from S3 at every boot. Safe to re-run.
# user-data prepends:  export RADAR_S3_BUCKET=... RADAR_HOST=<elastic-ip-dashed>.sslip.io RADAR_ENHANCE_INSTANCE=...
set -euo pipefail
: "${RADAR_S3_BUCKET:?}" "${RADAR_HOST:?}"
VENV=/opt/radar-venv
export CUDA_HOME=/usr/local/cuda PATH=/usr/local/cuda/bin:$PATH DEBIAN_FRONTEND=noninteractive HF_HOME=/opt/hf
mkdir -p /opt/radar /opt/weights /opt/hf
cd /opt

apt-get update -qq && apt-get install -y -qq python3.10-venv python3.10-dev build-essential git curl debian-keyring debian-archive-keyring apt-transport-https >/dev/null
# Caddy: automatic Let's Encrypt certificate for the sslip.io name, reverse proxy to the relay
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
fi

[ -d lingbot-map ] || git clone --depth 1 https://github.com/Robbyant/lingbot-map
[ -d $VENV ] || python3.10 -m venv $VENV
$VENV/bin/pip install -q --upgrade pip
$VENV/bin/pip install -q torch==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu128
$VENV/bin/pip install -q -e "lingbot-map[vis]"
$VENV/bin/pip install -q flashinfer-python huggingface_hub fastapi "uvicorn[standard]" "matplotlib<3.9" websockets pillow boto3 numpy
[ -f /opt/weights/lingbot-map.pt ] || $VENV/bin/hf download robbyant/lingbot-map lingbot-map.pt --local-dir /opt/weights
$VENV/bin/python -c "import torch, lingbot_map, flashinfer; print('setup ok', torch.__version__, torch.cuda.get_device_name(0))"

cat > /etc/radar-live.env <<EOF
RADAR_S3_BUCKET=$RADAR_S3_BUCKET
RADAR_HOST=$RADAR_HOST
RADAR_ENHANCE_INSTANCE=${RADAR_ENHANCE_INSTANCE:-}
AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-us-east-2}
HF_HOME=/opt/hf
CUDA_HOME=/usr/local/cuda
PATH=$VENV/bin:/usr/local/cuda/bin:/usr/local/bin:/usr/bin:/bin
EOF

# every boot: latest code from S3 (server/, hub/, the teammate's worker.py), uploaded by live.sh deploy
cat > /usr/local/bin/radar-pull-code <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
. /etc/radar-live.env
aws s3 cp "s3://$RADAR_S3_BUCKET/code/live.tgz" /tmp/live.tgz --quiet && tar xzf /tmp/live.tgz -C /opt/radar
EOF
# worker, then warm its 3 models before any phone can connect (a build mid-stream corrupts the stream)
cat > /usr/local/bin/radar-warm <<'EOF'
#!/usr/bin/env bash
. /etc/radar-live.env
for _ in $(seq 120); do curl -sf localhost:8010/health | grep -q '"gpu"' && break; sleep 2; done
python /opt/radar/server/ec2/warm_worker.py --streams 3 --images /opt/lingbot-map/example/courthouse
EOF
chmod +x /usr/local/bin/radar-pull-code /usr/local/bin/radar-warm

cat > /etc/systemd/system/radar-worker.service <<EOF
[Unit]
Description=RadAR lingbot-map worker (127.0.0.1:8010)
After=network-online.target
Wants=network-online.target
[Service]
EnvironmentFile=/etc/radar-live.env
ExecStartPre=/usr/local/bin/radar-pull-code
ExecStart=$VENV/bin/python /opt/radar/server/ec2/worker.py --model_path /opt/weights/lingbot-map.pt --host 127.0.0.1 --depth_stride 3 --reset_frames 20
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/radar-relay.service <<EOF
[Unit]
Description=RadAR relay + hub (127.0.0.1:8888, behind Caddy)
After=radar-worker.service
Requires=radar-worker.service
[Service]
EnvironmentFile=/etc/radar-live.env
WorkingDirectory=/opt/radar
ExecStartPre=/usr/local/bin/radar-warm
TimeoutStartSec=900
ExecStart=$VENV/bin/python /opt/radar/server/relay/relay.py --port 8888 --gpu ws://127.0.0.1:8010/stream
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/caddy/Caddyfile <<EOF
$RADAR_HOST {
  reverse_proxy 127.0.0.1:8888
}
EOF
systemctl daemon-reload
systemctl restart caddy                    # first: HTTPS answers (503) while the relay warms up for minutes
systemctl enable --now radar-worker radar-relay
echo "live setup done: https://$RADAR_HOST"
