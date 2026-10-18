#!/usr/bin/env bash
# The EC2 live box (lingbot worker + relay + hub behind HTTPS) from the Mac. Ids come from
# server/aws/live.env, written when the instance was created.
#   server/aws/live.sh deploy    upload server/ + hub/ + worker.py to S3 and restart the services
#   server/aws/live.sh up        start the instance (hub is up ~3 min later: boot, worker, warm-up)
#   server/aws/live.sh down      stop it (disk, weights and venv are kept; no GPU billing while stopped)
#   server/aws/live.sh status    instance state, worker health, relay maps
#   server/aws/live.sh logs      worker + relay + caddy logs (via SSM: no SSH key, no open port)
#   server/aws/live.sh ssh       interactive shell via SSM (needs the session-manager-plugin)
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd); ROOT=$(cd "$DIR/../.." && pwd)
[ -f "$DIR/live.env" ] && . "$DIR/live.env"
: "${RADAR_S3_BUCKET:=radar-dubhacks-784917519323}" "${AWS_DEFAULT_REGION:=us-east-2}" "${RADAR_LIVE_INSTANCE:?run the provisioning first (see README)}"
export AWS_DEFAULT_REGION
eval "$(aws configure export-credentials --format env 2>/dev/null || true)"

ssm() {   # run a shell command on the box, print its output
  local ID; ID=$(aws ssm send-command --instance-ids "$RADAR_LIVE_INSTANCE" --document-name AWS-RunShellScript --parameters "commands=[\"$1\"]" --query Command.CommandId --output text)
  sleep "${2:-4}"
  aws ssm get-command-invocation --command-id "$ID" --instance-id "$RADAR_LIVE_INSTANCE" --query '[StandardOutputContent,StandardErrorContent]' --output text
}
case "${1:-}" in
  deploy)
    [ -f "$ROOT/server/ec2/worker.py" ] || { echo "missing server/ec2/worker.py (the teammate's worker)"; exit 1; }
    COPYFILE_DISABLE=1 tar czf /tmp/live.tgz --no-xattrs -C "$ROOT" --exclude='server/maps' --exclude='server/sessions' --exclude='__pycache__' server hub
    aws s3 cp /tmp/live.tgz "s3://$RADAR_S3_BUCKET/code/live.tgz" --quiet
    echo "uploaded; restarting services (worker warm-up takes ~2 min)"
    ssm 'systemctl restart radar-worker radar-relay; sleep 2; systemctl is-active radar-worker radar-relay caddy' 6 ;;
  up) aws ec2 start-instances --instance-ids "$RADAR_LIVE_INSTANCE" --query 'StartingInstances[0].CurrentState.Name' --output text; echo "hub: https://$RADAR_HOST" ;;
  down) aws ec2 stop-instances --instance-ids "$RADAR_LIVE_INSTANCE" --query 'StoppingInstances[0].CurrentState.Name' --output text ;;
  status)
    aws ec2 describe-instances --instance-ids "$RADAR_LIVE_INSTANCE" --query 'Reservations[0].Instances[0].[State.Name,PublicIpAddress,InstanceType]' --output text
    echo "hub:    https://$RADAR_HOST"; echo "phones: wss://$RADAR_HOST   (or scan the hub's Connect QR)"
    echo "worker: $(curl -s --max-time 5 "https://$RADAR_HOST/maps" | head -c 160 || echo down)" ;;
  logs) ssm 'systemctl is-active radar-worker radar-relay caddy; journalctl -u radar-worker -n 15 --no-pager -o cat; echo ----; journalctl -u radar-relay -n 25 --no-pager -o cat; echo ----; journalctl -u caddy -n 5 --no-pager -o cat' ;;
  ssh) aws ssm start-session --target "$RADAR_LIVE_INSTANCE" ;;
  *) sed -n '2,9p' "$0"; exit 1 ;;
esac
