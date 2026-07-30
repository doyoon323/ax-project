"""Lane detection helpers."""


def detect_lane_offset(edge_samples_m: list[float]) -> float:
    """Estimate the vehicle offset from the lane center."""
    if not edge_samples_m:
        return 0.0
    return sum(edge_samples_m) / len(edge_samples_m)


def is_lane_departure(lane_offset_m: float, limit_m: float = 0.4) -> bool:
    """Return whether the vehicle has left the expected lane center."""
    return abs(lane_offset_m) > limit_m
