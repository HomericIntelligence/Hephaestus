#!/usr/bin/env python3
"""
Validation script for CI/CD setup.
Checks that all components are properly configured before pushing to GitHub.
"""

import sys
from pathlib import Path
import subprocess


class Colors:
    """ANSI color codes for terminal output."""
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RESET = '\033[0m'


def check_mark(passed: bool) -> str:
    """Return colored checkmark or X."""
    if passed:
        return f"{Colors.GREEN}✓{Colors.RESET}"
    return f"{Colors.RED}✗{Colors.RESET}"


def print_header(text: str):
    """Print section header."""
    print(f"\n{Colors.BLUE}{'=' * 70}{Colors.RESET}")
    print(f"{Colors.BLUE}{text}{Colors.RESET}")
    print(f"{Colors.BLUE}{'=' * 70}{Colors.RESET}\n")


def check_files_exist():
    """Check that required files exist."""
    print_header("Checking Required Files")

    required_files = [
        ".github/workflows/ci.yml",
        "pytest.ini",
        "requirements.txt",
        "requirements-dev.txt",
        "setup.py",
        "CI_CD_SETUP.md",
        "CICD_IMPLEMENTATION_SUMMARY.md",
        "TEST_QUICK_START.md",
    ]

    repo_root = Path(__file__).parent
    all_exist = True

    for file_path in required_files:
        full_path = repo_root / file_path
        exists = full_path.exists()
        all_exist = all_exist and exists
        print(f"  {check_mark(exists)} {file_path}")

    return all_exist


def check_files_deleted():
    """Check that obsolete files are deleted."""
    print_header("Checking Obsolete Files Deleted")

    should_not_exist = [
        "shared",
        "tools",
        "hephaestus/shared",
        "scripts/deployment",
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

    repo_root = Path(__file__).parent
    all_deleted = True

    for file_path in should_not_exist:
        full_path = repo_root / file_path
        deleted = not full_path.exists()
        all_deleted = all_deleted and deleted
        status = "deleted" if deleted else f"{Colors.YELLOW}still exists{Colors.RESET}"
        print(f"  {check_mark(deleted)} {file_path} - {status}")

    return all_deleted


def check_package_import():
    """Check that hephaestus can be imported."""
    print_header("Checking Package Import")

    try:
        import hephaestus
        version = hephaestus.__version__
        print(f"  {check_mark(True)} hephaestus imports successfully")
        print(f"  {check_mark(True)} Version: {version}")
        return True
    except ImportError as e:
        print(f"  {check_mark(False)} Failed to import hephaestus: {e}")
        return False


def check_dependencies():
    """Check that key dependencies are available."""
    print_header("Checking Dependencies")

    dependencies = {
        "pytest": "pytest",
        "PyYAML": "yaml",
        "black": "black",
        "flake8": "flake8",
        "mypy": "mypy",
    }

    all_available = True

    for name, import_name in dependencies.items():
        try:
            __import__(import_name)
            print(f"  {check_mark(True)} {name}")
        except ImportError:
            print(f"  {check_mark(False)} {name} - {Colors.YELLOW}not installed{Colors.RESET}")
            all_available = False

    return all_available


def check_test_files():
    """Check that test files exist and are valid Python."""
    print_header("Checking Test Files")

    repo_root = Path(__file__).parent
    test_dir = repo_root / "tests"

    if not test_dir.exists():
        print(f"  {check_mark(False)} tests/ directory not found")
        return False

    test_files = list(test_dir.glob("test_*.py"))

    if not test_files:
        print(f"  {check_mark(False)} No test files found")
        return False

    print(f"  {check_mark(True)} Found {len(test_files)} test files:")
    for test_file in sorted(test_files):
        print(f"    - {test_file.name}")

    # Check if test files are valid Python
    all_valid = True
    for test_file in test_files:
        try:
            compile(test_file.read_text(), str(test_file), 'exec')
        except SyntaxError as e:
            print(f"  {check_mark(False)} {test_file.name} has syntax error: {e}")
            all_valid = False

    if all_valid:
        print(f"\n  {check_mark(True)} All test files have valid Python syntax")

    return all_valid


def check_workflow_syntax():
    """Check GitHub Actions workflow YAML syntax."""
    print_header("Checking Workflow YAML")

    workflow_file = Path(__file__).parent / ".github/workflows/ci.yml"

    if not workflow_file.exists():
        print(f"  {check_mark(False)} Workflow file not found")
        return False

    try:
        import yaml
        content = workflow_file.read_text()
        yaml.safe_load(content)
        print(f"  {check_mark(True)} ci.yml has valid YAML syntax")
        return True
    except ImportError:
        print(f"  {Colors.YELLOW}⚠{Colors.RESET}  PyYAML not available, skipping YAML validation")
        return True
    except Exception as e:
        print(f"  {check_mark(False)} YAML syntax error: {e}")
        return False


def run_quick_test():
    """Run a quick test to verify pytest works."""
    print_header("Running Quick Test")

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode == 0:
            lines = result.stdout.strip().split('\n')
            test_count = 0
            for line in lines:
                if 'test' in line.lower():
                    test_count += 1

            print(f"  {check_mark(True)} pytest can collect tests")
            print(f"  {check_mark(True)} Found {test_count} tests")
            return True
        else:
            print(f"  {check_mark(False)} pytest collection failed:")
            print(f"    {result.stderr}")
            return False
    except FileNotFoundError:
        print(f"  {check_mark(False)} pytest not found")
        return False
    except Exception as e:
        print(f"  {check_mark(False)} Error running pytest: {e}")
        return False


def check_git_status():
    """Check git status."""
    print_header("Checking Git Status")

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if result.returncode != 0:
            print(f"  {Colors.YELLOW}⚠{Colors.RESET}  Not a git repository or git not available")
            return True

        changes = result.stdout.strip()
        if changes:
            print(f"  {Colors.YELLOW}⚠{Colors.RESET}  Uncommitted changes detected:")
            for line in changes.split('\n')[:10]:  # Show first 10
                print(f"    {line}")
            if len(changes.split('\n')) > 10:
                print(f"    ... and {len(changes.split('\n')) - 10} more")
        else:
            print(f"  {check_mark(True)} Working directory clean")

        return True
    except Exception as e:
        print(f"  {Colors.YELLOW}⚠{Colors.RESET}  Could not check git status: {e}")
        return True


def main():
    """Run all validation checks."""
    print(f"\n{Colors.BLUE}╔══════════════════════════════════════════════════════════════════╗{Colors.RESET}")
    print(f"{Colors.BLUE}║  ProjectHephaestus CI/CD Validation                              ║{Colors.RESET}")
    print(f"{Colors.BLUE}╚══════════════════════════════════════════════════════════════════╝{Colors.RESET}")

    checks = [
        ("Required Files", check_files_exist),
        ("Obsolete Files Deleted", check_files_deleted),
        ("Package Import", check_package_import),
        ("Dependencies", check_dependencies),
        ("Test Files", check_test_files),
        ("Workflow YAML", check_workflow_syntax),
        ("Quick Test", run_quick_test),
        ("Git Status", check_git_status),
    ]

    results = {}
    for name, check_func in checks:
        try:
            results[name] = check_func()
        except Exception as e:
            print(f"\n{Colors.RED}Error in {name}: {e}{Colors.RESET}")
            results[name] = False

    # Summary
    print_header("Validation Summary")

    total = len(results)
    passed = sum(1 for r in results.values() if r)

    for name, result in results.items():
        status = f"{Colors.GREEN}PASS{Colors.RESET}" if result else f"{Colors.RED}FAIL{Colors.RESET}"
        print(f"  {check_mark(result)} {name}: {status}")

    print(f"\n{Colors.BLUE}Results: {passed}/{total} checks passed{Colors.RESET}\n")

    if passed == total:
        print(f"{Colors.GREEN}✓ All validation checks passed!{Colors.RESET}")
        print(f"\n{Colors.GREEN}Ready to push to GitHub!{Colors.RESET}")
        print(f"\nNext steps:")
        print(f"  1. Review changes: git status")
        print(f"  2. Commit changes: git add . && git commit -m 'feat: Add CI/CD pipeline'")
        print(f"  3. Push to GitHub: git push origin main")
        print(f"  4. Check Actions tab on GitHub")
        return 0
    else:
        print(f"{Colors.RED}✗ Some validation checks failed{Colors.RESET}")
        print(f"\n{Colors.YELLOW}Please fix the issues above before pushing to GitHub.{Colors.RESET}")
        print(f"\nFor help, see:")
        print(f"  - CI_CD_SETUP.md")
        print(f"  - TEST_QUICK_START.md")
        return 1


if __name__ == "__main__":
    sys.exit(main())
