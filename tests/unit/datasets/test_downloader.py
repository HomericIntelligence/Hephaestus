#!/usr/bin/env python3
"""Tests for dataset downloading utilities."""

import gzip
from http.client import HTTPMessage
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from hephaestus.datasets import downloader
from hephaestus.datasets.downloader import (
    EMNIST_SPLITS,
    CIFAR10Downloader,
    CIFAR100Downloader,
    DatasetDownloader,
    EMNISTDownloader,
    FashionMNISTDownloader,
    MNISTDownloader,
)


class TestDatasetDownloader:
    """Tests for DatasetDownloader."""

    def test_init_strips_trailing_slash(self) -> None:
        """Base URL trailing slash is stripped."""
        d = DatasetDownloader("https://example.com/data/")
        assert d.base_url == "https://example.com/data"

    def test_init_defaults(self) -> None:
        """Default values are set correctly."""
        d = DatasetDownloader("https://example.com")
        assert d.max_retries == 3
        assert len(d.retry_delays) == 3

    def test_init_custom_retries(self) -> None:
        """Custom retry count is respected."""
        d = DatasetDownloader("https://example.com", max_retries=5)
        assert d.max_retries == 5

    def test_init_custom_user_agent(self) -> None:
        """Custom user agent is stored."""
        d = DatasetDownloader("https://example.com", user_agent="TestAgent/1.0")
        assert d.user_agent == "TestAgent/1.0"

    @pytest.mark.parametrize(
        "bad_url",
        [
            "http://example.com/data",
            "file:///etc/passwd",
            "ftp://example.com/data",
            "gopher://example.com",
            "data:text/plain;base64,AA==",
            "/etc/passwd",  # no scheme at all
        ],
    )
    def test_init_rejects_non_https_scheme(self, bad_url: str) -> None:
        """Constructing with an insecure or non-web base URL raises ValueError."""
        with pytest.raises(ValueError):
            DatasetDownloader(bad_url)

    @pytest.mark.parametrize("base_url", ["http://example.com", "file:///etc"])
    def test_download_rejects_non_https_scheme_after_reassignment(
        self, base_url: str, tmp_path: Path
    ) -> None:
        """A base URL reassigned to an insecure scheme is rejected before urlopen.

        Guards the EMNIST mirror-fallback path, which mutates ``base_url``
        after construction and would otherwise bypass the constructor check.
        """
        downloader = DatasetDownloader("https://example.com")
        downloader.base_url = base_url
        with pytest.raises(ValueError, match="non-HTTPS"):
            downloader.download_with_retry("passwd", tmp_path / "out")

    def test_redirect_handler_rejects_plaintext_http(self) -> None:
        """An HTTPS download cannot follow a redirect to plaintext HTTP."""
        with pytest.raises(ValueError, match="non-HTTPS"):
            downloader._HTTPSRedirectHandler().redirect_request(
                Request("https://example.com/dataset"),
                MagicMock(),
                302,
                "Found",
                HTTPMessage(),
                "http://example.com/dataset",
            )

    def test_decompress_gz(self, tmp_path: Path) -> None:
        """decompress_gz unpacks a valid gzip file."""
        content = b"hello compressed world"
        gz_path = tmp_path / "test.gz"
        out_path = tmp_path / "test.txt"

        with gzip.open(gz_path, "wb") as f:
            f.write(content)

        downloader = DatasetDownloader("https://example.com")
        success = downloader.decompress_gz(gz_path, out_path)

        assert success is True
        assert out_path.read_bytes() == content

    def test_decompress_gz_invalid_file(self, tmp_path: Path) -> None:
        """decompress_gz returns False for invalid gzip."""
        bad_gz = tmp_path / "bad.gz"
        bad_gz.write_bytes(b"not gzip data")
        out_path = tmp_path / "out.txt"

        downloader = DatasetDownloader("https://example.com")
        success = downloader.decompress_gz(bad_gz, out_path)
        assert success is False

    def test_download_with_retry_failure(self, tmp_path: Path) -> None:
        """download_with_retry returns False when all attempts fail."""
        downloader = DatasetDownloader("https://localhost:1", max_retries=1)
        downloader.retry_delays = [0]
        output = tmp_path / "file.bin"
        success = downloader.download_with_retry("nonexistent.bin", output, max_retries=1)
        assert success is False

    @patch("hephaestus.datasets.downloader._HTTPS_OPENER.open")
    def test_download_with_retry_success(self, mock_urlopen, tmp_path: Path) -> None:
        """download_with_retry returns True on successful download."""
        content = b"file content"
        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.headers.get.return_value = str(len(content))
        mock_response.read.side_effect = [content, b""]
        mock_urlopen.return_value = mock_response

        downloader = DatasetDownloader("https://example.com")
        output = tmp_path / "file.bin"
        success = downloader.download_with_retry("test.bin", output, max_retries=1)
        assert success is True
        assert output.exists()

    @patch("hephaestus.datasets.downloader._HTTPS_OPENER.open")
    def test_download_retries_on_http_error(self, mock_urlopen, tmp_path: Path) -> None:
        """download_with_retry retries on HTTPError."""
        mock_urlopen.side_effect = HTTPError(
            url="http://example.com/file",
            code=503,
            msg="Service Unavailable",
            hdrs=HTTPMessage(),
            fp=None,
        )
        downloader = DatasetDownloader("https://example.com", max_retries=2)
        downloader.retry_delays = [0, 0]
        output = tmp_path / "file.bin"
        success = downloader.download_with_retry("test.bin", output, max_retries=2)
        assert success is False
        assert mock_urlopen.call_count == 2

    @patch("hephaestus.datasets.downloader._HTTPS_OPENER.open")
    def test_download_retries_on_url_error(self, mock_urlopen, tmp_path: Path) -> None:
        """download_with_retry retries on URLError."""
        mock_urlopen.side_effect = URLError(reason="connection refused")
        downloader = DatasetDownloader("https://example.com", max_retries=2)
        downloader.retry_delays = [0, 0]
        output = tmp_path / "file.bin"
        success = downloader.download_with_retry("test.bin", output, max_retries=2)
        assert success is False

    @patch("hephaestus.datasets.downloader._HTTPS_OPENER.open")
    def test_download_shows_progress_no_content_length(self, mock_urlopen, tmp_path: Path) -> None:
        """Handles missing Content-Length header gracefully."""
        content = b"data"
        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.headers.get.return_value = "0"  # No content length
        mock_response.read.side_effect = [content, b""]
        mock_urlopen.return_value = mock_response

        downloader = DatasetDownloader("https://example.com")
        output = tmp_path / "file.bin"
        success = downloader.download_with_retry("test.bin", output, max_retries=1)
        assert success is True

    @patch("hephaestus.datasets.downloader._HTTPS_OPENER.open")
    def test_download_retries_on_oserror(self, mock_urlopen, tmp_path: Path) -> None:
        """download_with_retry retries on OSError."""
        mock_urlopen.side_effect = OSError("disk write failed")
        downloader = DatasetDownloader("https://example.com", max_retries=2)
        downloader.retry_delays = [0, 0]
        output = tmp_path / "file.bin"
        success = downloader.download_with_retry("test.bin", output, max_retries=2)
        assert success is False
        assert mock_urlopen.call_count == 2

    @patch("hephaestus.datasets.downloader._HTTPS_OPENER.open")
    def test_download_retry_delay_clamped_to_last(self, mock_urlopen, tmp_path: Path) -> None:
        """When attempt index exceeds retry_delays length, last delay is used."""
        mock_urlopen.side_effect = URLError(reason="refused")
        downloader = DatasetDownloader("https://example.com", max_retries=4)
        downloader.retry_delays = [0, 0]  # fewer delays than retries
        output = tmp_path / "file.bin"
        success = downloader.download_with_retry("test.bin", output, max_retries=4)
        assert success is False


class TestMNISTDownloader:
    """Tests for MNISTDownloader."""

    def test_inherits_downloader(self) -> None:
        """MNISTDownloader is a DatasetDownloader."""
        d = MNISTDownloader()
        assert isinstance(d, DatasetDownloader)

    def test_files_list_populated(self) -> None:
        """MNIST files list has 4 entries."""
        d = MNISTDownloader()
        assert len(d.files) == 4

    def test_download_mnist_already_exists(self, tmp_path: Path) -> None:
        """download_mnist skips files that already exist."""
        d = MNISTDownloader()
        output_dir = tmp_path / "mnist"
        output_dir.mkdir()

        for _, output_filename in d.files:
            (output_dir / output_filename).write_bytes(b"dummy")

        success = d.download_mnist(str(output_dir))
        assert success is True

    @patch.object(DatasetDownloader, "download_with_retry", return_value=False)
    def test_download_mnist_failure(self, mock_download, tmp_path: Path) -> None:
        """download_mnist returns False when download fails."""
        d = MNISTDownloader()
        success = d.download_mnist(str(tmp_path / "mnist"))
        assert success is False

    @patch.object(DatasetDownloader, "download_with_retry", return_value=True)
    @patch.object(DatasetDownloader, "decompress_gz", return_value=True)
    def test_download_mnist_success(self, mock_decompress, mock_download, tmp_path: Path) -> None:
        """download_mnist returns True when all downloads succeed."""
        d = MNISTDownloader()
        mnist_dir = tmp_path / "mnist"
        mnist_dir.mkdir()
        # Create dummy gz files so unlink() doesn't fail
        for gz_filename, _ in d.files:
            (mnist_dir / gz_filename).write_bytes(b"dummy")
        success = d.download_mnist(str(mnist_dir))
        assert success is True
        assert mock_download.call_count == len(d.files)
        assert mock_decompress.call_count == len(d.files)

    @patch.object(DatasetDownloader, "download_with_retry", return_value=True)
    @patch.object(DatasetDownloader, "decompress_gz", return_value=False)
    def test_download_mnist_decompress_failure(
        self, mock_decompress, mock_download, tmp_path: Path
    ) -> None:
        """download_mnist returns False when decompression fails."""
        d = MNISTDownloader()
        mnist_dir = tmp_path / "mnist"
        mnist_dir.mkdir()
        for gz_filename, _ in d.files:
            (mnist_dir / gz_filename).write_bytes(b"dummy")
        success = d.download_mnist(str(mnist_dir))
        assert success is False


class TestMain:
    """Tests for the main() entry point."""

    @patch("hephaestus.datasets.downloader.MNISTDownloader")
    def test_main_mnist_success(self, mock_cls, tmp_path: Path) -> None:
        """main() exits 0 on successful MNIST download."""
        mock_instance = MagicMock()
        mock_instance.download_mnist.return_value = True
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["prog", "mnist", str(tmp_path)]):
            from hephaestus.datasets.downloader import main

            exit_code = main()
        assert exit_code == 0

    @patch("hephaestus.datasets.downloader.MNISTDownloader")
    def test_main_mnist_failure(self, mock_cls, tmp_path: Path) -> None:
        """main() exits 1 on failed MNIST download."""
        mock_instance = MagicMock()
        mock_instance.download_mnist.return_value = False
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["prog", "mnist"]):
            from hephaestus.datasets.downloader import main

            exit_code = main()
        assert exit_code == 1

    @patch("hephaestus.datasets.downloader.MNISTDownloader")
    def test_main_mnist_default_output_dir(self, mock_cls) -> None:
        """main() uses default output dir when none is provided."""
        mock_instance = MagicMock()
        mock_instance.download_mnist.return_value = True
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["prog", "mnist"]):
            from hephaestus.datasets.downloader import main

            exit_code = main()

        assert exit_code == 0
        mock_instance.download_mnist.assert_called_once_with("datasets/mnist")


class TestFashionMNISTDownloader:
    """Tests for FashionMNISTDownloader."""

    def test_inherits_downloader(self) -> None:
        d = FashionMNISTDownloader()
        assert isinstance(d, DatasetDownloader)

    def test_files_list_populated(self) -> None:
        d = FashionMNISTDownloader()
        assert len(d.files) == 4

    def test_download_skips_existing_files(self, tmp_path: Path) -> None:
        d = FashionMNISTDownloader()
        out = tmp_path / "fashion_mnist"
        out.mkdir()
        for _, output_filename in d.files:
            (out / output_filename).write_bytes(b"dummy")
        assert d.download_fashion_mnist(str(out)) is True

    @patch.object(DatasetDownloader, "download_with_retry", return_value=False)
    def test_download_failure(self, _mock, tmp_path: Path) -> None:
        assert FashionMNISTDownloader().download_fashion_mnist(str(tmp_path)) is False

    @patch.object(DatasetDownloader, "download_with_retry", return_value=True)
    @patch.object(DatasetDownloader, "decompress_gz", return_value=True)
    def test_download_success(self, _dc, _dl, tmp_path: Path) -> None:
        d = FashionMNISTDownloader()
        out = tmp_path / "fashion_mnist"
        out.mkdir()
        for gz_filename, _ in d.files:
            (out / gz_filename).write_bytes(b"dummy")
        assert d.download_fashion_mnist(str(out)) is True


class TestCIFAR100Downloader:
    """Tests for CIFAR100Downloader."""

    def test_inherits_downloader(self) -> None:
        assert isinstance(CIFAR100Downloader(), DatasetDownloader)

    @patch.object(DatasetDownloader, "download_with_retry", return_value=False)
    def test_download_failure(self, _mock, tmp_path: Path) -> None:
        assert CIFAR100Downloader().download_cifar100(str(tmp_path)) is False

    @patch.object(DatasetDownloader, "download_with_retry", return_value=True)
    def test_download_tar_extraction_failure(self, _mock, tmp_path: Path) -> None:
        # download_with_retry succeeds but the tar file is empty/invalid
        tar_path = tmp_path / "cifar-100-python.tar.gz"
        tar_path.write_bytes(b"not a valid tar")
        assert CIFAR100Downloader().download_cifar100(str(tmp_path)) is False


class TestCIFAR10Downloader:
    """Tests for CIFAR10Downloader."""

    def test_inherits_downloader(self) -> None:
        assert isinstance(CIFAR10Downloader(), DatasetDownloader)

    def test_raises_import_error_without_numpy(self, tmp_path: Path) -> None:
        import sys

        with patch.dict(sys.modules, {"numpy": None}):
            with pytest.raises(ImportError, match="numpy"):
                CIFAR10Downloader().download_cifar10(str(tmp_path))


class TestEMNISTDownloader:
    """Tests for EMNISTDownloader."""

    def test_inherits_downloader(self) -> None:
        assert isinstance(EMNISTDownloader(), DatasetDownloader)

    def test_invalid_split_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown EMNIST split"):
            EMNISTDownloader().download_emnist(split="invalid_split")

    def test_valid_splits(self) -> None:
        assert "balanced" in EMNIST_SPLITS
        assert "digits" in EMNIST_SPLITS
        assert "mnist" in EMNIST_SPLITS

    @patch.object(DatasetDownloader, "download_with_retry", return_value=False)
    def test_download_failure_all_mirrors(self, _mock, tmp_path: Path) -> None:
        assert EMNISTDownloader().download_emnist("balanced", str(tmp_path)) is False


class TestSecurityHardening:
    """Regression tests for #478: checksum verification + safe tar extraction."""

    def test_dataset_md5_includes_known_files(self) -> None:
        """The per-file MD5 map covers CIFAR + Fashion-MNIST downloads."""
        from hephaestus.datasets.downloader import _DATASET_MD5

        for name in (
            "cifar-10-python.tar.gz",
            "cifar-100-python.tar.gz",
            "train-images-idx3-ubyte.gz",
            "t10k-images-idx3-ubyte.gz",
        ):
            assert name in _DATASET_MD5

    def test_verify_or_remove_passes_for_correct_md5(self, tmp_path: Path) -> None:
        """A file matching the known MD5 verifies True and is kept."""
        from hephaestus.datasets.downloader import _DATASET_MD5, _verify_or_remove

        name = "cifar-10-python.tar.gz"
        target = tmp_path / name
        target.write_bytes(b"")  # MD5 of empty = d41d8cd98f00b204e9800998ecf8427e
        # Patch the expected MD5 to the digest of the bytes we just wrote.
        with patch.dict(_DATASET_MD5, {name: "d41d8cd98f00b204e9800998ecf8427e"}):
            assert _verify_or_remove(target, name) is True
        assert target.exists()

    def test_verify_or_remove_removes_on_mismatch(self, tmp_path: Path) -> None:
        """A file failing the MD5 check is verified False AND deleted."""
        from hephaestus.datasets.downloader import _verify_or_remove

        target = tmp_path / "cifar-10-python.tar.gz"
        target.write_bytes(b"tampered content")
        # The real MD5 in _DATASET_MD5 will not match — the helper must remove.
        assert _verify_or_remove(target, "cifar-10-python.tar.gz") is False
        assert not target.exists()

    def test_verify_or_remove_unknown_filename_passes_with_warning(self, tmp_path: Path) -> None:
        """A file with no recorded checksum is allowed through (logged)."""
        from hephaestus.datasets.downloader import _verify_or_remove

        target = tmp_path / "novel.bin"
        target.write_bytes(b"x")
        assert _verify_or_remove(target, "novel.bin") is True
        assert target.exists()

    def test_extractall_uses_data_filter(self) -> None:
        """Both CIFAR extract sites pass filter='data' to extractall (CWE-22)."""
        from hephaestus.datasets.downloader import __file__ as downloader_file

        src = Path(downloader_file).read_text()
        # Two extract sites, both with the filter.
        assert src.count('tf.extractall(output_path, filter="data")') == 2
        # No bare extractall(output_path) without filter.
        assert "tf.extractall(output_path)\n" not in src

    def test_fashion_mnist_url_is_https(self) -> None:
        """Fashion-MNIST downloader uses HTTPS, not plain HTTP."""
        d = FashionMNISTDownloader()
        assert d.base_url.startswith("https://")


class TestMainJsonAndAll:
    """Additional smoke tests for main() covering --json + 'all' dataset branches."""

    def test_main_mnist_success_json(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        from hephaestus.datasets import downloader

        monkeypatch.setattr("sys.argv", ["dl", "mnist", "--json"])
        with patch.object(downloader.MNISTDownloader, "download_mnist", return_value=True):
            exit_code = downloader.main()
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert payload["datasets"] == ["mnist"]

    def test_main_failure_json(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        from hephaestus.datasets import downloader

        monkeypatch.setattr("sys.argv", ["dl", "cifar10", "--json"])
        with patch.object(downloader.CIFAR10Downloader, "download_cifar10", return_value=False):
            exit_code = downloader.main()
        assert exit_code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert "failed" in payload["message"]

    def test_main_all_invokes_each_downloader(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hephaestus.datasets import downloader

        monkeypatch.setattr("sys.argv", ["dl", "all"])
        with (
            patch.object(downloader.MNISTDownloader, "download_mnist", return_value=True) as m1,
            patch.object(
                downloader.FashionMNISTDownloader, "download_fashion_mnist", return_value=True
            ) as m2,
            patch.object(downloader.CIFAR10Downloader, "download_cifar10", return_value=True) as m3,
            patch.object(
                downloader.CIFAR100Downloader, "download_cifar100", return_value=True
            ) as m4,
            patch.object(downloader.EMNISTDownloader, "download_emnist", return_value=True) as m5,
        ):
            exit_code = downloader.main()
        assert exit_code == 0
        for m in (m1, m2, m3, m4, m5):
            m.assert_called_once()

    def test_main_emnist_with_split(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hephaestus.datasets import downloader

        monkeypatch.setattr("sys.argv", ["dl", "emnist", "--split", "digits"])
        with patch.object(
            downloader.EMNISTDownloader, "download_emnist", return_value=True
        ) as mock_dl:
            exit_code = downloader.main()
        assert exit_code == 0
        assert mock_dl.call_args.args[0] == "digits"
