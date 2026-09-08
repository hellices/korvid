from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts" / "release"
sys.path.insert(0, str(SCRIPTS))

import release_config  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path


def _pyproject(tmp_path: Path, *, version: str = "0.4.0", upgrade_from: str = "0.3.0") -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(
        "\n".join(
            (
                "[project]",
                'name = "korvid"',
                f'version = "{version}"',
                "",
                "[tool.korvid.release]",
                f'upgrade-from = "{upgrade_from}"',
                "",
            )
        ),
        encoding="utf-8",
    )
    return path


def test_read_release_config_returns_validated_values(tmp_path: Path) -> None:
    config = release_config.read_release_config(_pyproject(tmp_path))
    assert config == release_config.ReleaseConfig(version="0.4.0", upgrade_source="0.3.0")


def test_cli_prints_selected_value_for_future_release_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _pyproject(tmp_path, version="7.4.0", upgrade_from="7.3.0")

    assert release_config.main(["version", "--pyproject", str(pyproject)]) == 0
    captured = capsys.readouterr()
    assert captured.out == "7.4.0\n"
    assert captured.err == ""

    assert release_config.main(["upgrade-source", "--pyproject", str(pyproject)]) == 0
    captured = capsys.readouterr()
    assert captured.out == "7.3.0\n"
    assert captured.err == ""


def test_cli_uses_default_pyproject_path_from_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _pyproject(tmp_path, version="2.4.6", upgrade_from="2.4.5")
    monkeypatch.chdir(tmp_path)

    assert release_config.main(["version"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "2.4.6\n"
    assert captured.err == ""

    assert release_config.main(["upgrade-source"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "2.4.5\n"
    assert captured.err == ""


@pytest.mark.parametrize("argv", [[], ["current"], ["version", "--bad"], ["version", "extra"]])
def test_invalid_cli_arguments_print_usage(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert release_config.main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{release_config.USAGE}\n"


def test_nonexistent_pyproject_path_fails_actionably(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = tmp_path / "missing.toml"

    assert release_config.main(["version", "--pyproject", str(pyproject)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(pyproject) in captured.err
    assert "does not exist" in captured.err


def test_malformed_toml_fails_actionably(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\nversion =\n", encoding="utf-8")

    assert release_config.main(["version", "--pyproject", str(pyproject)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not valid TOML" in captured.err


def test_directory_input_fails_with_clear_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert release_config.main(["version", "--pyproject", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{tmp_path} is a directory, expected a pyproject.toml file\n"


def test_read_denied_pyproject_fails_with_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _pyproject(tmp_path)
    original = Path.read_text

    def _deny(self: Path, *, encoding: str = "utf-8") -> str:
        if self == pyproject:
            raise PermissionError("permission denied")
        return original(self, encoding=encoding)

    monkeypatch.setattr(Path, "read_text", _deny)

    assert release_config.main(["version", "--pyproject", str(pyproject)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"permission denied reading {pyproject}\n"


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        (
            '[project]\nname = "korvid"\n[tool.korvid.release]\nupgrade-from = "0.3.0"\n',
            "missing project.version",
        ),
        ('[tool.korvid.release]\nupgrade-from = "0.3.0"\n', "missing [project] table"),
        (
            '[project]\nname = "korvid"\nversion = "0.4.0"\n',
            "missing [tool.korvid.release] table",
        ),
        (
            '[project]\nname = "korvid"\nversion = "0.4.0"\n[tool.korvid.release]\n',
            "missing tool.korvid.release.upgrade-from",
        ),
        (
            '[project]\nname = "korvid"\nversion = "0.4.0"\n[tool]\nkorvid = "nope"\n',
            "missing [tool.korvid.release] table",
        ),
        (
            '[project]\nname = "korvid"\nversion = 4\n[tool.korvid.release]\nupgrade-from = "0.3.0"\n',
            "project.version must be a non-empty string",
        ),
        (
            '[project]\nname = "korvid"\nversion = ""\n[tool.korvid.release]\nupgrade-from = "0.3.0"\n',
            "project.version must be a non-empty string",
        ),
        (
            '[project]\nname = "korvid"\nversion = "0.4.0"\n[tool.korvid.release]\nupgrade-from = 3\n',
            "tool.korvid.release.upgrade-from must be a non-empty string",
        ),
        (
            '[project]\nname = "korvid"\nversion = "0.4.0"\n[tool.korvid.release]\nupgrade-from = ""\n',
            "tool.korvid.release.upgrade-from must be a non-empty string",
        ),
        (
            '[project]\nname = "korvid"\nversion = "0.4"\n[tool.korvid.release]\nupgrade-from = "0.3.0"\n',
            "project.version must be a supported release version",
        ),
        (
            '[project]\nname = "korvid"\nversion = "0.4.0"\n[tool.korvid.release]\nupgrade-from = "0.3.0rc1"\n',
            "tool.korvid.release.upgrade-from must be a supported release version",
        ),
        (
            '[project]\nname = "korvid"\nversion = "0.4.0"\n[tool.korvid.release]\nupgrade-from = "0.4.0"\n',
            "tool.korvid.release.upgrade-from must be older than project.version",
        ),
        (
            '[project]\nname = "korvid"\nversion = "0.4.0"\n[tool.korvid.release]\nupgrade-from = "0.5.0"\n',
            "tool.korvid.release.upgrade-from must be older than project.version",
        ),
    ],
)
def test_invalid_release_metadata_fails_actionably(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], contents: str, expected: str
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(contents, encoding="utf-8")

    assert release_config.main(["upgrade-source", "--pyproject", str(pyproject)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert expected in captured.err
