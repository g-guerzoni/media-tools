"""The HTTP surface of `media-tools serve`: stdlib only, JSON only.

    POST   /v1/jobs                    submit {task, command?, inputs[], options{}} -> 202
    GET    /v1/jobs/{id}               the job record, with the final `result` event
    GET    /v1/jobs/{id}/events?after=N  the CLI's JSON Lines events from line N
    DELETE /v1/jobs/{id}               cancel (SIGINT to the job's process group)
    GET    /healthz                    200 when the server can do work, 503 otherwise

Every /v1 request needs `Authorization: Bearer <token>`. A caller sees only its own
jobs; another caller's job id answers 404, exactly like one that does not exist.
"""

from __future__ import annotations

import hmac
import json
import re
import sys
from argparse import Namespace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from media_tools.core import disabled
from media_tools.tasks.serve import requests
from media_tools.tasks.serve.jobs import Store

MAX_BODY = 64 * 1024
CALLER_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
TOKEN_PREFIX = "caller-"
JOB_PATH = re.compile(r"^/v1/jobs/([0-9a-f]{16})(/events)?$")


class TokenError(RuntimeError):
    pass


def load_tokens(directory: Path) -> dict[str, str]:
    """{caller name: token}, one file per caller: `<directory>/caller-<name>`.

    A caller's name comes from its file's name, so identity is decided by whoever
    writes the secrets, never by anything in a request."""
    tokens: dict[str, str] = {}
    try:
        entries = sorted(directory.iterdir())
    except OSError as error:
        raise TokenError(f"cannot read caller tokens in {directory}: {error.strerror}") from None
    for entry in entries:
        if not entry.name.startswith(TOKEN_PREFIX):
            continue
        name = entry.name[len(TOKEN_PREFIX) :]
        if not CALLER_NAME.match(name):
            raise TokenError(f"invalid caller name in {entry.name}: use a-z, 0-9 and -")
        try:
            token = entry.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise TokenError(f"cannot read {entry}: {error.strerror}") from None
        if len(token) < 32:
            raise TokenError(f"{entry.name}: a token must be at least 32 characters")
        tokens[name] = token
    if not tokens:
        raise TokenError(f"no caller tokens ({TOKEN_PREFIX}<name>) in {directory}")
    return tokens


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, *, store: Store, tokens: dict[str, str], parser, problems):
        super().__init__(address, Handler)
        self.store = store
        self.tokens = tokens
        self.parser = parser
        self.problems = problems  # doctor checks that reported `missing` at startup


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - the base class's name
        sys.stderr.write(f"serve: {self.address_string()} {format % args}\n")

    # -- plumbing ------------------------------------------------------------------

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, message: str) -> None:
        self._send(status, {"error": message})

    def _caller(self) -> str | None:
        header = self.headers.get("Authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme != "Bearer" or not presented:
            return None
        found = None
        for name, token in self.server.tokens.items():
            # Compare against every token, so timing says nothing about which matched.
            if hmac.compare_digest(presented.encode(), token.encode()):
                found = name
        return found

    def _own_job(self, caller: str, job_id: str) -> dict | None:
        record = self.server.store.read(job_id)
        return record if record and record["caller"] == caller else None

    # -- routes --------------------------------------------------------------------

    def do_GET(self):  # noqa: N802 - the base class's name
        url = urlsplit(self.path)
        if url.path == "/healthz":
            return self._healthz()
        caller = self._caller()
        if caller is None:
            return self._error(HTTPStatus.UNAUTHORIZED, "missing or invalid bearer token")
        match = JOB_PATH.match(url.path)
        if not match:
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        record = self._own_job(caller, match.group(1))
        if record is None:
            return self._error(HTTPStatus.NOT_FOUND, "no such job")
        if match.group(2):
            try:
                after = int(parse_qs(url.query).get("after", ["0"])[0])
            except ValueError:
                return self._error(HTTPStatus.BAD_REQUEST, "after must be an integer")
            events, next_after = self.server.store.events(record["id"], max(0, after))
            return self._send(
                HTTPStatus.OK,
                {"id": record["id"], "status": record["status"], "events": events,
                 "next": next_after},
            )  # fmt: skip
        return self._send(HTTPStatus.OK, _public(record))

    def do_POST(self):  # noqa: N802
        caller = self._caller()
        if caller is None:
            return self._error(HTTPStatus.UNAUTHORIZED, "missing or invalid bearer token")
        if urlsplit(self.path).path != "/v1/jobs":
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            return self._error(HTTPStatus.BAD_REQUEST, f"body must be 1..{MAX_BODY} bytes")
        try:
            request = json.loads(self.rfile.read(length))
        except ValueError:
            return self._error(HTTPStatus.BAD_REQUEST, "body is not valid JSON")

        store = self.server.store
        store.prepare_caller(caller)
        try:
            plan = requests.plan(
                request,
                parser=self.server.parser,
                input_root=store.input_root(caller),
                max_workers=store.settings.max_workers,
            )
        except requests.RequestError as error:
            return self._error(HTTPStatus.BAD_REQUEST, str(error))
        refused = disabled.refusal(
            Namespace(task=plan.task_id, ebook_command=request.get("command")), disabled.load()
        )
        if refused:
            return self._error(HTTPStatus.FORBIDDEN, refused)
        if store.over_cap():
            return self._error(
                HTTPStatus.INSUFFICIENT_STORAGE,
                "the data volume is over its size cap; no job can start until space frees up",
            )
        record = store.submit(caller, request, plan.command_line(store.output_root(caller)))
        return self._send(HTTPStatus.ACCEPTED, {"id": record["id"], "status": "queued"})

    def do_DELETE(self):  # noqa: N802
        caller = self._caller()
        if caller is None:
            return self._error(HTTPStatus.UNAUTHORIZED, "missing or invalid bearer token")
        match = JOB_PATH.match(urlsplit(self.path).path)
        if not match or match.group(2):
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        if self._own_job(caller, match.group(1)) is None:
            return self._error(HTTPStatus.NOT_FOUND, "no such job")
        record = self.server.store.cancel(match.group(1))
        return self._send(HTTPStatus.ACCEPTED, _public(record))

    def _healthz(self) -> None:
        store = self.server.store
        body = {
            "ok": not self.server.problems and store.janitor_fresh(),
            "problems": self.server.problems,
            "janitor_last_run": store.janitor_last_run,
            "usage_bytes": store.usage_bytes,
            "max_data_bytes": store.settings.max_data_bytes,
        }
        self._send(HTTPStatus.OK if body["ok"] else HTTPStatus.SERVICE_UNAVAILABLE, body)


def _public(record: dict) -> dict:
    """What a caller sees: never the argv, which names server-side paths."""
    keys = ("id", "status", "request", "created_at", "started_at", "finished_at",
            "exit_code", "result")  # fmt: skip
    return {key: record.get(key) for key in keys}
