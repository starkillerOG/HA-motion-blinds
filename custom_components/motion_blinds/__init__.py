"""The motion_blinds component."""
import asyncio
from asyncio import TimeoutError as AsyncioTimeoutError
from datetime import timedelta
import logging
from socket import timeout

from motionblinds import MotionMulticast
import voluptuous as vol

from homeassistant import config_entries, core
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_API_KEY,
    CONF_HOST,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.dispatcher import dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    ATTR_ABSOLUTE_POSITION,
    ATTR_WIDTH,
    DOMAIN,
    KEY_COORDINATOR,
    KEY_GATEWAY,
    KEY_MULTICAST_LISTENER,
    MANUFACTURER,
    MOTION_PLATFORMS,
    SERVICE_SET_ABSOLUTE_POSITION,
)
from .gateway import ConnectMotionGateway

_LOGGER = logging.getLogger(__name__)

CALL_SCHEMA = vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.comp_entity_ids})

SET_ABSOLUTE_POSITION_SCHEMA = CALL_SCHEMA.extend({
        vol.Required(ATTR_ABSOLUTE_POSITION): vol.All(cv.positive_int, vol.Range(max=100)),
        vol.Optional(ATTR_WIDTH): vol.All(cv.positive_int, vol.Range(max=100))
    }
)

SERVICE_TO_METHOD = {
    SERVICE_SET_ABSOLUTE_POSITION: {
        "method": SERVICE_SET_ABSOLUTE_POSITION,
        "schema": SET_ABSOLUTE_POSITION_SCHEMA,
    }
}


def setup(hass: core.HomeAssistant, config: dict):
    """Set up the Motion Blinds component."""

    def service_handler(service):
        method = SERVICE_TO_METHOD.get(service.service)
        data = service.data.copy()
        data["method"] = method["method"]
        dispatcher_send(hass, DOMAIN, data)

    for service in SERVICE_TO_METHOD:
        schema = SERVICE_TO_METHOD[service]["schema"]
        hass.services.register(DOMAIN, service, service_handler, schema=schema)

    return True


async def async_setup_entry(
    hass: core.HomeAssistant, entry: config_entries.ConfigEntry
):
    """Set up the motion_blinds components from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    host = entry.data[CONF_HOST]
    key = entry.data[CONF_API_KEY]

    # Create multicast Listener
    multicast = hass.data[DOMAIN].setdefault(
        KEY_MULTICAST_LISTENER,
        MotionMulticast(),
    )

    if len(hass.data[DOMAIN]) == 1:
        # start listining for local pushes (only once)
        await hass.async_add_executor_job(multicast.Start_listen)

        # register stop callback to shutdown listining for local pushes
        def stop_motion_multicast(event):
            """Stop multicast thread."""
            _LOGGER.debug("Shutting down Motion Listener")
            multicast.Stop_listen()

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_motion_multicast)

    # Connect to motion gateway
    connect_gateway_class = ConnectMotionGateway(hass, multicast)
    if not await connect_gateway_class.async_connect_gateway(host, key):
        raise ConfigEntryNotReady
    motion_gateway = connect_gateway_class.gateway_device

    def update_gateway():
        """Call all updates using one async_add_executor_job."""
        motion_gateway.Update()
        for blind in motion_gateway.device_list.values():
            blind.Update()

    async def async_update_data():
        """Fetch data from the gateway and blinds."""
        try:
            await hass.async_add_executor_job(update_gateway)
        except timeout as socket_timeout:
            raise AsyncioTimeoutError from socket_timeout

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        # Name of the data. For logging purposes.
        name=entry.title,
        update_method=async_update_data,
        # Polling interval. Will only be polled if there are subscribers.
        update_interval=timedelta(seconds=900),
    )

    # Fetch initial data so we have data when entities subscribe
    await coordinator.async_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        KEY_GATEWAY: motion_gateway,
        KEY_COORDINATOR: coordinator,
    }

    device_registry = await dr.async_get_registry(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, motion_gateway.mac)},
        identifiers={(DOMAIN, entry.unique_id)},
        manufacturer=MANUFACTURER,
        name=entry.title,
        model="Wi-Fi bridge",
        sw_version=motion_gateway.protocol,
    )

    for component in MOTION_PLATFORMS:
        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(entry, component)
        )

    return True


async def async_unload_entry(
    hass: core.HomeAssistant, config_entry: config_entries.ConfigEntry
):
    """Unload a config entry."""
    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(config_entry, component)
                for component in MOTION_PLATFORMS
            ]
        )
    )

    if unload_ok:
        hass.data[DOMAIN].pop(config_entry.entry_id)

    if len(hass.data[DOMAIN]) == 1:
        # No motion gateways left, stop Motion multicast
        _LOGGER.debug("Shutting down Motion Listener")
        multicast = hass.data[DOMAIN].pop(KEY_MULTICAST_LISTENER)
        await hass.async_add_executor_job(multicast.Stop_listen)

    return unload_ok
