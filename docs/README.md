# JobPilot Backend — documentation

Ten directories, one rule for each: **what we decided**, **what we're
building**, **how to run it**, and **what the numbers actually are**. If a
document does not answer one of those, it is in the wrong place.

Only `README.md` and `CLAUDE.md` live at the repo root.

| Directory | Holds | Read it when |
| --- | --- | --- |
| [`decisions/`](decisions/) | Architecture decision records, `ADR-000` … `ADR-007` | You want to know **why** something is the way it is — before proposing to change it |
| [`specs/`](specs/) | What a feature is meant to do, before it exists | You are implementing or reviewing a feature |
| [`ops/`](ops/) | Runbooks, rollout/rollback, setup, and the human steps. [`tech-feed-rollout-gates.md`](ops/tech-feed-rollout-gates.md) is the one to read before widening the flag — every gate names the command that produces its number | Something is on fire, or you are deploying |
| [`security/`](security/) | Threat model, dataflow, audit | You are touching untrusted input, credentials, or the browser fleet |
| [`qa/`](qa/) | Acceptance criteria | You are deciding whether something is done |
| [`coverage/`](coverage/) | **The measured numbers**, and the definitions behind them | You want to quote a figure — never quote one from anywhere else |
| [`sourcing/`](sourcing/) | Catalog-expansion findings, validated but not yet pasted in | You are adding employers or boards |
| [`product/`](product/) | Product positions that constrain the code | You are changing generated output or user-visible copy |
| [`plans/`](plans/) | Dated implementation plans | You want the shape of a past piece of work |
| [`compass/`](compass/) | Where the deployed product sits against the north-star spec | You are planning what to build next |

## decisions/ — the ADRs

`ADR-000` predates the numbering and keeps its `000` because it was written
before `ADR-001`, not after `ADR-007`.

| ADR | Decision |
| --- | --- |
| [000](decisions/ADR-000-guest-discovery-freshness-and-fit.md) | Guest discovery, freshness, fit scoring and identity |
| [001](decisions/ADR-001-evolve-jobpilot-not-greenfield-compass.md) | Evolve JobPilot rather than greenfield Compass |
| [002](decisions/ADR-002-voicegraph-on-mongo-flask.md) | Voice Graph on Mongo + Flask |
| [003](decisions/ADR-003-microservices-and-bounded-contexts.md) | Microservices and bounded contexts |
| [004](decisions/ADR-004-universal-employer-coverage.md) | Universal employer coverage |
| [005](decisions/ADR-005-tech-occupation-taxonomy-and-unified-search.md) | The technology occupation taxonomy and unified search |
| [006](decisions/ADR-006-canonical-employer-identity-and-owner-verified-coverage.md) | Canonical employer identity and **owner-verified** coverage |
| [007](decisions/ADR-007-auto-apply-and-managed-mailbox-security.md) | Auto-apply + managed-mailbox security architecture |

## coverage/ — living numbers, and the snapshots they came from

The distinction matters more here than anywhere else in this tree, because
these are the documents people quote.

**Living** — regenerate before quoting:

| Document | What it measures |
| --- | --- |
| [`tech-employer-coverage.md`](coverage/tech-employer-coverage.md) | **Generated.** `python scripts/employer_coverage_report.py --markdown`, stamped with the source revision, inventory watermark and every version that produced it. `--persist` also stores it in `coverage_snapshots`, which is what the product's health banner reads — so there is ONE set of coverage numbers and every surface can see how old it is |
| [`employer-coverage-definition.md`](coverage/employer-coverage-definition.md) | What "covered" means, and why an endpoint that merely answers does not count |
| [`fit-score-v21-distribution.md`](coverage/fit-score-v21-distribution.md) | What each scoring change did to the live distribution, measured per account |
| [`ats-vendor-coverage.md`](coverage/ats-vendor-coverage.md) · [`source-provider-priority.md`](coverage/source-provider-priority.md) · [`state-registry-coverage.md`](coverage/state-registry-coverage.md) · [`tech-employers-and-their-career-pages.md`](coverage/tech-employers-and-their-career-pages.md) | Vendor, provider, registry and employer breakdowns |

> **Four of the documents in `coverage/` now open with a HISTORICAL banner**
> (`ats-vendor-coverage.md`, `source-provider-priority.md`,
> `state-registry-coverage.md`, `tech-employers-and-their-career-pages.md`).
> They were true on their dates and are not current truth; each banner names
> the live source that replaced it. Provider readiness in particular is no
> longer a document at all — `ats_providers.registry.readiness()` derives it
> from the registered implementations, because the written list named two
> vendors we had already built.

**[`coverage/reports/`](coverage/reports/)** — dated one-off snapshots and the
raw probe output behind them (`.csv`, `.tsv`, `.txt`, `.log`). These were true
on their date and are **not** current truth. The acceptance record for the
2026-09-02 milestone lives here:
[`2026-09-02-acceptance.md`](coverage/reports/2026-09-02-acceptance.md), and
the record for the canonical-employer, coverage, taxonomy and scorer work of
the same day is
[`2026-09-02-canonical-employer-and-scorer-acceptance.md`](coverage/reports/2026-09-02-canonical-employer-and-scorer-acceptance.md)
— which prints the numbers where a target was NOT reached rather than the
target.
