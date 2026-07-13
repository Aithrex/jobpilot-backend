"""LLM provider package — Claude on AWS Bedrock.

The abstraction exists so a future provider (or a different Bedrock-Claude
variant mix) can slot in without touching the caller files — everything
goes through `services.claude_client` → `get_provider()`.
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
