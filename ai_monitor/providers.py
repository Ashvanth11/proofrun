"""Pluggable model backends.

The analysis, judge, and synthesis modules all talk to a client through a
narrow surface - `client.messages.parse(...)` and `client.messages.create(...)`.
The Anthropic SDK satisfies it directly. `OllamaClient` mimics the same surface
over a local Ollama server, so a local model can be swapped in for development
without touching any calling code.

Local models are for iterating on prompts and control flow for free. They are
not a substitute for the eval judge or for any number that goes in a writeup -
see docs on the calibration gap.
"""

import json
import logging
from typing import Any, Optional

import anthropic
import httpx
from pydantic import BaseModel

log = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "llama3.1"

# Local inference is free; keeping the shape lets cost logging stay uniform.
LOCAL_PRICING = {"input": 0.0, "output": 0.0}


class _Usage:
    """Mirrors the token-count fields the Anthropic SDK returns."""

    def __init__(self, input_tokens: int, output_tokens: int):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _TextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _ParsedResponse:
    def __init__(self, parsed_output: Any, usage: _Usage):
        self.parsed_output = parsed_output
        self.usage = usage


class _CreateResponse:
    def __init__(self, text: str, usage: _Usage):
        self.content = [_TextBlock(text)]
        self.usage = usage
        self.stop_reason = "end_turn"


class OllamaMessages:
    """The `.messages` namespace of an Ollama-backed client."""

    def __init__(self, base_url: str, model: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def _chat(
        self,
        system: str,
        messages: list[dict],
        schema: Optional[dict] = None,
        options: Optional[dict] = None,
    ) -> tuple[str, _Usage]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + messages,
            "stream": False,
            "options": options or {"temperature": 0},
        }
        if schema:
            payload["format"] = schema

        try:
            response = httpx.post(
                f"{self.base_url}/api/chat", json=payload, timeout=self.timeout
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OllamaError(f"Ollama request failed: {exc}") from exc

        data = response.json()
        usage = _Usage(
            input_tokens=data.get("prompt_eval_count", 0),
            output_tokens=data.get("eval_count", 0),
        )
        return data["message"]["content"], usage

    def parse(
        self,
        model: str = "",  # ignored; the client is constructed with its model
        max_tokens: int = 1024,
        system: str = "",
        messages: Optional[list[dict]] = None,
        output_format: Optional[type[BaseModel]] = None,
        **_ignored,
    ) -> _ParsedResponse:
        if output_format is None:
            raise ValueError("output_format is required for parse()")

        schema = output_format.model_json_schema()
        text, usage = self._chat(system, messages or [], schema=schema)

        try:
            parsed = output_format.model_validate_json(text)
        except Exception as exc:
            # Ollama constrains generation to the schema, but a small model can
            # still emit values the pydantic validators reject (out-of-range
            # scores, say). Surface it as the same failure mode either backend
            # would produce rather than a bare pydantic error.
            raise OllamaError(
                f"{self.model} returned output failing schema validation: {exc}\n"
                f"raw: {text[:400]}"
            ) from exc

        return _ParsedResponse(parsed, usage)

    def create(
        self,
        model: str = "",
        max_tokens: int = 4096,
        system: str = "",
        messages: Optional[list[dict]] = None,
        **_ignored,
    ) -> _CreateResponse:
        text, usage = self._chat(system, messages or [])
        return _CreateResponse(text, usage)


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    """Drop-in stand-in for anthropic.Anthropic over a local Ollama server."""

    def __init__(
        self,
        model: str = DEFAULT_OLLAMA_MODEL,
        base_url: str = OLLAMA_URL,
        timeout: float = 300.0,
    ):
        self.model = model
        self.base_url = base_url
        self.messages = OllamaMessages(base_url, model, timeout)

    def health_check(self) -> None:
        """Fail early with an actionable message rather than mid-run."""
        try:
            response = httpx.get(f"{self.base_url}/api/tags", timeout=5.0)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OllamaError(
                f"Cannot reach Ollama at {self.base_url}. Start it with `ollama serve`."
            ) from exc

        available = {m["name"] for m in response.json().get("models", [])}
        # Ollama treats a bare name as ":latest".
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        if wanted not in available:
            raise OllamaError(
                f"Model '{self.model}' is not pulled. Run `ollama pull {self.model}`. "
                f"Available: {', '.join(sorted(available)) or 'none'}"
            )


def build_client(
    provider: str,
    api_key: str = "",
    ollama_model: str = DEFAULT_OLLAMA_MODEL,
):
    """Construct a client for the named provider.

    Returns (client, model_name) - the model name is what gets recorded against
    stored analyses, so results from different backends stay distinguishable.
    """
    if provider == "ollama":
        client = OllamaClient(model=ollama_model)
        client.health_check()
        log.info("using local Ollama model %s (no API cost)", ollama_model)
        return client, f"ollama/{ollama_model}"

    if provider == "anthropic":
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is not set. Add it to .env, or use "
                "--provider ollama to run against a local model."
            )
        return anthropic.Anthropic(api_key=api_key), None

    raise ValueError(f"unknown provider: {provider}")
