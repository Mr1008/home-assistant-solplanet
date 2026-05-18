"""Tests for the Solplanet coordinator runtime, refresh cadences, and write paths."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.solplanet.const import (
    BATTERY_IDENTIFIER,
    CONFIG_UPDATE_INTERVAL,
    DONGLE_IDENTIFIER,
    INVERTER_IDENTIFIER,
    INVERTER_UPDATE_INTERVAL,
    METER_IDENTIFIER,
    METER_UPDATE_INTERVAL,
)


class TestSetup:
    async def test_all_coordinators_populate_data(self, hass, setup_v2_entry):
        entry, runtime = setup_v2_entry
        assert runtime.api.version == "v2"

        # Telemetry first-refreshes
        assert "INV2024TEST0001" in runtime.inverter.data[INVERTER_IDENTIFIER]

        assert runtime.battery is not None
        assert "INV2024TEST0001" in runtime.battery.data[BATTERY_IDENTIFIER]

        assert runtime.meter is not None
        assert runtime.meter.data[METER_IDENTIFIER]

        assert runtime.dongle is not None
        assert runtime.dongle.data[DONGLE_IDENTIFIER]["DG2024TEST0001"]["data"]["psn"] == (
            "DG2024TEST0001"
        )

        # Config first-refresh
        assert runtime.config.data[INVERTER_IDENTIFIER]["INV2024TEST0001"]["info"] is not None

    async def test_v1_battery_coordinator_is_absent(self, hass, setup_v1_entry):
        entry, runtime = setup_v1_entry
        assert runtime.api.version == "v1"
        # No battery isns discovered (mty=1 isn't a storage type) → battery coordinator never created.
        assert runtime.battery is None
        assert runtime.dongle is None  # V1 has no dongle endpoint


class TestRefreshCadence:
    async def test_default_intervals_match_constants(self, hass, setup_v2_entry):
        _, runtime = setup_v2_entry
        assert runtime.inverter.update_interval == INVERTER_UPDATE_INTERVAL
        assert runtime.meter.update_interval == METER_UPDATE_INTERVAL
        assert runtime.config.update_interval == CONFIG_UPDATE_INTERVAL

    async def test_error_backoff_after_three_failures(self, hass, setup_v2_entry, fake_v2_client):
        _, runtime = setup_v2_entry
        fake_v2_client.errors_by_endpoint[
            "getdevdata.cgi?device=2&sn=INV2024TEST0001"
        ] = RuntimeError("inverter offline")

        for _ in range(3):
            await runtime.inverter.async_refresh()

        assert runtime.inverter.update_interval == runtime.inverter.error_interval
        assert runtime.inverter.last_update_success is False

    async def test_error_interval_resets_on_recovery(self, hass, setup_v2_entry, fake_v2_client):
        _, runtime = setup_v2_entry
        endpoint = "getdevdata.cgi?device=2&sn=INV2024TEST0001"
        fake_v2_client.errors_by_endpoint[endpoint] = RuntimeError("inverter offline")
        for _ in range(3):
            await runtime.inverter.async_refresh()
        assert runtime.inverter.update_interval == runtime.inverter.error_interval

        fake_v2_client.errors_by_endpoint.pop(endpoint)
        await runtime.inverter.async_refresh()
        assert runtime.inverter.update_interval == runtime.inverter.default_interval


class TestSharedLock:
    async def test_concurrent_refreshes_are_serialized(self, hass, setup_v2_entry, fake_v2_client):
        """Two concurrent coordinator refreshes must not overlap."""
        _, runtime = setup_v2_entry

        in_flight = 0
        max_concurrent = 0

        async def hook():
            nonlocal in_flight, max_concurrent
            in_flight += 1
            max_concurrent = max(max_concurrent, in_flight)
            await asyncio.sleep(0)  # yield to event loop
            in_flight -= 1

        fake_v2_client.async_hook = hook

        await asyncio.gather(
            runtime.inverter.async_refresh(),
            runtime.config.async_refresh(),
            runtime.meter.async_refresh(),
        )
        assert max_concurrent == 1


class TestWritePaths:
    async def test_inverter_power_write_triggers_config_refresh(
        self, hass, setup_v2_entry, fake_v2_client
    ):
        _, runtime = setup_v2_entry
        before = fake_v2_client.call_counts.get("fdbg.cgi", 0)
        await runtime.set_inverter_power(True)
        # fdbg.cgi was hit at least once (the write itself + the config refresh's modbus read).
        assert fake_v2_client.call_counts["fdbg.cgi"] > before

        # Verify a POST to fdbg.cgi was made.
        fdbg_calls = [c for c in fake_v2_client.calls if c[0] == "POST" and c[1] == "fdbg.cgi"]
        assert fdbg_calls

    async def test_v1_battery_setter_raises_home_assistant_error(self, hass, setup_v1_entry):
        from homeassistant.exceptions import HomeAssistantError

        from custom_components.solplanet.client import BatteryWorkMode

        _, runtime = setup_v1_entry
        # V1 doesn't support battery operations — the adapter raises NotImplementedError
        # which the runtime should surface as HomeAssistantError.
        with pytest.raises(HomeAssistantError, match="Battery operations are not supported"):
            await runtime.set_battery_work_mode(
                "anything", BatteryWorkMode("Self-consumption mode", 2, 1)
            )
