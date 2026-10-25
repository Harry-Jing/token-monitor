"""Publish only the official stable release's Worker to the deployment branch."""

import argparse
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile
import tomllib

UPSTREAM = "Javis603/token-monitor"
REPOSITORY = "Harry-Jing/token-monitor"
DEPLOY_BRANCH = "hub-stable"


def run(*args, cwd=None, capture=True):
    return subprocess.run(
        args, cwd=cwd, check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
    ).stdout


def validate_release(release):
    tag = release.get("tag_name", "")
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise ValueError("Only published, non-prerelease releases are allowed")
    if not release.get("published_at") or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError("Expected a published stable vMAJOR.MINOR.PATCH release")
    return tag


def validate_worker(config):
    if config.get("name") != "token-monitor-hub":
        raise ValueError("Worker identity changed; review before deploying")
    bindings = config.get("durable_objects", {}).get("bindings", [])
    if not any(b.get("name") == "HUB" and b.get("class_name") == "HubDO"
               and not b.get("script_name") for b in bindings):
        raise ValueError("HUB/HubDO data binding changed; review before deploying")
    migrations = config.get("migrations", [])
    if not any(m.get("tag") == "v1" and "HubDO" in m.get("new_sqlite_classes", [])
               for m in migrations):
        raise ValueError("Original SQLite migration is missing")
    for migration in migrations:
        if any(migration.get(k) for k in
               ("deleted_classes", "renamed_classes", "transferred_classes")):
            raise ValueError("Data migration needs manual review")
    variables = config.get("vars", {})
    if "TOKEN_MONITOR_SECRET" in variables:
        raise ValueError("Upstream must not replace the stored Hub secret")
    if str(variables.get("PUBLIC_STATS_ENABLED", "")).strip().lower() in ("1", "true", "yes", "on"):
        raise ValueError("Upstream enables public statistics; review first")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if os.environ.get("GITHUB_REPOSITORY", REPOSITORY) != REPOSITORY:
        raise ValueError("This workflow is configured for Harry-Jing/token-monitor")

    release = json.loads(run("gh", "api", f"repos/{UPSTREAM}/releases/latest"))
    tag = validate_release(release)
    # Resolve and fetch the tag itself, never its target_commitish (often main).
    commit = json.loads(run("gh", "api", f"repos/{UPSTREAM}/commits/{tag}"))["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid upstream commit SHA")

    with tempfile.TemporaryDirectory(prefix="token-monitor-stable-") as folder:
        run("git", "init", "--quiet", "--initial-branch", DEPLOY_BRANCH, folder)
        run("git", "config", "user.name", "github-actions[bot]", cwd=folder)
        run("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com", cwd=folder)
        run("git", "config", "credential.https://github.com.helper", "!gh auth git-credential", cwd=folder)
        run("git", "remote", "add", "origin", f"https://github.com/{REPOSITORY}.git", cwd=folder)
        run("git", "remote", "add", "upstream", f"https://github.com/{UPSTREAM}.git", cwd=folder)
        current = run("git", "ls-remote", "--heads", "origin", f"refs/heads/{DEPLOY_BRANCH}", cwd=folder).strip()
        if current:
            run("git", "fetch", "--quiet", "--depth=1", "origin", f"refs/heads/{DEPLOY_BRANCH}", cwd=folder)
            run("git", "checkout", "--quiet", "-B", DEPLOY_BRANCH, "FETCH_HEAD", cwd=folder)
            marker = Path(folder, ".hub-release.json")
            if marker.exists():
                previous = json.loads(marker.read_text())
                previous_version = tuple(map(int, previous["tag"].removeprefix("v").split(".")))
                next_version = tuple(map(int, tag.removeprefix("v").split(".")))
                if next_version < previous_version:
                    raise ValueError("Refusing to downgrade the deployed stable release")
                if tag == previous["tag"] and commit != previous["commit"]:
                    raise ValueError("Release tag moved; review instead of replacing it silently")

        run("git", "fetch", "--quiet", "--depth=1", "upstream", f"refs/tags/{tag}", cwd=folder)
        fetched = run("git", "rev-parse", "FETCH_HEAD^{commit}", cwd=folder).strip()
        if fetched != commit:
            raise ValueError("Release tag changed during the update; retry after review")
        config = tomllib.loads(run("git", "show", f"{commit}:worker/wrangler.toml", cwd=folder))
        validate_worker(config)
        archive = subprocess.check_output(["git", "archive", "--format=tar", commit, "worker"], cwd=folder)
        run("git", "rm", "--quiet", "-r", "--ignore-unmatch", ".", cwd=folder)
        with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
            for member in contents.getmembers():
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or path.parts[0] != "worker":
                    raise ValueError("Unexpected archive path")
                if not (member.isfile() or member.isdir()):
                    raise ValueError("Unexpected link or special file in Worker release")
            contents.extractall(folder, filter="data")
        metadata = {"upstream": UPSTREAM, "tag": tag, "commit": commit,
                    "published_at": release["published_at"]}
        Path(folder, ".hub-release.json").write_text(json.dumps(metadata, indent=2) + "\n")
        run("git", "add", "worker", ".hub-release.json", cwd=folder)
        unchanged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=folder).returncode
        if unchanged not in (0, 1):
            raise RuntimeError("Unable to compare deployment files")
        if args.check_only:
            result = "Validated; no changes were pushed"
        elif unchanged == 0:
            result = "Already at this official release; no new deployment needed"
        else:
            run("git", "commit", "--quiet", "-m", f"chore(hub): deploy official {tag}", cwd=folder)
            # A normal push rejects concurrent changes instead of overwriting them.
            run("git", "push", "origin", f"HEAD:refs/heads/{DEPLOY_BRANCH}", cwd=folder, capture=False)
            result = "Updated deployment branch; Cloudflare Builds will deploy it"

    summary = (f"## Hub stable update\n\n{result}.\n\n"
               f"- Official release: [{tag}]({release['html_url']})\n"
               f"- Source commit: `{commit}`\n"
               f"- Deployment branch: `{DEPLOY_BRANCH}`\n"
               "- Scope: only the release's `worker/` directory and its version record\n")
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as output:
            output.write(summary)


if __name__ == "__main__":
    main()
