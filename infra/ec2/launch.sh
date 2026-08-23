#!/bin/bash
# Launch the JobPilot browser executor (Phase 2).
# IAM role, instance profile and security group already exist (2026-08-22);
# this creates ONLY the instance. Safe to run from any directory.
set -euo pipefail
export AWS_PROFILE=aithrex
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

IID=$(aws ec2 run-instances --region us-east-1 \
  --image-id ami-052355af2a014bd2c \
  --instance-type t3.small \
  --iam-instance-profile Name=jobpilot-apply-executor-ec2 \
  --security-group-ids sg-0f94f14fda5697979 \
  --subnet-id subnet-0330e2150820bbef5 \
  --metadata-options 'HttpTokens=required,HttpEndpoint=enabled' \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":30,"VolumeType":"gp3","Encrypted":true,"DeleteOnTermination":true}}]' \
  --user-data "file://$HERE/executor-userdata.sh" \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=jobpilot-apply-executor},{Key=Project,Value=jobpilot},{Key=Role,Value=browser-executor}]' \
  --query 'Instances[0].InstanceId' --output text)

echo "INSTANCE_ID=$IID"
echo "Bootstrap takes ~4-6 min (Chromium + deps). Check with:"
echo "  aws ec2 describe-instance-status --region us-east-1 --instance-ids $IID"
