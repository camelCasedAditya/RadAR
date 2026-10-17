#!/usr/bin/env bash
# RadAR GPU worker on EC2. Run from the repo root on the Mac.
#
#   server/ec2/radar-ec2.sh launch   # create the g6e.xlarge (once; starts billing ~$1.86/hr)
#   server/ec2/radar-ec2.sh setup    # install lingbot-map + worker on it (once, ~15 min)
#   server/ec2/radar-ec2.sh start    # start a stopped instance (billing resumes)
#   server/ec2/radar-ec2.sh tunnel   # forward localhost:8010 -> worker; reconnects by itself
#   server/ec2/radar-ec2.sh status   # instance state + worker health
#   server/ec2/radar-ec2.sh ssh      # shell on the instance
#   server/ec2/radar-ec2.sh stop     # stop compute billing (disk is kept, ~$8/month)
#
# Requires `aws login` first. The worker listens on 127.0.0.1 only; the tunnel is the only way in.
set -euo pipefail

export AWS_REGION=us-east-2            # the only region our AWS organization allows launches in
TYPE=${TYPE:-g6e.xlarge}
NAME=radar-gpu
KEY=radar-mac                          # imported from ~/.ssh/id_ed25519.pub
SG_NAME=radar-gpu                      # inbound SSH only
DIR=$(cd "$(dirname "$0")" && pwd)
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o ConnectTimeout=10)

instance_id() {
  aws ec2 describe-instances --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text
}
host() { aws ec2 describe-instances --instance-ids "$(instance_id)" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text; }

case "${1:-}" in
  launch)
    [ "$(instance_id)" != "None" ] && { echo "already exists: $(instance_id)"; exit 0; }
    AMI=$(aws ssm get-parameter --name /aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id --query Parameter.Value --output text)
    SG=$(aws ec2 describe-security-groups --group-names "$SG_NAME" --query 'SecurityGroups[0].GroupId' --output text)
    aws ec2 run-instances --image-id "$AMI" --instance-type "$TYPE" --key-name "$KEY" --security-group-ids "$SG" \
      --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=150,VolumeType=gp3}' \
      --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" \
      --query 'Instances[0].InstanceId' --output text
    aws ec2 wait instance-running --instance-ids "$(instance_id)"
    echo "running at $(host)"
    ;;
  setup)
    H=$(host)
    until ssh "${SSH_OPTS[@]}" ubuntu@"$H" true 2>/dev/null; do echo "waiting for ssh..."; sleep 5; done
    [ -f "$DIR/worker.py" ] || { echo "missing $DIR/worker.py (the teammate's lingbot worker)"; exit 1; }
    ssh "${SSH_OPTS[@]}" ubuntu@"$H" mkdir -p rad-ar
    scp "${SSH_OPTS[@]}" "$DIR/worker.py" ubuntu@"$H":rad-ar/worker.py
    scp "${SSH_OPTS[@]}" "$DIR/setup.sh" ubuntu@"$H":setup.sh
    ssh "${SSH_OPTS[@]}" ubuntu@"$H" bash setup.sh
    ;;
  start)
    aws ec2 start-instances --instance-ids "$(instance_id)" --query 'StartingInstances[0].CurrentState.Name' --output text
    aws ec2 wait instance-running --instance-ids "$(instance_id)"
    echo "running at $(host); worker starts on boot (~1 min to load the model)"
    ;;
  stop)
    aws ec2 stop-instances --instance-ids "$(instance_id)" --query 'StoppingInstances[0].CurrentState.Name' --output text
    ;;
  status)
    ID=$(instance_id)
    [ "$ID" = "None" ] && { echo "no $NAME instance yet (run: $0 launch)"; exit 0; }
    aws ec2 describe-instances --instance-ids "$ID" --query 'Reservations[0].Instances[0].[InstanceId,InstanceType,State.Name,PublicIpAddress]' --output text
    curl -s --max-time 3 localhost:8010/health && echo || echo "worker not reachable on localhost:8010 (is the tunnel running?)"
    ;;
  tunnel)
    # Public IP changes on every start, so look it up each time we reconnect.
    while true; do
      H=$(host 2>/dev/null || echo None)
      if [ "$H" = "None" ] || [ -z "$H" ]; then echo "instance not running; retrying in 10s"; sleep 10; continue; fi
      echo "tunnel -> $H"
      ssh -N "${SSH_OPTS[@]}" -o ExitOnForwardFailure=yes -L 8010:localhost:8010 ubuntu@"$H" || true
      echo "tunnel dropped; retrying in 3s"; sleep 3
    done
    ;;
  ssh) ssh "${SSH_OPTS[@]}" ubuntu@"$(host)" ;;
  *) sed -n '2,13p' "$0"; exit 1 ;;
esac
