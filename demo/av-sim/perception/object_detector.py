"""Obstacle detection helpers."""


def nearest_obstacle_distance(distances_m: list[float]) -> float | None:
    """Return the nearest valid obstacle distance."""
    valid_distances = [distance for distance in distances_m if distance >= 0]
    if not valid_distances:
        return None
    return min(valid_distances)


def has_close_obstacle(distances_m: list[float], threshold_m: float = 8.0) -> bool:
    """Return whether an obstacle is inside the configured threshold."""
    nearest = nearest_obstacle_distance(distances_m)
    return nearest is not None and nearest < threshold_m
