"""Task queue management with Redis backend."""

import asyncio
import json
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, asdict
from enum import Enum

import redis.asyncio as redis
import structlog

from config import get_settings
from models import TaskPriority

logger = structlog.get_logger()


@dataclass
class QueuedTask:
    """A task in the queue."""
    task_id: str
    priority: TaskPriority
    created_at: str
    dependencies: list[str]
    retries: int = 0
    max_retries: int = 3

    def to_dict(self) -> dict:
        data = asdict(self)
        data["priority"] = self.priority.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "QueuedTask":
        data["priority"] = TaskPriority(data["priority"])
        return cls(**data)


class TaskQueue:
    """Redis-backed task queue with priority support."""

    QUEUE_KEY = "orchestrator:task_queue"
    PROCESSING_KEY = "orchestrator:processing"
    COMPLETED_KEY = "orchestrator:completed"
    FAILED_KEY = "orchestrator:failed"
    TASK_DATA_PREFIX = "orchestrator:task:"
    LOCK_PREFIX = "orchestrator:lock:"

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._redis: Optional[redis.Redis] = None
        self._connected = False

    async def connect(self):
        """Connect to Redis."""
        if self._connected:
            return

        self._redis = redis.from_url(self.redis_url, decode_responses=True)
        try:
            await self._redis.ping()
            self._connected = True
            logger.info("Connected to Redis", url=self.redis_url)
        except Exception as e:
            logger.error("Failed to connect to Redis", error=str(e))
            raise

    async def disconnect(self):
        """Disconnect from Redis."""
        if self._redis:
            await self._redis.close()
            self._connected = False

    async def enqueue(self, task: QueuedTask) -> bool:
        """Add a task to the queue."""
        if not self._connected:
            await self.connect()

        try:
            # Store task data
            task_key = f"{self.TASK_DATA_PREFIX}{task.task_id}"
            await self._redis.set(task_key, json.dumps(task.to_dict()))

            # Calculate priority score (lower = higher priority)
            # URGENT=0, HIGH=1, NORMAL=2, LOW=3
            priority_scores = {
                TaskPriority.URGENT: 0,
                TaskPriority.HIGH: 1,
                TaskPriority.NORMAL: 2,
                TaskPriority.LOW: 3
            }
            score = priority_scores.get(task.priority, 2)

            # Add timestamp component for FIFO within same priority
            timestamp = datetime.utcnow().timestamp()
            final_score = score * 1e12 + timestamp

            # Add to sorted set
            await self._redis.zadd(self.QUEUE_KEY, {task.task_id: final_score})

            logger.info("Task enqueued", task_id=task.task_id, priority=task.priority.value)
            return True

        except Exception as e:
            logger.error("Failed to enqueue task", error=str(e))
            return False

    async def dequeue(self, agent_id: str) -> Optional[QueuedTask]:
        """Get the next task from the queue."""
        if not self._connected:
            await self.connect()

        try:
            # Get highest priority task (lowest score)
            results = await self._redis.zrange(self.QUEUE_KEY, 0, 0)
            if not results:
                return None

            task_id = results[0]

            # Check if task has unmet dependencies
            task_data = await self._get_task_data(task_id)
            if task_data and task_data.dependencies:
                if not await self._check_dependencies(task_data.dependencies):
                    # Skip this task, try next
                    # Move to end of same priority
                    score = await self._redis.zscore(self.QUEUE_KEY, task_id)
                    await self._redis.zadd(self.QUEUE_KEY, {task_id: score + 0.001})
                    return None

            # Atomically move from queue to processing
            async with self._redis.pipeline() as pipe:
                pipe.zrem(self.QUEUE_KEY, task_id)
                pipe.hset(self.PROCESSING_KEY, task_id, agent_id)
                await pipe.execute()

            return task_data

        except Exception as e:
            logger.error("Failed to dequeue task", error=str(e))
            return None

    async def complete(self, task_id: str, success: bool = True) -> bool:
        """Mark a task as complete."""
        if not self._connected:
            await self.connect()

        try:
            # Remove from processing
            await self._redis.hdel(self.PROCESSING_KEY, task_id)

            # Add to appropriate set
            target_key = self.COMPLETED_KEY if success else self.FAILED_KEY
            await self._redis.sadd(target_key, task_id)

            # Clean up task data (keep for a while for reference)
            task_key = f"{self.TASK_DATA_PREFIX}{task_id}"
            await self._redis.expire(task_key, 86400)  # Keep for 24 hours

            logger.info("Task completed", task_id=task_id, success=success)
            return True

        except Exception as e:
            logger.error("Failed to complete task", error=str(e))
            return False

    async def requeue(self, task_id: str) -> bool:
        """Requeue a task for retry."""
        if not self._connected:
            await self.connect()

        try:
            task_data = await self._get_task_data(task_id)
            if not task_data:
                return False

            if task_data.retries >= task_data.max_retries:
                logger.warning("Task exceeded max retries", task_id=task_id)
                return await self.complete(task_id, success=False)

            # Increment retry count
            task_data.retries += 1
            task_key = f"{self.TASK_DATA_PREFIX}{task_id}"
            await self._redis.set(task_key, json.dumps(task_data.to_dict()))

            # Remove from processing
            await self._redis.hdel(self.PROCESSING_KEY, task_id)

            # Re-add to queue
            return await self.enqueue(task_data)

        except Exception as e:
            logger.error("Failed to requeue task", error=str(e))
            return False

    async def cancel(self, task_id: str) -> bool:
        """Cancel a queued task."""
        if not self._connected:
            await self.connect()

        try:
            # Remove from queue
            removed = await self._redis.zrem(self.QUEUE_KEY, task_id)

            # Remove from processing if there
            await self._redis.hdel(self.PROCESSING_KEY, task_id)

            # Clean up task data
            task_key = f"{self.TASK_DATA_PREFIX}{task_id}"
            await self._redis.delete(task_key)

            return removed > 0

        except Exception as e:
            logger.error("Failed to cancel task", error=str(e))
            return False

    async def get_queue_size(self) -> int:
        """Get the number of tasks in the queue."""
        if not self._connected:
            await self.connect()

        return await self._redis.zcard(self.QUEUE_KEY)

    async def get_processing_count(self) -> int:
        """Get the number of tasks being processed."""
        if not self._connected:
            await self.connect()

        return await self._redis.hlen(self.PROCESSING_KEY)

    async def get_queue_status(self) -> dict:
        """Get detailed queue status."""
        if not self._connected:
            await self.connect()

        return {
            "queued": await self._redis.zcard(self.QUEUE_KEY),
            "processing": await self._redis.hlen(self.PROCESSING_KEY),
            "completed": await self._redis.scard(self.COMPLETED_KEY),
            "failed": await self._redis.scard(self.FAILED_KEY)
        }

    async def get_processing_tasks(self) -> dict[str, str]:
        """Get all tasks currently being processed (task_id -> agent_id)."""
        if not self._connected:
            await self.connect()

        return await self._redis.hgetall(self.PROCESSING_KEY)

    async def acquire_lock(self, resource: str, agent_id: str, ttl: int = 300) -> bool:
        """Acquire a distributed lock on a resource."""
        if not self._connected:
            await self.connect()

        lock_key = f"{self.LOCK_PREFIX}{resource}"
        acquired = await self._redis.set(lock_key, agent_id, nx=True, ex=ttl)
        return bool(acquired)

    async def release_lock(self, resource: str, agent_id: str) -> bool:
        """Release a distributed lock."""
        if not self._connected:
            await self.connect()

        lock_key = f"{self.LOCK_PREFIX}{resource}"

        # Only release if we own the lock
        current = await self._redis.get(lock_key)
        if current == agent_id:
            await self._redis.delete(lock_key)
            return True
        return False

    async def extend_lock(self, resource: str, agent_id: str, ttl: int = 300) -> bool:
        """Extend a lock's TTL if we own it."""
        if not self._connected:
            await self.connect()

        lock_key = f"{self.LOCK_PREFIX}{resource}"
        current = await self._redis.get(lock_key)
        if current == agent_id:
            await self._redis.expire(lock_key, ttl)
            return True
        return False

    async def _get_task_data(self, task_id: str) -> Optional[QueuedTask]:
        """Get task data from Redis."""
        task_key = f"{self.TASK_DATA_PREFIX}{task_id}"
        data = await self._redis.get(task_key)
        if data:
            return QueuedTask.from_dict(json.loads(data))
        return None

    async def _check_dependencies(self, dependencies: list[str]) -> bool:
        """Check if all dependencies are completed."""
        for dep_id in dependencies:
            is_completed = await self._redis.sismember(self.COMPLETED_KEY, dep_id)
            if not is_completed:
                return False
        return True


class InMemoryTaskQueue:
    """In-memory task queue for development/testing without Redis."""

    def __init__(self):
        self._queue: list[QueuedTask] = []
        self._processing: dict[str, str] = {}
        self._completed: set[str] = set()
        self._failed: set[str] = set()
        self._task_data: dict[str, QueuedTask] = {}
        self._locks: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def connect(self):
        """No-op for in-memory queue."""
        pass

    async def disconnect(self):
        """No-op for in-memory queue."""
        pass

    async def enqueue(self, task: QueuedTask) -> bool:
        """Add a task to the queue."""
        async with self._lock:
            self._task_data[task.task_id] = task

            # Insert in priority order
            priority_order = {
                TaskPriority.URGENT: 0,
                TaskPriority.HIGH: 1,
                TaskPriority.NORMAL: 2,
                TaskPriority.LOW: 3
            }

            insert_idx = len(self._queue)
            for i, existing in enumerate(self._queue):
                if priority_order[task.priority] < priority_order[existing.priority]:
                    insert_idx = i
                    break

            self._queue.insert(insert_idx, task)
            return True

    async def dequeue(self, agent_id: str) -> Optional[QueuedTask]:
        """Get the next task from the queue."""
        async with self._lock:
            for i, task in enumerate(self._queue):
                # Check dependencies
                if task.dependencies:
                    all_complete = all(
                        dep in self._completed for dep in task.dependencies
                    )
                    if not all_complete:
                        continue

                # Found available task
                self._queue.pop(i)
                self._processing[task.task_id] = agent_id
                return task

            return None

    async def complete(self, task_id: str, success: bool = True) -> bool:
        """Mark a task as complete."""
        async with self._lock:
            if task_id in self._processing:
                del self._processing[task_id]

            if success:
                self._completed.add(task_id)
            else:
                self._failed.add(task_id)

            return True

    async def requeue(self, task_id: str) -> bool:
        """Requeue a task for retry."""
        async with self._lock:
            if task_id in self._processing:
                del self._processing[task_id]

            task_data = self._task_data.get(task_id)
            if not task_data:
                return False

            if task_data.retries >= task_data.max_retries:
                self._failed.add(task_id)
                return False

            task_data.retries += 1
            self._queue.append(task_data)
            return True

    async def cancel(self, task_id: str) -> bool:
        """Cancel a queued task."""
        async with self._lock:
            self._queue = [t for t in self._queue if t.task_id != task_id]
            if task_id in self._processing:
                del self._processing[task_id]
            if task_id in self._task_data:
                del self._task_data[task_id]
            return True

    async def get_queue_size(self) -> int:
        """Get the number of tasks in the queue."""
        return len(self._queue)

    async def get_processing_count(self) -> int:
        """Get the number of tasks being processed."""
        return len(self._processing)

    async def get_queue_status(self) -> dict:
        """Get detailed queue status."""
        return {
            "queued": len(self._queue),
            "processing": len(self._processing),
            "completed": len(self._completed),
            "failed": len(self._failed)
        }

    async def get_processing_tasks(self) -> dict[str, str]:
        """Get all tasks currently being processed."""
        return dict(self._processing)

    async def acquire_lock(self, resource: str, agent_id: str, ttl: int = 300) -> bool:
        """Acquire a lock on a resource."""
        async with self._lock:
            if resource in self._locks:
                return False
            self._locks[resource] = agent_id
            return True

    async def release_lock(self, resource: str, agent_id: str) -> bool:
        """Release a lock."""
        async with self._lock:
            if self._locks.get(resource) == agent_id:
                del self._locks[resource]
                return True
            return False

    async def extend_lock(self, resource: str, agent_id: str, ttl: int = 300) -> bool:
        """Extend a lock (no-op for in-memory, locks don't expire)."""
        return self._locks.get(resource) == agent_id
