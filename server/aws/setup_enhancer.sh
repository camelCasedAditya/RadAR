#!/usr/bin/env bash
# Runs ONCE on the EC2 enhancer (Deep Learning Base AMI, Ubuntu 22.04, NVIDIA driver + CUDA present)
# as root, via user-data at first boot. Installs the enhance venv and a systemd service that pulls
# the latest code from S3 and runs the job loop at every boot. Safe to re-run.
#   RADAR_S3_BUCKET=radar-dubhacks-... bash setup_enhancer.sh
set -euo pipefail
: "${RADAR_S3_BUCKET:?set RADAR_S3_BUCKET}"
VENV=/opt/radar-venv
export CUDA_HOME=/usr/local/cuda PATH=/usr/local/cuda/bin:$PATH DEBIAN_FRONTEND=noninteractive

apt-get update -qq && apt-get install -y -qq python3-venv python3-dev build-essential ffmpeg >/dev/null
[ -d $VENV ] || python3 -m venv $VENV
$VENV/bin/pip install -q --upgrade pip
$VENV/bin/pip install -q torch torchvision --index-url https://download.pytorch.org/whl/cu128
$VENV/bin/pip install -q av open3d transformers boto3 numpy pillow scipy ninja
# gsplat has no wheel for this torch: build its CUDA kernels once now (several minutes); they persist
# on this disk across stop/start.
TORCH_CUDA_ARCH_LIST="8.9" $VENV/bin/pip install -q --no-build-isolation gsplat

mkdir -p /opt/radar /opt/hf
cat > /etc/radar-enhance.env <<EOF
RADAR_S3_BUCKET=$RADAR_S3_BUCKET
AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-us-east-2}
HF_HOME=/opt/hf
CUDA_HOME=/usr/local/cuda
EOF
cat > /usr/local/bin/radar-enhance-run <<'EOF'
#!/usr/bin/env bash
# every boot: latest code from S3 (uploaded by server/aws/enhancer.sh deploy), then the job loop
set -euo pipefail
. /etc/radar-enhance.env; export $(cut -d= -f1 /etc/radar-enhance.env)
export PATH=/opt/radar-venv/bin:/usr/local/cuda/bin:$PATH     # ninja + nvcc: gsplat builds its kernels on first use
aws s3 cp "s3://$RADAR_S3_BUCKET/code/enhance.tgz" /tmp/enhance.tgz --quiet && tar xzf /tmp/enhance.tgz -C /opt/radar
exec /opt/radar-venv/bin/python /opt/radar/server/enhance/enhance_worker.py
EOF
chmod +x /usr/local/bin/radar-enhance-run
cat > /etc/systemd/system/radar-enhance.service <<EOF
[Unit]
Description=RadAR enhance job loop
After=network-online.target
Wants=network-online.target
[Service]
ExecStart=/usr/local/bin/radar-enhance-run
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now radar-enhance.service
echo "enhancer setup done: $(systemctl is-active radar-enhance.service)"
