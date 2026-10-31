# Auto-Apply Observability (spec §11)

> **Retired 2026-10-31.** This describes the AWS setup, which has been deleted. It is kept for history. Current hosting: [`infra/compose/README.md`](../../infra/compose/README.md).

The metrics + alarms for the auto-apply execution plane (AWS orchestrator +
private GCP Cloud Run worker). Every milestone is emitted as ONE structured,
PII-FREE log line by `shared/python/services/apply_metrics.py` (and the worker's
inline fallback in `infra/gcp/cloud-run-worker/worker.py`). CloudWatch metric
filters (AWS side) and GCP log-based metrics (worker side) turn those lines into
the alarms below.

Cross-links: `docs/ops/gcp-executor-setup.md` (queue/worker/budget wiring),
`docs/ops/managed-mailbox-setup.md` (mail plane), `docs/security/auto-apply-audit.md`
(the controls these metrics evidence), `docs/ops/auto-apply-incident-runbook.md`
(what to DO when an alarm fires), `docs/ops/auto-apply-rollout-rollback.md` (which
alarms gate each rollout stage).

---

## The metric line (contract)

Anchor prefix: **`apply_metric`** followed by a compact JSON object. Example:

```
apply_metric {"adapter":"greenhouse","application_id":"...","duration_ms":812,
              "execution_id":"...","metric":"apply.milestone","outcome":"submitted",
              "phase":"submit","ts":"2026-07-24T12:00:00.000Z"}
```

Correlation ids: **`execution_id`** (one dispatch), **`application_id`** (one
ledger row), **`adapter`** (ATS family). Measurement fields: **`phase`**,
**`outcome`**, **`duration_ms`**, plus bounded `reason` / `count`.

**PII rule (control C6):** a line NEVER carries answers, résumé bytes/keys, mail,
credentials, cookies, or any user text. `apply_metrics.emit()` enforces this
structurally — only a fixed allow-list of bounded scalar fields is serialized; a
non-scalar (dict/list/object) is dropped, never stringified. Do not add free-text
fields.

### Phases (bounded vocabulary)

| phase | emitted at | site |
|---|---|---|
| `queue_ack` | Cloud Task enqueued / worker received the task | `cloud_run_executor.submit` (AWS) |
| `task_start` | worker began the run (post-claim) | `worker.py::_run` |
| `fill_start` | adapter began filling the form | `worker.py::_run`, `browser_executor` |
| `submit` | the Submit click boundary | `worker.py::_run` |
| `confirmation` | a confirmed submission was observed | `worker.py`, `auto_apply_service.finalize_submission` |
| `submission_unknown` | clicked, outcome ambiguous — HELD, never retried | `worker.py`, `finalize_submission` |
| `waiting_human` | captcha / verify wall handed back to the human | `worker.py::_run` |
| `pause` | execution paused (kill switch / user pause / revocation / precheck abort) | `worker.py::_run` |
| `selector_drift` | expected fields not found (ATS template change) | `browser_executor._fill_fields` |

---

## Metrics to alarm on

Derive each from the metric filter noted; thresholds are starting points — tune
against the canary baseline (see rollout doc).

| # | Metric | Derivation (filter on the JSON line) | Alarm target |
|---|---|---|---|
| 1 | **queue-ack p95 latency** | time from ledger `queued_for_execution` → first `phase="queue_ack"` | p95 > 60s for 10m |
| 2 | **task-start p95 latency** | `queue_ack.ts` → `task_start.ts` per `execution_id` | p95 > 120s for 10m (cold-start / worker starvation) |
| 3 | **success rate** | count(`phase="confirmation"`) / count(`phase="submit"`) over 1h | < 0.80 for 30m |
| 4 | **submission_unknown count** | count(`phase="submission_unknown"`) | > 3 in 1h (ANY sustained value is a human-review backlog — see incident runbook) |
| 5 | **per-ATS success/failure** | success rate #3 grouped by `adapter` | any single `adapter` < 0.50 for 30m → per-adapter kill-switch candidate |
| 6 | **selector-drift rate** | count(`phase="selector_drift"`) grouped by `adapter` | > 5 in 15m for one `adapter` (leading indicator of a template change → drift/circuit-breaker) |
| 7 | **cost / attempt** | Cloud Run billable time + Bedrock spend / count(`phase="task_start"`) | > 1.5× 7-day baseline (also covered by the GCP budget alarm) |
| 8 | **bounce / complaint rate** | SES bounce + complaint notifications / sends (managed-mailbox plane) | bounce > 5% or ANY complaint spike — pause mail sends |
| 9 | **waiting_human rate** | count(`phase="waiting_human"`) / count(`phase="submit"`) | > 0.30 for 1h (an ATS started walling — expected for some; alarm on a step change) |

### Alarm targets already in place (do NOT duplicate)

- **P5 Cloud Tasks DLQ** — bounded retry + dead-letter on the executor queue
  (`infra/gcp/terraform/cloud_tasks.tf`); alarm on DLQ depth > 0.
- **GCP budget alarms** — Cloud Run + Cloud Tasks project budget alerts
  (`docs/ops/gcp-executor-setup.md`); back-stop for metric #7.
- **Ledger `submission_unknown` hold** — already never auto-retried
  (`finalize_submission`); metric #4 is the operator-facing surface of that hold.

---

## Wiring notes

- AWS side (resume Lambda): lines land in the Lambda's CloudWatch log group. Add
  metric filters keyed on `apply_metric` + the `phase`/`adapter` fields; graph on
  a per-plane dashboard.
- Worker side (Cloud Run): lines land in Cloud Logging. Create log-based metrics
  with the same anchor; the worker's fallback emitter guarantees the prefix even
  if the shared layer is not bundled into the image.
- Correlate across planes by `execution_id` (the same value is on the AWS
  `queue_ack` and every worker milestone).
