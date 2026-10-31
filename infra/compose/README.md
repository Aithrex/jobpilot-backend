# Production on the Oracle box

Everything runs on one Oracle Cloud A1 VM (`jobpilot-ingest`, us-ashburn-1,
`150.136.238.50`) under Docker Compose, except the crawler, which stays a
systemd unit. AWS was retired on 2026-10-31.

```
Browser -> Cloudflare (DNS, CDN, HTTPS) -> Tunnel -> web (nginx)
                                                      |-- /        the built SPA
                                                      |-- /api/    api_blue or api_green (gunicorn)
                                                      `-- files.aspirely.me  signed links only -> s3
worker (scheduled + async jobs) ----> s3 (VersityGW) ----> /mnt/jobpilot-data/s3
ingest-runner (systemd, the crawl) -> s3 at 172.30.0.10:7070
All of it -> MongoDB Atlas
```

No website port is open. The only way in is the outbound tunnel. SSH is limited
to the owner's IP by the VCN security list.

## Where things live

| What | Where |
|---|---|
| Stack definition | `infra/compose/docker-compose.yml`, checked out at `/opt/jobpilot/src` |
| Secrets | `/etc/jobpilot/stack.env`, `s3.env`, `rclone.conf` (root only) |
| Crawler settings | `/etc/jobpilot/ingest-runner.env` |
| Tunnel | `/etc/jobpilot/cloudflared/` (tunnel `f3ebe07b-118f-4424-aef3-6fe8193df351`) |
| Live files | `/mnt/jobpilot-data/s3/<bucket>/<key>`, plain files on a 150 GB block volume |
| Website build | `/srv/jobpilot/web` |
| Deploy state | `/var/lib/jobpilot/`, `/etc/jobpilot/compose.env` |

Buckets keep their old names, so S3 keys stored in Mongo stay valid. Any
S3-compatible store can replace VersityGW by changing `AWS_ENDPOINT_URL_S3`.

## Deploys

Merge to `main`. `tests.yml` runs, `deploy.yml` waits for it, then moves
`deploy/ingest-runner` to that commit. CI has no server credentials: a 2-minute
timer on the box pulls (`infra/oracle/pull-deploy.sh`), reinstalls the crawler
and runs `deploy.sh`:

1. Build the image while the live version keeps serving.
2. Start the idle colour and wait for its health check.
3. Reload nginx onto it, and confirm the revision through nginx.
4. Replace the worker, then stop the old colour. Running jobs finish first.

A failure leaves the old version serving. CI goes green only when
`/api/health` reports the commit and `scripts/check_deployed_api.py` passes.

The frontend's CI publishes its build to `deploy/web`; `pull-web.sh` copies new
assets first and swaps `index.html` last.

**Roll back** by reverting the commit on `main`. Check what is live with
`curl https://aspirely.me/api/health`.

## Everyday commands

Run in `/opt/jobpilot/src/infra/compose` as root.

```bash
docker compose --env-file /etc/jobpilot/compose.env ps
docker compose --env-file /etc/jobpilot/compose.env logs --tail 100 worker
journalctl -u jobpilot-pull-deploy -u ingest-runner --since "1 hour ago"
systemctl list-timers | grep jobpilot
```

The live API colour is in `/var/lib/jobpilot/active-color`. To hold scheduled
jobs through a deploy, `touch /etc/jobpilot/hold-worker`.

## Scheduled jobs

`services/scheduler/runner.py` runs what EventBridge used to. A Mongo claim in
`scheduler_slots` gives each slot one run, and a slot missed in a restart is
picked up within 10 minutes. `ingest_maintenance` belongs to the crawler.

| Job | When |
|---|---|
| `digest_hourly` | Hourly |
| `learning_due` | Every 10 minutes |
| `bandit_nightly`, `trend_aggregate`, `ats_signal_aggregate` | 03:00, 04:00, 04:30 UTC |
| `guest_retention`, `events_refresh` | 05:45, 06:00 UTC |

The retention watchdog is the timer `jobpilot-retention-watchdog` (06:37 UTC).

## Backups

| Backup | Schedule | Kept | Goes to |
|---|---|---|---|
| User files and mail | Hourly | Current, plus deleted or replaced files for 30 days | Object Storage `jobpilot-backups` |
| Job descriptions, archive, logos | Nightly, 07:30 UTC | Latest only | Object Storage |
| Whole data disk | Sunday 08:00 UTC | 14 days | OCI volume backups |
| Database | Daily | 8 days | Atlas snapshots |

Object Storage backups are encrypted on the box by rclone crypt. **Losing
`rclone.conf` loses them.** A copy is at
`D:\jobpilot-aws-backup\secrets\oracle-backup-rclone.conf`.

A failed timer mails `ALERT_EMAIL` from `/etc/jobpilot/alerts.env`.
`backup.sh check` fails at 9 GB in the bucket or 85% on the disk.

Restore a user file, or the whole job archive:

```bash
rclone --config /etc/jobpilot/rclone.conf copyto "backup:user/<bucket>/<key>" /tmp/restored
rclone --config /etc/jobpilot/rclone.conf cat backup:nightly/jobs.tar.zst | zstd -d | tar -xC /tmp/restored
```

Files copied straight onto the disk must be owned by `10002:10002`.

## Free-tier limits

The account has no payment method and a $1 budget alert. **Do not upgrade it.**

| Limit | Allowance | Use |
|---|---|---|
| A1 compute | 1,500 OCPU-hours, 9,000 GB-hours a month (2 OCPU / 12 GB) | Box is 4/24, over the limit |
| Block storage | 200 GB across all disks | 200 GB: 50 boot + 150 data, no room left |
| Volume backups | 5 | At most 4 |
| Object Storage | 20 GB, 50,000 requests a month | 1.9 GB, about 8,000 requests (estimate) |
| Outbound data | 10 TB a month | 105 GB in October |

**Oracle's docs say A1 instances over 2 OCPU / 12 GB are disabled when the trial
ends, then deleted after 30 days.** The owner chose to stay at 4/24 until Oracle
gives notice. Compose limits and the crawler's `MemoryMax` already fit 2/12.
Never terminate the box. Sign in to Oracle at least every 60 days.

## AI

The app selects its provider with `LLM_PROVIDER` in `stack.env`, currently
`gemini`. **As of 2026-10-31 there is no working key, so AI features fail.**
Gemini 2.5 is closed to new users, so a key needs `GEMINI_FLASH_MODEL_ID`,
`GEMINI_PRO_MODEL_ID` and `GEMINI_PREVIEW_MODEL_ID` too. Calls mask contact
details by default, and `GEMINI_FALLBACK_MODELS` moves past exhausted models.

## Settings

`stack.env` is built by `render_stack_env.py` from the AWS backup on D:. To
change one value, edit the file and recreate the containers with a deploy.
Keep `CREDENTIALS_MASTER_KEY` safe: it opens the saved job-site passwords, and
its copy is `D:\jobpilot-aws-backup\secrets\oracle-generated.json`.

Atlas accepts only the box and the owner's home IP, so a new IP has to be added
to the Atlas access list.

## Moving to another provider

Copy `/mnt/jobpilot-data/s3` and `/etc/jobpilot`, install Docker, check out this
repo, run `deploy.sh`, and point the tunnel at the new box.
