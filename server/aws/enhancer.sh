#!/usr/bin/env bash
# The EC2 enhancer from the Mac. Instance id and bucket come from server/aws/enhancer.env (written when
# the instance is created) or the environment.
#   server/aws/enhancer.sh deploy    upload the current enhance + relay code to S3 (picked up at next boot)
#   server/aws/enhancer.sh up        start the instance (the relay does this itself on "Enhance")
#   server/aws/enhancer.sh down      stop it now (it stops itself after 3 idle minutes anyway)
#   server/aws/enhancer.sh status    instance state, queued jobs
#   server/aws/enhancer.sh logs      job loop log (via SSM: no SSH key, no open port)
#   server/aws/enhancer.sh restart   restart the job loop (re-pulls the code)
#   server/aws/enhancer.sh ssh       interactive shell via SSM (needs the session-manager-plugin)
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd); ROOT=$(cd "$DIR/../.." && pwd)
[ -f "$DIR/enhancer.env" ] && . "$DIR/enhancer.env"
: "${RADAR_S3_BUCKET:=radar-dubhacks-784917519323}" "${AWS_DEFAULT_REGION:=us-east-2}"
export AWS_DEFAULT_REGION
eval "$(aws configure export-credentials --format env 2>/dev/null || true)"

case "${1:-}" in
  deploy)
    COPYFILE_DISABLE=1 tar czf /tmp/enhance.tgz --no-xattrs -C "$ROOT" server/enhance server/relay/store.py server/relay/wire.py
    aws s3 cp /tmp/enhance.tgz "s3://$RADAR_S3_BUCKET/code/enhance.tgz"
    echo "uploaded; a running enhancer picks it up after: $0 restart" ;;
  up) aws ec2 start-instances --instance-ids "$RADAR_ENHANCE_INSTANCE" --query 'StartingInstances[0].CurrentState.Name' --output text ;;
  down) aws ec2 stop-instances --instance-ids "$RADAR_ENHANCE_INSTANCE" --query 'StoppingInstances[0].CurrentState.Name' --output text ;;
  status)
    aws ec2 describe-instances --instance-ids "$RADAR_ENHANCE_INSTANCE" --query 'Reservations[0].Instances[0].[State.Name,PublicIpAddress,InstanceType]' --output text
    echo "queued jobs:"; aws s3 ls "s3://$RADAR_S3_BUCKET/jobs/" || true ;;
  logs|restart)
    CMD='journalctl -u radar-enhance -n 60 --no-pager; tail -5 /var/log/cloud-init-output.log'
    [ "$1" = restart ] && CMD='systemctl restart radar-enhance; sleep 3; journalctl -u radar-enhance -n 10 --no-pager'
    ID=$(aws ssm send-command --instance-ids "$RADAR_ENHANCE_INSTANCE" --document-name AWS-RunShellScript --parameters "commands=[\"$CMD\"]" --query Command.CommandId --output text)
    sleep 4
    aws ssm get-command-invocation --command-id "$ID" --instance-id "$RADAR_ENHANCE_INSTANCE" --query '[StandardOutputContent,StandardErrorContent]' --output text ;;
  ssh) aws ssm start-session --target "$RADAR_ENHANCE_INSTANCE" ;;
  *) sed -n '2,9p' "$0"; exit 1 ;;
esac
