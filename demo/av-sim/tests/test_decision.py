from control.controller import build_control_command
from planning.decision_maker import choose_maneuver


def test_decision_stops_for_close_obstacle() -> None:
    assert choose_maneuver(lane_offset_m=0.0, obstacle_distance_m=2.0) == "emergency_stop"


def test_controller_brakes_for_unsafe_path() -> None:
    command = build_control_command(
        speed_mps=10.0,
        obstacle_distance_m=2.0,
        lane_offset_m=0.0,
    )

    assert command["brake"] == 1.0
    assert command["maneuver"] == "emergency_stop"
