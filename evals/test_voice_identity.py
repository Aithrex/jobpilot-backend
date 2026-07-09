"""§6.12.9 eval — voice identity: style.distance() separates authors.

Contract under test:
  * ``style.distance(text, profile)`` is the voice_identity core: a held-out
    text by the SAME author must score strictly closer to that author's
    profile than held-out texts by DIFFERENT authors (full matrix, every
    profile x every held-out text, every hold-out rotation).
  * The real profile pipeline (build_profile over ready samples) produces a
    profile that drives the same separation.
  * Below MIN_PROFILE_WORDS the profile honestly refuses
    (``{"insufficient": True}``) instead of guessing a voice.

Fixture corpus: three synthetic authors, three small texts each, with
deliberately distinct measured styles (terse/concrete + contractions +
numbers; hedged/passive/winding; formal corporate). Small by design — this
pins the separation property of the deterministic stylometry, not model
quality.

Hard-fail criteria: any held-out text scoring closer to a different author's
profile than to its own author's; margin collapse; or a profile built from
insufficient signal.
"""
import pytest

from services.voicegraph import samples, style

# --- Author A: terse, concrete, active, contractions, numbers --------------
A = (
    "I shipped the billing worker in March. It's small: two queues, one "
    "consumer, 900 lines. The p99 dropped to 80ms. I don't add layers until "
    "something hurts. We cut the deploy down to 4 minutes flat. Postgres "
    "does the heavy lifting; Redis holds the hot keys. When the pager fired "
    "at 2am, I traced it to a bad index in 20 minutes and shipped the fix "
    "before standup. Small diffs, fast reviews, no drama. I write the "
    "ticket after I've read the code, not before. If a fix can't ship "
    "today, I split it until it can.",

    "The importer choked on 3 million rows, so I rewrote it over a weekend. "
    "It's boring code: one loop, batched writes, 250 lines. Throughput went "
    "from 40 to 900 rows a second. I don't trust clever abstractions. We "
    "shipped on Friday and the pager stayed quiet all week. Kafka feeds it; "
    "Postgres stores it. That's the whole design. I'd rather delete code "
    "than tune it. The queue tells me when I'm wrong, and it's loud about "
    "it.",

    "We killed the nightly batch in June. It's one stream now: 3 stages, "
    "600 lines, no cron. Failures page us in 90 seconds instead of the next "
    "morning. I read the logs, fixed two off-by-ones, and shipped the patch "
    "the same day. Don't guess; measure. The dashboard shows lag in ms and "
    "that's the only number I watch. Costs fell 30% too. Nobody misses the "
    "old job.",
)

# --- Author B: hedged, passive, long winding subordinate clauses ------------
B = (
    "It could probably be argued that the migration, which was undertaken "
    "because the previous system had been considered somewhat fragile, was "
    "generally successful, although there were moments when it seemed as "
    "though the rollout might perhaps have been rushed. The service was "
    "maintained by the platform group, and the decisions that were made, "
    "while arguably reasonable, were usually revisited whenever concerns "
    "were raised, which happened fairly often, essentially because the "
    "expectations had never really been aligned.",

    "It seems fair to suggest that the redesign, which had been requested "
    "because the interface was thought to be somewhat confusing, was "
    "received rather well, though it might possibly have benefited from "
    "further review. The feedback was gathered by the research group, and "
    "the changes that were proposed, while generally sensible, were often "
    "reconsidered whenever objections were voiced, which occurred quite "
    "regularly, essentially because the goals had never been fully agreed, "
    "at least so far as anyone could really tell at the time.",

    "One might reasonably suppose that the documentation effort, which had "
    "been initiated because onboarding was felt to be somewhat slower than "
    "desired, was broadly appreciated, even though it could perhaps have "
    "been organized differently. The pages were written by various "
    "contributors, and the structure that was chosen, while essentially "
    "workable, was frequently debated whenever newcomers were consulted, "
    "which tended to happen rather often, arguably because ownership had "
    "never really been established.",
)

# --- Author C: formal corporate, no contractions, no hedges, no numbers -----
C = (
    "Our organization completed the annual planning cycle on schedule. The "
    "committee reviewed each proposal and assigned funding according to the "
    "published criteria. Department leaders presented their objectives to "
    "the board. The finance team prepared the consolidated forecast. Every "
    "initiative received a designated sponsor and a quarterly reporting "
    "cadence. The office will publish the final roadmap next month. "
    "Stakeholders received the summary documentation in advance of the "
    "quarterly meeting. The secretariat recorded the decisions and "
    "circulated the minutes to all participants within the standard "
    "interval.",

    "The organization concluded the vendor selection process this quarter. "
    "The committee evaluated each submission against the published "
    "requirements. Regional managers presented their assessments to the "
    "board. The procurement team prepared the comparative summary. Every "
    "contract received a designated owner and a monthly reporting cadence. "
    "The office will circulate the final agreement shortly. Stakeholders "
    "reviewed the transition plan during the scheduled briefing. The "
    "secretariat documented the outcome and forwarded the record to the "
    "appropriate departments for retention.",

    "The division finalized the compliance review ahead of the deadline. "
    "The committee examined each finding and assigned remediation owners "
    "according to the established procedure. Program managers presented "
    "their status reports to the steering group. The audit team prepared "
    "the closing memorandum. Every action item received a designated "
    "approver and a weekly reporting cadence. The office will distribute "
    "the certification letter in due course.",
)

AUTHORS = {"A": A, "B": B, "C": C}


def _profile_excluding(texts, held_out_index):
    corpus = "\n\n".join(t for i, t in enumerate(texts) if i != held_out_index)
    return {"features": style.compute_features(corpus)}


# ---------------------------------------------------------------------------
# The separation matrix — every hold-out rotation, every author pair
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("held_out_index", [0, 1, 2])
def test_same_author_scores_strictly_closer(held_out_index):
    profiles = {a: _profile_excluding(t, held_out_index)
                for a, t in AUTHORS.items()}
    for author, texts in AUTHORS.items():
        held_out = texts[held_out_index]
        own = style.distance(held_out, profiles[author])
        for other, other_profile in profiles.items():
            if other == author:
                continue
            cross = style.distance(held_out, other_profile)
            assert own < cross, (
                f"voice identity broken (hold-out {held_out_index}): "
                f"{author}'s text scored {own:.4f} to its own profile but "
                f"{cross:.4f} to {other}'s profile"
            )


def test_separation_has_a_real_margin():
    """Not just strictly-less: the nearest cross-author distance must exceed
    the same-author distance by a real margin, so small feature drift cannot
    silently erase identity."""
    for author, texts in AUTHORS.items():
        profile = _profile_excluding(texts, 2)
        own = style.distance(texts[2], profile)
        cross = min(
            style.distance(t[2], profile)
            for a, t in AUTHORS.items() if a != author
        )
        assert cross > own * 1.15, (
            f"{author}: margin too thin (own={own:.4f}, "
            f"nearest cross={cross:.4f})"
        )


# ---------------------------------------------------------------------------
# Through the real profile pipeline (build_profile over ready samples)
# ---------------------------------------------------------------------------
def test_pipeline_profile_preserves_identity(mock_db):
    profiles = {}
    for author, texts in AUTHORS.items():
        email = f"identity-{author.lower()}@example.com"
        samples.save_sample(email, texts[0], finalize=True)
        samples.save_sample(email, texts[1], finalize=True)
        prof = style.build_profile(email)
        assert not prof.get("insufficient"), \
            f"author {author} fixtures too short for a profile"
        profiles[author] = prof

    for author, texts in AUTHORS.items():
        held_out = texts[2]
        own = style.distance(held_out, profiles[author])
        for other, prof in profiles.items():
            if other == author:
                continue
            assert own < style.distance(held_out, prof), (
                f"pipeline profile lost identity: {author} vs {other}"
            )


def test_insufficient_signal_is_refused(mock_db):
    email = "too-short@example.com"
    samples.save_sample(email, "I write short things sometimes.", finalize=True)
    prof = style.build_profile(email)
    assert prof.get("insufficient") is True
    assert prof.get("word_count", 0) < style.MIN_PROFILE_WORDS
