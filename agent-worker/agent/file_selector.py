"""
Clones the target repo and selects the source files most relevant to the error.

Strategy (in priority order):
  1. Parse the stack trace for file paths — these are the files Claude needs most.
  2. If < 2 files found (sparse or missing stack trace), fuzzy-match the route
     path segments against the repo directory structure to add more context.
  3. Cap total content at MAX_CONTENT_CHARS to stay well within Claude's context
     window (each file is included in full up to that budget).
"""

import logging
import os
import re
import tempfile
from pathlib import Path

from git import Repo

from .models import ErrorContext

logger = logging.getLogger(__name__)

MAX_CONTENT_CHARS = 80_000   # ~20k tokens — leaves ample room for the prompt
MAX_FILES = 8                # never send more than this many files


def _get_installation_token() -> str:
    """
    Exchange GitHub App credentials for an installation access token.
    Uses direct HTTP calls to the GitHub API 
    """
    import time
    import jwt as pyjwt
    import httpx

    app_id = os.environ["GITHUB_APP_ID"]
    private_key = os.environ["GITHUB_APP_PRIVATE_KEY"].replace("\\n", "\n")
    installation_id = os.environ["GITHUB_INSTALLATION_ID"]

    now = int(time.time())
    payload = {
        "iat": now - 60,   # issued slightly in the past to account for clock skew
        "exp": now + 540,  # 9 minutes (max is 10)
        "iss": app_id,
    }
    encoded_jwt = pyjwt.encode(payload, private_key, algorithm="RS256")

    response = httpx.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {encoded_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=15,
    )
    response.raise_for_status()
    token = response.json()["token"]
    logger.info("GitHub installation token obtained successfully")
    return token


def _clone_url(repo_full_name: str) -> str:
    """Returns an HTTPS clone URL with the GitHub App installation token embedded."""
    token = _get_installation_token()
    return f"https://x-access-token:{token}@github.com/{repo_full_name}.git"


def _parse_stack_trace_paths(stack_trace: str) -> list[str]:
    """
    Extract relative file paths from a Python stack trace.
    Returns de-duped relative paths, stripping leading slashes and common
    absolute prefixes (/app/, /service/, etc.).
    """
    print(f"[FILE_SELECTOR] Raw stack trace:\n{stack_trace[:500]}", flush=True)

    pattern = re.compile(r'File "([^"]+\.py)", line \d+')
    paths = []
    seen = set()

    for match in pattern.finditer(stack_trace):
        raw = match.group(1)
        print(f"[FILE_SELECTOR] Found path in stack trace: {raw}", flush=True)

        # Strip common absolute path prefixes used in containers/Windows
        for prefix in ("/app/", "/service/", "/code/", "/src/", "C:/", "c:/",
                       "C:\\", "c:\\"):
            if raw.lower().startswith(prefix.lower()):
                raw = raw[len(prefix):]
                break

        # Also strip Windows-style absolute paths like C:\Users\...
        raw = re.sub(r'^[A-Za-z]:[/\\]', '', raw)
        raw = raw.lstrip("/\\")
        # Normalise Windows backslashes to forward slashes
        raw = raw.replace("\\", "/")

        if raw not in seen:
            seen.add(raw)
            paths.append(raw)

    print(f"[FILE_SELECTOR] Parsed {len(paths)} path(s) from stack trace: {paths}", flush=True)
    return paths


def _is_third_party(path: str) -> bool:
    """Skip virtual env and installed package paths — not part of the repo."""
    skip = ["site-packages", "dist-packages", "venv", "virtual", ".venv", "lib/python"]
    return any(s in path.lower() for s in skip)


def _strip_to_repo_relative(path: str, repo_name: str) -> str:
    """
    Strip everything up to and including the repo directory name from the path.
    """
    # repo_name is "org/repo" — we want just the repo folder name
    repo_folder = repo_name.split("/")[-1].lower()

    parts = path.replace("\\", "/").split("/")
    for i, part in enumerate(parts):
        if part.lower() == repo_folder:
            relative = "/".join(parts[i + 1:])
            print(f"[FILE_SELECTOR] Stripped to repo-relative path: {relative}", flush=True)
            return relative

    # Could not find repo folder — return as-is and hope for the best
    return path


def _fuzzy_match_route(route: str, repo_root: Path, language: str) -> list[Path]:
    """
    When the stack trace is sparse, try to find files by matching route path
    segments against the repo directory tree.
    """
    ext_map = {
        "python": ".py",
        "typescript": ".ts",
        "javascript": ".js",
        "go": ".go",
        "java": ".java",
        "ruby": ".rb",
    }
    ext = ext_map.get(language.lower(), ".py")

    # Extract meaningful segments (skip version segments like v1, v2)
    segments = [s for s in route.strip("/").split("/") if not re.match(r"^v\d+$", s)]

    candidates = []
    for segment in segments:
        segment_l = segment.lower()
        # Search common source roots
        for src_dir in ["src", "app", "lib", "api", ""]:
            base = repo_root / src_dir if src_dir else repo_root
            if not base.exists():
                continue
            for p in base.rglob(f"*{ext}"):
                full_path_l = str(p).replace("\\", "/").lower()
                if segment_l in p.stem.lower() or f"/{segment_l}/" in full_path_l:
                    candidates.append(p)

    # De-dupe preserving order
    seen = set()
    result = []
    for p in candidates:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result[:MAX_FILES]


def select_files(context: ErrorContext, clone_dir: str) -> tuple[str, str]:
    """
    Clone the repo into a temp directory under clone_dir and select relevant
    source files.

    Returns:
        (repo_local_path, formatted_file_contents_string)
    """
    repo_name = context.route_config.repo
    event = context.event

    logger.info(f"Cloning {repo_name}", extra={"error_id": event.error_id})
    clone_url = _clone_url(repo_name)

    # Clone into a subdirectory named after the error_id for isolation
    local_path = Path(clone_dir) / event.error_id
    local_path.mkdir(parents=True, exist_ok=True)

    Repo.clone_from(clone_url, str(local_path), depth=1)  # shallow clone

    repo_root = local_path
    selected_paths: list[Path] = []

    # Strategy 1: stack trace paths
    if event.stack_trace:
        raw_paths = _parse_stack_trace_paths(event.stack_trace)
        for raw_path in raw_paths:
            # Skip third-party / virtual env files
            if _is_third_party(raw_path):
                print(f"[FILE_SELECTOR] Skipping third-party path: {raw_path}", flush=True)
                continue

            # Strip down to a path relative to the repo root
            rel_path = _strip_to_repo_relative(raw_path, context.route_config.repo)

            abs_path = repo_root / rel_path
            print(f"[FILE_SELECTOR] Checking path exists: {abs_path}", flush=True)
            if abs_path.exists():
                selected_paths.append(abs_path)
            else:
                print(f"[FILE_SELECTOR] Path not found in repo: {rel_path}", flush=True)

    # Strategy 2: fuzzy route matching (supplement if we have fewer than 2 files)
    if len(selected_paths) < 2:
        fuzzy = _fuzzy_match_route(
            event.route, repo_root, context.route_config.language
        )
        for p in fuzzy:
            if p not in selected_paths:
                selected_paths.append(p)

    selected_paths = selected_paths[:MAX_FILES]

    if not selected_paths:
        logger.warning(
            "No relevant files found — agent will have limited context",
            extra={"error_id": event.error_id, "repo": repo_name},
        )

    # Build the formatted file contents string
    sections = []
    total_chars = 0

    for path in selected_paths:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning(f"Could not read {path}: {e}")
            continue

        rel = path.relative_to(repo_root)
        header = f"--- {rel} ---"
        chunk = f"{header}\n{content}\n"

        if total_chars + len(chunk) > MAX_CONTENT_CHARS:
            # Truncate this file to fit within budget
            remaining = MAX_CONTENT_CHARS - total_chars
            if remaining > len(header) + 200:
                chunk = f"{header}\n{content[:remaining - len(header) - 50]}\n... [truncated]\n"
                sections.append(chunk)
            break

        sections.append(chunk)
        total_chars += len(chunk)

    file_contents = "\n".join(sections) if sections else "(no source files found)"

    logger.info(
        "Selected files for agent context",
        extra={
            "error_id": event.error_id,
            "file_count": len(sections),
            "total_chars": total_chars,
            "files": [str(p.relative_to(repo_root)) for p in selected_paths],
        },
    )

    return str(local_path), file_contents
