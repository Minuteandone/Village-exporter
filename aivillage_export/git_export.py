from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .client import PoliteHttpClient
from .utils import iso_to_dt, village_day_bounds

GITHUB_ORG = "ai-village-agents"
GITLAB_GROUP_ID = "136149641"
GITHUB_API = "https://api.github.com"
GITLAB_API = "https://gitlab.com/api/v4"
PAGE_SIZE = 100


@dataclass
class GitDayResult:
    github: list[dict[str, Any]]
    gitlab: list[dict[str, Any]]
    gitlab_projects_scanned: int
    warnings: list[str]


class GitExporter:
    """Optional, deliberately slow public Git activity collector."""

    def __init__(self, http: PoliteHttpClient) -> None:
        self.http = http
        # GitHub's unauthenticated commit-search rate limit is much tighter than normal REST.
        self.http.set_host_delay("api.github.com", 6.25)
        self.http.set_host_delay("gitlab.com", 1.25)
        self._gitlab_projects: list[dict[str, Any]] | None = None

    def _load_gitlab_projects(self, max_pages: int = 20) -> list[dict[str, Any]]:
        if self._gitlab_projects is not None:
            return self._gitlab_projects
        projects: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            params = urllib.parse.urlencode(
                {
                    "include_subgroups": "true",
                    "with_shared": "false",
                    "simple": "true",
                    "order_by": "last_activity_at",
                    "sort": "desc",
                    "per_page": PAGE_SIZE,
                    "page": page,
                }
            )
            batch = self.http.get_json(f"{GITLAB_API}/groups/{GITLAB_GROUP_ID}/projects?{params}")
            if not isinstance(batch, list):
                break
            projects.extend(p for p in batch if isinstance(p, dict))
            if len(batch) < PAGE_SIZE:
                break
        self._gitlab_projects = projects
        return projects

    @staticmethod
    def _project_candidate(project: dict[str, Any], start, end) -> bool:
        if not project.get("default_branch") or project.get("empty_repo"):
            return False
        created = iso_to_dt(project.get("created_at"))
        activity = iso_to_dt(project.get("last_activity_at"))
        return bool(created and activity and created < end and activity >= start)

    def github_commits(self, day: str, max_pages: int = 10) -> tuple[list[dict[str, Any]], list[str]]:
        start, end = village_day_bounds(day)
        end_inclusive = end - timedelta(seconds=1)
        query = f"org:{GITHUB_ORG} committer-date:{start.isoformat().replace('+00:00', 'Z')}..{end_inclusive.isoformat().replace('+00:00', 'Z')}"
        commits: list[dict[str, Any]] = []
        warnings: list[str] = []
        total = None
        for page in range(1, max_pages + 1):
            params = urllib.parse.urlencode(
                {
                    "q": query,
                    "sort": "committer-date",
                    "order": "desc",
                    "per_page": PAGE_SIZE,
                    "page": page,
                }
            )
            payload = self.http.get_json(
                f"{GITHUB_API}/search/commits?{params}",
                accept="application/vnd.github+json",
            )
            items = payload.get("items", []) if isinstance(payload, dict) else []
            total = payload.get("total_count", total) if isinstance(payload, dict) else total
            commits.extend(item for item in items if isinstance(item, dict))
            if isinstance(payload, dict) and payload.get("incomplete_results"):
                warnings.append("GitHub marked commit search as incomplete.")
            if len(items) < PAGE_SIZE or (isinstance(total, int) and len(commits) >= total):
                break
        if isinstance(total, int) and total > len(commits):
            warnings.append(f"GitHub search reported {total} commits but {len(commits)} were retrieved under the safety cap.")
        return commits, warnings

    def gitlab_commits(self, day: str, max_projects: int = 240, max_pages_per_project: int = 3) -> tuple[list[dict[str, Any]], int, list[str]]:
        start, end = village_day_bounds(day)
        candidates = [
            p for p in self._load_gitlab_projects()
            if self._project_candidate(p, start, end)
        ]
        warnings: list[str] = []
        if len(candidates) > max_projects:
            warnings.append(f"GitLab had {len(candidates)} candidate projects; only the first {max_projects} were scanned under the safety cap.")
            candidates = candidates[:max_projects]

        commits: list[dict[str, Any]] = []
        until = (end - timedelta(microseconds=1)).isoformat().replace("+00:00", "Z")
        since = start.isoformat().replace("+00:00", "Z")
        for project in candidates:
            project_id = project.get("id")
            if project_id is None:
                continue
            for page in range(1, max_pages_per_project + 1):
                params = urllib.parse.urlencode(
                    {
                        "all": "true",
                        "since": since,
                        "until": until,
                        "per_page": PAGE_SIZE,
                        "page": page,
                    }
                )
                try:
                    batch = self.http.get_json(
                        f"{GITLAB_API}/projects/{project_id}/repository/commits?{params}"
                    )
                except Exception as exc:
                    warnings.append(f"GitLab project {project.get('path_with_namespace', project_id)} failed: {exc}")
                    break
                if not isinstance(batch, list):
                    break
                for item in batch:
                    if isinstance(item, dict):
                        commits.append({"project": project, "commit": item})
                if len(batch) < PAGE_SIZE:
                    break
                if page == max_pages_per_project:
                    warnings.append(f"GitLab project {project.get('path_with_namespace', project_id)} hit the {max_pages_per_project}-page commit safety cap.")
        return commits, len(candidates), warnings

    def export_day(self, day: str) -> GitDayResult:
        github, gw = self.github_commits(day)
        gitlab, scanned, lw = self.gitlab_commits(day)
        return GitDayResult(github=github, gitlab=gitlab, gitlab_projects_scanned=scanned, warnings=gw + lw)
