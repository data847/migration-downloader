#!/usr/bin/env python
"""Headless equivalent of the UI — same flow, scriptable.

  ./.venv/bin/python cli.py github google/guava TEST_ORG/guava   # personal + org repos mixed
  ./.venv/bin/python cli.py github --scope user  google/guava
  ./.venv/bin/python cli.py github --scope org --org TEST_ORG guava
  ./.venv/bin/python cli.py gitlab google/guava --poll-interval 20
  ./.venv/bin/python cli.py github --file github_repos.txt
  ./.venv/bin/python cli.py github org/repo --extras all            # + wikis, CI, alerts, hooks…
  ./.venv/bin/python cli.py bitbucket ws/repo --extras all          # Cloud: extras only
  ./.venv/bin/python cli.py bitbucket-dc PROJ/repo --api-base https://bb.example.com
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import bootstrap  # noqa: F401  (sets sys.path)

from migration_api import MigrationError  # noqa: E402
from runner import PROVIDERS, JobSpec, run_job  # noqa: E402
from supplementary import HEAVY, REGISTRY  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("provider", choices=sorted(PROVIDERS))
    p.add_argument("targets", nargs="*", help="owner/repo, bare repo name (org scope) or namespace/project")
    p.add_argument("--file", type=Path, help="read targets from a file, one per line")
    p.add_argument("--scope", choices=["auto", "user", "org"], default="auto",
                   help="GitHub only. auto: owner/repo targets, personal vs org decided by owner")
    p.add_argument("--org", default="", help="GitHub organization (with --scope org)")
    p.add_argument("--token", default="", help="PAT; default: from .env")
    p.add_argument("--api-base", default="", help="GitHub Enterprise / self-hosted GitLab base URL")
    p.add_argument("--api-version", default="", help="X-GitHub-Api-Version override")
    p.add_argument("--poll-interval", type=int, default=15)
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--run-name", default="", help="output subdirectory name")
    p.add_argument("--lock-repositories", action="store_true")
    p.add_argument("--single-archive", action="store_true", help="GitHub: one migration for all repos")
    p.add_argument("--no-checksum", action="store_true")
    p.add_argument("--no-for-check", action="store_true",
                   help="do not write <archive>.for-check.csv (source-side counts/refs/file list for Garmr)")
    p.add_argument("--extras", default="",
                   help="comma list of supplementary collectors, or all (light ones) / everything; "
                        "see --list-extras")
    p.add_argument("--list-extras", action="store_true", help="print collector names per provider and exit")
    p.add_argument("--max-items", type=int, default=200,
                   help="items collected per listing for extras (default 200; 0 = no limit)")
    p.add_argument("--exclude", default="",
                   help="GitHub: comma list of metadata,git_data,attachments,releases,owner_projects")
    p.add_argument("--org-metadata-only", action="store_true", help="GitHub org scope")
    p.add_argument("--unlock-repos", action="store_true",
                   help="GitHub: unlock repos after download (needs --lock-repositories)")
    p.add_argument("--delete-archive", action="store_true",
                   help="GitHub: delete the archive from GitHub after download (irreversible)")
    p.add_argument("--upload-url", default="", help="GitLab: have GitLab upload the export here")
    p.add_argument("--upload-method", default="", choices=["", "PUT", "POST"])
    p.add_argument("--description", default="", help="GitLab export description")
    p.add_argument("--dc-action", default="export", choices=["export", "preview", "cancel", "none"],
                   help="bitbucket-dc export job action")
    p.add_argument("--dc-job-id", default="", help="bitbucket-dc job to cancel")
    args = p.parse_args(argv)

    if args.list_extras:
        for provider, names in sorted(REGISTRY.items()):
            print(f"{provider}:")
            for name in sorted(names):
                print(f"  {name}{'  (heavy)' if name in HEAVY.get(provider, set()) else ''}")
        return 0

    targets = list(args.targets)
    if args.file:
        targets += [l.strip() for l in args.file.read_text().splitlines()
                    if l.strip() and not l.startswith("#")]

    spec = JobSpec(
        provider=args.provider,
        scope=args.scope,
        org=args.org,
        targets=targets,
        token=args.token,
        api_base=args.api_base,
        api_version=args.api_version,
        lock_repositories=args.lock_repositories,
        single_archive=args.single_archive,
        poll_interval=args.poll_interval,
        timeout=args.timeout,
        verify_checksum=not args.no_checksum,
        run_name=args.run_name,
        for_check=not args.no_for_check,
        extras=[e for e in args.extras.split(",") if e.strip()],
        max_items=args.max_items,
        exclude=[e for e in args.exclude.split(",") if e.strip()],
        org_metadata_only=args.org_metadata_only,
        unlock_repos=args.unlock_repos,
        delete_archive=args.delete_archive,
        upload_url=args.upload_url,
        upload_method=args.upload_method,
        description=args.description,
        dc_action=args.dc_action,
        dc_job_id=args.dc_job_id,
    )
    try:
        manifest = run_job(spec, log=print)
    except MigrationError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    return 0 if manifest["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
