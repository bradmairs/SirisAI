"""Thin client for Synology DSM REST API (Web API).

Uses SYNO.API.Auth for session-based authentication and SYNO.Core.System,
SYNO.Core.Storage for system status and disk/volume/backup task health.

Synology DSM Web API reference: https://global.download.synology.com/download/Document/DeveloperGuide/Synology_File_Station_API_Guide.pdf
and https://github.com/SynologyOpenSource/API-Docs
"""

import httpx


class SynologyClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 10.0,
        verify_ssl: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._sid: str | None = None
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            headers={"Accept": "application/json"},
            verify=verify_ssl,
            transport=transport,
        )

    def _api_url(self, path: str) -> str:
        return f"/webapi/{path}"

    def _request(
        self, http_method: str, api: str, version: int, params: dict | None = None, **kwargs
    ) -> httpx.Response:
        """Make a request to a Synology API endpoint."""
        if params is None:
            params = {}
        params.setdefault("api", api)
        params.setdefault("version", version)
        params.setdefault("method", kwargs.pop("api_method", "get"))
        if self._sid:
            params["_sid"] = self._sid
        url = self._api_url("entry.cgi")
        response = self._client.request(http_method, url, params=params, **kwargs)
        response.raise_for_status()
        return response

    def login(self) -> None:
        """Authenticate and obtain a session ID (SID)."""
        response = self._request(
            "GET",
            api="SYNO.API.Auth",
            version=6,
            api_method="login",
            params={
                "account": self._username,
                "passwd": self._password,
                "session": "FileStation",
                "format": "sid",
            },
        )
        data = response.json()
        if not data.get("success"):
            raise RuntimeError(f"Synology login failed: {data}")
        self._sid = data["data"]["sid"]

    def logout(self) -> None:
        """Invalidate the current session."""
        if self._sid:
            self._request(
                "GET",
                api="SYNO.API.Auth",
                version=6,
                api_method="logout",
                params={"session": "FileStation"},
            )
            self._sid = None

    def get_system_status(self) -> dict:
        """Get system status via SYNO.Core.System (model, serial, temperature, fan, etc.)."""
        response = self._request(
            "GET",
            api="SYNO.Core.System",
            version=1,
            api_method="get",
        )
        data = response.json()
        if not data.get("success"):
            raise RuntimeError(f"SYNO.Core.System failed: {data}")
        return data["data"]

    def get_storage_info(self) -> dict:
        """Get storage/volume info via SYNO.Core.Storage (volumes, disks, usage)."""
        response = self._request(
            "GET",
            api="SYNO.Core.Storage",
            version=1,
            api_method="get",
        )
        data = response.json()
        if not data.get("success"):
            raise RuntimeError(f"SYNO.Core.Storage failed: {data}")
        return data["data"]

    def get_disk_smart_info(self) -> list[dict]:
        """Get SMART info for all disks via SYNO.Core.Storage (disk smart)."""
        response = self._request(
            "GET",
            api="SYNO.Core.Storage",
            version=1,
            api_method="disk_smart_info_get",
        )
        data = response.json()
        if not data.get("success"):
            raise RuntimeError(f"SYNO.Core.Storage disk_smart_info_get failed: {data}")
        return data["data"].get("disks", [])

    def get_backup_tasks(self) -> list[dict]:
        """Get Hyper Backup task status via SYNO.Core.Backup (or SYNO.Backup.Task)."""
        # Try SYNO.Backup.Task first (newer DSM), fallback to SYNO.Core.Backup
        for api in ["SYNO.Backup.Task", "SYNO.Core.Backup"]:
            try:
                response = self._request(
                    "GET",
                    api=api,
                    version=1,
                    api_method="list",
                )
                data = response.json()
                if data.get("success"):
                    return data["data"].get("tasks", [])
            except Exception:
                continue
        return []

    def close(self) -> None:
        """Close the underlying HTTP client."""
        try:
            self.logout()
        except Exception:
            pass
        self._client.close()

    def __enter__(self) -> "SynologyClient":
        self.login()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()