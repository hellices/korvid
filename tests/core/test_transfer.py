"""Tests for core/transfer.py — pure helpers for pod file transfer (issue #47)."""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import pytest

from korvid.core.transfer import (
    TransferSpec,
    default_local_path,
    download_command,
    extract_single_file,
    pack_file,
    permission_hint,
    upload_command,
    validate_spec,
)


class TestTransferSpec:
    def test_frozen(self) -> None:
        spec = TransferSpec(direction="download", remote_path="/tmp/a", local_path="/tmp/b")
        with pytest.raises(AttributeError, match="cannot assign"):
            spec.direction = "upload"  # type: ignore[misc]  # frozen check

    def test_fields(self) -> None:
        spec = TransferSpec(direction="upload", remote_path="/etc/cfg", local_path="~/cfg")
        assert spec.direction == "upload"
        assert spec.remote_path == "/etc/cfg"
        assert spec.local_path == "~/cfg"


class TestValidateSpec:
    def test_valid_download(self) -> None:
        spec = TransferSpec("download", "/var/log/app.log", "/tmp/app.log")
        assert validate_spec(spec) is None

    def test_valid_upload(self, tmp_path: Path) -> None:
        local = tmp_path / "script.sh"
        local.write_text("echo hi\n")
        spec = TransferSpec("upload", "/tmp/script.sh", str(local))
        assert validate_spec(spec) is None

    def test_empty_remote_path(self) -> None:
        spec = TransferSpec("download", "", "/tmp/x")
        error = validate_spec(spec)
        assert error is not None
        assert "remote path" in error

    def test_relative_remote_path(self) -> None:
        spec = TransferSpec("download", "var/log/app.log", "/tmp/x")
        error = validate_spec(spec)
        assert error is not None
        assert "absolute" in error

    def test_remote_path_trailing_slash(self) -> None:
        spec = TransferSpec("download", "/var/log/", "/tmp/x")
        error = validate_spec(spec)
        assert error is not None
        assert "file" in error

    def test_remote_path_with_trailing_space_is_validated_verbatim(self) -> None:
        # "/srv/ " names the file " " in /srv — valid; stripping before
        # validation turned it into the directory "/srv/" and rejected it.
        spec = TransferSpec("download", "/srv/ ", "/tmp/x")
        assert validate_spec(spec) is None

    def test_remote_path_with_leading_space_rejected_as_relative(self) -> None:
        # " /srv/x" is not absolute; validating a stripped copy accepted a
        # different path than the one transferred.
        spec = TransferSpec("download", " /srv/x", "/tmp/x")
        error = validate_spec(spec)
        assert error is not None
        assert "absolute" in error

    def test_blank_remote_path_still_reported_as_required(self) -> None:
        spec = TransferSpec("download", "   ", "/tmp/x")
        error = validate_spec(spec)
        assert error is not None
        assert "required" in error

    def test_empty_local_path(self) -> None:
        spec = TransferSpec("download", "/var/log/app.log", "")
        error = validate_spec(spec)
        assert error is not None
        assert "local path" in error

    def test_upload_missing_local_file(self, tmp_path: Path) -> None:
        spec = TransferSpec("upload", "/tmp/x", str(tmp_path / "nope"))
        error = validate_spec(spec)
        assert error is not None
        assert "not found" in error

    def test_upload_local_directory_rejected(self, tmp_path: Path) -> None:
        spec = TransferSpec("upload", "/tmp/x", str(tmp_path))
        error = validate_spec(spec)
        assert error is not None
        assert "not a regular file" in error

    def test_download_into_missing_directory(self, tmp_path: Path) -> None:
        spec = TransferSpec("download", "/tmp/x", str(tmp_path / "nodir" / "x"))
        error = validate_spec(spec)
        assert error is not None
        assert "directory" in error

    def test_download_local_path_is_existing_directory(self, tmp_path: Path) -> None:
        spec = TransferSpec("download", "/tmp/x", str(tmp_path))
        error = validate_spec(spec)
        assert error is not None
        assert "directory" in error

    @pytest.mark.parametrize("remote", ["/tmp/.", "/tmp/.."])
    def test_dot_basenames_rejected_as_directories(self, remote: str) -> None:
        # "/tmp/." would make tar archive the whole directory recursively;
        # "/tmp/.." an even larger parent tree.
        spec = TransferSpec("download", remote, "/tmp/x")
        error = validate_spec(spec)
        assert error is not None
        assert "directory" in error

    def test_local_path_tilde_expanded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        spec = TransferSpec("download", "/tmp/x", "~/x.log")
        assert validate_spec(spec) is None

    def test_unknown_user_tilde_is_a_validation_error(self) -> None:
        # Path.expanduser raises RuntimeError for an unknown user; that must
        # surface as a validation message, not escape the submit handler.
        spec = TransferSpec("download", "/tmp/x", "~korvid_no_such_user_x9/f")
        error = validate_spec(spec)
        assert error is not None
        assert "cannot expand local path" in error


class TestCommands:
    def test_download_command_splits_dir_and_base(self) -> None:
        assert download_command("/var/log/app.log") == [
            "tar",
            "cf",
            "-",
            "-C",
            "/var/log",
            "app.log",
        ]

    def test_download_command_root_file(self) -> None:
        assert download_command("/app.log") == ["tar", "cf", "-", "-C", "/", "app.log"]

    def test_upload_command_targets_parent_dir(self) -> None:
        assert upload_command("/opt/tools/dbg.sh") == ["tar", "xf", "-", "-C", "/opt/tools"]

    def test_download_command_option_looking_basename_is_neutralised(self) -> None:
        # A basename starting with "-" must not be parsed by tar as an option.
        assert download_command("/tmp/-f") == ["tar", "cf", "-", "-C", "/tmp", "./-f"]


class TestDefaultLocalPath:
    def test_uses_downloads_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "Downloads").mkdir()
        assert default_local_path("/var/log/app.log") == str(tmp_path / "Downloads" / "app.log")

    def test_falls_back_to_home_without_downloads_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Not every home has ~/Downloads; the default must still pass
        # validate_spec (parent exists), so fall back to the home directory.
        monkeypatch.setenv("HOME", str(tmp_path))
        assert default_local_path("/var/log/app.log") == str(tmp_path / "app.log")


class TestPackExtract:
    def test_pack_dereferences_symlink_source(self, tmp_path: Path) -> None:
        # Path.is_file() (used by validation) follows symlinks, so packing
        # must too: the archive carries the target's bytes as a regular file,
        # never a symlink entry the remote tar would recreate as a link.
        target = tmp_path / "real.txt"
        target.write_bytes(b"real bytes")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        archive = tmp_path / "out.tar"
        size = pack_file(link, "f.txt", archive)
        assert size == len(b"real bytes")
        with tarfile.open(archive) as tf:
            member = tf.getmembers()[0]
            assert member.isfile()
            assert not member.issym()
            extracted = tf.extractfile(member)
            assert extracted is not None
            assert extracted.read() == b"real bytes"

    def test_truncated_member_leaves_destination_untouched(self, tmp_path: Path) -> None:
        # Extraction stages into a temp file and atomically replaces the
        # destination only on success, so a truncated archive can never
        # leave partial bytes at (or truncate) an existing destination.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo("f.bin")
            info.size = 4096
            tf.addfile(info, io.BytesIO(b"\xaa" * 4096))
        archive = tmp_path / "truncated.tar"
        archive.write_bytes(buf.getvalue()[: 512 + 100])  # header + partial data
        dest = tmp_path / "dest.bin"
        dest.write_bytes(b"precious old content")
        with pytest.raises(tarfile.ReadError, match="unexpected end of data"):
            extract_single_file(archive, dest)
        assert dest.read_bytes() == b"precious old content"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["dest.bin", "truncated.tar"]

    def test_pack_then_extract_roundtrip(self, tmp_path: Path) -> None:
        src = tmp_path / "data.bin"
        payload = bytes(range(256)) * 100
        src.write_bytes(payload)
        archive = tmp_path / "out.tar"
        size = pack_file(src, "renamed.bin", archive)
        assert size == len(payload)

        dest = tmp_path / "restored.bin"
        written = extract_single_file(archive, dest)
        assert written == len(payload)
        assert dest.read_bytes() == payload

    def test_extract_rejects_directory_archive(self, tmp_path: Path) -> None:
        # A remote path naming a directory makes the remote tar emit the
        # directory and all children recursively; silently extracting the
        # first child would report the wrong file as a success.
        archive = tmp_path / "in.tar"
        inner = tmp_path / "f.txt"
        inner.write_bytes(b"hello")
        with tarfile.open(archive, "w") as tf:
            tf.add(str(tmp_path), arcname="dir", recursive=False)
            tf.add(str(inner), arcname="dir/f.txt")
        dest = tmp_path / "out.txt"
        with pytest.raises(ValueError, match="not a regular file"):
            extract_single_file(archive, dest)
        assert not dest.exists()

    def test_extract_rejects_multi_member_archive(self, tmp_path: Path) -> None:
        # tar cf of a single file yields exactly one member; anything more
        # means the remote path was not the single file that was requested.
        archive = tmp_path / "in.tar"
        first = tmp_path / "a.txt"
        first.write_bytes(b"aa")
        second = tmp_path / "b.txt"
        second.write_bytes(b"bb")
        with tarfile.open(archive, "w") as tf:
            tf.add(str(first), arcname="a.txt")
            tf.add(str(second), arcname="b.txt")
        dest = tmp_path / "out.txt"
        with pytest.raises(ValueError, match="single file"):
            extract_single_file(archive, dest)
        assert not dest.exists()

    def test_extract_empty_archive_raises(self, tmp_path: Path) -> None:
        archive = tmp_path / "empty.tar"
        with tarfile.open(archive, "w"):
            pass
        with pytest.raises(ValueError, match="no file"):
            extract_single_file(archive, tmp_path / "out")

    def test_extract_never_writes_outside_dest(self, tmp_path: Path) -> None:
        # A malicious member name like ../../evil must not matter: extraction
        # streams the member's bytes into the caller-chosen dest path only.
        archive = tmp_path / "evil.tar"
        inner = tmp_path / "f.txt"
        inner.write_bytes(b"pwned")
        with tarfile.open(archive, "w") as tf:
            tf.add(str(inner), arcname="../../evil.txt")
        dest = tmp_path / "safe.txt"
        assert extract_single_file(archive, dest) == 5
        assert dest.read_bytes() == b"pwned"
        assert not (tmp_path.parent.parent / "evil.txt").exists()


_PERMISSIONS_MEANINGLESS = os.name == "nt" or os.geteuid() == 0
_skip_permission_tests = pytest.mark.skipif(
    _PERMISSIONS_MEANINGLESS,
    reason="POSIX permission bits are not meaningful here (Windows or root)",
)


@_skip_permission_tests
class TestValidateSpecPermissions:
    """Issue #123: directory/file permission problems must fail inside the
    dialog with a clear message, never after the intent audit as a raw errno
    leaking the staging file name."""

    def test_download_into_unwritable_directory(self, tmp_path: Path) -> None:
        restricted = tmp_path / "restricted"
        restricted.mkdir(mode=0o500)
        spec = TransferSpec("download", "/var/log/app.log", str(restricted / "app.log"))
        error = validate_spec(spec)
        assert error is not None
        assert "not writable" in error
        assert str(restricted) in error

    def test_download_into_unsearchable_directory(self, tmp_path: Path) -> None:
        # Creating the .part staging file needs the directory's search bit
        # too: read/write without search (0o600) still cannot create the
        # destination.
        unsearchable = tmp_path / "unsearchable"
        unsearchable.mkdir(mode=0o600)
        try:
            spec = TransferSpec("download", "/var/log/app.log", str(unsearchable / "app.log"))
            error = validate_spec(spec)
            assert error is not None
            assert "not writable" in error
        finally:
            unsearchable.chmod(0o700)

    def test_download_onto_readonly_file(self, tmp_path: Path) -> None:
        dest = tmp_path / "app.log"
        dest.write_text("old")
        dest.chmod(0o400)
        spec = TransferSpec("download", "/var/log/app.log", str(dest))
        error = validate_spec(spec)
        assert error is not None
        assert "not writable" in error

    def test_upload_unreadable_source(self, tmp_path: Path) -> None:
        src = tmp_path / "secret.bin"
        src.write_bytes(b"x")
        src.chmod(0o200)
        spec = TransferSpec("upload", "/tmp/secret.bin", str(src))
        error = validate_spec(spec)
        assert error is not None
        assert "not readable" in error

    def test_writable_paths_still_pass(self, tmp_path: Path) -> None:
        spec = TransferSpec("download", "/var/log/app.log", str(tmp_path / "app.log"))
        assert validate_spec(spec) is None


@_skip_permission_tests
class TestDefaultLocalPathPermissions:
    def test_skips_unwritable_downloads_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The default must always survive validate_spec's new writability
        # check, so a read-only ~/Downloads falls back to the home directory.
        monkeypatch.setenv("HOME", str(tmp_path))
        downloads = tmp_path / "Downloads"
        downloads.mkdir(mode=0o500)
        assert default_local_path("/var/log/app.log") == str(tmp_path / "app.log")

    def test_skips_unsearchable_downloads_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        downloads = tmp_path / "Downloads"
        downloads.mkdir(mode=0o600)
        try:
            assert default_local_path("/var/log/app.log") == str(tmp_path / "app.log")
        finally:
            downloads.chmod(0o700)


class TestPermissionHint:
    """Issue #123: a remote permission failure keeps the server's verbatim
    message but gains one actionable hint line."""

    def test_permission_denied_gets_hint(self) -> None:
        hint = permission_hint("tar: app.log: Cannot open: Permission denied", "/app/app.log")
        assert hint is not None
        assert "/app" in hint
        assert "/tmp" in hint

    def test_read_only_filesystem_gets_hint(self) -> None:
        hint = permission_hint("tar: cfg: Cannot open: Read-only file system", "/etc/cfg")
        assert hint is not None
        assert "readOnlyRootFilesystem" in hint

    def test_unrelated_failure_gets_no_hint(self) -> None:
        assert permission_hint("tar: /nope: No such file or directory", "/nope/f") is None

    def test_root_target_names_root(self) -> None:
        hint = permission_hint("Permission denied", "/f")
        assert hint is not None
        assert "to / " in hint or "to /" in hint
