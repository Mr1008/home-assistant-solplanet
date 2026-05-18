"""Solplanet data coordinators.

Per-endpoint coordinators sharing a single per-config-entry lock. Structured after the
Fronius integration: each coordinator polls one logical endpoint at its own cadence; a
shared `asyncio.Lock` serializes all dongle traffic because the dongle does not
tolerate concurrent requests well. After a streak of failures a coordinator switches
to a longer error interval until the next success.

Coordinator ownership of `coordinator.data` slices (entity navigation is unchanged):

    inverter coord ─ {INVERTER_IDENTIFIER: {isn: {"data": ...}}}
    battery  coord ─ {BATTERY_IDENTIFIER:  {isn: {"data": ...}}}
    meter    coord ─ {METER_IDENTIFIER:    {sn:  {"data" | "app_data": ...}}}
    dongle   coord ─ {DONGLE_IDENTIFIER:   {id:  {"data", "network", "warnings"}}}
    config   coord ─ everything else (info, more_settings, schedule, work_modes,
                                       app_info, meter_req, meter_power)

Device inventory is discovered once at config-entry setup (`SolplanetRuntime.async_setup`)
and frozen for the life of the entry. Hot-plugging a device requires reloading.

All setters live on `SolplanetRuntime`. Entities and services call into the runtime
rather than into a specific coordinator so the refresh target is decided in one place.
"""

from __future__ import annotations

from abc import abstractmethod
import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api_adapter import SolplanetApiAdapter
from .client import (
    BatterySchedule,
    BatteryWorkMode,
    BatteryWorkModes,
    GetInverterInfoResponse,
    ScheduleSlot,
)
from .const import (
    BATTERY_ERROR_INTERVAL,
    BATTERY_IDENTIFIER,
    BATTERY_UPDATE_INTERVAL,
    CONFIG_ERROR_INTERVAL,
    CONFIG_UPDATE_INTERVAL,
    DOMAIN,
    DONGLE_ERROR_INTERVAL,
    DONGLE_IDENTIFIER,
    DONGLE_UPDATE_INTERVAL,
    INVERTER_ERROR_INTERVAL,
    INVERTER_IDENTIFIER,
    INVERTER_UPDATE_INTERVAL,
    METER_ERROR_INTERVAL,
    METER_IDENTIFIER,
    METER_UPDATE_INTERVAL,
)
from .modbus import DataType

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inventory + runtime
# ---------------------------------------------------------------------------


@dataclass
class SolplanetInventory:
    """Static device inventory discovered at config-entry setup.

    Hot-plugging is not supported: changes here require a config-entry reload.
    """

    inverters_info: GetInverterInfoResponse | None = None
    inverter_isns: list[str] = field(default_factory=list)
    battery_isns: list[str] = field(default_factory=list)
    meter_sns: list[str] = field(default_factory=list)
    app_primary_meter_sn: str | None = None
    dongle_id: str | None = None
    has_legacy_meter: bool = False


class SolplanetRuntime:
    """Per-config-entry runtime: shared state, lock, coordinators, and all setters."""

    api: SolplanetApiAdapter
    hass: HomeAssistant
    lock: asyncio.Lock
    inventory: SolplanetInventory

    inverter: SolplanetInverterCoordinator
    config: SolplanetConfigCoordinator
    battery: SolplanetBatteryCoordinator | None = None
    meter: SolplanetMeterCoordinator | None = None
    dongle: SolplanetDongleCoordinator | None = None

    def __init__(self, hass: HomeAssistant, api: SolplanetApiAdapter) -> None:
        """Initialize the runtime."""
        self.hass = hass
        self.api = api
        self.lock = asyncio.Lock()
        self.inventory = SolplanetInventory()

    # -- Discovery ---------------------------------------------------------

    async def async_setup(self) -> None:
        """Run one-time device discovery. Populates `self.inventory`.

        Raises:
            UpdateFailed: if the primary inverter-info call fails (without an inverter
                list there is nothing to coordinate).
        """
        try:
            inverters_info = await self.api.get_inverter_info()
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"Error fetching inverter info: {err}") from err

        self.inventory.inverters_info = inverters_info
        self.inventory.inverter_isns = [inv.isn for inv in inverters_info.inv if inv.isn]
        self.inventory.battery_isns = [
            inv.isn for inv in inverters_info.inv if inv.isStorage() and inv.isn
        ]

        # V2: discover dongle ID + meter inventory via app protocol.
        if self.api.version == "v2":
            try:
                dongle_info = await self.api.client.get("getdev.cgi")
                self.inventory.dongle_id = (
                    dongle_info.get("psn")
                    or dongle_info.get("ethmac")
                    or dongle_info.get("wlanmac")
                    or "unknown"
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Dongle discovery failed: %s", err, exc_info=True)

            try:
                rsp = await self.api.client.post(
                    "getting.cgi",
                    {"cmd": "get_app_dev_info_req", "payload": {"type": [4]}},
                )
                if isinstance(rsp, dict) and rsp.get("status") == 200:
                    payload = rsp.get("payload") or {}
                    main_meters = payload.get("mainMeter") or []
                    sub_meters = payload.get("subMeter") or []
                    if main_meters and isinstance(main_meters[0], dict):
                        self.inventory.app_primary_meter_sn = main_meters[0].get("sn")
                    for meter in [*main_meters, *sub_meters]:
                        if not isinstance(meter, dict):
                            continue
                        sn = meter.get("sn") or f"addr_{meter.get('address')}"
                        if sn and sn not in self.inventory.meter_sns:
                            self.inventory.meter_sns.append(sn)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("App-protocol meter discovery failed: %s", err, exc_info=True)

        # Fall back to legacy meter discovery (V1, or V2 firmware that 404s on getting.cgi).
        if not self.inventory.meter_sns:
            try:
                legacy_data = await self.api.get_meter_data()
                legacy_info = await self.api.get_meter_info()
                if legacy_meter_payload_looks_valid(legacy_data):
                    sn = (
                        getattr(legacy_info, "sn", None)
                        or (self.inventory.inverter_isns[0] if self.inventory.inverter_isns else None)
                    )
                    if sn:
                        self.inventory.meter_sns.append(sn)
                        self.inventory.has_legacy_meter = True
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Legacy meter probe failed: %s", err, exc_info=True)

    # -- Utility -----------------------------------------------------------

    def get_max_inverter_rate_w(self) -> int:
        """Return the highest inverter rated power (W) from frozen inventory."""
        info = self.inventory.inverters_info
        if info is None:
            return 10000
        rates = [
            int(getattr(inv, "rate", 0) or 0)
            for inv in info.inv
            if isinstance(getattr(inv, "rate", None), int) and inv.rate > 0
        ]
        return max(rates) if rates else 10000

    def coordinator_for(self, device_type: str, data_type: str) -> SolplanetCoordinatorBase:
        """Resolve the coordinator that owns the given (device_type, data_type) slice.

        Telemetry data_types (`data`, `app_data`) live on the per-device telemetry
        coordinator. Everything else (info, more_settings, schedule, work_modes,
        app_info, meter_req, meter_power) lives on the config coordinator. Dongle
        data is always served by the dongle coordinator.
        """
        if device_type == DONGLE_IDENTIFIER:
            if self.dongle is None:
                raise HomeAssistantError("Dongle coordinator is not available")
            return self.dongle

        telemetry_data_types = {"data", "app_data"}
        if data_type in telemetry_data_types:
            if device_type == INVERTER_IDENTIFIER:
                return self.inverter
            if device_type == BATTERY_IDENTIFIER and self.battery is not None:
                return self.battery
            if device_type == METER_IDENTIFIER and self.meter is not None:
                return self.meter

        return self.config

    # -- Setters (all writes go through the shared lock) ------------------

    async def _modbus_write_register(self, register_offset: int, value: int) -> None:
        """Write a single holding register via the shared API."""
        async with self.lock:
            try:
                await self.api.modbus_write_multiple_holding_registers(
                    device_address=3,
                    register_address=40001 + register_offset,
                    values=[value],
                )
            except NotImplementedError as err:
                raise HomeAssistantError("Modbus operations are not supported with V1 protocol") from err

    async def set_inverter_power(self, on: bool) -> None:
        """Set inverter power (Modbus holding register offset 200)."""
        async with self.lock:
            try:
                await self.api.modbus_write_single_holding_register(
                    data_type=DataType.U16,
                    device_address=3,
                    register_address=40201,  # 40001 + 200
                    value=1 if on else 0,
                    dry_run=False,
                )
            except NotImplementedError as err:
                raise HomeAssistantError(
                    "Modbus operations are not supported with V1 protocol"
                ) from err
        await self.config.async_request_refresh()

    async def set_battery_power(self, on: bool) -> None:
        """Set battery power (Modbus holding register offset 1500). 1=on, 0=shutdown."""
        await self._modbus_write_register(1500, 1 if on else 0)
        await self.config.async_request_refresh()

    async def set_battery_sleep_enabled(self, enabled: bool) -> None:
        """Set battery sleep flag (offset 1501). 0=enabled, 1=disabled."""
        await self._modbus_write_register(1501, 0 if enabled else 1)
        await self.config.async_request_refresh()

    async def set_battery_led_color_index(self, index: int) -> None:
        """Set battery LED color index (offset 1502)."""
        await self._modbus_write_register(1502, int(index))
        await self.config.async_request_refresh()

    async def set_battery_led_brightness(self, brightness: int) -> None:
        """Set battery LED brightness percent (offset 1503)."""
        await self._modbus_write_register(1503, int(brightness))
        await self.config.async_request_refresh()

    async def set_battery_work_mode(self, sn: str, mode: BatteryWorkMode) -> None:
        """Set battery work mode."""
        async with self.lock:
            try:
                await self.api.set_battery_work_mode(sn, mode)
            except NotImplementedError as err:
                raise HomeAssistantError(
                    "Battery operations are not supported with V1 protocol"
                ) from err
        await self.config.async_request_refresh()

    async def set_battery_soc_min(self, sn: str, value: int) -> None:
        """Set battery SOC min."""
        async with self.lock:
            try:
                await self.api.set_battery_soc_min(sn, value)
            except NotImplementedError as err:
                raise HomeAssistantError(
                    "Battery operations are not supported with V1 protocol"
                ) from err
        await self.config.async_request_refresh()

    async def set_battery_soc_max(self, sn: str, value: int) -> None:
        """Set battery SOC max."""
        async with self.lock:
            try:
                await self.api.set_battery_soc_max(sn, value)
            except NotImplementedError as err:
                raise HomeAssistantError(
                    "Battery operations are not supported with V1 protocol"
                ) from err
        await self.config.async_request_refresh()

    async def set_battery_schedule_slots(
        self, sn: str, slots: dict[str, list[ScheduleSlot]]
    ) -> None:
        """Replace battery schedule slots for the supplied days."""
        async with self.lock:
            try:
                _LOGGER.debug("Setting schedule slots for %s: %s", sn, slots)
                current = await self.api.get_schedule()
                raw_schedule = BatterySchedule.encode_schedule(
                    slots,
                    pin=current["raw"].get("Pin", 0),
                    pout=current["raw"].get("Pout", 0),
                )
                _LOGGER.debug("Encoded schedule: %s", raw_schedule)
                await self.api.set_schedule_slots(raw_schedule)
            except NotImplementedError as err:
                raise HomeAssistantError(
                    "Battery operations are not supported with V1 protocol"
                ) from err
        self.hass.async_create_task(self.config.async_request_refresh())

    async def set_battery_schedule_power(
        self, pin: int | None = None, pout: int | None = None
    ) -> None:
        """Set battery schedule input/output power simultaneously."""
        async with self.lock:
            try:
                await self.api.set_schedule_power(pin, pout)
            except NotImplementedError as err:
                raise HomeAssistantError(
                    "Battery operations are not supported with V1 protocol"
                ) from err
        self.hass.async_create_task(self.config.async_request_refresh())

    async def set_battery_schedule_pin(self, sn: str, pin: int) -> None:
        """Set battery schedule pin."""
        async with self.lock:
            try:
                await self.api.set_schedule_pin(pin)
            except NotImplementedError as err:
                raise HomeAssistantError(
                    "Battery operations are not supported with V1 protocol"
                ) from err
        await self.config.async_request_refresh()

    async def set_battery_schedule_pout(self, sn: str, pout: int) -> None:
        """Set battery schedule pout."""
        async with self.lock:
            try:
                await self.api.set_schedule_pout(pout)
            except NotImplementedError as err:
                raise HomeAssistantError(
                    "Battery operations are not supported with V1 protocol"
                ) from err
        await self.config.async_request_refresh()

    async def set_meter_power_limit(self, payload: dict) -> None:
        """Set meter power limit / zero-export configuration (V2 app protocol)."""
        if self.api.version != "v2":
            raise HomeAssistantError(
                "Meter power limit control is not supported with V1 protocol"
            )

        async with self.lock:
            try:
                rsp = await self.api.client.post(
                    "setting.cgi",
                    {"cmd": "set_meter_req", "payload": payload},
                )
            except Exception as err:  # noqa: BLE001
                raise HomeAssistantError(f"Failed to set meter power limit: {err}") from err

        if not isinstance(rsp, dict) or rsp.get("status") != 200:
            raise HomeAssistantError(f"Unexpected response from set_meter_req: {rsp}")

        self.hass.async_create_task(self.config.async_request_refresh())

    async def dongle_sync_time(self) -> None:
        """Sync dongle time using Home Assistant local time."""
        if self.api.version != "v2":
            raise HomeAssistantError("Dongle operations are not supported with V1 protocol")
        if self.dongle is None:
            raise HomeAssistantError("Dongle coordinator is not available")

        now = dt_util.now()
        payload = {
            "device": 1,
            "action": "settime",
            "value": {"time": now.strftime("%Y%m%d%H%M%S")},
        }
        async with self.lock:
            try:
                await self.api.client.post("setting.cgi", payload)
            except Exception as err:  # noqa: BLE001
                raise HomeAssistantError(f"Failed to sync dongle time: {err}") from err
        await self.dongle.async_request_refresh()

    async def dongle_reboot(self) -> None:
        """Reboot the dongle."""
        if self.api.version != "v2":
            raise HomeAssistantError("Dongle operations are not supported with V1 protocol")

        payload = {
            "device": 1,
            "action": "operation",
            "value": {"reboot": 1},
        }
        async with self.lock:
            try:
                await self.api.client.post("setting.cgi", payload)
            except Exception as err:  # noqa: BLE001
                raise HomeAssistantError(f"Failed to reboot dongle: {err}") from err


def legacy_meter_payload_looks_valid(meter_data: object) -> bool:
    """Return True if a legacy `device=3` meter payload looks real vs a stub/zeros payload."""
    if meter_data is None:
        return False

    tim = getattr(meter_data, "tim", None)
    if isinstance(tim, str) and tim.strip():
        return True

    for attr in ("pac", "itd", "otd", "iet", "oet"):
        value = getattr(meter_data, attr, None)
        if isinstance(value, (int, float)) and value != 0:
            return True

    return False


# ---------------------------------------------------------------------------
# Base coordinator
# ---------------------------------------------------------------------------


class SolplanetCoordinatorBase(DataUpdateCoordinator[dict[str, Any]]):
    """Base for Solplanet endpoint coordinators.

    Subclasses implement `_update()` and declare `default_interval`/`error_interval`.

    All updates run under the runtime's shared lock so only one HTTP/Modbus call hits
    the dongle at a time. After `MAX_FAILED_UPDATES` consecutive failures the
    coordinator switches to `error_interval` until the next successful refresh.
    """

    default_interval: timedelta
    error_interval: timedelta
    MAX_FAILED_UPDATES = 3

    def __init__(self, hass: HomeAssistant, runtime: SolplanetRuntime, name: str) -> None:
        """Initialize the base coordinator."""
        self.runtime = runtime
        self._failed = 0
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}.{name}",
            update_interval=self.default_interval,
        )

    @abstractmethod
    async def _update(self) -> dict[str, Any]:
        """Fetch and return the new data payload."""

    async def _async_update_data(self) -> dict[str, Any]:
        """Acquire the shared lock, dispatch to `_update`, handle error backoff."""
        async with self.runtime.lock:
            try:
                data = await self._update()
            except Exception as err:  # noqa: BLE001
                self._failed += 1
                if self._failed >= self.MAX_FAILED_UPDATES and self.update_interval != self.error_interval:
                    _LOGGER.warning(
                        "%s: %d consecutive failures, switching to error interval %s",
                        self.name,
                        self._failed,
                        self.error_interval,
                    )
                    self.update_interval = self.error_interval
                raise UpdateFailed(str(err)) from err

            if self._failed:
                self._failed = 0
                if self.update_interval != self.default_interval:
                    _LOGGER.info(
                        "%s: recovered, restoring default interval %s",
                        self.name,
                        self.default_interval,
                    )
                    self.update_interval = self.default_interval

            return data


# ---------------------------------------------------------------------------
# Telemetry coordinators
# ---------------------------------------------------------------------------


class SolplanetInverterCoordinator(SolplanetCoordinatorBase):
    """Per-inverter live telemetry (`get_inverter_data`)."""

    default_interval = INVERTER_UPDATE_INTERVAL
    error_interval = INVERTER_ERROR_INTERVAL

    def __init__(self, hass: HomeAssistant, runtime: SolplanetRuntime) -> None:
        """Initialize the inverter coordinator."""
        super().__init__(hass, runtime, name="inverter")

    async def _update(self) -> dict[str, Any]:
        """Fetch live data for every known inverter."""
        previous = (self.data or {}).get(INVERTER_IDENTIFIER, {})
        payload: dict[str, dict] = {}
        any_success = False
        last_err: Exception | None = None

        for isn in self.runtime.inventory.inverter_isns:
            try:
                payload[isn] = {"data": await self.runtime.api.get_inverter_data(isn)}
                any_success = True
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Inverter %s live read failed: %s", isn, err, exc_info=True)
                last_err = err
                # Preserve previous "data" so entities don't flap to Unknown on a single
                # timeout. The base class still escalates after MAX_FAILED_UPDATES.
                if isn in previous:
                    payload[isn] = previous[isn]

        if not any_success and last_err is not None:
            raise last_err

        return {INVERTER_IDENTIFIER: payload}


class SolplanetBatteryCoordinator(SolplanetCoordinatorBase):
    """Per-battery live telemetry (`get_battery_data`)."""

    default_interval = BATTERY_UPDATE_INTERVAL
    error_interval = BATTERY_ERROR_INTERVAL

    def __init__(self, hass: HomeAssistant, runtime: SolplanetRuntime) -> None:
        """Initialize the battery coordinator."""
        super().__init__(hass, runtime, name="battery")

    async def _update(self) -> dict[str, Any]:
        """Fetch live battery data for every known storage inverter."""
        previous = (self.data or {}).get(BATTERY_IDENTIFIER, {})
        payload: dict[str, dict] = {}
        any_success = False
        last_err: Exception | None = None

        for isn in self.runtime.inventory.battery_isns:
            try:
                payload[isn] = {"data": await self.runtime.api.get_battery_data(isn)}
                any_success = True
            except NotImplementedError:
                _LOGGER.info("Battery operations not supported (V1 protocol)")
                return {BATTERY_IDENTIFIER: {}}
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Battery %s live read failed: %s", isn, err, exc_info=True)
                last_err = err
                if isn in previous:
                    payload[isn] = previous[isn]

        if not any_success and last_err is not None:
            raise last_err

        return {BATTERY_IDENTIFIER: payload}


class SolplanetMeterCoordinator(SolplanetCoordinatorBase):
    """Live meter values.

    V2 app protocol: `POST get_meter_data_req`. The response has no SN selector, so
    live values attach to `inventory.app_primary_meter_sn` (the main meter from
    `get_app_dev_info_req`).

    V1 / legacy V2 fallback: `get_meter_data()` + `get_meter_info()`.
    """

    default_interval = METER_UPDATE_INTERVAL
    error_interval = METER_ERROR_INTERVAL

    def __init__(self, hass: HomeAssistant, runtime: SolplanetRuntime) -> None:
        """Initialize the meter coordinator."""
        super().__init__(hass, runtime, name="meter")

    async def _update(self) -> dict[str, Any]:
        """Fetch live meter values via the appropriate protocol."""
        runtime = self.runtime
        if runtime.api.version == "v2" and not runtime.inventory.has_legacy_meter:
            return await self._update_v2()
        return await self._update_legacy()

    async def _update_v2(self) -> dict[str, Any]:
        runtime = self.runtime
        rsp = await runtime.api.client.post("getting.cgi", {"cmd": "get_meter_data_req"})
        if not isinstance(rsp, dict) or rsp.get("status") != 200:
            raise RuntimeError(f"Unexpected response from get_meter_data_req: {rsp}")

        app_data = rsp.get("payload") or {}
        target_sn = runtime.inventory.app_primary_meter_sn or (
            runtime.inventory.meter_sns[0] if runtime.inventory.meter_sns else None
        )
        if not target_sn:
            return {METER_IDENTIFIER: {}}
        return {METER_IDENTIFIER: {target_sn: {"app_data": app_data}}}

    async def _update_legacy(self) -> dict[str, Any]:
        runtime = self.runtime
        data = await runtime.api.get_meter_data()
        info = await runtime.api.get_meter_info()
        sn = getattr(info, "sn", None) or (
            runtime.inventory.meter_sns[0] if runtime.inventory.meter_sns else None
        )
        if not sn:
            return {METER_IDENTIFIER: {}}
        return {METER_IDENTIFIER: {sn: {"data": data, "info": info}}}


class SolplanetDongleCoordinator(SolplanetCoordinatorBase):
    """V2 dongle diagnostics (info + network + warnings)."""

    default_interval = DONGLE_UPDATE_INTERVAL
    error_interval = DONGLE_ERROR_INTERVAL

    def __init__(self, hass: HomeAssistant, runtime: SolplanetRuntime) -> None:
        """Initialize the dongle coordinator."""
        super().__init__(hass, runtime, name="dongle")

    async def _update(self) -> dict[str, Any]:
        runtime = self.runtime
        dongle_id = runtime.inventory.dongle_id
        if dongle_id is None:
            return {DONGLE_IDENTIFIER: {}}

        # Primary device info is required; network/warnings are best-effort. Warnings
        # commonly returns 404 (no active warnings) and that is not a failure.
        previous = (self.data or {}).get(DONGLE_IDENTIFIER, {}).get(dongle_id, {})
        dongle_info = await runtime.api.client.get("getdev.cgi")

        try:
            network = await runtime.api.client.get("wlanget.cgi?info=2")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Dongle network info read failed: %s", err, exc_info=True)
            network = previous.get("network")

        try:
            warnings = await runtime.api.client.get("getdevdata.cgi?device=1")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Dongle warnings read failed: %s", err, exc_info=True)
            warnings = previous.get("warnings")

        return {
            DONGLE_IDENTIFIER: {
                dongle_id: {"data": dongle_info, "network": network, "warnings": warnings},
            }
        }


# ---------------------------------------------------------------------------
# Config (slow) coordinator
# ---------------------------------------------------------------------------


class SolplanetConfigCoordinator(SolplanetCoordinatorBase):
    """Slowly-changing config and metadata.

    Owns `info`, `more_settings`, `schedule`, `work_modes`, `app_info`, `meter_req`,
    and `meter_power`. These rarely change and are expensive to fetch; the cadence is
    long but writes from the UI trigger an immediate refresh via `async_request_refresh`.
    """

    default_interval = CONFIG_UPDATE_INTERVAL
    error_interval = CONFIG_ERROR_INTERVAL

    def __init__(self, hass: HomeAssistant, runtime: SolplanetRuntime) -> None:
        """Initialize the config coordinator."""
        super().__init__(hass, runtime, name="config")

    async def _update(self) -> dict[str, Any]:
        runtime = self.runtime
        previous = self.data or {}

        inverter_payload = await self._update_inverter_config(previous)
        battery_payload = await self._update_battery_config(previous)
        meter_payload = await self._update_meter_config(previous)

        return {
            INVERTER_IDENTIFIER: inverter_payload,
            BATTERY_IDENTIFIER: battery_payload,
            METER_IDENTIFIER: meter_payload,
        }

    # -- Inverter config --------------------------------------------------

    async def _update_inverter_config(self, previous: dict) -> dict[str, dict]:
        runtime = self.runtime
        prev = previous.get(INVERTER_IDENTIFIER, {}) if isinstance(previous, dict) else {}

        # Inverter power control register (offset 200).
        power_on: bool | None = None
        try:
            reg = await runtime.api.modbus_read_holding_registers(
                data_type=DataType.U16,
                device_address=3,
                register_address=40201,
                register_count=1,
            )
            if isinstance(reg, list):
                reg = reg[0] if reg else None
            if isinstance(reg, int):
                power_on = reg == 1
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Inverter power register read failed: %s", err, exc_info=True)

        payload: dict[str, dict] = {}
        info = runtime.inventory.inverters_info
        for idx, isn in enumerate(runtime.inventory.inverter_isns):
            inv_info = info.inv[idx] if info and idx < len(info.inv) else None
            prev_entry = prev.get(isn, {})
            payload[isn] = {
                "info": inv_info,
                "more_settings": (
                    {"power_on": power_on}
                    if power_on is not None
                    else prev_entry.get("more_settings", {})
                ),
            }
        return payload

    # -- Battery config ---------------------------------------------------

    async def _update_battery_config(self, previous: dict) -> dict[str, dict]:
        runtime = self.runtime
        prev = previous.get(BATTERY_IDENTIFIER, {}) if isinstance(previous, dict) else {}

        if not runtime.inventory.battery_isns:
            return {}

        # Shared across batteries: schedule and the 4-register More Settings block.
        schedule: dict | None = None
        try:
            schedule = await runtime.api.get_schedule()
        except NotImplementedError:
            return {}
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Schedule read failed: %s", err, exc_info=True)

        more_settings: dict | None = None
        try:
            regs = await runtime.api.modbus_read_holding_registers(
                data_type=DataType.U16,
                device_address=3,
                register_address=41501,
                register_count=4,
            )
            if isinstance(regs, list) and len(regs) >= 4:
                more_settings = {
                    "power_on": int(regs[0] or 0) == 1,
                    # Sleep flag semantics: 0 = enabled, 1 = disabled
                    "sleep_enabled": int(regs[1] or 0) == 0,
                    "led_color_index": int(regs[2] or 0),
                    "led_brightness": int(regs[3] or 0),
                }
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Battery More Settings register read failed: %s", err, exc_info=True)

        payload: dict[str, dict] = {}
        for isn in runtime.inventory.battery_isns:
            prev_entry = prev.get(isn, {})
            try:
                info = await runtime.api.get_battery_info(isn)
            except NotImplementedError:
                return {}
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Battery %s info read failed: %s", isn, err, exc_info=True)
                # Preserve previous info; a single failure should not delete the entry.
                info = prev_entry.get("info")

            work_modes: dict
            if info is not None and getattr(info, "type", None) is not None:
                work_modes = {
                    "all": BatteryWorkModes().get_all_modes(info.type, info.mod_r),
                    "selected": BatteryWorkModes().get_mode(info.type, info.mod_r),
                }
            else:
                work_modes = prev_entry.get("work_modes", {"all": [], "selected": None})

            payload[isn] = {
                "info": info,
                "work_modes": work_modes,
                "schedule": schedule or prev_entry.get("schedule", {}),
                "more_settings": more_settings or prev_entry.get("more_settings", {}),
            }
        return payload

    # -- Meter config -----------------------------------------------------

    async def _update_meter_config(self, previous: dict) -> dict[str, dict]:
        runtime = self.runtime
        prev = previous.get(METER_IDENTIFIER, {}) if isinstance(previous, dict) else {}

        if not runtime.inventory.meter_sns:
            return {}

        # V1 / V2 legacy: only `info` is config-ish; the live `data` lives in MeterCoord.
        if runtime.inventory.has_legacy_meter:
            sn = runtime.inventory.meter_sns[0]
            prev_entry = prev.get(sn, {})
            try:
                info = await runtime.api.get_meter_info()
                return {sn: {"info": info}}
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Legacy meter info read failed: %s", err, exc_info=True)
                return {sn: prev_entry} if prev_entry else {}

        # V2 app protocol: inventory + meter_req + meter_power.
        payload: dict[str, dict] = {sn: dict(prev.get(sn, {})) for sn in runtime.inventory.meter_sns}

        try:
            rsp = await runtime.api.client.post(
                "getting.cgi",
                {"cmd": "get_app_dev_info_req", "payload": {"type": [4]}},
            )
            if isinstance(rsp, dict) and rsp.get("status") == 200:
                p = rsp.get("payload") or {}
                for meter in [*(p.get("mainMeter") or []), *(p.get("subMeter") or [])]:
                    if isinstance(meter, dict):
                        sn = meter.get("sn") or f"addr_{meter.get('address')}"
                        if sn in payload:
                            payload[sn]["app_info"] = meter
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("App meter info read failed: %s", err, exc_info=True)

        target_sn = runtime.inventory.app_primary_meter_sn or (
            runtime.inventory.meter_sns[0] if runtime.inventory.meter_sns else None
        )

        if target_sn:
            try:
                rsp = await runtime.api.client.post("getting.cgi", {"cmd": "get_meter_req"})
                if isinstance(rsp, dict) and rsp.get("status") == 200:
                    payload.setdefault(target_sn, {})["meter_req"] = rsp.get("payload") or {}
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("get_meter_req failed: %s", err, exc_info=True)

            try:
                rsp = await runtime.api.client.post("getting.cgi", {"cmd": "get_meter_power_req"})
                if isinstance(rsp, dict) and rsp.get("status") == 200:
                    payload.setdefault(target_sn, {})["meter_power"] = rsp.get("payload") or {}
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("get_meter_power_req failed: %s", err, exc_info=True)

        return payload
