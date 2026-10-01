"""Parcel tracking: the deliveries you're waiting for, in the briefing and
pushed when one is out for delivery, delivered, or stuck.

With SIRISAI_SEVENTEENTRACK_API_KEY set, statuses come from 17TRACK's API
(v2.2; it detects the carrier from the number, ~2,000 carriers) -- numbers
are registered with 17TRACK when added and refreshed every
SIRISAI_PARCEL_REFRESH_MINUTES. Without a key it's a plain list you keep
yourself ("the headphones arrived" -> mark delivered).
"""

from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.models import Parcel

# 17TRACK's main statuses, in plain words.
STATUS_WORDS = {
    "NotFound": "not found yet",
    "InfoReceived": "label created",
    "InTransit": "in transit",
    "Expired": "tracking expired",
    "AvailableForPickup": "ready for pickup",
    "OutForDelivery": "out for delivery",
    "DeliveryFailure": "delivery failed",
    "Delivered": "delivered",
    "Exception": "has a problem",
    "Unknown": "status unknown",
}
NOTIFY_ON = {"OutForDelivery", "Delivered", "DeliveryFailure", "AvailableForPickup", "Exception"}


class SeventeenTrackClient:
    def __init__(self, api_key: str, base_url: str = "https://api.17track.net", timeout: float = 15.0, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, headers={"17token": api_key}, transport=transport)

    def register(self, number: str) -> None:
        response = self._client.post("/track/v2.2/register", json=[{"number": number}])
        response.raise_for_status()

    def track(self, numbers: list[str]) -> dict[str, dict[str, Any]]:
        response = self._client.post("/track/v2.2/gettrackinfo", json=[{"number": n} for n in numbers])
        response.raise_for_status()
        out = {}
        for item in (response.json().get("data") or {}).get("accepted") or []:
            info = item.get("track_info") or {}
            latest = (info.get("latest_status") or {}).get("status") or "Unknown"
            event = info.get("latest_event") or {}
            eta = ((info.get("time_metrics") or {}).get("estimated_delivery_date") or {}).get("from")
            out[item.get("number")] = {
                "status": latest,
                "last_event": " -- ".join(p for p in (event.get("time_iso"), event.get("description"), event.get("location")) if p) or None,
                "eta": eta,
                "carrier": str(item.get("carrier") or ""),
            }
        return out

    def close(self) -> None:
        self._client.close()


class ParcelStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def add(self, tracking_number: str, label: str = "", carrier: str = "") -> dict[str, Any]:
        with self._session_factory() as session:
            parcel = Parcel(tracking_number=tracking_number.strip().replace(" ", ""), label=label, carrier=carrier)
            session.add(parcel)
            session.commit()
            session.refresh(parcel)
            return self._serialize(parcel)

    def active(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(select(Parcel).where(Parcel.active.is_(True)).order_by(Parcel.id)).all()
            return [self._serialize(p) for p in rows]

    def update(self, parcel_id: int, **fields: Any) -> dict[str, Any] | None:
        with self._session_factory() as session:
            parcel = session.get(Parcel, parcel_id)
            if parcel is None:
                return None
            for key, value in fields.items():
                setattr(parcel, key, value)
            parcel.updated_at = datetime.now(UTC).replace(tzinfo=None)
            session.commit()
            return self._serialize(parcel)

    @staticmethod
    def _serialize(p: Parcel) -> dict[str, Any]:
        return {
            "id": p.id,
            "tracking_number": p.tracking_number,
            "label": p.label or p.tracking_number,
            "carrier": p.carrier,
            "status": p.status,
            "status_text": STATUS_WORDS.get(p.status, p.status),
            "last_event": p.last_event,
            "eta": p.eta,
            "active": p.active,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        }


def refresh(store: ParcelStore, tracker: SeventeenTrackClient | None) -> list[dict[str, Any]]:
    """Update active parcels from 17TRACK; returns the ones whose status
    changed to something worth telling you about."""
    parcels = store.active()
    if tracker is None or not parcels:
        return []
    results = tracker.track([p["tracking_number"] for p in parcels])
    changed = []
    for parcel in parcels:
        info = results.get(parcel["tracking_number"])
        if not info:
            continue
        updated = store.update(parcel["id"], status=info["status"], last_event=info["last_event"], eta=info["eta"], carrier=info["carrier"] or parcel["carrier"])
        if updated and info["status"] != parcel["status"] and info["status"] in NOTIFY_ON:
            changed.append(updated)
    return changed
