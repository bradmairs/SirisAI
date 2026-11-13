"""Cron overview tools -- read and toggle crontab entries on other machines
on the network (a home Linux server, Raspberry Pis), via CronHostsClient
(app/cron/client.py). Registered only when SIRISAI_CRON_HOSTS is set.

Distinct from create_routine/list_routines/set_routine_enabled
(app/plugins/builtin/routines.py): those are SirisAI's own scheduled tool
runs, stored and executed by SirisAI itself. These tools don't create or
run anything -- they only look at (and flip on/off) cron jobs that already
exist independently on other machines.
"""

from typing import Any

from app.cron.client import CronHostsClient
from app.plugins.base import Tool, ToolPermission


class CronListJobsTool(Tool):
    name = "cron_list_jobs"
    description = (
        "List cron jobs on every configured server (SIRISAI_CRON_HOSTS), e.g. a home Linux server or "
        "a Raspberry Pi: per server its hostname, UTC offset and uptime, then each job with schedule, "
        "description (the schedule in words), command, run_as, source ('crontab' for the account's own, "
        "else the system file such as /etc/crontab or /etc/cron.d/x), enabled, next_run and last_run "
        "(server time, from the cron log when readable), editable, and line (the exact text to pass to "
        "cron_set_job_enabled, for editable jobs only). An unreachable server has reachable=false and error."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: CronHostsClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        from app.cron.client import overview

        return overview(self._client)


class CronSetJobEnabledTool(Tool):
    name = "cron_set_job_enabled"
    description = (
        "Enable or disable one cron job on a configured host, by commenting out (or uncommenting) "
        "its line in that host's own crontab. Use the exact host and line values from "
        "cron_list_jobs -- line identifies the job by its schedule+command text, not by position, "
        "since the crontab may have changed since it was listed."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "host": {"type": "string", "description": "The host, e.g. 'brad@192.168.0.127', from cron_list_jobs."},
            "line": {"type": "string", "description": "The job's exact 'line' value from cron_list_jobs."},
            "enabled": {"type": "boolean", "description": "True to enable (uncomment), false to disable (comment out)."},
        },
        "required": ["host", "line", "enabled"],
    }

    def __init__(self, client: CronHostsClient) -> None:
        self._client = client

    def run(self, host: str = "", line: str = "", enabled: bool = True, **kwargs: Any) -> Any:
        return self._client.set_job_enabled(host, line, enabled)


class CronEditJobTool(Tool):
    """6.38: add, change or delete a job in a server's own crontab."""

    name = "cron_edit_job"
    description = (
        "Add, change or delete a cron job in a configured server's own crontab (not the system files). "
        "action 'add' needs host, schedule (five fields like '0 3 * * *', or @daily, @reboot) and command; "
        "'change' needs host and line (from cron_list_jobs) plus the new schedule and/or command; 'delete' needs "
        "host and line. Always asks first. Returns the schedule in words and when it next runs."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "change", "delete"]},
            "host": {"type": "string", "description": "The server, e.g. 'brad@192.168.0.127', from cron_list_jobs."},
            "line": {"type": "string", "description": "For change/delete: the job's exact 'line' from cron_list_jobs."},
            "schedule": {"type": "string", "description": "Five-field cron schedule or @daily/@hourly/@weekly/@reboot."},
            "command": {"type": "string", "description": "The command to run, one line."},
            "comment": {"type": "string", "description": "For add: an optional comment line above it."},
        },
        "required": ["action", "host"],
    }

    def __init__(self, client: CronHostsClient) -> None:
        self._client = client

    def run(self, action: str = "add", host: str = "", line: str = "", schedule: str = "", command: str = "",
            comment: str = "", **kwargs: Any) -> Any:
        from datetime import UTC, datetime

        from app.cron.schedule import describe, next_run

        if action == "add":
            if not schedule or not command:
                raise ValueError("Adding a job needs a schedule and a command.")
            out = self._client.add_job(host, schedule, command, comment)
        elif action == "change":
            if not line or not (schedule or command):
                raise ValueError("Changing a job needs its line and a new schedule or command.")
            out = self._client.change_job(host, line, schedule or None, command or None)
        elif action == "delete":
            if not line:
                raise ValueError("Deleting a job needs its line.")
            return {**self._client.delete_job(host, line), "summary": f"Deleted it from {host}'s crontab."}
        else:
            raise ValueError("action is add, change or delete.")
        sched = out["line"].split()[0] if out["line"].startswith("@") else " ".join(out["line"].split()[:5])
        upcoming = next_run(sched, datetime.now(UTC))
        words = describe(sched)
        return {**out, "description": words, "next_run_utc": upcoming.isoformat() if upcoming else None,
                "summary": f"{'Added' if action == 'add' else 'Changed'} on {host}: {words.lower()}."}


class CronWatchJobTool(Tool):
    """6.38: log a job's exit code so a failure is noticed."""

    name = "cron_watch_job"
    description = (
        "Watch (or stop watching) whether a cron job in a server's own crontab succeeds: SirisAI wraps the command so "
        "its exit code goes to the system log, and the attention feed says when a run fails. Use the host and line from "
        "cron_list_jobs. Asks first, since it rewrites the crontab line."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "host": {"type": "string"},
            "line": {"type": "string", "description": "The job's exact 'line' from cron_list_jobs."},
            "watch": {"type": "boolean", "description": "True to watch, false to stop."},
        },
        "required": ["host", "line"],
    }

    def __init__(self, client: CronHostsClient) -> None:
        self._client = client

    def run(self, host: str = "", line: str = "", watch: bool = True, **kwargs: Any) -> Any:
        return self._client.set_watched(host, line, watch)


CRON_TOOLS = (CronListJobsTool, CronSetJobEnabledTool, CronEditJobTool, CronWatchJobTool)
