"""SirisOS (github.com/bradmairs/SirisOS), the home screen, seen from
SirisAI: the engineering library it owns -- standards, calculators,
projects, SirisHydro -- the state of every app it monitors, and Brad's
career record (CPD imported from Engineers Australia, the pathways to
Chartered and registration, competency evidence, goals).

Authenticates with SirisOS's service key (`Authorization: Bearer
<SIRISOS_SERVICE_KEY>`, matched by SIRISAI_SIRISOS_SERVICE_KEY here), which
SirisOS accepts only on these read and compute routes -- not for anything
that changes its data."""

from __future__ import annotations

from typing import Any

import httpx


class SirisOSError(RuntimeError):
    pass


class SirisOSClient:
    def __init__(self, base_url: str, service_key: str, timeout: float = 20.0, transport: httpx.BaseTransport | None = None) -> None:
        headers = {"Authorization": f"Bearer {service_key}", "X-Siris-Client": "SirisAI"} if service_key else {"X-Siris-Client": "SirisAI"}
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, headers=headers, transport=transport)

    def _get(self, path: str, timeout: float | None = None, **params: Any) -> Any:
        return self._handle(self._client.get(path, params={k: v for k, v in params.items() if v is not None}, timeout=timeout or self._client.timeout))

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        return self._handle(self._client.post(path, json=body))

    @staticmethod
    def _handle(response: httpx.Response) -> Any:
        if response.status_code in (401, 403):
            raise SirisOSError("SirisOS refused the service key (check SIRISOS_SERVICE_KEY there and SIRISAI_SIRISOS_SERVICE_KEY here).")
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = response.text[:200]
            raise SirisOSError(f"SirisOS: HTTP {response.status_code}: {detail}")
        return response.json()

    def apps(self) -> list[dict[str, Any]]:
        return self._get("/api/v1/hub/apps").get("apps") or []

    def search_standards(self, query: str, authority: str | None = None, limit: int = 8) -> dict[str, Any]:
        return self._get("/api/v1/engineering/standards", query=query, authority=authority, limit=limit)

    def calculators(self) -> list[dict[str, Any]]:
        return self._get("/api/v1/engineering/calculators").get("calculators") or []

    def calculate(self, calculator_id: str, inputs: dict[str, float]) -> dict[str, Any]:
        return self._post(f"/api/v1/engineering/calculators/{calculator_id}/run", {"inputs": inputs})

    def projects(self) -> list[dict[str, Any]]:
        return self._get("/api/v1/projects").get("projects") or []

    def career(self) -> dict[str, Any]:
        """The Career tab's record and overview (SirisOS ADR 111)."""
        return self._get("/api/v1/career")

    def sirishydro(self, question: str, limit: int = 6) -> dict[str, Any]:
        # Synthesis can take a while: SirisOS asks a model (SirisAI's, via the hub).
        return self._get("/api/v1/engineering/sirishydro/evidence", timeout=120.0, question=question, limit=limit)

    def gym_sessions(self, month: str | None = None) -> list[dict[str, Any]]:
        """SirisGym's workouts for one month (YYYY-MM), newest first: summaries."""
        return self._get("/api/v1/gym/sessions", month=month).get("sessions") or []

    def gym_session(self, session_id: str) -> dict[str, Any]:
        """One workout in full: every exercise and set, records and notes. SirisAI has the
        Watch's heart rate itself, so SirisOS needn't look the Watch workout up."""
        return self._get(f"/api/v1/gym/sessions/{session_id}", watch="false")
