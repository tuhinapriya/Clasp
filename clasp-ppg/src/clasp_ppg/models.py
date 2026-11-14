from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class PPGFrame:
    """One decoded Polar PMD PPG packet. Its timestamp marks the last sample."""

    sensor_timestamp_ns: int
    received_at: datetime
    samples: tuple[tuple[int, ...], ...]


@dataclass(frozen=True)
class ACCFrame:
    """One Polar PMD accelerometer packet in milli-g; timestamp is sensor-clock ns."""

    sensor_timestamp_ns: int
    received_at: datetime
    samples: tuple[tuple[int, int, int], ...]


@dataclass
class Capture:
    participant_id: str
    session_id: str
    session_type: str
    started_at: datetime
    ended_at: datetime | None = None
    sampling_rate_hz: int = 135
    accelerometer_sampling_rate_hz: int = 52
    channel_names: tuple[str, ...] = ("ppg_1", "ppg_2", "ppg_3", "ambient")
    frames: list[PPGFrame] = field(default_factory=list)
    acc_frames: list[ACCFrame] = field(default_factory=list)
    device_name: str | None = None
    device_address: str | None = None
    stream_errors: dict[str, str] = field(default_factory=dict)
