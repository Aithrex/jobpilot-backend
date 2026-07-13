"""LLM client facade — re-exports the active provider's public surface.

All AI in JobPilot runs on Claude via AWS Bedrock; the implementation lives
in `services/llm_providers/claude.py` (boto3 `invoke_model`, no Anthropic
SDK). This facade exists so callers never touch boto3/Bedrock directly and
so the model tiers stay swappable via env.

Tier override via env (Bedrock):
    BEDROCK_FLASH_MODEL_ID, BEDROCK_PRO_MODEL_ID, BEDROCK_PREVIEW_MODEL_ID
"""
from services.llm_providers import get_provider
from services.llm_providers.base import LLMRetriesExhaustedError

_provider = get_provider()

# Public model constants — these names are imported all over the codebase.
# They resolve to whichever provider is active at module-load time.
# FLASH = Haiku (fast extraction), PRO = Sonnet (tailoring/JD/scoring),
# PREVIEW = repair tier.
CLAUDE_FLASH = _provider.FLASH
CLAUDE_PRO = _provider.PRO
CLAUDE_PREVIEW = _provider.PREVIEW


def claude_json(
    prompt=None,
    max_tokens: int = 8192,
    temperature: float = 0.3,
    model: str = None,
    max_retries: int = 2,
    schema: dict = None,
    parts: list = None,
    system: str = None,
) -> dict:
    """Primary entry point used by all service modules.

    Routes to the active provider's `json()` implementation. The optional
    `system` puts trusted instructions out of the untrusted-data user
    message (defense in depth against prompt injection).
    """
    return _provider.json(
        prompt=prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        model=model or _provider.PRO,
        max_retries=max_retries,
        schema=schema,
        parts=parts,
        system=system,
    )


def get_claude_client():
    """Returns the active provider's raw client — for Claude this is the
    boto3 Bedrock runtime client. Most callers should use `claude_json` +
    provider abstractions rather than touching the client directly."""
    return _provider.lazy_client()


def get_active_provider():
    """Escape hatch for the small number of callsites that need provider-
    specific behavior (multimodal PDF parts, tool-calling orchestrator)."""
    return _provider


# Convenience re-exports — some callsites import these from claude_client too.
clean_json_response = getattr(_provider, "clean_json_response", None)

__all__ = [
    "claude_json",
    "get_claude_client",
    "get_active_provider",
    "CLAUDE_FLASH",
    "CLAUDE_PRO",
    "CLAUDE_PREVIEW",
    "LLMRetriesExhaustedError",
    "clean_json_response",
]
