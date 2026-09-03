"""Lightweight headless-DOM harness for the ATS adapter tests.

WHY THIS AND NOT A REAL BROWSER
-------------------------------
``tests/test_browser_executor.py`` drives an ACTUAL headless Chromium and
``pytest.importorskip``s / ``skipif``s itself off wherever Playwright/Chromium is
not installed (CI, most dev boxes). The deterministic ATS ADAPTER framework
(``services.ats_adapters``) is intentionally page-agnostic: it only ever touches a
tiny, Playwright-shaped duck-typed surface (``page.query_selector(css)`` /
``query_selector_all`` / ``page.url`` / ``page.inner_text("body")`` and, per
element, ``get_attribute`` / ``inner_text`` / ``is_visible`` / ``fill`` /
``click``). This module implements exactly that surface over an lxml-parsed static
HTML document, so the adapter suite runs with ZERO browser and ZERO network — the
CHOSEN harness (noted in the task): a lightweight HTML-parse driver rather than a
live browser, so the suite never self-skips.

``cssselect`` is NOT installed in this environment, so we translate the small,
fixed CSS subset the adapters use into XPath (libxml2 handles XPath natively).

``FakePage`` also models the ONE stateful thing the submit boundary needs: a
single click on a submit control transitions the document to a caller-supplied
``next_html`` (the post-submit page). Clicks are counted so a test can prove the
adapter clicks submit exactly once and NEVER auto-retries an ambiguous result.
"""
from __future__ import annotations

import re

import lxml.html

# ---------------------------------------------------------------------------
# Tiny CSS -> XPath translator (the fixed subset the adapters use).
# Supports: comma lists, a leading tag or '*', '#id', '.class', attribute
# selectors '[a]', '[a=v]', '[a*=v]', '[a^=v]', '[a$=v]' (quoted or bare values),
# and Playwright's ``:has-text('...')`` pseudo-class.
# No combinators are needed (the adapters query single compound selectors and do
# any structural/text work in Python).
#
# ``:has-text`` is NOT decoration: `_click_submit` asks for
# ``button:has-text('Submit application')`` and a real ATS form carries several
# other buttons (a react-select's "Toggle flyout", the résumé "Attach"). Ignoring
# the text filter silently returned the FIRST button on the page, i.e. the
# harness clicked the wrong control and no fixture with more than one button
# could ever be trusted.
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"^([a-zA-Z][\w-]*|\*)")
_HAS_TEXT_RE = re.compile(r""":has-text\(\s*(['"])(.*?)\1\s*\)""")
_TOKEN_RE = re.compile(r"\#([\w-]+)|\.([\w-]+)|\[([^\]]*)\]")
_ATTR_RE = re.compile(r"\s*([\w:-]+)\s*(?:([*^$]?=)\s*(.*))?$")


def _xstr(s: str) -> str:
    """Quote a Python string as an XPath 1.0 string literal (handles quotes)."""
    if "'" not in s:
        return "'%s'" % s
    if '"' not in s:
        return '"%s"' % s
    return "concat(" + ", \"'\", ".join("'%s'" % p for p in s.split("'")) + ")"


def _attr_pred(body: str) -> str:
    m = _ATTR_RE.match(body)
    if not m:
        return "true()"
    name, op, val = m.group(1), m.group(2), (m.group(3) or "").strip()
    if val[:1] in ("'", '"') and val[-1:] == val[:1]:
        val = val[1:-1]
    if not op:
        return "@%s" % name
    if op == "=":
        return "@%s=%s" % (name, _xstr(val))
    if op == "*=":
        return "contains(@%s,%s)" % (name, _xstr(val))
    if op == "^=":
        return "starts-with(@%s,%s)" % (name, _xstr(val))
    if op == "$=":
        return ("substring(@%s,string-length(@%s)-string-length(%s)+1)=%s"
                % (name, name, _xstr(val), _xstr(val)))
    return "@%s" % name


def _simple_to_xpath(sel: str) -> str:
    sel = sel.strip()
    texts = [m.group(2) for m in _HAS_TEXT_RE.finditer(sel)]
    sel = _HAS_TEXT_RE.sub("", sel)
    tag, i = "*", 0
    m = _TAG_RE.match(sel)
    if m:
        tag, i = m.group(1), m.end()
    preds = []
    for tok in _TOKEN_RE.finditer(sel[i:]):
        if tok.group(1):
            preds.append("@id=%s" % _xstr(tok.group(1)))
        elif tok.group(2):
            preds.append("contains(concat(' ',normalize-space(@class),' '),' %s ')"
                         % tok.group(2))
        else:
            preds.append(_attr_pred(tok.group(3)))
    # Playwright's :has-text is a case-insensitive SUBSTRING match on the
    # element's rendered text (which is also why the executor must never use it
    # to pick a country - see browser_executor._click_option_by_text).
    for text in texts:
        preds.append("contains(translate(., %s, %s), %s)" % (
            _xstr(text.upper()), _xstr(text.lower()), _xstr(text.lower())))
    return "//" + tag + "".join("[%s]" % p for p in preds)


def css_to_xpath(css: str) -> str:
    return " | ".join(_simple_to_xpath(part) for part in css.split(",") if part.strip())


# ---------------------------------------------------------------------------
# The Playwright-shaped fakes.
# ---------------------------------------------------------------------------
class FakeElement:
    """A single DOM node exposing the Playwright ElementHandle subset the
    adapters use."""

    _SUBMIT_TYPES = ("submit",)

    def __init__(self, node, page: "FakePage"):
        self._node = node
        self._page = page

    @property
    def tag(self) -> str:
        return self._node.tag

    def get_attribute(self, name):
        return self._node.get(name)

    def query_selector_all(self, css: str):
        """Element-SCOPED query (Playwright ElementHandle parity) — descendants of
        THIS node only. Used by the adapters' ``options_for`` to read a specific
        select's <option> children rather than every option on the page."""
        scoped = " | ".join("." + part.strip()
                            for part in css_to_xpath(css).split("|"))
        return [FakeElement(n, self._page) for n in self._node.xpath(scoped)]

    def query_selector(self, css: str):
        found = self.query_selector_all(css)
        return found[0] if found else None

    def inner_text(self) -> str:
        return (self._node.text_content() or "").strip()

    def is_visible(self) -> bool:
        node = self._node
        while node is not None and hasattr(node, "get"):
            style = (node.get("style") or "").replace(" ", "").lower()
            if "display:none" in style or "visibility:hidden" in style:
                return False
            if node.get("hidden") is not None:
                return False
            node = node.getparent()
        return True

    def bounding_box(self):
        # Best-effort from an inline style so the adapter's "sizable challenge"
        # heuristic works offline; None when unknown (adapter falls back to
        # presence+visibility).
        style = (self._node.get("style") or "").lower()
        w = re.search(r"width:\s*(\d+)", style)
        h = re.search(r"height:\s*(\d+)", style)
        if w and h:
            return {"x": 0, "y": 0, "width": int(w.group(1)), "height": int(h.group(1))}
        return None

    def fill(self, value) -> None:
        self._node.set("value", "" if value is None else str(value))

    def check(self) -> None:
        self._node.set("checked", "checked")

    def click(self) -> None:
        self._page._on_click(self)

    def evaluate(self, script: str):
        """The ONE page-function the executor's fill path runs per element
        (``e => e.tagName``). Anything else is out of scope for a static DOM and
        raises, exactly as an unsupported call would in a real browser."""
        if "tagName" in script:
            return str(self._node.tag).upper()
        raise NotImplementedError("FakeElement.evaluate: %s" % script)

    def screenshot(self, **kwargs) -> bytes:
        return b"\x89PNG\r\n\x1a\n" + self.inner_text().encode("utf-8")[:64]


class FakePage:
    """An lxml-backed static-DOM page. ``next_html`` (optional) is the document a
    single submit click transitions to (the post-submit state)."""

    def __init__(self, html: str, url: str = "", next_html: str = None):
        self._tree = lxml.html.fromstring(html)
        self.url = url
        self._next_html = next_html
        self.clicks = 0
        self.uploads = {}

    @classmethod
    def from_file(cls, path, url: str = "", next_file=None):
        html = _read(path)
        nxt = _read(next_file) if next_file else None
        return cls(html, url=url, next_html=nxt)

    def query_selector(self, css: str):
        nodes = self._tree.xpath(css_to_xpath(css))
        return FakeElement(nodes[0], self) if nodes else None

    def query_selector_all(self, css: str):
        return [FakeElement(n, self) for n in self._tree.xpath(css_to_xpath(css))]

    def inner_text(self, selector: str = "body") -> str:
        nodes = self._tree.xpath(css_to_xpath(selector))
        if nodes:
            return (nodes[0].text_content() or "").strip()
        return (self._tree.text_content() or "").strip()

    # -- page-level surface the browser_executor drive uses ------------------
    # (Timing calls are no-ops on a static DOM; the file/select actuators are
    # real state changes so a test can assert what actually landed in the form.)
    def set_default_timeout(self, ms) -> None:
        pass

    def wait_for_timeout(self, ms) -> None:
        pass

    def screenshot(self, **kwargs) -> bytes:
        return b"\x89PNG\r\n\x1a\n" + self.inner_text("body").encode("utf-8")[:64]

    def click(self, selector: str, **kwargs) -> None:
        el = self.query_selector(selector)
        if el is None:
            raise ValueError("click: no element for %r" % selector)
        el.click()

    def fill(self, selector: str, value=None, **kwargs) -> None:
        el = self.query_selector(selector)
        if el is None:
            raise ValueError("fill: no element for %r" % selector)
        el.fill(value)

    def set_input_files(self, selector: str, files=None) -> None:
        """Playwright parity: raises when the selector matches nothing, so a
        driver that guessed the wrong upload selector cannot silently 'succeed'."""
        el = self.query_selector(selector)
        if el is None:
            raise ValueError("set_input_files: no element for %r" % selector)
        payload = (files or [{}])[0]
        name = payload.get("name") if isinstance(payload, dict) else str(payload)
        el._node.set("value", name or "")
        self.uploads[selector] = name

    def select_option(self, selector: str, label=None, value=None):
        el = self.query_selector(selector)
        if el is None:
            raise ValueError("select_option: no element for %r" % selector)
        wanted = label if label is not None else value
        for opt in el._node.xpath(".//option"):
            text = (opt.text_content() or "").strip()
            if wanted in (text, opt.get("value")):
                el._node.set("value", opt.get("value") or text)
                return [opt.get("value") or text]
        raise ValueError("select_option: no option %r" % wanted)

    # -- stateful submit transition -----------------------------------------
    def _on_click(self, element: FakeElement) -> None:
        self.clicks += 1
        node = element._node
        # react-select parity (the SECOND stateful thing a modern ATS form does).
        # A div-based combobox renders NO option until its listbox is opened, and
        # a value is committed ONLY by clicking a rendered row - never by typing.
        # Both halves are modelled so the executor's open -> read -> match ->
        # click path is exercised end to end against
        # tests/fixtures/ats/greenhouse_react_select.html.
        if self._react_select_click(element):
            return
        typ = (node.get("type") or "").lower()
        is_submit = (node.tag == "button" and typ in ("", "submit")) \
            or (node.tag == "input" and typ == "submit")
        if is_submit and self._next_html is not None:
            self._tree = lxml.html.fromstring(self._next_html)
            self._next_html = None  # consume once — the click is not repeatable

    # -- react-select modelling ---------------------------------------------
    #: Class tokens that bound the walk from a clicked node up to the ONE
    #: combobox it belongs to. Without a bound, a page with two comboboxes would
    #: open both (and a click near the page root would find someone else's menu).
    _SELECT_SHELL_CLASSES = ("select-shell", "select__container", "select")

    def _select_shell(self, node):
        """The nearest enclosing combobox that owns a `.select__menu`."""
        cur = node
        while cur is not None and hasattr(cur, "get"):
            classes = (cur.get("class") or "").split()
            if any(c in classes for c in self._SELECT_SHELL_CLASSES):
                if cur.xpath(".//*[contains(concat(' ',normalize-space(@class),"
                             "' '),' select__menu ')]"):
                    return cur
            cur = cur.getparent()
        return None

    def _react_select_click(self, element: FakeElement) -> bool:
        """Model one click on a react-select. Returns True when this WAS a
        combobox interaction (so the caller stops); False leaves the click to
        the ordinary submit handling."""
        node = element._node
        shell = self._select_shell(node)
        if shell is None:
            return False
        menus = shell.xpath(".//*[contains(concat(' ',normalize-space(@class),"
                            "' '),' select__menu ')]")
        inputs = shell.xpath(".//*[contains(concat(' ',normalize-space(@class),"
                             "' '),' select__input ')]")
        if not menus or not inputs:
            return False
        menu, box = menus[0], inputs[0]
        if " select__option " in " %s " % " ".join((node.get("class") or "").split()):
            # Commit: the option's own text becomes the control's value, and the
            # listbox closes - exactly what react-select does on select.
            box.set("value", (node.text_content() or "").strip())
            box.set("aria-expanded", "false")
            menu.set("style", "display:none")
            return True
        # Anything else inside the control (the input itself, the "Toggle
        # flyout" button) OPENS the listbox.
        menu.set("style", "")
        box.set("aria-expanded", "true")
        return True


# ---------------------------------------------------------------------------
# Locator surface — OPT IN ONLY.
#
# `FakePage` deliberately has NO `locator`: `ats_adapters.WorkdayAdapter` probes
# `hasattr(page, "locator")` to tell a live Playwright page from this harness
# (`_fill_dropdown` / `_fill_month_year` short-circuit on it), so growing one on
# FakePage would silently re-route the whole Workday suite.
#
# `LocatorPage` is the opt-in for the ONE place that needs it: proving what
# `browser_executor._fill_select` does with `page.locator(...)`. Without it every
# such call raised AttributeError into an `except Exception: continue`, so the
# substring (`:has-text`) option click and the blind fallbacks were invisible to
# the offline suite — a test could not tell "we refused to guess" from "the
# harness could not reach the guess".
# ---------------------------------------------------------------------------
def _scoped(node, sel: str):
    """Descendants of ``node`` matching ``sel`` (CSS, or ``xpath=...``)."""
    if sel.startswith("xpath="):
        return node.xpath(sel[len("xpath="):])
    scoped = " | ".join("." + part.strip()
                        for part in css_to_xpath(sel).split("|"))
    return node.xpath(scoped)


class FakeLocator:
    """The Playwright ``Locator`` subset the executor uses: ``.first`` /
    ``count()`` / ``is_visible()`` / ``inner_text()`` / ``click()`` and a SCOPED
    ``.locator()`` (CSS or ``xpath=``)."""

    def __init__(self, page: FakePage, nodes):
        self._page = page
        self._nodes = list(nodes)

    @property
    def first(self) -> "FakeLocator":
        return FakeLocator(self._page, self._nodes[:1])

    def count(self) -> int:
        return len(self._nodes)

    def is_visible(self) -> bool:
        return bool(self._nodes) and FakeElement(self._nodes[0],
                                                 self._page).is_visible()

    def inner_text(self) -> str:
        if not self._nodes:
            raise ValueError("locator.inner_text: no element")
        return FakeElement(self._nodes[0], self._page).inner_text()

    def click(self, **kwargs) -> None:
        if not self._nodes:
            raise ValueError("locator.click: no element")
        FakeElement(self._nodes[0], self._page).click()

    def locator(self, sel: str) -> "FakeLocator":
        out, seen = [], set()
        for node in self._nodes:
            for hit in _scoped(node, sel):
                if id(hit) not in seen:
                    seen.add(id(hit))
                    out.append(hit)
        return FakeLocator(self._page, out)


class LocatorPage(FakePage):
    """A ``FakePage`` that also answers ``locator()`` / ``focus()``."""

    def locator(self, selector: str) -> FakeLocator:
        if selector.startswith("xpath="):
            return FakeLocator(self, self._tree.xpath(selector[len("xpath="):]))
        return FakeLocator(self, self._tree.xpath(css_to_xpath(selector)))

    def focus(self, selector: str) -> None:
        if self.query_selector(selector) is None:
            raise ValueError("focus: no element for %r" % selector)


def _read(path) -> str:
    with open(str(path), "r", encoding="utf-8") as fh:
        return fh.read()
