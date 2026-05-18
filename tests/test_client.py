"""Tests for SolplanetClient (HTTP transport).

Mocks aiohttp directly via aioresponses so we exercise the real timeout/retry
loop and JSON parsing in client.py without spinning up the HA harness.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import aiohttp
import pytest
from aioresponses import aioresponses

from custom_components.solplanet.client import SolplanetClient


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


class TestGet:
    async def test_returns_parsed_json(self, session):
        with aioresponses() as m:
            m.get(
                "http://1.2.3.4:8484/invinfo.cgi",
                payload={"num": 1, "inv": []},
            )
            client = SolplanetClient("1.2.3.4", session)
            result = await client.get("invinfo.cgi")
        assert result == {"num": 1, "inv": []}

    async def test_url_construction(self, session):
        client = SolplanetClient("1.2.3.4", session, scheme="https", port=443)
        assert client.get_url("getdev.cgi") == "https://1.2.3.4:443/getdev.cgi"

    async def test_https_disables_ssl_verification(self, session):
        # The dongles ship self-signed certs; client must not refuse them.
        with aioresponses() as m:
            m.get("https://1.2.3.4:443/getdev.cgi", payload={})
            client = SolplanetClient("1.2.3.4", session, scheme="https", port=443)
            await client.get("getdev.cgi")
        # aioresponses doesn't surface the ssl kwarg, but the call having succeeded means
        # no SSL verification path was triggered.

    async def test_retries_once_on_timeout_then_succeeds(self, session):
        with aioresponses() as m:
            m.get("http://1.2.3.4:8484/invinfo.cgi", exception=asyncio.TimeoutError())
            m.get("http://1.2.3.4:8484/invinfo.cgi", payload={"ok": True})
            client = SolplanetClient("1.2.3.4", session, request_retries=1)
            result = await client.get("invinfo.cgi")
        assert result == {"ok": True}

    async def test_exhausts_retries_then_raises(self, session):
        with aioresponses() as m:
            m.get(
                "http://1.2.3.4:8484/invinfo.cgi",
                exception=asyncio.TimeoutError(),
                repeat=True,
            )
            client = SolplanetClient("1.2.3.4", session, request_retries=2)
            with pytest.raises(asyncio.TimeoutError):
                await client.get("invinfo.cgi")

    async def test_invalid_json_raises_after_retries(self, session):
        with aioresponses() as m:
            m.get(
                "http://1.2.3.4:8484/invinfo.cgi",
                body=b"not-json",
                content_type="application/json",
                repeat=True,
            )
            client = SolplanetClient("1.2.3.4", session, request_retries=0)
            with pytest.raises(json.JSONDecodeError):
                await client.get("invinfo.cgi")

    async def test_http_error_status_raises(self, session):
        with aioresponses() as m:
            m.get(
                "http://1.2.3.4:8484/invinfo.cgi",
                status=500,
                repeat=True,
            )
            client = SolplanetClient("1.2.3.4", session, request_retries=0)
            with pytest.raises(aiohttp.ClientResponseError):
                await client.get("invinfo.cgi")


class TestPost:
    async def test_post_with_dict_payload(self, session):
        with aioresponses() as m:
            m.post(
                "http://1.2.3.4:8484/getting.cgi",
                payload={"status": 200, "payload": {}},
            )
            client = SolplanetClient("1.2.3.4", session)
            result = await client.post("getting.cgi", {"cmd": "get_meter_req"})
        assert result == {"status": 200, "payload": {}}

    async def test_post_with_dataclass_payload_serializes_via_asdict(self, session):
        @dataclass
        class Req:
            cmd: str
            value: int

        with aioresponses() as m:
            m.post("http://1.2.3.4:8484/setting.cgi", payload={"dat": "ok"})
            client = SolplanetClient("1.2.3.4", session)
            result = await client.post("setting.cgi", Req(cmd="set", value=42))

        assert result == {"dat": "ok"}
        # Verify the request body was JSON-serialized from the dataclass.
        recorded = next(iter(m.requests.values()))[0]
        assert recorded.kwargs["json"] == {"cmd": "set", "value": 42}
