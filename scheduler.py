"""Scheduler — reads cron tables from TOML, fires personas on schedule.

A while loop with time.sleep(60) and croniter. That's it.
No Celery. No APScheduler. No magic.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Any

from croniter import croniter

log = logging.getLogger("frank.scheduler")


@dataclass
class Task:
    name: str
    persona: str
    prompt: str
    cron: str
    enabled: bool = True
    last_run: datetime | None = None

    def is_due(self, now: datetime) -> bool:
        """True if this task should fire at `now` (within the current minute)."""
        if not self.enabled:
            return False
        # Check if any scheduled time fell in the last 60 seconds
        itr = croniter(self.cron, now)
        prev = itr.get_prev(datetime)
        if self.last_run is None:
            # On startup, only run if cron fired in the last 60s
            return (now - prev).total_seconds() < 60
        return prev > self.last_run


def load_tasks(personas_dir: Path) -> list[Task]:
    """Scan personas_dir for schedule.toml files. Return list of Tasks."""
    import sys
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib

    tasks = []
    personas_dir = Path(personas_dir)
    if not personas_dir.exists():
        return tasks

    for persona_dir in sorted(personas_dir.iterdir()):
        if not persona_dir.is_dir():
            continue
        schedule_file = persona_dir / "schedule.toml"
        if not schedule_file.exists():
            continue
        try:
            with open(schedule_file, "rb") as f:
                data = tomllib.load(f)
            for t in data.get("task", []):
                if not t.get("cron") or not t.get("prompt"):
                    continue
                tasks.append(Task(
                    name=t.get("name", f"{persona_dir.name}-task"),
                    persona=persona_dir.name,
                    prompt=t["prompt"],
                    cron=t["cron"],
                    enabled=t.get("enabled", True),
                ))
                log.info(f"Loaded task '{t.get('name')}' for {persona_dir.name} @ {t['cron']}")
        except Exception as e:
            log.error(f"Failed to load schedule for {persona_dir.name}: {e}")

    return tasks


class Scheduler:
    """
    Cron-based scheduler. Loads tasks from all persona schedule.toml files.
    Calls run_fn(persona_name, prompt) when a task fires.

    Backpressure: if a persona's task is already running when the next one
    fires, the new one is skipped and logged. Prevents pile-up when a
    research task runs long (CJ's weekly draft, Kato's news digest).
    """

    def __init__(
        self,
        personas_dir: Path | str,
        run_fn: Callable[[str, str], Any],
    ):
        self.personas_dir = Path(personas_dir)
        self.run_fn = run_fn
        self.tasks = load_tasks(self.personas_dir)
        self._running = False
        self._active: set[str] = set()  # task names currently running

    def reload_tasks(self) -> None:
        """Hot-reload task list from disk."""
        self.tasks = load_tasks(self.personas_dir)
        log.info(f"Reloaded {len(self.tasks)} tasks")

    async def run_forever(self, tick_seconds: int = 60) -> None:
        """Main scheduler loop. Checks for due tasks every tick_seconds."""
        self._running = True
        log.info(f"Scheduler started with {len(self.tasks)} tasks")

        while self._running:
            now = datetime.now()
            for task in self.tasks:
                if task.is_due(now):
                    if task.name in self._active:
                        log.warning(
                            f"Task '{task.name}' still running from previous fire — skipping. "
                            f"Consider increasing cron interval or reducing max_turns."
                        )
                        continue
                    log.info(f"Firing task '{task.name}' for persona '{task.persona}'")
                    task.last_run = now
                    asyncio.create_task(self._run_task(task))

            await asyncio.sleep(tick_seconds)

    async def _run_task(self, task: Task) -> None:
        """Run a single task with backpressure tracking."""
        self._active.add(task.name)
        try:
            if asyncio.iscoroutinefunction(self.run_fn):
                await self.run_fn(task.persona, task.prompt)
            else:
                await asyncio.to_thread(self.run_fn, task.persona, task.prompt)
            log.info(f"Task '{task.name}' completed")
        except Exception as e:
            log.error(f"Task '{task.name}' failed: {e}", exc_info=True)
        finally:
            self._active.discard(task.name)

    def stop(self) -> None:
        self._running = False

    def list_tasks(self) -> list[dict]:
        """Return task info for display."""
        return [
            {
                "name": t.name,
                "persona": t.persona,
                "cron": t.cron,
                "enabled": t.enabled,
                "last_run": t.last_run.isoformat() if t.last_run else None,
                "next_run": croniter(t.cron, datetime.now()).get_next(datetime).isoformat(),
            }
            for t in self.tasks
        ]
