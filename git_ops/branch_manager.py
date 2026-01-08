"""Git branch management for the orchestrator."""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional
import re

from git import Repo, GitCommandError
import structlog

from config import get_settings

logger = structlog.get_logger()


class BranchManager:
    """Manages git branches for task isolation."""

    def __init__(self, repo_path: Path):
        self.repo_path = Path(repo_path)
        self.settings = get_settings()

    def _get_repo(self) -> Repo:
        """Get the git repository."""
        return Repo(self.repo_path)

    async def create_task_branch(
        self,
        task_id: str,
        task_description: str,
        from_branch: Optional[str] = None
    ) -> str:
        """
        Create a new branch for a task.

        Args:
            task_id: Task identifier
            task_description: Task description for branch name
            from_branch: Base branch to create from

        Returns:
            Created branch name
        """
        loop = asyncio.get_event_loop()

        def _create():
            repo = self._get_repo()
            base = from_branch or self.settings.github_base_branch

            # Generate branch name
            branch_name = self._generate_branch_name(task_id, task_description)

            # Fetch latest
            try:
                repo.remotes.origin.fetch()
            except Exception:
                pass

            # Checkout base and pull
            repo.git.checkout(base)
            try:
                repo.git.pull("origin", base)
            except Exception:
                pass

            # Create new branch
            repo.git.checkout("-b", branch_name)

            logger.info(
                "Task branch created",
                branch=branch_name,
                base=base,
                task_id=task_id
            )

            return branch_name

        return await loop.run_in_executor(None, _create)

    async def switch_branch(self, branch_name: str) -> bool:
        """Switch to a branch."""
        loop = asyncio.get_event_loop()

        def _switch():
            repo = self._get_repo()

            # Stash any changes
            try:
                repo.git.stash()
            except Exception:
                pass

            repo.git.checkout(branch_name)
            return True

        try:
            return await loop.run_in_executor(None, _switch)
        except Exception as e:
            logger.error("Failed to switch branch", branch=branch_name, error=str(e))
            return False

    async def get_current_branch(self) -> str:
        """Get the current branch name."""
        repo = self._get_repo()
        return repo.active_branch.name

    async def delete_branch(self, branch_name: str, force: bool = False) -> bool:
        """Delete a branch."""
        loop = asyncio.get_event_loop()

        def _delete():
            repo = self._get_repo()
            base = self.settings.github_base_branch

            # Can't delete current branch
            if repo.active_branch.name == branch_name:
                repo.git.checkout(base)

            # Delete local branch
            if force:
                repo.git.branch("-D", branch_name)
            else:
                repo.git.branch("-d", branch_name)

            # Try to delete remote branch
            try:
                repo.git.push("origin", "--delete", branch_name)
            except Exception:
                pass

            return True

        try:
            return await loop.run_in_executor(None, _delete)
        except Exception as e:
            logger.error("Failed to delete branch", branch=branch_name, error=str(e))
            return False

    async def merge_branch(
        self,
        source_branch: str,
        target_branch: Optional[str] = None,
        delete_after: bool = True
    ) -> dict:
        """
        Merge a branch into target.

        Args:
            source_branch: Branch to merge
            target_branch: Target branch (default: base branch)
            delete_after: Delete source branch after merge

        Returns:
            Dict with success status and details
        """
        loop = asyncio.get_event_loop()
        target = target_branch or self.settings.github_base_branch

        def _merge():
            repo = self._get_repo()

            # Checkout target
            repo.git.checkout(target)
            try:
                repo.git.pull("origin", target)
            except Exception:
                pass

            # Merge
            try:
                repo.git.merge(source_branch, "--no-ff", "-m", f"Merge branch '{source_branch}' into {target}")
                merge_success = True
                conflict = False
            except GitCommandError as e:
                if "CONFLICT" in str(e):
                    merge_success = False
                    conflict = True
                else:
                    raise

            if merge_success and delete_after:
                try:
                    repo.git.branch("-d", source_branch)
                except Exception:
                    pass

            return {
                "success": merge_success,
                "conflict": conflict,
                "source": source_branch,
                "target": target
            }

        try:
            return await loop.run_in_executor(None, _merge)
        except Exception as e:
            logger.error("Merge failed", source=source_branch, target=target, error=str(e))
            return {
                "success": False,
                "error": str(e),
                "source": source_branch,
                "target": target
            }

    async def rebase_branch(self, branch_name: str, onto: Optional[str] = None) -> bool:
        """Rebase a branch onto another."""
        loop = asyncio.get_event_loop()
        onto_branch = onto or self.settings.github_base_branch

        def _rebase():
            repo = self._get_repo()

            # Fetch latest
            try:
                repo.remotes.origin.fetch()
            except Exception:
                pass

            # Checkout branch
            repo.git.checkout(branch_name)

            # Rebase
            repo.git.rebase(f"origin/{onto_branch}")
            return True

        try:
            return await loop.run_in_executor(None, _rebase)
        except Exception as e:
            logger.error("Rebase failed", branch=branch_name, onto=onto_branch, error=str(e))
            # Abort rebase on failure
            try:
                repo = self._get_repo()
                repo.git.rebase("--abort")
            except Exception:
                pass
            return False

    async def list_branches(self, remote: bool = False) -> list[str]:
        """List all branches."""
        repo = self._get_repo()

        if remote:
            try:
                repo.remotes.origin.fetch()
            except Exception:
                pass
            return [ref.name.replace("origin/", "") for ref in repo.remotes.origin.refs]
        else:
            return [branch.name for branch in repo.branches]

    async def get_branch_diff(
        self,
        branch_name: str,
        base_branch: Optional[str] = None
    ) -> dict:
        """Get diff statistics for a branch."""
        repo = self._get_repo()
        base = base_branch or self.settings.github_base_branch

        try:
            diff_output = repo.git.diff(f"{base}...{branch_name}", "--stat")

            # Parse stats
            files_changed = len(diff_output.strip().split("\n")) - 1 if diff_output else 0
            insertions = 0
            deletions = 0

            for line in diff_output.split("\n"):
                ins_match = re.search(r"(\d+) insertion", line)
                del_match = re.search(r"(\d+) deletion", line)
                if ins_match:
                    insertions += int(ins_match.group(1))
                if del_match:
                    deletions += int(del_match.group(1))

            # Get commit count
            commits = list(repo.iter_commits(f"{base}..{branch_name}"))

            return {
                "files_changed": files_changed,
                "insertions": insertions,
                "deletions": deletions,
                "commits": len(commits)
            }
        except Exception as e:
            logger.error("Failed to get branch diff", branch=branch_name, error=str(e))
            return {
                "files_changed": 0,
                "insertions": 0,
                "deletions": 0,
                "commits": 0
            }

    async def has_conflicts(self, branch_name: str, target: Optional[str] = None) -> bool:
        """Check if merging would cause conflicts."""
        loop = asyncio.get_event_loop()
        target_branch = target or self.settings.github_base_branch

        def _check():
            repo = self._get_repo()
            current = repo.active_branch.name

            try:
                # Try dry-run merge
                repo.git.checkout(target_branch)
                repo.git.merge("--no-commit", "--no-ff", branch_name)
                # If we get here, no conflicts
                repo.git.merge("--abort")
                has_conflict = False
            except GitCommandError:
                try:
                    repo.git.merge("--abort")
                except Exception:
                    pass
                has_conflict = True
            finally:
                repo.git.checkout(current)

            return has_conflict

        try:
            return await loop.run_in_executor(None, _check)
        except Exception as e:
            logger.error("Failed to check conflicts", error=str(e))
            return True  # Assume conflicts on error

    def _generate_branch_name(self, task_id: str, description: str) -> str:
        """Generate a branch name from task info."""
        # Clean description
        clean = description.lower()
        clean = re.sub(r"[^a-z0-9\s]", "", clean)
        words = clean.split()[:4]
        slug = "-".join(words) if words else "task"

        # Add task ID suffix
        short_id = task_id[:6]

        return f"task/{slug}-{short_id}"
