#!/usr/bin/env python3
"""Build and export the local CI image used by Linux host verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from hephaestus.automation.pipeline.host_verification_pyxis import (
    DEFAULT_HOST_VERIFICATION_PYXIS_IMAGE,
)

IMAGE_TAG = "hephaestus-ci:local"
_CONTAINERFILE = Path("ci/Containerfile")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class HostVerificationImagePreparationError(RuntimeError):
    """Raised when the local Pyxis image cannot be prepared safely."""


def _validate_local_import_uri(uri: str) -> str:
    """Return a local Enroot URI and reject registry-backed sources."""
    if not (uri.startswith("podman://") or uri.startswith("dockerd://")):
        raise HostVerificationImagePreparationError(
            "Enroot import must use a local podman:// or dockerd:// image"
        )
    image = uri.split("://", 1)[1]
    if not image or "/" in image or "@" in image:
        raise HostVerificationImagePreparationError(
            "Enroot import must use a local image, not a registry URI"
        )
    return uri


def _regular_path(root: Path, relative: Path) -> Path:
    """Return a non-symlink path below *root* or fail closed."""
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise HostVerificationImagePreparationError(
            f"build context path is not a regular file: {relative}"
        )
    return path


def _safe_output_path(root: Path, output: Path) -> Path:
    """Return an absolute output path with no symlinked path component."""
    candidate = output.expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    absolute = candidate.absolute()
    for component in (absolute, *absolute.parents):
        if component.is_symlink():
            raise HostVerificationImagePreparationError(
                "output image path cannot contain a symlink"
            )
    return absolute


def _engine_command(engine: str) -> tuple[str, ...]:
    """Return the selected OCI engine executable command."""
    if engine not in {"podman", "docker"}:
        raise ValueError(f"unsupported container engine: {engine}")
    return (engine,)


def _select_engine(which: Callable[[str], str | None] = shutil.which) -> str:
    """Select Podman first, then Docker, from local executables."""
    for engine in ("podman", "docker"):
        if which(engine):
            return engine
    raise HostVerificationImagePreparationError("podman or docker is unavailable")


def _run(
    argv: Sequence[str], *, cwd: Path, runner: Callable[..., subprocess.CompletedProcess[str]]
) -> subprocess.CompletedProcess[str]:
    """Run one host-owned preparation command with captured diagnostics."""
    try:
        result = runner(
            tuple(argv),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostVerificationImagePreparationError(f"preparation command failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-500:]
        raise HostVerificationImagePreparationError(
            f"preparation command failed with exit code {result.returncode}: {detail}"
        )
    return result


def _read_image_id(result: subprocess.CompletedProcess[str]) -> str:
    """Return one immutable OCI image ID from an inspect result."""
    image_id = result.stdout.strip()
    if not _IMAGE_ID_RE.fullmatch(image_id):
        raise HostVerificationImagePreparationError("container engine returned an invalid image ID")
    return image_id


def prepare_image(
    *,
    repo_root: Path,
    output: Path = DEFAULT_HOST_VERIFICATION_PYXIS_IMAGE,
    rebuild: bool = False,
    engine: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, object]:
    """Build, export, and attest one local CI image for Pyxis."""
    root = repo_root.expanduser().resolve()
    _regular_path(root, _CONTAINERFILE)
    if (root / "ci").is_symlink():
        raise HostVerificationImagePreparationError("CI build context cannot be a symlink")
    target = _safe_output_path(root, output)
    target.parent.mkdir(parents=True, exist_ok=True)
    selected_engine = engine or _select_engine(which)
    executable = _engine_command(selected_engine)
    if rebuild or not target.exists():
        _run(
            (*executable, "build", "-f", str(_CONTAINERFILE), "-t", IMAGE_TAG, "."),
            cwd=root,
            runner=runner,
        )
        source_uri = f"{selected_engine}://{IMAGE_TAG}"
        if selected_engine == "docker":
            source_uri = f"dockerd://{IMAGE_TAG}"
        source_uri = _validate_local_import_uri(source_uri)
        _run(
            ("enroot", "import", "--output", str(target), source_uri),
            cwd=root,
            runner=runner,
        )
    if not target.is_file() or target.is_symlink():
        raise HostVerificationImagePreparationError(
            "Enroot did not create a regular squashfs image"
        )
    image_id = _read_image_id(
        _run(
            (*executable, "image", "inspect", "--format={{.Id}}", IMAGE_TAG),
            cwd=root,
            runner=runner,
        )
    )
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    sidecar = target.with_name(f"{target.name}.sha256")
    if sidecar.is_symlink():
        raise HostVerificationImagePreparationError("output image sidecar cannot be a symlink")
    sidecar.write_text(f"{digest}  {target.name}\n", encoding="ascii")
    contract = target.with_suffix(".contract.json")
    if contract.is_symlink():
        raise HostVerificationImagePreparationError("output image contract cannot be a symlink")
    contract.write_text(
        json.dumps(
            {
                "schema": "hephaestus-host-verification-pyxis-v1",
                "containerfile": str(_CONTAINERFILE),
                "container_engine": selected_engine,
                "local_import_uri": (
                    f"{selected_engine}://{IMAGE_TAG}"
                    if selected_engine == "podman"
                    else f"dockerd://{IMAGE_TAG}"
                ),
                "container_image": IMAGE_TAG,
                "container_image_id": image_id,
                "container_image_sha256": digest,
                "squashfs": str(target),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "image": str(target),
        "sha256": digest,
        "image_id": image_id,
        "sidecar": str(sidecar),
        "contract": str(contract),
        "engine": selected_engine,
    }


def _build_parser() -> argparse.ArgumentParser:
    """Build the image-preparation command parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=DEFAULT_HOST_VERIFICATION_PYXIS_IMAGE)
    parser.add_argument("--rebuild", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the image preparation command."""
    args = _build_parser().parse_args(argv)
    try:
        result = prepare_image(
            repo_root=args.repo_root,
            output=args.output,
            rebuild=args.rebuild,
        )
    except HostVerificationImagePreparationError as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
