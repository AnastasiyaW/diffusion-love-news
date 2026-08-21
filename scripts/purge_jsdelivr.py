#!/usr/bin/env python3
"""Purge released news JSON from jsDelivr's branch-addressed cache."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import quote
from urllib.request import Request, urlopen


PURGE_ROOT = (
    "https://purge.jsdelivr.net/gh/"
    "AnastasiyaW/diffusion-love-news@main/"
)
CRITICAL_PATHS = (
    "news/_aliases.json",
    "news/_categories.json",
    "news/_index.json",
    "news/_meta.json",
    "news/_projects.json",
    "news/_tags.json",
    "news/_years.json",
    "news/all.json",
)
ITEM_NAME = re.compile(r"n-[0-9a-f]{10}\.json\Z")
SCHEMA_NAME = re.compile(r"schema-v[0-9]+\.[0-9]+\.json\Z")


class PurgeError(RuntimeError):
    """The purge endpoint did not confirm every requested provider."""


def select_purge_paths(changed_paths: Iterable[str]) -> list[str]:
    selected = set(CRITICAL_PATHS)
    for raw_path in changed_paths:
        value = raw_path.strip().replace("\\", "/")
        if not value:
            continue
        path = PurePosixPath(value)
        allowed = (
            value in CRITICAL_PATHS
            or (
                path.parent == PurePosixPath("news")
                and SCHEMA_NAME.fullmatch(path.name) is not None
            )
            or (
                path.parent == PurePosixPath("news/items")
                and ITEM_NAME.fullmatch(path.name) is not None
            )
        )
        if path.is_absolute() or ".." in path.parts or not allowed:
            raise ValueError(f"not a public news JSON path: {raw_path!r}")
        selected.add(path.as_posix())
    return sorted(selected)


def validate_purge_response(payload: object, expected_path: str) -> tuple[str, ...]:
    if not isinstance(payload, dict) or payload.get("status") != "finished":
        raise PurgeError("purge did not finish")
    paths = payload.get("paths")
    if not isinstance(paths, dict) or set(paths) != {expected_path}:
        raise PurgeError("purge response did not bind the requested path")
    result = paths[expected_path]
    if not isinstance(result, dict) or result.get("throttled") is not False:
        raise PurgeError("purge was throttled or malformed")
    providers = result.get("providers")
    if (
        not isinstance(providers, dict)
        or not providers
        or any(value is not True for value in providers.values())
    ):
        raise PurgeError("one or more CDN providers rejected the purge")
    return tuple(sorted(providers))


def purge_path(path: str) -> tuple[str, ...]:
    encoded_path = quote(path, safe="/")
    expected_path = (
        "/gh/AnastasiyaW/diffusion-love-news@main/" + encoded_path
    )
    request = Request(
        PURGE_ROOT + encoded_path,
        headers={"User-Agent": "diffusion-love-news-release/1.0"},
    )
    with urlopen(request, timeout=30) as response:  # nosec B310: fixed HTTPS host
        payload = json.load(response)
    return validate_purge_response(payload, expected_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--paths-file",
        type=Path,
        required=True,
        help="newline-separated changed public JSON paths",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    changed_paths = args.paths_file.read_text(encoding="utf-8").splitlines()
    paths = select_purge_paths(changed_paths)
    for path in paths:
        providers = purge_path(path)
        print(f"purged {path} via {','.join(providers)}")
    print(f"purged {len(paths)} public JSON files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
