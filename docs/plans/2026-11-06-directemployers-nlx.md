# NLX public employer-board reader plan

The Stantec public board returned 650 US location rows containing 300 distinct
requisitions on 2026-11-05 UTC. This meets the user-defined new-reader gate.

Use a registered generic NLX reader with named employer configuration, the
published public JSON transport, exact employer/country boundaries, and robots
checks. Merge location rows by requisition ID and flag every incomplete census.
Keep dates from the field the employer's JobPosting schema uses (`date_added`).

- [x] Add trimmed real fixtures and contract tests before implementation.
- [x] Implement and register the public reader; review scope and completeness.
- [x] Validate the proven Stantec source and add its seed/plan/probe rows.
- [x] Regenerate onboarding docs from recorded validation and UTC evidence.
- [x] Run focused checks and the full Linux suite; report exact results.

The provider module is delegated to the under-reads agent. Fixtures, tests,
registry, catalog and docs belong to the provider-value agent. Other agents are
working in separate worktrees. No dotenv, MongoDB, production, write flags,
browser runtime, guessed endpoints, commits or PRs are used in this subtask.

Focused provider/integration checks:162passed. The exact full command was invoked with nativePython3.12; it is stillrunning and has reported at leastonefailure. This checkbox records invocation, not a passingfull result. LinuxCI mustpass beforemerge.
