import json

import httpx
import pytest
from pydantic import BaseModel, Field

from ai_monitor import providers
from ai_monitor.analysis.analyzer import Usage
from ai_monitor.providers import OllamaClient, OllamaError


class Verdict(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    reason: str


def mock_transport(handler):
    """Build an OllamaClient whose HTTP calls are intercepted."""
    return httpx.MockTransport(handler)


@pytest.fixture
def patch_post(monkeypatch):
    """Capture the payload sent to Ollama and return a canned response."""
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        return httpx.Response(
            200,
            json={
                "message": {"content": captured.get("content", '{"score":0.7,"reason":"ok"}')},
                "prompt_eval_count": 120,
                "eval_count": 45,
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(providers.httpx, "post", fake_post)
    return captured


def test_parse_returns_validated_model(patch_post):
    client = OllamaClient(model="llama3.1")
    response = client.messages.parse(
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        output_format=Verdict,
    )
    assert isinstance(response.parsed_output, Verdict)
    assert response.parsed_output.score == 0.7
    assert response.usage.input_tokens == 120
    assert response.usage.output_tokens == 45


def test_parse_sends_json_schema(patch_post):
    OllamaClient(model="llama3.1").messages.parse(
        system="sys", messages=[{"role": "user", "content": "hi"}], output_format=Verdict
    )
    payload = patch_post["payload"]
    assert payload["format"]["properties"]["score"]["type"] == "number"
    assert payload["stream"] is False
    assert payload["messages"][0] == {"role": "system", "content": "sys"}


def test_create_returns_text_block(patch_post):
    patch_post["content"] = "## A brief\n\nSome markdown."
    response = OllamaClient(model="llama3.1").messages.create(
        system="sys", messages=[{"role": "user", "content": "hi"}]
    )
    assert response.content[0].type == "text"
    assert response.content[0].text == "## A brief\n\nSome markdown."


def test_schema_violating_output_raises_ollama_error(patch_post):
    """A small model can emit an out-of-range value; surface it as our error."""
    patch_post["content"] = '{"score": 4.2, "reason": "way out of range"}'
    with pytest.raises(OllamaError, match="failing schema validation"):
        OllamaClient(model="llama3.1").messages.parse(
            system="s", messages=[], output_format=Verdict
        )


def test_malformed_json_raises_ollama_error(patch_post):
    patch_post["content"] = "not json at all"
    with pytest.raises(OllamaError):
        OllamaClient(model="llama3.1").messages.parse(
            system="s", messages=[], output_format=Verdict
        )


def test_parse_requires_output_format():
    with pytest.raises(ValueError, match="output_format is required"):
        OllamaClient().messages.parse(system="s", messages=[])


def test_connection_failure_is_actionable(monkeypatch):
    def fail(*a, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(providers.httpx, "get", fail)
    with pytest.raises(OllamaError, match="ollama serve"):
        OllamaClient().health_check()


def test_missing_model_names_the_pull_command(monkeypatch):
    def ok(url, timeout=None):
        return httpx.Response(
            200,
            json={"models": [{"name": "llama3.1:latest"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(providers.httpx, "get", ok)
    with pytest.raises(OllamaError, match="ollama pull qwen3:8b"):
        OllamaClient(model="qwen3:8b").health_check()


def test_bare_model_name_matches_latest_tag(monkeypatch):
    def ok(url, timeout=None):
        return httpx.Response(
            200,
            json={"models": [{"name": "llama3.1:latest"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(providers.httpx, "get", ok)
    OllamaClient(model="llama3.1").health_check()  # must not raise


def test_build_client_requires_key_for_anthropic():
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        providers.build_client("anthropic", api_key="")


def test_build_client_rejects_unknown_provider():
    with pytest.raises(ValueError, match="unknown provider"):
        providers.build_client("openai")


def test_local_runs_report_zero_cost():
    usage = Usage(input_tokens=500_000, output_tokens=100_000, model="ollama/llama3.1")
    assert usage.cost_usd == 0.0


def test_model_name_records_backend():
    """Stored analyses must stay distinguishable between backends."""
    _, name = providers.build_client("anthropic", api_key="sk-test")
    assert name is None  # falls back to the module default
