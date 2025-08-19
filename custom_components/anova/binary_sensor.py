# homeassistant/components/anova/binary_sensor.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import AnovaConfigEntry, AnovaCoordinator
from .entity import AnovaDescriptionEntity

@dataclass(frozen=True, kw_only=True)
class AnovaBinaryEntityDescription(BinarySensorEntityDescription):
    value_fn: Callable[[object], bool | None]

BINARY_DESCRIPTIONS: list[AnovaBinaryEntityDescription] = [
    AnovaBinaryEntityDescription(
        key="low_water_warning",
        translation_key="low_water_warning",
        value_fn=lambda d: getattr(getattr(d, "raw", {}).get("state", {}).get("nodes", {}).get("lowWater", {}), "get", dict()).__call__("warning", None)
        if isinstance(getattr(d, "raw", None), dict) else getattr(getattr(d, "low_water", None), "warning", None),
    ),
    AnovaBinaryEntityDescription(
        key="low_water_empty",
        translation_key="low_water_empty",
        value_fn=lambda d: getattr(getattr(d, "raw", {}).get("state", {}).get("nodes", {}).get("lowWater", {}), "get", dict()).__call__("empty", None)
        if isinstance(getattr(d, "raw", None), dict) else getattr(getattr(d, "low_water", None), "empty", None),
    ),
]

async def async_setup_entry(
    hass: HomeAssistant,
    entry: AnovaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    anova_data = entry.runtime_data
    for coordinator in anova_data.coordinators:
        setup_coordinator(coordinator, async_add_entities)

def setup_coordinator(
    coordinator: AnovaCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    def _async_listener() -> None:
        if getattr(coordinator, "binary_data_set", False):
            return
        if coordinator.data is None:
            return
        ents: set[AnovaLowWaterBinary] = set()
        for desc in BINARY_DESCRIPTIONS:
            try:
                v = desc.value_fn(coordinator.data.sensor)
            except Exception:
                v = None
            if v is not None:
                ents.add(AnovaLowWaterBinary(coordinator, desc))
        if ents:
            async_add_entities(ents)
            coordinator.binary_data_set = True

    if coordinator.data is not None:
        _async_listener()
    coordinator.async_add_listener(_async_listener)

class AnovaLowWaterBinary(AnovaDescriptionEntity, BinarySensorEntity):
    entity_description: AnovaBinaryEntityDescription

    @property
    def is_on(self) -> bool | None:
        return bool(self.entity_description.value_fn(self.coordinator.data.sensor))
