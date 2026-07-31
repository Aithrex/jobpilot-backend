"""An ATS anti-bot rejection is a human handoff, not a failed application.

Two live Ashby submissions (Cerebras, Chainalysis) came back with

    "We couldn't submit your application. Your application submission was
     flagged as possible spam."

and were recorded as terminal `failed` rows carrying that text as the error.
Nothing was wrong with the application and nothing reached the employer, but
the user was never offered the one-tap "finish this in your browser" handoff
that the captcha path already builds for exactly this situation.
"""
import pytest

from services.browser_executor import BrowserExecutor


class _Element:
    def __init__(self, text, visible=True):
        self._text, self._visible = text, visible

    def is_visible(self):
        return self._visible

    def inner_text(self):
        return self._text

    def get_attribute(self, name):
        return None


class _Page:
    """Answers one selector with an element and everything else with None."""

    def __init__(self, matches):
        self._matches = matches

    def query_selector(self, selector):
        return self._matches.get(selector)

    def inner_text(self, _selector):
        return " ".join(e.inner_text() for e in self._matches.values() if e)


@pytest.fixture
def executor():
    return BrowserExecutor.__new__(BrowserExecutor)


ANTIBOT_TEXTS = [
    "We couldn't submit your application\n\nYour application submission was "
    "flagged as possible spam. If you believe this was a mistake, please "
    "submit your application",
    "Your submission was suspected as spam.",
    "We detected unusual traffic from your network.",
    "Automated submissions are not permitted.",
    "Please verify you are human to continue.",
    "Security check failed.",
    "Too many attempts. Try again later.",
]

REAL_FIELD_ERRORS = [
    "This field is required.",
    "Please enter a valid email address.",
    "Resume is required to submit.",
    "Phone number format is invalid.",
]


@pytest.mark.parametrize("text", ANTIBOT_TEXTS)
def test_antibot_text_is_detected(executor, text):
    page = _Page({"[role=alert]": _Element(text)})
    assert executor._antibot_block(page) is not None


@pytest.mark.parametrize("text", REAL_FIELD_ERRORS)
def test_a_genuine_field_error_is_not_an_antibot_block(executor, text):
    """A real validation failure must stay a failure — it means the form is
    genuinely missing an answer, which is a question for the user."""
    page = _Page({"[role=alert]": _Element(text)})
    assert executor._antibot_block(page) is None


def test_hidden_text_is_ignored(executor):
    """Greenhouse ships error placeholders in the DOM of every form."""
    page = _Page({"[role=alert]": _Element("flagged as possible spam",
                                           visible=False)})
    assert executor._antibot_block(page) is None


def test_no_alert_at_all(executor):
    assert executor._antibot_block(_Page({})) is None


def test_detection_is_bounded(executor):
    """The returned text is surfaced to the user, so it must be trimmed."""
    page = _Page({"[role=alert]": _Element("flagged as possible spam " + "x" * 5000)})
    assert len(executor._antibot_block(page)) <= 200


# ---------------------------------------------------------------------------
# Workday account-gate preflight
# ---------------------------------------------------------------------------
class TestWorkdayAccountPreflight:
    """Creating a Workday candidate account commits us to reading a
    verification email. When the deployment cannot receive mail at a managed
    alias, that round trip is impossible.

    Live consequence before this guard: `mailbox_enabled()` was False and
    `apply_mail_domain()` empty, an alias was minted anyway at
    `harshith.manne@my-private-mail.com`, four Workday applications registered
    accounts against it, every portal mailed a verification link there, and
    `mail_messages` never held a single document.
    """

    def test_capability_needs_both_flag_and_domain(self, monkeypatch):
        from services import managed_mailbox_service as mm

        for enabled, domain, expected in [
            (True, "pilotmail.co", True),
            (True, "", False),
            (False, "pilotmail.co", False),
            (False, "", False),
        ]:
            monkeypatch.setattr(mm, "mailbox_enabled", lambda e=enabled: e)
            monkeypatch.setattr(mm, "apply_mail_domain", lambda d=domain: d)
            assert mm.can_receive_mail() is expected

    def test_gate_refuses_to_register_when_mail_is_unreadable(self, monkeypatch):
        from services import managed_mailbox_service as mm

        monkeypatch.setattr(mm, "can_receive_mail", lambda: False)
        monkeypatch.setattr(mm, "has_ever_received", lambda e: True)
        ex = BrowserExecutor.__new__(BrowserExecutor)

        def _boom(*a, **k):                      # must never be reached
            raise AssertionError("registered an account we could not verify")

        monkeypatch.setattr(BrowserExecutor, "create_account", _boom, raising=False)
        assert ex._workday_clear_account_gate(
            _Page({}), object(), {"apply_url": "https://x.wd1.myworkdayjobs.com/a"},
            "user@example.com", "x") is False

    def test_a_probe_fault_does_not_re_enable_the_flow(self, monkeypatch):
        from services import managed_mailbox_service as mm

        def _raise():
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(mm, "can_receive_mail", _raise)
        monkeypatch.setattr(mm, "has_ever_received", lambda e: True)
        ex = BrowserExecutor.__new__(BrowserExecutor)
        monkeypatch.setattr(
            BrowserExecutor, "create_account",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("must not register")), raising=False)
        assert ex._workday_clear_account_gate(
            _Page({}), object(), {}, "user@example.com", "x") is False


    def test_configured_but_never_delivered_still_refuses(self, monkeypatch):
        """Configuration is necessary but not sufficient. Prod has both flags
        on and live MX, yet nothing has ever been observed arriving — which is
        exactly the state that produced four unverifiable Workday accounts."""
        from services import managed_mailbox_service as mm

        monkeypatch.setattr(mm, "can_receive_mail", lambda: True)
        monkeypatch.setattr(mm, "has_ever_received", lambda e: False)
        ex = BrowserExecutor.__new__(BrowserExecutor)
        monkeypatch.setattr(
            BrowserExecutor, "create_account",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("must not register")), raising=False)
        assert ex._workday_clear_account_gate(
            _Page({}), object(), {}, "user@example.com", "x") is False
