import io
import json

import pytest

from media_tools.integrations import openrouter


class _Response(io.BytesIO):
    def __init__(self, payload, status=200):
        super().__init__(json.dumps(payload).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _reply(content: dict, usage=None):
    return {
        "choices": [{"message": {"content": json.dumps(content)}}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5},
    }


def test_chat_sends_deterministic_settings_and_returns_usage():
    seen = {}

    def opener(request, timeout=None):
        seen["body"] = json.loads(request.data)
        seen["auth"] = request.headers.get("Authorization")
        return _Response(_reply({"books": []}))

    data, usage = openrouter.chat(
        [{"role": "user", "content": "hi"}],
        model="openai/gpt-4o-mini",
        api_key="k-123",
        opener=opener,
    )
    assert data == {"books": []}
    assert usage.prompt_tokens == 10
    assert seen["body"]["temperature"] == 0
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["auth"] == "Bearer k-123"


def test_chat_retries_a_rate_limit_then_succeeds(monkeypatch):
    monkeypatch.setattr(openrouter.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def opener(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            import urllib.error

            raise urllib.error.HTTPError(
                "u", 429, "slow down", {"Retry-After": "0"}, io.BytesIO(b"{}")
            )
        return _Response(_reply({"ok": True}))

    data, _ = openrouter.chat([], model="m", api_key="k", opener=opener)
    assert data == {"ok": True} and calls["n"] == 2


def test_chat_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(openrouter.time, "sleep", lambda _s: None)

    def opener(request, timeout=None):
        import urllib.error

        raise urllib.error.HTTPError("u", 500, "boom", {}, io.BytesIO(b"{}"))

    with pytest.raises(openrouter.OpenRouterError):
        openrouter.chat([], model="m", api_key="k", opener=opener, max_retries=2)


def test_resolve_key_prefers_a_literal_env_value():
    assert openrouter.resolve_key(env={"OPENROUTER_API_KEY": "literal"}) == "literal"


def test_resolve_key_follows_an_op_reference():
    def runner(argv):
        assert argv[:2] == ["op", "read"]
        return "from-1password"

    assert (
        openrouter.resolve_key(
            env={"OPENROUTER_API_KEY": "op://Private/x/credential"}, runner=runner
        )
        == "from-1password"
    )


def test_resolve_key_reads_a_named_item():
    def runner(argv):
        assert argv[:3] == ["op", "item", "get"]
        return "item-secret" if "label=credential" in argv else ""

    assert openrouter.resolve_key("My Item", env={}, runner=runner) == "item-secret"


def test_resolve_key_without_any_source_explains_itself():
    with pytest.raises(openrouter.OpenRouterError) as excinfo:
        openrouter.resolve_key(env={}, runner=lambda argv: "")
    message = str(excinfo.value)
    assert "OPENROUTER_API_KEY" in message and "--op-item" in message


def test_key_present_never_returns_the_value():
    assert openrouter.key_present(env={"OPENROUTER_API_KEY": "super-secret"}) is True
    assert openrouter.key_present(env={}, runner=lambda argv: "") is False
