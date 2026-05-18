"""Tests for SolplanetApiAdapter — version detection and dispatch."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.solplanet.api_adapter import SolplanetApiAdapter
from custom_components.solplanet.client import (
    GetInverterInfoResponse,
    SolplanetApiV1,
    SolplanetApiV2,
)


class _FakeClient:
    """Minimal stand-in tracking scheme/port/endpoint of every get() call."""

    def __init__(self, succeed_on: set[tuple[str, int, str]]) -> None:
        self.succeed_on = succeed_on
        self.scheme = "http"
        self.port = 8484
        self.attempts: list[tuple[str, int, str]] = []

    async def get(self, endpoint: str):
        key = (self.scheme, self.port, endpoint)
        self.attempts.append(key)
        if key in self.succeed_on:
            return {}
        raise aiohttp.ClientError(f"refused {key}")


class TestVersionDetection:
    async def test_prefers_v2_https(self):
        client = _FakeClient({("https", 443, "getdev.cgi?device=2")})
        adapter = await SolplanetApiAdapter.create(client)
        assert adapter.version == "v2"
        # The HTTPS attempt must be the first probe.
        assert client.attempts[0] == ("https", 443, "getdev.cgi?device=2")

    async def test_falls_back_to_v2_http(self):
        client = _FakeClient({("http", 8484, "getdev.cgi?device=2")})
        adapter = await SolplanetApiAdapter.create(client)
        assert adapter.version == "v2"
        assert ("https", 443, "getdev.cgi?device=2") in client.attempts
        assert ("http", 8484, "getdev.cgi?device=2") in client.attempts

    async def test_falls_back_to_v1(self):
        client = _FakeClient({("http", 8484, "invinfo.cgi")})
        adapter = await SolplanetApiAdapter.create(client)
        assert adapter.version == "v1"
        # All three probes were attempted in order.
        endpoints = [a[2] for a in client.attempts]
        assert endpoints == [
            "getdev.cgi?device=2",
            "getdev.cgi?device=2",
            "invinfo.cgi",
        ]

    async def test_no_protocol_raises_runtime_error(self):
        client = _FakeClient(succeed_on=set())
        with pytest.raises(RuntimeError, match="Failed to detect"):
            await SolplanetApiAdapter.create(client)


class TestVersionGating:
    async def test_v1_battery_operations_raise_not_implemented(self):
        client = MagicMock()
        api = SolplanetApiV1(client)
        adapter = SolplanetApiAdapter(client, api)
        assert adapter.version == "v1"
        for coro in (
            adapter.get_battery_data("X"),
            adapter.get_battery_info("X"),
            adapter.get_schedule(),
            adapter.set_schedule_pin(0),
            adapter.set_schedule_pout(0),
            adapter.set_schedule_slots({}),
        ):
            with pytest.raises(NotImplementedError):
                await coro

    async def test_v2_delegates_to_v2_client(self):
        client = MagicMock()
        api = SolplanetApiV2(client)
        adapter = SolplanetApiAdapter(client, api)
        # Replace the delegated method with an AsyncMock to verify the call.
        api.get_battery_data = AsyncMock(return_value="stub")
        result = await adapter.get_battery_data("BAT001")
        api.get_battery_data.assert_awaited_once_with("BAT001")
        assert result == "stub"


class TestDispatch:
    async def test_get_inverter_info_parses_into_dataclass(self):
        """Confirm `_create_class_from_dict` accepts firmware payload shape."""
        client = MagicMock()
        client.get = AsyncMock(
            return_value={
                "num": 1,
                "inv": [{"isn": "X", "model": "ASW", "mty": 11, "rate": 5000}],
            }
        )
        api = SolplanetApiV2(client)
        info = await api.get_inverter_info()
        assert isinstance(info, GetInverterInfoResponse)
        assert info.num == 1
        assert info.inv[0].isn == "X"
        # mty=11 is in the storage range; isStorage() should be True.
        assert info.inv[0].isStorage() is True

    async def test_get_inverter_info_ignores_unknown_keys(self):
        client = MagicMock()
        client.get = AsyncMock(
            return_value={
                "num": 1,
                "inv": [{"isn": "X", "futureField": "ignored"}],
                "otherFutureField": 42,
            }
        )
        api = SolplanetApiV2(client)
        info = await api.get_inverter_info()
        assert info.inv[0].isn == "X"
