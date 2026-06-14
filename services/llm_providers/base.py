"""Abstract LLM provider interface.

Each concrete provider (Gemini, Claude) implements this interface. Callers
import via `services.gemini_client` (kept for backward compat) which proxies
to the active provider chosen by `LLM_PROVIDER` env var.

Three public surface areas:
  1. `json(prompt, ...)` — single-shot text/multimodal call returning a dict.
  2. `build_pdf_parts(...)` — provider-native multimodal payload for inline PDFs.
  3. `tool_call(...)` — multi-round function-calling orchestration loop.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple


class LLMRetriesExhaustedError(Exception):
    """Raised when all upstream LLM retry attempts have been exhausted."""
    code = "LLM_RETRIES_EXHAUSTED"


class LLMProvider(ABC):
    """Provider-agnostic LLM interface.

    Subclasses set the three model constants (FLASH / PRO / PREVIEW) to
    provider-native model IDs. Each constant maps to a tier used by callers
    via `GEMINI_FLASH` / `GEMINI_PRO` / `GEMINI_PREVIEW` re-exports.
    """

    # Subclasses override with provider-native model IDs.
    FLASH: str = ""
    PRO: str = ""
    PREVIEW: str = ""

    @abstractmethod
    def json(
        self,
        prompt: Optional[str] = None,
        max_tokens: int = 8192,
        temperature: float = 0.3,
        model: Optional[str] = None,
        max_retries: int = 2,
        schema: Optional[dict] = None,
        parts: Optional[list] = None,
    ) -> dict:
        """Call the LLM and return a parsed JSON dict.

        Mirrors the original `gemini_json` signature so callers don't change.
        """

    @abstractmethod
    def text(
        self,
        prompt: str,
        max_tokens: int = 1024,
        temperature: float = 0.5,
        model: Optional[str] = None,
        max_retries: int = 2,
        system: Optional[str] = None,
        history: Optional[list] = None,
    ) -> str:
        """Call the LLM and return plain text output (no JSON parsing).

        Used by chat features for conversational generation. Optional
        system prompt + multi-turn history (uniform shape:
        [{"role": "user"|"assistant", "content": str}, ...]).
        """

    @abstractmethod
    def build_pdf_parts(
        self,
        prompt_text: str,
        pdf_bytes: bytes,
        mime_type: str = "application/pdf",
    ) -> list:
        """Build provider-native parts for an inline-PDF multimodal call.

        Returns a list whose shape is opaque to the caller — passed back to
        `json(parts=...)` as-is.
        """

    @abstractmethod
    def tool_call(
        self,
        system_text: str,
        user_message: str,
        tools: List[Dict[str, Any]],
        execute_tool,
        max_rounds: int = 6,
        deadline_seconds: float = 25.0,
        temperature: float = 0.35,
        max_tokens: int = 2800,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Run a ReAct-style tool-calling loop.

        Args:
            system_text:       Single system message (RAG context, rules, etc).
            user_message:      The user's turn.
            tools:             List of tool specs in the format used by
                               `_build_function_declarations()` —
                               [{"name", "description", "parameters" (JSON Schema)}].
            execute_tool:      Callable(tool_name, args_dict) -> (result_dict, agent_tag)
                               that runs the tool and returns its result.
            max_rounds:        Max orchestrator rounds.
            deadline_seconds:  Wall-clock soft deadline.

        Returns:
            (final_text, pipeline_log)
        """

    def lazy_client(self):
        """Optional — kept for backward compat with `get_gemini_client()`.

        Default impl returns None; only Gemini provider needs an exposed client.
        """
        return None
