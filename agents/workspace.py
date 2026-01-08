"""Workspace management for agent isolation."""

import asyncio
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional
import uuid

from git import Repo, GitCommandError
import structlog

from config import get_settings

logger = structlog.get_logger()


class WorkspaceManager:
    """Manages isolated workspaces for agents."""

    def __init__(self, base_path: Path, repo_url: Optional[str] = None):
        self.base_path = Path(base_path)
        self.repo_url = repo_url
        self.base_path.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    async def create_workspace(
        self,
        agent_id: str,
        branch_name: Optional[str] = None,
        clone_from: Optional[str] = None
    ) -> Path:
        """
        Create or reset an agent's workspace.

        Args:
            agent_id: The agent identifier
            branch_name: Branch to checkout (creates if not exists)
            clone_from: Repository URL to clone from (uses default if not provided)

        Returns:
            Path to the workspace
        """
        workspace_path = self.base_path / agent_id
        repo_url = clone_from or self.repo_url

        # Get or create lock for this workspace
        if agent_id not in self._locks:
            self._locks[agent_id] = asyncio.Lock()

        async with self._locks[agent_id]:
            try:
                if workspace_path.exists():
                    # Reset existing workspace
                    await self._reset_workspace(workspace_path, branch_name)
                elif repo_url:
                    # Clone new workspace
                    await self._clone_workspace(workspace_path, repo_url, branch_name)
                else:
                    # Create empty workspace
                    workspace_path.mkdir(parents=True, exist_ok=True)
                    await self._init_git(workspace_path)

                logger.info(
                    "Workspace prepared",
                    agent_id=agent_id,
                    path=str(workspace_path),
                    branch=branch_name
                )

                return workspace_path

            except Exception as e:
                logger.error(
                    "Failed to create workspace",
                    agent_id=agent_id,
                    error=str(e)
                )
                raise

    async def _clone_workspace(
        self,
        workspace_path: Path,
        repo_url: str,
        branch_name: Optional[str] = None
    ):
        """Clone a repository into the workspace."""
        loop = asyncio.get_event_loop()

        def _clone():
            if workspace_path.exists():
                shutil.rmtree(workspace_path)

            repo = Repo.clone_from(repo_url, workspace_path)

            if branch_name:
                # Create and checkout branch
                try:
                    repo.git.checkout(branch_name)
                except GitCommandError:
                    # Branch doesn't exist, create it
                    repo.git.checkout("-b", branch_name)

            return repo

        await loop.run_in_executor(None, _clone)

    async def _reset_workspace(
        self,
        workspace_path: Path,
        branch_name: Optional[str] = None
    ):
        """Reset an existing workspace to a clean state."""
        loop = asyncio.get_event_loop()

        def _reset():
            repo = Repo(workspace_path)

            # Fetch latest
            try:
                repo.remotes.origin.fetch()
            except Exception:
                pass  # May not have remote

            # Reset any changes
            repo.git.reset("--hard")
            repo.git.clean("-fdx")

            settings = get_settings()
            base_branch = settings.github_base_branch

            if branch_name:
                # Checkout the specified branch
                try:
                    repo.git.checkout(branch_name)
                except GitCommandError:
                    # Branch doesn't exist, create from base
                    repo.git.checkout(base_branch)
                    repo.git.checkout("-b", branch_name)
            else:
                # Checkout base branch
                repo.git.checkout(base_branch)
                try:
                    repo.git.pull("origin", base_branch)
                except Exception:
                    pass

            return repo

        await loop.run_in_executor(None, _reset)

    async def _init_git(self, workspace_path: Path):
        """Initialize a new git repository."""
        loop = asyncio.get_event_loop()

        def _init():
            repo = Repo.init(workspace_path)
            # Create initial commit
            readme = workspace_path / "README.md"
            readme.write_text("# Project\n\nInitialized by orchestrator.")
            repo.index.add(["README.md"])
            repo.index.commit("Initial commit")
            return repo

        await loop.run_in_executor(None, _init)

    async def create_branch(
        self,
        agent_id: str,
        branch_name: str,
        from_branch: Optional[str] = None
    ) -> bool:
        """Create a new branch in the workspace."""
        workspace_path = self.base_path / agent_id

        if not workspace_path.exists():
            return False

        loop = asyncio.get_event_loop()

        def _create_branch():
            repo = Repo(workspace_path)
            settings = get_settings()

            # Checkout base branch first
            base = from_branch or settings.github_base_branch
            try:
                repo.git.checkout(base)
                repo.git.pull("origin", base)
            except Exception:
                pass

            # Create and checkout new branch
            repo.git.checkout("-b", branch_name)
            return True

        try:
            return await loop.run_in_executor(None, _create_branch)
        except Exception as e:
            logger.error("Failed to create branch", error=str(e))
            return False

    async def get_branch(self, agent_id: str) -> Optional[str]:
        """Get the current branch for a workspace."""
        workspace_path = self.base_path / agent_id

        if not workspace_path.exists():
            return None

        try:
            repo = Repo(workspace_path)
            return repo.active_branch.name
        except Exception:
            return None

    async def get_changes(self, agent_id: str) -> dict:
        """Get uncommitted changes in the workspace."""
        workspace_path = self.base_path / agent_id

        if not workspace_path.exists():
            return {"staged": [], "unstaged": [], "untracked": []}

        try:
            repo = Repo(workspace_path)
            return {
                "staged": [item.a_path for item in repo.index.diff("HEAD")],
                "unstaged": [item.a_path for item in repo.index.diff(None)],
                "untracked": repo.untracked_files
            }
        except Exception as e:
            logger.error("Failed to get changes", error=str(e))
            return {"staged": [], "unstaged": [], "untracked": []}

    async def commit_changes(
        self,
        agent_id: str,
        message: str,
        files: Optional[list[str]] = None
    ) -> Optional[str]:
        """Commit changes in the workspace."""
        workspace_path = self.base_path / agent_id

        if not workspace_path.exists():
            return None

        loop = asyncio.get_event_loop()

        def _commit():
            repo = Repo(workspace_path)

            if files:
                repo.index.add(files)
            else:
                repo.git.add("-A")

            if not repo.index.diff("HEAD") and not repo.untracked_files:
                return None  # Nothing to commit

            commit = repo.index.commit(message)
            return commit.hexsha

        try:
            return await loop.run_in_executor(None, _commit)
        except Exception as e:
            logger.error("Failed to commit", error=str(e))
            return None

    async def push_branch(self, agent_id: str, branch_name: str) -> bool:
        """Push a branch to the remote."""
        workspace_path = self.base_path / agent_id

        if not workspace_path.exists():
            return False

        loop = asyncio.get_event_loop()

        def _push():
            repo = Repo(workspace_path)
            repo.git.push("-u", "origin", branch_name)
            return True

        try:
            return await loop.run_in_executor(None, _push)
        except Exception as e:
            logger.error("Failed to push", error=str(e))
            return False

    async def cleanup_workspace(self, agent_id: str):
        """Remove a workspace."""
        workspace_path = self.base_path / agent_id

        if workspace_path.exists():
            shutil.rmtree(workspace_path)
            logger.info("Workspace cleaned up", agent_id=agent_id)

    async def get_recent_commits(
        self,
        agent_id: str,
        count: int = 10
    ) -> list[dict]:
        """Get recent commits in the workspace."""
        workspace_path = self.base_path / agent_id

        if not workspace_path.exists():
            return []

        try:
            repo = Repo(workspace_path)
            commits = []

            for commit in repo.iter_commits(max_count=count):
                commits.append({
                    "sha": commit.hexsha,
                    "short_sha": commit.hexsha[:8],
                    "message": commit.message.strip(),
                    "author": str(commit.author),
                    "timestamp": commit.committed_datetime.isoformat(),
                    "files_changed": len(commit.stats.files)
                })

            return commits
        except Exception as e:
            logger.error("Failed to get commits", error=str(e))
            return []


def generate_branch_name(task_description: str, prefix: str = "task") -> str:
    """Generate a branch name from a task description."""
    # Clean up description
    clean = task_description.lower()
    clean = "".join(c if c.isalnum() or c.isspace() else " " for c in clean)
    words = clean.split()[:5]  # First 5 words
    slug = "-".join(words)

    # Add unique suffix
    suffix = uuid.uuid4().hex[:6]

    return f"{prefix}/{slug}-{suffix}"
