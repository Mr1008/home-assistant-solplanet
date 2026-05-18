"""Tests for the Solplanet config flow."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from custom_components.solplanet.const import CONF_INTERVAL, DOMAIN

from homeassistant import config_entries
from homeassistant.const import CONF_HOST
from homeassistant.data_entry_flow import FlowResultType


@pytest.fixture
def user_input():
    return {CONF_HOST: "1.2.3.4", CONF_INTERVAL: 60}


class TestUserFlow:
    async def test_v2_happy_path_uses_dongle_psn_as_unique_id(
        self, hass, fake_v2_client, patch_client_factory, user_input
    ):
        with patch_client_factory(fake_v2_client, "v2"):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            assert result["type"] == FlowResultType.FORM
            assert result["step_id"] == "user"

            result2 = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input
            )
            await hass.async_block_till_done()

        assert result2["type"] == FlowResultType.CREATE_ENTRY
        assert result2["title"] == "1.2.3.4"
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        assert entry.unique_id == "DG2024TEST0001"  # dongle psn

    async def test_v1_happy_path_falls_back_to_inverter_sn(
        self, hass, fake_v1_client, patch_client_factory, user_input
    ):
        with patch_client_factory(fake_v1_client, "v1"):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result2 = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input
            )
            await hass.async_block_till_done()

        assert result2["type"] == FlowResultType.CREATE_ENTRY
        # V1 has no dongle endpoint → falls back to inverter serial
        assert result2["title"] == "INV2020V1TEST01"
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        assert entry.unique_id == "INV2020V1TEST01"

    async def test_v2_dongle_failure_falls_back_to_inverter_sn(
        self, hass, fake_v2_client, patch_client_factory, user_input
    ):
        # Drop dongle identity → covers fallback at config_flow.py:58-72
        fake_v2_client.errors_by_endpoint["getdev.cgi"] = RuntimeError("dongle offline")
        with patch_client_factory(fake_v2_client, "v2"):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result2 = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input
            )
            await hass.async_block_till_done()
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        assert entry.unique_id == "INV2024TEST0001"  # inverter serial fallback

    async def test_cannot_connect_when_all_endpoints_fail(self, hass, user_input):
        with patch(
            "custom_components.solplanet.config_flow.SolplanetApiAdapter.create",
            side_effect=RuntimeError("all probes failed"),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result2 = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input
            )

        assert result2["type"] == FlowResultType.FORM
        assert result2["errors"] == {"base": "cannot_connect"}

    async def test_duplicate_unique_id_aborts(
        self, hass, fake_v2_client, patch_client_factory, make_config_entry, user_input
    ):
        existing = make_config_entry(host="9.9.9.9", unique_id="DG2024TEST0001")
        existing.add_to_hass(hass)

        with patch_client_factory(fake_v2_client, "v2"):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result2 = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input
            )
        assert result2["type"] == FlowResultType.ABORT
        assert result2["reason"] == "already_configured"
        # Host should have been updated on the existing entry.
        assert existing.data[CONF_HOST] == "1.2.3.4"


class TestOptionsFlow:
    async def test_options_flow_updates_interval(
        self, hass, fake_v2_client, patch_client_factory, make_config_entry
    ):
        entry = make_config_entry(interval=60)
        entry.add_to_hass(hass)
        with patch_client_factory(fake_v2_client, "v2"):
            await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

            result = await hass.config_entries.options.async_init(entry.entry_id)
            assert result["type"] == FlowResultType.FORM

            result2 = await hass.config_entries.options.async_configure(
                result["flow_id"], {CONF_INTERVAL: 120}
            )
            await hass.async_block_till_done()

        assert result2["type"] == FlowResultType.CREATE_ENTRY
        assert entry.data[CONF_INTERVAL] == 120
