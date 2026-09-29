"""Exact package process matching for post-adaptation cleanup."""

from __future__ import annotations


def matching_package_processes(ps_output: str, package_name: str) -> list[str]:
    """Match the main process and package:name children, never similar apps."""
    package_name = str(package_name or "").strip()
    if not package_name:
        return []
    matches = []
    for line in str(ps_output or "").splitlines():
        columns = line.split()
        process_name = columns[-1] if columns else ""
        if process_name == package_name or process_name.startswith(package_name + ":"):
            matches.append(process_name)
    return matches
