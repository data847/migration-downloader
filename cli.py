#!/usr/bin/env python
"""Headless equivalent of the UI — same flow, scriptable.

  ./.venv/bin/python cli.py github --scope user  google/guava
  ./.venv/bin/python cli.py github --scope org --org TEST_ORG guava
  ./.venv/bin/python cli.py gitlab google/guava --poll-interval 20
  ./.venv/bin/python cli.py github --file github_repos.txt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import bootstrap  # noqa: F401  (sets sys.path)

from migration_api import MigrationError  # noqa: E402
from runner import JobSpec, run_job  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("provider", choices=["github", "gitlab"])
    p.add_argument("targets", nargs="*", help="owner/repo, bare repo name (org scope) or namespace/project")
    p.add_argument("--file", type=Path, help="read targets from a file, one per line")
    p.add_argument("--scope", choices=["user", "org"], default="user", help="GitHub only")
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
    args = p.parse_args(argv)

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
    )
    try:
        manifest = run_job(spec, log=print)
    except MigrationError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    return 0 if manifest["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
