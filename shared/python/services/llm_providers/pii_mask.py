"""Reversible masking of contact details in text sent to an outside LLM.

An email, phone number or profile link becomes a placeholder before the call
and is restored in the reply, so résumé parsing still returns the real value.
The mapping lives only for one call, in this process.

This narrows what leaves the box; it does not anonymise a résumé. A name or an
employment history can still identify someone and is not masked here.
"""
import re
from typing import Any, Dict, List

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_PROFILE = re.compile(
    r"(?:https?://)?(?:[\w-]+\.)?(?:linkedin\.com|github\.com|gitlab\.com|leetcode\.com)/[^\s,;)\]>\"']+",
    re.IGNORECASE,
)
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# Separators or a + are required, so a plain number (a salary, an ID, a year
# range like 2019-2023) is left alone.
_PHONE = re.compile(
    r"(?<![\w.])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?|\d{2,5}[\s.-])\d{2,5}(?:[\s.-]\d{2,5})?(?![\w])"
    r"|(?<![\w.])\+\d{10,14}(?![\w])"
)
_TOKEN = re.compile(r"\[(EMAIL|LINK|ID|PHONE)_(\d+)\]")


def _looks_like_phone(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    return 10 <= len(digits) <= 15 and not re.fullmatch(r"(?:19|20)\d{2}\D+(?:19|20)\d{2}", value.strip())


class PiiMasker:
    def __init__(self) -> None:
        self._by_value: Dict[str, str] = {}
        self._by_token: Dict[str, str] = {}

    def _token(self, kind: str, value: str) -> str:
        token = self._by_value.get(value)
        if token is None:
            token = f"[{kind}_{len(self._by_value) + 1}]"
            self._by_value[value] = token
            self._by_token[token] = value
        return token

    def mask(self, text: Any) -> Any:
        if not isinstance(text, str) or not text:
            return text
        text = _EMAIL.sub(lambda m: self._token("EMAIL", m.group()), text)
        text = _PROFILE.sub(lambda m: self._token("LINK", m.group()), text)
        text = _SSN.sub(lambda m: self._token("ID", m.group()), text)
        return _PHONE.sub(
            lambda m: self._token("PHONE", m.group()) if _looks_like_phone(m.group()) else m.group(),
            text,
        )

    def unmask(self, value: Any) -> Any:
        if isinstance(value, str):
            return _TOKEN.sub(lambda m: self._by_token.get(m.group(), m.group()), value)
        if isinstance(value, list):
            return [self.unmask(v) for v in value]
        if isinstance(value, dict):
            return {k: self.unmask(v) for k, v in value.items()}
        return value


class MaskedParts(List[Any]):
    """Content parts built from masked text, carrying the masker that can
    restore the reply (build_pdf_parts -> json(parts=...))."""

    masker: PiiMasker
