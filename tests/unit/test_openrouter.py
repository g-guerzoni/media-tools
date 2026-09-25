import io
import json
import os

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


def test_chat_raises_a_clear_error_when_response_has_no_choices():
    # A malformed or unexpected OpenRouter response (no "choices" at all) must
    # surface as a clear OpenRouterError, not a raw KeyError/IndexError leaking
    # out of chat()'s internals.
    def opener(request, timeout=None):
        return _Response({"usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    with pytest.raises(openrouter.OpenRouterError):
        openrouter.chat([], model="m", api_key="k", opener=opener)


def test_resolve_key_tries_every_label_until_one_has_a_value():
    calls = []

    def runner(argv):
        calls.append(argv)
        return "found-it" if "label=key" in argv else ""

    assert openrouter.resolve_key("My Item", env={}, runner=runner) == "found-it"
    # "key" is the 5th of 6 field labels; every earlier one must have been tried.
    assert len(calls) == openrouter._FIELD_LABELS.index("key") + 1


def test_default_runner_signals_a_timeout_distinctly_from_an_empty_result(monkeypatch):
    import subprocess

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(openrouter.subprocess, "run", fake_run)
    assert openrouter._default_runner(["op", "read", "op://x"]) is openrouter._TIMED_OUT


def test_resolve_key_stops_trying_op_item_labels_after_a_timeout():
    # Minor finding: a named --op-item that never resolves used to cost up to
    # len(_FIELD_LABELS) x 30s (~3 minutes) because a hanging/unreachable `op`
    # looked exactly like "this field is empty, try the next one". A timeout must
    # stop the loop instead of repeating the same wait for every remaining label.
    calls = []

    def fake_runner(argv):
        calls.append(argv)
        return openrouter._TIMED_OUT

    with pytest.raises(openrouter.OpenRouterError):
        openrouter.resolve_key("My Item", env={}, runner=fake_runner)

    assert len(calls) == 1, "a timed-out call must not be retried with the next field label"


def test_key_present_forwards_a_custom_timeout_to_resolve_key(monkeypatch):
    captured = {}

    def fake_resolve_key(op_item=None, *, env=None, runner=None, timeout=30):
        captured["timeout"] = timeout
        raise openrouter.OpenRouterError("no key")

    monkeypatch.setattr(openrouter, "resolve_key", fake_resolve_key)
    assert openrouter.key_present(env={}, timeout=5.0) is False
    assert captured["timeout"] == 5.0


def test_default_runner_returns_empty_string_when_op_is_not_installed(monkeypatch):
    # resolve_key's default runner (used whenever a test - or a real caller -
    # does not inject one) must degrade to "no value from this source" rather
    # than crash when the `op` binary itself is missing from PATH.
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("op: command not found")

    monkeypatch.setattr(openrouter.subprocess, "run", fake_run)
    assert openrouter._default_runner(["op", "read", "op://x"]) == ""


@pytest.mark.parametrize(
    "fenced",
    [
        '```json\n{"books": ["a", "b"]}\n```',
        '```\n{"books": ["a", "b"]}\n```',
    ],
)
def test_parse_json_content_tolerates_a_fenced_code_block(fenced):
    assert openrouter.parse_json_content(fenced) == {"books": ["a", "b"]}


# -- OPENROUTER_API_KEY_FILE ----------------------------------------------------------


def test_resolve_key_reads_a_key_file_first(tmp_path):
    key_file = tmp_path / "openrouter"
    key_file.write_text("sk-from-file\n")
    env = {"OPENROUTER_API_KEY_FILE": str(key_file), "OPENROUTER_API_KEY": "sk-literal"}
    assert openrouter.resolve_key(env=env, runner=lambda argv: pytest.fail("no op")) == (
        "sk-from-file"
    )


@pytest.mark.parametrize("content", [None, "", "  \n"])
def test_a_named_key_file_that_fails_never_falls_through(tmp_path, content):
    key_file = tmp_path / "openrouter"
    if content is not None:
        key_file.write_text(content)
    env = {"OPENROUTER_API_KEY_FILE": str(key_file), "OPENROUTER_API_KEY": "sk-literal"}
    with pytest.raises(openrouter.OpenRouterError) as info:
        openrouter.resolve_key(env=env)
    assert str(key_file) in str(info.value)
    assert "sk-literal" not in str(info.value)


def test_a_key_file_error_never_contains_the_key(tmp_path):
    key_file = tmp_path / "openrouter"
    key_file.write_text("sk-secret-value")
    key_file.chmod(0o000)
    env = {"OPENROUTER_API_KEY_FILE": str(key_file)}
    try:
        if os.access(key_file, os.R_OK):
            pytest.skip("running as a user that can read a mode-000 file")
        with pytest.raises(openrouter.OpenRouterError) as info:
            openrouter.resolve_key(env=env)
    finally:
        key_file.chmod(0o600)
    assert "sk-secret-value" not in str(info.value)
