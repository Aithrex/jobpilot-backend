"""Phase 1 — ATS question-frequency histogram (recon ONLY, never submits).

Greenhouse publishes each job's full application question set at
``boards-api.greenhouse.io/v1/boards/{tenant}/jobs/{id}?questions=true`` with NO
auth. This harvests that across many tenants and maps every question through
``application_question_bank.normalize_field(allow_llm=False)`` so the output is:

  * which canonical ``field_id`` a real employer actually asks, and how often
  * the share of questions the deterministic catalog CANNOT classify (``unknown``)

That second number is the onboarding gap: every ``unknown`` is a question a user
must be interrupted for. Read-only; no application is ever created or submitted.

Usage:  PYTHONPATH=shared/python python3 scripts/question_histogram.py [n_jobs]
"""
import json, os, random, sys, time, urllib.request, urllib.error
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared", "python"))

ENV = os.path.join(os.path.dirname(__file__), "..", ".env")
for line in open(ENV, encoding="utf-8-sig"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from pymongo import MongoClient
from services.application_question_bank import normalize_field, FIELD_INDEX

N_JOBS = int(sys.argv[1]) if len(sys.argv) > 1 else 250
PER_TENANT = 2
DELAY = 0.25
UA = "JobPilot-recon/1.0 (+careers research; contact: sandeep@gnanalytica.com)"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_http": e.code}
    except Exception as e:
        return {"_err": type(e).__name__}


def main():
    db = MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=15000)[
        os.environ.get("DB_NAME", "aspirely_db")]
    rows = list(db.job_postings.find(
        {"ats": "greenhouse", "status": {"$ne": "closed"}},
        {"_id": 0, "tenant": 1, "source_job_id": 1, "employer_name": 1}))
    by_tenant = defaultdict(list)
    for r in rows:
        jid = str(r.get("source_job_id") or "").rsplit(":", 1)[-1]
        if r.get("tenant") and jid.isdigit():
            by_tenant[r["tenant"]].append(jid)
    tenants = sorted(by_tenant)
    random.Random(42).shuffle(tenants)
    targets = []
    for t in tenants:
        for jid in by_tenant[t][:PER_TENANT]:
            targets.append((t, jid))
        if len(targets) >= N_JOBS:
            break
    targets = targets[:N_JOBS]
    print(f"greenhouse tenants={len(tenants)} sampling {len(targets)} jobs\n")

    field_ct, unknown_ct = Counter(), Counter()
    req_ct = Counter()
    ok = miss = 0
    q_total = 0
    per_job_unknown = []

    for i, (tenant, jid) in enumerate(targets, 1):
        d = fetch(f"https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs/{jid}?questions=true")
        time.sleep(DELAY)
        qs = (d or {}).get("questions")
        if not isinstance(qs, list):
            miss += 1
            continue
        ok += 1
        job_unknown = 0
        for q in qs:
            label = (q.get("label") or "").strip()
            if not label:
                continue
            q_total += 1
            required = bool(q.get("required"))
            fields = q.get("fields") or [{}]
            opts = []
            for f in fields:
                for v in (f.get("values") or []):
                    lbl = v.get("label")
                    if lbl:
                        opts.append(str(lbl))
            res = normalize_field(label, options=opts or None, allow_llm=False)
            fid = res.get("field_id") or "unknown"
            field_ct[fid] += 1
            if required:
                req_ct[fid] += 1
            if fid == "unknown":
                unknown_ct[label.lower()[:90]] += 1
                job_unknown += 1
        per_job_unknown.append(job_unknown)
        if i % 25 == 0:
            print(f"  ...{i}/{len(targets)} boards_ok={ok} questions={q_total}")

    print(f"\n=== SAMPLE ===\njobs_ok={ok} jobs_missing_questions={miss} questions_seen={q_total}")
    if not q_total:
        return
    unk = field_ct.get("unknown", 0)
    print(f"classified={q_total-unk} ({100*(q_total-unk)/q_total:.1f}%)  "
          f"UNKNOWN={unk} ({100*unk/q_total:.1f}%)")
    if per_job_unknown:
        clean = sum(1 for x in per_job_unknown if x == 0)
        print(f"jobs with ZERO unknown questions: {clean}/{len(per_job_unknown)} "
              f"({100*clean/len(per_job_unknown):.1f}%)  <-- zero-touch-eligible share")

    print("\n=== TOP CANONICAL FIELDS ASKED ===")
    print(f"{'field_id':38} {'asked':>6} {'req':>6}  category/sensitivity")
    for fid, n in field_ct.most_common(40):
        f = FIELD_INDEX.get(fid)
        meta = f"{f['category']}/{f['sensitivity']}" if f else "-"
        print(f"  {fid:36} {n:>6} {req_ct.get(fid,0):>6}  {meta}")

    print("\n=== TOP UNCLASSIFIED QUESTION TEXTS (the onboarding gap) ===")
    for label, n in unknown_ct.most_common(45):
        print(f"  {n:>4}  {label}")

    out = os.path.join(os.path.dirname(__file__), "..", "question_histogram.json")
    json.dump({"jobs_ok": ok, "questions": q_total,
               "fields": field_ct.most_common(), "required": req_ct.most_common(),
               "unknown": unknown_ct.most_common(300)},
              open(out, "w", encoding="utf-8"), indent=2)
    print(f"\nwrote {os.path.abspath(out)}")


if __name__ == "__main__":
    main()
