"""Red tests for the keyframe planner stubs.

Every test calls at least one public planner function.  The initial
implementation raises ``NotImplementedError`` for all of them, so the whole
suite is red without any collection or import error.
"""

import pytest

from keyframe.planner import (
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


def _seg(start, end, force_idr=False):
    """Build a segment using the documented three-field shape."""
    return {"start": start, "end": end, "force_idr": force_idr}


def _batch_config(**overrides):
    """Build a valid batch config, with optional overrides."""
    config = {
        "gop": 30,
        "max_interval": 30,
        "min_frames": 1,
        "total_bitrate": 900,
    }
    config.update(overrides)
    return config


def test_cut_snaps_forward_to_next_gop_boundary():
    segments = [_seg(0, 95), _seg(95, 200)]

    snapped = snap_cuts(segments, gop=100)

    assert [(seg["start"], seg["end"]) for seg in snapped] == [
        (0, 100),
        (100, 200),
    ]
    # The input plan must not be mutated.
    assert segments[0]["end"] == 95


def test_idr_spacing_uses_frame_counts_not_fixed_seconds():
    # Frame 33 is an irregular VFR boundary; IDR spacing stays in frames.
    plan = [_seg(0, 33), _seg(33, 80)]

    idrs = insert_idr(plan, max_interval=30)

    assert idrs == [0, 30, 60]
    assert all(right - left <= 30 for left, right in zip(idrs, idrs[1:]))


def test_short_segments_are_merged_not_left_as_one_frame_gop():
    plan = [_seg(0, 30), _seg(30, 31), _seg(31, 90)]

    merged = avoid_short(plan, min_frames=12)

    assert merged[0]["start"] == 0
    assert merged[-1]["end"] == 90
    assert all(seg["end"] - seg["start"] >= 12 for seg in merged)
    assert all(
        left["end"] == right["start"]
        for left, right in zip(merged, merged[1:])
    )


def test_scene_changes_snap_to_nearest_frame():
    aligned = align_scene_changes(
        cuts=[97, 211, 305],
        scene_frames=[100, 210, 300],
    )

    assert aligned == [100, 210, 300]


def test_bitrate_is_allocated_by_duration():
    segments = [_seg(0, 10), _seg(10, 90)]

    bitrates = plan_bitrate(segments, total_bitrate=1000)

    assert set(bitrates) == {0, 1}
    assert sum(bitrates.values()) == 1000
    assert bitrates[1] == 9 * bitrates[0]


def test_dense_idr_is_rejected_and_reported():
    plan = [
        _seg(0, 20, True),
        _seg(20, 40, True),
        _seg(40, 60, True),
        _seg(60, 90, True),
    ]

    # Four IDRs in 90 frames is 4 * 30 / 90 = 1.33 IDRs per second.
    with pytest.raises(KeyframeError):
        estimate_decoder_load(plan, max_idr_per_sec=1.2)

    problems = validate_plan(plan, duration=90)
    assert any("idr" in problem.lower() for problem in problems)


def test_2997_time_base_uses_integer_frame_cadence():
    # Ten minutes at 30000/1001 fps is 17982 whole frames (17982.017...).
    time_base_num, time_base_den = 30000, 1001
    frames = 600 * time_base_num // time_base_den

    idrs = insert_idr([_seg(0, frames)], max_interval=time_base_den)

    assert idrs == list(range(0, frames, time_base_den))


def test_overlapping_segments_are_reported():
    plan = [_seg(0, 60), _seg(50, 100)]

    problems = validate_plan(plan, duration=100)

    assert any("overlap" in problem.lower() for problem in problems)


def test_batch_keeps_idr_sequences_independent():
    videos = [
        {"id": "long", "duration": 90, "segments": [_seg(0, 90)]},
        {"id": "short", "duration": 30, "segments": [_seg(0, 30)]},
    ]

    result = plan_batch(videos, _batch_config())

    assert result["long"]["idr"] == [0, 30, 60]
    assert result["short"]["idr"] == [0]
    assert result["long"]["idr"] != result["short"]["idr"]


def test_zero_gop_raises_keyframe_error():
    with pytest.raises(KeyframeError):
        snap_cuts([_seg(0, 100)], gop=0)


def test_single_segment_needs_no_extra_idr():
    idrs = insert_idr([_seg(0, 100, True)], max_interval=300)

    assert idrs == [0]


def test_gop_one_keeps_every_frame_as_boundary():
    segments = [_seg(0, 3), _seg(3, 7), _seg(7, 12)]

    snapped = snap_cuts(segments, gop=1)

    assert [(seg["start"], seg["end"]) for seg in snapped] == [
        (0, 3),
        (3, 7),
        (7, 12),
    ]


def test_empty_segments_return_empty():
    assert snap_cuts([], gop=100) == []
    assert insert_idr([], max_interval=30) == []
    assert avoid_short([], min_frames=12) == []
    assert plan_bitrate([], total_bitrate=0) == {}


def test_batch_success_preserves_video_order():
    videos = [
        {"id": "a", "duration": 60, "segments": [_seg(0, 60)]},
        {"id": "b", "duration": 90, "segments": [_seg(0, 90)]},
        {"id": "c", "duration": 120, "segments": [_seg(0, 120)]},
    ]

    result = plan_batch(videos, _batch_config())

    assert list(result) == ["a", "b", "c"]


def test_batch_rolls_back_when_any_video_is_invalid():
    videos = [
        {"id": "ok", "duration": 90, "segments": [_seg(0, 90)]},
        {"id": "bad", "duration": 100, "segments": [_seg(0, 90), _seg(90, 120)]},
    ]

    with pytest.raises(KeyframeError):
        plan_batch(videos, _batch_config())

    # A failed batch must not rewrite the caller's input.
    assert videos[1]["segments"][1]["end"] == 120
