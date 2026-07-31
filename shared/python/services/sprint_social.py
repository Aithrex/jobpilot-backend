"""Learning Hub — Social Sharing Service (INTERFACE + DATA MODEL ONLY).

Not implemented yet. This exists so a future LinkedIn integration can plug in
without changing the Learning Hub architecture. When built, after a challenge or
the full sprint is completed, the platform will generate an EDITABLE LinkedIn
post the user shares manually (OAuth only — never credentials).

The abstraction: a `SocialSharer` builds a `ShareDraft` from a completed
day/achievement. The blueprint intentionally exposes NO share route today.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class ShareDraft:
    platform: str                    # "linkedin"
    headline: str
    body: str
    hashtags: List[str] = field(default_factory=list)
    editable: bool = True

    def to_doc(self) -> Dict:
        return {
            "platform": self.platform,
            "headline": self.headline,
            "body": self.body,
            "hashtags": self.hashtags,
            "editable": self.editable,
        }


class SocialSharer(ABC):
    platform: str = "generic"

    @abstractmethod
    def build_draft(self, user_email: str, context: Dict) -> ShareDraft:
        """Build an editable share draft from a completion/achievement context."""
        ...


class LinkedInSharer(SocialSharer):
    platform = "linkedin"

    def build_draft(self, user_email: str, context: Dict) -> ShareDraft:
        raise NotImplementedError("LinkedIn sharing is not implemented yet.")


# Registry so future platforms plug in the same way validators do.
SHARER_REGISTRY: Dict[str, SocialSharer] = {
    "linkedin": LinkedInSharer(),
}


def get_sharer(platform: str) -> SocialSharer:
    return SHARER_REGISTRY[platform]
