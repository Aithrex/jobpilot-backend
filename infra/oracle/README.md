# Ingest runner on Oracle Cloud Always Free

The crawl (`services/ingest_runner`) moves off the EC2 `t3.small`
(`i-06bf0e03e60d9d409`), which cost ~$35/month in September 2026: instance
$16, burst CPU credits $9.50 (it averaged 36% CPU against a 20% baseline),
public IPv4 $3.90, data transfer $3.40, EBS $2.60.

Oracle's Always Free tier includes a 4-OCPU / 24 GB Ampere A1 VM, 200 GB of
block storage and 10 TB/month egress, at $0. That is roughly 4x the CPU the
crawl has today, with no burst credits to run out of.

Nothing about the crawl changes: same unit file (`infra/ec2/ingest-runner.service`),
same installer (`infra/ec2/install-ingest-runner.sh`), same sources and
dials. Only two things differ off AWS, both in `ingest-runner.oracle.conf`:
secrets come from `/etc/jobpilot/ingest-runner.env` instead of SSM, and the
memory cap fits a 24 GB box.

CloudTrail for the EC2 box's role (2026-09-30 to 10-01) shows the crawl calls
exactly two AWS services, SSM `GetParameters` (the Mongo URI) and S3 (the JD
store). No Bedrock, no Lambda. So the env file needs the Mongo URI and one
narrow S3 key, and nothing else.

## How deploys work

EC2 is deployed over SSM: no inbound port, no key in CI. The off-AWS
equivalent is that the box **pulls**:

1. On push to `main`, after the offline suite is green for that SHA, the
   `deploy-ingest-runner-oracle` job in `deploy.yml` force-pushes the SHA to
   `deploy/ingest-runner`. Nothing else writes that branch.
2. `jobpilot-pull-deploy.timer` on the box checks the branch every 2 minutes
   and runs the shared installer (import checks included) from a git
   checkout. A SHA that fails to install is not retried until the next push.
3. The runner writes `revision` into `ingest_runner_heartbeat`. The CI job
   stays red until the heartbeat reports the SHA it published.

## Cutover

### 1. Oracle account and VM (you, in the Oracle console)

1. Sign up at cloud.oracle.com. A card is required for verification, and
   Always Free resources are never charged.
2. **Upgrade the account to Pay As You Go.** Always Free resources stay $0,
   but Oracle can reclaim an idle Always Free VM on a free-tier-only account.
   Then add a **budget alert at $1** so any accidental paid resource emails you.
3. Create an instance:
   - Image: **Canonical Ubuntu 24.04** (aarch64)
   - Shape: **VM.Standard.A1.Flex**, 4 OCPU, 24 GB
   - Boot volume: 50 GB
   - Public IPv4: yes (ephemeral; it is the crawl's egress)
   - SSH key: yours
   - If you get "Out of host capacity", try another availability domain, or
     retry later. It is common in popular regions.
4. Security list: allow ingress TCP 22 **from your IP only**. The box needs
   no other inbound port, because deploys are pulled.

### 2. A narrow AWS key for the JD store (you)

```bash
export AWS_PROFILE=aithrex
aws iam create-user --user-name jobpilot-ingest-oracle
aws iam put-user-policy --user-name jobpilot-ingest-oracle \
  --policy-name ingest-content-store --policy-document '{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": "s3:GetObject",
     "Resource": ["arn:aws:s3:::jobpilot-resumes-prod-032398581420/job-jd/*",
                  "arn:aws:s3:::jobpilot-resumes-prod-032398581420/job-archive/*"]},
    {"Effect": "Allow", "Action": "s3:PutObject",
     "Resource": ["arn:aws:s3:::jobpilot-resumes-prod-032398581420/job-jd/*",
                  "arn:aws:s3:::jobpilot-resumes-prod-032398581420/job-archive/*"]}
  ]}'
aws iam create-access-key --user-name jobpilot-ingest-oracle
```

The key can touch only the two prefixes the crawl writes, and none of the
user files. There is deliberately no `s3:ListBucket`: without it, a HEAD on a
missing JD returns 403 instead of 404, and `put_jd` uploads in both cases,
which is exactly what a missing JD needs. The bucket's default encryption is the AWS-managed `aws/s3` key,
which needs no KMS grant.

### 3. Bootstrap the box

```bash
scp -r infra/oracle ubuntu@<ORACLE_IP>:
ssh ubuntu@<ORACLE_IP> sudo bash oracle/bootstrap.sh
#  -> prints a deploy key and exits 2. Add it (read-only):
gh repo deploy-key add <(ssh ubuntu@<ORACLE_IP> sudo cat /root/.ssh/jobpilot_deploy.pub) \
  --repo Aithrex/jobpilot-backend --title oracle-ingest
ssh ubuntu@<ORACLE_IP> sudo bash oracle/bootstrap.sh
#  -> exits 3: fill MONGODB_URI and the step-2 keys in /etc/jobpilot/ingest-runner.env
ssh ubuntu@<ORACLE_IP> sudo nano /etc/jobpilot/ingest-runner.env
ssh ubuntu@<ORACLE_IP> sudo bash oracle/bootstrap.sh
```

### 4. First deploy

Set the repo variable, then trigger a deploy:

```bash
gh variable set ORACLE_INGEST_ENABLED --body true --repo Aithrex/jobpilot-backend
gh workflow run deploy.yml --repo Aithrex/jobpilot-backend --ref main
```

`Deploy ingest runner (Oracle)` goes green once the box reports the SHA.
Running both boxes at once is safe, because a Mongo lease makes the loser a
no-op. It just pays twice, so do not leave it for long.

### 5. Retire EC2 (same day)

```bash
export AWS_PROFILE=aithrex AWS_REGION=us-east-1
# stop the crawl on EC2 (the box itself keeps running, so it can roll back in a minute)
aws ssm send-command --instance-ids i-06bf0e03e60d9d409 --document-name AWS-RunShellScript \
  --parameters 'commands=["systemctl disable --now ingest-runner"]'
gh variable set EC2_INGEST_ENABLED --body false --repo Aithrex/jobpilot-backend
```

Wait for one full daily cycle: `ingest_runner_daily_runs` should show the
05:20 maintenance and 05:45 coverage jobs from the Oracle hostname, and the
retention watchdog should stay green. Then:

```bash
aws ec2 terminate-instances --instance-ids i-06bf0e03e60d9d409
# the Elastic IP is billed while unattached - release it
aws ec2 describe-addresses --query 'Addresses[?PublicIp==`35.169.137.220`].AllocationId' --output text \
  | xargs -n1 aws ec2 release-address --allocation-id
```

The EBS volume has `DeleteOnTermination=true`. Afterwards, delete the
`deploy-ingest-runner` (EC2) job from `deploy.yml`.

### Rollback

Before termination: re-enable the unit on EC2 (`systemctl enable --now
ingest-runner`), unset `EC2_INGEST_ENABLED`, and stop the Oracle unit. The
two can overlap safely.

## Operating

| What | Where |
|---|---|
| Crawl logs | `journalctl -u ingest-runner` |
| Deploy logs | `journalctl -u jobpilot-pull-deploy` |
| Running SHA | `/opt/jobpilot/app/REVISION`, and `revision` in `ingest_runner_heartbeat` |
| Retry a failed SHA | `sudo rm /var/lib/jobpilot/failed-sha` |
| Secrets | `/etc/jobpilot/ingest-runner.env` (root:jobpilot 0640) |
