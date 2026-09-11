"""Check the explicit engine process boundary and Linux cleanup observations."""

from __future__ import annotations

import json
import socket
import sys

import pytest

from tests.unit.automation.test_fleet_containment import specification

pytestmark = pytest.mark.precommit


@pytest.fixture
def engine_process(tmp_path, monkeypatch):
    """Replace only Podman with an executable that records exact arguments and environment."""
    from hephaestus.automation import fleet_podman

    private = tmp_path / "engine"
    private.mkdir(mode=0o700)
    executable = private / "podman"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "root = pathlib.Path(__file__).parent\n"
        "with (root / 'calls.jsonl').open('a') as stream:\n"
        " stream.write(json.dumps({'argv':sys.argv[1:],'env':dict(os.environ)})+'\\n')\n"
        "args=sys.argv[1:]\n"
        "if 'create' in args: print('b'*64)\n"
        "elif 'inspect' in args: print(json.dumps([{'Id':'b'*64}]))\n"
        "elif 'exists' in args: sys.exit(int((root / 'exists-code').read_text()))\n"
        "elif 'start' in args: sys.stdout.write(sys.stdin.readline()); sys.stdout.flush()\n"
    )
    executable.chmod(0o700)
    (private / "exists-code").write_text("0")
    home = private / "home"
    home.mkdir(mode=0o700)
    with socket.socket(socket.AF_UNIX) as connection:
        socket_path = private / "engine.sock"
        connection.bind(str(socket_path))
        socket_path.chmod(0o600)
        monkeypatch.setenv("CONTAINER_HOST", "unix:///untrusted.sock")
        monkeypatch.setenv("AGAMEMNON_API_KEY", "synthetic-only")
        monkeypatch.setenv("HTTP_PROXY", "http://synthetic.invalid")
        engine = fleet_podman.PodmanEngine(executable, socket_path, home)
        yield engine, private
        engine.close()


def test_explicit_engine_context_and_finite_container_environment(engine_process, tmp_path):
    """No ambient context, image environment, proxy, or authority can enter a tool container."""
    from hephaestus.automation.fleet_containment import TOOL_ENVIRONMENT

    engine, private = engine_process
    spec = specification(tmp_path)
    assert engine.create(spec, "c" * 32) == "b" * 64
    call = json.loads((private / "calls.jsonl").read_text().splitlines()[0])
    args, environment = call["argv"], call["env"]
    assert args[:3] == ["--remote", "--url", "unix://" + str(private / "engine.sock")]
    assert "--unsetenv-all" in args and "--http-proxy=false" in args
    assert "--pull=never" in args and "--network=none" in args
    assert "--userns=keep-id:uid=1000,gid=1000" in args
    assert "--cap-drop=ALL" in args and "--read-only" in args
    assert "--security-opt=no-new-privileges" in args
    assert "--memory-swap=" + str(spec.memory_bytes) in args
    assert args[-4:] == [spec.image_digest, "exec-server", "--listen", "stdio"]
    actual_env = dict(
        args[index + 1].split("=", 1) for index, arg in enumerate(args) if arg == "--env"
    )
    assert actual_env == TOOL_ENVIRONMENT
    assert set(environment) <= {
        "HOME",
        "PATH",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "LC_CTYPE",
    }
    assert environment["HOME"] == str(private / "home")
    assert "--mount" in args and str(spec.workspace) in args[args.index("--mount") + 1]


def test_engine_attachment_uses_the_owned_full_id_and_real_stdio(engine_process):
    """Preserve exec-server streams without a shell or implicit container selection."""
    engine, private = engine_process
    process = engine.attach("b" * 64)
    output, _ = process.communicate(b"synthetic-no-auth\n", timeout=3)
    assert output == b"synthetic-no-auth\n" and process.returncode == 0
    call = json.loads((private / "calls.jsonl").read_text().splitlines()[0])
    assert call["argv"][-5:] == [
        "start",
        "--attach",
        "--interactive",
        "--sig-proxy=false",
        "b" * 64,
    ]


def test_tool_hostname_and_environment_are_explicit_before_engine_creation(
    engine_process, tmp_path
):
    """Prevent a runtime-generated hostname from extending the finite tool environment."""
    engine, private = engine_process
    engine.create(specification(tmp_path), "c" * 32)
    arguments = json.loads((private / "calls.jsonl").read_text().splitlines()[0])["argv"]
    assert "--hostname=fleet-tool" in arguments
    assert any(
        argument == "--env" and arguments[index + 1] == "HOSTNAME=fleet-tool"
        for index, argument in enumerate(arguments)
    )


def test_workspace_gets_a_private_selinux_label_without_disabling_enforcement(
    engine_process, tmp_path
):
    """Relabel only the exclusive workspace bind for its private container domain."""
    engine, private = engine_process
    engine.create(specification(tmp_path), "c" * 32)
    arguments = json.loads((private / "calls.jsonl").read_text().splitlines()[0])["argv"]
    assert arguments[arguments.index("--mount") + 1].endswith(
        ",bind-propagation=rprivate,relabel=private"
    )
    assert "--security-opt=label=disable" not in arguments
    assert "--privileged" not in arguments


@pytest.mark.parametrize("container_id", ["short-id", "--all", "b" * 63, "../other"])
def test_engine_refuses_ambiguous_container_targets(engine_process, container_id):
    """No lifecycle command can target a name, option, prefix, or unrelated selector."""
    engine, private = engine_process
    with pytest.raises(ValueError, match="invalid_container_identity"):
        engine.remove(container_id)
    assert not (private / "calls.jsonl").exists()


def test_engine_distinguishes_absent_from_unavailable(engine_process):
    """An engine error cannot establish causal container absence."""
    engine, private = engine_process
    assert engine.exists("b" * 64) is True
    (private / "exists-code").write_text("1")
    assert engine.exists("b" * 64) is False
    (private / "exists-code").write_text("125")
    with pytest.raises(RuntimeError, match="engine_command_failed"):
        engine.exists("b" * 64)


def test_engine_identity_binds_executable_and_socket_replacement(engine_process):
    """Replacement of either control endpoint changes the durable context identity."""
    engine, private = engine_process
    before = engine.identity()
    (private / "podman").write_text("#!/bin/sh\nexit 99\n")
    assert engine.identity() != before


@pytest.mark.parametrize("source", ["home", "engine_parent"])
def test_engine_refuses_workspace_overlap_with_control_authority(engine_process, tmp_path, source):
    """Do not mount the engine socket or its private configuration into contained tools."""
    from dataclasses import replace

    engine, private = engine_process
    workspace = private / "home" if source == "home" else private
    spec = replace(specification(tmp_path), workspace=workspace)
    with pytest.raises(ValueError, match="engine_authority_overlap"):
        engine.create(spec, "c" * 32)
    assert not (private / "calls.jsonl").exists()


@pytest.fixture
def kernel_files(tmp_path, monkeypatch):
    """Supply synthetic Linux proc and cgroup files at the filesystem boundary."""
    from hephaestus.automation import fleet_podman

    proc, cgroup = tmp_path / "proc", tmp_path / "cgroup"
    proc.mkdir()
    cgroup.mkdir()
    monkeypatch.setattr(fleet_podman, "_PROC", proc)
    monkeypatch.setattr(fleet_podman, "_CGROUP", cgroup)
    monkeypatch.setattr(fleet_podman.sys, "platform", "linux")
    boot = proc / "sys/kernel/random/boot_id"
    boot.parent.mkdir(parents=True)
    boot.write_text("synthetic-boot-1\n")
    cid = "b" * 64
    scope = cgroup / "user.slice" / f"libpod-{cid}.scope"
    scope.mkdir(parents=True)
    (scope / "cgroup.procs").write_text("123\n456\n")
    (scope / "memory.max").write_text(str(1024**3))
    (scope / "cpu.max").write_text("100000 100000")
    (scope / "pids.max").write_text("128")
    for pid in (123, 456):
        base = proc / str(pid)
        base.mkdir()
        fields = ["S"] + ["0"] * 18 + [str(pid * 10)] + ["0"] * 5
        (base / "stat").write_text(f"{pid} (synthetic name) " + " ".join(fields))
        (base / "cgroup").write_text(f"0::/user.slice/libpod-{cid}.scope\n")
        (base / "status").write_text("NoNewPrivs:\t1\nCapEff:\t0000000000000000\n")
        (base / "ns").mkdir()
        for name in ("pid", "mnt", "net", "ipc"):
            (base / "ns" / name).symlink_to(f"{name}:[100]")
    (proc / "self/ns").mkdir(parents=True)
    for name in ("pid", "mnt", "net", "ipc"):
        (proc / "self/ns" / name).symlink_to(f"{name}:[200]")
    snapshot = {"State": {"Pid": 123, "CgroupPath": f"/user.slice/libpod-{cid}.scope"}}
    return fleet_podman.LinuxKernel(), proc, scope, snapshot, specification(tmp_path)


def test_kernel_observation_binds_limits_namespaces_and_original_processes(kernel_files):
    """Capture immutable process identities and recheck their absence after disposal."""
    kernel, proc, scope, snapshot, spec = kernel_files
    before = kernel.capture("b" * 64, snapshot, spec)
    assert before["processes"] == [
        {"pid": 123, "startTimeTicks": "1230"},
        {"pid": 456, "startTimeTicks": "4560"},
    ]
    assert kernel.absent(before) is False
    import shutil

    shutil.rmtree(scope)
    assert kernel.absent(before) is False
    for pid in (123, 456):
        shutil.rmtree(proc / str(pid))
    assert kernel.absent(before) is True
    (proc / "sys/kernel/random/boot_id").write_text("different-boot")
    assert kernel.absent(before) is False


@pytest.mark.parametrize(
    "failure", ["foreign_scope", "memory", "cpu", "pids", "namespace", "capability", "privilege"]
)
def test_kernel_rejects_incomplete_or_different_enforcement(kernel_files, failure):
    """The engine inspect response alone cannot establish the process boundary."""
    kernel, proc, scope, snapshot, spec = kernel_files
    if failure == "foreign_scope":
        snapshot["State"]["CgroupPath"] = "/user.slice/other.scope"
    elif failure in {"memory", "cpu", "pids"}:
        (
            scope / {"memory": "memory.max", "cpu": "cpu.max", "pids": "pids.max"}[failure]
        ).write_text("max")
    elif failure == "namespace":
        target = proc / "123/ns/net"
        target.unlink()
        target.symlink_to("net:[200]")
    else:
        (proc / "123/status").write_text(
            "NoNewPrivs:\t0\nCapEff:\t0\n"
            if failure == "privilege"
            else "NoNewPrivs:\t1\nCapEff:\t1\n"
        )
    with pytest.raises(ValueError, match="kernel_boundary_unconfirmed"):
        kernel.capture("b" * 64, snapshot, spec)
