"""Require the maintained nested-sandbox domain and observed private labels."""

import os

import pytest

from tests.unit.automation.test_fleet_containment import Engine, specification, supervisor
from tests.unit.automation.test_fleet_podman import (
    engine_process as engine_process,
    kernel_files as kernel_files,
)

pytestmark = pytest.mark.precommit

PROCESS_LABEL = "system_u:system_r:container_userns_t:s0:c315,c717"
MOUNT_LABEL = "system_u:object_r:container_file_t:s0:c315,c717"
SECURITY_OPTIONS = ["no-new-privileges", "label=type:container_userns_t"]


def test_enforcing_host_selects_maintained_domain_without_changing_controls(
    engine_process, tmp_path
):
    """Use the supported domain with the existing private workspace and capability drop."""
    import json

    from hephaestus.automation import fleet_podman

    engine, private = engine_process
    before = engine.identity()
    fleet_podman._SELINUX_ENFORCE.write_text("1\n")
    engine.create(specification(tmp_path), "c" * 32)
    arguments = json.loads((private / "calls.jsonl").read_text().splitlines()[0])["argv"]
    assert "--security-opt=label=type:container_userns_t" in arguments
    assert "--security-opt=no-new-privileges" in arguments
    assert "--cap-drop=ALL" in arguments
    assert "--network=none" in arguments and "--read-only" in arguments
    assert arguments[arguments.index("--mount") + 1].endswith("relabel=private")
    assert engine.identity() == {**before, "selinuxType": "container_userns_t"}


@pytest.mark.parametrize("enforce", ["0\n", "unexpected\n"])
def test_non_enforcing_or_unknown_selinux_state_cannot_create(engine_process, tmp_path, enforce):
    """A present SELinux interface must positively establish enforcement."""
    from hephaestus.automation import fleet_podman

    engine, private = engine_process
    fleet_podman._SELINUX_ENFORCE.write_text(enforce)
    with pytest.raises(ValueError, match="selinux_enforcement_unconfirmed"):
        engine.create(specification(tmp_path), "c" * 32)
    assert not (private / "calls.jsonl").exists()


def test_non_selinux_host_keeps_existing_engine_identity_and_options(engine_process, tmp_path):
    """Do not require an unavailable SELinux domain on an existing non-SELinux host."""
    import json

    engine, private = engine_process
    assert "selinuxType" not in engine.identity()
    engine.create(specification(tmp_path), "c" * 32)
    arguments = json.loads((private / "calls.jsonl").read_text().splitlines()[0])["argv"]
    assert [arg for arg in arguments if arg.startswith("--security-opt=")] == [
        "--security-opt=no-new-privileges"
    ]


def selinux_engine():
    """Supply engine metadata independently of the containment validator."""
    engine = Engine()
    identity = engine.identity()
    engine.identity = lambda: {**identity, "selinuxType": "container_userns_t"}

    def labels(snapshot):
        snapshot.update(ProcessLabel=PROCESS_LABEL, MountLabel=MOUNT_LABEL)
        snapshot["HostConfig"]["SecurityOpt"] = list(SECURITY_OPTIONS)

    engine.mutate = labels
    return engine


def test_matching_private_engine_labels_allow_only_created_state(tmp_path):
    """Valid declared labels permit creation but do not replace live kernel evidence."""
    engine = selinux_engine()
    owner = supervisor(tmp_path, engine)
    try:
        assert owner.create(specification(tmp_path))["phase"] == "created"
        assert "attach" not in engine.calls
    finally:
        owner.close()


@pytest.mark.parametrize(
    "process_categories,mount_categories",
    [("c717,c315", "c315,c717"), ("c1.c2", "c1,c2")],
)
def test_equivalent_private_category_pairs_are_accepted(
    tmp_path, process_categories, mount_categories
):
    """SELinux may express the same two private categories in either canonical form."""
    engine = selinux_engine()
    valid_labels = engine.mutate

    def mutate(snapshot):
        valid_labels(snapshot)
        snapshot["ProcessLabel"] = PROCESS_LABEL.replace("c315,c717", process_categories)
        snapshot["MountLabel"] = MOUNT_LABEL.replace("c315,c717", mount_categories)

    engine.mutate = mutate
    owner = supervisor(tmp_path, engine)
    try:
        assert owner.create(specification(tmp_path))["phase"] == "created"
    finally:
        owner.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("ProcessLabel", None),
        ("ProcessLabel", "system_u:system_r:container_t:s0:c315,c717"),
        ("ProcessLabel", "system_u:system_r:container_userns_t:s0"),
        ("ProcessLabel", "system_u:system_r:container_userns_t:s0:c315,c315"),
        ("ProcessLabel", "system_u:system_r:container_userns_t:s0:c1.c9"),
        ("ProcessLabel", "system_u:system_r:container_userns_t:s0:c1,c1024"),
        ("MountLabel", "system_u:object_r:container_file_t:s0:c1,c2"),
        ("MountLabel", "system_u:object_r:container_file_t:s0"),
        ("MountLabel", "system_u:object_r:container_ro_file_t:s0:c315,c717"),
        ("SecurityOpt", [*SECURITY_OPTIONS, "label=disable"]),
        ("SecurityOpt", [*SECURITY_OPTIONS, "label=level:s0:c315,c717"]),
        ("SecurityOpt", [*SECURITY_OPTIONS, "seccomp=unconfined"]),
        ("SecurityOpt", [*SECURITY_OPTIONS, "no-new-privileges"]),
        ("SecurityOpt", ["no-new-privileges"]),
    ],
)
def test_unsupported_engine_labels_never_expose_an_attachment(tmp_path, field, value):
    """Retain uncertain ownership when the engine ignores or changes the selected policy."""
    engine = selinux_engine()
    valid_labels = engine.mutate

    def mutate(snapshot):
        valid_labels(snapshot)
        (snapshot["HostConfig"] if field == "SecurityOpt" else snapshot)[field] = value

    engine.mutate = mutate
    owner = supervisor(tmp_path, engine)
    try:
        with pytest.raises(ValueError, match="container_policy_mismatch"):
            owner.create(specification(tmp_path))
        assert "attach" not in engine.calls
        assert owner.inventory()[0]["phase"] == "uncertain"
    finally:
        owner.close()


@pytest.fixture
def selinux_kernel(kernel_files, monkeypatch):
    """Provide only proc files, selinuxfs, and filesystem xattrs as external fixtures."""
    from hephaestus.automation import fleet_podman

    kernel, proc, scope, snapshot, spec = kernel_files
    fleet_podman._SELINUX_ENFORCE.write_text("1\n")
    snapshot.update(ProcessLabel=PROCESS_LABEL, MountLabel=MOUNT_LABEL)
    for pid in (123, 456):
        (proc / str(pid) / "attr").mkdir()
        (proc / str(pid) / "attr/current").write_text(PROCESS_LABEL + "\n")

    def label(path, name, *, follow_symlinks):
        assert path == spec.workspace and name == "security.selinux"
        assert follow_symlinks is False
        return MOUNT_LABEL.encode() + b"\0"

    monkeypatch.setattr(os, "getxattr", label, raising=False)
    return kernel, proc, scope, snapshot, spec


def test_live_selinux_capture_records_matching_process_and_workspace_labels(selinux_kernel):
    """Engine metadata is confirmed against every process and the real workspace label."""
    kernel, _proc, _scope, snapshot, spec = selinux_kernel
    observed = kernel.capture("b" * 64, snapshot, spec)
    assert observed["selinux"] == {
        "processLabel": PROCESS_LABEL,
        "workspaceLabel": MOUNT_LABEL,
    }


def test_live_process_label_accepts_a_single_terminal_nul(selinux_kernel):
    """A kernel context terminator does not change its type or private category pair."""
    kernel, proc, _scope, snapshot, spec = selinux_kernel
    (proc / "456/attr/current").write_bytes(PROCESS_LABEL.encode() + b"\0")
    assert kernel.capture("b" * 64, snapshot, spec)["selinux"]["processLabel"] == PROCESS_LABEL


@pytest.mark.parametrize(
    "failure", ["process", "workspace", "missing-process", "missing-xattr", "permissive"]
)
def test_live_label_or_enforcement_mismatch_blocks_execution(selinux_kernel, monkeypatch, failure):
    """A matching inspect response cannot authorize a different effective boundary."""
    from hephaestus.automation import fleet_podman

    kernel, proc, _scope, snapshot, spec = selinux_kernel
    if failure == "process":
        (proc / "456/attr/current").write_text(PROCESS_LABEL.replace("c717", "c718"))
    elif failure == "workspace":
        monkeypatch.setattr(os, "getxattr", lambda *args, **kwargs: b"unlabeled")
    elif failure == "missing-process":
        (proc / "123/attr/current").unlink()
    elif failure == "missing-xattr":
        monkeypatch.delattr(os, "getxattr")
    else:
        fleet_podman._SELINUX_ENFORCE.write_text("0\n")
    with pytest.raises(ValueError, match="kernel_boundary_unconfirmed"):
        kernel.capture("b" * 64, snapshot, spec)
