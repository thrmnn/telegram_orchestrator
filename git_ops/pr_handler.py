"""Pull request handling via GitHub API."""

import asyncio
from datetime import datetime
from typing import Optional
from dataclasses import dataclass

import httpx
import structlog

from config import get_settings

logger = structlog.get_logger()


@dataclass
class PullRequest:
    """Pull request data."""
    number: int
    title: str
    body: str
    url: str
    html_url: str
    state: str
    head_branch: str
    base_branch: str
    created_at: datetime
    updated_at: datetime
    mergeable: Optional[bool] = None
    merged: bool = False
    draft: bool = False


class PRHandler:
    """Handles GitHub Pull Request operations."""

    def __init__(self, repo: Optional[str] = None, token: Optional[str] = None):
        self.settings = get_settings()
        self.repo = repo or self.settings.github_repo
        self.token = token or self.settings.github_token
        self.api_base = "https://api.github.com"

    def _get_headers(self) -> dict:
        """Get API headers."""
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def create_pr(
        self,
        title: str,
        head_branch: str,
        base_branch: Optional[str] = None,
        body: Optional[str] = None,
        draft: bool = False
    ) -> Optional[PullRequest]:
        """
        Create a new pull request.

        Args:
            title: PR title
            head_branch: Source branch
            base_branch: Target branch (default: repo default)
            body: PR description
            draft: Create as draft PR

        Returns:
            PullRequest object or None on failure
        """
        if not self.repo:
            logger.error("No repository configured")
            return None

        base = base_branch or self.settings.github_base_branch
        url = f"{self.api_base}/repos/{self.repo}/pulls"

        payload = {
            "title": title,
            "head": head_branch,
            "base": base,
            "body": body or "",
            "draft": draft
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    headers=self._get_headers(),
                    json=payload,
                    timeout=30
                )

                if response.status_code == 201:
                    data = response.json()
                    pr = self._parse_pr(data)
                    logger.info(
                        "PR created",
                        number=pr.number,
                        url=pr.html_url
                    )
                    return pr
                else:
                    logger.error(
                        "Failed to create PR",
                        status=response.status_code,
                        response=response.text
                    )
                    return None

        except Exception as e:
            logger.error("PR creation error", error=str(e))
            return None

    async def get_pr(self, pr_number: int) -> Optional[PullRequest]:
        """Get a pull request by number."""
        if not self.repo:
            return None

        url = f"{self.api_base}/repos/{self.repo}/pulls/{pr_number}"

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers=self._get_headers(),
                    timeout=30
                )

                if response.status_code == 200:
                    return self._parse_pr(response.json())
                return None

        except Exception as e:
            logger.error("Failed to get PR", pr_number=pr_number, error=str(e))
            return None

    async def update_pr(
        self,
        pr_number: int,
        title: Optional[str] = None,
        body: Optional[str] = None,
        state: Optional[str] = None
    ) -> bool:
        """Update a pull request."""
        if not self.repo:
            return False

        url = f"{self.api_base}/repos/{self.repo}/pulls/{pr_number}"

        payload = {}
        if title:
            payload["title"] = title
        if body:
            payload["body"] = body
        if state:
            payload["state"] = state

        if not payload:
            return True

        try:
            async with httpx.AsyncClient() as client:
                response = await client.patch(
                    url,
                    headers=self._get_headers(),
                    json=payload,
                    timeout=30
                )

                return response.status_code == 200

        except Exception as e:
            logger.error("Failed to update PR", pr_number=pr_number, error=str(e))
            return False

    async def merge_pr(
        self,
        pr_number: int,
        commit_title: Optional[str] = None,
        commit_message: Optional[str] = None,
        merge_method: str = "squash"
    ) -> dict:
        """
        Merge a pull request.

        Args:
            pr_number: PR number
            commit_title: Custom merge commit title
            commit_message: Custom merge commit message
            merge_method: merge, squash, or rebase

        Returns:
            Dict with success status and details
        """
        if not self.repo:
            return {"success": False, "error": "No repository configured"}

        url = f"{self.api_base}/repos/{self.repo}/pulls/{pr_number}/merge"

        payload = {"merge_method": merge_method}
        if commit_title:
            payload["commit_title"] = commit_title
        if commit_message:
            payload["commit_message"] = commit_message

        try:
            async with httpx.AsyncClient() as client:
                response = await client.put(
                    url,
                    headers=self._get_headers(),
                    json=payload,
                    timeout=30
                )

                if response.status_code == 200:
                    data = response.json()
                    logger.info("PR merged", pr_number=pr_number, sha=data.get("sha"))
                    return {
                        "success": True,
                        "sha": data.get("sha"),
                        "message": data.get("message")
                    }
                elif response.status_code == 405:
                    return {"success": False, "error": "PR cannot be merged (conflicts or status checks)"}
                elif response.status_code == 409:
                    return {"success": False, "error": "PR head has been modified"}
                else:
                    return {"success": False, "error": response.text}

        except Exception as e:
            logger.error("Failed to merge PR", pr_number=pr_number, error=str(e))
            return {"success": False, "error": str(e)}

    async def close_pr(self, pr_number: int) -> bool:
        """Close a pull request."""
        return await self.update_pr(pr_number, state="closed")

    async def list_prs(
        self,
        state: str = "open",
        head: Optional[str] = None,
        base: Optional[str] = None,
        sort: str = "created",
        direction: str = "desc",
        per_page: int = 30
    ) -> list[PullRequest]:
        """List pull requests."""
        if not self.repo:
            return []

        url = f"{self.api_base}/repos/{self.repo}/pulls"

        params = {
            "state": state,
            "sort": sort,
            "direction": direction,
            "per_page": per_page
        }
        if head:
            params["head"] = head
        if base:
            params["base"] = base

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers=self._get_headers(),
                    params=params,
                    timeout=30
                )

                if response.status_code == 200:
                    return [self._parse_pr(pr) for pr in response.json()]
                return []

        except Exception as e:
            logger.error("Failed to list PRs", error=str(e))
            return []

    async def add_comment(self, pr_number: int, body: str) -> bool:
        """Add a comment to a PR."""
        if not self.repo:
            return False

        url = f"{self.api_base}/repos/{self.repo}/issues/{pr_number}/comments"

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    headers=self._get_headers(),
                    json={"body": body},
                    timeout=30
                )

                return response.status_code == 201

        except Exception as e:
            logger.error("Failed to add comment", pr_number=pr_number, error=str(e))
            return False

    async def request_review(self, pr_number: int, reviewers: list[str]) -> bool:
        """Request review from users."""
        if not self.repo:
            return False

        url = f"{self.api_base}/repos/{self.repo}/pulls/{pr_number}/requested_reviewers"

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    headers=self._get_headers(),
                    json={"reviewers": reviewers},
                    timeout=30
                )

                return response.status_code == 201

        except Exception as e:
            logger.error("Failed to request review", pr_number=pr_number, error=str(e))
            return False

    async def get_pr_files(self, pr_number: int) -> list[dict]:
        """Get list of files changed in a PR."""
        if not self.repo:
            return []

        url = f"{self.api_base}/repos/{self.repo}/pulls/{pr_number}/files"

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers=self._get_headers(),
                    timeout=30
                )

                if response.status_code == 200:
                    return [
                        {
                            "filename": f["filename"],
                            "status": f["status"],
                            "additions": f["additions"],
                            "deletions": f["deletions"],
                            "changes": f["changes"]
                        }
                        for f in response.json()
                    ]
                return []

        except Exception as e:
            logger.error("Failed to get PR files", pr_number=pr_number, error=str(e))
            return []

    async def get_pr_checks(self, pr_number: int) -> dict:
        """Get status checks for a PR."""
        if not self.repo:
            return {"success": False, "checks": []}

        # First get the PR to get the head SHA
        pr = await self.get_pr(pr_number)
        if not pr:
            return {"success": False, "checks": []}

        url = f"{self.api_base}/repos/{self.repo}/commits/{pr.head_branch}/check-runs"

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers=self._get_headers(),
                    timeout=30
                )

                if response.status_code == 200:
                    data = response.json()
                    checks = [
                        {
                            "name": check["name"],
                            "status": check["status"],
                            "conclusion": check.get("conclusion")
                        }
                        for check in data.get("check_runs", [])
                    ]

                    all_passed = all(
                        c["conclusion"] == "success"
                        for c in checks
                        if c["status"] == "completed"
                    )

                    return {
                        "success": True,
                        "all_passed": all_passed,
                        "checks": checks
                    }

                return {"success": False, "checks": []}

        except Exception as e:
            logger.error("Failed to get PR checks", pr_number=pr_number, error=str(e))
            return {"success": False, "checks": []}

    def _parse_pr(self, data: dict) -> PullRequest:
        """Parse PR data from API response."""
        return PullRequest(
            number=data["number"],
            title=data["title"],
            body=data.get("body", ""),
            url=data["url"],
            html_url=data["html_url"],
            state=data["state"],
            head_branch=data["head"]["ref"],
            base_branch=data["base"]["ref"],
            created_at=datetime.fromisoformat(data["created_at"].replace("Z", "+00:00")),
            updated_at=datetime.fromisoformat(data["updated_at"].replace("Z", "+00:00")),
            mergeable=data.get("mergeable"),
            merged=data.get("merged", False),
            draft=data.get("draft", False)
        )


async def create_pr_for_task(
    task_id: str,
    task_description: str,
    branch_name: str,
    changes_summary: Optional[str] = None
) -> Optional[PullRequest]:
    """
    Convenience function to create a PR for a task.

    Args:
        task_id: Task identifier
        task_description: Task description for PR title
        branch_name: Source branch
        changes_summary: Summary of changes for PR body

    Returns:
        Created PullRequest or None
    """
    handler = PRHandler()

    # Generate PR title (truncate if too long)
    title = task_description[:100]
    if len(task_description) > 100:
        title = title[:97] + "..."

    # Generate PR body
    body = f"""## Task
{task_description}

## Changes
{changes_summary or "See commit history for details."}

---
*Created by Telegram Development Orchestrator*
*Task ID: {task_id}*
"""

    return await handler.create_pr(
        title=title,
        head_branch=branch_name,
        body=body
    )
