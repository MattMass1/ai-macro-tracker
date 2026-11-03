"""Terra protocol: mocked HTTP only, no credentials or external providers."""
import json
import httpx
import pytest
from food_agent.model_adapter import ModelInterpreter, InterpreterError

TURN = {"schema_version": "food-agent/1", "actions": [{"type": "not_food"}]}

def completed(text=None):
    return {"status": "completed", "output": [{"type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text or json.dumps(TURN)}]}]}

async def test_terra_payload_auth_endpoint_and_precedence(monkeypatch, caplog):
    import food_agent.model_adapter as adapter
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=completed())
    client = httpx.AsyncClient
    monkeypatch.setattr(adapter.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-standard")
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "synthetic-legacy")
    monkeypatch.setenv("COACH_MODEL", "unrelated-coach")
    monkeypatch.delenv("FOOD_AGENT_MODEL", raising=False)
    turn = await ModelInterpreter()("hi", {})
    assert turn.actions[0].type == "not_food"
    (request,) = calls
    assert str(request.url) == "https://api.openai.com/v1/responses"
    assert request.headers["Authorization"] == "Bearer synthetic-standard"
    payload = json.loads(request.content)
    assert payload["model"] == "gpt-6.1-terra"
    assert payload["store"] is False and payload["reasoning"] == {"effort": "none"}
    assert payload["max_output_tokens"] == 2000
    assert payload["text"]["format"]["strict"] is True
    assert payload["text"]["format"]["schema"]["additionalProperties"] is False
    assert "synthetic-standard" not in caplog.text

@pytest.mark.parametrize("body", [
    {"status": "incomplete", "output": []}, {"status": "failed", "error": {"message": "x"}},
    {"status": "completed", "output": []},
    completed('{"actions":'), completed('{"schema_version":"food-agent/1","actions":[{"type":"not_food","confirmed":true}]}'),
    {"status": "completed", "output": [{"type": "message", "status": "completed", "role": "assistant",
      "content": [{"type": "refusal", "refusal": "no"}]}]},
    {"status": "completed", "output": completed()["output"] * 2},
    {"status": "completed", "output": [{"type": "function_call", "arguments": "{}"}]},
    {"status": "completed", "error": {"message": "bad"}, "output": completed()["output"]},
])
async def test_terra_rejects_noncomplete_or_ambiguous_output(body):
    async def post(token, payload):
        return httpx.Response(200, json=body, request=httpx.Request("POST", "https://fake.invalid"))
    with pytest.raises(InterpreterError):
        await ModelInterpreter(post=post, token="synthetic")("hi", {})

async def test_terra_missing_key_no_call_and_legacy_compatibility(monkeypatch):
    calls = []
    async def post(token, payload):
        calls.append(token)
        return httpx.Response(200, json=completed(), request=httpx.Request("POST", "https://fake.invalid"))
    for name in ("OPENAI_API_KEY", "OPENAI_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(InterpreterError):
        await ModelInterpreter(post=post)("hi", {})
    assert not calls
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "synthetic-compat")
    await ModelInterpreter(post=post)("hi", {})
    assert calls == ["synthetic-compat"]

async def test_terra_adapter_is_exercised_through_real_canvas_turn():
    from test_food_agent_canvas import Rig, HALF_QP
    from auth import bind_user, reset_user
    from uuid import uuid4
    from food_agent.contracts import AgentTurn
    calls = []
    async def post(token, payload):
        calls.append(payload)
        body = json.dumps(HALF_QP if "schema_version" in HALF_QP else
                          {"schema_version":"food-agent/1", "actions":[HALF_QP]})
        return httpx.Response(200,json=completed(body),request=httpx.Request("POST","https://fake.invalid"))
    scope = bind_user(uuid4())
    try:
        rig = Rig()
        rig.agent.interpret = ModelInterpreter(post=post,token="synthetic")
        first = await rig.say("half a quarter pounder")
        assert "Log it?" in first["reply"] and not rig.drafts.entries
        final = await rig.say("yes")
        assert "Logged" in final["reply"] and len(rig.drafts.entries) == 1
        assert len(calls) == 1 and calls[0]["model"] == "gpt-6.1-terra"
    finally:
        reset_user(scope)
