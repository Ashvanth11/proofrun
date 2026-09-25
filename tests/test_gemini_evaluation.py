import json

import httpx
import pytest

import evaluate_gemini as ev
from app import load_examples
from ai_monitor.agent.investigate import Investigation


def result(parts):
    return {"candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": parts}}]}


def test_report_is_validated_and_request_has_no_paid_search():
    case = load_examples()[0]
    report = Investigation(question=case["question"], verdict="inconclusive", summary="The evidence is limited.", limitations=["Only recorded evidence was reviewed."])
    def handler(request):
        body = json.loads(request.content)
        assert "tools" not in body
        assert body["generationConfig"]["maxOutputTokens"] == 4096
        assert request.headers["x-goog-api-key"] == "fake"
        return httpx.Response(200, json=result([{"text": report.model_dump_json()}]))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        answer = ev.evaluate_case(client, "fake", case)
    assert answer["checks"]["question_preserved"]
    assert "pending" in answer["human_review"]


def test_rate_limit_does_not_retry_or_expose_key():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"error": "secret"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ev.EvaluationError, match="HTTP 429") as error:
            ev.request(client, "secret", {})
    assert len(calls) == 1
    assert "secret" not in str(error.value)


def test_probe_preserves_thought_signature_and_never_executes_model_commands():
    calls = []
    signature = {"functionCall": {"name": "lookup_repository", "args": {"repo": "example/demo"}, "id": "c1"}, "thoughtSignature": "test-signature"}
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(200, json=result([signature]))
        assert body["contents"][1]["parts"] == [signature]
        assert body["contents"][2]["parts"][0]["functionResponse"]["id"] == "c1"
        return httpx.Response(200, json=result([{"text": "It converts CSV files into charts."}]))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert ev.tool_probe(client, "fake")["returned_text"]
    assert len(calls) == 2


def test_selected_flash_model_is_used_without_changing_default():
    def handler(request):
        assert request.url.path.endswith("/gemini-3.8-flash:generateContent")
        return httpx.Response(200, json={"ok": True})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert ev.request(client, "fake", {}, model="gemini-3.8-flash") == {"ok": True}
    assert ev.MODEL == "gemini-3.1-flash-lite"
