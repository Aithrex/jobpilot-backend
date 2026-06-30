"""LLM provider package — Claude on AWS Bedrock.

Originally supported a `LLM_PROVIDER` env var to swap between Claude and
Gemini providers. The Gemini implementation was removed once Google
locked new accounts into a separate Prepaid wallet that excluded the
$300 Cloud Billing trial credit. The abstraction stays in place so a
future provider (OpenAI, Groq, Bedrock-Claude variants) can slot in
without touching the 9+ caller files.
"""
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_provider():
    """Return the active LLM provider instance (cached per-process)."""
    from .claude import ClaudeProvider
    logger.info("LLM provider: claude (AWS Bedrock)")
    return ClaudeProvider()
