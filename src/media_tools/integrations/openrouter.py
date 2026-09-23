"""A minimal OpenRouter chat client, and the project's key-lookup rules.

No third-party HTTP dependency: this uses `urllib` from the standard library, with
the actual network call routed through an injectable `opener` so tests never touch
the network. Likewise, 1Password lookups go through an injectable `runner` so tests
never shell out to the real `op` binary.

A key may be supplied three ways: a literal value in `OPENROUTER_API_KEY`, an
`op://vault/item/field` reference in that same variable (resolved via `op read`),
or a named 1Password item passed as `--op-item` (its `credential`/`password`/
`api key`/`apikey`/`key`/`token` field is tried, in that order, via `op item get
... --reveal`). There is no built-in default item — a missing key must send the
caller to one of these three, or to `--no-llm`. The resolved value is returned to
the caller and never printed, logged, or otherwise surfaced by this module.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

API_URL = "https://openrouter.ai/api/v1/chat/completions"

# 408 Request Timeout, 409 Conflict, 425 Too Early, 429 Too Many Requests, plus any
# 5xx — everything else (400, 401, 403, 404, ...) is a caller mistake, not a blip,
# and retrying it would just repeat the same failure.
_RETRYABLE_STATUSES = frozenset({408, 409, 425, 429})

# Tried in order against a named 1Password item; the first field with a non-empty
# value wins. Covers this project's own convention and OpenRouter's own dashboard
# wording, without hardcoding any particular item name.
_FIELD_LABELS = ("credential", "password", "api key", "apikey", "key", "token")

_NO_KEY_MESSAGE = (
    "No OpenRouter API key available. Provide one via the OPENROUTER_API_KEY "
    "environment variable (either a literal key or an op://vault/item/field "
    "reference resolved through the 1Password CLI), via --op-item NAME (a named "
    "1Password item), or pass --no-llm to skip LLM-assisted features."
)


class OpenRouterError(RuntimeError):
    """An OpenRouter request or key lookup failed."""


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int


def _default_runner(argv: list[str]) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (subprocess.SubprocessError, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def resolve_key(op_item: str | None = None, *, env=None, runner=None) -> str:
    """Resolve the OpenRouter API key. Raises OpenRouterError, never a fallback
    default, when none of the three sources produces one."""
    env = os.environ if env is None else env
    runner = _default_runner if runner is None else runner

    raw = (env.get("OPENROUTER_API_KEY") or "").strip()
    if raw:
        if raw.startswith("op://"):
            resolved = runner(["op", "read", raw]).strip()
            if resolved:
                return resolved
        else:
            return raw

    if op_item:
        for label in _FIELD_LABELS:
            value = runner(
                ["op", "item", "get", op_item, "--fields", f"label={label}", "--reveal"]
            ).strip()
            if value:
                return value

    raise OpenRouterError(_NO_KEY_MESSAGE)


def key_present(op_item: str | None = None, *, env=None, runner=None) -> bool:
    """For `doctor`: whether a key resolves, without ever exposing it."""
    try:
        resolve_key(op_item, env=env, runner=runner)
    except OpenRouterError:
        return False
    return True


def parse_json_content(text: str) -> dict:
    """Parse a model reply's `content` string as a JSON object, tolerating a
    ```json fenced code block around it (some models add one despite being asked
    for a bare JSON object)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return json.loads(cleaned)


def _retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
    headers = error.headers
    retry_after = headers.get("Retry-After") if headers is not None else None
    if retry_after:
        try:
            return max(float(retry_after), 0.0)
        except ValueError:
            pass
    return min(2 ** (attempt - 1), 30)


def chat(
    messages,
    *,
    model: str,
    api_key: str,
    timeout: int = 120,
    max_retries: int = 5,
    opener=None,
) -> tuple[dict, Usage]:
    """POST a chat completion request and return (parsed_json_content, Usage).

    Always sends temperature=0 and response_format={"type": "json_object"} — the
    normalize stage's cache assumes the same input always yields the same answer,
    which only holds if the request itself is deterministic. Retries 408/409/425/
    429/5xx with exponential backoff, honouring a `Retry-After` header when the
    server sends one, and gives up after `max_retries` attempts with a clear
    OpenRouterError rather than looping forever.
    """
    opener = urllib.request.urlopen if opener is None else opener
    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
    ).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    attempts = max(max_retries, 1)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
        try:
            with opener(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code not in _RETRYABLE_STATUSES and not (500 <= error.code < 600):
                raise OpenRouterError(
                    f"OpenRouter request failed: {error.code} {error.reason}"
                ) from error
            if attempt == attempts:
                raise OpenRouterError(
                    f"OpenRouter request failed after {attempt} attempt(s): "
                    f"{error.code} {error.reason}"
                ) from error
            time.sleep(_retry_delay(error, attempt))
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            if attempt == attempts:
                raise OpenRouterError(
                    f"OpenRouter request failed after {attempt} attempt(s): {error}"
                ) from error
            time.sleep(min(2 ** (attempt - 1), 30))
            continue

        try:
            message = payload["choices"][0]["message"]["content"]
            data = parse_json_content(message)
            usage_raw = payload.get("usage") or {}
            usage = Usage(
                prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
                completion_tokens=int(usage_raw.get("completion_tokens", 0)),
            )
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise OpenRouterError(f"OpenRouter returned an unexpected response: {error}") from error
        return data, usage

    # Unreachable: the loop above always returns or raises.
    raise OpenRouterError("OpenRouter request failed") from last_error
