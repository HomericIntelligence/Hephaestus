#!/usr/bin/env python3
"""Dataset downloading utilities for ProjectHephaestus.

Provides functionality to download and manage common machine learning datasets
with proper error handling, progress tracking, and decompression support.
"""

import gzip
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class DatasetDownloader:
    """Generic dataset downloader with retry logic and progress tracking."""

    def __init__(
        self,
        base_url: str,
        user_agent: str = "Mozilla/5.0 (compatible; ProjectHephaestus/1.0)",
        max_retries: int = 3,
        retry_delays: list[float] | None = None
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
        self,
        filename: str,
        output_path: Path,
        max_retries: int | None = None
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
                print(f"  Retry {attempt}/{max_retries - 1} after {delay}s delay...")
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

                            # Progress bar
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

                print()  # New line after progress bar
                return True

            except HTTPError as e:
                last_error = f"HTTP {e.code}: {e.reason}"
                print(f"\n  Download failed: {last_error}")
            except URLError as e:
                last_error = f"URL Error: {e.reason}"
                print(f"\n  Download failed: {last_error}")
            except Exception as e:
                last_error = str(e)
                print(f"\n  Download failed: {last_error}")

        print(f"Failed to download {filename} after {max_retries} attempts. Last error: {last_error}")
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
        except Exception as e:
            print(f"Failed to decompress {gz_path}: {e}")
            return False


class MNISTDownloader(DatasetDownloader):
    """Specialized downloader for MNIST dataset."""

    def __init__(self):
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
                print(f"Downloading MNIST {output_filename.split('_')[0]} data...")

                if self.download_with_retry(gz_filename, gz_path):
                    # Decompress
                    print(f"Decompressing {gz_filename}...")
                    if self.decompress_gz(gz_path, output_file_path):
                        # Clean up gzip file
                        gz_path.unlink()
                        print(f"✓ {output_filename} ready")
                    else:
                        success = False
                else:
                    success = False
            else:
                print(f"✓ {output_filename} already exists")

        if success:
            print(f"\n✓ MNIST dataset ready at: {output_path}")
        else:
            print("\n✗ Some MNIST files failed to download.")

        return success


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Download machine learning datasets")
    parser.add_argument(
        "dataset",
        choices=["mnist"],
        help="Dataset to download"
    )
    parser.add_argument(
        "output_dir",
        nargs="?",
        default=None,
        help="Output directory (default: datasets/<dataset>)"
    )

    args = parser.parse_args()

    if args.dataset == "mnist":
        output_dir = args.output_dir or "datasets/mnist"
        downloader = MNISTDownloader()
        success = downloader.download_mnist(output_dir)
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
