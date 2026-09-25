"""serve: the internal job API other apps on the same host call.

`media-tools serve` runs a stdlib HTTP server (see `http.py`) in front of a small job
queue (see `jobs.py`). Each job is a subprocess of this same CLI. The design, and why
every limit below exists: docs/specs/2026-09-25-media-tools-container-design.md.

It is never exposed to the web: in production it listens on an internal Docker network
with no host port. Settings come from flags or, in a container, from the environment:

    MEDIA_TOOLS_DATA            data root (default /data)
    MEDIA_TOOLS_CALLER_TOKENS   directory of caller-<name> token files (default /run/secrets)
    MEDIA_TOOLS_MAX_JOBS        jobs running at once (default 1)
    MEDIA_TOOLS_MAX_WORKERS     cap on a job's --workers (default 1)
    MEDIA_TOOLS_JOB_TIMEOUT     seconds before a job is interrupted (default 21600)
    MEDIA_TOOLS_RETENTION_HOURS how long a finished job and its output are kept (default 720)
    MEDIA_TOOLS_MAX_DATA_BYTES  the data volume's size cap (default 30000000000)
    MEDIA_TOOLS_SWEEP_SECONDS   how often the janitor runs (default 600)
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from media_tools.core.events import EXIT_DEPENDENCY, EXIT_OK
from media_tools.tasks.common import UsageError

NAME = "serve"
HELP = "Run the internal job API (for other apps on the same host)."


def _env_number(name: str, default: float, kind=int):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = kind(raw)
    except ValueError:
        raise UsageError(f"{name} must be a number, got {raw!r}") from None
    if value <= 0:
        raise UsageError(f"{name} must be positive, got {raw!r}")
    return value


def register(subparsers):
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP)
    parser.add_argument("--host", default="0.0.0.0", help="Address to bind (default 0.0.0.0).")
    parser.add_argument("--port", type=int, default=8080, help="Port (default 8080).")
    parser.add_argument(
        "--data",
        type=Path,
        default=Path(os.environ.get("MEDIA_TOOLS_DATA", "/data")),
        help="Data root holding in/, out/, jobs/ and tmp/ (default: MEDIA_TOOLS_DATA or /data).",
    )
    parser.add_argument(
        "--tokens",
        type=Path,
        default=Path(os.environ.get("MEDIA_TOOLS_CALLER_TOKENS", "/run/secrets")),
        help="Directory of caller-<name> token files (default: MEDIA_TOOLS_CALLER_TOKENS "
        "or /run/secrets).",
    )
    return parser


def run(args) -> int:
    # Imported here so the CLI's other commands never pay for the server's imports.
    from media_tools.cli import build_parser
    from media_tools.tasks import doctor
    from media_tools.tasks.serve.http import Server, TokenError, load_tokens
    from media_tools.tasks.serve.jobs import Settings, Store

    settings = Settings(
        data=args.data,
        max_jobs=_env_number("MEDIA_TOOLS_MAX_JOBS", 1),
        max_workers=_env_number("MEDIA_TOOLS_MAX_WORKERS", 1),
        job_timeout_s=_env_number("MEDIA_TOOLS_JOB_TIMEOUT", 6 * 3600, float),
        retention_s=_env_number("MEDIA_TOOLS_RETENTION_HOURS", 720, float) * 3600,
        max_data_bytes=_env_number("MEDIA_TOOLS_MAX_DATA_BYTES", 30 * 10**9),
        sweep_s=_env_number("MEDIA_TOOLS_SWEEP_SECONDS", 600, float),
    )
    try:
        tokens = load_tokens(args.tokens)
    except TokenError as error:
        raise UsageError(str(error), code="config_missing", exit_code=EXIT_DEPENDENCY) from None
    for sub in ("in", "out", "jobs", "tmp"):
        (settings.data / sub).mkdir(parents=True, exist_ok=True)

    problems = [
        f"{check.name}: {check.detail}"
        for check in doctor.check_all(check_updates=False, output_dir=settings.data / "out")
        if check.status == "missing"
    ]
    # A job is cancelled with SIGINT, and a job inherits this process's disposition of
    # it. Started in the background by a non-interactive shell, this process begins
    # with SIGINT *ignored*, and every job would then ignore cancellation too. Reset it
    # so jobs always start with Python's normal KeyboardInterrupt handling.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    store = Store(settings)
    store.recover()
    store.start_janitor()
    store.start_workers()

    server = Server(
        (args.host, args.port), store=store, tokens=tokens, parser=build_parser(),
        problems=problems,
    )  # fmt: skip
    print(
        f"serve: listening on {args.host}:{server.server_address[1]} for "
        f"{len(tokens)} caller(s); data at {settings.data}",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return EXIT_OK
