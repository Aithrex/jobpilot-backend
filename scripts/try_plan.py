"""Generate a learning plan straight from the terminal — no server, no auth, no quota.

Calls learning_service.generate_plan() directly so you can iterate on the prompt
and PLAN_SCHEMA without clicking through the UI, waiting on the async job, or
burning your daily entitlement. Nothing is written to MongoDB.

    python scripts/try_plan.py "Kubernetes"
    python scripts/try_plan.py "System design" --role "Backend Engineer" --weeks 4
    python scripts/try_plan.py "React" --skills "JavaScript,HTML,CSS" --capstone
    python scripts/try_plan.py "Rust" --json > /tmp/plan.json

Edit the prompt + PLAN_SCHEMA in shared/python/services/learning_service.py,
re-run this, read the output. That's the loop.
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def trace_llm_calls() -> None:
    """Print the exact prompt sent to Claude and the raw dict it returned.

    Wraps the `claude_json` reference that learning_service imported, so the
    service code stays untouched. Also turns on provider DEBUG logging, which
    is where retries, JSON-repair rounds, and schema coercion show up.
    """
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s:%(name)s:%(message)s")
    for noisy in ("botocore", "boto3", "urllib3", "pymongo"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    import services.learning_service as ls

    original = ls.claude_json

    def traced(**kwargs):
        print(f"\n{'#' * 72}\n# PROMPT SENT TO CLAUDE\n{'#' * 72}")
        print(f"# model={kwargs.get('model')}  max_tokens={kwargs.get('max_tokens')}  "
              f"temperature={kwargs.get('temperature')}")
        if kwargs.get("system"):
            print(f"\n--- system ---\n{kwargs['system']}")
        print(f"\n--- user ---\n{kwargs.get('prompt')}")
        print(f"\n{'#' * 72}\n# SCHEMA REQUESTED\n{'#' * 72}")
        print(json.dumps(kwargs.get("schema"), indent=2, default=lambda o: getattr(o, "__name__", str(o))))

        started = time.time()
        result = original(**kwargs)
        print(f"\n{'#' * 72}\n# RAW RESPONSE ({time.time() - started:.1f}s)\n{'#' * 72}")
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"{'#' * 72}\n")
        return result

    ls.claude_json = traced


def render_sprint(sprint: dict) -> None:
    """Human-readable view of a day-by-day Jira-ticket sprint."""
    print(f"\n{'=' * 72}")
    print(f"  {sprint.get('title') or '(no title)'}")
    print(f"{'=' * 72}")
    if sprint.get("summary"):
        print(f"\n{sprint['summary']}\n")

    skills = sprint.get("target_skills") or []
    if skills:
        print(f"Target skills: {', '.join(skills)}\n")

    for d in sprint.get("days") or []:
        print(f"{'-' * 72}")
        print(f"DAY {d.get('day', '?')} — {d.get('title', '(untitled)')}")
        print(f"{'-' * 72}")
        if d.get("business_context"):
            print(f"  WHY: {d['business_context']}\n")
        if d.get("task_instructions"):
            print("  TASKS:")
            for t in d["task_instructions"]:
                print(f"    • {t}")
        if d.get("acceptance_criteria"):
            print("  DONE WHEN:")
            for a in d["acceptance_criteria"]:
                print(f"    ✓ {a}")
        if d.get("required_proof_instructions"):
            print(f"  PROOF REQUIRED: {d['required_proof_instructions']}")
        print()


def render(plan: dict) -> None:
    """Human-readable view of the generated plan."""
    print(f"\n{'=' * 72}")
    print(f"  {plan.get('title') or '(no title)'}")
    print(f"{'=' * 72}")
    if plan.get("summary"):
        print(f"\n{plan['summary']}\n")

    skills = plan.get("target_skills") or []
    if skills:
        print(f"Target skills: {', '.join(skills)}\n")

    modules = plan.get("modules") or []
    for m in modules:
        print(f"{'-' * 72}")
        print(f"WEEK {m.get('week', '?')} — {m.get('title', '(untitled)')}")
        print(f"{'-' * 72}")
        if m.get("objective"):
            print(f"  Objective    : {m['objective']}")
        if m.get("inferred_skill"):
            print(f"  Proves skill : {m['inferred_skill']}")
        for c in m.get("concepts") or []:
            print(f"    • {c}")
        if m.get("practice_task"):
            print(f"  Practice     : {m['practice_task']}")
        if m.get("checkpoint"):
            print(f"  Checkpoint   : {m['checkpoint']}")
        for q in m.get("resource_queries") or []:
            print(f"  Search       : {q}")
        print()

    capstone = plan.get("capstone")
    if capstone:
        print(f"{'=' * 72}")
        print(f"CAPSTONE — {capstone.get('title', '')}")
        print(f"{'=' * 72}")
        print(f"{capstone.get('description', '')}\n")

    # Surface schema drift fast: a field you added to PLAN_SCHEMA but the model
    # ignored (or vice versa) is the usual reason a plan "looks fine" but the UI
    # renders blanks.
    known = {"week", "title", "objective", "concepts", "practice_task",
             "resource_queries", "checkpoint", "inferred_skill"}
    if modules:
        seen = set().union(*(set(m.keys()) for m in modules))
        extra = seen - known
        missing = known - seen
        if extra:
            print(f"NOTE: modules returned unexpected fields: {sorted(extra)}")
        if missing:
            print(f"NOTE: modules missing expected fields: {sorted(missing)}")


def run_sprint(args) -> int:
    """--sprint mode: day-by-day Jira tickets with proof-of-work demands."""
    try:
        from services.learning_service import generate_sprint, TYPESCRIPT_SPRINT
    except Exception as e:
        print(f"❌ Could not import learning_service: {type(e).__name__}: {e}")
        return 1

    if args.topic.strip().lower() in ("typescript", "ts"):
        cfg = dict(TYPESCRIPT_SPRINT)
    else:
        # No authored progression for this skill yet — let the model design one.
        cfg = {
            "skill": args.topic,
            "role": "Software Engineer",
            "baseline": args.skills or "Comfortable programming; new to this skill.",
            "progression": ("Design your own three-phase progression: core foundations → "
                            "real-world application → advanced/production hardening."),
        }
    if args.role:
        cfg["role"] = args.role

    print(f"Generating {args.days}-day sprint: skill={cfg['skill']!r} role={cfg['role']!r}")
    print("Calling Claude (PRO)… a 10-day sprint usually takes 60-120s.\n")

    started = time.time()
    try:
        sprint = generate_sprint(**cfg, days=args.days)
    except Exception as e:
        print(f"❌ Generation failed: {type(e).__name__}: {str(e)[:400]}")
        print("→ If this is a credentials/model error, run: python scripts/check_bedrock.py")
        return 1
    elapsed = time.time() - started

    days = sprint.get("days") or []
    if not days:
        print(f"⚠️  Model returned no days after {elapsed:.1f}s. Raw response:")
        print(json.dumps(sprint, indent=2, default=str))
        print("\n→ Usually means the prompt and SPRINT_SCHEMA disagree. Check both.")
        return 1

    if args.json:
        print(json.dumps(sprint, indent=2, ensure_ascii=False, default=str))
    else:
        render_sprint(sprint)

    print(f"Generated {len(days)} of {args.days} requested days in {elapsed:.1f}s.")
    if len(days) < args.days:
        print("⚠️  Fewer days than requested — likely hit max_tokens. Raise it in generate_sprint().")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate a learning plan locally (no DB write, no quota).")
    ap.add_argument("topic", help='What the learner wants to master, e.g. "Kubernetes"')
    ap.add_argument("--role", default=None, help="Target role to tailor depth/examples to")
    ap.add_argument("--weeks", type=int, default=6, help="Number of weekly modules (default 6)")
    ap.add_argument("--skills", default="", help="Comma-separated skills they already have")
    ap.add_argument("--capstone", action="store_true", help="Ask for a capstone project")
    ap.add_argument("--json", action="store_true", help="Print raw JSON instead of the readable view")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Show the prompt sent, the schema requested, the raw response, and provider debug logs")
    ap.add_argument("--sprint", action="store_true",
                    help="Generate a day-by-day Jira-ticket sprint instead of weekly study modules")
    ap.add_argument("--days", type=int, default=10, help="Number of sprint days (--sprint only, default 10)")
    args = ap.parse_args()

    if args.verbose:
        trace_llm_calls()

    current_skills = [s.strip() for s in args.skills.split(",") if s.strip()]

    if args.sprint:
        return run_sprint(args)

    print(f"Generating: topic={args.topic!r} role={args.role!r} "
          f"weeks={args.weeks} capstone={args.capstone}")
    if current_skills:
        print(f"Existing skills: {', '.join(current_skills)}")
    print("Calling Claude (PRO)… this usually takes 20-40s.\n")

    try:
        from services.learning_service import generate_plan
    except Exception as e:
        print(f"❌ Could not import learning_service: {type(e).__name__}: {e}")
        return 1

    started = time.time()
    try:
        plan = generate_plan(
            topic=args.topic,
            target_role=args.role,
            current_skills=current_skills,
            weeks=args.weeks,
            include_capstone=args.capstone,
        )
    except Exception as e:
        print(f"❌ Generation failed: {type(e).__name__}: {str(e)[:400]}")
        print("→ If this is a credentials/model error, run: python scripts/check_bedrock.py")
        return 1

    elapsed = time.time() - started

    if not plan or not plan.get("modules"):
        print(f"⚠️  Model returned no modules after {elapsed:.1f}s. Raw response:")
        print(json.dumps(plan, indent=2, default=str))
        print("\n→ Usually means the prompt and PLAN_SCHEMA disagree. Check both.")
        return 1

    if args.json:
        print(json.dumps(plan, indent=2, ensure_ascii=False, default=str))
    else:
        render(plan)

    print(f"Generated {len(plan['modules'])} modules in {elapsed:.1f}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
