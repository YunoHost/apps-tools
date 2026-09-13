#!/usr/bin/env python3

import json
import subprocess
from typing import Any, Optional, Literal, cast, TypedDict
from functools import cache
from pathlib import Path
from git import Repo

import jsonschema
import toml

REPO_APPS_ROOT = Path(Repo(__file__, search_parent_directories=True).working_dir)

SecurityLevel = Literal["danger", "warning"]


class SecurityEntry(TypedDict):
    date: str
    title: str
    more_infos: list[str]
    fixed_in_version: str | dict[str, str]
    level: SecurityLevel


class SecurityData(TypedDict, total=False):
    apps: dict[str, list[SecurityEntry]]
    system: dict[str, list[SecurityEntry]]
    version: int


def set_apps_path(apps_path: Path) -> None:
    global REPO_APPS_ROOT
    REPO_APPS_ROOT = apps_path


def git(cmd: list[str], cwd: Optional[Path] = None) -> str:
    full_cmd = ["git"]
    if cwd:
        full_cmd.extend(["-C", str(cwd)])
    full_cmd.extend(cmd)
    return (
        subprocess.check_output(
            full_cmd,
            # env=my_env,
        )
        .strip()
        .decode("utf-8")
    )


@cache
def get_catalog(working_only: bool = False) -> dict[str, dict[str, Any]]:
    """Load the app catalog and filter out the non-working ones"""
    catalog = toml.load((REPO_APPS_ROOT / "apps.toml").open("r", encoding="utf-8"))
    if working_only:
        catalog = {
            app: infos
            for app, infos in catalog.items()
            if infos.get("state") != "notworking"
        }
    return catalog


@cache
def get_categories() -> dict[str, Any]:
    categories_path = REPO_APPS_ROOT / "categories.toml"
    return toml.load(categories_path)


@cache
def get_antifeatures() -> dict[str, Any]:
    antifeatures_path = REPO_APPS_ROOT / "antifeatures.toml"
    return toml.load(antifeatures_path)


@cache
def get_wishlist() -> dict[str, dict[str, str]]:
    wishlist_path = REPO_APPS_ROOT / "wishlist.toml"
    return toml.load(wishlist_path)


@cache
def get_graveyard() -> dict[str, dict[str, str]]:
    wishlist_path = REPO_APPS_ROOT / "graveyard.toml"
    return toml.load(wishlist_path)


@cache
def get_security() -> SecurityData:
    security_path = REPO_APPS_ROOT / "security.toml"
    schema_path = REPO_APPS_ROOT / "schemas" / "security.toml.schema.json"
    data = toml.load(security_path)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.validate(instance=data, schema=schema)
    return cast(SecurityData, data)
