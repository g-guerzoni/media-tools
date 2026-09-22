"""What a task's engine must provide, and how one is chosen for a file."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class Dependency:
    name: str
    locate: Callable[[], str | None]
    install_hint: str


class Engine(Protocol):
    name: str
    inputs: frozenset[str]
    outputs: frozenset[str]
    dependencies: tuple[Dependency, ...]

    def add_arguments(self, group) -> None: ...
    def hash_options(self, args) -> dict: ...
    def output_names(self, src: Path, args) -> list[str]: ...
    def process(self, item, ctx) -> Any: ...


def select_engine(engines: Sequence[Engine], path: Path, to: str | None = None) -> Engine | None:
    suffix = path.suffix.lower().lstrip(".")
    for engine in engines:
        if f".{suffix}" in engine.inputs and (to is None or to in engine.outputs):
            return engine
    return None


def missing_dependencies(engine: Engine) -> list[Dependency]:
    return [dep for dep in engine.dependencies if dep.locate() is None]
