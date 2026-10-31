#!/usr/bin/env bash
# Mail the owner when a timer fails. Called by jobpilot-alert@.service. Prints
# no secret. ALERT_EMAIL is in /etc/jobpilot/alerts.env.
set -euo pipefail

UNIT="${1:-unknown}"
. /etc/jobpilot/alerts.env
TOKEN=$(grep -m1 '^ZEPTOMAIL_TOKEN=' /etc/jobpilot/stack.env | cut -d= -f2-)
FROM=$(grep -m1 -E '^(EMAIL_FROM_ADDRESS|SES_FROM_ADDRESS)=' /etc/jobpilot/stack.env | cut -d= -f2- | sed -E 's/.*<(.*)>.*/\1/')
LOG=$(journalctl -u "$UNIT" -n 15 --no-pager -o cat | sed 's/[<>&]//g' | tail -15)

case "$TOKEN" in [Zz]oho-enczapikey*) AUTH="$TOKEN" ;; *) AUTH="Zoho-enczapikey $TOKEN" ;; esac
jq -n --arg from "$FROM" --arg to "$ALERT_EMAIL" --arg unit "$UNIT" --arg log "$LOG" '{
  from: {address: $from, name: "Aspirely server"},
  to: [{email_address: {address: $to}}],
  subject: ("Aspirely server alert: " + $unit + " failed"),
  htmlbody: ("<p>" + $unit + " failed on the Oracle box.</p><pre>" + $log + "</pre>")
}' | curl -fsS --max-time 20 -o /dev/null -X POST https://api.zeptomail.com/v1.1/email \
  -H "Authorization: $AUTH" -H 'Content-Type: application/json' -d @-
