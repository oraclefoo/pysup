from __future__ import annotations

import shutil
import sys
import tempfile
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pysup.config import ProgramConfig, SupervisorConfig, load_config, parse_config

PY = sys.executable


@pytest.fixture
def workdir() -> Iterator[Path]:
    """A short temp dir (Unix socket paths are limited to ~104 bytes)."""
    path = Path(tempfile.mkdtemp(prefix="pysup-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def write_config(directory: Path, text: str, name: str = "pysup.yaml") -> Path:
    path = directory / name
    path.write_text(textwrap.dedent(text))
    return path


def make_config(directory: Path, text: str) -> SupervisorConfig:
    return load_config(write_config(directory, text))


def program_config(directory: Path, **options: Any) -> ProgramConfig:
    """Build a validated ProgramConfig named ``prog`` with fast test defaults."""
    options.setdefault("startsecs", 0.2)
    options.setdefault("stop_timeout", 2)
    config = parse_config({"programs": {"prog": options}}, base_dir=directory)
    return config.programs["prog"]


def python_cmd(code: str) -> list[str]:
    return [PY, "-u", "-c", textwrap.dedent(code)]
