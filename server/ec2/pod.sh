#!/usr/bin/env bash
# One command to (re)host RadAR on the RunPod pod from the Mac:
#   server/ec2/pod.sh up        copy latest code, (re)install if needed, start worker + relay + hub
#   server/ec2/pod.sh status    worker health, relay maps, URLs
#   server/ec2/pod.sh logs      tail worker + relay logs
#   server/ec2/pod.sh ssh       shell on the pod
# After a pod Stop/Start the SSH ip:port usually changes: read it from RunPod -> Connect and pass it
# as POD_IP / POD_PORT (the proxy URL only depends on POD_ID, which never changes).
#   POD_IP=1.2.3.4 POD_PORT=12345 server/ec2/pod.sh up
set -euo pipefail
POD_ID=${POD_ID:-eq56y1wn325nmv}
POD_IP=${POD_IP:-202.181.159.226}
POD_PORT=${POD_PORT:-14452}
KEY=${POD_KEY:-$HOME/.ssh/id_ed25519}
DIR=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$DIR/../.." && pwd)
SSH=(ssh -p "$POD_PORT" -i "$KEY" -o ConnectTimeout=15 root@"$POD_IP")
SCP=(scp -q -P "$POD_PORT" -i "$KEY")
URL="https://$POD_ID-8888.proxy.runpod.net"

case "${1:-}" in
  up)
    echo "== copying code to the pod"
    "${SSH[@]}" 'mkdir -p /workspace/rad-ar /workspace/radar/server/relay /workspace/radar/hub'
    "${SCP[@]}" "$DIR"/setup_runpod.sh "$DIR"/start_worker_runpod.sh "$DIR"/warm_worker.py "$DIR"/radar_up.sh root@"$POD_IP":/workspace/
    # worker.py is the teammate's; only seed it if the pod has none
    if [ -f "$DIR/worker.py" ]; then
      "${SSH[@]}" '[ -f /workspace/rad-ar/worker.py ]' || "${SCP[@]}" "$DIR/worker.py" root@"$POD_IP":/workspace/rad-ar/worker.py
    fi
    "${SCP[@]}" "$ROOT"/server/relay/*.py "$ROOT"/server/relay/protocol.md root@"$POD_IP":/workspace/radar/server/relay/
    "${SCP[@]}" "$ROOT"/hub/* root@"$POD_IP":/workspace/radar/hub/
    "${SSH[@]}" bash /workspace/radar_up.sh "${2:-}"
    echo
    echo "hub:    $URL"
    echo "phones: wss://$POD_ID-8888.proxy.runpod.net   (or scan the hub's Connect QR)"
    ;;
  status)
    "${SSH[@]}" 'echo "worker: $(curl -s --max-time 3 localhost:8010/health || echo down)"; echo "relay:  $(curl -s --max-time 3 localhost:8888/maps | head -c 200 || echo down)"; nvidia-smi --query-gpu=name,memory.used --format=csv,noheader'
    echo "hub:    $URL"
    ;;
  logs) "${SSH[@]}" 'tail -n 20 /workspace/worker.log; echo ----; tail -n 20 /workspace/relay.log' ;;
  ssh) "${SSH[@]}" ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
