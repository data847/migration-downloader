# Spec: a Bitbucket Cloud archive at GitHub/GitLab parity

Status: built on branch `worktree-bitbucket-cloud-archive` (2026-11-12).

## Goal

Bitbucket Cloud has no export API. Build, per repo, one archive that holds what a GitHub migration archive or a GitLab
project export holds, for as few REST calls as possible, and make everything else extras that run by default and can
be unticked (opt out), not opt in.

## The archive: `<workspace>-<repo>.tar.gz`

The name stays what the mirror already ships under (links must not change).

| Entry | Source | REST calls |
|---|---|---|
| `<repo>.git/` bare mirror, every branch, tag and note | `git clone --mirror` (git pool, 60k/hr) | 0 |
| `<repo>.git/lfs/objects/` | `git lfs fetch --all`, only when the history uses LFS | 0 |
| `refs/pull/<id>/head` inside the mirror | written locally for every PR whose source commit the mirror holds | 0 |
| `repositories_000001.json` | `GET /repositories/{ws}/{repo}` | 1 |
| `organizations_000001.json` | workspace + project | 2 |
| `pull_requests_000001.json` | PR list, every state, reviewers and participants in the listing (`fields=`) | PRs/50 |
| `pull_request_reviews_000001.json` | approvals and change requests | from the activity feed |
| `pull_request_review_comments_000001.json` | inline PR comments | from the activity feed |
| `issue_comments_000001.json` | PR conversation comments | from the activity feed |
| `issue_events_000001.json` | PR updates (state changes, pushes) | from the activity feed |
| `raw/pullrequest_activity.json` | the repo-wide `/pullrequests/activity` feed, unchanged | events/50 |
| `protected_branches_000001.json` | branch restrictions + branching model | 2 |
| `releases_000001.json` | Downloads listing (metadata) | 1 |
| `webhooks_000001.json` | repo hooks | 1 |
| `pipelines_000001.json`, `pipeline_schedules_000001.json` | pipeline listing + schedules | P/100 + 1 |
| `users_000001.json` | every account seen above | 0 |
| `schema.json` | format, what each file is, what is not available, what is omitted by policy, errors | 0 |

If the repo-wide activity feed is refused, the per-PR feed is used instead (one call per PR) and `schema.json` says so.
A part that fails does not stop the archive: it is listed in `schema.json` and the run reports it as an issue.

Not available on Bitbucket Cloud: issues, wikis (removed Aug 2026), labels, milestones, attachments, reactions.
Omitted by policy: deploy keys, members/permissions (`test_removed_collectors_stay_removed` keeps them out).

## for-check.csv

Written beside the archive like GitHub/GitLab: `counts_before/after` (PRs per state from the listing's `size`, branches
and tags from `git ls-remote`, default branch and size from the repo object), `ref_before/after` (`git ls-remote`),
the file list. Unknown counts stay blank.

## Extras: opt out

- The UI already ticks every extra by default; that stays.
- CLI: Bitbucket runs `everything` unless `--extras` names a set; `--skip-extras a,b` (any provider) unticks some;
  `--extras none` turns them off.
- `commits`, `commit-diffstat` are computed from the repo's mirror when the run has it (0 calls
  instead of one call per commit); without a mirror they use the API as before.
- New extras: `pr-tasks` (only PRs with tasks), `deployments` (deployments + environments), `commit-comments` (heavy,
  one call per commit), `test-reports` (heavy, one call per pipeline step).

## Out of scope

Rewriting the rate limiter (upstream already pauses Bitbucket on a 429 from its own call window), per-repo access
tokens, computing PR diffs locally (Bitbucket's diff base for merged PRs is not documented well enough to match it).
