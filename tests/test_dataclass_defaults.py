"""Cross-version guards on dataclass field defaults.

CPython 3.11's `dataclasses` refuses a field default whose *class* defines
no `__hash__`::

    ValueError: mutable default <class 'mappingproxy'> for field credential
    is not allowed: use default_factory

The check was not changed by `dataclasses` itself; rather, `types.MappingProxyType`
gained a `__hash__` slot in CPython 3.12 (bpo-87995), so a
`types.MappingProxyType({})` default imports cleanly on 3.12+ and explodes only on
the oldest interpreter korvid supports. The explosion happens while the class body
executes — at *import* time — so the whole test module fails to collect and the
failure looks nothing like the field that caused it.

The sweep and the reinstated-gate import both fail on the offending code on
every interpreter; the last test runs the real import under a real 3.11
(``sys.executable``) when the process is already Python 3.11, and skips on
newer interpreters because the CI matrix has a dedicated 3.11 job.
"""

from __future__ import annotations

import dataclasses
import importlib
import pkgutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

import korvid

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Imports only. Either dataclass raises `ValueError` from its class body on
#: 3.11 when a field default is an unhashable constant, so the import is the
#: whole assertion.
_IMPORT_PROBE = "import korvid.providers.litellm_request, korvid.providers.provider_default"

#: The same import, but with 3.11's gate reinstated on whatever interpreter is
#: running. `dataclasses.dataclass` is replaced before korvid is imported, so
#: every dataclass on the import chain is checked the way 3.11 checks it —
#: including the ones a `list`/`dict` sweep would miss. Runs in a subprocess
#: because the replacement is process-global.
_EMULATED_3_11_PROBE = f"""
import dataclasses as dc

_real = dc.dataclass


def _gate(cls=None, /, **kwargs):
    def wrap(target):
        for name in getattr(target, "__annotations__", {{}}):
            if name not in vars(target):
                continue
            value = vars(target)[name]
            if isinstance(value, dc.Field):
                value = value.default
            if value is dc.MISSING:
                continue
            try:
                hash(value)
            except TypeError:
                raise ValueError(
                    f"mutable default {{type(value)}} for field {{name}}"
                    " is not allowed: use default_factory"
                ) from None
        return _real(target, **kwargs)

    return wrap if cls is None else wrap(cls)


dc.dataclass = _gate

{_IMPORT_PROBE}
"""


def _korvid_modules() -> list[ModuleType]:
    """Import every module in the `korvid` package and return them."""
    modules = [korvid]
    for found in pkgutil.walk_packages(korvid.__path__, "korvid."):
        modules.append(importlib.import_module(found.name))
    return modules


def _korvid_dataclasses() -> list[type]:
    """Every dataclass reachable as an attribute of a `korvid` module."""
    found: dict[str, type] = {}
    for module in _korvid_modules():
        for obj in vars(module).values():
            if isinstance(obj, type) and dataclasses.is_dataclass(obj):
                found[f"{obj.__module__}.{obj.__qualname__}"] = obj
    return list(found.values())


def _is_unhashable(value: object) -> bool:
    """Would CPython 3.11's dataclasses reject this as a mutable default?

    3.11 tests the *class* (`value.__class__.__hash__ is None`). The
    instance check catches the same offenders on 3.12+, where `mappingproxy`
    has a `__hash__` slot that raises for the `dict` underneath it.
    """
    if type(value).__hash__ is None:
        return True
    try:
        hash(value)
    except TypeError:
        return True
    return False


def test_no_dataclass_field_default_is_unhashable() -> None:
    """An unhashable default is a 3.11 import error, so no interpreter may
    ship one — the newest one just cannot see it."""
    offenders = [
        f"{cls.__module__}.{cls.__qualname__}.{spec.name} = {type(spec.default).__name__}"
        for cls in _korvid_dataclasses()
        for spec in dataclasses.fields(cls)
        if spec.default is not dataclasses.MISSING and _is_unhashable(spec.default)
    ]

    assert offenders == [], (
        "these dataclass defaults raise ValueError on Python 3.11; "
        "use a typed default_factory instead: " + ", ".join(sorted(offenders))
    )


def test_the_two_credential_defaults_are_factories_not_constants() -> None:
    """Named, so the fix is pinned where it was made and not only by the
    sweep above, which a future offender would also trip."""
    from korvid.providers.litellm_request import RequestPlan
    from korvid.providers.provider_default import ResolvedCredential

    specs = {
        "RequestPlan.credential": next(
            spec for spec in dataclasses.fields(RequestPlan) if spec.name == "credential"
        ),
        "ResolvedCredential.parameters": next(
            spec for spec in dataclasses.fields(ResolvedCredential) if spec.name == "parameters"
        ),
    }

    for label, spec in specs.items():
        assert spec.default is dataclasses.MISSING, f"{label} still has a constant default"
        assert spec.default_factory is not dataclasses.MISSING, f"{label} has no default_factory"


def test_the_provider_modules_import_with_python_3_11s_gate_reinstated() -> None:
    """The failure as CI saw it — a `ValueError` out of a class body, i.e. a
    collection error — reproduced on any interpreter."""
    result = subprocess.run(
        [sys.executable, "-c", _EMULATED_3_11_PROBE],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        check=False,
        timeout=120,
    )

    assert result.returncode == 0, f"import fails under 3.11's rule:\n{result.stderr}"


def test_the_provider_modules_import_under_python_3_11() -> None:
    """The failure as CI saw it: a collection error on the oldest supported
    interpreter, from an import that succeeds on every newer one.

    Runs only when this process is already Python 3.11; the CI matrix has a
    dedicated 3.11 job so other interpreters skip rather than attempt to
    discover or install a second toolchain.
    """
    if sys.version_info[:2] != (3, 11):
        pytest.skip("not Python 3.11; the CI 3.11 matrix job covers this")

    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        check=False,
        timeout=120,
    )

    assert result.returncode == 0, f"import failed under Python 3.11:\n{result.stderr}"
