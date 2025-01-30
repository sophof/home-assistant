"""Numeric derivative of data coming from a source sensor over time."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, DecimalException
import logging

import voluptuous as vol

from homeassistant.components.sensor import (
    ATTR_STATE_CLASS,
    PLATFORM_SCHEMA as SENSOR_PLATFORM_SCHEMA,
    RestoreSensor,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_NAME,
    CONF_SOURCE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTime,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.device import async_device_info_to_link_from_entity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import (
    CONF_ROUND_DIGITS,
    CONF_STALE_TIME,
    CONF_TIME_WINDOW,
    CONF_UNIT,
    CONF_UNIT_PREFIX,
    CONF_UNIT_TIME,
)

_LOGGER = logging.getLogger(__name__)

ATTR_SOURCE_ID = "source"

# SI Metric prefixes
UNIT_PREFIXES = {
    None: 1,
    "n": 1e-9,
    "µ": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
}

# SI Time prefixes
UNIT_TIME = {
    UnitOfTime.SECONDS: 1,
    UnitOfTime.MINUTES: 60,
    UnitOfTime.HOURS: 60 * 60,
    UnitOfTime.DAYS: 24 * 60 * 60,
}

DEFAULT_ROUND = 3
DEFAULT_TIME_WINDOW = 0
DEFAULT_STALE_TIME = 0

PLATFORM_SCHEMA = SENSOR_PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_NAME): cv.string,
        vol.Required(CONF_SOURCE): cv.entity_id,
        vol.Optional(CONF_ROUND_DIGITS, default=DEFAULT_ROUND): vol.Coerce(int),
        vol.Optional(CONF_UNIT_PREFIX, default=None): vol.In(UNIT_PREFIXES),
        vol.Optional(CONF_UNIT_TIME, default=UnitOfTime.HOURS): vol.In(UNIT_TIME),
        vol.Optional(CONF_UNIT): cv.string,
        vol.Optional(CONF_STALE_TIME, default=DEFAULT_STALE_TIME): cv.time_period,
        vol.Optional(CONF_TIME_WINDOW, default=DEFAULT_TIME_WINDOW): cv.time_period,
        vol.Optional(CONF_UNIT_TIME, default=UnitOfTime.HOURS): vol.In(UNIT_TIME),
    }
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Initialize Derivative config entry."""
    registry = er.async_get(hass)
    # Validate + resolve entity registry id to entity_id
    source_entity_id = er.async_validate_entity_id(
        registry, config_entry.options[CONF_SOURCE]
    )

    device_info = async_device_info_to_link_from_entity(
        hass,
        source_entity_id,
    )

    if (unit_prefix := config_entry.options.get(CONF_UNIT_PREFIX)) == "none":
        # Before we had support for optional selectors, "none" was used for selecting nothing
        unit_prefix = None

    derivative_sensor = DerivativeSensor(
        name=config_entry.title,
        round_digits=int(config_entry.options[CONF_ROUND_DIGITS]),
        source_entity=source_entity_id,
        stale_time=cv.time_period_dict(
            config_entry.options.get(CONF_STALE_TIME, {"seconds": 0})
        ),
        time_window=cv.time_period_dict(config_entry.options[CONF_TIME_WINDOW]),
        unique_id=config_entry.entry_id,
        unit_of_measurement=None,
        unit_prefix=unit_prefix,
        unit_time=config_entry.options[CONF_UNIT_TIME],
        device_info=device_info,
    )

    async_add_entities([derivative_sensor])


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the derivative sensor."""
    derivative = DerivativeSensor(
        name=config.get(CONF_NAME),
        round_digits=config[CONF_ROUND_DIGITS],
        source_entity=config[CONF_SOURCE],
        stale_time=config[CONF_STALE_TIME],
        time_window=config[CONF_TIME_WINDOW],
        unit_of_measurement=config.get(CONF_UNIT),
        unit_prefix=config[CONF_UNIT_PREFIX],
        unit_time=config[CONF_UNIT_TIME],
        unique_id=None,
    )

    async_add_entities([derivative])


class DerivativeSensor(RestoreSensor, SensorEntity):
    """Representation of a derivative sensor."""

    _attr_translation_key = "derivative"
    _attr_should_poll = False

    def __init__(
        self,
        *,
        name: str | None,
        round_digits: int,
        source_entity: str,
        stale_time: timedelta | None,
        time_window: timedelta,
        unit_of_measurement: str | None,
        unit_prefix: str | None,
        unit_time: UnitOfTime,
        unique_id: str | None,
        device_info: DeviceInfo | None = None,
    ) -> None:
        """Initialize the derivative sensor."""
        self._attr_unique_id = unique_id
        self._attr_device_info = device_info
        self._sensor_source_id = source_entity
        self._round_digits = round_digits
        self._attr_native_value = round(Decimal(0), round_digits)
        # List of tuples with (timestamp_start, timestamp_end, derivative)
        self._state_list: list[tuple[datetime, datetime, Decimal]] = []

        self._attr_name = name if name is not None else f"{source_entity} derivative"
        self._attr_extra_state_attributes = {ATTR_SOURCE_ID: source_entity}

        if unit_of_measurement is None:
            final_unit_prefix = "" if unit_prefix is None else unit_prefix
            self._unit_template = f"{final_unit_prefix}{{}}/{unit_time}"
            # we postpone the definition of unit_of_measurement to later
            self._attr_native_unit_of_measurement = None
        else:
            self._attr_native_unit_of_measurement = unit_of_measurement

        self._unit_prefix = UNIT_PREFIXES[unit_prefix]
        self._unit_time = UNIT_TIME[unit_time]
        self._time_window = time_window.total_seconds()
        self._stale_time = stale_time.total_seconds()
        self._has_stale_timer = self._stale_time > 0

        self.timer = None

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        restored_data = await self.async_get_last_sensor_data()
        if restored_data:
            self._attr_native_unit_of_measurement = (
                restored_data.native_unit_of_measurement
            )
            try:
                self._attr_native_value = round(
                    Decimal(restored_data.native_value),  # type: ignore[arg-type]
                    self._round_digits,
                )
            except SyntaxError as err:
                _LOGGER.warning("Could not restore last state: %s", err)

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, self._sensor_source_id, self.source_sensor_state_changed
            )
        )

    def update_state(self, begin, end, new_derivative, stale=False):
        """Update the state of the derivative sensor, depending on if it uses a time window or not and handle the stale timer."""
        self._remove_old_derivatives(end)
        self._state_list.append((begin, end, new_derivative))

        # If outside of time window (or there is no window) just report derivative (is the same as modeling it in the window),
        # otherwise take the weighted average with the previous derivatives
        if len(self._state_list) == 1:
            derivative = new_derivative
        else:
            derivative = self.calculate_sma()

        if self.timer and not stale:
            self.timer()  # Cancel stale timer if it is already running

        # Run stale timer if requested unless the total derivative is 0, because the new derivative then wouldn't change anyway
        if self._has_stale_timer and derivative != 0:
            self.timer = async_call_later(
                self.hass, self._stale_time, self.source_sensor_stale_state
            )
            self.async_on_remove(self.timer)

        self._attr_native_value = round(derivative, self._round_digits)
        self.async_write_ha_state()

    @callback
    def source_sensor_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Handle the source sensor state changes."""
        if (
            (old_state := event.data["old_state"]) is None
            or old_state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE)
            or (new_state := event.data["new_state"]) is None
            or new_state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE)
        ):
            return

        if self.native_unit_of_measurement is None:
            unit = new_state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
            self._attr_native_unit_of_measurement = self._unit_template.format(
                "" if unit is None else unit
            )

        new_derivative = self.calculate_single_derivative(old_state, new_state)
        if new_derivative is None:
            return

        self.update_state(
            old_state.last_updated, new_state.last_updated, new_derivative, stale=False
        )

    @callback
    def source_sensor_stale_state(self, now) -> None:
        """Handle the callbacks when the sensor doesn't update within the stale period."""
        begin = self._state_list[-1][1]
        _LOGGER.debug(
            "%s has not updated in %s, setting derivative to 0",
            self._sensor_source_id,
            now - begin,
        )
        self.update_state(begin, now, Decimal(0), stale=True)

    def calculate_single_derivative(self, old_state, new_state) -> Decimal | None:
        """Calculate the derivative based on old and new state and an assumed linear slope."""
        try:
            elapsed_time = (
                new_state.last_updated - old_state.last_updated
            ).total_seconds()
            delta_value = Decimal(new_state.state) - Decimal(old_state.state)
            derivative = (
                delta_value
                / Decimal(elapsed_time)
                / Decimal(self._unit_prefix)
                * Decimal(self._unit_time)
            )

        except ValueError as err:
            _LOGGER.warning("While calculating derivative: %s", err)
        except DecimalException as err:
            _LOGGER.warning(
                "Invalid state (%s > %s): %s", old_state.state, new_state.state, err
            )
        except AssertionError as err:
            _LOGGER.error("Could not calculate derivative: %s", err)

        # For total inreasing sensors, the value is expected to continuously increase.
        # A negative derivative for a total increasing sensor likely indicates the
        # sensor has been reset. To prevent inaccurate data, discard this sample.
        if (
            new_state.attributes.get(ATTR_STATE_CLASS)
            == SensorStateClass.TOTAL_INCREASING
            and derivative < 0
        ):
            return None

        return derivative

    def calculate_sma(self) -> Decimal:
        """Calculate the Simple Moving Average of the derivative. The time window is assumed to end with the last derivative in the list."""
        derivative = Decimal("0.00")
        for start, end, value in self._state_list:
            weight = self.calculate_weight(start, end, self._state_list[-1][1])
            derivative = derivative + (value * Decimal(weight))
        return derivative

    def _remove_old_derivatives(self, current_time: datetime) -> None:
        """Remove derivatives that are outside the time window."""
        window_start = current_time - timedelta(seconds=self._time_window)
        self._state_list = [
            (start, end, value)
            for start, end, value in self._state_list
            if end >= window_start
        ]

    def calculate_weight(self, start: datetime, end: datetime, now: datetime) -> float:
        """Calculate the weight for a derivative based on its time range."""
        window_start = now - timedelta(seconds=self._time_window)
        if (
            start < window_start
        ):  # Start time is before the start of the window, only calculate the weight for the part inside the window
            weight = (end - window_start).total_seconds() / self._time_window
        else:
            weight = (end - start).total_seconds() / self._time_window
        return weight
