#!/usr/bin/env python3

import argparse
import json
import logging
import multiprocessing
import requests
import shutil
import subprocess
import time
from collections import OrderedDict
from functools import cache
from itertools import repeat
from pathlib import Path
from typing import Any, Optional, TypedDict, Union, cast

import toml
import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm
from git import Repo

import appslib.logging_sender  # pylint: disable=import-error
from appslib.utils import (
    get_antifeatures,  # pylint: disable=import-error
    get_catalog,
    get_categories,
    get_security,
    AntiFeature,
    CatalogItem,
    Category,
    SecurityData,
    Subtag,
)
import appslib.get_apps_repo as get_apps_repo

now = time.time()


class BranchCIResult(TypedDict):
    """Structure of a single branch's CI result."""
    commit: str
    level: str
    commit_timestamp: int
    app_version: str
    pr_url: str


AppDevCIResults = dict[str, BranchCIResult]


TOOLS_DIR = Path(__file__).resolve().parent
TOKEN_PATH = TOOLS_DIR / ".forum_token"
FORUM_TOKEN = TOKEN_PATH.open("r", encoding="utf-8").read().strip() if TOKEN_PATH.is_file() else None
FORUM_URL = "https://forum.yunohost.org"

@cache
def categories_list() -> list[Category]:
    # Load categories and reformat the structure to have a list with an "id" key
    categories_data = get_categories()
    result: list[Category] = []
    for category_id, infos in categories_data.items():
        subtags_raw = infos.get("subtags", {})
        subtags_list: list[Subtag] = []
        if isinstance(subtags_raw, dict):
            for subtag_id, subtag_infos in subtags_raw.items():
                subtags_list.append(
                    {
                        "id": subtag_id,
                        "title": subtag_infos["title"],
                    }
                )
        result.append(
            {
                "id": category_id,
                "icon": infos["icon"],
                "title": infos["title"],
                "description": infos["description"],
                "subtags": subtags_list,
            }
        )
    return result


@cache
def antifeatures_list() -> list[AntiFeature]:
    # (Same for antifeatures)
    antifeatures_data = get_antifeatures()
    result: list[AntiFeature] = []
    for antifeature_id, infos in antifeatures_data.items():
        subtags_raw = infos.get("subtags", {})
        subtags_list: list[Subtag] = []
        if isinstance(subtags_raw, dict):
            for subtag_id, subtag_infos in subtags_raw.items():
                subtags_list.append(
                    {
                        "id": subtag_id,
                        "title": subtag_infos["title"],
                    }
                )
        item: AntiFeature = {
            "id": antifeature_id,
            "icon": infos["icon"],
            "title": infos["title"],
            "description": infos["description"],
        }
        if subtags_list:
            item["subtags"] = subtags_list
        result.append(item)
    return result


@cache
def security_list() -> SecurityData:
    security = get_security()
    security["version"] = 1
    return security


def _validate_branch_ci_result(data: Any) -> BranchCIResult:
    """Validate that data conforms to BranchCIResult structure."""
    if not isinstance(data, dict):
        raise TypeError(f"Expected dict, got {type(data)}")
    
    required_keys = {"commit", "level", "commit_timestamp", "app_version", "pr_url"}
    missing_keys = required_keys - set(data.keys())
    if missing_keys:
        raise ValueError(f"Missing required keys: {missing_keys}")
    
    if not isinstance(data["commit"], str):
        raise TypeError(f"commit must be str, got {type(data['commit'])}")
    if not isinstance(data["level"], str):
        raise TypeError(f"level must be str, got {type(data['level'])}")
    if not isinstance(data["commit_timestamp"], int):
        raise TypeError(f"commit_timestamp must be int, got {type(data['commit_timestamp'])}")
    if not isinstance(data["app_version"], str):
        raise TypeError(f"app_version must be str, got {type(data['app_version'])}")
    if not isinstance(data["pr_url"], str):
        raise TypeError(f"pr_url must be str, got {type(data['pr_url'])}")
    
    return cast(BranchCIResult, data)


@cache
def dev_ci_result_per_branch() -> dict[str, AppDevCIResults]:
    url = "https://ci-apps-dev.yunohost.org/ci/api/results-dev"
    try:
        response = requests.get(url).json()
        if not isinstance(response, dict):
            raise TypeError(f"Expected dict at top level, got {type(response)}")
        
        # Validate structure: dict[str, dict[str, BranchCIResult]]
        validated: dict[str, AppDevCIResults] = {}
        for app_id, app_results in response.items():
            if not isinstance(app_results, dict):
                raise TypeError(f"app_results for {app_id} must be dict, got {type(app_results)}")
            
            validated_app_results: AppDevCIResults = {}
            for branch_name, branch_result in app_results.items():
                validated_app_results[branch_name] = _validate_branch_ci_result(branch_result)
            validated[app_id] = validated_app_results
        
        return validated
    except Exception as e:
        logging.error(f"[List builder] Failed to fetch the CI apps dev result : {e}")
        return {}


################################
# Actual list build management #
################################


def __build_app_dict(data: tuple[tuple[str, CatalogItem], Path]) -> Optional[tuple[str, dict[str, Any]]]:
    (name, info), cache_path = data
    try:
        return name, build_app_dict(name, info, cache_path)
    except Exception as err:
        logging.error("[List builder] Error while updating %s: %s", name, err)
        return None


def build_base_catalog(
    catalog: dict[str, CatalogItem], cache_path: Path, nproc: int
) -> dict[str, dict[str, Any]]:
    result_dict: dict[str, dict[str, Any]] = {}

    with multiprocessing.Pool(processes=nproc) as pool:
        with logging_redirect_tqdm():
            tasks = pool.imap(
                __build_app_dict, zip(catalog.items(), repeat(cache_path))
            )

            for result in tqdm.tqdm(tasks, total=len(catalog.keys()), ascii=" ·#"):
                if result is not None:
                    name, info = result
                    result_dict[name] = info

    return result_dict


def write_catalog_v3(base_catalog: dict[str, dict[str, Any]], apps_path: Path, target_dir: Path) -> None:
    logos_dir = target_dir / "logos"
    logos_dir.mkdir(parents=True, exist_ok=True)

    def infos_for_v3(app_id: str, infos: Any) -> Any:
        # We remove the app install question and resources parts which aint
        # needed anymore by webadmin etc (or at least we think ;P)
        if "manifest" in infos and "install" in infos["manifest"]:
            del infos["manifest"]["install"]
        if "manifest" in infos and "resources" in infos["manifest"]:
            del infos["manifest"]["resources"]

        app_id = app_id.lower()
        logo_source = apps_path / "logos" / f"{app_id}.png"
        if logo_source.exists():
            logo_hash = (
                subprocess.check_output(["sha256sum", logo_source])
                .strip()
                .decode("utf-8")
                .split()[0]
            )
            shutil.copyfile(logo_source, logos_dir / f"{logo_hash}.png")
            # FIXME: implement something to cleanup old logo stuf in the builds/.../logos/ folder somehow
        else:
            logo_hash = None
        infos["logo_hash"] = logo_hash

        return infos

    full_catalog = {
        "apps": {app: infos_for_v3(app, info) for app, info in base_catalog.items()},
        "categories": categories_list(),
        "antifeatures": antifeatures_list(),
        "security": security_list(),
    }

    target_file = target_dir / "apps.json"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.open("w", encoding="utf-8").write(
        json.dumps(full_catalog, sort_keys=True)
    )


def build_app_dict(app: str, infos: CatalogItem | dict[str, Any], cache_path: Path) -> dict[str, Any]:
    # Make sure we have some cache
    this_app_cache = cache_path / app
    assert this_app_cache.exists(), f"No cache yet for {app}"

    repo = Repo(this_app_cache)

    # Cast to dict to allow adding new keys
    infos_dict: dict[str, Any] = cast(dict[str, Any], infos)

    # If added_date is not present, we are in a github action of the PR that adds it... so default to a bad value.
    infos_dict["added_in_catalog"] = infos_dict.get("added_date", 0)
    # int(commit_timestamps_for_this_app_in_catalog.split("\n")[0])

    infos_dict["branch"] = infos_dict.get("branch", "master")
    infos_dict["revision"] = infos_dict.get("revision", "HEAD")

    # If using head, find the most recent meaningful commit in logs
    if infos_dict["revision"] == "HEAD":
        infos_dict["revision"] = repo.head.commit.hexsha

    # Otherwise, validate commit exists
    else:
        try:
            _ = repo.commit(infos_dict["revision"])
        except ValueError as err:
            raise RuntimeError(
                f"Revision ain't in history ? {infos_dict['revision']}"
            ) from err

    # Find timestamp corresponding to that commit
    timestamp = repo.commit(infos_dict["revision"]).committed_date

    alternative_branches = {}
    dev_ci_result_for_this_app = dev_ci_result_per_branch().get(app, {})
    for branch, result_infos in dev_ci_result_for_this_app.items():
        # For now, we only advertise testing.
        # We'll probably advertise another branch specifically for Nextcloud.
        # Maybe we should design a proper mechanism to declare somewhere what alternative branch exist
        if branch != "testing":
            continue
        try:
            ahead = not repo.is_ancestor(result_infos["commit"], infos_dict["branch"])  # type: ignore
        except Exception:
            # This will typically fail if the ref of the commit is unknown because it's only a single-branch checkout
            # ... BUT it could also be a super-old commit and we only have the last X commits in our checkout to optimize space hmpf
            ahead = True

        alternative_branches[branch] = {
            # NB : this is the latest commit tested by the dev CI, not the top of the branch
            "revision": result_infos["commit"],
            "level": result_infos["level"],
            "timestamp": result_infos["commit_timestamp"],
            "ahead": ahead,
            "version": result_infos["app_version"],
            "pr_url": result_infos["pr_url"],
        }

    # Build the dict with all the infos
    manifest_file = this_app_cache / "manifest.toml"
    manifest = toml.load(manifest_file.open("r"), _dict=OrderedDict)

    return {
        "id": manifest["id"],
        "git": {
            "branch": infos_dict["branch"],
            "revision": infos_dict["revision"],
            "url": infos_dict["url"],
        },
        "alternative_branches": alternative_branches,
        "added_in_catalog": infos_dict["added_in_catalog"],
        "lastUpdate": timestamp,
        "manifest": manifest,
        "state": infos_dict["state"],
        "level": infos_dict.get("level", "?"),
        "maintained": "package-not-maintained" not in infos_dict.get("antifeatures", []),
        "high_quality": infos_dict.get("high_quality", False),
        "featured": infos_dict.get("featured", False),
        "category": infos_dict.get("category", None),
        "subtags": infos_dict.get("subtags", []),
        "potential_alternative_to": infos_dict.get("potential_alternative_to", []),
        "antifeatures": list(
            set(
                list(manifest.get("antifeatures", {}).keys())
                + infos_dict.get("antifeatures", [])
            )
        ),
    }

def put_forum_app_tags(forum_app_tags: list[str]) -> dict[str, Any] | requests.Response:
    if FORUM_TOKEN is None:
        logging.warning("FORUM_TOKEN not set, skipping tags update.")
        return {}
    # 8 is the ID of the Applications tags list
    # We send the whole list, Discourse can manage pre-existing tags 
    url = f"{FORUM_URL}/tag_groups/8.json"
    try:
        with requests.Session() as s:
            s.headers.update({"Api-Key": FORUM_TOKEN, "Api-Username": "system"})
            return s.put(url, json={"tag_names": forum_app_tags})
    except Exception as e:
        logging.error(f"[List builder] Failed to PUT the forum's apps tags: {e}")
        return {}

def main() -> None:
    parser = argparse.ArgumentParser()
    get_apps_repo.add_args(parser)
    parser.add_argument(
        "target_dir",
        type=Path,
        nargs="?",
        help="The directory to write the catalogs to. Defaults to apps/builds/default",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=multiprocessing.cpu_count(),
        metavar="N",
        help="Allow N threads to run in parallel",
    )
    args = parser.parse_args()

    appslib.logging_sender.enable()

    apps_dir = get_apps_repo.from_args(args)
    cache_path = get_apps_repo.cache_path(args)
    cache_path.mkdir(exist_ok=True, parents=True)
    target_dir = args.target_dir or apps_dir / "builds" / "default"

    catalog = get_catalog(apps_dir)

    print("Retrieving all apps' information to build the catalog...")
    base_catalog = build_base_catalog(catalog, cache_path, args.jobs)

    print(f"Writing the catalogs to {target_dir}...")
    write_catalog_v3(base_catalog, apps_dir, target_dir / "v3")

    print("PUTting the forum's apps tags list")
    put_forum_app_tags(list(base_catalog.keys()))

    print("Done!")


if __name__ == "__main__":
    main()
