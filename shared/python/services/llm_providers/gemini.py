"""Gemini provider — Google AI Studio via the `google-genai` SDK.

Restored from the pre-migration implementation (portfolio commit 4479492^)
and adapted to the current `LLMProvider` contract. Interchangeable with
`ClaudeProvider`: selected by `LLM_PROVIDER=gemini`.

Isolation: this module imports `google.genai` lazily (inside methods), so a
process running Claude never loads the Gemini SDK, and vice versa. Nothing
here touches AWS Bedrock.

Request shape (single-shot JSON):
    client.models.generate_content(
        model=...,
        contents=[types.Content(role="user", parts=[...])],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=...,          # OpenAPI-style, UPPERCASE types
            system_instruction=...,       # trusted instructions
            temperature=..., max_output_tokens=...,
        ),
    )

Default models (override via env): 2.5 Flash for FLASH/PREVIEW (fast
structured extraction and repair), 2.5 Pro for PRO (tailoring, JD analysis,
scoring).
"""
import json
import logging
import os
import random
import time
from typing import Any, Dict, List, Optional, Tuple

from .base import LLMProvider, LLMRetriesExhaustedError
from ._json_repair import try_parse_json, clean_json_response

logger = logging.getLogger(__name__)


_MAX_ORCHESTRATOR_ROUNDS = 6
_ORCHESTRATOR_DEADLINE_SECONDS = 25.0


def _model_id(env_key: str, default: str) -> str:
    """Read a model ID from env or fall back to default. Mirrors the same
    helper in claude.py so both providers are tuned the same way."""
    return os.getenv(env_key, default).strip() or default


class GeminiProvider(LLMProvider):
    """Gemini implementation of the shared LLM provider contract.

    Tier strategy mirrors Claude's:
      FLASH   → 2.5 Flash — cheap, fast. Parser, verifiers, classifiers.
      PRO     → 2.5 Pro   — tailoring, JD analysis, scoring, intelligence.
      PREVIEW → 2.5 Flash — JSON repair tier; small outputs, cheap is fine.

    Each is independently overridable:
      GEMINI_FLASH_MODEL_ID / GEMINI_PRO_MODEL_ID / GEMINI_PREVIEW_MODEL_ID
    """

    FLASH = _model_id("GEMINI_FLASH_MODEL_ID", "gemini-2.5-flash")
    PRO = _model_id("GEMINI_PRO_MODEL_ID", "gemini-2.5-pro")
    PREVIEW = _model_id("GEMINI_PREVIEW_MODEL_ID", "gemini-2.5-flash")

    # Per-model output ceilings. Gemini 2.5 allows far more than Claude
    # (65535 vs Sonnet's 16384), so callers that ask for 24000 — e.g. the
    # ATS keyword pass in content_augmenter — are served in full here rather
    # than being clamped. Do NOT copy Claude's ceilings.
    _MAX_OUTPUT_TOKENS = {
        "gemini-2.5-flash": 65535,
        "gemini-2.5-pro": 65535,
        "gemini-2.5-flash-lite": 64000,
    }
    _DEFAULT_MAX_OUTPUT_TOKENS = 8192

    def __init__(self):
        self._client = None

    # ------------------------------------------------------------------
    # Client lifecycle
    # ------------------------------------------------------------------

    def lazy_client(self):
        """Lazy-load the Gemini client so non-AI requests don't pay the
        cold-start cost of importing the SDK."""
        if self._client is not None:
            return self._client

        from utils.config import _get_config_value
        api_key = _get_config_value("GEMINI_API_KEY", "")
        if not api_key:
            # Fail loudly: silently degrading would look like an AI outage.
            raise RuntimeError(
                "GEMINI_API_KEY is not configured but LLM_PROVIDER=gemini. "
                "Set it in .env locally, or in SSM for deployed environments."
            )

        from google import genai
        self._client = genai.Client(api_key=api_key)
        return self._client

    # ------------------------------------------------------------------
    # Schema conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _to_openapi_schema(spec) -> dict:
        """Convert the project's Python schema DSL to Gemini's OpenAPI shape.

        Gemini's `response_schema` wants UPPERCASE type names; Claude's
        tool-use wants lowercase JSON Schema (see
        `_json_repair.python_schema_to_json_schema`). Both converters must
        coexist — they are not interchangeable.

        Note `bool` is not part of the DSL and falls through to STRING, which
        matches the Claude converter's behavior exactly. Callers therefore
        compare stringly (`str(v).lower() == "true"`); keep it that way so a
        provider swap can't change how a field parses.
        """
        if spec is str:
            return {"type": "STRING"}
        if spec is int:
            return {"type": "INTEGER"}
        if spec is float:
            return {"type": "NUMBER"}
        if isinstance(spec, list) and len(spec) == 1:
            return {"type": "ARRAY", "items": GeminiProvider._to_openapi_schema(spec[0])}
        if isinstance(spec, dict) and "_dict_of" in spec:
            return {
                "type": "OBJECT",
                "additionalProperties": GeminiProvider._to_openapi_schema(spec["_dict_of"]),
            }
        if isinstance(spec, dict):
            props = {k: GeminiProvider._to_openapi_schema(v) for k, v in spec.items()}
            return {"type": "OBJECT", "properties": props}
        return {"type": "STRING"}

    # Kept as a passthrough so `claude_client.clean_json_response` resolves to
    # a real callable under this provider too (the facade re-exports it via
    # getattr on the active provider).
    clean_json_response = staticmethod(clean_json_response)

    # ------------------------------------------------------------------
    # Low-level call with retry
    # ------------------------------------------------------------------

    def _clamp_max_tokens(self, requested: int, model: str) -> int:
        """Clamp to the model's output ceiling.

        Exceeding it is rejected outright by the API, and a silently truncated
        response loses whole résumé sections that the repair pass cannot
        reconstruct.
        """
        ceiling = self._MAX_OUTPUT_TOKENS.get(model, self._DEFAULT_MAX_OUTPUT_TOKENS)
        if requested > ceiling:
            logger.info("Clamping max_tokens %d → %d for model %s", requested, ceiling, model)
            return ceiling
        return requested

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """Only retry transient failures.

        The original implementation retried a bare `except Exception`, so a
        malformed request or a bad API key burned all three attempts (plus
        backoff) before surfacing the same error. Retry 429/5xx and transport
        errors; fail fast on 4xx, which will never succeed on retry.
        """
        status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if isinstance(status, int):
            return status == 429 or status >= 500

        msg = str(exc).lower()
        if any(t in msg for t in ("429", "resource_exhausted", "quota", "rate limit",
                                  "unavailable", "deadline", "timeout", "internal error")):
            return True
        if any(t in msg for t in ("api key not valid", "api_key_invalid", "permission",
                                  "invalid_argument", "not found", "400", "401", "403", "404")):
            return False
        # Unknown (often a transport hiccup) — one more try is cheap.
        return True

    @staticmethod
    def _retry_after_seconds(exc: Exception, attempt: int) -> float:
        """Honor a server-provided retry delay when present, else exponential
        backoff with jitter (avoids synchronized retries across concurrent
        Lambdas hammering the same per-minute quota)."""
        for attr in ("retry_delay", "retry_after"):
            val = getattr(exc, attr, None)
            secs = getattr(val, "seconds", val)
            if isinstance(secs, (int, float)) and secs > 0:
                return min(float(secs), 30.0)
        return (2 ** attempt) + random.uniform(0, 0.5)

    def _generate(self, *, model: str, contents, config, max_retries: int):
        """Single entry point for every Gemini API call, with retry policy."""
        client = self.lazy_client()

        last_error = None
        for attempt in range(max_retries + 1):
            try:
                return client.models.generate_content(
                    model=model, contents=contents, config=config,
                )
            except Exception as e:
                last_error = e
                if attempt == max_retries or not self._is_retryable(e):
                    break
                wait = self._retry_after_seconds(e, attempt)
                logger.warning(
                    "Gemini API error (attempt %d/%d) model=%s, retrying in %.1fs: %s",
                    attempt + 1, max_retries + 1, model, wait, e,
                )
                time.sleep(wait)

        raise LLMRetriesExhaustedError(
            f"AI service unavailable after {max_retries + 1} attempts: {last_error}"
        )

    # ------------------------------------------------------------------
    # JSON mode
    # ------------------------------------------------------------------

    def json(
        self,
        prompt: Optional[str] = None,
        max_tokens: int = 8192,
        temperature: float = 0.3,
        model: Optional[str] = None,
        max_retries: int = 2,
        schema: Optional[dict] = None,
        parts: Optional[list] = None,
        system: Optional[str] = None,
    ) -> dict:
        from google.genai import types

        model = model or self.PRO
        max_tokens = self._clamp_max_tokens(max_tokens, model)

        # `parts` (from build_pdf_parts) is already in Gemini's native shape.
        user_parts = parts if parts else [types.Part.from_text(text=prompt or "")]

        config_kwargs: Dict[str, Any] = {
            "response_mime_type": "application/json",
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if schema is not None:
            config_kwargs["response_schema"] = self._to_openapi_schema(schema)
        # Trusted instructions stay out of the untrusted user message. The
        # pre-migration provider lacked this on the JSON path; every caller
        # now relies on it (guarded_system), so it is not optional.
        if system:
            config_kwargs["system_instruction"] = system

        response = self._generate(
            model=model,
            contents=[types.Content(role="user", parts=user_parts)],
            config=types.GenerateContentConfig(**config_kwargs),
            max_retries=max_retries,
        )

        raw = (response.text or "").strip()
        if not raw:
            raise ValueError("AI returned an empty response. Please try again.")

        try:
            result = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("Gemini JSON decode failed, attempting repair. Raw: %s", raw[:500])
            result = try_parse_json(raw)
            if result is None:
                raise ValueError(f"AI returned invalid format: {e}")

        # Python-side coercion fills defaults and fixes types — the same pass
        # Claude's path runs, so downstream code sees an identical shape.
        if schema is not None:
            try:
                from schemas.resume_schemas import validate_and_coerce
                result = validate_and_coerce(result, schema)
            except ValueError as e:
                logger.warning("Python-side coercion failed: %s", e)
                raise

        return result

    # ------------------------------------------------------------------
    # Plain text generation
    # ------------------------------------------------------------------

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
        """Conversational text generation.

        `history` shape: [{"role": "user"|"assistant"|"model", "content": str}].
        Gemini uses "model" for assistant turns; "assistant" is mapped to it
        (Claude's provider maps the other direction, so callers can pass
        either spelling to either provider).
        """
        from google.genai import types

        model = model or self.FLASH
        max_tokens = self._clamp_max_tokens(max_tokens, model)

        contents: List[Any] = []
        for turn in (history or []):
            role = turn.get("role", "user")
            if role == "assistant":
                role = "model"
            elif role not in ("user", "model"):
                role = "user"
            content = (turn.get("content") or "")[:8000]
            if not content:
                continue
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=content)]))

        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=prompt)]))

        config_kwargs: Dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if system:
            config_kwargs["system_instruction"] = system

        response = self._generate(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(**config_kwargs),
            max_retries=max_retries,
        )
        return (response.text or "").strip()

    # ------------------------------------------------------------------
    # Multimodal PDF
    # ------------------------------------------------------------------

    def build_pdf_parts(
        self,
        prompt_text: str,
        pdf_bytes: bytes,
        mime_type: str = "application/pdf",
    ) -> list:
        """Build Gemini content parts for an inline document + prompt.

        The returned list is opaque to the caller — `resume_parser` passes it
        straight back via `json(parts=...)`, which is what lets the same
        caller drive Claude's `document` blocks and Gemini's inline bytes.

        Unlike Claude (PDF-only), Gemini accepts DOCX and other MIME types
        inline, so no pre-extraction is required here.
        """
        from google.genai import types

        # Text-only fallback: caller signals this with empty bytes / text/plain.
        if not pdf_bytes or mime_type == "text/plain":
            return [types.Part.from_text(text=prompt_text)]
        return [
            types.Part.from_bytes(data=pdf_bytes, mime_type=mime_type),
            types.Part.from_text(text=prompt_text),
        ]

    # ------------------------------------------------------------------
    # Tool calling (ReAct loop)
    # ------------------------------------------------------------------

    def tool_call(
        self,
        system_text: str,
        user_message: str,
        tools: List[Dict[str, Any]],
        execute_tool,
        max_rounds: int = _MAX_ORCHESTRATOR_ROUNDS,
        deadline_seconds: float = _ORCHESTRATOR_DEADLINE_SECONDS,
        temperature: float = 0.35,
        max_tokens: int = 2800,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Multi-round function-calling loop.

        Callers pass tool specs as {"name", "description", "parameters"},
        which is already Gemini's `function_declarations` shape (Claude's
        provider translates `parameters` → `input_schema` instead).
        """
        from google.genai import types

        pipeline_log: List[Dict[str, Any]] = []
        start_ts = time.time()

        tool_config = types.Tool(function_declarations=tools)
        contents: List[Any] = [
            types.Content(role="user", parts=[
                types.Part.from_text(text=f"USER_MESSAGE:\n{user_message}"),
            ]),
        ]

        # Prefer FLASH; escalate to PRO once if a round fails. This is a
        # within-provider tier fallback — it never crosses to Bedrock.
        model = self.FLASH
        text_parts: List[str] = []

        for round_num in range(max_rounds):
            if (time.time() - start_ts) > deadline_seconds:
                return (
                    "I started processing your request but hit a time limit. "
                    "Try a narrower follow-up.",
                    pipeline_log,
                )

            config = types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
                system_instruction=system_text,
                tools=[tool_config],
            )

            try:
                response = self._generate(
                    model=model, contents=contents, config=config, max_retries=1,
                )
            except Exception as e:
                logger.exception("Orchestrator round %d failed: %s", round_num, e)
                if model == self.FLASH and self.PRO != self.FLASH:
                    model = self.PRO
                    try:
                        response = self._generate(
                            model=model, contents=contents, config=config, max_retries=1,
                        )
                    except Exception as e2:
                        logger.exception("Orchestrator fallback failed: %s", e2)
                        return ("I could not generate a response — try again.", pipeline_log)
                else:
                    return ("I could not generate a response — try again.", pipeline_log)

            candidate = response.candidates[0] if response.candidates else None
            if not candidate or not candidate.content or not candidate.content.parts:
                return ("I could not generate a response — try again.", pipeline_log)

            text_parts = []
            function_call_parts = []
            for part in candidate.content.parts:
                if getattr(part, "function_call", None):
                    function_call_parts.append(part)
                elif getattr(part, "text", None):
                    text_parts.append(part.text)

            if not function_call_parts:
                return (
                    "\n".join(text_parts) if text_parts else "I could not generate a response.",
                    pipeline_log,
                )

            # Append the model turn verbatim so function-call IDs line up.
            contents.append(candidate.content)

            function_response_parts = []
            for part in function_call_parts:
                if (time.time() - start_ts) > deadline_seconds:
                    return (
                        "I completed part of your request but hit a time limit while running tools.",
                        pipeline_log,
                    )

                fc = part.function_call
                tool_name = fc.name
                tool_args = dict(fc.args) if fc.args else {}

                logger.info(
                    "Orchestrator round %d: calling tool %s(%s)",
                    round_num, tool_name, list(tool_args.keys()),
                )
                try:
                    result, agent_tag = execute_tool(tool_name, tool_args)
                except Exception as e:
                    # Matches Claude's behavior: report the failure back to the
                    # model as a tool result instead of aborting the whole turn.
                    logger.exception("Tool %s execution failed: %s", tool_name, e)
                    result, agent_tag = {"error": str(e)}, "error"

                pipeline_log.append({
                    "agent": agent_tag,
                    "label": tool_name,
                    "summary": f"Called {tool_name} with {list(tool_args.keys())}",
                    "round": round_num,
                })

                result_str = json.dumps(result, default=str)[:8000]
                function_response_parts.append(
                    types.Part.from_function_response(
                        name=tool_name,
                        response={"result": result_str},
                    )
                )

            contents.append(types.Content(role="user", parts=function_response_parts))

        return (
            "\n".join(text_parts) if text_parts else "Reached maximum tool-calling rounds.",
            pipeline_log,
        )
