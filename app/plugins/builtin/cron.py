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
        "List cron jobs from every configured host's own crontab (SIRISAI_CRON_HOSTS), e.g. a "
        "home Linux server or a Raspberry Pi. Each entry has host, schedule, command, line (the "
        "exact text to pass to cron_set_job_enabled), and enabled (false if it's commented out). "
        "A host that couldn't be reached shows up as {host, error} instead of being silently omitted."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: CronHostsClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.list_jobs()


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
