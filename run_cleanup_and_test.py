#!/usr/bin/env python3
"""
Run cleanup and tests for ProjectHephaestus.
This script performs the manual cleanup steps and then runs tests.
"""

import shutil
import sys
from pathlib import Path
import subprocess

def cleanup():
    """Perform manual cleanup steps."""
    print("=== ProjectHephaestus Cleanup & Test ===\n")

    repo_root = Path(__file__).parent

    # Step 1: Delete obsolete directories
    print("Step 1: Deleting obsolete directories...")
    dirs_to_delete = [
        repo_root / "shared",
        repo_root / "tools",
        repo_root / "hephaestus" / "shared",
        repo_root / "scripts" / "deployment",
    ]

    for dir_path in dirs_to_delete:
        if dir_path.exists():
            shutil.rmtree(dir_path)
            print(f"  ✓ Deleted: {dir_path.relative_to(repo_root)}")
        else:
            print(f"  - Already deleted: {dir_path.relative_to(repo_root)}")

    # Step 2: Delete ad-hoc test scripts
    print("\nStep 2: Deleting ad-hoc test scripts...")
    scripts_to_delete = [
        "verify_setup.py",
        "manual_test.py",
        "validate_implementation.py",
        "final_validation.py",
        "comprehensive_test.py",
        "end_to_end_test.py",
        "fixed_test.py",
        "validate_fixes.py",
        "verify_ported_utilities.py",
    ]

    deleted_count = 0
    for script in scripts_to_delete:
        script_path = repo_root / script
        if script_path.exists():
            script_path.unlink()
            deleted_count += 1

    print(f"  ✓ Deleted {deleted_count} ad-hoc test scripts")

    print("\n=== Cleanup Complete ===\n")

def run_tests():
    """Run pytest tests."""
    print("Running pytest...\n")

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-v"],
            cwd=Path(__file__).parent,
        )
        return result.returncode
    except Exception as e:
        print(f"Error running tests: {e}")
        return 1

def verify_installation():
    """Verify package can be imported."""
    print("Verifying installation...\n")

    try:
        import hephaestus
        print(f"✓ Hephaestus version: {hephaestus.__version__}")
        return 0
    except ImportError as e:
        print(f"✗ Failed to import hephaestus: {e}")
        return 1

if __name__ == "__main__":
    # Run cleanup
    cleanup()

    # Verify installation
    verify_code = verify_installation()
    if verify_code != 0:
        sys.exit(verify_code)

    print()

    # Run tests
    test_code = run_tests()

    sys.exit(test_code)
