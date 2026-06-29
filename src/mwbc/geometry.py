from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Size:
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class Point:
    x: int
    y: int


def clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, value))


def should_activate(edge: str, point: Point, size: Size, threshold: int) -> bool:
    if edge == "right":
        return point.x >= size.width - threshold
    if edge == "left":
        return point.x <= threshold - 1
    if edge == "bottom":
        return point.y >= size.height - threshold
    if edge == "top":
        return point.y <= threshold - 1
    return False


def entry_position(edge: str, local_point: Point, local_size: Size, remote_size: Size) -> Point:
    if edge in {"left", "right"}:
        scaled_y = round(local_point.y / max(1, local_size.height - 1) * max(1, remote_size.height - 1))
        x = 1 if edge == "right" else remote_size.width - 2
        return Point(clamp(x, 0, remote_size.width - 1), clamp(scaled_y, 0, remote_size.height - 1))

    scaled_x = round(local_point.x / max(1, local_size.width - 1) * max(1, remote_size.width - 1))
    y = 1 if edge == "bottom" else remote_size.height - 2
    return Point(clamp(scaled_x, 0, remote_size.width - 1), clamp(y, 0, remote_size.height - 1))


def should_exit(edge: str, remote_point: Point, delta: Point, remote_size: Size) -> bool:
    if edge == "right":
        return remote_point.x <= 0 and delta.x < 0
    if edge == "left":
        return remote_point.x >= remote_size.width - 1 and delta.x > 0
    if edge == "bottom":
        return remote_point.y <= 0 and delta.y < 0
    if edge == "top":
        return remote_point.y >= remote_size.height - 1 and delta.y > 0
    return False


def local_exit_position(edge: str, remote_point: Point, local_size: Size, remote_size: Size) -> Point:
    if edge in {"left", "right"}:
        scaled_y = round(remote_point.y / max(1, remote_size.height - 1) * max(1, local_size.height - 1))
        x = local_size.width - 3 if edge == "right" else 2
        return Point(clamp(x, 0, local_size.width - 1), clamp(scaled_y, 0, local_size.height - 1))

    scaled_x = round(remote_point.x / max(1, remote_size.width - 1) * max(1, local_size.width - 1))
    y = local_size.height - 3 if edge == "bottom" else 2
    return Point(clamp(scaled_x, 0, local_size.width - 1), clamp(y, 0, local_size.height - 1))

