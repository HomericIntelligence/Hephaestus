#!/usr/bin/env python3
"""Fetch or verify the reviewed SMG runtime wheelhouse."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from typing import Any

MAX_WHEEL_BYTES = 32 * 1024 * 1024
NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
PYTHON_MINOR_PATTERN = re.compile(r"3\.[0-9]+\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
VERSION_PATTERN = re.compile(r"[0-9][0-9A-Za-z.+-]*\Z")


@dataclass(frozen=True)
class WheelRecord:
    """One reviewed wheel for one or more Python minor versions."""

    filename: str
    name: str
    python_minors: tuple[str, ...]
    sha256: str
    size: int
    url: str
    version: str


@dataclass(frozen=True)
class DependencyLock:
    """The complete reviewed SMG Python dependency contract."""

    python_minors: tuple[str, ...]
    requirements: tuple[tuple[str, str], ...]
    smg_requires_dist: tuple[str, ...]
    wheels: tuple[WheelRecord, ...]


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a nonempty string list")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} contains a duplicate")
    return result


def load_dependency_lock(path: Path) -> DependencyLock:
    """Load and strictly validate one dependency lock."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("the dependency lock is not a regular file")
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("the dependency lock is invalid JSON") from error
    expected_keys = {"format", "python_minors", "requirements", "smg_requires_dist", "wheels"}
    if not isinstance(payload, dict) or set(payload) != expected_keys or payload["format"] != 1:
        raise ValueError("the dependency lock schema is invalid")

    python_minors = _string_tuple(payload["python_minors"], "python_minors")
    if any(PYTHON_MINOR_PATTERN.fullmatch(item) is None for item in python_minors):
        raise ValueError("the dependency lock has an invalid Python minor version")
    smg_requires_dist = _string_tuple(payload["smg_requires_dist"], "smg_requires_dist")

    raw_requirements = payload["requirements"]
    if not isinstance(raw_requirements, list) or not raw_requirements:
        raise ValueError("requirements must be a nonempty list")
    requirements: list[tuple[str, str]] = []
    for item in raw_requirements:
        if not isinstance(item, dict) or set(item) != {"name", "version"}:
            raise ValueError("a dependency requirement is invalid")
        name = item["name"]
        version = item["version"]
        if (
            not isinstance(name, str)
            or NAME_PATTERN.fullmatch(name) is None
            or not isinstance(version, str)
            or VERSION_PATTERN.fullmatch(version) is None
        ):
            raise ValueError("a dependency requirement is invalid")
        requirements.append((name, version))
    if len({name for name, _version in requirements}) != len(requirements):
        raise ValueError("the dependency lock has a duplicate requirement")

    raw_wheels = payload["wheels"]
    if not isinstance(raw_wheels, list) or not raw_wheels:
        raise ValueError("wheels must be a nonempty list")
    wheels: list[WheelRecord] = []
    wheel_keys = {"filename", "name", "python_minors", "sha256", "size", "url", "version"}
    requirement_set = set(requirements)
    for item in raw_wheels:
        if not isinstance(item, dict) or set(item) != wheel_keys:
            raise ValueError("a wheel record is invalid")
        filename = item["filename"]
        name = item["name"]
        version = item["version"]
        sha256 = item["sha256"]
        size = item["size"]
        url = item["url"]
        record_minors = _string_tuple(item["python_minors"], "wheel python_minors")
        parsed_url = urllib.parse.urlsplit(url) if isinstance(url, str) else None
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not filename.endswith(".whl")
            or (name, version) not in requirement_set
            or any(minor not in python_minors for minor in record_minors)
            or not isinstance(sha256, str)
            or SHA256_PATTERN.fullmatch(sha256) is None
            or not isinstance(size, int)
            or not 0 < size <= MAX_WHEEL_BYTES
            or parsed_url is None
            or parsed_url.scheme != "https"
            or parsed_url.hostname != "files.pythonhosted.org"
            or Path(urllib.parse.unquote(parsed_url.path)).name != filename
        ):
            raise ValueError("a wheel record is invalid")
        wheels.append(
            WheelRecord(
                filename=filename,
                name=name,
                python_minors=record_minors,
                sha256=sha256,
                size=size,
                url=url,
                version=version,
            )
        )
    if len({wheel.filename for wheel in wheels}) != len(wheels):
        raise ValueError("the dependency lock has a duplicate wheel filename")
    for python_minor in python_minors:
        selected = [wheel for wheel in wheels if python_minor in wheel.python_minors]
        selected_requirements = [(wheel.name, wheel.version) for wheel in selected]
        if sorted(selected_requirements) != sorted(requirements):
            raise ValueError(f"the wheel set for Python {python_minor} is incomplete")
    return DependencyLock(
        python_minors=python_minors,
        requirements=tuple(requirements),
        smg_requires_dist=smg_requires_dist,
        wheels=tuple(wheels),
    )


def _wheel_identity(payload: bytes) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = [
                item
                for item in archive.infolist()
                if item.filename.endswith(".dist-info/METADATA") and not item.is_dir()
            ]
            if len(entries) != 1:
                raise ValueError("a dependency wheel must contain one METADATA file")
            metadata = BytesParser(policy=default).parsebytes(archive.read(entries[0]))
    except zipfile.BadZipFile as error:
        raise ValueError("a dependency wheel has invalid ZIP data") from error
    names = metadata.get_all("Name", [])
    versions = metadata.get_all("Version", [])
    if len(names) != 1 or len(versions) != 1:
        raise ValueError("a dependency wheel has invalid identity metadata")
    return names[0].lower().replace("_", "-"), versions[0]


def _verify_payload(payload: bytes, record: WheelRecord) -> None:
    if len(payload) != record.size:
        raise ValueError(f"the size is incorrect for {record.filename}")
    if hashlib.sha256(payload).hexdigest() != record.sha256:
        raise ValueError(f"the SHA-256 is incorrect for {record.filename}")
    if _wheel_identity(payload) != (record.name, record.version):
        raise ValueError(f"the identity is incorrect for {record.filename}")


def _selected_wheels(lock: DependencyLock, python_minor: str) -> tuple[WheelRecord, ...]:
    if python_minor not in lock.python_minors:
        raise ValueError(f"Python {python_minor} is not in the dependency lock")
    return tuple(wheel for wheel in lock.wheels if python_minor in wheel.python_minors)


def verify_wheelhouse(lock: DependencyLock, output: Path, python_minor: str) -> None:
    """Verify that one directory has exactly the selected reviewed wheels."""
    if output.is_symlink() or not output.is_dir():
        raise ValueError("the wheelhouse is not a regular directory")
    selected = _selected_wheels(lock, python_minor)
    expected_names = {record.filename for record in selected}
    actual_names = {path.name for path in output.iterdir()}
    if actual_names != expected_names:
        raise ValueError("the wheelhouse file set is incorrect")
    for record in selected:
        wheel = output / record.filename
        if wheel.is_symlink() or not wheel.is_file():
            raise ValueError(f"the wheel is not a regular file: {record.filename}")
        _verify_payload(wheel.read_bytes(), record)


def prepare_wheelhouse(lock: DependencyLock, output: Path, python_minor: str) -> None:
    """Fetch and verify the selected reviewed wheels."""
    if output.is_symlink():
        raise ValueError("the wheelhouse path is a symbolic link")
    output.mkdir(parents=True, exist_ok=True)
    for record in _selected_wheels(lock, python_minor):
        destination = output / record.filename
        if destination.is_file() and not destination.is_symlink():
            try:
                _verify_payload(destination.read_bytes(), record)
            except ValueError:
                destination.unlink()
            else:
                continue
        with urllib.request.urlopen(record.url, timeout=60) as response:
            payload = response.read(MAX_WHEEL_BYTES + 1)
        if len(payload) > MAX_WHEEL_BYTES:
            raise ValueError(f"the wheel is too large: {record.filename}")
        _verify_payload(payload, record)
        temporary = output / f".{record.filename}.partial-{os.getpid()}"
        temporary.write_bytes(payload)
        temporary.replace(destination)
    verify_wheelhouse(lock, output, python_minor)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--python-minor", required=True)
    parser.add_argument("--offline", action="store_true")
    return parser


def main() -> int:
    """Run the wheelhouse command."""
    parser = _argument_parser()
    arguments = parser.parse_args()
    try:
        lock = load_dependency_lock(arguments.lock)
        if arguments.offline:
            verify_wheelhouse(lock, arguments.output, arguments.python_minor)
        else:
            prepare_wheelhouse(lock, arguments.output, arguments.python_minor)
    except (OSError, ValueError) as error:
        parser.exit(1, f"error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
