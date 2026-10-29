"""Wake-on-LAN: turn a server back on, then watch for it to answer.

The MAC address comes from SIRISAI_SSH_WAKE_MACS ("nas=aa:bb:cc:dd:ee:ff"),
or else from what the server watch learned the last time the server was up
(app/servers/watch.py reads the default interface's address), so most
servers need no setup at all.

The magic packet is a broadcast, and a Docker container on a bridge network
(deploy/) can't reach the LAN's broadcast address. So it's sent every way
that's available, and one of them only has to work:

- through a relay server that's up (SIRISAI_SSH_WAKE_RELAY, over SSH, with
  a python3 one-liner every Linux server has);
- through Home Assistant's wake_on_lan integration, when Home Assistant is
  connected (it usually runs on the host network);
- straight from SirisAI, to 255.255.255.255 and the server's /24 broadcast
  (or SIRISAI_SSH_WAKE_BROADCAST), which works when SirisAI isn't in a
  container.

Then it checks in the background, every 15 seconds for up to
SIRISAI_SSH_WAKE_TIMEOUT_SECONDS (4 minutes), whether the server answers SSH,
and says so in the attention feed: "nas is awake (took 48s)" or "nas didn't
wake up".
"""

import ipaddress
import logging
import re
import socket
import threading
import time
from typing import TYPE_CHECKING, Any

from app.ssh.client import SshError, SshHost

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

STATE_KEY = "server_macs"
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
POLL_SECONDS = 15.0


def _entries(siris: "SirisAI") -> list[tuple[str, str, str]]:
    """SIRISAI_SSH_WAKE_MACS as (name, mac, address): "nas=aa:bb:cc:dd:ee:ff",
    or "desktop=aa:bb:cc:dd:ee:ff@192.168.0.50" with the address it answers on."""
    out = []
    for entry in siris.settings.ssh_wake_macs.split(","):
        name, _, rest = entry.partition("=")
        mac, _, address = rest.partition("@")
        mac = mac.strip().lower().replace("-", ":")
        if name.strip() and _MAC_RE.match(mac):
            out.append((name.strip().lower(), mac, address.strip()))
    return out


def configured_macs(siris: "SirisAI") -> dict[str, str]:
    return {name: mac for name, mac, _ in _entries(siris)}


def wake_only_machines(siris: "SirisAI") -> list[SshHost]:
    """Computers in SIRISAI_SSH_WAKE_MACS that aren't SSH servers (a desktop, a
    laptop): SirisAI can wake them, but not run anything on them."""
    ssh_names = {h.name for h in siris.ssh_client.hosts} if siris.ssh_client is not None else set()
    return [SshHost(name=name, user="", host=address) for name, _, address in _entries(siris) if name not in ssh_names]


def is_wake_only(siris: "SirisAI", host: SshHost) -> bool:
    return any(m.name == host.name for m in wake_only_machines(siris))


_PROBE_PORTS = (22, 445, 3389, 5900, 80, 139)


def answers(address: str, timeout: float = 2.0) -> bool:
    """Whether a machine is on the network: any port connecting, or refusing
    (a refusal comes from a running machine), counts."""
    for port in _PROBE_PORTS:
        try:
            with socket.create_connection((address, port), timeout=timeout):
                return True
        except ConnectionRefusedError:
            return True
        except OSError:
            continue
    return False


def mac_for(siris: "SirisAI", host: SshHost) -> str | None:
    return configured_macs(siris).get(host.name) or (siris.state.get(STATE_KEY) or {}).get(host.name)


def remember_mac(siris: "SirisAI", host: SshHost, mac: str) -> None:
    known = dict(siris.state.get(STATE_KEY) or {})
    if known.get(host.name) != mac:
        known[host.name] = mac
        siris.state.set(STATE_KEY, known)


def magic_packet(mac: str) -> bytes:
    if not _MAC_RE.match(mac):
        raise ValueError(f"'{mac}' isn't a MAC address")
    return b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16


def broadcasts_for(siris: "SirisAI", host: SshHost) -> list[str]:
    if siris.settings.ssh_wake_broadcast:
        return [siris.settings.ssh_wake_broadcast]
    out = ["255.255.255.255"]
    try:
        network = ipaddress.ip_network(f"{host.host}/24", strict=False)
        out.append(str(network.broadcast_address))
    except ValueError:
        pass  # a hostname, not an address: the general broadcast only
    return out


def send_direct(mac: str, addresses: list[str], port: int = 9) -> None:
    packet = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for address in addresses:
            sock.sendto(packet, (address, port))


def relay_command(mac: str, addresses: list[str]) -> str:
    magic_packet(mac)  # validates
    targets = ",".join(f"'{a}'" for a in addresses if re.fullmatch(r"[0-9.]+", a))
    return (
        "python3 -c \"import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);"
        "s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1);"
        f"p=bytes.fromhex('ff'*6+'{mac.replace(':', '')}'*16);"
        f"[s.sendto(p,(a,9)) for a in ({targets},)];print('sent')\""
    )


def send(siris: "SirisAI", host: SshHost, mac: str) -> list[str]:
    """Send the packet every way available. Returns the ways that went out."""
    addresses = broadcasts_for(siris, host)
    via: list[str] = []
    relay = siris.settings.ssh_wake_relay.strip().lower()
    if relay and relay != host.name and siris.ssh_client is not None:
        try:
            result = siris.ssh_client.run(relay, relay_command(mac, addresses), timeout=15)
            if result["exit_code"] == 0:
                via.append(f"relay {relay}")
        except (SshError, ValueError) as exc:
            logger.warning("Wake relay %s failed: %s", relay, exc)
    ha = getattr(siris, "_home_assistant_client", None)
    if ha is not None:
        try:
            ha.call_service("wake_on_lan", "send_magic_packet", mac=mac)
            via.append("Home Assistant")
        except Exception as exc:  # noqa: BLE001 - the integration may not be set up
            logger.info("Home Assistant wake_on_lan unavailable: %s", exc)
    try:
        send_direct(mac, addresses)
        via.append("direct broadcast")
    except OSError as exc:
        logger.warning("Direct wake broadcast failed: %s", exc)
    return via


def wait_until_up(siris: "SirisAI", host: SshHost, timeout: float, poll: float = POLL_SECONDS) -> bool:
    """Poll until the machine answers (SSH, or any port for a wake-only computer);
    say how it went in the feed."""
    from app.upkeep.alerts import raise_alert

    start = time.monotonic()
    wake_only = is_wake_only(siris, host)
    while time.monotonic() - start < timeout:
        try:
            if wake_only:
                up = answers(host.host)
            else:
                up = siris.ssh_client is not None and siris.ssh_client.run(host.name, "true", timeout=8)["exit_code"] == 0
            if up:
                took = int(time.monotonic() - start)
                siris.attention.resolve_key(f"servers:down:{host.name}", "Woke up")
                raise_alert(siris, f"servers:woke:{host.name}", f"{host.name} is awake", f"It answered {took}s after the wake-up packet.", severity="info")
                return True
        except (SshError, ValueError):
            pass
        time.sleep(poll)
    raise_alert(
        siris,
        f"servers:woke:{host.name}",
        f"{host.name} didn't wake up",
        f"No answer within {int(timeout)}s of the wake-up packet. Check Wake-on-LAN is on in its BIOS and network settings, "
        "and that the packet can reach it (SIRISAI_SSH_WAKE_RELAY, or Home Assistant's wake_on_lan integration).",
    )
    return False


def wake(siris: "SirisAI", host: SshHost, wait: bool = True) -> dict[str, Any]:
    mac = mac_for(siris, host)
    if not mac:
        raise ValueError(
            f"I don't know {host.name}'s MAC address yet. It's learned automatically while the server is up, "
            f"or set it in SIRISAI_SSH_WAKE_MACS ({host.name}=aa:bb:cc:dd:ee:ff)."
        )
    via = send(siris, host, mac)
    if not via:
        raise RuntimeError(f"Couldn't send the wake-up packet for {host.name} any way at all.")
    if wait and not host.host:
        wait = False  # a wake-only computer with no address: nothing to check it by
    if wait:
        threading.Thread(target=wait_until_up, args=(siris, host, siris.settings.ssh_wake_timeout_seconds), daemon=True).start()
    return {"server": host.name, "mac": mac, "sent_via": via, "watching": wait}
