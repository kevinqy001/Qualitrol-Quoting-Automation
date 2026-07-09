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
    # GIS gas-zone assets (2026-07 DMS GIS SLD diagram review).
    "gas_zone_count": "MET_ASSET_COUNT",
    "compartment_count": "MET_ASSET_COUNT",
    "disconnector_count": "MET_ASSET_COUNT",
    "earthing_switch_count": "MET_ASSET_COUNT",
}

# Quantity-rule count field -> drawing asset type(s) (from 14_Drawing_Asset_List).
COUNT_FIELD_TO_ASSET_TYPE = {
    "transformer_count": ["Transformer"],
    "breaker_count": ["Circuit Breaker"],
    "gis_bay_count": ["GIS", "GIS Bay"],
    "panel_count": ["Switchgear Panel", "Switchgear"],
    "generator_count": ["Generator"],
    "motor_count": ["Motor"],
    "sensor_count": ["Sensor", "PD Sensor", "Gas Density Sensor"],
    "bushing_count": ["Bushing"],
    "accessory_count": ["Accessory", "Sensor"],
    # Feeders are the primary sizing basis for DFR/PMU/PQ recorders (1 DAU per
    # feeder-group); buses / PCCs are the fallback when feeders aren't extracted.
    "pcc_count": ["Feeder", "PCC", "Bus"],
    "measurement_point_count": ["Feeder", "Bus", "PCC", "Measurement Point"],
    "feeder_count": ["Feeder"],
    "channel_count": ["Channel"],
    # Extended assets (keep in sync with _VALID_ASSET_TYPES in llm_extract.py).
    "reactor_count": ["Reactor"],
    "transmission_line_count": ["Transmission Line"],
    "line_count": ["Transmission Line", "Feeder"],
    "cable_count": ["Cable"],
    "surge_arrester_count": ["Surge Arrester"],
    "instrument_transformer_count": ["Instrument Transformer"],
    "tap_changer_count": ["Tap Changer"],
    "capacitor_bank_count": ["Capacitor Bank"],
    "cabinet_count": ["Cabinet"],
    # GIS gas-zone assets (from 2026-07 DMS GIS SLD diagram review). A monitored
    # gas zone / compartment drives one WIKA GDHT-20 gas-density sensor
    # (GIS_SF6_001 / QR_SF6_SNS = 1 sensor per zone); disconnector / earthing
    # switch zones inform the UHF protector recommendation.
    "gas_zone_count": ["Gas Compartment", "Gas Density Sensor"],
    "compartment_count": ["Gas Compartment"],
    "disconnector_count": ["Disconnector Switch"],
    "earthing_switch_count": ["Earthing Switch"],
}

# --------------------------------------------------------------------------- #
# Quantity sizing (P1-B): recorder/DAU sizing and fixed-quantity families.
# --------------------------------------------------------------------------- #
# Qualitrol IDM+ DAUs are sized per feeder group: each unit carries a fixed
# analogue-channel budget, and the real BOQ allocates ~12 analogue channels per
# feeder (see quote note "12 analogue channels for each feeder"), so one
# 36-analogue DAU typically covers ~3 feeders. These are editable assumptions.
CHANNELS_PER_FEEDER = 12
CHANNELS_PER_DAU = 36
FEEDERS_PER_DAU = max(1, CHANNELS_PER_DAU // CHANNELS_PER_FEEDER)  # = 3

# count_field substrings that denote a system-level item quoted once per
# substation/system (monitoring software, central gateway, server, licences)
# rather than scaled by a drawing asset count.
FIXED_ONE_COUNT_FIELD_HINTS = (
    "tag_count", "gateway", "site_count", "license", "licence",
    "server", "user_count", "software", "platform",
)

# count fields that should be sized by the recorder/DAU formula (feeder-based).
DAU_SIZED_COUNT_FIELDS = {"channel_count", "feeder_count"}

# --------------------------------------------------------------------------- #
# TAQA / ADNOC MEA configuration ruleset (sourced from CR_MEA_* rules in the
# data package). Drives the Step 2 accessory / panel / software / service
# expansion so the BOQ includes the packaging the real engineered BOQ carries,
# not just the recorders. Editable assumptions.
# --------------------------------------------------------------------------- #
MEA_DAUS_PER_GPS_MASTER = 12       # CR_MEA_06: 1 GPS master per 12 DAU
MEA_ANTENNAS_PER_MASTER = 2        # CR_MEA_06: 2 antennas per master
MEA_EPG_LICENSES_PER_PMU = 4       # CR_MEA_08: 4 EPG licences per PMU device
MEA_DEVICES_PER_PANEL = 4          # CR_MEA_05: max 4x 3U (or 2x 6U) per panel

# Families that are emitted by the MEA expansion pass (accessories / panels /
# network / timing / software / services), NOT by the generic per-family
# matcher — so they are quantified by the ruleset rather than appearing twice.
EXPANSION_FAMILY_IDS = {
    "PF_DAU_REC", "PF_MON_PANEL", "PF_NET_SEC", "PF_TIMING",
    "PF_SW_LIC", "PF_SERVICES", "PF_PANEL_ACC",
}

# Scenario IDs that indicate recorder / DAU scope (used to size accessories).
RECORDER_SCENARIO_IDS = {
    "FMS_001", "DFR_DDR_001", "PMU_001", "WAMS_001", "PQ_CLASSA_001",
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
