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
