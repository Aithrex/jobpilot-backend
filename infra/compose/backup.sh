#!/usr/bin/env bash
# Backups to Oracle Object Storage, encrypted on this box by rclone crypt: Oracle
# only ever holds ciphertext. Free tier: 20 GB and 50,000 requests a month.
#
#   backup.sh user     hourly. Only changed files are sent. A file deleted or
#                      replaced here moves to deleted/<date>, kept 30 days.
#   backup.sh nightly  one compressed archive of job descriptions and archived
#                      postings, replacing yesterday's only after it uploads.
#   backup.sh check    fails when the bucket or the data disk nears its limit.
#
# Request budget: about 4,000 a month hourly + 3,000 nightly. --use-server-modtime
# is what keeps it there: without it rclone asks about every file, every hour.
set -euo pipefail

DATA=/mnt/jobpilot-data/s3
RESUMES=jobpilot-resumes-prod-032398581420
MAIL=jobpilot-mail-prod-032398581420
RC=(rclone --config /etc/jobpilot/rclone.conf --stats 0)
LIMIT_BYTES=$((17 * 1000 * 1000 * 1000))   # alert at 17 GB of the 20 GB
DISK_LIMIT_PCT=85

case "${1:-}" in
  user)
    STAMP=$(date -u +%Y-%m-%d)
    SYNC=(sync --update --use-server-modtime --fast-list --transfers 4 --s3-no-check-bucket)
    "${RC[@]}" "${SYNC[@]}" "$DATA/$RESUMES" "backup:user/$RESUMES" \
      --exclude '/job-jd/**' --exclude '/job-archive/**' --exclude '/company-logos/**' \
      --backup-dir "backup:deleted/$STAMP/$RESUMES"
    "${RC[@]}" "${SYNC[@]}" "$DATA/$MAIL" "backup:user/$MAIL" \
      --backup-dir "backup:deleted/$STAMP/$MAIL"
    "${RC[@]}" delete --min-age 30d --fast-list backup:deleted
    ;;
  nightly)
    tar -C "$DATA/$RESUMES" -cf - job-jd job-archive company-logos \
      | zstd -q -T2 -9 \
      | "${RC[@]}" rcat --s3-chunk-size 64M --s3-upload-concurrency 2 --s3-no-check-bucket backup:nightly/jobs.tar.zst.new
    "${RC[@]}" moveto --s3-no-check-bucket backup:nightly/jobs.tar.zst.new backup:nightly/jobs.tar.zst
    "$0" check
    ;;
  check)
    USED=$("${RC[@]}" size --json --fast-list oci:jobpilot-backups | jq .bytes)
    echo "backup bucket: $USED bytes"
    [ "$USED" -lt "$LIMIT_BYTES" ] || { echo "backup bucket over $LIMIT_BYTES bytes" >&2; exit 1; }
    PCT=$(df --output=pcent "$DATA" | tail -1 | tr -dc 0-9)
    echo "data disk: ${PCT}%"
    [ "$PCT" -lt "$DISK_LIMIT_PCT" ] || { echo "data disk at ${PCT}%" >&2; exit 1; }
    ;;
  *) echo "usage: backup.sh user|nightly|check" >&2; exit 2 ;;
esac
