"""Claude Code CLI wrapper for agent execution."""

import asyncio
import json
import os
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, Any
import uuid

import structlog

from config import get_settings

logger = structlog.get_logger()


class ClaudeCodeProcess:
    """Represents a running Claude Code process."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        task_id: str,
        workspace_path: Path,
        output_callback: Optional[Callable[[str], Any]] = None
    ):
        self.process = process
        self.task_id = task_id
        self.workspace_path = workspace_path
        self.output_callback = output_callback
        self.output_lines: list[str] = []
        self.error_lines: list[str] = []
        self.started_at = datetime.utcnow()
        self.completed_at: Optional[datetime] = None
        self.exit_code: Optional[int] = None
        self._output_task: Optional[asyncio.Task] = None
        self._error_task: Optional[asyncio.Task] = None

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def is_running(self) -> bool:
        return self.process.returncode is None

    async def start_monitoring(self):
        """Start monitoring stdout and stderr."""
        self._output_task = asyncio.create_task(self._read_output())
        self._error_task = asyncio.create_task(self._read_errors())

    async def _read_output(self):
        """Read stdout continuously."""
        while True:
            if self.process.stdout is None:
                break
            try:
                line = await self.process.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                self.output_lines.append(decoded)
                if self.output_callback:
                    await self._safe_callback(decoded)
            except Exception as e:
                logger.error("Error reading stdout", error=str(e))
                break

    async def _read_errors(self):
        """Read stderr continuously."""
        while True:
            if self.process.stderr is None:
                break
            try:
                line = await self.process.stderr.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                self.error_lines.append(decoded)
            except Exception as e:
                logger.error("Error reading stderr", error=str(e))
                break

    async def _safe_callback(self, line: str):
        """Safely invoke the output callback."""
        try:
            result = self.output_callback(line)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.error("Output callback error", error=str(e))

    async def wait(self, timeout: Optional[float] = None) -> int:
        """Wait for the process to complete."""
        try:
            await asyncio.wait_for(self.process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Process timeout", task_id=self.task_id, timeout=timeout)
            await self.terminate()
            raise

        self.exit_code = self.process.returncode
        self.completed_at = datetime.utcnow()

        # Wait for output tasks to complete
        if self._output_task:
            try:
                await asyncio.wait_for(self._output_task, timeout=5)
            except asyncio.TimeoutError:
                self._output_task.cancel()
        if self._error_task:
            try:
                await asyncio.wait_for(self._error_task, timeout=5)
            except asyncio.TimeoutError:
                self._error_task.cancel()

        return self.exit_code

    async def terminate(self):
        """Terminate the process gracefully."""
        if self.is_running:
            try:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=10)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            except ProcessLookupError:
                pass

        self.completed_at = datetime.utcnow()
        self.exit_code = self.process.returncode

    def get_output(self) -> str:
        """Get all stdout output."""
        return "\n".join(self.output_lines)

    def get_errors(self) -> str:
        """Get all stderr output."""
        return "\n".join(self.error_lines)


class ClaudeCodeWrapper:
    """Wrapper for invoking Claude Code CLI."""

    def __init__(
        self,
        agent_id: str,
        workspace_path: Path,
        claude_code_path: str = "claude"
    ):
        self.agent_id = agent_id
        self.workspace_path = Path(workspace_path)
        self.claude_code_path = claude_code_path
        self.current_process: Optional[ClaudeCodeProcess] = None
        self.settings = get_settings()

    async def execute_task(
        self,
        task_id: str,
        prompt: str,
        context: Optional[str] = None,
        output_callback: Optional[Callable[[str], Any]] = None,
        timeout: Optional[float] = None
    ) -> dict:
        """
        Execute a task using Claude Code CLI.

        Args:
            task_id: Unique task identifier
            prompt: The task description/prompt
            context: Additional context to provide
            output_callback: Callback for real-time output
            timeout: Maximum execution time in seconds

        Returns:
            dict with execution results
        """
        if timeout is None:
            timeout = self.settings.agent_timeout_seconds

        # Ensure workspace exists
        self.workspace_path.mkdir(parents=True, exist_ok=True)

        # Build the prompt
        full_prompt = prompt
        if context:
            full_prompt = f"{context}\n\n{prompt}"

        # Build command
        cmd = [
            self.claude_code_path,
            "--print",  # Non-interactive mode
            "--output-format", "text",
            full_prompt
        ]

        logger.info(
            "Starting Claude Code execution",
            agent_id=self.agent_id,
            task_id=task_id,
            workspace=str(self.workspace_path)
        )

        try:
            # Start the process
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace_path),
                env={**os.environ, "CLAUDE_CODE_TELEMETRY": "0"}
            )

            self.current_process = ClaudeCodeProcess(
                process=process,
                task_id=task_id,
                workspace_path=self.workspace_path,
                output_callback=output_callback
            )

            # Start monitoring output
            await self.current_process.start_monitoring()

            # Wait for completion
            exit_code = await self.current_process.wait(timeout=timeout)

            result = {
                "success": exit_code == 0,
                "exit_code": exit_code,
                "output": self.current_process.get_output(),
                "errors": self.current_process.get_errors(),
                "duration_seconds": (
                    self.current_process.completed_at - self.current_process.started_at
                ).total_seconds() if self.current_process.completed_at else 0
            }

            logger.info(
                "Claude Code execution completed",
                agent_id=self.agent_id,
                task_id=task_id,
                success=result["success"],
                duration=result["duration_seconds"]
            )

            return result

        except asyncio.TimeoutError:
            logger.error(
                "Claude Code execution timeout",
                agent_id=self.agent_id,
                task_id=task_id,
                timeout=timeout
            )
            return {
                "success": False,
                "exit_code": -1,
                "output": self.current_process.get_output() if self.current_process else "",
                "errors": f"Execution timed out after {timeout} seconds",
                "duration_seconds": timeout
            }

        except Exception as e:
            logger.error(
                "Claude Code execution error",
                agent_id=self.agent_id,
                task_id=task_id,
                error=str(e)
            )
            return {
                "success": False,
                "exit_code": -1,
                "output": "",
                "errors": str(e),
                "duration_seconds": 0
            }

        finally:
            self.current_process = None

    async def execute_with_conversation(
        self,
        task_id: str,
        initial_prompt: str,
        follow_ups: list[str] = None,
        output_callback: Optional[Callable[[str], Any]] = None,
        timeout: Optional[float] = None
    ) -> dict:
        """
        Execute a multi-turn conversation with Claude Code.

        This uses the --continue flag for follow-up messages.
        """
        results = []

        # Execute initial prompt
        result = await self.execute_task(
            task_id=task_id,
            prompt=initial_prompt,
            output_callback=output_callback,
            timeout=timeout
        )
        results.append(result)

        if not result["success"] or not follow_ups:
            return result

        # Execute follow-ups
        for i, follow_up in enumerate(follow_ups):
            follow_up_id = f"{task_id}_followup_{i}"

            # Build command with --continue
            cmd = [
                self.claude_code_path,
                "--print",
                "--continue",  # Continue conversation
                "--output-format", "text",
                follow_up
            ]

            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self.workspace_path),
                    env={**os.environ, "CLAUDE_CODE_TELEMETRY": "0"}
                )

                follow_up_process = ClaudeCodeProcess(
                    process=process,
                    task_id=follow_up_id,
                    workspace_path=self.workspace_path,
                    output_callback=output_callback
                )

                await follow_up_process.start_monitoring()
                exit_code = await follow_up_process.wait(timeout=timeout)

                follow_up_result = {
                    "success": exit_code == 0,
                    "exit_code": exit_code,
                    "output": follow_up_process.get_output(),
                    "errors": follow_up_process.get_errors(),
                    "duration_seconds": (
                        follow_up_process.completed_at - follow_up_process.started_at
                    ).total_seconds() if follow_up_process.completed_at else 0
                }
                results.append(follow_up_result)

                if not follow_up_result["success"]:
                    break

            except Exception as e:
                logger.error("Follow-up execution error", error=str(e))
                results.append({
                    "success": False,
                    "exit_code": -1,
                    "output": "",
                    "errors": str(e),
                    "duration_seconds": 0
                })
                break

        # Combine results
        return {
            "success": all(r["success"] for r in results),
            "exit_code": results[-1]["exit_code"],
            "output": "\n---\n".join(r["output"] for r in results),
            "errors": "\n".join(r["errors"] for r in results if r["errors"]),
            "duration_seconds": sum(r["duration_seconds"] for r in results),
            "conversation_turns": len(results)
        }

    async def terminate_current(self):
        """Terminate the currently running process."""
        if self.current_process and self.current_process.is_running:
            await self.current_process.terminate()
            logger.info("Terminated Claude Code process", agent_id=self.agent_id)

    def get_current_output(self) -> Optional[str]:
        """Get output from the current running process."""
        if self.current_process:
            return self.current_process.get_output()
        return None

    async def health_check(self) -> bool:
        """Check if Claude Code CLI is available."""
        try:
            process = await asyncio.create_subprocess_exec(
                self.claude_code_path,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(process.wait(), timeout=10)
            return process.returncode == 0
        except Exception as e:
            logger.error("Health check failed", error=str(e))
            return False


class AgentPool:
    """Pool of Claude Code agent wrappers."""

    def __init__(self, workspace_base: Path, agent_count: int = 3):
        self.workspace_base = Path(workspace_base)
        self.agents: dict[str, ClaudeCodeWrapper] = {}
        self._initialize_agents(agent_count)

    def _initialize_agents(self, count: int):
        """Initialize the agent pool."""
        for i in range(count):
            agent_id = f"agent_{i+1}"
            workspace = self.workspace_base / agent_id
            workspace.mkdir(parents=True, exist_ok=True)

            self.agents[agent_id] = ClaudeCodeWrapper(
                agent_id=agent_id,
                workspace_path=workspace
            )

        logger.info("Agent pool initialized", count=count)

    def get_agent(self, agent_id: str) -> Optional[ClaudeCodeWrapper]:
        """Get an agent by ID."""
        return self.agents.get(agent_id)

    def get_available_agent(self) -> Optional[ClaudeCodeWrapper]:
        """Get an available (not currently executing) agent."""
        for agent in self.agents.values():
            if agent.current_process is None:
                return agent
        return None

    def get_all_agents(self) -> list[ClaudeCodeWrapper]:
        """Get all agents."""
        return list(self.agents.values())

    async def terminate_all(self):
        """Terminate all running agent processes."""
        for agent in self.agents.values():
            await agent.terminate_current()

    async def health_check_all(self) -> dict[str, bool]:
        """Check health of all agents."""
        results = {}
        for agent_id, agent in self.agents.items():
            results[agent_id] = await agent.health_check()
        return results
