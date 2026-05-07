#!/usr/bin/env python3
"""Simple test runner for ProjectHephaestus.

This script runs basic tests without requiring pytest installation.
"""

import sys
import tempfile
import traceback
from pathlib import Path

# Import our utilities
try:
    from hephaestus.config.utils import get_setting
    from hephaestus.io.utils import ensure_directory
    from hephaestus.utils.helpers import human_readable_size, slugify

    print("✓ All imports successful")
except Exception as e:
    print(f"✗ Import error: {e}")
    traceback.print_exc()
    sys.exit(1)


def test_slugify():
    """Test slugify function."""
    try:
        assert slugify("Hello World") == "hello-world"
        assert slugify("My Project v1.0") == "my-project-v1-0"
        print("✓ slugify tests passed")
        return True
    except Exception as e:
        print(f"✗ slugify test failed: {e}")
        return False


def test_human_readable_size():
    """Test human_readable_size function."""
    try:
        assert human_readable_size(0) == "0 B"
        assert human_readable_size(1023) == "1023.0 B"
        assert human_readable_size(1024) == "1.0 KB"
        print("✓ human_readable_size tests passed")
        return True
    except Exception as e:
        print(f"✗ human_readable_size test failed: {e}")
        return False


def test_get_setting():
    """Test get_setting function."""
    try:
        config = {"database": {"host": "localhost", "port": 5432}}
        assert get_setting(config, "database.host") == "localhost"
        assert get_setting(config, "database.port") == 5432
        assert get_setting(config, "non.existing", "default") == "default"
        print("✓ get_setting tests passed")
        return True
    except Exception as e:
        print(f"✗ get_setting test failed: {e}")
        return False


def test_ensure_directory():
    """Test ensure_directory function."""
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            test_path = Path(temp_dir) / "test" / "nested" / "directory"
            ensure_directory(test_path)
            assert test_path.exists()
        print("✓ ensure_directory tests passed")
        return True
    except Exception as e:
        print(f"✗ ensure_directory test failed: {e}")
        return False


def main():
    """Run all tests."""
    print("Running ProjectHephaestus tests...")

    tests = [test_slugify, test_human_readable_size, test_get_setting, test_ensure_directory]

    passed = 0
    failed = 0

    for test in tests:
        if test():
            passed += 1
        else:
            failed += 1

    print(f"\nTest Results: {passed} passed, {failed} failed")

    if failed == 0:
        print("🎉 All tests passed!")
        return 0
    else:
        print("❌ Some tests failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
