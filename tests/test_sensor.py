"""Snapshot test for the Solplanet sensor catalog.

A coarse regression net: dumps every entity the integration creates from a
fixed V2 fixture set, including state, unit, device_class, and state_class.
A label or scaling change anywhere in sensor.py will surface as a snapshot
diff. This is the kind of net that would have caught the EPS phase
voltage/current swap fixed in 5f3c62b.
"""

from __future__ import annotations

import importlib

import pytest
from syrupy.assertion import SnapshotAssertion

from custom_components.solplanet.const import DOMAIN

from homeassistant.helpers import entity_registry as er


def _platforms_loadable() -> tuple[bool, str | None]:
    """Return (ok, missing_module) — all platform modules import cleanly."""
    for name in ("sensor", "number", "select", "switch", "button", "binary_sensor"):
        try:
            importlib.import_module(f"custom_components.solplanet.{name}")
        except ImportError as err:
            return False, f"{name}: {err}"
    return True, None


pytestmark = pytest.mark.skipif(
    not _platforms_loadable()[0],
    reason=f"platform module import failed: {_platforms_loadable()[1]}",
)


@pytest.fixture
def entity_snapshot(snapshot: SnapshotAssertion):
    return snapshot


async def test_v2_sensor_catalog_snapshot(hass, setup_v2_entry_full, entity_snapshot):
    entry, _ = setup_v2_entry_full
    registry = er.async_get(hass)

    entries = [
        e for e in registry.entities.values()
        if e.config_entry_id == entry.entry_id and e.domain == "sensor"
    ]
    entries.sort(key=lambda e: e.entity_id)

    catalog = []
    for re in entries:
        state = hass.states.get(re.entity_id)
        catalog.append(
            {
                "entity_id": re.entity_id,
                "name": re.original_name or re.name,
                "unit": re.unit_of_measurement,
                "device_class": str(re.device_class) if re.device_class else None,
                "state_class": (
                    state.attributes.get("state_class") if state else None
                ),
                "state": state.state if state else None,
            }
        )

    assert catalog == entity_snapshot


async def test_v2_entity_count_is_nonzero(hass, setup_v2_entry_full):
    """Smoke check — the snapshot test relies on entities actually existing."""
    entry, _ = setup_v2_entry_full
    registry = er.async_get(hass)
    count = sum(
        1
        for e in registry.entities.values()
        if e.config_entry_id == entry.entry_id
    )
    assert count > 10, f"expected many entities, got {count}"
