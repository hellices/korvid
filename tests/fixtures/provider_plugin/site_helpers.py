"""Helpers for building discoverable provider-plugin fixture distributions."""

from __future__ import annotations

import importlib.metadata
import textwrap
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent


def build_dist_info(
    base_dir: Path,
    *,
    dist_name: str,
    version: str,
    entry_point_name: str,
    entry_point_value: str,
) -> Path:
    """Create a minimal dist-info directory that importlib.metadata can discover."""
    dist_info = base_dir / f"{dist_name}-{version}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)

    metadata = dist_info / "METADATA"
    metadata.write_text(
        textwrap.dedent(f"""\
            Metadata-Version: 2.1
            Name: {dist_name}
            Version: {version}
        """)
    )

    entry_points = dist_info / "entry_points.txt"
    entry_points.write_text(
        textwrap.dedent(f"""\
            [korvid.provider]
            {entry_point_name} = {entry_point_value}
        """)
    )
    return dist_info


def discover_provider_entry_points(
    path: Path,
) -> list[tuple[importlib.metadata.EntryPoint, str]]:
    """Discover korvid.provider entry points from distributions under *path*."""
    results: list[tuple[importlib.metadata.EntryPoint, str]] = []
    for dist in importlib.metadata.distributions(path=[str(path)]):
        dist_name = dist.name or "<unknown>"
        for entry_point in dist.entry_points:
            if entry_point.group == "korvid.provider":
                results.append((entry_point, dist_name))
    return results
