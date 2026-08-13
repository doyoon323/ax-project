"""Driving decision helpers."""

from planning.path_planner import plan_path


def choose_maneuver(lane_offset_m: float, obstacle_distance_m: float) -> str:
    """Choose between an emergency stop and a path correction."""
    if obstacle_distance_m < 5.0:
        return "emergency_stop"
    return plan_path(lane_offset_m)
