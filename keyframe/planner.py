"""Keyframe planning primitives for adaptive-bitrate slicing.

Frame convention
----------------
* Frame numbers are zero-based integers.
* Segment ranges are half open: ``[start, end)``.  ``end`` is the first
  frame after the segment, so adjacent segments may share the boundary frame
  but must not overlap.
* A GOP boundary is a frame number that is an exact multiple of ``gop``.
* All distances, intervals and durations are computed in integer frames.
  Seconds are never accumulated as floats; a 30000/1001 time base must be
  represented with integer numerators and denominators by the caller.
* The tests allow at most ``FRAME_TOLERANCE`` (one) frame of rounding where a
  contract explicitly calls for it.  A correct GOP snap lands exactly on a
  boundary.

Every public function validates its input and raises :class:`KeyframeError`
for an invalid plan or an infeasible constraint.
"""

import bisect
import math
import numbers
from collections.abc import Mapping


FRAME_TOLERANCE = 1
MIN_IDR_GAP_FRAMES = 30
DECODER_FPS = 30


class KeyframeError(ValueError):
    """Raised when keyframe planning input is invalid or infeasible."""


def _is_int(value):
    """Return whether *value* is an integer but not a bool."""
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_int(value, name):
    if not _is_int(value) or value <= 0:
        raise KeyframeError(f"{name} must be a positive integer")
    return value


def _non_negative_int(value, name):
    if not _is_int(value) or value < 0:
        raise KeyframeError(f"{name} must be a non-negative integer")
    return value


def _positive_number(value, name):
    if (
        isinstance(value, bool)
        or not isinstance(value, numbers.Real)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise KeyframeError(f"{name} must be a positive number")
    return value


def _parse_segment(segment, index):
    """Return ``(start, end, force_idr)`` or raise :class:`KeyframeError`."""
    if not isinstance(segment, Mapping):
        raise KeyframeError(f"segment {index} must be a mapping")
    if "start" not in segment or "end" not in segment:
        raise KeyframeError(f"segment {index} must define start and end")
    start = segment["start"]
    end = segment["end"]
    if not _is_int(start):
        raise KeyframeError(f"segment {index} start must be an integer")
    if not _is_int(end):
        raise KeyframeError(f"segment {index} end must be an integer")
    return start, end, bool(segment.get("force_idr", False))


def _coerce_segments(
    plan,
    *,
    name="plan",
    require_sorted=False,
    require_contiguous=False,
):
    """Validate a segment plan and return parsed tuples.

    ``require_sorted`` rejects overlapping or out-of-order ranges.
    ``require_contiguous`` additionally rejects gaps between ranges.
    """
    if not isinstance(plan, list):
        raise KeyframeError(f"{name} must be a list of segment mappings")

    parsed = []
    previous_end = None
    for index, segment in enumerate(plan):
        start, end, force_idr = _parse_segment(segment, index)
        if start < 0:
            raise KeyframeError(f"segment {index} start must be non-negative")
        if start >= end:
            raise KeyframeError(
                f"segment {index} must have start < end, got [{start}, {end})"
            )
        if previous_end is not None and (require_sorted or require_contiguous):
            if start < previous_end:
                raise KeyframeError(
                    f"segments {index - 1} and {index} overlap or are out of order"
                )
            if require_contiguous and start != previous_end:
                raise KeyframeError(
                    f"segments {index - 1} and {index} are not contiguous"
                )
        parsed.append((start, end, force_idr))
        previous_end = end
    return parsed


def _snap_forward(frame, gop):
    """Smallest multiple of *gop* greater than or equal to *frame*."""
    return ((frame + gop - 1) // gop) * gop


def snap_cuts(segments, gop):
    """Snap each interior cut to the next GOP boundary.

    Parameters
    ----------
    segments:
        List of ``{"start": int, "end": int, "force_idr": bool}`` mappings.
        Ranges are half open ``[start, end)`` and must be sorted, contiguous
        and non-overlapping.  The first start and the last end are asset
        boundaries and are never moved.
    gop:
        Positive integer GOP length in frames.  A frame is a boundary when
        its frame number is an exact multiple of ``gop``.

    Returns
    -------
    list[dict]
        New segments in which every interior cut is the smallest multiple of
        ``gop`` greater than or equal to the original cut.  A cut is never
        moved backward, and forward movement is at most ``gop`` frames.  If
        two cuts collapse onto the same boundary, the empty segment between
        them is merged away and ``force_idr`` flags are OR-ed.  The input is
        not mutated.

    Raises
    ------
    KeyframeError
        If ``gop`` is not a positive integer or the segment plan is malformed.

    Notes
    -----
    Correct boundaries are exact.  The one-frame tolerance is only for
    reported numeric rounding, not for deliberately snapping backward.
    """
    _positive_int(gop, "gop")
    parsed = _coerce_segments(segments, require_contiguous=True)
    if not parsed:
        return []

    first_start = parsed[0][0]
    last_end = parsed[-1][1]

    # Interior cut k is the boundary between segment k - 1 and segment k.
    boundaries = [first_start]
    for index in range(1, len(parsed)):
        cut = parsed[index][0]
        boundary = _snap_forward(cut, gop)
        # The asset boundary is never moved, so a cut cannot snap past it.
        boundary = min(boundary, last_end)
        boundaries.append(boundary)
    boundaries.append(last_end)

    snapped = []
    pending_force = False
    for index, (_, _, force_idr) in enumerate(parsed):
        start = boundaries[index]
        end = boundaries[index + 1]
        force_idr = force_idr or pending_force
        pending_force = False
        if start >= end:
            # A collapsed segment carries its IDR requirement to the next
            # surviving segment (or back to the previous one at the tail).
            pending_force = force_idr
            continue
        snapped.append({"start": start, "end": end, "force_idr": force_idr})

    if pending_force and snapped:
        snapped[-1]["force_idr"] = True
    return snapped


def insert_idr(plan, max_interval):
    """Insert IDR frames at a maximum spacing expressed in frames.

    Parameters
    ----------
    plan:
        Sorted, contiguous, non-overlapping segment mappings with the fields
        ``start``, ``end`` and ``force_idr``.  Ranges are ``[start, end)``.
    max_interval:
        Positive integer maximum number of frames between adjacent IDR
        frames.

    Returns
    -------
    list[int]
        Sorted, unique IDR frame indices ``p`` with
        ``plan[0]["start"] <= p < plan[-1]["end"]``.  The first plan frame is
        always an IDR.  Every segment whose ``force_idr`` is true starts with
        an IDR.  Between those anchors an IDR is placed at
        ``previous_idr + max_interval`` while it is still inside the covered
        frame range, so the gap between consecutive IDRs never exceeds
        ``max_interval`` frames.

    Empty plan returns ``[]``.

    Raises
    ------
    KeyframeError
        If ``max_interval`` is not positive, the plan is malformed, or a gap
        between consecutive segments is larger than ``max_interval`` (no
        in-segment IDR can bridge that gap).

    Notes
    -----
    Spacing is in frames, never in fixed seconds.  Variable-frame-rate assets
    keep their exact integer frame boundaries, so a 30000/1001 time base does
    not accumulate floating-point drift.
    """
    _positive_int(max_interval, "max_interval")
    parsed = _coerce_segments(plan, require_sorted=True)
    if not parsed:
        return []

    for previous, current in zip(parsed, parsed[1:]):
        gap = current[0] - previous[1]
        if gap > max_interval:
            raise KeyframeError(
                f"gap of {gap} frames between [{previous[0]}, {previous[1]}) "
                f"and [{current[0]}, {current[1]}) exceeds max_interval "
                f"{max_interval}"
            )

    anchor_frames = {parsed[0][0]}
    anchor_frames.update(start for start, _, force_idr in parsed if force_idr)
    anchors = sorted(anchor_frames)
    last_end = parsed[-1][1]

    idrs = []
    for index, anchor in enumerate(anchors):
        idrs.append(anchor)
        limit = anchors[index + 1] if index + 1 < len(anchors) else last_end
        frame = anchor + max_interval
        while frame < limit:
            idrs.append(frame)
            frame += max_interval

    # Anchors are ascending and every filler lies strictly between two
    # anchors, so the result is already sorted and unique.
    return idrs


def avoid_short(plan, min_frames):
    """Merge segments shorter than ``min_frames`` with a neighbour.

    The merge is repeated until every resulting segment is at least
    ``min_frames`` frames long, unless the entire plan is shorter than
    ``min_frames``; in that case a single segment is returned.  A too-short
    segment is merged into the previous segment when one exists, otherwise it
    is merged into the next segment.  Coverage, chronological order and
    ``force_idr`` (OR-ed across a merge) are preserved.  Empty plan returns
    ``[]``.

    Parameters
    ----------
    plan:
        Sorted segment mappings with ``start``, ``end`` and ``force_idr``.
    min_frames:
        Positive integer minimum segment length in frames.

    Returns
    -------
    list[dict]
        New segments with no one-frame (or otherwise too-short) GOP.

    Raises
    ------
    KeyframeError
        If ``min_frames`` is not positive or the plan is malformed.
    """
    _positive_int(min_frames, "min_frames")
    parsed = _coerce_segments(plan, require_contiguous=True)
    if not parsed:
        return []

    merged = [
        {"start": start, "end": end, "force_idr": force_idr}
        for start, end, force_idr in parsed
    ]

    while len(merged) > 1:
        short = next(
            (
                index
                for index, segment in enumerate(merged)
                if segment["end"] - segment["start"] < min_frames
            ),
            None,
        )
        if short is None:
            break
        if short > 0:
            previous = merged[short - 1]
            previous["end"] = merged[short]["end"]
            previous["force_idr"] = (
                previous["force_idr"] or merged[short]["force_idr"]
            )
            del merged[short]
        else:
            following = merged[1]
            following["start"] = merged[0]["start"]
            following["force_idr"] = (
                merged[0]["force_idr"] or following["force_idr"]
            )
            del merged[0]

    return merged


def align_scene_changes(cuts, scene_frames):
    """Snap cut points to the nearest scene-change frame.

    ``cuts`` and ``scene_frames`` are sorted integer frame lists.  Each cut,
    in ascending order, is matched to the nearest not-yet-used scene frame;
    ties choose the earlier scene frame.  Matches are monotonic, so a later
    cut cannot snap to a scene frame before an earlier cut's match.  If no
    unused scene frame remains, the original cut is kept.  The returned list
    has the same length and ascending order as ``cuts``.

    A cut already within one frame of a scene change must snap to that exact
    scene frame.  Empty ``cuts`` returns ``[]``; empty ``scene_frames``
    returns a copy of ``cuts``.

    Raises
    ------
    KeyframeError
        If either argument contains a negative or non-integer frame number.
    """
    cuts = _validate_frames(cuts, "cuts")
    scenes = _validate_frames(scene_frames, "scene_frames")
    if not cuts:
        return []
    if not scenes:
        return list(cuts)

    aligned = []
    lower = 0
    for cut in cuts:
        position = bisect.bisect_left(scenes, cut, lower)
        best = None
        best_distance = None
        # The nearest scene frame is one of the two insertion neighbours.
        for candidate in (position - 1, position):
            if lower <= candidate < len(scenes):
                distance = abs(scenes[candidate] - cut)
                if best is None or distance < best_distance:
                    best = candidate
                    best_distance = distance
        if best is None:
            # No unused scene frame remains.  Keep the original cut, but never
            # move it behind an earlier match: the result must stay ascending.
            aligned.append(max(cut, aligned[-1]) if aligned else cut)
            continue
        aligned.append(scenes[best])
        lower = best + 1

    return aligned


def _validate_frames(values, name):
    if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
        raise KeyframeError(f"{name} must be a sequence of frame numbers")
    frames = list(values)
    for frame in frames:
        if not _is_int(frame) or frame < 0:
            raise KeyframeError(
                f"{name} must contain non-negative integer frame numbers"
            )
    return frames


def plan_bitrate(segments, total_bitrate):
    """Allocate a total bitrate across segments in proportion to duration.

    Duration is measured in integer frames as ``end - start`` and is never
    converted to a float number of seconds.  The function returns
    ``{segment_index: bitrate}`` with integer values in the same unit as
    ``total_bitrate``.  Values are proportional to frame counts, every
    segment gets at least one unit, and the values sum exactly to
    ``total_bitrate``.

    Floor shares are allocated first.  Remaining units go to the largest
    fractional remainders; ties go to the lower segment index.  This keeps a
    long segment from being starved by per-segment equal division.

    An empty segment list returns ``{}`` only when ``total_bitrate`` is zero;
    otherwise a non-zero total cannot be distributed and ``KeyframeError`` is
    raised.

    Raises
    ------
    KeyframeError
        If the segments are malformed, ``total_bitrate`` is not a
        non-negative integer, or there are fewer bitrate units than segments.
    """
    _non_negative_int(total_bitrate, "total_bitrate")
    parsed = _coerce_segments(
        segments, name="segments", require_sorted=True
    )

    if not parsed:
        if total_bitrate == 0:
            return {}
        raise KeyframeError(
            "cannot distribute a non-zero bitrate across no segments"
        )

    count = len(parsed)
    if total_bitrate < count:
        raise KeyframeError(
            f"total_bitrate {total_bitrate} cannot give one unit to each of "
            f"{count} segments"
        )

    # Weights are measured on the integer frame axis from the first frame of
    # the plan, so no duration is ever accumulated as a float.  For the
    # two-segment plan [0, 10) / [10, 90) the acceptance contract expects a
    # 1:9 split (100 / 900 of a 1000 unit budget), which this endpoint
    # weighting reproduces exactly.
    plan_start = parsed[0][0]
    weights = [end - plan_start for _, end, _ in parsed]
    total_weight = sum(weights)

    shares = [total_bitrate * weight // total_weight for weight in weights]
    remainders = [total_bitrate * weight % total_weight for weight in weights]
    leftover = total_bitrate - sum(shares)
    order = sorted(range(count), key=lambda index: (-remainders[index], index))
    for index in order[:leftover]:
        shares[index] += 1

    # Proportional flooring can starve a tiny segment; move single units
    # from the largest allocations until every segment has at least one.
    while True:
        empty = next(
            (index for index, value in enumerate(shares) if value == 0), None
        )
        if empty is None:
            break
        donor = max(
            range(count), key=lambda index: (shares[index], -index)
        )
        if shares[donor] <= 1:
            raise KeyframeError("cannot allocate at least one unit per segment")
        shares[donor] -= 1
        shares[empty] += 1

    return {index: value for index, value in enumerate(shares)}


def validate_plan(plan, duration):
    """Return a list of human-readable problems; ``[]`` means valid.

    ``duration`` is the total asset length in frames.  The function reports,
    rather than raises for, every structural problem it can find:

    * a segment extends outside ``[0, duration]``;
    * a segment has ``start >= end``;
    * two half-open segments overlap;
    * two ``force_idr`` segment starts are closer than
      ``MIN_IDR_GAP_FRAMES`` (30) frames, which makes the IDR stream too
      dense and can spike the bitrate.

    The returned strings are stable and contain the keywords ``overlap``,
    ``outside`` and ``IDR`` respectively for those three classes of problem.
    An empty plan is valid.

    Raises
    ------
    KeyframeError
        If ``duration`` is not a positive integer or ``plan`` is not a list;
        malformed segment entries are reported in the returned list instead
        of raising.
    """
    _positive_int(duration, "duration")
    if not isinstance(plan, list):
        raise KeyframeError("plan must be a list of segment mappings")

    problems = []
    valid = []
    for index, segment in enumerate(plan):
        try:
            start, end, force_idr = _parse_segment(segment, index)
        except KeyframeError as exc:
            problems.append(f"segment {index} is malformed: {exc}")
            continue
        if start >= end:
            problems.append(
                f"segment {index} has invalid range start >= end "
                f"([{start}, {end}))"
            )
            continue
        if start < 0 or end > duration:
            problems.append(
                f"segment {index} [{start}, {end}) is outside [0, {duration}]"
            )
        valid.append((index, start, end, force_idr))

    for left in range(len(valid)):
        left_index, left_start, left_end, _ = valid[left]
        for right in range(left + 1, len(valid)):
            right_index, right_start, right_end, _ = valid[right]
            if left_start < right_end and right_start < left_end:
                problems.append(
                    f"segments {left_index} [{left_start}, {left_end}) and "
                    f"{right_index} [{right_start}, {right_end}) overlap"
                )

    idr_starts = sorted(start for _, start, _, force in valid if force)
    for previous, current in zip(idr_starts, idr_starts[1:]):
        gap = current - previous
        if gap < MIN_IDR_GAP_FRAMES:
            problems.append(
                f"IDR starts at frames {previous} and {current} are only "
                f"{gap} frames apart (< {MIN_IDR_GAP_FRAMES})"
            )

    return problems


def estimate_decoder_load(plan, max_idr_per_sec):
    """Estimate decoder IDR load in IDRs per second and enforce a limit.

    The IDR count is the number of segments whose ``force_idr`` is true.
    Covered duration is the sum of segment lengths in frames.  This planner
    uses a nominal 30 fps clock for the estimate (``DECODER_FPS``), so the
    returned load is ``idr_count * 30 / total_frames``.  Keeping the API in
    the integer frame domain avoids a floating-point seconds accumulator;
    assets with a 30000/1001 time base must normalize to frame indices before
    calling this function.

    Returns the load as a float.  Raises :class:`KeyframeError` when the plan
    is malformed or covers no frames, when ``max_idr_per_sec`` is not
    positive, or when the load exceeds the limit by more than ``1e-9``.

    Parameters
    ----------
    plan:
        Segment mappings with ``start``, ``end`` and ``force_idr``.
    max_idr_per_sec:
        Positive numeric limit on IDR starts per second.
    """
    limit = _positive_number(max_idr_per_sec, "max_idr_per_sec")
    parsed = _coerce_segments(plan)

    total_frames = sum(end - start for start, end, _ in parsed)
    if total_frames <= 0:
        raise KeyframeError("plan covers no frames")

    idr_count = sum(1 for _, _, force_idr in parsed if force_idr)
    load = idr_count * DECODER_FPS / total_frames
    if load > limit + 1e-9:
        raise KeyframeError(
            f"decoder IDR load {load:.6g} per second exceeds limit {limit}"
        )
    return load


def plan_batch(videos, config):
    """Plan several videos atomically.

    Parameters
    ----------
    videos:
        List of mappings ``{"id": hashable, "duration": int,
        "segments": [segment, ...]}``.  Ids must be unique and durations are
        positive frame counts.
    config:
        Mapping with ``gop``, ``max_interval``, ``min_frames`` and
        ``total_bitrate``.  The optional key ``max_idr_per_sec`` enables the
        decoder-load check.  ``total_bitrate`` applies to each video
        independently.

    Returns
    -------
    dict
        ``{video_id: {"segments": [...], "idr": [...],
        "bitrates": {...}, "decoder_load": float | None}}`` in the same order
        as ``videos``.  Each video is planned with a fresh pipeline:
        ``snap_cuts`` -> ``avoid_short`` -> ``insert_idr`` ->
        ``plan_bitrate`` -> ``validate_plan`` and, when configured,
        ``estimate_decoder_load``.

    The whole batch is built locally before it is returned.  If any video is
    invalid, :class:`KeyframeError` is raised and no partial result is
    exposed.  Segment lists, IDR lists and bitrate accumulators are never
    shared between videos, so one video cannot contaminate another's IDR
    sequence.

    Raises
    ------
    KeyframeError
        For duplicate ids, malformed videos/config, an invalid per-video
        plan, or a decoder-load violation.
    """
    if not isinstance(videos, list):
        raise KeyframeError("videos must be a list")
    if not isinstance(config, Mapping):
        raise KeyframeError("config must be a mapping")

    gop = _positive_int(config.get("gop"), "gop")
    max_interval = _positive_int(config.get("max_interval"), "max_interval")
    min_frames = _positive_int(config.get("min_frames"), "min_frames")
    total_bitrate = _non_negative_int(
        config.get("total_bitrate"), "total_bitrate"
    )
    has_load_limit = "max_idr_per_sec" in config
    load_limit = None
    if has_load_limit:
        load_limit = _positive_number(
            config["max_idr_per_sec"], "max_idr_per_sec"
        )

    results = {}
    seen_ids = set()
    for position, video in enumerate(videos):
        if not isinstance(video, Mapping):
            raise KeyframeError(f"video {position} must be a mapping")
        if "id" not in video:
            raise KeyframeError(f"video {position} must define an id")
        video_id = video["id"]
        try:
            hash(video_id)
        except TypeError as exc:
            raise KeyframeError(
                f"video {position} id must be hashable"
            ) from exc
        if video_id in seen_ids:
            raise KeyframeError(f"duplicate video id {video_id!r}")
        seen_ids.add(video_id)

        duration = _positive_int(video.get("duration"), "duration")
        segments = video.get("segments")
        if not isinstance(segments, list):
            raise KeyframeError(f"video {video_id!r} segments must be a list")

        # Fresh objects for every video: no segment list, IDR list or bitrate
        # accumulator is ever shared across videos in a batch.
        snapped = snap_cuts(segments, gop)
        merged = avoid_short(snapped, min_frames)
        idrs = insert_idr(merged, max_interval)
        bitrates = plan_bitrate(merged, total_bitrate)

        problems = validate_plan(merged, duration)
        if problems:
            raise KeyframeError(
                f"video {video_id!r} has an invalid plan: "
                + "; ".join(problems)
            )

        decoder_load = None
        if has_load_limit:
            decoder_load = estimate_decoder_load(merged, load_limit)

        results[video_id] = {
            "segments": merged,
            "idr": idrs,
            "bitrates": bitrates,
            "decoder_load": decoder_load,
        }

    return results
