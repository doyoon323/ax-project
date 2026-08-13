"""Safety rules for generated control commands."""


def minimum_safe_distance(speed_mps: float) -> float:
    """Calculate a simple minimum following distance."""
    return max(3.0, speed_mps * 0.8)


def is_path_safe(speed_mps: float, obstacle_distance_m: float) -> bool:
    """Return whether the nearest obstacle is outside the safe distance."""
    return obstacle_distance_m >= minimum_safe_distance(speed_mps)
