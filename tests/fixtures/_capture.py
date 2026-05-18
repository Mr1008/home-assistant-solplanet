"""Dev tool: dump live responses from a real Solplanet dongle into JSON fixtures.

Usage::

    SOLPLANET_HOST=192.0.2.10 python tests/fixtures/_capture.py

Captures the V2 endpoint set; the integration's protocol auto-detection picks
the right scheme/port at runtime, so this tool also re-detects on its own.

Not exercised by CI — run it manually whenever you upgrade dongle firmware so
the recorded fixtures track reality.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import aiohttp

from custom_components.solplanet.api_adapter import SolplanetApiAdapter
from custom_components.solplanet.client import SolplanetClient

logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger("solplanet.capture")

OUT_DIR = Path(__file__).parent
V2_DIR = OUT_DIR / "v2"
V1_DIR = OUT_DIR / "v1"


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    _LOGGER.info("wrote %s", path.relative_to(OUT_DIR.parent.parent))


async def _safe(label: str, coro):
    try:
        return await coro
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("skipped %s: %s", label, err)
        return None


async def main() -> None:
    host = os.environ.get("SOLPLANET_HOST")
    if not host:
        raise SystemExit("Set SOLPLANET_HOST=<dongle-ip> to capture fixtures.")

    async with aiohttp.ClientSession() as session:
        client = SolplanetClient(host, session)
        adapter = await SolplanetApiAdapter.create(client)
        _LOGGER.info("Detected protocol %s on %s:%s", adapter.version, host, client.port)

        if adapter.version == "v2":
            out = V2_DIR
            out.mkdir(parents=True, exist_ok=True)

            for label, endpoint in [
                ("getdev_device2", "getdev.cgi?device=2"),
                ("getdev", "getdev.cgi"),
                ("wlanget_info2", "wlanget.cgi?info=2"),
                ("getdevdata_device1", "getdevdata.cgi?device=1"),
                ("getdefine", "getdefine.cgi"),
            ]:
                data = await _safe(endpoint, client.get(endpoint))
                if data is not None:
                    _write(out / f"{label}.json", data)

            info = await adapter.get_inverter_info()
            if info.inv:
                sn = info.inv[0].isn
                for label, endpoint in [
                    ("getdevdata_device2", f"getdevdata.cgi?device=2&sn={sn}"),
                    ("getdev_device4", f"getdev.cgi?device=4&sn={sn}"),
                    ("getdevdata_device4", f"getdevdata.cgi?device=4&sn={sn}"),
                ]:
                    data = await _safe(endpoint, client.get(endpoint))
                    if data is not None:
                        _write(out / f"{label}.json", data)

            for label, cmd in [
                ("getting_app_dev_info", {"cmd": "get_app_dev_info_req", "payload": {"type": [4]}}),
                ("getting_meter_data", {"cmd": "get_meter_data_req"}),
                ("getting_meter_req", {"cmd": "get_meter_req"}),
                ("getting_meter_power", {"cmd": "get_meter_power_req"}),
            ]:
                data = await _safe(label, client.post("getting.cgi", cmd))
                if data is not None:
                    _write(out / f"{label}.json", data)
        else:
            out = V1_DIR
            out.mkdir(parents=True, exist_ok=True)
            for label, endpoint in [
                ("invinfo", "invinfo.cgi"),
                ("emeter", "emeter.cgi"),
                ("pwrlim", "pwrlim.cgi"),
            ]:
                data = await _safe(endpoint, client.get(endpoint))
                if data is not None:
                    _write(out / f"{label}.json", data)

            info = await adapter.get_inverter_info()
            if info.inv:
                sn = info.inv[0].isn
                data = await _safe("invdata", client.get(f"invdata.cgi?sn={sn}"))
                if data is not None:
                    _write(out / "invdata.json", data)


if __name__ == "__main__":
    asyncio.run(main())
