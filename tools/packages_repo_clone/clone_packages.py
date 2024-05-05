import os
import toml
import time
import config
import requests


APPS_REPO_ROOT = config.APPS_REPO_ROOT
GITHUB_TOKEN = config.GITHUB_TOKEN
FORGEJO_TOKEN = config.FORGEJO_TOKEN

catalog = toml.load(open(os.path.join(APPS_REPO_ROOT, "apps.toml"), encoding="utf-8"))


def generate_catalog_repo_list():
    # list the apps in the catalog

    apps_repos = []

    for app in catalog:
        url = catalog.get(app)["url"]
        name = url.split("/")[-1]  # get the last part of the URL as the repo name

        apps_repos.append([name, url])

    return apps_repos


def generate_mirror_list():
    # list the existing mirrors on our forgejo

    existing_clones = []

    page = 1
    while True:
        data = requests.get(
            f"https://git.yunohost.org/api/v1/repos/search?topic=false&includeDesc=false&priority_owner_id=17&mode=mirror&page={page}&limit=100",
            timeout=60,
        ).json()["data"]

        # once the data list is empty the whole available pages are consumed
        if not data:
            break

        for repo in data:
            existing_clones.append(repo["name"])

        page += 1

    return existing_clones


def request_handling_rate_limit(method, *args, **kwargs):
    if "timeout" not in kwargs:
        kwargs["timeout"] = 60

    while True:
        response = method(*args, **kwargs)

        # we are not rate limited
        if response.status_code != 422:
            break

        print("We're rate limited. Waiting for 1 minute before continuing.")
        time.sleep(60)

    return response


def generate_mirrors():
    app_catalog = generate_catalog_repo_list()
    mirror_list = generate_mirror_list()

    for app in app_catalog:
        repo_name = app[0]
        repo_url = app[1]

        if not repo_url.startswith("https://github.com/YunoHost-Apps/"):
            # do not care about repos that are not in the YunoHost-Apps org
            continue

        if repo_name in mirror_list:
            # the mirror is already existing
            continue

        print(f"A mirror for '{repo_name}' must be created.")

        api_header = {
            "Content-type": "application/json",
            "Authorization": "{FORGEJO_TOKEN}",
        }

        create_mirror_data = {
            "clone_addr": repo_url,
            "auth_token": GITHUB_TOKEN,
            "mirror": True,
            "repo_name": repo_name,
            "repo_owner": "YunoHost-Apps",
            "service": "github",
        }

        create_mirror_request = request_handling_rate_limit(
            requests.post,
            "https://git.yunohost.org/api/v1/repos/migrate",
            headers=api_header,
            params=f"access_token={FORGEJO_TOKEN}",
            json=create_mirror_data,
        )

        if create_mirror_request.status_code == 409:
            print(f"A repo named '{repo_name}' is already existing.")
            continue

        if create_mirror_request.status_code != 201:
            raise Exception(
                "Request failed:",
                create_mirror_request.status_code,
                create_mirror_request.text,
            )

        # configuring properly the new repository
        settings_mirror_data = {
            "has_packages": False,
            "has_projects": False,
            "has_releases": False,
            "has_wiki": False,
        }

        settings_mirror_request = request_handling_rate_limit(
            requests.patch,
            f"https://git.yunohost.org/api/v1/repos/YunoHost-Apps/{repo_name}",
            headers=api_header,
            params=f"access_token={FORGEJO_TOKEN}",
            json=settings_mirror_data,
        )

        if settings_mirror_request.status_code != 200:
            raise Exception("Request failed:", settings_mirror_request.text)

        print("Repository cloned and configured.")
        time.sleep(5)  # Sleeping for 5 seconds to cooldown the API


generate_mirrors()
