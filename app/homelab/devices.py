"""New devices on the network, from UniFi's client list (every
SIRISAI_NETWORK_DEVICE_WATCH_MINUTES, 5).

A device that joins and hasn't been seen before raises "New device:
Espressif on IoT" in the attention feed, with what UniFi knows about it
(hostname, maker, network or Wi-Fi, IP, MAC) and:
- **Name it**: remembers it under a name (the reply text, if the client
  sends one, else its hostname or maker) so it never alerts again;
- **Block**: blocks its MAC in UniFi (works even once it has left). Needs
  the approval PIN when one is set.

The first check records everything already there without alerting. Known
devices persist across restarts (KnownDeviceStore, shared with the Home
Assistant based `new_device_alert`, which stands down while this runs).
`network_name_device` names a device from chat.
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.hub.attention import action, register_action

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

NAMES_KEY = "network_device_names"
PREFIX = "unifi:"


def enabled(siris: "SirisAI") -> bool:
    return getattr(siris, "_unifi_client", None) is not None and siris.settings.network_device_watch_minutes > 0


def names(siris: "SirisAI") -> dict[str, str]:
    return dict(siris.state.get(NAMES_KEY) or {})


def describe(device: dict[str, Any]) -> str:
    return device.get("name") or device.get("hostname") or device.get("vendor") or "Unknown device"


def check(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    clients = siris._unifi_client.client_details()  # type: ignore[union-attr]
    known = {k for k in siris.known_devices.list_known() if k.startswith(PREFIX)}
    current = {PREFIX + c["mac"]: c for c in clients}
    if not known:
        siris.known_devices.mark_known(current)
        return {"baseline": len(current), "new": []}
    new = [current[k] for k in sorted(set(current) - known)]
    siris.known_devices.mark_known(PREFIX + d["mac"] for d in new)
    for device in new:
        where = device.get("ssid") or device.get("network") or ("wired" if device.get("wired") else "the network")
        facts = [
            f"Name in UniFi: {device['name']}" if device.get("name") else None,
            f"Hostname: {device['hostname']}" if device.get("hostname") else None,
            f"Maker: {device['vendor']}" if device.get("vendor") else None,
            f"{'Wired' if device.get('wired') else 'Wi-Fi'}: {where}",
            f"IP: {device['ip']}" if device.get("ip") else None,
            f"MAC: {device['mac']}",
        ]
        siris.attention.raise_item(
            f"devices:new:{device['mac']}",
            "alert",
            f"New device: {describe(device)} on {where}",
            source="devices",
            severity="attention",
            body="\n".join(f for f in facts if f) + "\nIf you don't recognise it, block it.",
            actions=[action("name", "Name it", "primary"), action("block", "Block", "danger", confirm=f"Block {describe(device)} ({device['mac']}) in UniFi?")],
            payload={"mac": device["mac"], "suggested_name": describe(device)},
        )
    return {"new": [{"mac": d["mac"], "name": describe(d)} for d in new], "checked_at": (now or datetime.now(UTC)).isoformat()}


def name_device(siris: "SirisAI", mac: str, name: str) -> dict[str, Any]:
    mac, name = mac.strip().lower(), name.strip()[:80]
    if not mac or not name:
        raise ValueError("Give the device's MAC and a name.")
    kept = names(siris)
    kept[mac] = name
    siris.state.set(NAMES_KEY, kept)
    siris.known_devices.mark_known({PREFIX + mac} - siris.known_devices.list_known())
    return {"mac": mac, "name": name}


def find_mac(siris: "SirisAI", what: str) -> str:
    """A MAC from a MAC, a name given here, or what UniFi calls it."""
    what = what.strip().lower()
    if what.count(":") == 5:
        return what
    for mac, name in names(siris).items():
        if name.lower() == what:
            return mac
    for device in siris._unifi_client.client_details():  # type: ignore[union-attr]
        if what in {str(device.get(k) or "").lower() for k in ("name", "hostname", "ip")}:
            return device["mac"]
    raise LookupError(f"No device called '{what}' is connected.")


def _name(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    result = name_device(siris, payload["mac"], payload.get("body") or payload.get("suggested_name") or payload["mac"])
    return result, f"Named it {result['name']}"


def _block(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    # By MAC (the classic stamgr command), so it works even if the device has
    # just left; unblock it in the UniFi app or SirisOS's network view.
    mac = payload["mac"]
    result = siris._unifi_client.set_blocked(mac, True)  # type: ignore[union-attr]
    siris.audit.record("unifi_block", {"mac": mac}, result=result, trigger=trigger)
    return result, f"Blocked {mac}"


register_action("devices", "name", _name)
register_action("devices", "block", _block)
