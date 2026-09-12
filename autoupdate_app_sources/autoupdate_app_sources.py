#!/usr/bin/env python3

import argparse
import hashlib
import multiprocessing
import logging
from enum import Enum
from typing import Any, Optional, Union
import re
import sys
from pathlib import Path
from functools import cache
from datetime import datetime

import requests
import toml
import tqdm
import github

# add apps/tools to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from rest_api import (
    GithubAPI,
    GitlabAPI,
    GiteaForgejoAPI,
    DownloadPageAPI,
    RefType,
)  # noqa: E402,E501 pylint: disable=import-error,wrong-import-position
import appslib.get_apps_repo as get_apps_repo
import appslib.logging_sender  # noqa: E402 pylint: disable=import-error,wrong-import-position
from appslib.utils import (
    get_catalog,
)  # noqa: E402 pylint: disable=import-error,wrong-import-position

TOOLS_DIR = Path(__file__).resolve().parent.parent

STRATEGIES = [
    "latest_github_release",
    "latest_github_tag",
    "latest_github_commit",
    "latest_gitlab_release",
    "latest_gitlab_tag",
    "latest_gitlab_commit",
    "latest_gitea_release",
    "latest_gitea_tag",
    "latest_gitea_commit",
    "latest_forgejo_release",
    "latest_forgejo_tag",
    "latest_forgejo_commit",
    "latest_webpage_link",
]

ARCHITECTURES = [
    "amd64", "arm64", "armfh", "i386"
]

TARBALL = "tarball"
ARCHITECTURE_AGNOSTIC = "generic"

class AutoUpdateError(RuntimeError):
    pass

class AssetInfo:
    def __init__(self, url: str, name: str, sha256: str | None = None):
        self.url = url
        self.name = name
        self.sha256 = sha256

    def __str__(self) -> str:
        return self.name

class LatestVersionAvailable:
    def __init__(self, version: str, assets: dict[str, AssetInfo], msg: str):
        self.version = version
        self.assets = assets
        self.msg = msg

    def get_asset_for_arch(self, arch: str) -> AssetInfo:
        if arch in self.assets:
            return self.assets[arch]

        raise AutoUpdateError(f"Arch {arch} not among {self.assets.keys()}")

    def __str__(self) -> str:
        return self.version

@cache
def get_github() -> tuple[
    Optional[tuple[str, str]],
    Optional[github.Github],
    Optional[github.InputGitAuthor],
]:
    try:
        github_login = (
            (TOOLS_DIR / ".github_login").open("r", encoding="utf-8").read().strip()
        )
        github_token = (
            (TOOLS_DIR / ".github_token").open("r", encoding="utf-8").read().strip()
        )
        github_email = (
            (TOOLS_DIR / ".github_email").open("r", encoding="utf-8").read().strip()
        )

        auth = (github_login, github_token)
        github_api = github.Github(github_token, retry=None)
        author = github.InputGitAuthor(github_login, github_email)
        return auth, github_api, author
    except Exception as e:
        logging.warning(f"Could not get github: {e}")
        return None, None, None


def apps_to_run_auto_update_for(cache_path: Path) -> list[str]:
    apps_flagged_as_working_and_on_yunohost_apps_org = [
        app
        for app, infos in get_catalog().items()
        if infos["state"] == "working"
        and "/github.com/yunohost-apps" in infos["url"].lower()
    ]

    relevant_apps = []
    for app in apps_flagged_as_working_and_on_yunohost_apps_org:
        try:
            manifest_toml = cache_path / app / "manifest.toml"
            if manifest_toml.exists():
                manifest = toml.load(manifest_toml.open("r", encoding="utf-8"))
                sources = manifest.get("resources", {}).get("sources", {})
                if any("autoupdate" in source for source in sources.values()):
                    relevant_apps.append(app)
        except Exception as e:
            logging.error(f"Error while loading {app}'s manifest: {e}")
            raise e
    return relevant_apps


class LocalOrRemoteRepo:
    def __init__(self, app: Union[str, Path]) -> None:
        self.local = False
        self.remote = False

        self.app = app
        if isinstance(app, Path):
            # It's local
            self.local = True
            self.manifest_path = app / "manifest.toml"

            if not self.manifest_path.exists():
                raise AutoUpdateError(f"{app.name}: manifest.toml doesnt exists?")
            # app is in fact a path
            self.manifest_raw = (
                (app / "manifest.toml").open("r", encoding="utf-8").read()
            )

        elif isinstance(app, str):
            # It's remote
            self.remote = True
            github = get_github()[1]
            assert github, "Could not get github authentication!"
            self.repo = github.get_repo(f"Yunohost-Apps/{app}_ynh")
            self.pr_branch: Optional[str] = None
            # Determine base branch, either `testing` or default branch
            try:
                self.base_branch = self.repo.get_branch("testing").name
            except Exception:
                self.base_branch = self.repo.default_branch
            contents = self.repo.get_contents("manifest.toml", ref=self.base_branch)
            assert not isinstance(contents, list)
            self.manifest_raw = contents.decoded_content.decode()
            self.manifest_raw_sha = contents.sha

        else:
            raise TypeError(f"Invalid argument type for app: {type(app)}")

    def edit_manifest(self, content: str):
        self.manifest_raw = content
        if self.local:
            self.manifest_path.open("w", encoding="utf-8").write(content)

    def commit(self, message: str):
        if self.remote:
            author = get_github()[2]
            assert author, "Could not get Github author!"
            assert self.pr_branch is not None, "Did you forget to create a branch?"
            self.repo.update_file(
                "manifest.toml",
                message=message,
                content=self.manifest_raw,
                sha=self.manifest_raw_sha,
                branch=self.pr_branch,
                author=author,
            )

    def new_branch(self, name: str) -> bool:
        if self.local:
            logging.warning("Can't create branches for local repositories")
            return False
        if self.remote:
            self.pr_branch = name
            commit_sha = self.repo.get_branch(self.base_branch).commit.sha
            if self.pr_branch in [branch.name for branch in self.repo.get_branches()]:
                print("already existing")
                return False
            self.repo.create_git_ref(ref=f"refs/heads/{name}", sha=commit_sha)
            return True
        return False

    def create_pr(self, branch: str, title: str, message: str) -> Optional[str]:
        if self.remote:
            # Open the PR
            pr = self.repo.create_pull(
                title=title, body=message, head=branch, base=self.base_branch
            )
            return pr.html_url
        logging.warning("Can't create pull requests for local repositories")
        return None

    def get_pr(self, branch: str) -> str:
        return next(pull.html_url for pull in self.repo.get_pulls(head=branch))


class State(Enum):
    up_to_date = 0
    already = 1
    created = 2
    failure = 3


class AppAutoUpdater:
    def __init__(self, app_id: Union[str, Path]) -> None:
        self.repo = LocalOrRemoteRepo(app_id)
        self.manifest = toml.loads(self.repo.manifest_raw)

        self.app_id = self.manifest["id"]
        self.current_version = self.manifest["version"].split("~")[0]
        self.sources = self.manifest.get("resources", {}).get("sources")
        self.main_upstream = self.manifest.get("upstream", {}).get("code")

        if not self.sources:
            raise AutoUpdateError("There's no resources.sources in manifest.toml ?")

        self.main_upstream = self.manifest.get("upstream", {}).get("code")
        self.latest_commit_weekly = False

    def run(
        self, edit: bool = False, commit: bool = False, pr: bool = False
    ) -> tuple[State, str, str, str]:
        state = State.up_to_date
        main_version = ""
        pr_url = ""

        pr_title = commit_msg = "Upgrade sources"
        branch_name = ""
        string_input = ""

        for source, infos in self.sources.items():
            version = self.get_source_update(source, infos)
            if version is None:
                continue
            # We assume we'll create a PR
            state = State.created

            if source == "main":
                main_version = version.version
                branch_name = f"ci-auto-update-{version.version}"
                pr_title = f"Upgrade to v{version}"

            if version.msg:
                commit_msg += f"\n- `{source}` v{version}: {version.msg}"
                string_input += f"\n{source}v{version}"

            self.repo.manifest_raw = self.replace_version_and_asset_in_manifest(
                self.repo.manifest_raw,
                version,
                infos,
                is_main=source == "main",
            )

        if state == State.up_to_date:
            return (State.up_to_date, "", "", "")

        if main_version == "":
            self.repo.manifest_raw = self.bump_version(
                self.repo.manifest_raw, self.current_version, bump_ynh_level=True
            )

        if edit:
            self.repo.edit_manifest(self.repo.manifest_raw)

        try:
            if pr:
                if len(branch_name) == 0:
                    # calculate branch name based on changed asset(s)/version mix to avoid same-update loop
                    branch_name = f"ci-auto-update-sources-{hashlib.sha256(string_input.encode()).hexdigest()[:8]}"

                self.repo.new_branch(branch_name)
        except github.GithubException as e:
            if e.status == 409:
                print("Branch already exists!")

        try:
            if commit:
                self.repo.commit(commit_msg)
        except github.GithubException as e:
            if e.status == 409:
                print("Commits were already commited on branch!")
        try:
            if pr:
                pr_url = self.repo.create_pr(branch_name, pr_title, commit_msg) or ""
        except github.GithubException as e:
            if e.status == 422 or e.status == 409:
                state = State.already
                pr_url = self.repo.get_pr(branch_name)
            else:
                raise
        return (state, self.current_version, main_version, pr_url)

    @staticmethod
    def relevant_versions(
        tags: list[str], app_id: str, version_regex: Optional[str]
    ) -> tuple[str, str]:
        def apply_version_regex(tag: str) -> Optional[str]:
            # First preprocessing according to the manifest version_regex…
            if version_regex:
                match = re.match(version_regex, tag)
                if match is None:
                    return None
                # Basically: either groupdict if named capture gorups, sorted by names, or groups()
                tag = ".".join(
                    dict(sorted(match.groupdict().items())).values() or match.groups()
                )

            if app_id == "photoprism":
                # Stupid fix for Photoprism whose package version is 2024.x while app version is 24.x
                tag = f"20{tag}"

            # Then remove leading v
            tag = tag.lstrip("v")
            return tag

        def version_numbers(tag: str) -> Optional[tuple[int, ...]]:
            filter_keywords = ["start", "rc", "beta", "alpha"]
            if any(keyword in tag for keyword in filter_keywords):
                logging.debug(
                    f"Tag {tag} contains filtered keyword from {filter_keywords}."
                )
                return None

            t_to_check = tag
            if tag.startswith(app_id + "-"):
                t_to_check = tag.split("-", 1)[-1]
            # Boring special case for dokuwiki...
            elif tag.startswith("release-"):
                t_to_check = tag.split("-", 1)[-1].replace("-", ".")

            if re.match(r"^v?\d+(\.\d+)*(\-\d+)?$", t_to_check):
                return AppAutoUpdater.tag_to_int_tuple(t_to_check)
            print(f"Ignoring tag {t_to_check}, doesn't look like a version number")
            return None

        tags_dict: dict[tuple[int, ...], tuple[str, str]] = {}
        for tag in tags:
            tag_clean = apply_version_regex(tag)
            if tag_clean is None:
                continue
            tag_as_ints = version_numbers(tag_clean)
            if tag_as_ints is None:
                continue
            tags_dict[tag_as_ints] = (tag, tag_clean)

        if app_id == "focalboard":
            # Stupid ad-hoc patch for focalboard where 7.11.4 doesn't have the proper asset
            # because idk it was just a patch for mattermost or something
            if "v7.11.4" in tags_dict:
                del tags_dict["v7.11.4"]
            if "7.11.4" in tags_dict:
                del tags_dict["7.11.4"]

        # sorted will sort by keys, tag_as_ints
        # reverse=True will set the last release as first element
        tags_dict = dict(sorted(tags_dict.items(), reverse=True))
        if not tags_dict:
            raise AutoUpdateError("No tags were found after sanity filtering!")
        the_tag_list, (the_tag_orig, the_tag_clean) = next(iter(tags_dict.items()))
        assert the_tag_list is not None
        return the_tag_orig, the_tag_clean

    @staticmethod
    def tag_to_int_tuple(tag: str) -> tuple[int, ...]:
        tag = tag.lstrip("v").replace("-", ".").rstrip(".")
        int_tuple = tag.split(".")
        assert all(i.isdigit() for i in int_tuple), (
            f"Cant convert {tag} to int tuple :/"
        )
        return tuple(int(i) for i in int_tuple)

    @staticmethod
    def sha256_of_remote_file(url: str) -> str:
        print(f"Computing sha256sum for {url} ...")
        try:
            r = requests.get(url, stream=True)
            m = hashlib.sha256()
            for data in r.iter_content(8192):
                m.update(data)
            return m.hexdigest()
        except Exception as e:
            raise AutoUpdateError(f"Failed to compute sha256 for {url} : {e}") from e

    def get_source_update(
        self, name: str, infos: dict[str, Any]
    ) -> Optional[LatestVersionAvailable]:
        autoupdate = infos.get("autoupdate")
        if autoupdate is None:
            return None

        print(f"\n  Checking {name} ...")
        asset = autoupdate.get("asset", TARBALL)

        if (
            isinstance(asset, dict)
            and isinstance(infos.get("url", None), str)
            or isinstance(asset, str)
            and not isinstance(infos.get("url"), str)
        ):
            raise AutoUpdateError(
                "It looks like there's an inconsistency between the old asset list and the new ones... "
                "One is arch-specific, the other is not... Did you forget to define arch-specific regexes? "
            )

        strategy = autoupdate.get("strategy")
        if strategy not in STRATEGIES:
            raise ValueError(
                f"Unknown update strategy '{strategy}' for '{name}', expected one of {STRATEGIES}"
            )

        new_version = self.get_latest_version_and_asset(strategy, asset, infos)
        if new_version is None:
            return None

        if name == "main":
            print(f"Current version in manifest: {self.current_version}")
            print(f"Newest  version on upstream: {new_version.version}")

            # Maybe new version is older than current version
            # Which can happen for example if we manually release a RC,
            # which is ignored by this script
            # Though we wrap this in a try/except pass, because don't want to miserably crash
            # if the tag can't properly be converted to int tuple ...
            if self.current_version == new_version.version:
                print("Up to date")
                return None
            try:
                if self.tag_to_int_tuple(self.current_version) > self.tag_to_int_tuple(
                    new_version.version
                ):
                    print(
                        "Up to date (current version appears more recent than newest version found)"
                    )
                    return None
            except (AssertionError, ValueError):
                pass

        current_assets_urls = infos.get("url", None)
        if isinstance(current_assets_urls, str):
            new_asset = new_version.get_asset_for_arch(ARCHITECTURE_AGNOSTIC)
            if not new_asset:
                raise AutoUpdateError(f"No updated asset found for {name}")
            if new_asset.url == current_assets_urls:
                print(f"URL for asset {name} is up to date")
                return None
        elif all(not infos.get(arch, None) or (new_version.get_asset_for_arch(arch) and new_version.get_asset_for_arch(arch).url == infos[arch]["url"]) for arch in ARCHITECTURES):
            print(f"URLs for asset {name} are up to date")
            return None
        print(f"Update needed for {name}")
        return new_version

    @staticmethod
    def find_matching_asset(assets: list[AssetInfo], regex: str) -> AssetInfo:
        matching_assets = [
            asset for asset in assets if re.match(regex, asset.name)
        ]
        if not matching_assets:
            raise AutoUpdateError(
                f"No assets matching regex '{regex}' in {list(a.name for a in assets)}"
            )
        if len(matching_assets) > 1:
            raise AutoUpdateError(
                f"Too many assets matching regex '{regex}': {matching_assets}"
            )
        return matching_assets[0]

    def get_latest_version_and_asset(
        self, strategy: str, asset: Union[str, dict], infos: dict[str, Union[str, dict[str, str]]]
    ) -> Optional[LatestVersionAvailable]:
        autoupdate = infos.get("autoupdate")
        assert autoupdate is not None and isinstance(autoupdate, dict)
        upstream = autoupdate.get("upstream", self.main_upstream)
        assert upstream and isinstance(upstream, str)
        version_re = autoupdate.get("version_regex", None)
        allow_prereleases = autoupdate.get("allow_prereleases", False)
        branch_name = autoupdate.get("branch", None)
        _, remote_type, revision_type = strategy.split("_")

        api: Union[GithubAPI, GitlabAPI, GiteaForgejoAPI, DownloadPageAPI]

        if remote_type == "github":
            assert upstream.startswith("https://github.com/"), (
                f"When using strategy {strategy}, having a defined upstream code repo on github.com is required"
            )
            api = GithubAPI(upstream, auth=get_github()[0])
        if remote_type == "gitlab":
            api = GitlabAPI(upstream)
        if remote_type in ["gitea", "forgejo"]:
            api = GiteaForgejoAPI(upstream)

        if revision_type == "release":
            releases: dict[str, dict[str, Any]] = {
                release["tag_name"]: release for release in api.releases()
            }

            if not allow_prereleases:
                releases = {
                    name: info
                    for name, info in releases.items()
                    if not info["draft"] and not info["prerelease"]
                }

            latest_version_orig, latest_version = self.relevant_versions(
                list(releases.keys()), self.app_id, version_re
            )
            latest_release = releases[latest_version_orig]
            latest_assets = [
                AssetInfo(a["browser_download_url"], a["name"], a["digest"].replace("sha256:", "") if a.get("digest") else None)
                for a in latest_release["assets"]
                if not a["name"].endswith(".md5")
            ]
            if remote_type in ["gitea", "forgejo"] and len(latest_assets) == 0:
                # if empty (so only the base asset), take the tarball_url
                latest_assets = [ AssetInfo(latest_release["tarball_url"], TARBALL) ]
            # get the release changelog link
            latest_release_html_url = latest_release["html_url"]
            if latest_release_html_url is None or latest_release_html_url == "":
                latest_release_html_url = api.changelog_for_ref(
                    latest_version_orig, "", RefType.releases
                )

            if asset == TARBALL:
                latest_tarball = api.url_for_ref(latest_version_orig, RefType.tags)
                return LatestVersionAvailable(latest_version, { ARCHITECTURE_AGNOSTIC: AssetInfo(latest_tarball, TARBALL) }, latest_release_html_url)
            # FIXME
            if isinstance(asset, str):
                try:
                    asset_info = self.find_matching_asset(latest_assets, asset)
                    return LatestVersionAvailable(latest_version, {ARCHITECTURE_AGNOSTIC: asset_info}, latest_release_html_url)
                except AutoUpdateError as e:
                    raise AutoUpdateError(
                        f"{e}.\nFull release details on {latest_release_html_url}."
                    ) from e
            elif isinstance(asset, dict):
                new_assets = {}
                for asset_name, asset_regex in asset.items():
                    try:
                        asset_info = self.find_matching_asset(latest_assets, asset_regex)
                        new_assets[asset_name] = asset_info
                    except AutoUpdateError as e:
                        raise AutoUpdateError(
                            f"{e}.\nFull release details on {latest_release_html_url}."
                        ) from e
                return LatestVersionAvailable(latest_version, new_assets, latest_release_html_url)

            return None

        elif revision_type == "tag":
            if asset != TARBALL:
                raise ValueError(
                    "For the latest tag strategies, only asset = 'tarball' is supported"
                )
            raw_tags = api.tags()
            try:
                tags = [t["name"] for t in raw_tags]
            except TypeError as e:
                raise Exception(
                    f"Failed to get tag names with raw_tags: {raw_tags}. Original TypeError: {e}"
                )

            if self.app_id == "snweb":
                # Stupid ad-hoc patch for snweb which has a gazillion tags for different components in their repo
                # and we need to get to second page to get the ones relevant for the app ...
                tags += [
                    t["name"]
                    for t in api.internal_api(
                        f"repos/{api.upstream_repo}/tags?per_page=100&page=2"
                    )
                ]
            latest_version_orig, latest_version = self.relevant_versions(
                tags, self.app_id, version_re
            )
            latest_tarball = api.url_for_ref(latest_version_orig, RefType.tags)
            return LatestVersionAvailable(latest_version, {ARCHITECTURE_AGNOSTIC: AssetInfo(latest_tarball, TARBALL) },
                api.changelog_for_ref(latest_version, "", RefType.tags))

        elif revision_type == "commit":
            if self.latest_commit_weekly and datetime.now().weekday() != 0:
                logging.warning(
                    f"Skipping autoupdater for {self.app_id} because 'commit' strategy are only effective on mondays"
                )
                return None
            if asset != TARBALL:
                raise ValueError(
                    "For the latest commit strategies, only asset = 'tarball' is supported"
                )
            latest_commit = None
            if branch_name is None:
                commits = api.commits()
                latest_commit = commits[0]
            else:
                latest_commit = api.tip_of_branch(branch_name)
            latest_tarball = api.url_for_ref(latest_commit["sha"], RefType.commits)
            # Let's have the version as something like "2023.01.23"
            latest_commit_date = datetime.strptime(
                latest_commit["commit"]["author"]["date"][:10], "%Y-%m-%d"
            )
            version_format = autoupdate.get("force_version", "%Y.%m.%d")
            latest_version = latest_commit_date.strftime(version_format)
            return LatestVersionAvailable(
                latest_version,
                { ARCHITECTURE_AGNOSTIC : AssetInfo(latest_tarball, TARBALL) },
                api.changelog_for_ref(
                    latest_commit["sha"], self.get_old_ref(infos), RefType.commits
                )
            )

        elif remote_type == "webpage" and revision_type == "link":
            api = DownloadPageAPI(upstream)
            links = api.get_web_page_links()
            latest_url, latest_version = self.relevant_versions(
                list(links.values()), self.app_id, version_re
            )
            return LatestVersionAvailable(latest_version, {ARCHITECTURE_AGNOSTIC : AssetInfo(latest_url, TARBALL) }, "")

        return None

    @staticmethod
    def get_old_ref(infos: dict[str, Any]) -> str:
        regex = r".*[\/-]([a-f0-9]+)\."
        if isinstance(infos["url"], str):
            try:
                match = re.match(regex, infos["url"])
                assert match and match.group(1)
                return match.group(1)
            except AttributeError as e:
                raise Exception(
                    f"Failed to match regex {regex} on url '{infos['url']}' ?"
                )
        elif isinstance(infos["url"], dict):
            for _, url in infos["url"]:
                match = re.match(regex, url)
                assert match and match.group(1)
                return match.group(1)
        else:
            raise Exception("not reachable, not found old ref")

    def replace_version_and_asset_in_manifest(
        self,
        content: str,
        new_version: LatestVersionAvailable,
        current_assets: dict,
        is_main: bool,
    ):
        replacements = []
        if current_assets.get("url", None):
            asset = new_version.get_asset_for_arch(ARCHITECTURE_AGNOSTIC)
            assert asset
            replacements = [
                (current_assets["url"], asset.url),
                (current_assets["sha256"], asset.sha256 or self.sha256_of_remote_file(asset.url)),
            ]
        else:
            replacements = [
                repl
                for arch in ARCHITECTURES if arch in current_assets
                for repl in (
                    (current_assets[arch]["url"], new_version.get_asset_for_arch(arch).url),
                    (current_assets[arch]["sha256"], new_version.get_asset_for_arch(arch).sha256 or self.sha256_of_remote_file(new_version.get_asset_for_arch(arch).url)),
                )
            ]

        if is_main:
            content = self.bump_version(content, new_version.version)

        for old, new in replacements:
            content = content.replace(old, new)

        return content

    def bump_version(
        self, content: str, new_version: str, bump_ynh_level: bool = False
    ) -> str:
        ynh_level = 1
        if bump_ynh_level:
            ynh_level = (
                int(
                    re.search(
                        r"\s*version\s*=\s*[\"\'][^~\"\']+~ynh(\d+)[\"\']", content
                    ).group(1)
                )
                + 1
            )

        def repl(m: re.Match) -> str:
            return m.group(1) + new_version + f'~ynh{ynh_level}"'

        return re.sub(
            r"(\s*version\s*=\s*[\"\'])([^~\"\']+)(~ynh\d+[\"\'])", repl, content
        )


def paste_on_haste(data):
    # NB: we hardcode this here and can't use the yunopaste command
    # because this script runs on the same machine than haste is hosted on...
    # and doesn't have the proper front-end LE cert in this context
    SERVER_HOST = "https://paste.yunohost.org"
    TIMEOUT = 3
    try:
        url = f"{SERVER_HOST}/documents"
        response = requests.post(url, data=data.encode("utf-8"), timeout=TIMEOUT)
        response.raise_for_status()
        dockey = response.json()["key"]
        return f"{SERVER_HOST}/raw/{dockey}"
    except requests.exceptions.RequestException as e:
        logging.error("\033[31mError: {}\033[0m".format(e))
        raise


class StdoutSwitch:
    class DummyFile:
        def __init__(self) -> None:
            self.result = ""

        def write(self, x: str) -> None:
            self.result += x

    def __init__(self) -> None:
        self.save_stdout = sys.stdout
        sys.stdout = self.DummyFile()  # type: ignore

    def reset(self) -> str:
        result = ""
        if isinstance(sys.stdout, self.DummyFile):
            result = sys.stdout.result
            sys.stdout = self.save_stdout
        return result

    def __exit__(self) -> None:
        sys.stdout = self.save_stdout


def run_autoupdate_for_multiprocessing(data) -> tuple[str, tuple[State, str, str, str]]:
    app, edit, commit, pr, latest_commit_weekly = data
    stdoutswitch = StdoutSwitch()
    try:
        autoupdater = AppAutoUpdater(app)
        autoupdater.latest_commit_weekly = latest_commit_weekly
        result = autoupdater.run(edit=edit, commit=commit, pr=pr)
        return (app, result)
    except AutoUpdateError as e:
        log_str = stdoutswitch.reset()
        return (app, (State.failure, log_str, str(e), ""))
    except requests.exceptions.RequestException as e:
        log_str = stdoutswitch.reset()
        try:
            # Wrapping this in a try/except because not 100% convinced it's the right syntax. To be removed later if that doesn't crash the whole thing
            url = e.request.url
        except Exception as e2:
            url = f"(??? {e2} ???) "
        message = f"HTTP request failed when fetching {url} ... : {e}"
        return (app, (State.failure, log_str, message, ""))
    except Exception:
        log_str = stdoutswitch.reset()
        import traceback

        t = traceback.format_exc()
        return (app, (State.failure, log_str, str(t), ""))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "apps",
        nargs="*",
        type=Path,
        help="If not passed, the script will run on the catalog. Github keys required.",
    )
    parser.add_argument(
        "-w",
        "--latest-commit-weekly",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="For latest_commit versions, only run weekly to prevent too many PRs",
    )
    parser.add_argument(
        "--edit",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Edit the local files",
    )
    parser.add_argument(
        "--commit",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Create a commit with the changes",
    )
    parser.add_argument(
        "--pr",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Create a pull request with the changes",
    )
    parser.add_argument(
        "--matrix-notification",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Send a matrix notification",
    )
    parser.add_argument("--paste", action="store_true")
    parser.add_argument(
        "-j", "--processes", type=int, default=multiprocessing.cpu_count()
    )
    get_apps_repo.add_args(parser)
    args = parser.parse_args()

    appslib.logging_sender.enable()

    if args.commit and not args.edit:
        logging.error("--commit requires --edit")
        sys.exit(1)
    if args.pr and not args.commit:
        logging.error("--pr requires --commit")
        sys.exit(1)

    # Handle apps or no apps
    apps = []
    if args.apps:
        apps = list(args.apps)
    else:
        get_apps_repo.from_args(args)
        cache_path = get_apps_repo.cache_path(args)
        apps_to_run_auto_update_for(cache_path)
    apps_already = {}  # for which a PR already exists
    apps_updated = {}
    apps_failed = {}

    with multiprocessing.Pool(processes=args.processes) as pool:
        tasks = pool.imap(
            run_autoupdate_for_multiprocessing,
            (
                (app, args.edit, args.commit, args.pr, args.latest_commit_weekly)
                for app in apps
            ),
        )
        for app, result in tqdm.tqdm(tasks, total=len(apps), ascii=" ·#"):
            state, current_version, main_version, pr_url = result
            if state == State.up_to_date:
                continue
            if state == State.already:
                apps_already[app] = (current_version, main_version, pr_url)
            if state == State.created:
                apps_updated[app] = (current_version, main_version, pr_url)
            if state == State.failure:
                apps_failed[app] = current_version, main_version  # actually stores logs

    paste_message = ""
    matrix_message = "Autoupdater just ran, here are the results:\n"
    if apps_already:
        paste_message += f"\n{'=' * 80}\nApps already with an update PR:"
        matrix_message += f"\n- {len(apps_already)} pending update PRs"
    for app, info in apps_already.items():
        paste_message += f"\n- {app}"
        paste_message += (
            f" ({info[0]} -> {info[1]})" if info[1] else " (app version did not change)"
        )
        if info[2]:
            paste_message += f" see {info[2]}"

    if apps_updated:
        paste_message += f"\n{'=' * 80}\nApps updated:"
        matrix_message += f"\n- {len(apps_updated)} new apps PRs: {', '.join(str(app) for app in apps_updated.keys())}"
    for app, info in apps_updated.items():
        paste_message += f"\n- {app}"
        paste_message += (
            f" ({info[0]} -> {info[1]})" if info[1] else " (app version did not change)"
        )
        if info[2]:
            paste_message += f" see {info[2]}"

    if apps_failed:
        paste_message += f"\n{'=' * 80}\nApps failed:"
        matrix_message += f"\n- {len(apps_failed)} failed apps updates: {', '.join(str(app) for app in apps_failed.keys())}\n"
    for app, logs in apps_failed.items():
        manifest_url = (
            f"https://github.com/YunoHost-Apps/{app}_ynh/blob/testing/manifest.toml"
        )
        paste_message += f"\n{'=' * 40}\n{app}\n{'-' * 40}\n{logs[0]}\n{logs[1]}\n\nLink to manifest: {manifest_url}\n\n"

    if args.paste:
        paste_url = paste_on_haste(paste_message)
        matrix_message += f"\nSee the full log here: {paste_url}"

    matrix_message += (
        "\nAutoupdate dashboard: https://apps.yunohost.org/dash?filter=autoupdate"
    )

    if args.matrix_notification:
        appslib.logging_sender.notify(matrix_message, "apps", markdown=True)
    print(paste_message)


if __name__ == "__main__":
    main()
