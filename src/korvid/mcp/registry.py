"""Private endpoint discovery for the local MCP stdio adapter."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import stat
import sys
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, TypeGuard

_MAX_REGISTRY_BYTES = 256 * 1024
_WINDOWS = os.name == "nt"
_DARWIN = sys.platform == "darwin"
_MAX_PID = 0xFFFFFFFF if _WINDOWS else 0x7FFFFFFF
_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"

_MISSING = "MCP endpoint registry is unavailable; start the korvid TUI with MCP enabled."
_UNSAFE = "MCP endpoint registry is unsafe; restart korvid to recreate a private registry."
_TOO_LARGE = "MCP endpoint registry is too large; restart korvid to recreate it."
_CHANGED = "MCP endpoint registry changed while reading; retry the MCP command."
_INVALID = "MCP endpoint registry is invalid; restart the korvid TUI to refresh it."
_OUTDATED = (
    "MCP endpoint registry lacks a valid capability; restart the korvid TUI "
    "to enable authenticated MCP."
)

_TOKEN_QUERY = 0x0008
_TOKEN_USER_CLASS = 1
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_SDDL_REVISION_1 = 1
_GENERIC_WRITE = 0x40000000
_CREATE_NEW = 1
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_SE_DACL_PROTECTED = 0x1000
_ACL_SIZE_INFORMATION_CLASS = 2
_ACCESS_ALLOWED_ACE_TYPE = 0
_OTHER_ACCESS_ALLOWED_ACE_TYPES = frozenset({4, 5, 9, 11})
_ACL_TYPE_EXTENDED = 0x00000100
_ACL_FLAG_NO_INHERIT = 1 << 17
_FILESEC_MODE = 4
_FILESEC_ACL = 5


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [
        ("Sid", wintypes.LPVOID),
        ("Attributes", wintypes.DWORD),
    ]


class _TokenUser(ctypes.Structure):
    _fields_ = [("User", _SidAndAttributes)]


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("AceType", wintypes.BYTE),
        ("AceFlags", wintypes.BYTE),
        ("AceSize", wintypes.WORD),
    ]


class _AccessAllowedAce(ctypes.Structure):
    _fields_ = [
        ("Header", _AceHeader),
        ("Mask", wintypes.DWORD),
        ("SidStart", wintypes.DWORD),
    ]


class EndpointRegistryError(RuntimeError):
    """The local MCP endpoint registry cannot be used safely."""


@dataclass(frozen=True)
class TUIEndpoint:
    """One running TUI's authenticated loopback MCP endpoint."""

    pid: int
    port: int
    url: str
    capability: str = field(repr=False)


def _platform_attribute(module: object, name: str) -> Any:
    """Return an API whose typeshed availability depends on the target platform."""
    return getattr(module, name)


def _effective_uid() -> int:
    return int(_platform_attribute(os, "geteuid")())


def default_endpoint_path() -> Path:
    """Return the per-user MCP endpoint registry path."""
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "korvid" / "mcp-endpoint.json"


def _windows_libraries() -> tuple[Any, Any]:
    win_dll = _platform_attribute(ctypes, "WinDLL")
    return (
        win_dll("kernel32", use_last_error=True),
        win_dll("advapi32", use_last_error=True),
    )


def _windows_last_error() -> int:
    return int(_platform_attribute(ctypes, "get_last_error")())


def _raise_windows_error(code: int, path: Path | None = None) -> NoReturn:
    filename = str(path) if path is not None else None
    if code in {80, 183}:
        raise FileExistsError(errno.EEXIST, "private file already exists", filename)
    if code == 5:
        raise PermissionError(errno.EACCES, "Windows security operation was denied", filename)
    raise OSError(code, "Windows security operation failed", filename)


def _configure_windows_api(kernel32: Any, advapi32: Any) -> None:
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.LPVOID]
    kernel32.LocalFree.restype = wintypes.LPVOID
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_SecurityAttributes),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.GetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    ]
    advapi32.GetSecurityInfo.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorControl.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = [
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [wintypes.LPVOID, wintypes.LPVOID]
    advapi32.EqualSid.restype = wintypes.BOOL


def _windows_current_user_sid(kernel32: Any, advapi32: Any) -> str:
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        _TOKEN_QUERY,
        ctypes.byref(token),
    ):
        _raise_windows_error(_windows_last_error())
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token,
            _TOKEN_USER_CLASS,
            None,
            0,
            ctypes.byref(required),
        )
        if required.value == 0:
            _raise_windows_error(_windows_last_error())
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            _TOKEN_USER_CLASS,
            buffer,
            required,
            ctypes.byref(required),
        ):
            _raise_windows_error(_windows_last_error())
        token_user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(token_user.User.Sid, ctypes.byref(sid_text)):
            _raise_windows_error(_windows_last_error())
        try:
            if sid_text.value is None:
                _raise_windows_error(0)
            return sid_text.value
        finally:
            kernel32.LocalFree(ctypes.cast(sid_text, wintypes.LPVOID))
    finally:
        kernel32.CloseHandle(token)


def _windows_security_descriptor(kernel32: Any, advapi32: Any) -> wintypes.LPVOID:
    user_sid = _windows_current_user_sid(kernel32, advapi32)
    sddl = (
        f"O:{user_sid}D:P(A;;GA;;;{user_sid})(A;;GA;;;{_SYSTEM_SID})(A;;GA;;;{_ADMINISTRATORS_SID})"
    )
    descriptor = wintypes.LPVOID()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        None,
    ):
        _raise_windows_error(_windows_last_error())
    return descriptor


def _windows_create_private_fd(path: Path) -> int:
    import msvcrt

    kernel32, advapi32 = _windows_libraries()
    _configure_windows_api(kernel32, advapi32)
    descriptor = _windows_security_descriptor(kernel32, advapi32)
    attributes = _SecurityAttributes(
        ctypes.sizeof(_SecurityAttributes),
        descriptor,
        False,
    )
    try:
        handle = kernel32.CreateFileW(
            str(path),
            _GENERIC_WRITE,
            0,
            ctypes.byref(attributes),
            _CREATE_NEW,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            _raise_windows_error(_windows_last_error(), path)
        try:
            return int(
                _platform_attribute(msvcrt, "open_osfhandle")(
                    int(handle),
                    os.O_WRONLY | getattr(os, "O_BINARY", 0),
                )
            )
        except OSError:
            kernel32.CloseHandle(handle)
            path.unlink(missing_ok=True)
            raise
    finally:
        kernel32.LocalFree(descriptor)


def _remove_failed_private_file(path: Path, fd: int) -> None:
    try:
        os.close(fd)
    finally:
        path.unlink(missing_ok=True)


def _windows_open_private_file(path: Path) -> int:
    fd = _windows_create_private_fd(path)
    try:
        private = _windows_fd_is_private(fd)
    except OSError:
        _remove_failed_private_file(path, fd)
        raise PermissionError(
            "Cannot verify the created MCP endpoint registry Windows ACL."
        ) from None
    if not private:
        _remove_failed_private_file(path, fd)
        raise PermissionError("Created MCP endpoint registry Windows ACL is not private.")
    return fd


def _darwin_acl_library() -> Any:
    try:
        library: Any = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        library.acl_init.argtypes = [ctypes.c_int]
        library.acl_init.restype = ctypes.c_void_p
        library.acl_set_fd_np.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        library.acl_set_fd_np.restype = ctypes.c_int
        library.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
        library.acl_get_fd_np.restype = ctypes.c_void_p
        library.acl_get_flagset_np.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        library.acl_get_flagset_np.restype = ctypes.c_int
        library.acl_add_flag_np.argtypes = [ctypes.c_void_p, ctypes.c_int]
        library.acl_add_flag_np.restype = ctypes.c_int
        library.acl_set_flagset_np.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        library.acl_set_flagset_np.restype = ctypes.c_int
        library.acl_free.argtypes = [ctypes.c_void_p]
        library.acl_free.restype = ctypes.c_int
        library.filesec_init.argtypes = []
        library.filesec_init.restype = ctypes.c_void_p
        library.filesec_set_property.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        library.filesec_set_property.restype = ctypes.c_int
        library.filesec_free.argtypes = [ctypes.c_void_p]
        library.filesec_free.restype = None
        library.openx_np.argtypes = [
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        library.openx_np.restype = ctypes.c_int
    except (AttributeError, OSError) as exc:
        raise OSError(errno.ENOTSUP, "macOS ACL APIs are unavailable") from exc
    return library


def _darwin_acl_error() -> OSError:
    code = ctypes.get_errno()
    return OSError(code, os.strerror(code))


def _darwin_fd_is_private(fd: int) -> bool:
    library = _darwin_acl_library()
    ctypes.set_errno(0)
    acl = library.acl_get_fd_np(fd, _ACL_TYPE_EXTENDED)
    if not acl:
        if ctypes.get_errno() == errno.ENOENT:
            return True
        raise _darwin_acl_error()
    try:
        return False
    finally:
        library.acl_free(acl)


def _darwin_unlink_created_file(path: Path, file_fd: int) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    opened = os.fstat(file_fd)
    if stat.S_ISREG(current.st_mode) and _same_file(opened, current):
        path.unlink()


def _darwin_open_private_file(path: Path) -> int:
    library = _darwin_acl_library()
    acl = library.acl_init(1)
    if not acl:
        raise _darwin_acl_error()
    filesec = library.filesec_init()
    if not filesec:
        library.acl_free(acl)
        raise _darwin_acl_error()
    try:
        flagset = ctypes.c_void_p()
        if (
            library.acl_get_flagset_np(acl, ctypes.byref(flagset)) != 0
            or library.acl_add_flag_np(flagset, _ACL_FLAG_NO_INHERIT) != 0
            or library.acl_set_flagset_np(acl, flagset) != 0
        ):
            raise _darwin_acl_error()
        acl_pointer = ctypes.c_void_p(acl)
        mode = ctypes.c_uint16(0o600)
        if (
            library.filesec_set_property(
                filesec,
                _FILESEC_ACL,
                ctypes.byref(acl_pointer),
            )
            != 0
            or library.filesec_set_property(filesec, _FILESEC_MODE, ctypes.byref(mode)) != 0
        ):
            raise _darwin_acl_error()
        ctypes.set_errno(0)
        file_fd = int(
            library.openx_np(
                os.fsencode(path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                filesec,
            )
        )
        if file_fd < 0:
            code = ctypes.get_errno()
            if code == errno.EEXIST:
                raise FileExistsError(code, os.strerror(code), str(path))
            raise OSError(code, os.strerror(code), str(path))
        try:
            opened = os.fstat(file_fd)
            if (
                opened.st_uid != _effective_uid()
                or opened.st_mode & 0o077
                or not _darwin_fd_is_private(file_fd)
            ):
                raise PermissionError("Created MCP endpoint registry is not private.")
            return file_fd
        except BaseException:
            _darwin_unlink_created_file(path, file_fd)
            os.close(file_fd)
            raise
    finally:
        library.filesec_free(filesec)
        library.acl_free(acl)


def open_private_file(path: Path) -> int:
    """Atomically create a credential file accessible only to trusted principals."""
    if _WINDOWS:
        return _windows_open_private_file(path)
    if _DARWIN:
        return _darwin_open_private_file(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return fd


def _windows_sid_from_string(advapi32: Any, sid_text: str) -> wintypes.LPVOID:
    sid = wintypes.LPVOID()
    if not advapi32.ConvertStringSidToSidW(sid_text, ctypes.byref(sid)):
        _raise_windows_error(_windows_last_error())
    return sid


def _windows_allowed_aces_are_trusted(
    dacl: wintypes.LPVOID,
    trusted_sids: list[wintypes.LPVOID],
    advapi32: Any,
) -> bool:
    acl_info = _AclSizeInformation()
    if not advapi32.GetAclInformation(
        dacl,
        ctypes.byref(acl_info),
        ctypes.sizeof(acl_info),
        _ACL_SIZE_INFORMATION_CLASS,
    ):
        _raise_windows_error(_windows_last_error())
    for index in range(acl_info.AceCount):
        ace_pointer = wintypes.LPVOID()
        if not advapi32.GetAce(dacl, index, ctypes.byref(ace_pointer)):
            _raise_windows_error(_windows_last_error())
        if ace_pointer.value is None:
            return False
        header = ctypes.cast(ace_pointer, ctypes.POINTER(_AceHeader)).contents
        if header.AceType in _OTHER_ACCESS_ALLOWED_ACE_TYPES:
            return False
        if header.AceType != _ACCESS_ALLOWED_ACE_TYPE:
            continue
        allowed = ctypes.cast(ace_pointer, ctypes.POINTER(_AccessAllowedAce)).contents
        sid_address = ace_pointer.value + _AccessAllowedAce.SidStart.offset
        ace_sid = wintypes.LPVOID(sid_address)
        if allowed.Mask and not any(
            bool(advapi32.EqualSid(ace_sid, trusted_sid)) for trusted_sid in trusted_sids
        ):
            return False
    return True


def _windows_descriptor_is_private(
    descriptor: wintypes.LPVOID,
    owner: wintypes.LPVOID,
    dacl: wintypes.LPVOID,
    trusted_sids: list[wintypes.LPVOID],
    advapi32: Any,
) -> bool:
    if not owner or not dacl or not bool(advapi32.EqualSid(owner, trusted_sids[0])):
        return False
    control = wintypes.WORD()
    revision = wintypes.DWORD()
    if not advapi32.GetSecurityDescriptorControl(
        descriptor,
        ctypes.byref(control),
        ctypes.byref(revision),
    ):
        _raise_windows_error(_windows_last_error())
    return bool(control.value & _SE_DACL_PROTECTED) and _windows_allowed_aces_are_trusted(
        dacl,
        trusted_sids,
        advapi32,
    )


def _windows_fd_is_private(fd: int) -> bool:
    import msvcrt

    kernel32, advapi32 = _windows_libraries()
    _configure_windows_api(kernel32, advapi32)
    user_sid = _windows_current_user_sid(kernel32, advapi32)
    sid_texts = [user_sid, _SYSTEM_SID, _ADMINISTRATORS_SID]
    trusted_sids: list[wintypes.LPVOID] = []
    descriptor = wintypes.LPVOID()
    owner = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    try:
        for value in sid_texts:
            trusted_sids.append(_windows_sid_from_string(advapi32, value))
        status = int(
            advapi32.GetSecurityInfo(
                wintypes.HANDLE(_platform_attribute(msvcrt, "get_osfhandle")(fd)),
                _SE_FILE_OBJECT,
                _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
                ctypes.byref(owner),
                None,
                ctypes.byref(dacl),
                None,
                ctypes.byref(descriptor),
            )
        )
        if status:
            _raise_windows_error(status)
        return _windows_descriptor_is_private(
            descriptor,
            owner,
            dacl,
            trusted_sids,
            advapi32,
        )
    finally:
        if descriptor:
            kernel32.LocalFree(descriptor)
        for sid in trusted_sids:
            kernel32.LocalFree(sid)


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _preopen_snapshot(path: Path) -> os.stat_result:
    try:
        snapshot = path.lstat()
    except OSError:
        raise EndpointRegistryError(_MISSING) from None
    if not stat.S_ISREG(snapshot.st_mode):
        raise EndpointRegistryError(_UNSAFE)
    return snapshot


def _open_registry(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError:
        raise EndpointRegistryError(_UNSAFE) from None


def _validate_windows_privacy(fd: int) -> None:
    try:
        private = _windows_fd_is_private(fd)
    except OSError:
        raise EndpointRegistryError(
            "Cannot verify the MCP endpoint registry Windows ACL; "
            "restart korvid and check Windows security policy."
        ) from None
    if not private:
        raise EndpointRegistryError(
            "MCP endpoint registry Windows ACL is not private; restart korvid."
        )


def _validate_darwin_privacy(fd: int) -> None:
    try:
        private = _darwin_fd_is_private(fd)
    except OSError:
        raise EndpointRegistryError(
            "Cannot verify the MCP endpoint registry macOS ACL; restart korvid."
        ) from None
    if not private:
        raise EndpointRegistryError(
            "MCP endpoint registry macOS ACL is not private; restart korvid."
        )


def _validate_posix_privacy(fd: int, opened: os.stat_result) -> None:
    if _DARWIN:
        _validate_darwin_privacy(fd)
    if opened.st_uid != _effective_uid():
        raise EndpointRegistryError(
            "MCP endpoint registry has the wrong owner; restart korvid as the current user."
        )
    if opened.st_mode & 0o077:
        raise EndpointRegistryError(
            "MCP endpoint registry is not private; restrict it to owner-only access "
            "or restart korvid."
        )


def _validate_open_file(
    fd: int,
    before_open: os.stat_result,
    opened: os.stat_result,
) -> None:
    if not stat.S_ISREG(opened.st_mode):
        raise EndpointRegistryError(_UNSAFE)
    if not _same_file(before_open, opened):
        raise EndpointRegistryError(_CHANGED)
    if _WINDOWS:
        _validate_windows_privacy(fd)
    else:
        _validate_posix_privacy(fd, opened)
    if opened.st_size > _MAX_REGISTRY_BYTES:
        raise EndpointRegistryError(_TOO_LARGE)


def _read_bounded(fd: int) -> bytes:
    chunks: list[bytes] = []
    remaining = _MAX_REGISTRY_BYTES + 1
    while remaining:
        chunk = os.read(fd, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > _MAX_REGISTRY_BYTES:
        raise EndpointRegistryError(_TOO_LARGE)
    return data


def _read_registry_bytes(path: Path) -> bytes:
    before_open = _preopen_snapshot(path)
    fd = _open_registry(path)
    try:
        opened = os.fstat(fd)
        _validate_open_file(fd, before_open, opened)
        data = _read_bounded(fd)
        after_read = os.fstat(fd)
        _validate_open_file(fd, opened, after_read)
        if (
            not _same_file(opened, after_read)
            or opened.st_size != after_read.st_size
            or opened.st_mtime_ns != after_read.st_mtime_ns
            or opened.st_mode != after_read.st_mode
            or opened.st_uid != after_read.st_uid
        ):
            raise EndpointRegistryError(_CHANGED)
        return data
    except EndpointRegistryError:
        raise
    except OSError:
        raise EndpointRegistryError(_MISSING) from None
    finally:
        os.close(fd)


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-standard JSON constant")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _parse_json(data: bytes) -> object:
    try:
        text = data.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise EndpointRegistryError(_INVALID) from None


def _valid_integer(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_endpoint_pid(pid_key: str, value: object) -> int:
    if not isinstance(value, dict):
        raise EndpointRegistryError(_INVALID)
    pid = value.get("pid")
    if not _valid_integer(pid) or not 1 <= pid <= _MAX_PID or pid_key != str(pid):
        raise EndpointRegistryError(_INVALID)
    return pid


def _parse_endpoint(pid_key: str, value: object) -> TUIEndpoint:
    pid = _parse_endpoint_pid(pid_key, value)
    if not isinstance(value, dict):
        raise EndpointRegistryError(_INVALID)
    if "capability" not in value:
        raise EndpointRegistryError(_OUTDATED)
    if set(value) != {"pid", "port", "url", "capability"}:
        raise EndpointRegistryError(_INVALID)

    port = value["port"]
    url = value["url"]
    capability = value["capability"]
    if not _valid_integer(port) or not 1 <= port <= 65535:
        raise EndpointRegistryError(_INVALID)
    if not isinstance(url, str) or url not in {
        f"http://127.0.0.1:{port}/mcp",
        f"http://127.0.0.1:{port}/mcp/",
    }:
        raise EndpointRegistryError(
            "MCP endpoint registry contains a non-loopback URL; restart korvid."
        )
    if (
        not isinstance(capability, str)
        or len(capability) < 32
        or any(not 0x21 <= ord(character) <= 0x7E for character in capability)
    ):
        raise EndpointRegistryError(_OUTDATED)
    return TUIEndpoint(pid=pid, port=port, url=url, capability=capability)


def _parse_endpoints(data: bytes) -> list[TUIEndpoint]:
    document = _parse_json(data)
    if not isinstance(document, dict) or set(document) != {"servers"}:
        raise EndpointRegistryError(_INVALID)
    servers = document["servers"]
    if not isinstance(servers, dict):
        raise EndpointRegistryError(_INVALID)
    endpoints: list[TUIEndpoint] = []
    for pid_key, entry in servers.items():
        if not isinstance(pid_key, str):
            raise EndpointRegistryError(_INVALID)
        pid = _parse_endpoint_pid(pid_key, entry)
        if isinstance(entry, dict) and set(entry) == {"pid", "port", "url"}:
            if _pid_alive(pid):
                raise EndpointRegistryError(_OUTDATED)
            continue
        endpoints.append(_parse_endpoint(pid_key, entry))
    return endpoints


def _windows_pid_alive(pid: int) -> bool:
    """Query process state without sending a Windows console signal."""
    kernel32, advapi32 = _windows_libraries()
    _configure_windows_api(kernel32, advapi32)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return _windows_last_error() == _ERROR_ACCESS_DENIED
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return int(exit_code.value) == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    if _WINDOWS:
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def read_endpoints(path: Path | None = None) -> list[TUIEndpoint]:
    """Read and validate all live endpoints from the private registry."""
    registry_path = path or default_endpoint_path()
    endpoints = _parse_endpoints(_read_registry_bytes(registry_path))
    return [endpoint for endpoint in endpoints if _pid_alive(endpoint.pid)]


def select_endpoint(
    endpoints: list[TUIEndpoint],
    instance: int | None = None,
) -> TUIEndpoint:
    """Choose one running TUI endpoint, requiring a PID when ambiguous."""
    if instance is not None:
        if not _valid_integer(instance) or instance <= 0:
            raise EndpointRegistryError(
                "Pass a valid positive PID with --instance to select a korvid TUI."
            )
        for endpoint in endpoints:
            if endpoint.pid == instance:
                return endpoint
        raise EndpointRegistryError(
            "No running korvid MCP instance matches --instance PID; "
            "check the PID or restart the TUI."
        )
    if len(endpoints) == 1:
        return endpoints[0]
    if not endpoints:
        raise EndpointRegistryError(
            "No running korvid MCP endpoint was found; start the TUI and enable MCP."
        )
    raise EndpointRegistryError(
        "Multiple korvid MCP instances are running; pass --instance PID to select one."
    )
