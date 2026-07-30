from control.safety_check import is_path_safe, minimum_safe_distance


def test_minimum_safe_distance_has_lower_bound() -> None:
    assert minimum_safe_distance(speed_mps=1.0) >= 3.0


def test_path_is_unsafe_inside_minimum_distance() -> None:
    assert not is_path_safe(speed_mps=10.0, obstacle_distance_m=2.0)
