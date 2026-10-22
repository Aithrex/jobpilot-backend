"""Hold-out evaluation for the contextual fraud indicators (trust-1.0.0).

WRITTEN BEFORE THE RULES WERE TUNED, and deliberately NOT copied from the unit
tests in `tests/test_url_trust_and_fraud_context.py`. Every row is a single
sentence a reviewer labelled by hand with the warning code a careful human
would raise, or `None` when the sentence must NOT warn:

  * `payment_request`     — the applicant is asked to pay / buy / deposit money
                             (fees, equipment, training, gift cards, crypto,
                             check-deposit schemes) before or as a condition of
                             being hired.
  * `credential_request`  — the applicant is asked for bank / account
                             credentials or an SSN before any offer.
  * `offplatform_contact` — the applicant is INSTRUCTED to move the hiring
                             conversation to a chat app.

`None` rows are the hard negatives the old phrase matcher got wrong: anti-scam
disclaimers, quoted warnings, job duties (fraud analysts, payments engineers),
product names (WhatsApp, Signal, Telegram as products), and the facts that must
never be fraud on their own (W2-only, personal email, old posting, post-offer
I-9 / direct deposit).

Run by hand to read the misses:

    PYTHONPATH=shared/python python3 evals/eval_trust_context.py

The gates and the measured numbers live in `evals/test_trust_context_eval.py`.
`HOLDOUT_B` is the clean generalisation figure (precision 1.0, recall 0.44 on
first and only run); `HOLDOUT` was used once to fix a general logic error and
is no longer clean. Misses are printed, not hidden. Do not add a rule whose
only purpose is to flip one of these rows.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "shared" / "python") not in sys.path:
    sys.path.insert(0, str(ROOT / "shared" / "python"))

# (title, sentence, expected code or None). Title is "" unless the title is
# part of what makes the row hard.
HOLDOUT: List[Tuple[str, str, Optional[str]]] = [
    # --- genuine demands: money -------------------------------------------
    ("", "Candidates must pay a $150 onboarding fee before their first shift.", "payment_request"),
    ("", "Please send $99 via Zelle to secure your interview slot.", "payment_request"),
    ("", "A refundable deposit of $300 is required for the laptop we ship you.", "payment_request"),
    ("", "You will purchase your home-office equipment from our approved vendor "
         "and we will reimburse you after your first paycheck.", "payment_request"),
    ("", "Buy three Google Play gift cards and send us the codes for your training materials.",
     "payment_request"),
    ("", "Payment for the certification course is deducted from your first salary, "
         "or you can pay upfront in Bitcoin.", "payment_request"),
    ("", "Applicants must cover the cost of their background check ($45), payable "
         "before the interview.", "payment_request"),
    ("", "Kindly make payment of the visa processing charges to our agent to confirm "
         "your offer letter.", "payment_request"),
    ("", "You'll be reimbursed for the equipment you buy, after you deposit the check "
         "we mail you.", "payment_request"),
    # --- genuine demands: credentials / identity before an offer ----------
    ("", "To complete onboarding, provide your bank login credentials so payroll can "
         "verify the account.", "credential_request"),
    ("", "Send a photo of your SSN card and driver's license with your application.",
     "credential_request"),
    ("", "Before we can proceed, kindly forward your online banking username and password.",
     "credential_request"),
    ("", "Provide your Social Security Number in the form below to be considered.",
     "credential_request"),
    # --- genuine instructions to move off-platform ------------------------
    ("", "Contact our hiring manager on Telegram @hr_desk to schedule your interview.",
     "offplatform_contact"),
    ("", "Text 'JOB' to our WhatsApp number +1 555 0100 to receive the next steps.",
     "offplatform_contact"),
    ("", "Interviews are conducted exclusively via Google Hangouts chat with our HR manager.",
     "offplatform_contact"),
    # --- disclaimers and quoted warnings: must NOT warn -------------------
    ("", "We will never ask for payment, gift cards or banking information during the "
         "hiring process.", None),
    ("", "Acme does not charge candidates any fee at any stage of recruitment.", None),
    ("", "Beware of fraudsters who ask you to pay for training; report them to "
         "security@acme.com.", None),
    ("", "Note: legitimate recruiters will never request your Social Security number "
         "before an offer is made.", None),
    ("", "\"Pay a registration fee to apply\" is a common scam line - we never use it.", None),
    # --- job duties: must NOT warn ----------------------------------------
    ("Fraud Analyst", "Investigate payment fraud cases, including gift card and wire "
                      "transfer scams targeting our customers.", None),
    ("Payments Engineer", "Build reconciliation services that move funds between bank "
                          "accounts for our merchants.", None),
    ("Customer Support Specialist", "Collect routing numbers and account numbers from "
                                    "customers during ACH enrollment calls.", None),
    ("", "Our security team trains employees to spot phishing emails that ask for passwords.",
     None),
    ("Senior Software Engineer", "Help us build the Payments platform: card issuing, wire "
                                 "transfers and crypto custody.", None),
    # --- product names: must NOT warn -------------------------------------
    ("Software Engineer, WhatsApp", "Design end-to-end encrypted messaging features used by "
                                    "billions on WhatsApp.", None),
    ("Android Engineer", "Join the Signal Messenger team to improve the Android client.", None),
    ("Backend Engineer", "Experience with Telegram Bot API or similar messaging platforms is "
                         "a plus.", None),
    ("Integration Engineer", "WhatsApp Business API integration experience preferred.", None),
    # --- never fraud on their own -----------------------------------------
    ("", "This is a W2-only contract role; no C2C.", None),
    ("", "Send your resume to jane.recruiter@gmail.com.", None),
    ("", "Posted 45 days ago; we are still accepting applications.", None),
    ("", "The successful candidate will be asked to complete an I-9 and provide identity "
         "documents after an offer is accepted.", None),
    ("", "As part of benefits enrollment after hire, you'll provide your direct deposit "
         "details.", None),
    ("", "We are an equal opportunity employer and never discriminate.", None),
]


# Second batch, written AFTER the first measurement and run ONCE before any
# further change, because fixing the one miss in HOLDOUT (the "refundable
# deposit ... for the laptop we ship you" row) made HOLDOUT's own numbers no
# longer a clean held-out measurement. Report HOLDOUT_B separately.
HOLDOUT_B: List[Tuple[str, str, Optional[str]]] = [
    ("", "New associates are responsible for a one-time $75 uniform charge, payable on "
         "acceptance of this offer.", "payment_request"),
    ("", "Wire the onboarding deposit to the account in the attached PDF within 24 hours.",
     "payment_request"),
    ("", "To unlock your training portal, purchase the $129 software license from our "
         "partner store.", "payment_request"),
    ("", "Please reply with your full card number and expiry date for identity checks.",
     "credential_request"),
    ("", "Our onboarding agent will need your mother's maiden name, date of birth and SSN "
         "before the interview.", "credential_request"),
    ("", "Download Telegram and message @talent_team_hr with the code JP-42 to continue.",
     "offplatform_contact"),
    ("", "For faster processing, all interviews happen over WhatsApp text chat.",
     "offplatform_contact"),
    ("", "Your job is to receive customer payments into your personal account and forward "
         "them by wire.", "money_mule"),
    ("", "No interview needed - start earning today!", "too_good_to_be_true"),
    ("", "We never charge job seekers - not for applications, not for training.", None),
    ("", "Heads up: anyone requesting gift cards on our behalf is not affiliated with us.",
     None),
    ("Risk Analyst", "Analyze chargeback patterns and help customers recover funds lost to "
                     "fake recruiters.", None),
    ("Staff Engineer, Payments", "You will own the ledger that tracks card payments and "
                                 "bank transfers across 40 countries.", None),
    ("Product Manager, Messaging", "Drive the roadmap for our WhatsApp and Telegram "
                                   "channel integrations.", None),
    ("", "Relocation assistance and a home-office stipend are provided.", None),
    ("", "Hourly pay: $28-$34 depending on experience.", None),
    ("", "Questions? Email careers.team@outlook.com.", None),
    ("", "You'll complete tax and direct deposit forms on your first day.", None),
    ("Security Engineer", "Harden our SSO so employees are never prompted for passwords "
                          "outside the identity provider.", None),
    ("", "Must be able to pass a background check.", None),
]


def run(verbose: bool = True, rows: Optional[List[Tuple[str, str, Optional[str]]]] = None
        ) -> Dict[str, Any]:
    from services.job_legitimacy_service import assess_posting_trust

    tp = fp = fn = tn = 0
    code_correct = 0
    misses: List[str] = []
    false_alarms: List[str] = []
    for title, sentence, expected in (rows if rows is not None else HOLDOUT):
        trust = assess_posting_trust({"title_raw": title or "Engineer",
                                      "employer_name": "Example Co",
                                      "description_text": sentence})
        codes = [w["code"] for w in trust["warnings"]]
        predicted = bool(codes)
        if expected is None:
            if predicted:
                fp += 1
                false_alarms.append(f"{codes} <- {sentence}")
            else:
                tn += 1
        else:
            if predicted:
                tp += 1
                if expected in codes:
                    code_correct += 1
                else:
                    misses.append(f"wrong code {codes} (want {expected}) <- {sentence}")
            else:
                fn += 1
                misses.append(f"missed {expected} <- {sentence}")
    positives = tp + fn
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / positives if positives else 1.0
    out = {
        "rows": len(rows if rows is not None else HOLDOUT), "positives": positives, "negatives": fp + tn,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(precision, 3), "recall": round(recall, 3),
        "code_accuracy_on_hits": round(code_correct / tp, 3) if tp else 1.0,
        "misses": misses, "false_alarms": false_alarms,
    }
    if verbose:
        print(f"rows={out['rows']} positives={positives} negatives={out['negatives']}")
        print(f"precision={out['precision']} recall={out['recall']} "
              f"code_accuracy_on_hits={out['code_accuracy_on_hits']}")
        for m in misses:
            print("  MISS  ", m)
        for f in false_alarms:
            print("  FALSE ", f)
    return out


if __name__ == "__main__":
    print("== HOLDOUT (36 rows) ==")
    run()
    print("== HOLDOUT_B (20 rows, written after the first measurement) ==")
    run(rows=HOLDOUT_B)
