"""A labelled mailbox for the Gmail scan.

Every email here is written the way a real one arrives: the default wording of
the hiring systems people actually get mail from (Greenhouse, Workday, Lever,
Ashby, iCIMS, SmartRecruiters, LinkedIn, Indeed), recruiters writing by hand,
and the look-alikes that share an inbox with them (order receipts, security
alerts, newsletters and job alerts from a company the person also applied to).

Employers are invented. Nothing here is copied from a real person's mail.

Each email says what the scan must do with it:

  auto       move exactly this application to this status, by itself
  review     ask the user; the application and the proposed status are given
  unmatched  keep for review as "no application in the tracker matches"
  ignore     write nothing at all
  not        whatever happens, never propose or apply this status

`evals/test_gmail_scan_corpus.py` runs each one through the real pipeline:
Gmail's JSON in, tracker and review queue out.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# record_id -> (company, role title, status the tracker starts with)
TRACKER: Dict[str, Tuple[str, str, str]] = {
    "acme-mle": ("Acme", "Machine Learning Engineer", "applied"),
    "globex-qa": ("Globex", "QA Engineer", "applied"),
    "initech-devops": ("Initech", "DevOps Engineer", "applied"),
    "hooli-frontend": ("Hooli", "Frontend Engineer", "applied"),
    "umbrella-analyst": ("Umbrella Corporation", "Data Analyst", "applied"),
    "vandelay-backend": ("Vandelay Industries", "Backend Engineer", "applied"),
    "wonka-supply": ("Wonka", "Supply Chain Analyst", "applied"),
    "stark-systems": ("Stark Industries", "Systems Engineer", "applied"),
    "soylent-data": ("Soylent", "Data Engineer", "interviewing"),
    "piedpiper-backend": ("Pied Piper", "Backend Engineer", "saved"),
    "massive-swe": ("Massive Dynamic", "Software Engineer", "applied"),
    "massive-senior": ("Massive Dynamic", "Senior Software Engineer", "applied"),
    # Employers that are also a shop, an account and a bank in the same inbox.
    "brightmart-analyst": ("Brightmart", "Data Analyst", "applied"),
    "nimbus-scientist": ("Nimbus", "Data Scientist", "applied"),
    "harbor-analyst": ("Harbor Bank", "Business Analyst", "applied"),
}

ME = "priya.sharma@gmail.test"


def mail(mid: str, sender: str, subject: str, body: str, *, expect: Tuple[Any, ...],
         html: bool = False, charset: str = "utf-8", labels: Optional[List[str]] = None,
         plain_stub: str = "", when: int = 0, note: str = "") -> Dict[str, Any]:
    return {"id": mid, "sender": sender, "subject": subject, "body": body, "expect": expect,
            "html": html, "charset": charset, "labels": labels or ["INBOX"],
            "plain_stub": plain_stub, "when": when, "note": note}


_CSS = "<style>" + ".wrapper{margin:0 auto;padding:24px;font-family:Arial,Helvetica,sans-serif;}" * 260 + "</style>"


def _page(*paragraphs: str, quoted: str = "") -> str:
    """An HTML-only email the way a hiring system sends it: a stylesheet longer
    than the message, invisible preview padding, then the paragraphs."""
    padding = "&zwnj;&nbsp;" * 80
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return (f"<html><head><title>Notification</title>{_CSS}</head><body>"
            f"<div style=\"display:none\">{padding}</div><table><tr><td>{body}</td></tr></table>"
            f"{quoted}</body></html>")


EMAILS: List[Dict[str, Any]] = [
    # ── Clear declines: the application and the decision are both stated ────
    mail("R1", "Acme Recruiting <no-reply@us.greenhouse-mail.io>", "Your application to Acme",
         "Hi Priya,\n\nThank you for your interest in the Machine Learning Engineer role at Acme. "
         "After careful consideration, we have decided to move forward with other candidates whose "
         "experience more closely matches our needs at this time.\n\nWe will keep your resume on file "
         "and wish you the best in your job search.\n\nAcme Recruiting",
         expect=("auto", "acme-mle", "rejected"), note="Greenhouse default decline"),
    mail("R2", "Globex <globex@myworkday.com>", "Update on your application for QA Engineer",
         _page("Dear Priya Sharma,",
               "Thank you for taking the time to apply for the QA Engineer position.",
               "We regret to inform you that you have not been selected for this position.",
               "We encourage you to visit our careers site for future openings."),
         html=True, expect=("auto", "globex-qa", "rejected"),
         note="Workday: HTML only, employer named only by the sender"),
    mail("R3", "Initech Hiring Team <no-reply@hire.lever.co>", "Initech - DevOps Engineer",
         "Hi Priya,\n\nThanks so much for your interest in Initech and for applying to the DevOps "
         "Engineer role. Unfortunately, we won't be moving forward with your application at this "
         "time.\n\nBest,\nInitech Hiring Team",
         expect=("auto", "initech-devops", "rejected"), note="Lever default decline"),
    mail("R4", "Hooli Recruiting <no-reply@ashbyhq.com>", "Hooli application update",
         "Hi Priya,\n\nThank you for applying to the Frontend Engineer position at Hooli. After "
         "reviewing your application, we will not be moving forward at this time.\n\nHooli Recruiting",
         expect=("auto", "hooli-frontend", "rejected"), note="Ashby default decline"),
    mail("R5", "Umbrella Talent Acquisition <noreply@talent.icims.com>",
         "Thank you for your interest in Umbrella Corporation",
         "Dear Priya,\n\nThank you for your interest in the Data Analyst position (Req ID: 10234) "
         "with Umbrella Corporation. We have reviewed your qualifications and have determined that "
         "other candidates are better suited for this position.\n\nUmbrella Talent Acquisition",
         expect=("review", "umbrella-analyst", "rejected"),
         note="iCIMS: names a requisition number the tracker cannot confirm, so it asks"),
    mail("R6", "LinkedIn <jobs-noreply@linkedin.com>",
         "Your application to Backend Engineer at Vandelay Industries",
         "Your update from Vandelay Industries.\n\nThank you for your interest in the Backend "
         "Engineer position at Vandelay Industries. Unfortunately, we will not be moving forward "
         "with your application, but we appreciate your time.\n\nSimilar jobs\nSenior Engineer at "
         "OtherCo\nApply now\n\nUnsubscribe · Privacy Policy",
         expect=("auto", "vandelay-backend", "rejected"), note="LinkedIn, with other jobs listed under it"),
    mail("R7", "Wonka <notification@smartrecruiters.com>", "Your application to Wonka",
         "Dear Priya,\n\nThank you for applying to the Supply Chain Analyst position at Wonka. We\n"
         "appreciate the time you invested. After careful review, we have decided to\nmove forward "
         "with other candidates for this role.\n\nKind regards,\nWonka Talent Team",
         expect=("auto", "wonka-supply", "rejected"), note="plain text, wrapped mid-sentence"),
    mail("R8", "Stark Industries Careers <careers@starkindustries.test>", "Systems Engineer application",
         "Hello Priya,\n\nThank you for your application for the Systems Engineer role at Stark "
         "Industries. We wanted to let you know that this position has been filled.\n\nStark Careers",
         expect=("auto", "stark-systems", "rejected"), note="position filled"),
    mail("R9", "Acme Recruiting <recruiting@acme.test>", "Acme Machine Learning Engineer application update",
         _page("Hi Priya,",
               "Thank you for your interest in the Machine Learning Engineer role at Acme.",
               "We’ve decided to pursue other candidates for this role."),
         html=True, charset="cp1252", expect=("auto", "acme-mle", "rejected"),
         note="Windows-1252 bytes with a curly apostrophe"),
    mail("R10", "Maria Lopez <maria.lopez@globex.test>", "Re: Globex QA Engineer interview",
         "Hi Priya,\n\nThank you for speaking with us about the QA Engineer role at Globex. We have "
         "decided to move forward with another candidate.\n\nMaria\n\n"
         "On Tue, Nov 3, 2026 at 9:12 AM Maria Lopez <maria.lopez@globex.test> wrote:\n"
         "> We'd like to invite you to a phone screen. Are you available on Thursday?\n",
         expect=("auto", "globex-qa", "rejected"), note="a reply that quotes the earlier invitation"),
    mail("R11", "Initech Careers <careers@initech.test>", "Your application for DevOps Engineer at Initech",
         "Hi Priya,\n\nWe appreciate your interest in Initech. We will not be moving forward.\n\nInitech",
         expect=("auto", "initech-devops", "rejected"), note="the shortest honest decline"),
    mail("R12", "Priya Sharma <priya.other@outlook.test>", "Fwd: Your application to Wonka",
         "---------- Forwarded message ---------\nFrom: Wonka <notification@smartrecruiters.com>\n"
         "Date: Mon, Nov 2, 2026\nSubject: Your application to Wonka\nTo: priya.other@outlook.test\n\n"
         "Thank you for applying to the Supply Chain Analyst position at Wonka. We have decided to "
         "move forward with other candidates.",
         expect=("auto", "wonka-supply", "rejected"), note="forwarded from the person's other mailbox"),

    # ── Soft wording: a question for the user, never a silent change ────────
    mail("S1", "Dana Kim <dana.kim@soylent.test>", "Your Data Engineer interview at Soylent",
         "Hi Priya,\n\nUnfortunately, we need to reschedule your interview for the Data Engineer role "
         "at Soylent. Our interviewer is out sick. I will send a new time shortly.\n\nDana",
         expect=("not", "rejected"), note="'unfortunately' about scheduling"),
    mail("S2", "Massive Dynamic Careers <careers@massivedynamic.test>",
         "Massive Dynamic Senior Software Engineer application update",
         "Hi Priya,\n\nThank you for your interest in the Senior Software Engineer role at Massive "
         "Dynamic. Unfortunately, at this time our hiring plans have changed.\n\nMassive Dynamic",
         expect=("review", "massive-senior", "rejected"), note="one soft phrase, no decision stated"),
    mail("S3", "Hooli Recruiting <no-reply@ashbyhq.com>", "Hooli application update",
         "Hi Priya,\n\nRegarding the Frontend Engineer position at Hooli: we cannot move forward "
         "until we receive your work authorization documents. Please upload them in the portal.",
         expect=("not", "rejected"), note="'cannot move forward until' is a request"),

    # ── Receipts ────────────────────────────────────────────────────────────
    mail("A1", "Pied Piper Recruiting <no-reply@us.greenhouse-mail.io>", "Thank you for applying to Pied Piper",
         "Hi Priya,\n\nThank you for applying to the Backend Engineer role at Pied Piper. We have "
         "received your application and our team will review it. If your qualifications match, a "
         "recruiter will reach out to schedule a call. We will keep your resume on file for future "
         "openings.\n\nPied Piper Recruiting",
         expect=("auto", "piedpiper-backend", "applied"),
         note="receipt for a saved job: conditional call, resume kept on file"),
    mail("A2", "Globex <globex@myworkday.com>", "Thank you for applying to Globex",
         _page("Dear Priya,", "We have received your application for the QA Engineer position.",
               "Should you be selected for an interview, a member of our Talent team will contact you "
               "to schedule a call.",
               "Any offer of employment is contingent upon a successful background check."),
         html=True, expect=("ignore",), note="receipt for a job already marked applied"),
    mail("A3", "Indeed Apply <indeedapply@indeed.com>", "Indeed Application: Backend Engineer",
         "Application submitted\n\nBackend Engineer\nPied Piper\n\nThe following items were sent to "
         "Pied Piper. Good luck!\n\nResume",
         expect=("auto", "piedpiper-backend", "applied"), note="Indeed receipt"),
    mail("A4", "Pied Piper Recruiting <no-reply@us.greenhouse-mail.io>", "Thank you for applying to Pied Piper",
         "Hi Priya,\n\nThank you for applying to the Backend Engineer role at Pied Piper. The next "
         "step in our process is a review of your application by our recruiting team.",
         expect=("auto", "piedpiper-backend", "applied"), note="'next step in our process' is not progress"),

    # ── Interviews ──────────────────────────────────────────────────────────
    mail("I1", "Jane Park <jane.park@acme.test>", "Acme - Machine Learning Engineer interview",
         "Hi Priya,\n\nThanks for applying to the Machine Learning Engineer role at Acme. We'd like "
         "to invite you to a 30 minute phone screen. Please pick a time here: "
         "https://calendly.com/jane-park/30min\n\nJane",
         expect=("auto", "acme-mle", "interviewing"), note="recruiter with a scheduling link"),
    mail("I2", "Maria Lopez <maria.lopez@globex.test>",
         "Invitation: Globex QA Engineer interview @ Thu Nov 19, 2026 2pm - 2:45pm (PST) (priya.sharma@gmail.test)",
         "You have been invited to the following event.\n\nGlobex QA Engineer interview\nWhen: Thu "
         "Nov 19, 2026 2pm\nJoining info: meet.google.com/abc-defg-hij\n",
         expect=("auto", "globex-qa", "interviewing"), note="calendar invitation"),
    mail("I3", "Omar Haddad <omar.haddad@umbrella.test>", "Data Analyst position at Umbrella Corporation",
         "Hi Priya,\n\nRegarding the Data Analyst position at Umbrella Corporation: are you available "
         "on Tuesday or Wednesday for a 45 minute conversation with the hiring manager?\n\nOmar",
         expect=("auto", "umbrella-analyst", "interviewing"), note="availability ask"),
    mail("I4", "Hooli via HackerRank <support@hackerrank.com>", "Hooli Frontend Engineer coding assessment",
         "Hooli has invited you to complete the coding assessment for the Frontend Engineer position. "
         "Please complete the coding assessment within 5 days: https://www.hackerrank.com/test/abc123",
         expect=("auto", "hooli-frontend", "interviewing"), note="assessment invitation"),
    mail("I5", "Stark Industries Careers <careers@starkindustries.test>", "Systems Engineer at Stark Industries",
         "Hi Priya,\n\nWe are pleased to offer you the opportunity to interview for the Systems "
         "Engineer role at Stark Industries. Please share your availability for next week.",
         expect=("auto", "stark-systems", "interviewing"), note="'pleased to offer' an interview, not a job"),
    mail("I6", "Initech Events <events@initech.test>", "Join our engineering open house",
         "Join Initech engineers for a live tech talk about careers on our platform team. Register "
         "now: https://zoom.us/j/123456789",
         expect=("ignore",), note="event invitation with a video link"),
    mail("I7", "Globex Talent Community <talent@globex.test>", "Welcome to the Globex Talent Community",
         "Welcome to the team of people who hear about Globex roles first. We're excited to have you "
         "join our talent community. You will receive our newsletter with new openings.",
         expect=("ignore",), note="talent community welcome, not an offer"),

    # ── Offer and withdrawal ───────────────────────────────────────────────
    mail("O1", "Omar Haddad <omar.haddad@umbrella.test>", "Your offer from Umbrella Corporation",
         "Hi Priya,\n\nCongratulations! We are pleased to offer you the position of Data Analyst at "
         "Umbrella Corporation. Your offer letter is attached for your review.\n\nOmar",
         expect=("auto", "umbrella-analyst", "offer"), note="offer with letter"),
    mail("W1", "Globex <globex@myworkday.com>", "Your application for QA Engineer",
         "Dear Priya,\n\nThis confirms that your application for the QA Engineer position at Globex "
         "has been withdrawn at your request.",
         expect=("auto", "globex-qa", "withdrawn"), note="withdrawal is not a rejection"),

    # ── Mail that names a tracked employer and is not about the application ─
    mail("N1", "Brightmart <orders@brightmart.test>", "Your Brightmart order #114-2231 has shipped",
         "Hello Priya, your order has shipped and is on its way. Track your package in your account.",
         expect=("ignore",), note="order receipt from an employer that is also a shop"),
    mail("N2", "Nimbus <no-reply@accounts.nimbus.test>", "Security alert: new sign-in to your Nimbus Account",
         "We noticed a new sign-in to your Nimbus Account on a Windows device. If this was you, you "
         "do not need to do anything.",
         expect=("ignore",), note="account security alert"),
    mail("N3", "Harbor Bank <alerts@harborbank.test>", "Your Harbor Bank credit card application",
         "Thank you for applying. Your Harbor Bank credit card application is being reviewed. We "
         "will let you know our decision within 7 to 10 business days.",
         expect=("ignore",), note="a credit card application, at a bank the person also applied to work at"),
    mail("N4", "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
         "Machine Learning Engineer at Acme and 12 more new jobs",
         "Your job alert for machine learning engineer. Machine Learning Engineer at Acme. Apply now. "
         "See all jobs.",
         expect=("ignore",), note="job alert naming a tracked role and employer"),
    mail("N5", "Hooli <news@hooli.test>", "The role of AI in payments: Hooli Monthly",
         "This month: the role of AI in payments, a new opening for our developer conference, and a "
         "look at the job of a platform team.",
         expect=("ignore",), note="newsletter that happens to say role, opening and job"),
    mail("N6", f"Priya Sharma <{ME}>", "Following up on my Machine Learning Engineer application",
         "Hi Jane,\n\nI applied for the Machine Learning Engineer role at Acme last week. I'd love to "
         "schedule a call to discuss my application.\n\nPriya",
         labels=["SENT"], expect=("ignore",), note="the person's own sent mail"),
    mail("N7", "Globex <globex@myworkday.com>", "Your Globex verification code",
         "Your verification code for your candidate account is 483920.",
         expect=("ignore",), note="sign-in code from a hiring portal"),
    mail("N8", "Ravi Menon <ravi@quickstaff.test>", "Java Developer - Hooli",
         "Hi, we have an urgent job opening for the Java Developer position with our client Hooli. "
         "Please send your updated resume. Are you available for a quick call today?",
         expect=("ignore",), note="staffing-agency outreach for a role never applied to"),
    mail("N9", "OtherCo Recruiting <no-reply@us.greenhouse-mail.io>", "Your application to OtherCo",
         "Thank you for your interest in the Software Engineer role at OtherCo. We have decided to "
         "move forward with other candidates.",
         expect=("ignore",), note="same title as a tracked job, at an employer that is not tracked"),

    mail("N10", "Indeed <no-reply@indeed.com>", "Acme viewed your application",
         "Good news: Acme viewed your application for Machine Learning Engineer. Employers who view "
         "an application often respond within a few days.",
         expect=("ignore",), note="'viewed your application' changes nothing for a job already applied to"),
    mail("N11", "Globex <globex@myworkday.com>", "Finish your application for QA Engineer",
         "You started an application for the QA Engineer position at Globex. Your application is "
         "incomplete. Complete your application to be considered.",
         expect=("ignore",), note="reminder about an application that was never submitted"),
    mail("N12", "Soylent Recruiting <no-reply@us.greenhouse-mail.io>", "How was your interview experience with Soylent?",
         "Thank you for interviewing for the Data Engineer role at Soylent. Please take our two "
         "minute candidate survey about your interview experience.",
         expect=("ignore",), note="candidate survey"),
    mail("N13", "Dana Kim <dana.kim@soylent.test>", "Thank you for interviewing with Soylent",
         "Hi Priya,\n\nThank you for interviewing for the Data Engineer role at Soylent yesterday. The "
         "team enjoyed meeting you and we will be in touch soon about next steps.\n\nDana",
         expect=("not", "rejected"), note="a thank-you after an interview decides nothing"),
    mail("I8", "Acme Recruiting <no-reply@us.greenhouse-mail.io>", "Acme Machine Learning Engineer interview",
         "Hi Priya,\n\nWe would like to invite you to interview for the Machine Learning Engineer role "
         "at Acme. Please share your availability for this week.\n\nP.S. Join our talent community "
         "to hear about new roles.",
         expect=("auto", "acme-mle", "interviewing"),
         note="a real invitation that also mentions the talent community"),

    # ── Real application mail the tracker cannot place by itself ────────────
    mail("U1", "Acme Recruiting <no-reply@us.greenhouse-mail.io>", "Your application to Acme",
         "Thank you for your interest in the Senior Staff Engineer role at Acme. Your application for "
         "Senior Staff Engineer at Acme was not selected.",
         expect=("unmatched",), note="a decision about a role that is not in the tracker"),
    mail("U2", "Massive Dynamic Careers <careers@massivedynamic.test>", "Your application to Massive Dynamic",
         "Hi Priya,\n\nThank you for your interest in Massive Dynamic. We have decided not to move "
         "forward with your application.",
         expect=("review", {"massive-swe", "massive-senior"}, "rejected"),
         note="two applications at this employer and the email names neither"),
]

# Several emails about one application, oldest first. The newest fact wins and
# nothing older may undo it.
THREADS: List[Dict[str, Any]] = [
    {"id": "T1", "note": "receipt, then a decline",
     "emails": [
         mail("T1a", "Acme Recruiting <no-reply@us.greenhouse-mail.io>", "Thank you for applying to Acme",
              "Thank you for applying to the Machine Learning Engineer role at Acme. We have received "
              "your application.", expect=(), when=1),
         mail("T1b", "Acme Recruiting <no-reply@us.greenhouse-mail.io>", "Your application to Acme",
              "Thank you for your interest in the Machine Learning Engineer role at Acme. We have "
              "decided to move forward with other candidates.", expect=(), when=2)],
     "expect": ("auto", "acme-mle", "rejected")},
    {"id": "T2", "note": "interview, then a decline",
     "emails": [
         mail("T2a", "Jane Park <jane.park@acme.test>", "Acme - Machine Learning Engineer interview",
              "Regarding the Machine Learning Engineer role at Acme: let's schedule a call. "
              "https://calendly.com/jane-park/30min", expect=(), when=1),
         mail("T2b", "Jane Park <jane.park@acme.test>", "Acme - Machine Learning Engineer",
              "Regarding the Machine Learning Engineer role at Acme: we have decided to move forward "
              "with other candidates.", expect=(), when=2)],
     "expect": ("auto", "acme-mle", "rejected")},
    {"id": "T3", "note": "a decline, then a newer interview invitation: ask, never guess",
     "emails": [
         mail("T3a", "Jane Park <jane.park@acme.test>", "Acme - Machine Learning Engineer",
              "Regarding the Machine Learning Engineer role at Acme: we have decided to move forward "
              "with other candidates.", expect=(), when=1),
         mail("T3b", "Jane Park <jane.park@acme.test>", "Acme - Machine Learning Engineer interview",
              "Regarding the Machine Learning Engineer role at Acme: a spot opened up. Are you "
              "available on Friday for an interview?", expect=(), when=2)],
     "expect": ("review", "acme-mle", "interviewing")},
]
