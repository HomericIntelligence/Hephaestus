#!/usr/bin/env python3
"""Tests for dataset downloading utilities."""

import gzip
from http.client import HTTPMessage
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from hephaestus.datasets.downloader import DatasetDownloader, MNISTDownloader


class TestDatasetDownloader:
    """Tests for DatasetDownloader."""

    def test_init_strips_trailing_slash(self) -> None:
        """Base URL trailing slash is stripped."""
        d = DatasetDownloader("http://example.com/data/")
        assert d.base_url == "http://example.com/data"

    def test_init_defaults(self) -> None:
        """Default values are set correctly."""
        d = DatasetDownloader("http://example.com")
        assert d.max_retries == 3
        assert len(d.retry_delays) == 3

    def test_init_custom_retries(self) -> None:
        """Custom retry count is respected."""
        d = DatasetDownloader("http://example.com", max_retries=5)
        assert d.max_retries == 5

    def test_init_custom_user_agent(self) -> None:
        """Custom user agent is stored."""
        d = DatasetDownloader("http://example.com", user_agent="TestAgent/1.0")
        assert d.user_agent == "TestAgent/1.0"

    def test_decompress_gz(self, tmp_path: Path) -> None:
        """decompress_gz unpacks a valid gzip file."""
        content = b"hello compressed world"
        gz_path = tmp_path / "test.gz"
        out_path = tmp_path / "test.txt"

        with gzip.open(gz_path, "wb") as f:
            f.write(content)

        downloader = DatasetDownloader("http://example.com")
        success = downloader.decompress_gz(gz_path, out_path)

        assert success is True
        assert out_path.read_bytes() == content

    def test_decompress_gz_invalid_file(self, tmp_path: Path) -> None:
        """decompress_gz returns False for invalid gzip."""
        bad_gz = tmp_path / "bad.gz"
        bad_gz.write_bytes(b"not gzip data")
        out_path = tmp_path / "out.txt"

        downloader = DatasetDownloader("http://example.com")
        success = downloader.decompress_gz(bad_gz, out_path)
        assert success is False

    def test_download_with_retry_failure(self, tmp_path: Path) -> None:
        """download_with_retry returns False when all attempts fail."""
        downloader = DatasetDownloader("http://localhost:1", max_retries=1)
        downloader.retry_delays = [0]
        output = tmp_path / "file.bin"
        success = downloader.download_with_retry("nonexistent.bin", output, max_retries=1)
        assert success is False

    @patch("hephaestus.datasets.downloader.urlopen")
    def test_download_with_retry_success(self, mock_urlopen, tmp_path: Path) -> None:
        """download_with_retry returns True on successful download."""
        content = b"file content"
        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.headers.get.return_value = str(len(content))
        mock_response.read.side_effect = [content, b""]
        mock_urlopen.return_value = mock_response

        downloader = DatasetDownloader("http://example.com")
        output = tmp_path / "file.bin"
        success = downloader.download_with_retry("test.bin", output, max_retries=1)
        assert success is True
        assert output.exists()

    @patch("hephaestus.datasets.downloader.urlopen")
    def test_download_retries_on_http_error(self, mock_urlopen, tmp_path: Path) -> None:
        """download_with_retry retries on HTTPError."""
        mock_urlopen.side_effect = HTTPError(
            url="http://example.com/file",
            code=503,
            msg="Service Unavailable",
            hdrs=HTTPMessage(),
            fp=None,
        )
        downloader = DatasetDownloader("http://example.com", max_retries=2)
        downloader.retry_delays = [0, 0]
        output = tmp_path / "file.bin"
        success = downloader.download_with_retry("test.bin", output, max_retries=2)
        assert success is False
        assert mock_urlopen.call_count == 2

    @patch("hephaestus.datasets.downloader.urlopen")
    def test_download_retries_on_url_error(self, mock_urlopen, tmp_path: Path) -> None:
        """download_with_retry retries on URLError."""
        mock_urlopen.side_effect = URLError(reason="connection refused")
        downloader = DatasetDownloader("http://example.com", max_retries=2)
        downloader.retry_delays = [0, 0]
        output = tmp_path / "file.bin"
        success = downloader.download_with_retry("test.bin", output, max_retries=2)
        assert success is False

    @patch("hephaestus.datasets.downloader.urlopen")
    def test_download_shows_progress_no_content_length(self, mock_urlopen, tmp_path: Path) -> None:
        """Handles missing Content-Length header gracefully."""
        content = b"data"
        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.headers.get.return_value = "0"  # No content length
        mock_response.read.side_effect = [content, b""]
        mock_urlopen.return_value = mock_response

        downloader = DatasetDownloader("http://example.com")
        output = tmp_path / "file.bin"
        success = downloader.download_with_retry("test.bin", output, max_retries=1)
        assert success is True

    @patch("hephaestus.datasets.downloader.urlopen")
    def test_download_retries_on_oserror(self, mock_urlopen, tmp_path: Path) -> None:
        """download_with_retry retries on OSError."""
        mock_urlopen.side_effect = OSError("disk write failed")
        downloader = DatasetDownloader("http://example.com", max_retries=2)
        downloader.retry_delays = [0, 0]
        output = tmp_path / "file.bin"
        success = downloader.download_with_retry("test.bin", output, max_retries=2)
        assert success is False
        assert mock_urlopen.call_count == 2

    @patch("hephaestus.datasets.downloader.urlopen")
    def test_download_retry_delay_clamped_to_last(self, mock_urlopen, tmp_path: Path) -> None:
        """When attempt index exceeds retry_delays length, last delay is used."""
        mock_urlopen.side_effect = URLError(reason="refused")
        downloader = DatasetDownloader("http://example.com", max_retries=4)
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
            with pytest.raises(SystemExit) as exc_info:
                from hephaestus.datasets.downloader import main

                main()
        assert exc_info.value.code == 0

    @patch("hephaestus.datasets.downloader.MNISTDownloader")
    def test_main_mnist_failure(self, mock_cls, tmp_path: Path) -> None:
        """main() exits 1 on failed MNIST download."""
        mock_instance = MagicMock()
        mock_instance.download_mnist.return_value = False
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["prog", "mnist"]):
            with pytest.raises(SystemExit) as exc_info:
                from hephaestus.datasets.downloader import main

                main()
        assert exc_info.value.code == 1

    @patch("hephaestus.datasets.downloader.MNISTDownloader")
    def test_main_mnist_default_output_dir(self, mock_cls) -> None:
        """main() uses default output dir when none is provided."""
        mock_instance = MagicMock()
        mock_instance.download_mnist.return_value = True
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["prog", "mnist"]):
            with pytest.raises(SystemExit):
                from hephaestus.datasets.downloader import main

                main()

        mock_instance.download_mnist.assert_called_once_with("datasets/mnist")
