"""Tests for hephaestus.validation.test_structure."""

from pathlib import Path

from hephaestus.validation.test_structure import (
    SANCTIONED_EXTRA_TEST_DIRS,
    _detect_src_package,
    _get_subpackages,
    check_no_ghost_packages,
    check_no_loose_test_files,
    check_no_stray_tests_root_files,
    check_no_unsanctioned_test_dirs,
    check_scripts_coverage,
    check_test_directory_mirrors,
    check_test_structure,
    main,
)


def _make_package(root: Path, name: str) -> Path:
    """Create a minimal Python package directory."""
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").touch()
    return pkg


def _make_test_dir(root: Path, name: str) -> Path:
    """Create a test subpackage directory with a placeholder ``test_*.py``.

    Real ``tests/unit/`` subpackages always contain at least one test module,
    so fixtures must too: a bare directory with no Python source is now treated
    as a ghost by ``_get_subpackages`` and would not be enumerated.
    """
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / f"test_{name}.py").touch()
    return pkg


class TestCheckTestDirectoryMirrors:
    """Tests for check_test_directory_mirrors()."""

    def test_all_mirrored(self, tmp_path: Path) -> None:
        """Returns True when all source subpackages have test dirs."""
        src = tmp_path / "mypackage"
        tests = tmp_path / "tests" / "unit"
        for name in ["utils", "config", "io"]:
            _make_package(src, name)
            _make_test_dir(tests, name)
        mirrored, missing = check_test_directory_mirrors(src, tests)
        assert mirrored is True
        assert missing == set()

    def test_missing_test_dir(self, tmp_path: Path) -> None:
        """Returns False with missing dirs listed."""
        src = tmp_path / "mypackage"
        tests = tmp_path / "tests" / "unit"
        _make_package(src, "utils")
        _make_package(src, "config")
        _make_test_dir(tests, "utils")
        mirrored, missing = check_test_directory_mirrors(src, tests)
        assert mirrored is False
        assert missing == {"config"}

    def test_empty_source(self, tmp_path: Path) -> None:
        """Empty source package passes."""
        src = tmp_path / "mypackage"
        src.mkdir()
        tests = tmp_path / "tests" / "unit"
        tests.mkdir(parents=True)
        mirrored, _missing = check_test_directory_mirrors(src, tests)
        assert mirrored is True

    def test_ignores_hidden_dirs(self, tmp_path: Path) -> None:
        """Directories starting with . or _ are ignored."""
        src = tmp_path / "mypackage"
        tests = tmp_path / "tests" / "unit"
        _make_package(src, "utils")
        (src / "__pycache__").mkdir()
        (src / ".hidden").mkdir()
        _make_test_dir(tests, "utils")
        mirrored, _missing = check_test_directory_mirrors(src, tests)
        assert mirrored is True


class TestCheckNoLooseTestFiles:
    """Tests for check_no_loose_test_files()."""

    def test_clean_structure(self, tmp_path: Path) -> None:
        """No loose test files returns True."""
        unit_root = tmp_path / "tests" / "unit"
        unit_root.mkdir(parents=True)
        (unit_root / "__init__.py").touch()
        (unit_root / "conftest.py").touch()
        sub = unit_root / "utils"
        sub.mkdir()
        (sub / "test_helpers.py").touch()
        no_loose, violations = check_no_loose_test_files(unit_root)
        assert no_loose is True
        assert violations == []

    def test_loose_test_file_detected(self, tmp_path: Path) -> None:
        """Loose test_*.py at root is flagged."""
        unit_root = tmp_path / "tests" / "unit"
        unit_root.mkdir(parents=True)
        (unit_root / "test_bad.py").touch()
        no_loose, violations = check_no_loose_test_files(unit_root)
        assert no_loose is False
        assert len(violations) == 1
        assert violations[0].name == "test_bad.py"

    def test_allowed_files_not_flagged(self, tmp_path: Path) -> None:
        """__init__.py and conftest.py are allowed at root."""
        unit_root = tmp_path / "tests" / "unit"
        unit_root.mkdir(parents=True)
        (unit_root / "__init__.py").touch()
        (unit_root / "conftest.py").touch()
        no_loose, _violations = check_no_loose_test_files(unit_root)
        assert no_loose is True

    def test_missing_directory(self, tmp_path: Path) -> None:
        """Missing directory returns True (no violations)."""
        no_loose, _violations = check_no_loose_test_files(tmp_path / "nonexistent")
        assert no_loose is True

    def test_multiple_loose_files(self, tmp_path: Path) -> None:
        """Multiple loose files all detected."""
        unit_root = tmp_path / "tests" / "unit"
        unit_root.mkdir(parents=True)
        (unit_root / "test_a.py").touch()
        (unit_root / "test_b.py").touch()
        no_loose, violations = check_no_loose_test_files(unit_root)
        assert no_loose is False
        assert len(violations) == 2


class TestCheckNoStrayTestsRootFiles:
    """Tests for check_no_stray_tests_root_files (issue #1467)."""

    def test_clean_tests_root_passes(self, tmp_path: Path) -> None:
        """A tests/ root holding only allowed files passes."""
        (tmp_path / "__init__.py").write_text("", encoding="utf-8")
        (tmp_path / "conftest.py").write_text("", encoding="utf-8")
        ok, violations = check_no_stray_tests_root_files(tmp_path)
        assert ok is True
        assert violations == []

    def test_stray_test_file_detected(self, tmp_path: Path) -> None:
        """A test_*.py directly at tests/ root is flagged (the #1467 defect)."""
        (tmp_path / "test_show_prompt.py").write_text("def test_x(): pass\n", encoding="utf-8")
        ok, violations = check_no_stray_tests_root_files(tmp_path)
        assert ok is False
        assert [p.name for p in violations] == ["test_show_prompt.py"]

    def test_allowed_files_not_flagged(self, tmp_path: Path) -> None:
        """__init__.py and conftest.py at tests/ root are allowed."""
        (tmp_path / "conftest.py").write_text("", encoding="utf-8")
        ok, violations = check_no_stray_tests_root_files(tmp_path)
        assert ok is True
        assert violations == []

    def test_multiple_stray_files_all_detected(self, tmp_path: Path) -> None:
        """Multiple stray files are all reported, sorted."""
        (tmp_path / "test_a.py").write_text("", encoding="utf-8")
        (tmp_path / "test_b.py").write_text("", encoding="utf-8")
        ok, violations = check_no_stray_tests_root_files(tmp_path)
        assert ok is False
        assert [p.name for p in violations] == ["test_a.py", "test_b.py"]

    def test_missing_directory(self, tmp_path: Path) -> None:
        """A missing tests/ directory yields no violations."""
        ok, violations = check_no_stray_tests_root_files(tmp_path / "nope")
        assert ok is True
        assert violations == []

    def test_real_repo_tests_root_is_clean(self) -> None:
        """The shipped tests/ root must have no stray test_*.py (gate ships green)."""
        repo_root = Path(__file__).resolve().parents[3]
        ok, violations = check_no_stray_tests_root_files(repo_root / "tests")
        assert ok is True, f"stray test files at tests/ root: {violations}"


class TestCheckNoUnsanctionedTestDirs:
    """Tests for check_no_unsanctioned_test_dirs()."""

    def test_all_mirrored_passes(self, tmp_path: Path) -> None:
        src = tmp_path / "mypackage"
        tests = tmp_path / "tests" / "unit"
        for name in ["utils", "io"]:
            _make_package(src, name)
            _make_test_dir(tests, name)
        ok, unsanctioned = check_no_unsanctioned_test_dirs(src, tests, frozenset())
        assert ok is True
        assert unsanctioned == set()

    def test_unsanctioned_dir_flagged(self, tmp_path: Path) -> None:
        src = tmp_path / "mypackage"
        tests = tmp_path / "tests" / "unit"
        _make_package(src, "utils")
        _make_test_dir(tests, "utils")
        _make_test_dir(tests, "rogue")
        ok, unsanctioned = check_no_unsanctioned_test_dirs(src, tests, frozenset())
        assert ok is False
        assert unsanctioned == {"rogue"}

    def test_sanctioned_dir_allowed(self, tmp_path: Path) -> None:
        src = tmp_path / "mypackage"
        tests = tmp_path / "tests" / "unit"
        _make_package(src, "utils")
        _make_test_dir(tests, "utils")
        _make_test_dir(tests, "scripts")
        ok, unsanctioned = check_no_unsanctioned_test_dirs(src, tests, frozenset({"scripts"}))
        assert ok is True
        assert unsanctioned == set()

    def test_reuses_subpackage_filtering(self, tmp_path: Path) -> None:
        """__pycache__/dotdirs are ignored (shared _get_subpackages semantics)."""
        src = tmp_path / "mypackage"
        tests = tmp_path / "tests" / "unit"
        _make_package(src, "utils")
        _make_test_dir(tests, "utils")
        (tests / "__pycache__").mkdir()
        (tests / ".hidden").mkdir()
        ok, unsanctioned = check_no_unsanctioned_test_dirs(src, tests, frozenset())
        assert ok is True
        assert unsanctioned == set()

    def test_real_repo_extras_are_sanctioned(self) -> None:
        """The real tests/unit/ extras are all in the shipped allowlist."""
        assert {"constants", "docs", "plugins", "scripts", "shell"} <= SANCTIONED_EXTRA_TEST_DIRS


class TestCheckNoGhostPackages:
    """Tests for check_no_ghost_packages()."""

    def test_populated_pair_not_ghost(self, tmp_path: Path) -> None:
        """A source pkg with a module + tests with test_*.py is not a ghost."""
        src = tmp_path / "mypackage"
        tests = tmp_path / "tests" / "unit"
        _make_package(src, "utils")
        (src / "utils" / "helpers.py").touch()
        (tests / "utils").mkdir(parents=True)
        (tests / "utils" / "test_helpers.py").touch()
        ok, ghosts = check_no_ghost_packages(src, tests)
        assert ok is True
        assert ghosts == set()

    def test_content_free_pair_flagged(self, tmp_path: Path) -> None:
        """Both dirs name-mirror but neither has content -> ghost.

        The test dir holds only ``__init__.py`` (so ``_get_subpackages``
        enumerates it as a subpackage) and no ``test_*.py``, mirroring a
        source pkg that holds only ``__init__.py`` — the exact ghost case.
        """
        src = tmp_path / "mypackage"
        tests = tmp_path / "tests" / "unit"
        _make_package(src, "git")  # only __init__.py
        _make_package(tests, "git")  # only __init__.py, no test_*.py
        ok, ghosts = check_no_ghost_packages(src, tests)
        assert ok is False
        assert ghosts == {"git"}

    def test_source_has_module_not_ghost(self, tmp_path: Path) -> None:
        """A source module beyond __init__.py disqualifies the ghost verdict."""
        src = tmp_path / "mypackage"
        tests = tmp_path / "tests" / "unit"
        _make_package(src, "git")
        (src / "git" / "changelog.py").touch()
        (tests / "git").mkdir(parents=True)
        ok, ghosts = check_no_ghost_packages(src, tests)
        assert ok is True
        assert ghosts == set()

    def test_tests_present_not_ghost(self, tmp_path: Path) -> None:
        """A test_*.py file disqualifies the ghost verdict."""
        src = tmp_path / "mypackage"
        tests = tmp_path / "tests" / "unit"
        _make_package(src, "git")
        (tests / "git").mkdir(parents=True)
        (tests / "git" / "test_x.py").touch()
        ok, ghosts = check_no_ghost_packages(src, tests)
        assert ok is True
        assert ghosts == set()

    def test_real_repo_has_no_ghosts(self) -> None:
        """The shipped tree must have zero ghost mirror pairs."""
        repo_root = Path(__file__).resolve().parents[3]
        ok, ghosts = check_no_ghost_packages(repo_root / "hephaestus", repo_root / "tests" / "unit")
        assert ok is True, f"ghost dirs present: {ghosts}"


class TestGetSubpackages:
    """_get_subpackages must ignore ghost (__pycache__-only) directories."""

    def test_pycache_only_dir_not_counted(self, tmp_path: Path) -> None:
        # Acceptance: ghost hephaestus/git/ (only __pycache__) is NOT a subpackage.
        (tmp_path / "real").mkdir()
        (tmp_path / "real" / "__init__.py").write_text("", encoding="utf-8")
        ghost = tmp_path / "git"
        (ghost / "__pycache__").mkdir(parents=True)
        (ghost / "__pycache__" / "changelog.cpython-314.pyc").write_text("", encoding="utf-8")
        assert _get_subpackages(tmp_path) == {"real"}

    def test_dir_with_bare_module_counted(self, tmp_path: Path) -> None:
        # A dir with a *.py but no __init__.py still counts (namespace-style).
        pkg = tmp_path / "mod"
        pkg.mkdir()
        (pkg / "thing.py").write_text("", encoding="utf-8")
        assert _get_subpackages(tmp_path) == {"mod"}

    def test_empty_dir_not_counted(self, tmp_path: Path) -> None:
        # A directory with no Python source at all is not a subpackage.
        (tmp_path / "empty").mkdir()
        assert _get_subpackages(tmp_path) == set()


class TestGhostDirDoesNotMaskMirror:
    """A ghost source dir must not register as a satisfied/extra mirror."""

    def test_ghost_src_dir_ignored_in_mirror_checks(self, tmp_path: Path) -> None:
        # Acceptance: a __pycache__-only src dir is not reported as a mirrored
        # subpackage (would otherwise mask its deletion).
        src = tmp_path / "src"
        tests = tmp_path / "tests"
        _make_package(src, "real")
        ghost = src / "git"
        (ghost / "__pycache__").mkdir(parents=True)
        (ghost / "__pycache__" / "x.pyc").write_text("", encoding="utf-8")
        (tests / "real").mkdir(parents=True)
        (tests / "real" / "__init__.py").write_text("", encoding="utf-8")

        mirrored, missing = check_test_directory_mirrors(src, tests)
        assert mirrored and missing == set()  # ghost git/ not demanded as a mirror
        ok, unsanctioned = check_no_unsanctioned_test_dirs(src, tests, frozenset())
        assert ok and unsanctioned == set()

    def test_ghost_src_dir_not_demanded_by_check_test_structure(self, tmp_path: Path) -> None:
        # Orchestrator altitude: check_test_structure() must pass with a ghost
        # __pycache__-only src dir present (it must not be counted as a
        # subpackage requiring a mirror test directory).
        src = tmp_path / "mypackage"
        _make_package(src, "real")
        (src / "__init__.py").touch()
        ghost = src / "git"
        (ghost / "__pycache__").mkdir(parents=True)
        (ghost / "__pycache__" / "changelog.cpython-314.pyc").write_text("", encoding="utf-8")
        test_root = tmp_path / "tests" / "unit"
        _make_test_dir(test_root, "real")
        passed = check_test_structure(tmp_path, src_package="mypackage")
        assert passed is True


class TestCheckTestStructure:
    """Tests for check_test_structure()."""

    def test_passing_structure(self, tmp_path: Path) -> None:
        """Correctly structured project passes both checks."""
        # Create source package
        src = tmp_path / "mypackage"
        _make_package(src, "utils")
        _make_package(src, "config")
        (src / "__init__.py").touch()

        # Create matching test structure
        test_root = tmp_path / "tests" / "unit"
        utils_tests = test_root / "utils"
        utils_tests.mkdir(parents=True)
        (utils_tests / "test_helpers.py").touch()
        config_tests = test_root / "config"
        config_tests.mkdir()
        (config_tests / "test_config.py").touch()

        passed = check_test_structure(tmp_path, src_package="mypackage")
        assert passed is True

    def test_missing_src_root(self, tmp_path: Path) -> None:
        """Missing source root returns False."""
        (tmp_path / "tests" / "unit").mkdir(parents=True)
        passed = check_test_structure(tmp_path, src_package="nonexistent")
        assert passed is False

    def test_missing_test_root(self, tmp_path: Path) -> None:
        """Missing test root returns False."""
        src = tmp_path / "mypackage"
        src.mkdir()
        (src / "__init__.py").touch()
        passed = check_test_structure(tmp_path, src_package="mypackage")
        assert passed is False

    def test_verbose_output(self, tmp_path: Path) -> None:
        """Verbose mode prints details."""
        src = tmp_path / "mypackage"
        _make_package(src, "utils")
        (src / "__init__.py").touch()
        test_root = tmp_path / "tests" / "unit"
        _make_test_dir(test_root, "utils")
        passed = check_test_structure(tmp_path, src_package="mypackage", verbose=True)
        assert passed is True

    def test_auto_detect_src_package(self, tmp_path: Path) -> None:
        """Auto-detects source package from pyproject.toml."""
        (tmp_path / "pyproject.toml").write_text(
            '[tool.hatch.build.targets.wheel]\npackages = ["mypkg"]\n'
        )
        src = tmp_path / "mypkg"
        _make_package(src, "core")
        (src / "__init__.py").touch()
        test_root = tmp_path / "tests" / "unit"
        _make_test_dir(test_root, "core")
        passed = check_test_structure(tmp_path)
        assert passed is True

    def test_unsanctioned_test_dir_fails(self, tmp_path: Path, capsys) -> None:
        """check_test_structure fails and prints when an unsanctioned test dir exists.

        Exercises Check 3's failure branch (the sorted-unsanctioned print loop).
        """
        src = tmp_path / "mypackage"
        _make_package(src, "utils")
        (src / "__init__.py").touch()
        test_root = tmp_path / "tests" / "unit"
        _make_test_dir(test_root, "utils")
        _make_test_dir(test_root, "rogue")  # no source counterpart, not allowlisted
        passed = check_test_structure(tmp_path, src_package="mypackage")
        assert passed is False
        err = capsys.readouterr().err
        assert "tests/unit/rogue/" in err
        assert "SANCTIONED_EXTRA_TEST_DIRS" in err

    def test_ghost_package_fails(self, tmp_path: Path, capsys) -> None:
        """check_test_structure fails and prints when a ghost mirror pair exists.

        Exercises Check 4's failure branch (the ghost print loop). The source
        ``git`` pkg holds only ``__init__.py`` and the test ``git`` dir holds
        only ``__init__.py`` (no ``test_*.py``) — both content-free.
        """
        src = tmp_path / "mypackage"
        _make_package(src, "git")  # source pkg with only __init__.py
        (src / "__init__.py").touch()
        test_root = tmp_path / "tests" / "unit"
        _make_package(test_root, "git")  # mirror dir, only __init__.py, no test_*.py
        passed = check_test_structure(tmp_path, src_package="mypackage")
        assert passed is False
        err = capsys.readouterr().err
        assert "tests/unit/git/ (no tests)" in err
        assert "hephaestus/git/ (no modules)" in err

    def test_stray_tests_root_file_fails(self, tmp_path: Path, capsys) -> None:
        """check_test_structure fails and prints when a test_*.py sits at tests/ root.

        Exercises Check 5's failure branch (the stray-file print loop). The
        structure passes every other check; only a stray ``test_*.py`` at
        ``tests/`` root (outside testpaths) triggers the failure — the #1467
        regression.
        """
        src = tmp_path / "mypackage"
        _make_package(src, "utils")
        (src / "__init__.py").touch()
        test_root = tmp_path / "tests" / "unit"
        _make_test_dir(test_root, "utils")
        (tmp_path / "tests" / "test_orphan.py").write_text("def test_x(): pass\n", encoding="utf-8")
        passed = check_test_structure(tmp_path, src_package="mypackage")
        assert passed is False
        err = capsys.readouterr().err
        assert "test_orphan.py" in err
        assert "outside testpaths" in err


class TestDetectSrcPackage:
    """Tests for _detect_src_package()."""

    def test_from_pyproject(self, tmp_path: Path) -> None:
        """Detects package name from pyproject.toml."""
        (tmp_path / "pyproject.toml").write_text(
            '[tool.hatch.build.targets.wheel]\npackages = ["mypkg"]\n'
        )
        assert _detect_src_package(tmp_path) == "mypkg"

    def test_fallback_to_init(self, tmp_path: Path) -> None:
        """Falls back to first directory with __init__.py."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").touch()
        assert _detect_src_package(tmp_path) == "mypkg"

    def test_fallback_to_src(self, tmp_path: Path) -> None:
        """Returns 'src' when nothing found."""
        assert _detect_src_package(tmp_path) == "src"


class TestMain:
    """Tests for main() CLI entry point."""

    def test_passing_returns_zero(self, tmp_path: Path, monkeypatch) -> None:
        """Clean structure exits 0."""
        src = tmp_path / "mypkg"
        _make_package(src, "utils")
        (src / "__init__.py").touch()
        test_root = tmp_path / "tests" / "unit"
        _make_test_dir(test_root, "utils")
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-test-structure",
                "--repo-root",
                str(tmp_path),
                "--src-package",
                "mypkg",
            ],
        )
        assert main() == 0

    def test_failing_returns_one(self, tmp_path: Path, monkeypatch) -> None:
        """Missing test dirs exits 1."""
        src = tmp_path / "mypkg"
        _make_package(src, "utils")
        (src / "__init__.py").touch()
        (tmp_path / "tests" / "unit").mkdir(parents=True)
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-test-structure",
                "--repo-root",
                str(tmp_path),
                "--src-package",
                "mypkg",
            ],
        )
        assert main() == 1


class TestScriptsCoverageWiredIntoEntryPoint:
    """check_test_structure runs the scripts-coverage check."""

    def test_failing_scripts_coverage_fails_overall(self, tmp_path: Path, capsys) -> None:
        src = tmp_path / "mypkg"
        _make_package(src, "utils")
        (src / "__init__.py").touch()
        test_root = tmp_path / "tests" / "unit"
        _make_test_dir(test_root, "utils")
        scripts_root = tmp_path / "scripts"
        scripts_root.mkdir()
        (scripts_root / "foo.py").write_text("# script\n", encoding="utf-8")

        passed = check_test_structure(tmp_path, src_package="mypkg")

        assert passed is False
        assert "scripts/ smoke harness is required" in capsys.readouterr().err

    def test_absent_scripts_dir_is_skipped(self, tmp_path: Path) -> None:
        src = tmp_path / "mypkg"
        _make_package(src, "utils")
        (src / "__init__.py").touch()
        test_root = tmp_path / "tests" / "unit"
        _make_test_dir(test_root, "utils")

        assert check_test_structure(tmp_path, src_package="mypkg") is True


class TestCheckScriptsCoverage:
    """Tests for check_scripts_coverage()."""

    def _make_harness(self, tmp_path: Path, glob_marker: str = 'glob("*.py")') -> tuple[Path, Path]:
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "foo.py").write_text("print('hi')\n", encoding="utf-8")
        tests = tmp_path / "tests" / "unit"
        smoke = tests / "scripts"
        smoke.mkdir(parents=True)
        (smoke / "conftest.py").write_text(f"p.{glob_marker}\n", encoding="utf-8")
        (smoke / "test_scripts_smoke.py").write_text("def test_x(): pass\n", encoding="utf-8")
        return scripts, tests

    def test_healthy_harness_passes(self, tmp_path: Path) -> None:
        scripts, tests = self._make_harness(tmp_path)
        ok, errors = check_scripts_coverage(scripts, tests)
        assert ok
        assert errors == []

    def test_missing_conftest_flagged(self, tmp_path: Path) -> None:
        scripts, tests = self._make_harness(tmp_path)
        (tests / "scripts" / "conftest.py").unlink()
        ok, errors = check_scripts_coverage(scripts, tests)
        assert not ok
        assert any("conftest.py" in e for e in errors)

    def test_missing_smoke_test_flagged(self, tmp_path: Path) -> None:
        scripts, tests = self._make_harness(tmp_path)
        (tests / "scripts" / "test_scripts_smoke.py").unlink()
        ok, errors = check_scripts_coverage(scripts, tests)
        assert not ok
        assert any("test_scripts_smoke.py" in e for e in errors)

    def test_broken_glob_marker_flagged(self, tmp_path: Path) -> None:
        scripts, tests = self._make_harness(tmp_path, glob_marker="listdir()")
        ok, errors = check_scripts_coverage(scripts, tests)
        assert not ok
        assert any("auto-coverage is broken" in e for e in errors)

    def test_no_scripts_flagged(self, tmp_path: Path) -> None:
        scripts, tests = self._make_harness(tmp_path)
        (scripts / "foo.py").unlink()
        ok, errors = check_scripts_coverage(scripts, tests)
        assert not ok
        assert any("No scripts/*.py" in e for e in errors)

    def test_single_quote_glob_marker_accepted(self, tmp_path: Path) -> None:
        scripts, tests = self._make_harness(tmp_path, glob_marker="glob('*.py')")
        ok, _errors = check_scripts_coverage(scripts, tests)
        assert ok
