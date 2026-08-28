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


class _ToolUseBlock:
    """Mirrors an Anthropic tool_use content block.

    The agent loop is written against the Anthropic message shape; this lets
    Ollama's differently-shaped tool calls flow through the same code.
    """

    type = "tool_use"

    def __init__(self, id: str, name: str, input: dict):
        self.id = id
        self.name = name
        self.input = input


class _ParsedResponse:
    def __init__(self, parsed_output: Any, usage: _Usage):
        self.parsed_output = parsed_output
        self.usage = usage


class _CreateResponse:
    def __init__(self, content: list, usage: _Usage, stop_reason: str = "end_turn"):
        self.content = content
        self.usage = usage
        self.stop_reason = stop_reason


def _anthropic_tools_to_ollama(tools: list[dict]) -> list[dict]:
    """Translate Anthropic tool definitions into Ollama's function-call shape."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        }
        for t in tools
    ]


def _anthropic_messages_to_ollama(messages: list[dict]) -> list[dict]:
    """Flatten Anthropic content-block messages into Ollama's flat shape.

    Anthropic carries tool calls and their results as content blocks inside
    assistant/user messages; Ollama expects assistant messages with a
    `tool_calls` list and separate `role: "tool"` messages. Translating here
    keeps that difference out of the agent loop.
    """
    out: list[dict] = []
    for message in messages:
        content = message.get("content")

        if isinstance(content, str):
            out.append({"role": message["role"], "content": content})
            continue

        text_parts: list[str] = []
        tool_calls: list[dict] = []
        tool_results: list[dict] = []

        for block in content or []:
            btype = block.get("type") if isinstance(block, dict) else block.type

            if btype == "text":
                text_parts.append(
                    block["text"] if isinstance(block, dict) else block.text
                )
            elif btype == "tool_use":
                name = block["name"] if isinstance(block, dict) else block.name
                args = block["input"] if isinstance(block, dict) else block.input
                tool_calls.append({"function": {"name": name, "arguments": args}})
            elif btype == "tool_result":
                body = block.get("content") if isinstance(block, dict) else None
                tool_results.append(
                    {
                        "role": "tool",
                        "content": body if isinstance(body, str) else json.dumps(body),
                    }
                )

        if message["role"] == "assistant" and (text_parts or tool_calls):
            entry: dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(text_parts),
            }
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
        elif text_parts:
            out.append({"role": message["role"], "content": "\n".join(text_parts)})

        out.extend(tool_results)

    return out


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
        tools: Optional[list[dict]] = None,
        options: Optional[dict] = None,
    ) -> tuple[dict, _Usage]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + messages,
            "stream": False,
            "options": options or {"temperature": 0},
        }
        if schema:
            payload["format"] = schema
        if tools:
            payload["tools"] = _anthropic_tools_to_ollama(tools)

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
        return data["message"], usage

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
        message, usage = self._chat(system, messages or [], schema=schema)
        text = message.get("content", "")

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
        tools: Optional[list[dict]] = None,
        **_ignored,
    ) -> _CreateResponse:
        message, usage = self._chat(
            system,
            _anthropic_messages_to_ollama(messages or []),
            tools=tools,
        )

        blocks: list = []
        if message.get("content"):
            blocks.append(_TextBlock(message["content"]))

        # Ollama returns no call ids; the loop needs one to pair results back,
        # so synthesize a stable per-response id.
        for i, call in enumerate(message.get("tool_calls") or []):
            func = call.get("function", {})
            args = func.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            blocks.append(
                _ToolUseBlock(id=f"call_{i}", name=func.get("name", ""), input=args)
            )

        stop_reason = (
            "tool_use"
            if any(b.type == "tool_use" for b in blocks)
            else "end_turn"
        )
        return _CreateResponse(blocks or [_TextBlock("")], usage, stop_reason)


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
