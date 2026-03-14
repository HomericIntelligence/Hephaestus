#!/usr/bin/env python3
"""Tests for dataset downloading utilities."""

import gzip
from pathlib import Path

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
        downloader.retry_delays = [0]  # no sleep in tests
        output = tmp_path / "file.bin"
        # Should fail quickly with a URL/connection error
        success = downloader.download_with_retry("nonexistent.bin", output, max_retries=1)
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

        # Pre-create all output files so nothing is downloaded
        for _, output_filename in d.files:
            (output_dir / output_filename).write_bytes(b"dummy")

        success = d.download_mnist(str(output_dir))
        assert success is True
