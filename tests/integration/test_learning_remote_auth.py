"""Verify learning authentication with real Git and a local HTTP fixture."""

import base64
import functools
import http.server
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.automation.mnemosyne_binding import MnemosyneBindingService
from hephaestus.automation.mnemosyne_learning_preparation import BoundLearningWorkspace


@pytest.mark.parametrize("backend", ["learning", "binding"])
@pytest.mark.parametrize("admitted", [True, False])
def test_learning_probe_uses_trusted_helper(tmp_path: Path, admitted: bool, backend: str) -> None:
    """The default probe obtains credentials without repository helper settings."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(remote), "update-server-info"], check=True)
    calls = tmp_path / "helper-calls"
    helper = tmp_path / "gh"
    helper.write_text(
        "#!/bin/sh\n"
        'test "$GH_TOKEN" = synthetic-token || exit 1\n'
        "if [ \"$1 $2\" = 'auth status' ]; then exit 0; fi\n"
        f"printf 'called\\n' >> '{calls}'\n"
        "if [ \"$3\" = get ]; then printf 'username=fixture\\npassword=synthetic\\n'; fi\n"
    )
    helper.chmod(0o755)
    expected = "Basic " + base64.b64encode(b"fixture:synthetic").decode()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="fixture"')
                self.end_headers()
                return
            super().do_GET()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(tmp_path))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    local = tmp_path / "local"
    subprocess.run(["git", "init", str(local)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(local),
            "remote",
            "add",
            "origin",
            f"http://127.0.0.1:{server.server_port}/remote.git",
        ],
        check=True,
    )
    try:
        with (
            patch.dict("os.environ", {"GH_TOKEN": "synthetic-token"}, clear=True),
            patch(
                "hephaestus.automation.remote_git.trusted_gh_executable",
                return_value=str(helper) if admitted else None,
            ),
            patch(
                "hephaestus.automation.mnemosyne_binding.trusted_gh_executable",
                return_value=str(helper) if admitted else None,
            ),
        ):
            if admitted:
                if backend == "learning":
                    assert not BoundLearningWorkspace(timeout_s=10)._remote_branch_is_published(
                        local, "learn/fixture"
                    )
                else:
                    result = MnemosyneBindingService(timeout_s=10)._remote_git(
                        local, "ls-remote", "--exit-code", "origin", "refs/heads/learn/fixture"
                    )
                    assert result.returncode == 2
            else:
                with pytest.raises(RuntimeError, match=r"^remote Git authentication unavailable"):
                    if backend == "learning":
                        BoundLearningWorkspace(timeout_s=10)._remote_branch_is_published(
                            local, "learn/fixture"
                        )
                    else:
                        MnemosyneBindingService(timeout_s=10)._remote_git(
                            local, "ls-remote", "--exit-code", "origin", "refs/heads/learn/fixture"
                        )
        if admitted:
            assert calls.read_text().splitlines()
        else:
            assert not calls.exists()
        refs = subprocess.run(["git", "-C", str(remote), "show-ref"], capture_output=True)
        assert refs.returncode == 1 and not refs.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
