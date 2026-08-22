"""
Opens a GitHub PR for the autofix branch.

- Reads CODEOWNERS to assign reviewers (falls back to team name from routing config).
- Adds autofix + needs-review labels (creates them if missing).
- Includes diagnosis, changed files, and collapsible test output in the PR body.
"""

import logging
import os
import re
from pathlib import Path

from github import Auth, Github, GithubException

from .models import AgentResult, ErrorContext, PipelineResult

logger = logging.getLogger(__name__)


def _get_github_client() -> Github:
    app_id = int(os.environ["GITHUB_APP_ID"])
    private_key = os.environ["GITHUB_APP_PRIVATE_KEY"].replace("\\n", "\n")
    installation_id = int(os.environ["GITHUB_INSTALLATION_ID"])
    auth = Auth.AppAuth(app_id, private_key).get_installation_auth(installation_id)
    return Github(auth=auth)


def _parse_codeowners(repo_local_path: str, changed_files: list[str]) -> list[str]:
    """
    Reads the CODEOWNERS file (if present) and returns GitHub usernames / team
    slugs that own any of the changed files.

    Only handles simple glob patterns — not the full CODEOWNERS spec.
    """
    owners: set[str] = set()
    codeowners_paths = [
        Path(repo_local_path) / "CODEOWNERS",
        Path(repo_local_path) / ".github" / "CODEOWNERS",
        Path(repo_local_path) / "docs" / "CODEOWNERS",
    ]

    codeowners_file = next((p for p in codeowners_paths if p.exists()), None)
    if not codeowners_file:
        return []

    for line in codeowners_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split()
        if len(parts) < 2:
            continue

        pattern, *file_owners = parts
        pattern_re = pattern.replace("*", ".*").replace("?", ".")

        for changed in changed_files:
            if re.match(pattern_re, changed):
                for owner in file_owners:
                    # Strip leading @ and skip team slugs (contain /) for direct review requests
                    clean = owner.lstrip("@")
                    if "/" not in clean:   # individual user, not org/team
                        owners.add(clean)

    return list(owners)


def _ensure_labels(repo, label_names: list[str]):
    existing = {label.name for label in repo.get_labels()}
    label_colors = {"autofix": "0075ca", "needs-review": "e4e669"}

    for name in label_names:
        if name not in existing:
            try:
                repo.create_label(name, label_colors.get(name, "ededed"))
                logger.debug(f"Created label '{name}'")
            except GithubException:
                pass  # race condition — another worker created it first, ignore


def _build_pr_body(
    result: PipelineResult,
) -> str:
    ctx = result.context
    event = ctx.event
    agent = result.agent_result
    test_status = "passed" if result.test_passed else ("failed" if result.test_passed is False else "skipped")
    test_badge = "✅ passed" if result.test_passed else ("❌ failed — needs human review" if result.test_passed is False else "⏭ skipped")

    changed_files = "\n".join(f"- `{f.path}`" for f in agent.files) or "_none_"

    body = f"""## Automated Fix Report

| Field | Value |
|---|---|
| **Error route** | `{event.method} {event.route}` |
| **Status code** | `{event.status_code}` |
| **Exception** | `{event.exception_type}` |
| **Service** | `{event.service}` |
| **Environment** | `{event.environment}` |
| **Error ID** | `{event.error_id}` |

## Diagnosis

{agent.diagnosis}

## Changed files

{changed_files}

## Test results — {test_badge}

<details>
<summary>Test output</summary>

```
{result.test_output or "No output captured."}
```

</details>

---
*This PR was generated automatically by the API Error Auto-Fix Agent.*
*Review carefully before merging. The agent may be wrong.*
"""
    return body


def create_pr(result: PipelineResult, repo_local_path: str) -> PipelineResult:
    """
    Opens a GitHub PR and assigns reviewers. Returns the updated PipelineResult
    with pr_url and pr_number populated.
    """
    ctx = result.context
    event = ctx.event
    cfg = ctx.route_config
    agent = result.agent_result

    gh = _get_github_client()
    repo = gh.get_repo(cfg.repo)

    # Labels
    _ensure_labels(repo, ["autofix", "needs-review"])

    # Reviewers — CODEOWNERS first, team name as fallback display only (not assigned)
    changed_file_paths = [f.path for f in agent.files]
    reviewers = _parse_codeowners(repo_local_path, changed_file_paths)

    # PR title
    short_diagnosis = agent.diagnosis[:72]
    title = f"[autofix] {short_diagnosis} (error {event.error_id[:8]})"

    # Default branch
    default_branch = repo.default_branch

    pr = repo.create_pull(
        title=title,
        body=_build_pr_body(result),
        head=result.branch_name,
        base=default_branch,
    )

    pr.add_to_labels("autofix", "needs-review")

    if reviewers:
        try:
            pr.create_review_request(reviewers=reviewers)
        except GithubException as e:
            logger.warning(
                "Could not assign reviewers",
                extra={"error_id": event.error_id, "reviewers": reviewers, "error": str(e)},
            )

    result.pr_url = pr.html_url
    result.pr_number = pr.number

    logger.info(
        "PR created",
        extra={
            "error_id": event.error_id,
            "pr_url": pr.html_url,
            "pr_number": pr.number,
            "reviewers": reviewers,
        },
    )

    return result
