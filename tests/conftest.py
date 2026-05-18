"""Shared fixtures for the Solplanet integration test suite."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import json
from pathlib import Path
import struct
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solplanet.const import CONF_INTERVAL, DEFAULT_INTERVAL, DOMAIN

from homeassistant.const import CONF_HOST

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# --- pytest-homeassistant-custom-component plumbing ------------------------


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Force HA to load the local custom_components directory for every test."""
    yield


# --- Fixture loading -------------------------------------------------------


def load_fixture(*parts: str) -> Any:
    """Load a JSON fixture file by path components under tests/fixtures."""
    path = FIXTURES_DIR.joinpath(*parts)
    return json.loads(path.read_text())


# --- Modbus response synthesis ---------------------------------------------
#
# The integration parses Modbus RTU responses returned (hex-encoded) inside a
# `{"data": "<hex>"}` JSON envelope from the dongle's `fdbg.cgi` endpoint.
# Building real CRC-correct frames at test time lets us assert against the
# decoded result without hand-rolling vectors.


def _crc16(data: bytes) -> int:
    """Modbus RTU CRC-16. Mirrors ModbusRtuFrameGenerator._calculate_crc."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 0x0001 else crc >> 1
    return crc


def build_read_holding_response(device_id: int, register_values: list[int]) -> str:
    """Build a function-0x03 read-holding-registers response frame as a hex string."""
    byte_count = len(register_values) * 2
    body = struct.pack(">BBB", device_id, 0x03, byte_count) + b"".join(
        struct.pack(">H", v) for v in register_values
    )
    return (body + struct.pack("<H", _crc16(body))).hex()


def build_write_single_response(device_id: int, register_offset: int, value: int) -> str:
    """Build a function-0x06 write-single-register echo response."""
    body = struct.pack(">BBHH", device_id, 0x06, register_offset, value)
    return (body + struct.pack("<H", _crc16(body))).hex()


def build_write_multiple_response(device_id: int, register_offset: int, quantity: int) -> str:
    """Build a function-0x10 write-multiple-registers echo response."""
    body = struct.pack(">BBHH", device_id, 0x10, register_offset, quantity)
    return (body + struct.pack("<H", _crc16(body))).hex()


# --- Mock SolplanetClient --------------------------------------------------
#
# The integration's entire network surface is SolplanetClient.get() and
# .post(). Routing those two methods through a dispatcher gives full control
# over endpoint responses without poking at aiohttp internals.


class FakeSolplanetClient:
    """In-memory stand-in for SolplanetClient.

    Routes get/post calls by endpoint string (and POST body when relevant) to
    JSON fixture data. Mutable so individual tests can override specific
    endpoints or inject failures.
    """

    def __init__(self, host: str = "1.2.3.4") -> None:
        self.host = host
        self.scheme = "http"
        self.port = 8484
        self.session = None  # populated when wired into HA setup
        self.get_responses: dict[str, Any] = {}
        self.post_responses_by_cmd: dict[str, Any] = {}
        self.errors_by_endpoint: dict[str, Exception] = {}
        self.error_after_n_calls: dict[str, tuple[int, Exception]] = {}
        self.call_counts: dict[str, int] = {}
        # Modbus reads: keyed by (register_address, register_count) → values
        self.modbus_holding: dict[tuple[int, int], list[int]] = {}
        # Track all (method, endpoint, payload) for assertions.
        self.calls: list[tuple[str, str, Any]] = []
        # Optional async hook called inside every get/post — useful for testing
        # the shared lock or simulating slow responses.
        self.async_hook: Callable[[], Awaitable[None]] | None = None

    def get_url(self, endpoint: str) -> str:
        return f"{self.scheme}://{self.host}:{self.port}/{endpoint}"

    async def _maybe_fail(self, endpoint: str) -> None:
        self.call_counts[endpoint] = self.call_counts.get(endpoint, 0) + 1
        if endpoint in self.errors_by_endpoint:
            raise self.errors_by_endpoint[endpoint]
        if endpoint in self.error_after_n_calls:
            after, err = self.error_after_n_calls[endpoint]
            if self.call_counts[endpoint] > after:
                raise err
        if self.async_hook is not None:
            await self.async_hook()

    async def get(self, endpoint: str) -> Any:
        self.calls.append(("GET", endpoint, None))
        await self._maybe_fail(endpoint)
        if endpoint not in self.get_responses:
            raise KeyError(f"FakeSolplanetClient: no canned response for GET {endpoint}")
        value = self.get_responses[endpoint]
        if callable(value):
            return value()
        return value

    async def post(self, endpoint: str, data: Any) -> Any:
        # Mirror the real client: dataclasses get asdict()'d before send.
        from dataclasses import asdict, is_dataclass

        payload = asdict(data) if is_dataclass(data) else data
        self.calls.append(("POST", endpoint, payload))
        await self._maybe_fail(endpoint)

        if endpoint == "fdbg.cgi":
            return self._handle_modbus_post(payload)
        if endpoint == "getting.cgi":
            cmd = (payload or {}).get("cmd") if isinstance(payload, dict) else None
            if cmd in self.post_responses_by_cmd:
                value = self.post_responses_by_cmd[cmd]
                if callable(value):
                    return value(payload)
                return value
            return {"status": 404}
        if endpoint == "setting.cgi":
            return {"dat": "ok"}
        raise KeyError(f"FakeSolplanetClient: no canned response for POST {endpoint}")

    def _handle_modbus_post(self, payload: dict) -> dict[str, str]:
        frame_hex = (payload or {}).get("data") or (payload or {}).get("frame") or ""
        frame = bytes.fromhex(frame_hex) if frame_hex else b""
        if len(frame) < 6:
            return {"data": ""}
        device_id, function_code = frame[0], frame[1]
        register_offset = int.from_bytes(frame[2:4], "big")
        register_address = 40001 + register_offset

        if function_code == 0x03:
            register_count = int.from_bytes(frame[4:6], "big")
            values = self.modbus_holding.get(
                (register_address, register_count), [0] * register_count
            )
            return {"data": build_read_holding_response(device_id, values)}
        if function_code == 0x06:
            value = int.from_bytes(frame[4:6], "big")
            return {"data": build_write_single_response(device_id, register_offset, value)}
        if function_code == 0x10:
            quantity = int.from_bytes(frame[4:6], "big")
            return {"data": build_write_multiple_response(device_id, register_offset, quantity)}
        return {"data": ""}


def _populate_v2(client: FakeSolplanetClient) -> None:
    """Load V2 fixture set into a FakeSolplanetClient."""
    inv_sn = "INV2024TEST0001"
    client.get_responses = {
        "getdev.cgi?device=2": load_fixture("v2", "getdev_device2.json"),
        "getdev.cgi": load_fixture("v2", "getdev.json"),
        f"getdevdata.cgi?device=2&sn={inv_sn}": load_fixture("v2", "getdevdata_device2.json"),
        f"getdev.cgi?device=4&sn={inv_sn}": load_fixture("v2", "getdev_device4.json"),
        f"getdevdata.cgi?device=4&sn={inv_sn}": load_fixture("v2", "getdevdata_device4.json"),
        "wlanget.cgi?info=2": load_fixture("v2", "wlanget_info2.json"),
        "getdevdata.cgi?device=1": load_fixture("v2", "getdevdata_device1.json"),
        "getdefine.cgi": load_fixture("v2", "getdefine.json"),
        "getdev.cgi?device=3": {},
        "getdevdata.cgi?device=3": {},
    }
    client.post_responses_by_cmd = {
        "get_app_dev_info_req": load_fixture("v2", "getting_app_dev_info.json"),
        "get_meter_data_req": load_fixture("v2", "getting_meter_data.json"),
        "get_meter_req": load_fixture("v2", "getting_meter_req.json"),
        "get_meter_power_req": load_fixture("v2", "getting_meter_power.json"),
    }
    client.modbus_holding = {
        # Inverter power register (40201, 1) → on
        (40201, 1): [1],
        # Battery More Settings (41501, 4) → power_on, sleep_enabled, led=0, brightness=50
        (41501, 4): [1, 0, 0, 50],
    }


def _populate_v1(client: FakeSolplanetClient) -> None:
    """Load V1 fixture set into a FakeSolplanetClient."""
    inv_sn = "INV2020V1TEST01"
    client.get_responses = {
        "invinfo.cgi": load_fixture("v1", "invinfo.json"),
        f"invdata.cgi?sn={inv_sn}": load_fixture("v1", "invdata.json"),
        "emeter.cgi": load_fixture("v1", "emeter.json"),
        "pwrlim.cgi": load_fixture("v1", "pwrlim.json"),
    }


# --- Detection patches -----------------------------------------------------
#
# SolplanetApiAdapter.create() probes endpoints to detect the protocol version.
# When tests inject a FakeSolplanetClient we want to skip that probe and force
# a specific version directly.


def _make_adapter(client: FakeSolplanetClient, version: str):
    """Build a SolplanetApiAdapter wrapping the FakeSolplanetClient, no probing."""
    from custom_components.solplanet.api_adapter import SolplanetApiAdapter
    from custom_components.solplanet.client import SolplanetApiV1, SolplanetApiV2

    api_cls = SolplanetApiV2 if version == "v2" else SolplanetApiV1
    api = api_cls(client)
    return SolplanetApiAdapter(client, api)


# --- Pytest fixtures -------------------------------------------------------


@pytest.fixture
def fake_v2_client() -> FakeSolplanetClient:
    """A FakeSolplanetClient pre-loaded with V2 fixtures."""
    client = FakeSolplanetClient()
    _populate_v2(client)
    return client


@pytest.fixture
def fake_v1_client() -> FakeSolplanetClient:
    """A FakeSolplanetClient pre-loaded with V1 fixtures."""
    client = FakeSolplanetClient()
    _populate_v1(client)
    return client


@pytest.fixture
def patch_client_factory():
    """Patch SolplanetClient/SolplanetApiAdapter so HA setup uses a fake.

    Usage::

        with patch_client_factory(fake_v2_client, "v2"):
            await hass.config_entries.async_setup(entry.entry_id)
    """
    from contextlib import contextmanager

    @contextmanager
    def _patch(client: FakeSolplanetClient, version: str):
        adapter = _make_adapter(client, version)

        async def _fake_create(cls_client):
            return adapter

        with (
            patch(
                "custom_components.solplanet.SolplanetClient",
                return_value=client,
            ),
            patch(
                "custom_components.solplanet.SolplanetApiAdapter.create",
                side_effect=_fake_create,
            ),
            patch(
                "custom_components.solplanet.config_flow.SolplanetClient",
                return_value=client,
            ),
            patch(
                "custom_components.solplanet.config_flow.SolplanetApiAdapter.create",
                side_effect=_fake_create,
            ),
        ):
            yield adapter

    return _patch


@pytest.fixture
def make_config_entry():
    """Factory returning a MockConfigEntry for the integration."""

    def _factory(
        host: str = "1.2.3.4",
        interval: int = DEFAULT_INTERVAL,
        unique_id: str = "DG2024TEST0001",
        **extra: Any,
    ) -> MockConfigEntry:
        return MockConfigEntry(
            domain=DOMAIN,
            data={CONF_HOST: host, CONF_INTERVAL: interval, **extra},
            unique_id=unique_id,
            version=1,
            minor_version=2,
            title=host,
        )

    return _factory


@pytest.fixture
def skip_platforms():
    """Bypass platform loading so coordinator/runtime tests don't depend on platform code.

    Platform modules (sensor/number/switch/...) reference integration-level
    coordinator types; tests for the coordinator layer shouldn't need them
    loaded. Patches `async_forward_entry_setups` to a no-op.
    """
    from contextlib import contextmanager

    @contextmanager
    def _patch():
        async def _noop(*args, **kwargs):
            return True

        with patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            side_effect=_noop,
        ):
            yield

    return _patch


@pytest.fixture
async def setup_v2_entry(
    hass, fake_v2_client, make_config_entry, patch_client_factory, skip_platforms
):
    """Set up the integration with V2 fixtures and return (entry, runtime).

    Skips platform forwarding by default — coordinator-layer tests don't need
    sensor/number/etc. loaded. Use `setup_v2_entry_full` if you need platforms.
    """
    entry = make_config_entry()
    entry.add_to_hass(hass)
    with patch_client_factory(fake_v2_client, "v2"), skip_platforms():
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, entry.runtime_data


@pytest.fixture
async def setup_v1_entry(
    hass, fake_v1_client, make_config_entry, patch_client_factory, skip_platforms
):
    """Set up the integration with V1 fixtures and return (entry, runtime)."""
    entry = make_config_entry(unique_id="INV2020V1TEST01")
    entry.add_to_hass(hass)
    with patch_client_factory(fake_v1_client, "v1"), skip_platforms():
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, entry.runtime_data


@pytest.fixture
async def setup_v2_entry_full(hass, fake_v2_client, make_config_entry, patch_client_factory):
    """Set up the integration with V2 fixtures *including* platforms.

    Use for tests that need entities (e.g. sensor snapshot). Will fail if any
    platform module fails to import.
    """
    entry = make_config_entry()
    entry.add_to_hass(hass)
    with patch_client_factory(fake_v2_client, "v2"):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, entry.runtime_data
