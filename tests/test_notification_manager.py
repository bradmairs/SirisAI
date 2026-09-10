import uuid

from app.core.siris import SirisAI, get_siris
from app.notify.manager import NotificationManager


def _manager() -> tuple[NotificationManager, SirisAI]:
    siris = SirisAI(settings=get_siris().settings)
    return NotificationManager(siris.entities), siris


def test_classifies_a_resource_warning() -> None:
    manager, _ = _manager()
    items = manager.summarize(["CPU usage is high: 95%"])
    assert len(items) == 1
    assert items[0].category == "resource"
    assert items[0].severity == "warning"
    assert items[0].action == "notify"
    assert items[0].confidence == 1.0


def test_classifies_an_exited_container_as_critical_and_urgent() -> None:
    manager, _ = _manager()
    items = manager.summarize(["Container 'plex' is exited"])
    assert items[0].category == "docker"
    assert items[0].severity == "critical"
    assert items[0].action == "urgent"


def test_classifies_a_non_exited_container_state_as_warning_not_critical() -> None:
    manager, _ = _manager()
    items = manager.summarize(["Container 'plex' is restarting"])
    assert items[0].severity == "warning"
    assert items[0].action == "notify"


def test_classifies_an_unrecognized_warning_as_other() -> None:
    manager, _ = _manager()
    items = manager.summarize(["something weird happened"])
    assert items[0].category == "other"


def test_correlates_an_event_as_the_cause_with_a_related_disk_warning() -> None:
    """Cross-domain correlation (a Jarvis-concept brainstorm item): a
    flagged event's own named entity is now eligible as a correlation
    cause, the same role a container name already played."""
    nas = f"nas-{uuid.uuid4()}"
    disk = f"nas-storage-{uuid.uuid4()}"
    manager, siris = _manager()
    siris.entities.add_relationship(nas, "DEPENDS_ON", disk)

    warnings = [
        f"Event flagged: {nas} went offline (entity: {nas})",
        f"Disk usage on {disk} is high: 96%",
    ]
    items = manager.summarize(warnings)

    assert len(items) == 1
    assert items[0].category == "correlated"
    assert items[0].correlated_with == [warnings[1]]


def test_correlates_an_event_as_the_partner_of_a_docker_warning() -> None:
    """The same named entity is also eligible as the partner side --
    a container's related event, not just a container's related disk."""
    container = f"plex-{uuid.uuid4()}"
    nas = f"nas-{uuid.uuid4()}"
    manager, siris = _manager()
    siris.entities.add_relationship(container, "DEPENDS_ON", nas)

    warnings = [
        f"Container '{container}' is exited",
        f"Event flagged: {nas} went offline (entity: {nas})",
    ]
    items = manager.summarize(warnings)

    assert len(items) == 1
    assert items[0].category == "correlated"
    assert items[0].action == "urgent"
    assert items[0].correlated_with == [warnings[1]]


def test_does_not_correlate_an_event_without_a_recorded_relationship() -> None:
    nas = f"nas-{uuid.uuid4()}"
    disk = f"nas-storage-{uuid.uuid4()}"
    manager, _ = _manager()
    # No add_relationship call this time.

    warnings = [
        f"Event flagged: {nas} went offline (entity: {nas})",
        f"Disk usage on {disk} is high: 96%",
    ]
    items = manager.summarize(warnings)

    assert len(items) == 2
    assert {item.category for item in items} == {"event", "resource"}


def test_a_container_name_is_never_extracted_as_a_partner_entity() -> None:
    """Unchanged from before this extension: a container name is only
    ever the cause side of a correlation, never matched as another
    item's partner -- unlike a disk path or a flagged event's entity,
    both of which are eligible on either side."""
    from app.notify.manager import _partner_entity_name

    assert _partner_entity_name("Container 'plex' is exited") is None


def test_classifies_a_flagged_event_as_notify_never_urgent() -> None:
    """event_reaction_check's keyword scan runs against a completely
    freeform description, so even a keyword hit stays "notify", the same
    conservative stance new_device_alert's "network" category takes."""
    manager, _ = _manager()
    items = manager.summarize(["Event flagged: NAS offline (source: nas)"])
    assert items[0].category == "event"
    assert items[0].severity == "warning"
    assert items[0].action == "notify"
    assert items[0].confidence == 1.0
    assert items[0].action == "notify"


def test_correlates_container_and_disk_warnings_via_world_model() -> None:
    container = f"plex-{uuid.uuid4()}"
    disk = f"media-storage-{uuid.uuid4()}"
    manager, siris = _manager()
    siris.entities.add_relationship(container, "DEPENDS_ON", disk)

    warnings = [f"Container '{container}' is exited", f"Disk usage on {disk} is high: 96%"]
    items = manager.summarize(warnings)

    assert len(items) == 1
    assert items[0].category == "correlated"
    assert items[0].action == "urgent"  # docker item was critical, wins over the merge
    assert warnings[0] in items[0].message
    assert warnings[1] in items[0].message
    assert items[0].correlated_with == [warnings[1]]


def test_does_not_correlate_without_a_recorded_relationship() -> None:
    container = f"plex-{uuid.uuid4()}"
    disk = f"media-storage-{uuid.uuid4()}"
    manager, _ = _manager()
    # No add_relationship call this time.

    warnings = [f"Container '{container}' is exited", f"Disk usage on {disk} is high: 96%"]
    items = manager.summarize(warnings)

    assert len(items) == 2
    assert {item.category for item in items} == {"docker", "resource"}


def test_does_not_correlate_an_unrelated_disk_warning() -> None:
    container = f"plex-{uuid.uuid4()}"
    related_disk = f"media-storage-{uuid.uuid4()}"
    unrelated_disk = f"backups-{uuid.uuid4()}"
    manager, siris = _manager()
    siris.entities.add_relationship(container, "DEPENDS_ON", related_disk)

    warnings = [f"Container '{container}' is exited", f"Disk usage on {unrelated_disk} is high: 90%"]
    items = manager.summarize(warnings)

    assert len(items) == 2


def test_correlated_severity_falls_back_to_partner_when_docker_item_is_not_critical() -> None:
    container = f"plex-{uuid.uuid4()}"
    disk = f"media-storage-{uuid.uuid4()}"
    manager, siris = _manager()
    siris.entities.add_relationship(container, "DEPENDS_ON", disk)

    warnings = [f"Container '{container}' is restarting", f"Disk usage on {disk} is high: 96%"]
    items = manager.summarize(warnings)

    assert len(items) == 1
    assert items[0].severity == "warning"
    assert items[0].action == "notify"
