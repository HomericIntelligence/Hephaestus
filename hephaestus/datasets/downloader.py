#!/usr/bin/env python3
"""Dataset downloading utilities for ProjectHephaestus.

Provides functionality to download and manage common machine learning datasets
with proper error handling, progress tracking, and decompression support.

Supported datasets:

- MNIST — 60 k training / 10 k test 28×28 grayscale digits
- Fashion-MNIST — 60 k training / 10 k test 28×28 grayscale clothing items
- CIFAR-10 — 50 k training / 10 k test 32×32 RGB images, 10 classes
  (requires ``numpy``; install with ``pip install numpy``)
- CIFAR-100 — 50 k training / 10 k test 32×32 RGB images, 100 classes
- EMNIST — Extended MNIST with multiple splits (balanced, byclass, etc.)

Usage::

    hephaestus-download-dataset mnist
    hephaestus-download-dataset fashion_mnist
    hephaestus-download-dataset cifar10
    hephaestus-download-dataset cifar100
    hephaestus-download-dataset emnist
    hephaestus-download-dataset all
"""

from __future__ import annotations

import gzip
import pickle
import struct
import sys
import tarfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from hephaestus.logging.utils import get_logger

logger = get_logger(__name__)


class DatasetDownloader:
    """Generic dataset downloader with retry logic and progress tracking."""

    def __init__(
        self,
        base_url: str,
        user_agent: str = "Mozilla/5.0 (compatible; ProjectHephaestus/1.0)",
        max_retries: int = 3,
        retry_delays: list[float] | None = None,
    ):
        """Initialize the dataset downloader.

        Args:
            base_url: Base URL for dataset files
            user_agent: User-Agent header for HTTP requests
            max_retries: Maximum number of retry attempts
            retry_delays: Delay times between retries (exponential backoff)

        """
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.max_retries = max_retries
        self.retry_delays = retry_delays or [1.0, 2.0, 4.0]

    def download_with_retry(
        self, filename: str, output_path: Path, max_retries: int | None = None
    ) -> bool:
        """Download file with User-Agent header and retry logic.

        Args:
            filename: Name of file to download
            output_path: Path to save downloaded file
            max_retries: Override default max retries

        Returns:
            True if successful, False otherwise

        """
        url = f"{self.base_url}/{filename}"
        max_retries = max_retries if max_retries is not None else self.max_retries
        last_error = None

        for attempt in range(max_retries):
            if attempt > 0:
                delay = self.retry_delays[min(attempt - 1, len(self.retry_delays) - 1)]
                logger.info("Retry %d/%d after %ss delay...", attempt, max_retries - 1, delay)
                time.sleep(delay)

            try:
                request = Request(url, headers={"User-Agent": self.user_agent})
                with urlopen(request) as response:
                    total_size = int(response.headers.get("Content-Length", 0))
                    downloaded = 0
                    block_size = 8192

                    with open(output_path, "wb") as f:
                        while True:
                            block = response.read(block_size)
                            if not block:
                                break
                            f.write(block)
                            downloaded += len(block)

                            # Progress bar: intentional stdout output for interactive
                            # terminal feedback; not library logging
                            if total_size > 0:
                                percent = min(100, downloaded * 100 / total_size)
                                bar_length = 50
                                filled = int(bar_length * downloaded / total_size)
                                bar = "=" * filled + "-" * (bar_length - filled)
                                print(
                                    f"\rDownloading {filename}: [{bar}] {percent:.1f}%",
                                    end="",
                                    flush=True,
                                )

                print()  # terminates the progress bar line
                return True

            except HTTPError as e:
                last_error = f"HTTP {e.code}: {e.reason}"
                logger.warning("Download failed: %s", last_error)
            except URLError as e:
                last_error = f"URL Error: {e.reason}"
                logger.warning("Download failed: %s", last_error)
            except OSError as e:
                last_error = str(e)
                logger.warning("Download failed: %s", last_error)

        logger.error(
            "Failed to download %s after %d attempts. Last error: %s",
            filename,
            max_retries,
            last_error,
        )
        return False

    def decompress_gz(self, gz_path: Path, output_path: Path) -> bool:
        """Decompress gzip file.

        Args:
            gz_path: Path to .gz file
            output_path: Path to save decompressed file

        Returns:
            True if successful, False otherwise

        """
        try:
            with gzip.open(gz_path, "rb") as f_in:
                with open(output_path, "wb") as f_out:
                    f_out.write(f_in.read())
            return True
        except (OSError, EOFError) as e:
            logger.error("Failed to decompress %s: %s", gz_path, e)
            return False


class MNISTDownloader(DatasetDownloader):
    """Specialized downloader for MNIST dataset."""

    def __init__(self) -> None:
        """Initialize with the MNIST dataset URL."""
        super().__init__("https://yann.lecun.com/exdb/mnist")
        self.files = [
            ("train-images-idx3-ubyte.gz", "train_images.idx"),
            ("train-labels-idx1-ubyte.gz", "train_labels.idx"),
            ("t10k-images-idx3-ubyte.gz", "test_images.idx"),
            ("t10k-labels-idx1-ubyte.gz", "test_labels.idx"),
        ]

    def download_mnist(self, output_dir: str = "datasets/mnist") -> bool:
        """Download and extract MNIST dataset.

        Args:
            output_dir: Directory to save dataset

        Returns:
            True if successful, False otherwise

        """
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        success = True

        for gz_filename, output_filename in self.files:
            gz_path = output_path / gz_filename
            output_file_path = output_path / output_filename

            # Download if not exists
            if not output_file_path.exists():
                logger.info("Downloading MNIST %s data...", output_filename.split("_")[0])

                if self.download_with_retry(gz_filename, gz_path):
                    # Decompress
                    logger.info("Decompressing %s...", gz_filename)
                    if self.decompress_gz(gz_path, output_file_path):
                        # Clean up gzip file
                        gz_path.unlink()
                        logger.info("%s ready", output_filename)
                    else:
                        success = False
                else:
                    success = False
            else:
                logger.info("%s already exists", output_filename)

        if success:
            logger.info("MNIST dataset ready at: %s", output_path)
        else:
            logger.error("Some MNIST files failed to download.")

        return success


class FashionMNISTDownloader(DatasetDownloader):
    """Specialized downloader for Fashion-MNIST dataset."""

    def __init__(self) -> None:
        """Initialize with the Fashion-MNIST dataset URL."""
        super().__init__("http://fashion-mnist.s3-website.eu-central-1.amazonaws.com")
        self.files = [
            ("train-images-idx3-ubyte.gz", "train_images.idx"),
            ("train-labels-idx1-ubyte.gz", "train_labels.idx"),
            ("t10k-images-idx3-ubyte.gz", "test_images.idx"),
            ("t10k-labels-idx1-ubyte.gz", "test_labels.idx"),
        ]

    def download_fashion_mnist(self, output_dir: str = "datasets/fashion_mnist") -> bool:
        """Download and extract Fashion-MNIST dataset.

        Args:
            output_dir: Directory to save dataset.

        Returns:
            True if successful, False otherwise.

        """
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        success = True
        for gz_filename, output_filename in self.files:
            gz_path = output_path / gz_filename
            output_file_path = output_path / output_filename
            if not output_file_path.exists():
                logger.info("Downloading Fashion-MNIST %s...", output_filename)
                if self.download_with_retry(gz_filename, gz_path):
                    if self.decompress_gz(gz_path, output_file_path):
                        gz_path.unlink()
                    else:
                        success = False
                else:
                    success = False
            else:
                logger.info("%s already exists", output_filename)
        if success:
            logger.info("Fashion-MNIST dataset ready at: %s", output_path)
        else:
            logger.error("Some Fashion-MNIST files failed to download.")
        return success


class CIFAR10Downloader(DatasetDownloader):
    """Specialized downloader for CIFAR-10 dataset.

    Requires ``numpy`` for IDX-format conversion:
    ``pip install numpy``.
    """

    _CIFAR10_URL = "https://www.cs.toronto.edu/~kriz/cifar-10-python.tar.gz"

    def __init__(self) -> None:
        """Initialize with CIFAR-10 dataset URL."""
        super().__init__("https://www.cs.toronto.edu/~kriz")

    def download_cifar10(self, output_dir: str = "datasets/cifar10") -> bool:
        """Download, extract, and convert CIFAR-10 to IDX format.

        Requires ``numpy``.

        Args:
            output_dir: Directory to save dataset.

        Returns:
            True if successful, False otherwise.

        Raises:
            ImportError: If ``numpy`` is not installed.

        """
        try:
            import numpy as np
        except ImportError as exc:
            raise ImportError(
                "numpy is required for CIFAR-10 IDX conversion. "
                "Install with: pip install numpy"
            ) from exc

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        tarball_name = "cifar-10-python.tar.gz"
        tar_path = output_path / tarball_name

        logger.info("Downloading CIFAR-10 tarball...")
        if not self.download_with_retry(tarball_name, tar_path):
            return False

        logger.info("Extracting CIFAR-10 tarball...")
        batch_dir = output_path / "cifar-10-batches-py"
        try:
            with tarfile.open(tar_path) as tf:
                tf.extractall(output_path)
        except (tarfile.TarError, OSError) as exc:
            logger.error("Failed to extract CIFAR-10 tarball: %s", exc)
            return False

        tar_path.unlink(missing_ok=True)

        logger.info("Converting CIFAR-10 batches to IDX format...")
        success = self._convert_batches(batch_dir, output_path, np)
        if success:
            logger.info("CIFAR-10 dataset ready at: %s", output_path)
        return success

    def _convert_batches(
        self, batch_dir: Path, output_dir: Path, np: Any
    ) -> bool:
        """Convert CIFAR-10 pickle batches to IDX files.

        Args:
            batch_dir: Directory containing pickle batch files.
            output_dir: Where to write IDX files.
            np: numpy module (passed in to avoid repeated import).

        Returns:
            True if all conversions succeeded.

        """
        import numpy

        train_images: list[Any] = []
        train_labels: list[Any] = []

        for i in range(1, 6):
            batch_path = batch_dir / f"data_batch_{i}"
            try:
                with open(batch_path, "rb") as f:
                    batch = pickle.load(f, encoding="bytes")
            except (OSError, pickle.UnpicklingError) as exc:
                logger.error("Failed to load batch %d: %s", i, exc)
                return False
            train_images.append(batch[b"data"])
            train_labels.extend(batch[b"labels"])

        try:
            test_path = batch_dir / "test_batch"
            with open(test_path, "rb") as f:
                test_batch = pickle.load(f, encoding="bytes")
        except (OSError, pickle.UnpicklingError) as exc:
            logger.error("Failed to load test batch: %s", exc)
            return False

        try:
            images_np = numpy.vstack(train_images)
            labels_np = numpy.array(train_labels, dtype=numpy.uint8)
            self._write_idx_images(
                images_np.reshape(-1, 3, 32, 32), output_dir / "train_images.idx"
            )
            self._write_idx_labels(labels_np, output_dir / "train_labels.idx")

            test_images = numpy.array(test_batch[b"data"])
            test_labels = numpy.array(test_batch[b"labels"], dtype=numpy.uint8)
            self._write_idx_images(
                test_images.reshape(-1, 3, 32, 32), output_dir / "test_images.idx"
            )
            self._write_idx_labels(test_labels, output_dir / "test_labels.idx")
        except (ValueError, OSError) as exc:
            logger.error("Failed to convert batches to IDX: %s", exc)
            return False

        return True

    @staticmethod
    def _write_idx_labels(labels: Any, path: Path) -> None:
        """Write a 1-D uint8 array to IDX label format (magic 0x801)."""
        import numpy

        arr: Any = numpy.asarray(labels, dtype=numpy.uint8)
        with open(path, "wb") as f:
            f.write(struct.pack(">II", 0x00000801, len(arr)))
            f.write(arr.tobytes())

    @staticmethod
    def _write_idx_images(images: Any, path: Path) -> None:
        """Write a 4-D uint8 array to IDX image format (magic 0x803)."""
        import numpy

        arr: Any = numpy.asarray(images, dtype=numpy.uint8)
        n, c, h, w = arr.shape
        with open(path, "wb") as f:
            f.write(struct.pack(">IIIII", 0x00000803, n, c, h, w))
            f.write(arr.tobytes())


class CIFAR100Downloader(DatasetDownloader):
    """Specialized downloader for CIFAR-100 dataset."""

    def __init__(self) -> None:
        """Initialize with CIFAR-100 dataset URL."""
        super().__init__("https://www.cs.toronto.edu/~kriz")

    def download_cifar100(self, output_dir: str = "datasets/cifar100") -> bool:
        """Download and extract CIFAR-100 dataset.

        Args:
            output_dir: Directory to save dataset.

        Returns:
            True if successful, False otherwise.

        """
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        tarball_name = "cifar-100-python.tar.gz"
        tar_path = output_path / tarball_name

        logger.info("Downloading CIFAR-100 tarball...")
        if not self.download_with_retry(tarball_name, tar_path):
            return False

        logger.info("Extracting CIFAR-100 tarball...")
        try:
            with tarfile.open(tar_path) as tf:
                tf.extractall(output_path)
        except (tarfile.TarError, OSError) as exc:
            logger.error("Failed to extract CIFAR-100 tarball: %s", exc)
            return False

        tar_path.unlink(missing_ok=True)
        logger.info("CIFAR-100 dataset ready at: %s", output_path)
        return True


# EMNIST available splits
EMNIST_SPLITS = frozenset(
    ["balanced", "byclass", "bymerge", "digits", "letters", "mnist"]
)

# EMNIST primary URL and fallback mirrors
_EMNIST_URLS = [
    "https://biometrics.nist.gov/cs_links/EMNIST",
    "https://rds.westernsydney.edu.au/Institutes/MARCS/BENS/EMNIST",
]


class EMNISTDownloader(DatasetDownloader):
    """Specialized downloader for EMNIST dataset."""

    def __init__(self) -> None:
        """Initialize with primary EMNIST URL."""
        super().__init__(_EMNIST_URLS[0])
        self._fallback_urls = _EMNIST_URLS[1:]

    def download_emnist(
        self, split: str = "balanced", output_dir: str = "datasets/emnist"
    ) -> bool:
        """Download and extract EMNIST dataset for a specific split.

        Args:
            split: EMNIST split to download. One of: balanced, byclass,
                bymerge, digits, letters, mnist.
            output_dir: Directory to save dataset.

        Returns:
            True if successful, False otherwise.

        Raises:
            ValueError: If *split* is not a valid EMNIST split name.

        """
        if split not in EMNIST_SPLITS:
            raise ValueError(
                f"Unknown EMNIST split: {split!r}. "
                f"Valid splits: {sorted(EMNIST_SPLITS)}"
            )

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        # EMNIST is distributed as a single zip with all splits
        zip_name = "gzip.zip"
        zip_path = output_path / zip_name

        if not zip_path.exists():
            logger.info("Downloading EMNIST %s split...", split)
            downloaded = False
            for base_url in [self.base_url, *self._fallback_urls]:
                self.base_url = base_url
                if self.download_with_retry(zip_name, zip_path):
                    downloaded = True
                    break
            if not downloaded:
                logger.error("Failed to download EMNIST from all mirrors.")
                return False

        logger.info("Extracting EMNIST %s split...", split)
        try:
            import zipfile

            with zipfile.ZipFile(zip_path) as zf:
                # Extract only the files for the requested split
                targets = [n for n in zf.namelist() if split in n]
                for name in targets:
                    zf.extract(name, output_path)
        except (OSError, KeyError) as exc:
            logger.error("Failed to extract EMNIST zip: %s", exc)
            return False

        logger.info("EMNIST %s dataset ready at: %s", split, output_path)
        return True


def main() -> None:
    """Serve as the main entry point for dataset downloading."""
    import argparse

    dataset_choices = ["mnist", "fashion_mnist", "cifar10", "cifar100", "emnist", "all"]

    parser = argparse.ArgumentParser(
        description="Download machine learning datasets",
        epilog=(
            "Examples:\n"
            "  %(prog)s mnist\n"
            "  %(prog)s cifar10 datasets/cifar10\n"
            "  %(prog)s emnist --split balanced\n"
            "  %(prog)s all\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("dataset", choices=dataset_choices, help="Dataset to download")
    parser.add_argument(
        "output_dir",
        nargs="?",
        default=None,
        help="Output directory (default: datasets/<dataset>)",
    )
    parser.add_argument(
        "--split",
        default="balanced",
        choices=sorted(EMNIST_SPLITS),
        help="EMNIST split (only used when dataset=emnist, default: balanced)",
    )

    args = parser.parse_args()
    success = True

    datasets_to_run: list[str] = (
        ["mnist", "fashion_mnist", "cifar10", "cifar100", "emnist"]
        if args.dataset == "all"
        else [args.dataset]
    )

    for name in datasets_to_run:
        out = args.output_dir or f"datasets/{name}"
        if name == "mnist":
            success &= MNISTDownloader().download_mnist(out)
        elif name == "fashion_mnist":
            success &= FashionMNISTDownloader().download_fashion_mnist(out)
        elif name == "cifar10":
            success &= CIFAR10Downloader().download_cifar10(out)
        elif name == "cifar100":
            success &= CIFAR100Downloader().download_cifar100(out)
        elif name == "emnist":
            success &= EMNISTDownloader().download_emnist(args.split, out)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
