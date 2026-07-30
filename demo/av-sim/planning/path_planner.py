"""Path planning helpers."""

from perception.lane_detector import is_lane_departure


def plan_path(lane_offset_m: float) -> str:
    """Choose a basic path correction."""
    if not is_lane_departure(lane_offset_m):
        return "keep_lane"
    if lane_offset_m > 0:
        return "steer_left"
    return "steer_right"
