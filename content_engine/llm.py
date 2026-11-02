"""A small HTTP adapter for Nebius Token Factory chat completions."""

import httpx

from .config import Settings
from .prompts import WRITING_INSTRUCTIONS
from .tracing import RunTrace


class NebiusLLM:
    def __init__(self, settings: Settings, trace: RunTrace | None = None) -> None:
        self.settings = settings
        self.trace = trace or RunTrace()

    def generate(self, prompt: str) -> str:
        """Return completion text; WritingNode performs local output validation."""
        return self.complete(WRITING_INSTRUCTIONS, prompt, name="writing")

    def complete(self, system: str, prompt: str, *, name: str = "reasoning") -> str:
        """Use a role-specific system prompt without changing the provider contract."""
        payload = {
            "model": self.settings.nebius_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": self.settings.nebius_max_tokens,
            "temperature": 0.7 if name == "writing" else 0.2,
            "top_p": 0.8,
            "presence_penalty": 0.5,
        }
        with self.trace.span("nebius.chat_completions", "llm", inputs={"purpose": name, **payload}) as event:
            return self._generate(payload, event)

    def _generate(self, payload: dict, event: dict) -> str:
        try:
            response = httpx.post(
                f"{self.settings.nebius_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.nebius_api_key}"},
                json=payload,
                timeout=self.settings.nebius_timeout_seconds,
            )
            event["metadata"] = {"http_status": response.status_code}
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise RuntimeError(
                f"Nebius request failed (HTTP {error.response.status_code}). "
                "Check your key, model ID, JSON-mode support, and quota."
            ) from None
        except httpx.TimeoutException:
            raise RuntimeError(
                f"Nebius request timed out after {self.settings.nebius_timeout_seconds}s. "
                "Increase NEBIUS_TIMEOUT_SECONDS or shorten the requested drafts."
            ) from None
        except httpx.RequestError:
            raise RuntimeError("Nebius request failed. Check connectivity or timeout.") from None

        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
            content = message.get("content")
            event["outputs"] = {"content": content}
            event["metadata"].update({
                "model": body.get("model", self.settings.nebius_model),
                "finish_reason": choice.get("finish_reason"),
                "usage": body.get("usage"),
            })
            if message.get("refusal"):
                raise RuntimeError("Nebius refused the writing request; no draft was accepted.")
            if choice.get("finish_reason") == "length":
                raise RuntimeError(
                    f"Nebius reached the {self.settings.nebius_max_tokens}-token output limit. "
                    "Increase NEBIUS_MAX_TOKENS or shorten the requested drafts."
                )
            if choice.get("finish_reason") != "stop":
                raise RuntimeError("Nebius stopped without a completed answer; no draft was accepted.")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Empty completion")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise RuntimeError("Nebius returned an invalid completion response.") from None
        return content
