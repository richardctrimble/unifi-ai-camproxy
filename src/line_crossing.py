"""
LineCrossingDetector — checks if a tracked object's centroid
has crossed a user-defined virtual line between frames.

Lines are defined in config as normalised 0-1 coordinates in
screen space — (0, 0) is the top-left corner, (1, 1) is
bottom-right:

  lines:
    - name: "EntryLine"
      x1: 0.5  y1: 0.0
      x2: 0.5  y2: 1.0
      direction: "both"
      # direction options:
      #   both           — any crossing
      #   left_to_right  — object's centroid moved in +x
      #   right_to_left  — object's centroid moved in -x
      #   top_to_bottom  — object's centroid moved in +y (down the screen)
      #   bottom_to_top  — object's centroid moved in -y (up the screen)

Direction is derived from the motion vector of the centroid between
the two frames, which makes it independent of the order in which the
user drew the line endpoints. This matches the natural mental model
("an object walking left-to-right across this line") rather than
leaking the line's internal orientation into the filter.
"""

import logging
from typing import Optional


def _cross_product(o, a, b):
    """Z-component of (a-o) × (b-o)."""
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _segments_intersect(p1, p2, p3, p4) -> bool:
    """True if segment p1-p2 intersects segment p3-p4."""
    d1 = _cross_product(p3, p4, p1)
    d2 = _cross_product(p3, p4, p2)
    d3 = _cross_product(p1, p2, p3)
    d4 = _cross_product(p1, p2, p4)

    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
       ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True

    return False


class VirtualLine:
    def __init__(self, cfg: dict):
        self.name = cfg["name"]
        self.p1 = (cfg["x1"], cfg["y1"])
        self.p2 = (cfg["x2"], cfg["y2"])
        self.direction = cfg.get("direction", "both")

    def check_crossing(self, prev_centroid, curr_centroid) -> Optional[str]:
        """
        Returns the line name if the centroid path crosses this line
        AND the motion direction matches the configured filter.
        """
        if not _segments_intersect(prev_centroid, curr_centroid, self.p1, self.p2):
            return None

        if self.direction == "both":
            return self.name

        dx = curr_centroid[0] - prev_centroid[0]
        dy = curr_centroid[1] - prev_centroid[1]

        if self.direction == "left_to_right" and dx > 0:
            return self.name
        if self.direction == "right_to_left" and dx < 0:
            return self.name
        if self.direction == "top_to_bottom" and dy > 0:
            return self.name
        if self.direction == "bottom_to_top" and dy < 0:
            return self.name

        return None


class LineCrossingDetector:
    def __init__(self, lines_config: list, logger: logging.Logger):
        self.logger = logger
        self.lines = [VirtualLine(cfg) for cfg in lines_config]
        if self.lines:
            self.logger.info(f"Loaded {len(self.lines)} virtual line(s): "
                             f"{[l.name for l in self.lines]}")

    def check(self, prev_centroid: tuple, curr_centroid: tuple) -> Optional[str]:
        """
        Call this with the object's previous and current centroid.
        Returns the name of the first crossed line, or None.
        """
        for line in self.lines:
            crossed = line.check_crossing(prev_centroid, curr_centroid)
            if crossed:
                self.logger.info(f"Line crossing detected: {crossed}")
                return crossed
        return None
