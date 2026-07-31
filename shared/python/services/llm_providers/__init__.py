"""LLM provider package — runtime selection between Claude and Gemini.

Selection is driven by the `LLM_PROVIDER` setting:

    claude  → Anthropic Claude on AWS Bedrock  (default)
    gemini  → Google Gemini via google-genai

Resolved through `utils.config._get_config_value`, so the SAME key works in
both environments with no code difference: SSM Parameter Store in Lambda
(`USE_SSM_SECRETS=true`), `.env` locally. Flipping it in production is one
command and needs no redeploy:

    aws ssm put-parameter --name /jobpilot/<env>/llm-provider \\
      --value gemini --type String --overwrite

The public surface (`claude_json`, `CLAUDE_FLASH`, …) stays under
`services.claude_client`, so none of the ~90 call sites change.

ISOLATION: exactly one provider is active per process. The chosen provider's
module is imported INSIDE its branch, so the other SDK (boto3 Bedrock or
google-genai) is never loaded and its backend is never called.
"""
import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)

_VALID_PROVIDERS = ("claude", "gemini")


def _resolve_provider_name() -> str:
    """Read LLM_PROVIDER from SSM (prod) or env (local), defaulting to claude.

    The SSM read is best-effort: `utils.config` pulls in boto3 and is not
    guaranteed to be importable in every context (standalone scripts, some
    test paths). Falling back to the raw env var keeps provider selection
    working even then.
    """
    try:
        from utils.config import _get_config_value
        raw = _get_config_value("LLM_PROVIDER", "") or ""
    except Exception:  # pragma: no cover - defensive
        raw = ""

    if not raw:
        raw = os.getenv("LLM_PROVIDER", "") or ""
    return raw.strip().lower() or "claude"


@lru_cache(maxsize=1)
def get_provider():
    """Return the active LLM provider instance (cached per-process).

    Cached for the process lifetime, so a change to LLM_PROVIDER takes effect
    on the next cold start — not on in-flight invocations.
    """
    name = _resolve_provider_name()

    if name == "gemini":
        from .gemini import GeminiProvider  # lazy: boto3 Bedrock never loaded
        logger.info("LLM provider: gemini (Google AI Studio)")
        return GeminiProvider()

    if name == "claude":
        from .claude import ClaudeProvider  # lazy: google-genai never loaded
        logger.info("LLM provider: claude (AWS Bedrock)")
        return ClaudeProvider()

    # Fail fast rather than defaulting. A typo ("gemni") must not silently
    # route every AI call — and its cost — to the wrong provider.
    raise ValueError(
        f"Unknown LLM_PROVIDER={name!r}. Expected one of {_VALID_PROVIDERS}. "
        "Unset it to use the default (claude)."
    )
