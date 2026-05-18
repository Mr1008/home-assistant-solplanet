"""The Solplanet integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv
import homeassistant.helpers.device_registry as dr

from .api_adapter import SolplanetApiAdapter
from .client import SolplanetClient
from .const import (
    BATTERY_IDENTIFIER,
    BATTERY_MANUFACTURER_NAMES,
    BATTERY_MODEL_NAMES,
    CONF_INTERVAL,
    DEFAULT_INTERVAL,
    DOMAIN,
    DONGLE_IDENTIFIER,
    INVERTER_IDENTIFIER,
    MANUFACTURER,
    METER_IDENTIFIER,
    METER_MODEL_NAMES,
)
from .coordinator import (
    SolplanetBatteryCoordinator,
    SolplanetConfigCoordinator,
    SolplanetCoordinatorBase,
    SolplanetDongleCoordinator,
    SolplanetInverterCoordinator,
    SolplanetMeterCoordinator,
    SolplanetRuntime,
)
from .services import async_setup_services

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.BUTTON,
]
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)
_LOGGER = logging.getLogger(__name__)

type SolplanetConfigEntry = ConfigEntry[SolplanetRuntime]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Solplanet integration (services only).

    Config entries are set up in `async_setup_entry`.
    """
    hass.data.setdefault(DOMAIN, {})

    # Register services once for the integration domain.
    await async_setup_services(hass)

    return True


async def _init_optional_coordinator(
    coordinator: SolplanetCoordinatorBase,
) -> SolplanetCoordinatorBase | None:
    """Run a first refresh on an optional coordinator; discard on failure.

    Mirrors Fronius's `_init_optional_coordinator`: lets the integration proceed
    cleanly when a firmware doesn't support a particular endpoint.
    """
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:  # noqa: BLE001
        _LOGGER.info(
            "Optional coordinator %s unavailable, skipping: %s", coordinator.name, err
        )
        return None
    return coordinator


async def async_setup_entry(hass: HomeAssistant, entry: SolplanetConfigEntry) -> bool:
    """Set up Solplanet from a config entry."""
    client = SolplanetClient(entry.data[CONF_HOST], async_get_clientsession(hass))
    try:
        api = await SolplanetApiAdapter.create(client)
    except RuntimeError as e:
        raise ConfigEntryNotReady(str(e)) from e

    _LOGGER.info("Using Solplanet protocol version: %s", api.version)

    runtime = SolplanetRuntime(hass=hass, api=api)
    try:
        await runtime.async_setup()
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(f"Solplanet discovery failed: {err}") from err

    # Construct coordinators against the frozen inventory.
    runtime.inverter = SolplanetInverterCoordinator(hass, runtime)
    runtime.config = SolplanetConfigCoordinator(hass, runtime)
    if runtime.inventory.battery_isns:
        runtime.battery = SolplanetBatteryCoordinator(hass, runtime)
    if runtime.inventory.meter_sns:
        runtime.meter = SolplanetMeterCoordinator(hass, runtime)
    if api.version == "v2" and runtime.inventory.dongle_id is not None:
        runtime.dongle = SolplanetDongleCoordinator(hass, runtime)

    # Required first refreshes: a failure here keeps the entry from setting up.
    await runtime.inverter.async_config_entry_first_refresh()
    await runtime.config.async_config_entry_first_refresh()

    # Optional first refreshes: discard the coordinator on failure so the entry still
    # sets up. This handles V2 firmwares that 404 on `getting.cgi`, missing batteries,
    # and similar partial-support cases.
    if runtime.battery is not None:
        runtime.battery = await _init_optional_coordinator(runtime.battery)
    if runtime.meter is not None:
        runtime.meter = await _init_optional_coordinator(runtime.meter)
    if runtime.dongle is not None:
        runtime.dongle = await _init_optional_coordinator(runtime.dongle)

    entry.runtime_data = runtime
    hass.data[DOMAIN][entry.entry_id] = {"runtime": runtime}

    # Register devices from the inventory + first-refresh data.
    device_registry = dr.async_get(hass)
    config_data = runtime.config.data or {}

    if runtime.dongle is not None:
        dongle_data = runtime.dongle.data or {}
        for dongle_id, entry_data in dongle_data.get(DONGLE_IDENTIFIER, {}).items():
            dongle = entry_data.get("data", {}) if isinstance(entry_data, dict) else {}
            device_registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, f"{DONGLE_IDENTIFIER}_{dongle_id}")},
                name=dongle.get("nam") or "Solplanet Dongle",
                manufacturer=dongle.get("brd") or dongle.get("muf") or MANUFACTURER,
                model=dongle.get("mod") or dongle.get("hw") or "Dongle",
                serial_number=dongle.get("psn") or dongle_id,
                hw_version=dongle.get("hw") or "",
                sw_version=dongle.get("sw") or "",
            )

    for isn, inv_entry in config_data.get(INVERTER_IDENTIFIER, {}).items():
        inverter_info = inv_entry.get("info") if isinstance(inv_entry, dict) else None
        if inverter_info is None:
            continue
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, inverter_info.isn or isn)},
            name=inverter_info.model,
            model=inverter_info.model,
            manufacturer=MANUFACTURER,
            serial_number=inverter_info.isn,
            sw_version=(
                f"Master: {inverter_info.msw}, "
                f"Slave: {inverter_info.ssw}, "
                f"Security: {inverter_info.tsw}"
            ),
        )

    for isn, bat_entry in config_data.get(BATTERY_IDENTIFIER, {}).items():
        battery_info = bat_entry.get("info") if isinstance(bat_entry, dict) else None
        if battery_info is None:
            # Info unavailable (battery unreachable on first refresh); skip device creation.
            # The device will be registered on the next successful config update.
            continue

        # Battery endpoint (device=4) reports `isn` as the inverter serial.
        # Use the nested battery part number as the battery serial when available.
        battery_serial = (
            battery_info.battery.partno
            if battery_info.battery and battery_info.battery.partno
            else battery_info.isn
        )
        battery_manufacturer = (
            BATTERY_MANUFACTURER_NAMES.get(battery_info.muf)
            if battery_info.muf is not None
            else None
        )
        battery_model = (
            BATTERY_MODEL_NAMES.get((battery_info.muf, battery_info.mod))
            if battery_info.muf is not None and battery_info.mod is not None
            else None
        )
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"{BATTERY_IDENTIFIER}_{battery_info.isn or ''}")},
            name=battery_model or "Battery",
            manufacturer=battery_manufacturer,
            model=battery_model,
            serial_number=battery_serial,
            sw_version=battery_info.battery.softwarever if battery_info.battery else "",
            hw_version=battery_info.battery.hardwarever if battery_info.battery else "",
        )

    for meter_sn, meter_entry in config_data.get(METER_IDENTIFIER, {}).items():
        meter_info = meter_entry.get("info") if isinstance(meter_entry, dict) else None
        app_info = meter_entry.get("app_info") if isinstance(meter_entry, dict) else None

        # V2 meters discovered via `getting.cgi`
        if isinstance(app_info, dict):
            serial = app_info.get("sn") or meter_sn
            equip_model_raw = app_info.get("equipModel")
            equip_model = (
                int(equip_model_raw)
                if isinstance(equip_model_raw, int | str) and str(equip_model_raw).isdigit()
                else None
            )
            model_name = METER_MODEL_NAMES.get(equip_model) if equip_model is not None else None
            # Some firmwares report equipModel=255 as "None".
            if equip_model == 255:
                model_name = None
            name_prefix = model_name or "Meter"
            device_registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, f"{METER_IDENTIFIER}_{meter_sn or ''}")},
                name=name_prefix,
                serial_number=serial,
                manufacturer=MANUFACTURER,
                model=model_name or "",
            )
            continue

        if meter_info is not None:
            device_registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, f"{METER_IDENTIFIER}_{meter_sn or ''}")},
                name="Energy meter",
                serial_number=meter_info.sn,
                manufacturer=meter_info.manufactory,
                model=meter_info.name,
            )

    # Do not block setup if the inverter is sleeping or temporarily unreachable.
    # Entities are added regardless and will show `unknown` state until data is available.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: SolplanetConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry."""
    _LOGGER.debug(
        "Migrating configuration from version %s.%s",
        config_entry.version,
        config_entry.minor_version,
    )

    if config_entry.version > 1:
        # This means the user has downgraded from a future version
        return False

    if config_entry.version == 1 and config_entry.minor_version < 2:
        # 1.1 → 1.2: CONF_INTERVAL was added; inject the default for existing entries.
        new_data = {**config_entry.data, CONF_INTERVAL: DEFAULT_INTERVAL}
        hass.config_entries.async_update_entry(
            config_entry, data=new_data, version=1, minor_version=2
        )
        _LOGGER.info("Entry %s migrated to version 1.2.", config_entry.entry_id)

    return True
