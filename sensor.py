# homeassistant/components/anova/sensors.py
"""Support for Anova Sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from anova_wifi import APCUpdateSensor  # Beibehalten
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .coordinator import AnovaConfigEntry, AnovaCoordinator
from .entity import AnovaDescriptionEntity


@dataclass(frozen=True, kw_only=True)
class AnovaSensorEntityDescription(SensorEntityDescription):
    """Describes an Anova sensor."""
    value_fn: Callable[[APCUpdateSensor], StateType]


def _get(data: APCUpdateSensor, path: list[str]) -> Any:
    """Safe getter for nested attributes on APCUpdateSensor proxy objects."""
    obj: Any = data
    for key in path:
        obj = getattr(obj, key, None) if not isinstance(obj, dict) else obj.get(key)
        if obj is None:
            return None
    return obj


SENSOR_DESCRIPTIONS: list[AnovaSensorEntityDescription] = [
    # --- Temperatures ---
    AnovaSensorEntityDescription(
        key="water_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        translation_key="water_temperature",
        value_fn=lambda d: d.water_temperature,
    ),
    AnovaSensorEntityDescription(
        key="target_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        translation_key="target_temperature",
        value_fn=lambda d: d.target_temperature,
    ),
    AnovaSensorEntityDescription(
        key="heater_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        translation_key="heater_temperature",
        value_fn=lambda d: d.heater_temperature,
    ),
    AnovaSensorEntityDescription(
        key="triac_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        translation_key="triac_temperature",
        value_fn=lambda d: d.triac_temperature,
    ),

    # --- API Mode (RAW, genau wie gesendet) ---
    # Vorher ENUM mit AnovaMode -> jetzt RAW-String aus API (z.B. "cook")
    AnovaSensorEntityDescription(
        key="mode",
        translation_key="mode",
        # absichtlich KEIN device_class=ENUM, damit beliebige API-Strings durchgehen
        value_fn=lambda d: _get(d, ["raw", "state", "state", "mode"]) or d.mode,
    ),

    # Active Stage Mode (z.B. "running" / "paused")
    AnovaSensorEntityDescription(
        key="active_stage_mode",
        translation_key="active_stage_mode",
        value_fn=lambda d: _get(d, ["raw", "state", "cook", "activeStageMode"]),
    ),

    # --- Cook time (gesamt / verbleibend) als Rohsensoren ---
    AnovaSensorEntityDescription(
        key="cook_time",
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        translation_key="cook_time",
        device_class=SensorDeviceClass.DURATION,
        value_fn=lambda d: d.cook_time,
    ),
    AnovaSensorEntityDescription(
        key="cook_time_remaining",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        translation_key="cook_time_remaining",
        device_class=SensorDeviceClass.DURATION,
        value_fn=lambda d: d.cook_time_remaining,
    ),

    # --- Timer (RAW) ---
    AnovaSensorEntityDescription(
        key="timer_initial",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        translation_key="timer_initial",
        device_class=SensorDeviceClass.DURATION,
        value_fn=lambda d: _get(d, ["raw", "state", "nodes", "timer", "initial"]),
    ),
    AnovaSensorEntityDescription(
        key="timer_mode",
        translation_key="timer_mode",
        value_fn=lambda d: _get(d, ["raw", "state", "nodes", "timer", "mode"]),
    ),
    AnovaSensorEntityDescription(
        key="timer_started_at",
        translation_key="timer_started_at",
        value_fn=lambda d: _get(d, ["raw", "state", "nodes", "timer", "startedAtTimestamp"]),
    ),

    # --- Diagnostics ---
    AnovaSensorEntityDescription(
        key="firmware_version",
        translation_key="firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _get(d, ["raw", "state", "systemInfo", "firmwareVersion"]),
    ),
    AnovaSensorEntityDescription(
        key="hardware_version",
        translation_key="hardware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _get(d, ["raw", "state", "systemInfo", "hardwareVersion"]),
    ),
    AnovaSensorEntityDescription(
        key="online",
        translation_key="online",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _get(d, ["raw", "state", "systemInfo", "online"]),
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AnovaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Anova device."""
    anova_data = entry.runtime_data
    for coordinator in anova_data.coordinators:
        setup_coordinator(coordinator, async_add_entities)


def setup_coordinator(
    coordinator: AnovaCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up an individual Anova Coordinator."""

    def _async_sensor_listener() -> None:
        """Listen for new sensor data and add sensors if they did not exist."""
        if not coordinator.sensor_data_set and coordinator.data is not None:
            valid_entities: set[AnovaSensor] = set()
            for description in SENSOR_DESCRIPTIONS:
                try:
                    value = description.value_fn(coordinator.data.sensor)
                except Exception:  # defensive: einzelne Felder fehlen bei alten Geräten
                    value = None
                if value is not None:
                    valid_entities.add(AnovaSensor(coordinator, description))
            async_add_entities(valid_entities)
            coordinator.sensor_data_set = True

    if coordinator.data is not None:
        _async_sensor_listener()
    coordinator.async_add_listener(_async_sensor_listener)


class AnovaSensor(AnovaDescriptionEntity, SensorEntity):
    """A sensor using Anova coordinator."""

    entity_description: AnovaSensorEntityDescription

    @property
    def native_value(self) -> StateType:
        """Return the state."""
        return self.entity_description.value_fn(self.coordinator.data.sensor)
