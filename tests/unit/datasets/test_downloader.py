#!/usr/bin/env python3
"""Tests for dataset downloading utilities."""

import gzip
import hashlib
import io
import os
import stat
import tarfile
from collections.abc import Callable
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


def _cifar_archive_bytes(batch_bytes: bytes) -> bytes:
    """Build a minimal CIFAR tar archive for downloader lifecycle tests."""
    archive = io.BytesIO()
    member = tarfile.TarInfo("cifar-10-batches-py/data_batch_1")
    member.size = len(batch_bytes)
    member.mode = 0o600
    with tarfile.open(fileobj=archive, mode="w") as tf:
        tf.addfile(member, io.BytesIO(batch_bytes))
    return archive.getvalue()


class TestDatasetDownloader:
    """Tests for DatasetDownloader."""

    def test_init_strips_trailing_slash(self) -> None:
        """Base URL trailing slash is stripped."""
        d = DatasetDownloader("https://example.com/data/", checksum_manifest={})
        assert d.base_url == "https://example.com/data"

    def test_init_requires_explicit_checksum_manifest_for_generic_downloader(self) -> None:
        """Generic direct instances must not silently default to no checksums."""
        with pytest.raises(ValueError, match="checksum_manifest"):
            DatasetDownloader("https://example.com")

    def test_init_defaults(self) -> None:
        """Default values are set correctly."""
        d = DatasetDownloader("https://example.com", checksum_manifest={})
        assert d.max_retries == 3
        assert len(d.retry_delays) == 3

    def test_init_custom_retries(self) -> None:
        """Custom retry count is respected."""
        d = DatasetDownloader("https://example.com", max_retries=5, checksum_manifest={})
        assert d.max_retries == 5

    def test_init_custom_user_agent(self) -> None:
        """Custom user agent is stored."""
        d = DatasetDownloader(
            "https://example.com",
            user_agent="TestAgent/1.0",
            checksum_manifest={},
        )
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
            DatasetDownloader(bad_url, checksum_manifest={})

    @pytest.mark.parametrize("base_url", ["http://example.com", "file:///etc"])
    def test_download_rejects_non_https_scheme_after_reassignment(
        self, base_url: str, tmp_path: Path
    ) -> None:
        """A base URL reassigned to an insecure scheme is rejected before urlopen.

        Guards the EMNIST mirror-fallback path, which mutates ``base_url``
        after construction and would otherwise bypass the constructor check.
        """
        downloader = DatasetDownloader("https://example.com", checksum_manifest={})
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

        downloader = DatasetDownloader("https://example.com", checksum_manifest={})
        success = downloader.decompress_gz(gz_path, out_path)

        assert success is True
        assert out_path.read_bytes() == content

    def test_decompress_gz_invalid_file(self, tmp_path: Path) -> None:
        """decompress_gz returns False for invalid gzip."""
        bad_gz = tmp_path / "bad.gz"
        bad_gz.write_bytes(b"not gzip data")
        out_path = tmp_path / "out.txt"

        downloader = DatasetDownloader("https://example.com", checksum_manifest={})
        success = downloader.decompress_gz(bad_gz, out_path)
        assert success is False

    def test_download_with_retry_failure(self, tmp_path: Path) -> None:
        """download_with_retry returns False when all attempts fail."""
        downloader = DatasetDownloader(
            "https://localhost:1",
            max_retries=1,
            checksum_manifest={},
        )
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

        downloader = DatasetDownloader("https://example.com", checksum_manifest={})
        output = tmp_path / "file.bin"
        success = downloader.download_with_retry("test.bin", output, max_retries=1)
        assert success is True
        assert output.exists()

    @patch("hephaestus.datasets.downloader._HTTPS_OPENER.open")
    def test_generic_download_rejects_and_deletes_checksum_mismatch(
        self, mock_urlopen, tmp_path: Path
    ) -> None:
        """A generic downloader with a manifest rejects corrupt known files."""
        content = b"corrupt bytes"
        filename = "known.bin"
        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.headers.get.return_value = str(len(content))
        mock_response.read.side_effect = [content, b""]
        mock_urlopen.return_value = mock_response

        downloader = DatasetDownloader(
            "https://example.com",
            checksum_manifest={filename: "0" * 32},
        )
        output = tmp_path / filename

        success = downloader.download_with_retry(filename, output, max_retries=1)

        assert success is False
        assert not output.exists()

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
        downloader = DatasetDownloader(
            "https://example.com",
            max_retries=2,
            checksum_manifest={},
        )
        downloader.retry_delays = [0, 0]
        output = tmp_path / "file.bin"
        success = downloader.download_with_retry("test.bin", output, max_retries=2)
        assert success is False
        assert mock_urlopen.call_count == 2

    @patch("hephaestus.datasets.downloader._HTTPS_OPENER.open")
    def test_download_retries_on_url_error(self, mock_urlopen, tmp_path: Path) -> None:
        """download_with_retry retries on URLError."""
        mock_urlopen.side_effect = URLError(reason="connection refused")
        downloader = DatasetDownloader(
            "https://example.com",
            max_retries=2,
            checksum_manifest={},
        )
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

        downloader = DatasetDownloader("https://example.com", checksum_manifest={})
        output = tmp_path / "file.bin"
        success = downloader.download_with_retry("test.bin", output, max_retries=1)
        assert success is True

    @patch("hephaestus.datasets.downloader._HTTPS_OPENER.open")
    def test_download_retries_on_oserror(self, mock_urlopen, tmp_path: Path) -> None:
        """download_with_retry retries on OSError."""
        mock_urlopen.side_effect = OSError("disk write failed")
        downloader = DatasetDownloader(
            "https://example.com",
            max_retries=2,
            checksum_manifest={},
        )
        downloader.retry_delays = [0, 0]
        output = tmp_path / "file.bin"
        success = downloader.download_with_retry("test.bin", output, max_retries=2)
        assert success is False
        assert mock_urlopen.call_count == 2

    @patch("hephaestus.datasets.downloader._HTTPS_OPENER.open")
    def test_download_retry_delay_clamped_to_last(self, mock_urlopen, tmp_path: Path) -> None:
        """When attempt index exceeds retry_delays length, last delay is used."""
        mock_urlopen.side_effect = URLError(reason="refused")
        downloader = DatasetDownloader(
            "https://example.com",
            max_retries=4,
            checksum_manifest={},
        )
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


class TestChecksumScoping:
    """Tests that same-named archives use the active dataset manifest."""

    @pytest.mark.parametrize(
        ("downloader_cls", "filename", "expected_md5"),
        [
            (
                MNISTDownloader,
                "train-images-idx3-ubyte.gz",
                "f68b3c2dcbeaaa9fbdd348bbdeb94873",
            ),
            (
                MNISTDownloader,
                "train-labels-idx1-ubyte.gz",
                "d53e105ee54ea40749a09fcbcd1e9432",
            ),
            (
                MNISTDownloader,
                "t10k-images-idx3-ubyte.gz",
                "9fb629c4189551a2d022fa330f9573f3",
            ),
            (
                MNISTDownloader,
                "t10k-labels-idx1-ubyte.gz",
                "ec29112dd5afa0611ce80d1b7f02629c",
            ),
            (
                FashionMNISTDownloader,
                "train-images-idx3-ubyte.gz",
                "8d4fb7e6c68d591d4c3dfef9ec88bf0d",
            ),
            (
                FashionMNISTDownloader,
                "train-labels-idx1-ubyte.gz",
                "25c81989df183df01b3e8a0aad5dffbe",
            ),
            (
                FashionMNISTDownloader,
                "t10k-images-idx3-ubyte.gz",
                "bef4ecab320f06d8554ea6380940ec79",
            ),
            (
                FashionMNISTDownloader,
                "t10k-labels-idx1-ubyte.gz",
                "bb300cfdad3c16e7a12a480ee83cd310",
            ),
        ],
    )
    def test_download_uses_dataset_specific_manifest(
        self,
        downloader_cls: Callable[[], DatasetDownloader],
        filename: str,
        expected_md5: str,
        tmp_path: Path,
    ) -> None:
        """Each MNIST-family archive is checked against its source's digest."""
        content = b"synthetic archive bytes"
        response = MagicMock()
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        response.headers.get.return_value = str(len(content))
        response.read.side_effect = [content, b""]
        target = tmp_path / filename

        with (
            patch.object(downloader._HTTPS_OPENER, "open", return_value=response),
            patch.object(downloader, "_file_md5", return_value=expected_md5) as file_md5,
        ):
            assert downloader_cls().download_with_retry(filename, target, max_retries=1)

        file_md5.assert_called_once_with(target)
        assert target.read_bytes() == content


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

    @patch.object(DatasetDownloader, "download_with_retry", return_value=True)
    def test_download_uses_safe_extraction_helper(self, _mock, tmp_path: Path) -> None:
        """CIFAR-100 routes extraction through the cross-version safety helper."""
        tar_path = tmp_path / "cifar-100-python.tar.gz"
        with tarfile.open(tar_path, "w"):
            pass

        with patch.object(downloader, "_extract_tar_safely") as extract:
            assert CIFAR100Downloader().download_cifar100(str(tmp_path)) is True

        extract.assert_called_once()


class TestCIFAR10Downloader:
    """Tests for CIFAR10Downloader."""

    def test_inherits_downloader(self) -> None:
        assert isinstance(CIFAR10Downloader(), DatasetDownloader)

    def test_raises_import_error_without_numpy(self, tmp_path: Path) -> None:
        import sys

        with patch.dict(sys.modules, {"numpy": None}):
            with pytest.raises(ImportError, match="numpy"):
                CIFAR10Downloader().download_cifar10(str(tmp_path))

    def test_download_uses_private_staging_through_conversion(self, tmp_path: Path) -> None:
        """CIFAR pickles remain private and ephemeral through conversion."""
        import sys

        tarball_name = "cifar-10-python.tar.gz"
        archive_bytes = _cifar_archive_bytes(b"verified batch")
        expected_md5 = hashlib.md5(archive_bytes, usedforsecurity=False).hexdigest()
        caller_batch_dir = tmp_path / "cifar-10-batches-py"
        caller_batch_dir.mkdir()
        (caller_batch_dir / "data_batch_1").write_bytes(b"caller replacement")

        staged_root: Path | None = None
        subject: CIFAR10Downloader

        def fake_download(
            filename: str,
            path: Path,
            max_retries: int | None = None,
        ) -> bool:
            assert filename == tarball_name
            assert max_retries is None
            assert path.parent != tmp_path
            path.write_bytes(archive_bytes)
            return True

        def observe_conversion(
            batch_dir: Path,
            output_dir: Path,
            _np: object,
        ) -> bool:
            nonlocal staged_root
            staged_root = batch_dir.parent
            assert output_dir == tmp_path
            assert batch_dir != caller_batch_dir
            assert (batch_dir / "data_batch_1").read_bytes() == b"verified batch"
            assert (staged_root / tarball_name).is_file()
            if os.name == "posix":
                assert stat.S_IMODE(staged_root.stat().st_mode) == 0o700
            return True

        with (
            patch.dict(sys.modules, {"numpy": MagicMock()}),
            patch.dict(downloader._CIFAR10_MD5, {tarball_name: expected_md5}),
            patch.object(
                downloader,
                "_extract_tar_safely",
                wraps=downloader._extract_tar_safely,
            ) as extract,
        ):
            subject = CIFAR10Downloader()
            with (
                patch.object(subject, "download_with_retry", side_effect=fake_download),
                patch.object(subject, "_convert_batches", side_effect=observe_conversion),
            ):
                assert subject.download_cifar10(str(tmp_path)) is True

        extract.assert_called_once()
        assert staged_root is not None
        assert not staged_root.exists()
        assert (caller_batch_dir / "data_batch_1").read_bytes() == b"caller replacement"

    def test_download_rejects_archive_replaced_after_verification(self, tmp_path: Path) -> None:
        """A post-verification archive replacement fails before extraction."""
        import sys

        tarball_name = "cifar-10-python.tar.gz"
        verified_bytes = _cifar_archive_bytes(b"verified batch")
        replacement_bytes = _cifar_archive_bytes(b"replacement batch")
        expected_md5 = hashlib.md5(verified_bytes, usedforsecurity=False).hexdigest()

        staged_root: Path | None = None
        subject: CIFAR10Downloader

        def replace_after_verification(
            filename: str,
            path: Path,
            max_retries: int | None = None,
        ) -> bool:
            nonlocal staged_root
            assert max_retries is None
            path.write_bytes(verified_bytes)
            assert downloader._verify_or_remove(path, filename, subject._checksums) is True
            path.write_bytes(replacement_bytes)
            staged_root = path.parent
            return True

        with (
            patch.dict(sys.modules, {"numpy": MagicMock()}),
            patch.dict(downloader._CIFAR10_MD5, {tarball_name: expected_md5}),
            patch.object(downloader, "_extract_tar_safely") as extract,
        ):
            subject = CIFAR10Downloader()
            with (
                patch.object(
                    subject,
                    "download_with_retry",
                    side_effect=replace_after_verification,
                ),
                patch.object(subject, "_convert_batches") as convert,
            ):
                assert subject.download_cifar10(str(tmp_path)) is False

        extract.assert_not_called()
        convert.assert_not_called()
        assert staged_root is not None
        assert not staged_root.exists()


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

    def test_dataset_md5_manifests_pin_authentic_hashes(self) -> None:
        """Each downloader manifest contains its upstream-published hashes."""
        from hephaestus.datasets.downloader import (
            _CIFAR10_MD5,
            _CIFAR100_MD5,
            _FASHION_MNIST_MD5,
            _MNIST_MD5,
        )

        assert _CIFAR10_MD5 == {
            "cifar-10-python.tar.gz": "c58f30108f718f92721af3b95e74349a",
        }
        assert _CIFAR100_MD5 == {
            "cifar-100-python.tar.gz": "eb9058c3a382ffc7106e4002c42a8d85",
        }
        assert _MNIST_MD5 == {
            "train-images-idx3-ubyte.gz": "f68b3c2dcbeaaa9fbdd348bbdeb94873",
            "train-labels-idx1-ubyte.gz": "d53e105ee54ea40749a09fcbcd1e9432",
            "t10k-images-idx3-ubyte.gz": "9fb629c4189551a2d022fa330f9573f3",
            "t10k-labels-idx1-ubyte.gz": "ec29112dd5afa0611ce80d1b7f02629c",
        }
        assert _FASHION_MNIST_MD5 == {
            "train-images-idx3-ubyte.gz": "8d4fb7e6c68d591d4c3dfef9ec88bf0d",
            "train-labels-idx1-ubyte.gz": "25c81989df183df01b3e8a0aad5dffbe",
            "t10k-images-idx3-ubyte.gz": "bef4ecab320f06d8554ea6380940ec79",
            "t10k-labels-idx1-ubyte.gz": "bb300cfdad3c16e7a12a480ee83cd310",
        }

    def test_verify_or_remove_passes_for_correct_md5(self, tmp_path: Path) -> None:
        """A file matching the known MD5 verifies True and is kept."""
        from hephaestus.datasets.downloader import _verify_or_remove

        name = "cifar-10-python.tar.gz"
        target = tmp_path / name
        content = b"known archive bytes"
        target.write_bytes(content)
        expected_md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()
        assert _verify_or_remove(target, name, {name: expected_md5}) is True
        assert target.exists()

    def test_verify_or_remove_removes_on_mismatch(self, tmp_path: Path) -> None:
        """A file failing the MD5 check is verified False AND deleted."""
        from hephaestus.datasets.downloader import _CIFAR10_MD5, _verify_or_remove

        target = tmp_path / "cifar-10-python.tar.gz"
        target.write_bytes(b"tampered content")
        # The pinned CIFAR-10 MD5 will not match — the helper must remove.
        assert _verify_or_remove(target, "cifar-10-python.tar.gz", _CIFAR10_MD5) is False
        assert not target.exists()

    def test_verify_or_remove_unknown_filename_passes_with_warning(self, tmp_path: Path) -> None:
        """A file with no recorded checksum is allowed through (logged)."""
        from hephaestus.datasets.downloader import _verify_or_remove

        target = tmp_path / "novel.bin"
        target.write_bytes(b"x")
        assert _verify_or_remove(target, "novel.bin", {}) is True
        assert target.exists()

    def test_extract_tar_uses_data_filter(self, tmp_path: Path) -> None:
        """Modern Python uses the standard-library safe extraction filter."""
        archive = MagicMock(spec=tarfile.TarFile)

        downloader._extract_tar_safely(archive, tmp_path)

        archive.extractall.assert_called_once_with(tmp_path, filter="data")
        archive.getmembers.assert_not_called()

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
