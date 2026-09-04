"""KnownDeviceStore (a hardening pass): the persisted "have we seen this
device_tracker entity before" set NewDeviceAlertSkill checks against. See
tests/test_new_device_alert.py for the skill-level behavior this backs.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.network_devices.service import KnownDeviceStore


def _isolated_known_devices() -> KnownDeviceStore:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return KnownDeviceStore(sessionmaker(bind=engine))


def test_list_known_is_empty_for_a_fresh_store() -> None:
    store = _isolated_known_devices()
    assert store.list_known() == set()


def test_mark_known_then_list_known_reflects_it() -> None:
    store = _isolated_known_devices()
    store.mark_known(["device_tracker.a", "device_tracker.b"])
    assert store.list_known() == {"device_tracker.a", "device_tracker.b"}


def test_mark_known_with_an_empty_iterable_is_a_no_op() -> None:
    store = _isolated_known_devices()
    store.mark_known([])
    assert store.list_known() == set()


def test_mark_known_accumulates_across_calls() -> None:
    store = _isolated_known_devices()
    store.mark_known(["device_tracker.a"])
    store.mark_known(["device_tracker.b"])
    assert store.list_known() == {"device_tracker.a", "device_tracker.b"}
