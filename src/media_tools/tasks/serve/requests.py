"""Turn a caller's JSON job request into a CLI argv, or refuse it.

A request is structured, never argv:

    {"task": "convert", "inputs": ["talk.mp4"], "options": {"to": "mp3"}}
    {"task": "ebook", "command": "build", "inputs": ["books/"], "options": {"no-llm": true}}

Every `options` key must be a long flag of that task's OWN argparse parser, so the
allowlist is the CLI itself and cannot drift from it. On top of that:

- flags that would let a caller choose where output goes, or reach a secret, are never
  accepted (`FORBIDDEN_DESTS`): the server forces `-o` per caller and `--json`;
- a flag whose type is a path (`--list`) and every positional input are resolved
  inside the caller's input directory, symlinks followed;
- values are passed as `--flag=value`, one argv element, so a value that begins with
  `-` can never be read as a flag;
- `ebook kindle` is never reachable through the API: a device is not a service.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

TASKS = ("compress", "convert", "split", "download", "ebook")
EBOOK_COMMANDS = ("scan", "normalize", "dedup", "covers", "convert", "build")

# Never accepted from a caller. The server sets -o and --json itself; --quiet would
# silence the event stream the API returns; --summary-json writes a file wherever it is
# told; --op-item reaches into a 1Password vault that does not exist in a container.
FORBIDDEN_DESTS = frozenset(
    {"output_dir", "json_mode", "quiet", "summary_json", "op_item", "list_formats"}
)

MAX_INPUTS = 1000


class RequestError(ValueError):
    """The request is malformed or asks for something the API never allows."""


@dataclass(frozen=True)
class Plan:
    task_id: str  # what the disabled-task check compares: "ebook", "download", ...
    argv: list[str]  # the command and its options, without -o/--json
    positional: list[str]  # inputs, passed after `--` so none can be read as a flag

    def command_line(self, output_root: Path) -> list[str]:
        """Everything after `media-tools`, with the flags only the server may set."""
        tail = ["--", *self.positional] if self.positional else []
        return [*self.argv, "--json", f"--output-dir={output_root}", *tail]


def _subparser(parser: argparse.ArgumentParser, *names: str) -> argparse.ArgumentParser:
    for name in names:
        action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        if name not in action.choices:
            raise RequestError(f"unknown command: {' '.join(names)}")
        parser = action.choices[name]
    return parser


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def _confined_path(raw: object, root: Path, what: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise RequestError(f"{what} must be a non-empty string path")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    if not _inside(candidate, root):
        raise RequestError(f"{what} resolves outside your input directory: {raw}")
    if not candidate.exists():
        raise RequestError(f"{what} not found: {raw}")
    return str(candidate)


def _url(raw: object) -> str:
    if not isinstance(raw, str) or urlsplit(raw).scheme not in ("http", "https"):
        raise RequestError(f"download inputs must be http(s) URLs: {raw!r}")
    return raw


def _is_path_type(action: argparse.Action) -> bool:
    return action.type is Path


def _option_args(
    parser: argparse.ArgumentParser, options: object, root: Path, max_workers: int
) -> list[str]:
    if not isinstance(options, dict):
        raise RequestError("options must be an object")
    by_flag = {s[2:]: a for a in parser._actions for s in a.option_strings if s.startswith("--")}
    argv: list[str] = []
    for key, value in options.items():
        action = by_flag.get(key) if isinstance(key, str) else None
        if action is None or action.dest in FORBIDDEN_DESTS or action.dest == "help":
            raise RequestError(f"option not accepted: {key!r}")
        if isinstance(action, argparse._StoreTrueAction):
            if not isinstance(value, bool):
                raise RequestError(f"option {key!r} takes true or false")
            if value:
                argv.append(f"--{key}")
            continue
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise RequestError(f"option {key!r} takes a string or a number")
        if _is_path_type(action):
            value = _confined_path(value, root, f"option {key!r}")
        if action.dest == "workers":
            try:
                value = min(int(value), max_workers)
            except ValueError as error:
                raise RequestError(f"option {key!r} takes a positive integer") from error
        argv.append(f"--{key}={value}")
    if "workers" in {a.dest for a in parser._actions} and "workers" not in options:
        argv.append(f"--workers={max_workers}")
    return argv


def plan(
    request: object, *, parser: argparse.ArgumentParser, input_root: Path, max_workers: int
) -> Plan:
    if not isinstance(request, dict):
        raise RequestError("the request body must be a JSON object")
    unknown = set(request) - {"task", "command", "inputs", "options"}
    if unknown:
        raise RequestError(f"unknown request fields: {', '.join(sorted(unknown))}")

    task = request.get("task")
    if task not in TASKS:
        raise RequestError(f"task must be one of: {', '.join(TASKS)}")
    command = request.get("command")
    if task == "ebook":
        if command not in EBOOK_COMMANDS:
            raise RequestError(f"ebook command must be one of: {', '.join(EBOOK_COMMANDS)}")
        sub = _subparser(parser, "ebook", command)
        head = ["ebook", command]
    else:
        if command is not None:
            raise RequestError(f"{task} takes no command")
        sub = _subparser(parser, task)
        head = [task]

    inputs = request.get("inputs", [])
    if not isinstance(inputs, list) or len(inputs) > MAX_INPUTS:
        raise RequestError(f"inputs must be a list of at most {MAX_INPUTS} entries")
    if task == "download":
        positional = [_url(raw) for raw in inputs]
    else:
        positional = [_confined_path(raw, input_root, "input") for raw in inputs]

    argv = [*head, *_option_args(sub, request.get("options", {}), input_root, max_workers)]
    return Plan(task_id=task, argv=argv, positional=positional)
