"""Shared lookups linking quantity ``count_field`` names to metrics & assets.

Used by Step 1 (requirement typing) and Step 2 (quantity calculation) so the
two steps agree on how a quantity-basis field maps to a metric and a drawing
asset type.
"""

from __future__ import annotations

# Quantity-rule count field -> standard Metric ID (from 04_Metric_Dictionary).
COUNT_FIELD_TO_METRIC = {
    "transformer_count": "MET_ASSET_COUNT",
    "breaker_count": "MET_BREAKER_COUNT",
    "gis_bay_count": "MET_GIS_BAY_COUNT",
    "panel_count": "MET_PANEL_COUNT",
    "generator_count": "MET_GENERATOR_COUNT",
    "motor_count": "MET_MOTOR_COUNT",
    "sensor_count": "MET_SENSOR_COUNT",
    "bushing_count": "MET_BUSHING_COUNT",
    "channel_count": "MET_CURRENT_CHANNELS",
    "pcc_count": "MET_ASSET_COUNT",
    "measurement_point_count": "MET_ASSET_COUNT",
    "accessory_count": "MET_SENSOR_COUNT",
}

# Quantity-rule count field -> drawing asset type(s) (from 14_Drawing_Asset_List).
COUNT_FIELD_TO_ASSET_TYPE = {
    "transformer_count": ["Transformer"],
    "breaker_count": ["Circuit Breaker"],
    "gis_bay_count": ["GIS", "GIS Bay"],
    "panel_count": ["Switchgear Panel", "Switchgear"],
    "generator_count": ["Generator"],
    "motor_count": ["Motor"],
    "sensor_count": ["Sensor", "PD Sensor"],
    "bushing_count": ["Bushing"],
    "accessory_count": ["Accessory", "Sensor"],
    "pcc_count": ["PCC", "Bus", "Feeder"],
    "measurement_point_count": ["Bus", "PCC", "Measurement Point"],
    "channel_count": ["Channel"],
}

# Count metric -> drawing asset type(s); counts are taken from the drawing
# asset list rather than from unreliable numbers floating in spec text.
METRIC_TO_ASSET_TYPES = {
    "MET_ASSET_COUNT": ["Transformer", "Generator", "Motor"],
    "MET_BREAKER_COUNT": ["Circuit Breaker"],
    "MET_GIS_BAY_COUNT": ["GIS Bay", "GIS"],
    "MET_PANEL_COUNT": ["Switchgear Panel", "Switchgear"],
    "MET_GENERATOR_COUNT": ["Generator"],
    "MET_MOTOR_COUNT": ["Motor"],
    "MET_SENSOR_COUNT": ["PD Sensor", "Sensor"],
    "MET_BUSHING_COUNT": ["Bushing"],
    "MET_CURRENT_CHANNELS": ["Channel"],
    "MET_VOLTAGE_CHANNELS": ["Channel"],
}

# Count-style metrics that should be treated as a "Quantity Basis" requirement.
COUNT_METRIC_IDS = {
    "MET_ASSET_COUNT",
    "MET_BREAKER_COUNT",
    "MET_GIS_BAY_COUNT",
    "MET_PANEL_COUNT",
    "MET_GENERATOR_COUNT",
    "MET_MOTOR_COUNT",
    "MET_SENSOR_COUNT",
    "MET_BUSHING_COUNT",
    "MET_CURRENT_CHANNELS",
    "MET_VOLTAGE_CHANNELS",
}
