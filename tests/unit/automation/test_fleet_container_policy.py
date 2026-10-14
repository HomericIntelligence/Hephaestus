"""Check declared scratch mounts and namespace settings in actual engine-shaped state."""

import pytest

from tests.unit.automation.test_fleet_containment import Engine, specification, supervisor

pytestmark = pytest.mark.precommit


@pytest.mark.parametrize(
    "field,value",
    [
        ("Tmpfs", {}),
        ("Tmpfs", {"/tmp": "rw,nosuid,nodev,noexec,size=64m,mode=1777", "/authority": "rw"}),
        ("Tmpfs", {"/tmp": "rw,nodev,noexec,size=64m,mode=1777"}),
        ("Tmpfs", {"/tmp": "rw,nosuid,nodev,noexec,size=1g,mode=1777"}),
        ("UsernsMode", "host"),
        ("CgroupMode", "host"),
    ],
)
def test_container_scratch_and_namespaces_must_match_registered_policy(tmp_path, field, value):
    """Reject missing protections and extra writable paths before process attachment."""
    engine = Engine()
    engine.mutate = lambda snapshot: snapshot["HostConfig"].update({field: value})
    owner = supervisor(tmp_path, engine)
    try:
        with pytest.raises(ValueError, match="container_policy_mismatch"):
            owner.create(specification(tmp_path))
        assert "attach" not in engine.calls
    finally:
        owner.close()


def test_same_length_environment_value_replacement_is_rejected(tmp_path):
    """A correct variable count cannot authorize a different tool home."""
    engine = Engine()
    engine.mutate = lambda snapshot: snapshot["Config"]["Env"].__setitem__(0, "HOME=/authority")
    owner = supervisor(tmp_path, engine)
    try:
        with pytest.raises(ValueError, match="container_policy_mismatch"):
            owner.create(specification(tmp_path))
    finally:
        owner.close()


def test_engine_full_image_id_without_algorithm_prefix_matches_the_same_digest(tmp_path):
    """Podman renders Image as the complete hex ID in its actual inspect response."""
    engine = Engine()
    engine.mutate = lambda snapshot: snapshot.update(Image="a" * 64)
    owner = supervisor(tmp_path, engine)
    try:
        assert owner.create(specification(tmp_path))["phase"] == "created"
    finally:
        owner.close()


def test_changed_tool_hostname_is_rejected_before_attachment(tmp_path):
    """Require the configured container hostname as well as its environment value."""
    engine = Engine()
    engine.mutate = lambda snapshot: snapshot["Config"].update(Hostname="unexpected-host")
    owner = supervisor(tmp_path, engine)
    try:
        with pytest.raises(ValueError, match="container_policy_mismatch"):
            owner.create(specification(tmp_path))
        assert "attach" not in engine.calls
    finally:
        owner.close()


def podman_keep_id(snapshot):
    """Represent the actual Podman 6.1.1 annotation and rootless ID maps."""
    snapshot["HostConfig"]["UsernsMode"] = "private"
    snapshot["HostConfig"]["IDMappings"] = {
        "UidMap": ["0:1:1000", "1000:0:1", "1001:1001:999000"],
        "GidMap": ["0:1:1000", "1000:0:1", "1001:1001:999000"],
    }
    snapshot["Config"]["Annotations"] = {
        "io.podman.annotations.userns": "keep-id:uid=1000,gid=1000"
    }


def test_podman_private_userns_with_exact_keep_id_mapping_can_be_created(tmp_path):
    """Accept the observed representation without accepting a bare private namespace."""
    engine = Engine()
    engine.mutate = podman_keep_id
    owner = supervisor(tmp_path, engine)
    try:
        assert owner.create(specification(tmp_path))["phase"] == "created"
        assert "attach" not in engine.calls
    finally:
        owner.close()


@pytest.mark.parametrize("changed", ["annotation", "UidMap", "GidMap"])
def test_private_userns_without_matching_keep_id_evidence_is_rejected(tmp_path, changed):
    """Missing mappings or another requested user mapping cannot satisfy keep-id."""
    engine = Engine()

    def mutate(snapshot):
        podman_keep_id(snapshot)
        if changed == "annotation":
            snapshot["Config"]["Annotations"]["io.podman.annotations.userns"] = "keep-id"
        else:
            snapshot["HostConfig"]["IDMappings"][changed] = ["0:0:1000000"]

    engine.mutate = mutate
    owner = supervisor(tmp_path, engine)
    try:
        with pytest.raises(ValueError, match="container_policy_mismatch"):
            owner.create(specification(tmp_path))
        assert "attach" not in engine.calls
    finally:
        owner.close()


def test_initial_caps_absence_permits_only_unstarted_creation_with_all_caps_dropped(tmp_path):
    """An unstarted container has no effective process capability observation yet."""
    engine = Engine()
    engine.mutate = lambda snapshot: snapshot.update(EffectiveCaps=None)
    owner = supervisor(tmp_path, engine)
    try:
        lease = owner.create(specification(tmp_path))
        assert lease["phase"] == "created"
        assert "attach" not in engine.calls
        assert "capture" not in engine.calls
    finally:
        owner.close()


@pytest.mark.parametrize("kernel_available", [True, False])
def test_running_caps_metadata_absence_requires_independent_kernel_proof(
    tmp_path, kernel_available
):
    """Podman can omit running capability metadata; only kernel proof permits attachment."""
    from tests.unit.automation.test_fleet_containment import Kernel

    engine = Engine()
    engine.mutate = lambda snapshot: snapshot.update(EffectiveCaps=None)
    kernel = Kernel(engine)
    kernel.fail_capture = not kernel_available
    owner = supervisor(tmp_path, engine, kernel)
    try:
        lease = owner.create(specification(tmp_path))
        if kernel_available:
            owner.start(lease["leaseId"])
            assert owner.inspect(lease["leaseId"])["phase"] == "active"
        else:
            with pytest.raises((ValueError, OSError)):
                owner.start(lease["leaseId"])
            assert owner.inspect(lease["leaseId"])["phase"] == "uncertain"
        assert "capture" in engine.calls
    finally:
        owner.close()


def test_missing_caps_drop_cannot_use_initial_caps_absence(tmp_path):
    """Before-start capability absence does not replace the declared drop policy."""
    engine = Engine()

    def mutate(snapshot):
        snapshot["EffectiveCaps"] = None
        snapshot["HostConfig"]["CapDrop"] = []

    engine.mutate = mutate
    owner = supervisor(tmp_path, engine)
    try:
        with pytest.raises(ValueError, match="container_policy_mismatch"):
            owner.create(specification(tmp_path))
    finally:
        owner.close()
