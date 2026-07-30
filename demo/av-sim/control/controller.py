"""Vehicle command generation."""

from planning.decision_maker import choose_maneuver

from control.safety_check import is_path_safe


def build_control_command(
    speed_mps: float,
    obstacle_distance_m: float,
    lane_offset_m: float,
) -> dict[str, float | str]:
    """Build a simplified steering, throttle, and brake command."""
    maneuver = choose_maneuver(lane_offset_m, obstacle_distance_m)

    if not is_path_safe(speed_mps, obstacle_distance_m):
        return {
            "maneuver": "emergency_stop",
            "steering": 0.0,
            "throttle": 0.0,
            "brake": 1.0,
        }

    steering = 0.0
    if maneuver == "steer_left":
        steering = -0.3
    elif maneuver == "steer_right":
        steering = 0.3

    return {
        "maneuver": maneuver,
        "steering": steering,
        "throttle": 0.4,
        "brake": 0.0,
    }
