"""
Pydantic models for incoming telemetry (Phase 4).

The rules below are the data contract between any data source
(simulator today, ArduPilot SITL or a real drone later) and the pipeline.
Ranges are physical/protocol limits, not the anomaly thresholds:
an injected anomaly (e.g. altitude spike to 45 m, 3 satellites) is still
valid data and must be accepted. Detection happens in Phase 5.
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FlightMode = Literal["rest", "takeoff", "transit_out", "survey", "transit_back", "landing"]

# D0001_A_AM / D0001_C_IDLE  (day 4 digits, drone letter, slot)
MISSION_ID_PATTERN = r"^D\d{4}_[A-Z]_(AM|PM|IDLE)$"


class TelemetryIn(BaseModel):
    """One telemetry row, 1 Hz while flying, every 30 min while resting."""

    model_config = ConfigDict(extra="forbid")  # unknown columns = contract change, reject

    timestamp: datetime
    drone_id: str = Field(pattern=r"^[A-Z]$")
    mission_id: str = Field(pattern=MISSION_ID_PATTERN)
    flight_mode: FlightMode
    battery_vtol_pct: float = Field(ge=0, le=100)
    battery_cruise_pct: float = Field(ge=0, le=100)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    altitude_m: float = Field(ge=0, le=500)
    ground_speed_mps: float = Field(ge=0, le=60)
    satellite_count: int = Field(ge=0, le=64)
    hdop: float = Field(gt=0, le=99.99)


class RejectedRow(BaseModel):
    index: int  # position in the submitted batch (0-based)
    errors: list[str]


class BatchResult(BaseModel):
    received: int
    accepted: int
    rejected: list[RejectedRow]
