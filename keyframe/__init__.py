"""Adaptive-bitrate keyframe planning package.

All public planning functions operate in the integer frame domain.  See
:mod:`keyframe.planner` for the full contracts.
"""

from .planner import (
    KeyframeError,
    align_scene_changes,
    avoid_short,
    estimate_decoder_load,
    insert_idr,
    plan_batch,
    plan_bitrate,
    snap_cuts,
    validate_plan,
)

__all__ = [
    "KeyframeError",
    "align_scene_changes",
    "avoid_short",
    "estimate_decoder_load",
    "insert_idr",
    "plan_batch",
    "plan_bitrate",
    "snap_cuts",
    "validate_plan",
]
