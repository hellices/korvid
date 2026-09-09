#!/usr/bin/env python3
"""Validate wheel and sdist release metadata before publication."""

from __future__ import annotations

import argparse
import ast
import email
import re
import stat
import sys
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass
from email.message import Message
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class _ArchiveMember:
    name: str
    is_regular_file: bool


@dataclass(frozen=True)
class _MarkerValue:
    kind: str
    value: str


@dataclass(frozen=True)
class _MarkerComparison:
    left: _MarkerValue
    operator: str
    right: _MarkerValue


@dataclass(frozen=True)
class _MarkerBoolean:
    operator: str
    operands: tuple[_MarkerNode, ...]


_MarkerNode = _MarkerComparison | _MarkerBoolean


@dataclass(frozen=True)
class _RequirementIdentity:
    name: str
    extras: tuple[str, ...]
    specifiers: tuple[str, ...]
    url: str | None
    marker: _MarkerNode | None


_MARKER_VARIABLES = {
    "dependency_groups",
    "extra",
    "extras",
    "implementation_name",
    "implementation_version",
    "os_name",
    "platform_machine",
    "platform_python_implementation",
    "platform_release",
    "platform_system",
    "platform_version",
    "python_full_version",
    "python_version",
    "sys_platform",
}
# The release workflow runs this script with `uv run --no-project`, so requirement
# normalization must remain independent of `packaging` or any other project dependency.
_MARKER_SYMBOLS = ("===", "~=", "==", "!=", "<=", ">=", "<", ">")
_SPECIFIER = re.compile(r"\s*(===|~=|==|!=|<=|>=|<|>)\s*([^\s,();]+)\s*")
_REQUIREMENT_PREFIX = (
    r"\s*[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
    r"\s*(?:\[\s*[^\]]*\s*\])?\s*"
)


def _normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _quoted_marker_token(marker: str, index: int) -> tuple[str, int]:
    quote = marker[index]
    end = index + 1
    escaped = False
    while end < len(marker):
        character = marker[end]
        if character == quote and not escaped:
            return marker[index : end + 1], end + 1
        escaped = character == "\\" and not escaped
        if character != "\\":
            escaped = False
        end += 1
    raise ValueError(f"unsupported requirement marker {marker!r}")


def _marker_tokens(marker: str) -> tuple[str, ...]:
    tokens: list[str] = []
    index = 0
    while index < len(marker):
        if marker[index].isspace():
            index += 1
            continue
        if marker[index] in "()":
            tokens.append(marker[index])
            index += 1
            continue
        if marker[index] in "\"'":
            token, index = _quoted_marker_token(marker, index)
            tokens.append(token)
            continue
        symbol = next(
            (candidate for candidate in _MARKER_SYMBOLS if marker.startswith(candidate, index)),
            None,
        )
        if symbol is not None:
            tokens.append(symbol)
            index += len(symbol)
            continue
        word = re.match(r"[A-Za-z0-9_.-]+", marker[index:])
        if word is None:
            raise ValueError(f"unsupported requirement marker {marker!r}")
        tokens.append(word.group())
        index += len(word.group())
    return tuple(tokens)


class _MarkerParser:
    def __init__(self, marker: str) -> None:
        self._marker = marker
        self._tokens = _marker_tokens(marker)
        self._index = 0

    def parse(self) -> _MarkerNode:
        expression = self._parse_or()
        if self._index != len(self._tokens):
            raise ValueError(f"unsupported requirement marker {self._marker!r}")
        return expression

    def _parse_or(self) -> _MarkerNode:
        operands = [self._parse_and()]
        while self._take("or"):
            operands.append(self._parse_and())
        return _combine_marker("or", operands)

    def _parse_and(self) -> _MarkerNode:
        operands = [self._parse_atom()]
        while self._take("and"):
            operands.append(self._parse_atom())
        return _combine_marker("and", operands)

    def _parse_atom(self) -> _MarkerNode:
        if self._take("("):
            expression = self._parse_or()
            if not self._take(")"):
                raise ValueError(f"unsupported requirement marker {self._marker!r}")
            return expression
        left = self._parse_value()
        operator = self._parse_operator()
        right = self._parse_value()
        return _MarkerComparison(left, operator, right)

    def _parse_value(self) -> _MarkerValue:
        if self._index == len(self._tokens):
            raise ValueError(f"unsupported requirement marker {self._marker!r}")
        token = self._tokens[self._index]
        self._index += 1
        if token[:1] in "\"'":
            try:
                value = ast.literal_eval(token)
            except (SyntaxError, ValueError) as error:
                raise ValueError(f"unsupported requirement marker {self._marker!r}") from error
            if not isinstance(value, str):
                raise ValueError(f"unsupported requirement marker {self._marker!r}")
            return _MarkerValue("string", value)
        if token not in _MARKER_VARIABLES:
            raise ValueError(f"unsupported requirement marker {self._marker!r}")
        return _MarkerValue("variable", token)

    def _parse_operator(self) -> str:
        if self._index == len(self._tokens):
            raise ValueError(f"unsupported requirement marker {self._marker!r}")
        token = self._tokens[self._index]
        self._index += 1
        if token in _MARKER_SYMBOLS or token == "in":
            return token
        if token == "not" and self._take("in"):
            return "not in"
        raise ValueError(f"unsupported requirement marker {self._marker!r}")

    def _take(self, token: str) -> bool:
        if self._index < len(self._tokens) and self._tokens[self._index] == token:
            self._index += 1
            return True
        return False


def _combine_marker(operator: str, operands: list[_MarkerNode]) -> _MarkerNode:
    flattened: list[_MarkerNode] = []
    for operand in operands:
        if isinstance(operand, _MarkerBoolean) and operand.operator == operator:
            flattened.extend(operand.operands)
        else:
            flattened.append(operand)
    if len(flattened) == 1:
        return flattened[0]
    return _MarkerBoolean(operator, tuple(flattened))


def _split_requirement_marker(requirement: str) -> tuple[str, str | None]:
    direct_url = re.match(f"{_REQUIREMENT_PREFIX}@", requirement) is not None
    if direct_url:
        parts = re.split(r"\s+;\s*", requirement, maxsplit=1)
    else:
        parts = requirement.split(";", maxsplit=1)
    marker = parts[1] if len(parts) == 2 else None
    return parts[0], marker


def _requirement_extras(raw_extras: str | None, requirement: str) -> tuple[str, ...]:
    extras: list[str] = []
    if raw_extras is not None:
        for extra in raw_extras.split(","):
            stripped = extra.strip()
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", stripped):
                raise ValueError(f"unsupported requirement {requirement!r}")
            extras.append(_normalize_name(stripped))
    return tuple(sorted(set(extras)))


def _requirement_target(remainder: str, requirement: str) -> tuple[tuple[str, ...], str | None]:
    if not remainder:
        return (), None
    if remainder.startswith("@"):
        url = remainder[1:].strip()
        if not url or any(character.isspace() for character in url):
            raise ValueError(f"unsupported requirement {requirement!r}")
        return (), url
    if remainder.startswith("(") and remainder.endswith(")"):
        remainder = remainder[1:-1].strip()
    parsed_specifiers: list[str] = []
    for specifier in remainder.split(","):
        specifier_match = _SPECIFIER.fullmatch(specifier)
        if specifier_match is None:
            raise ValueError(f"unsupported requirement {requirement!r}")
        parsed_specifiers.append("".join(specifier_match.groups()))
    return tuple(sorted(parsed_specifiers)), None


def _requirement_identity(requirement: str) -> _RequirementIdentity:
    requirement_part, marker_part = _split_requirement_marker(requirement)
    match = re.fullmatch(
        rf"({_REQUIREMENT_PREFIX})(.*?)\s*",
        requirement_part,
    )
    if match is None:
        raise ValueError(f"unsupported requirement {requirement!r}")
    prefix = re.fullmatch(
        r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
        r"\s*(?:\[\s*([^\]]*)\s*\])?\s*",
        match.group(1),
    )
    if prefix is None:
        raise ValueError(f"unsupported requirement {requirement!r}")
    extras = _requirement_extras(prefix.group(2), requirement)
    specifiers, url = _requirement_target(match.group(2).strip(), requirement)
    marker = None
    if marker_part is not None:
        if not marker_part.strip():
            raise ValueError(f"unsupported requirement marker {marker_part!r}")
        marker = _MarkerParser(marker_part).parse()
    return _RequirementIdentity(
        name=_normalize_name(prefix.group(1)),
        extras=extras,
        specifiers=specifiers,
        url=url,
        marker=marker,
    )


def _extra_name(marker: _MarkerNode) -> str | None:
    if not isinstance(marker, _MarkerComparison) or marker.operator != "==":
        return None
    if marker.left == _MarkerValue("variable", "extra") and marker.right.kind == "string":
        return _normalize_name(marker.right.value)
    if marker.right == _MarkerValue("variable", "extra") and marker.left.kind == "string":
        return _normalize_name(marker.left.value)
    return None


def _contains_extra(marker: _MarkerNode) -> bool:
    if isinstance(marker, _MarkerComparison):
        return marker.left == _MarkerValue("variable", "extra") or marker.right == _MarkerValue(
            "variable", "extra"
        )
    return any(_contains_extra(operand) for operand in marker.operands)


def _metadata_extra(marker: _MarkerNode | None) -> tuple[str | None, _MarkerNode | None]:
    if marker is None:
        return None, None
    direct_extra = _extra_name(marker)
    if direct_extra is not None:
        return direct_extra, None
    if isinstance(marker, _MarkerBoolean) and marker.operator == "and":
        matches = [
            (index, extra)
            for index, operand in enumerate(marker.operands)
            if (extra := _extra_name(operand)) is not None
        ]
        if len(matches) == 1:
            index, extra = matches[0]
            remaining = [
                operand
                for operand_index, operand in enumerate(marker.operands)
                if operand_index != index
            ]
            return extra, _combine_marker("and", remaining)
    if _contains_extra(marker):
        raise ValueError("unsupported requirement marker: extra must be a top-level equality")
    return None, marker


def _wheel_metadata(path: Path) -> Message:
    with zipfile.ZipFile(path) as wheel:
        names = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ValueError(f"{path.name}: expected exactly one .dist-info/METADATA")
        return email.message_from_bytes(wheel.read(names[0]))


def _sdist_metadata(path: Path) -> Message:
    with tarfile.open(path) as sdist:
        members = [member for member in sdist.getmembers() if member.name.endswith("/PKG-INFO")]
        if len(members) != 1:
            raise ValueError(f"{path.name}: expected exactly one PKG-INFO")
        extracted = sdist.extractfile(members[0])
        if extracted is None:
            raise ValueError(f"{path.name}: could not read PKG-INFO")
        return email.message_from_bytes(extracted.read())


def _validate_project_page(artifact: Path, metadata: Message) -> None:
    """The fields that decide whether PyPI shows a page or a blank slab.

    None of this is recoverable after the fact: a released version number
    cannot be reuploaded, so a package that lands with no description keeps
    that page until the next release.
    """
    content_type = metadata.get("Description-Content-Type")
    # Compare the media type exactly. `startswith` also accepts
    # `text/markdown-broken`, which PyPI renders as plain text - a
    # fail-closed check that approves it is worse than no check, because it
    # reports success on the one property that cannot be fixed after upload.
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type != "text/markdown":
        raise ValueError(
            f"{artifact.name}: Description-Content-Type is {content_type!r};"
            " PyPI renders anything but text/markdown as plain text"
        )
    body = metadata.get_payload()
    description = body if isinstance(body, str) else ""
    if not description.strip():
        description = metadata.get("Description", "") or ""
    if len(description.strip()) < 200:
        raise ValueError(
            f"{artifact.name}: the long description is empty or truncated;"
            " it is the PyPI project page"
        )
    # A label alone is not a link: `Project-URL: Homepage` and
    # `Project-URL: Homepage,` both name the entry while pointing nowhere,
    # and either would render an empty sidebar.
    urls = set()
    for entry in metadata.get_all("Project-URL", []):
        label, _, target = entry.partition(",")
        if target.strip():
            urls.add(label.strip())
    required = {"Homepage", "Source", "Issues"}
    missing_urls = required - urls
    if missing_urls:
        raise ValueError(
            f"{artifact.name}: missing Project-URL entries: {sorted(missing_urls)};"
            " PyPI builds the project sidebar from them"
        )


def _validate_metadata(
    artifact: Path,
    metadata: Message,
    *,
    version: str,
    expected_dependencies: dict[str, set[_RequirementIdentity]],
) -> None:
    actual_version = metadata.get("Version")
    if actual_version != version:
        raise ValueError(
            f"{artifact.name}: metadata version {actual_version!r} does not match {version!r}"
        )
    _validate_project_page(artifact, metadata)
    provided = {_normalize_name(extra) for extra in metadata.get_all("Provides-Extra", [])}
    missing = set(expected_dependencies) - provided
    if missing:
        raise ValueError(f"{artifact.name}: missing Provides-Extra entries: {sorted(missing)}")
    requirements = metadata.get_all("Requires-Dist", [])
    actual_dependencies: dict[str, set[_RequirementIdentity]] = {
        extra: set() for extra in expected_dependencies
    }
    for requirement in requirements:
        try:
            identity = _requirement_identity(requirement)
            extra, source_marker = _metadata_extra(identity.marker)
        except ValueError as error:
            raise ValueError(
                f"{artifact.name}: unsupported requirement metadata {requirement!r}"
            ) from error
        if extra in actual_dependencies:
            actual_dependencies[extra].add(
                _RequirementIdentity(
                    name=identity.name,
                    extras=identity.extras,
                    specifiers=identity.specifiers,
                    url=identity.url,
                    marker=source_marker,
                )
            )
    for extra, expected in expected_dependencies.items():
        actual = actual_dependencies[extra]
        if actual != expected:
            missing_dependencies = sorted(repr(item) for item in expected - actual)
            unexpected_dependencies = sorted(repr(item) for item in actual - expected)
            raise ValueError(
                f"{artifact.name}: extra {extra!r} dependency metadata differs;"
                f" missing={missing_dependencies}, unexpected={unexpected_dependencies}"
            )


def _normalized_extras(raw_extras: dict[object, object]) -> dict[str, object]:
    extras = {_normalize_name(str(name)): requirements for name, requirements in raw_extras.items()}
    if len(extras) != len(raw_extras):
        raise ValueError("optional extra names collide after normalization")
    return extras


def _expected_extra_dependencies(
    project: dict[str, object],
) -> dict[str, set[_RequirementIdentity]]:
    """Expand pyproject extras into the requirements Hatch emits.

    Hatch expands a self-reference such as `all = ["korvid[agent,mcp]"]`
    into the dependency union of those extras in wheel/sdist metadata.
    """
    raw_extras = project.get("optional-dependencies", {})
    if not isinstance(raw_extras, dict):
        return {}
    extras = _normalized_extras(raw_extras)
    project_name = _normalize_name(str(project.get("name", "")))
    cache: dict[str, set[_RequirementIdentity]] = {}

    def expand(extra: str, active: frozenset[str] = frozenset()) -> set[_RequirementIdentity]:
        if extra in cache:
            return set(cache[extra])
        if extra in active:
            raise ValueError(f"cyclic optional-extra reference involving {extra!r}")
        requirements = extras.get(extra, [])
        if not isinstance(requirements, list):
            raise ValueError(f"optional extra {extra!r} is not a requirement list")
        dependencies: set[_RequirementIdentity] = set()
        for requirement in requirements:
            identity = _requirement_identity(str(requirement))
            if identity.name == project_name and identity.extras:
                if identity.specifiers or identity.url or identity.marker:
                    raise ValueError(
                        f"unsupported constrained optional-extra reference {requirement!r}"
                    )
                for referenced_extra in identity.extras:
                    dependencies.update(expand(referenced_extra, active | {extra}))
            else:
                dependencies.add(identity)
        cache[extra] = dependencies
        return set(dependencies)

    return {extra: expand(extra) for extra in extras}


def _archive_members(path: Path) -> tuple[_ArchiveMember, ...]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as wheel:
            members: list[_ArchiveMember] = []
            for info in wheel.infolist():
                file_type = stat.S_IFMT(info.external_attr >> 16)
                members.append(
                    _ArchiveMember(
                        name=info.orig_filename,
                        is_regular_file=not info.is_dir() and file_type in {0, stat.S_IFREG},
                    )
                )
            return tuple(members)
    with tarfile.open(path) as sdist:
        return tuple(
            _ArchiveMember(name=member.name, is_regular_file=member.isfile())
            for member in sdist.getmembers()
        )


def _has_contiguous_parts(name: str, expected: tuple[str, ...]) -> bool:
    parts = PurePosixPath(name.replace("\\", "/")).parts
    width = len(expected)
    return any(parts[index : index + width] == expected for index in range(len(parts) - width + 1))


def _validate_contents(
    artifact: Path,
    members: tuple[_ArchiveMember, ...],
    *,
    required_members: tuple[tuple[str, ...], ...],
) -> None:
    forbidden_patterns = [("korvid", "evals"), ("tests", "evals")]
    offender = next(
        (
            member.name
            for member in members
            for forbidden in forbidden_patterns
            if _has_contiguous_parts(member.name, forbidden)
        ),
        None,
    )
    if offender is not None:
        raise ValueError(
            f"{artifact.name}: contains development-only evaluation harness: {offender}"
        )
    member_parts = {
        PurePosixPath(member.name).parts for member in members if member.is_regular_file
    }
    if not any(required in member_parts for required in required_members):
        required = " or ".join("/".join(path) for path in required_members)
        raise ValueError(f"{artifact.name}: missing required production member: {required}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--pyproject", default="pyproject.toml")
    args = parser.parse_args(argv)

    dist = Path(args.dist)
    wheels = sorted(dist.glob("korvid-*.whl"))
    sdists = sorted(dist.glob("korvid-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("dist must contain exactly one korvid wheel and one sdist")
    project = tomllib.loads(Path(args.pyproject).read_text())
    expected_dependencies = _expected_extra_dependencies(project["project"])
    if not expected_dependencies:
        raise ValueError("pyproject.toml declares no optional extras to validate")

    _validate_contents(
        wheels[0],
        _archive_members(wheels[0]),
        required_members=(("korvid", "__init__.py"),),
    )
    sdist_root = sdists[0].name.removesuffix(".tar.gz")
    _validate_contents(
        sdists[0],
        _archive_members(sdists[0]),
        required_members=(("pyproject.toml",), (sdist_root, "pyproject.toml")),
    )
    _validate_metadata(
        wheels[0],
        _wheel_metadata(wheels[0]),
        version=args.version,
        expected_dependencies=expected_dependencies,
    )
    _validate_metadata(
        sdists[0],
        _sdist_metadata(sdists[0]),
        version=args.version,
        expected_dependencies=expected_dependencies,
    )
    print("wheel and sdist metadata verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
