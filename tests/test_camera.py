import random
from collections.abc import Callable

import pytest

from montaj.camera import (
    EDGE_MARGIN,
    FACE_PAD,
    MAX_SPEED,
    CameraConfig,
    Sample,
    ShotPlan,
    center_left,
    crop_fraction,
    path_position,
    plan_clip,
    plan_shot,
    split_shots,
)
from montaj.faces import FaceBox

CROP = crop_fraction(1920, 1080)
CONFIG = CameraConfig(CROP)


def face(left: float, width: float = 0.1, height: float = 0.25) -> FaceBox:
    return FaceBox(left, 0.2, width, height, 0.9)


def samples(
    faces: Callable[[float], tuple[FaceBox, ...]], end: float = 4.0, step: float = 0.25
) -> list[Sample]:
    count = round(end / step)
    return [Sample(i * step, faces(i * step)) for i in range(count)]


def contains(plan: ShotPlan, sample: Sample, config: CameraConfig = CONFIG) -> bool:
    """The padded union of the sample's faces lies inside the planned crop."""
    if not sample.faces:
        return True
    left = min(f.left for f in sample.faces)
    right = max(f.right for f in sample.faces)
    pad = FACE_PAD * (right - left) + EDGE_MARGIN * config.crop_width
    x = path_position(plan, sample.t, config)
    return x - 1e-6 <= left - pad and right + pad <= x + config.crop_width + 1e-6


def test_center_is_kept_when_it_already_contains_the_face() -> None:
    plan = plan_shot(samples(lambda t: (face(0.45),)), 0, 4, CONFIG)
    assert (plan.layout, plan.reason) == ("center", "center_contains_faces")


def test_static_off_center_face_gets_a_still_crop_that_contains_it() -> None:
    # YuNet boxes measured on the real source at 1501.00 s (320 px wide analysis).
    boxes = [(202, 32), (200, 32), (197, 32), (197, 31), (197, 31)]
    seen = [
        Sample(i * 0.2, (FaceBox(x / 320, 0.25, w / 320, 0.22, 0.92),))
        for i, (x, w) in enumerate(boxes)
    ]
    plan = plan_shot(seen, 0, 1, CONFIG)
    assert (plan.layout, plan.reason) == ("track", "static_face_crop")
    assert all(contains(plan, sample) for sample in seen)
    # The centered crop cuts this face, which is why the shot moves.
    center = ShotPlan(0, 1, "center", "")
    assert not all(contains(center, sample) for sample in seen)
    assert plan.path[0][1] == plan.path[-1][1]


def test_slow_moving_face_is_followed_smoothly_and_kept_in_frame() -> None:
    seen = samples(lambda t: (face(0.1 + 0.12 * t),), end=5.0)
    plan = plan_shot(seen, 0, 5, CONFIG)
    assert (plan.layout, plan.reason) == ("track", "dynamic_face_track")
    assert all(contains(plan, sample) for sample in seen)
    for (t0, x0), (t1, x1) in zip(plan.path, plan.path[1:], strict=False):
        assert abs(x1 - x0) / (t1 - t0) <= MAX_SPEED * 1.25 + 1e-9


def test_small_detection_jitter_never_moves_the_crop() -> None:
    noise = random.Random(4)
    seen = samples(lambda t: (face(0.66 + noise.uniform(-0.006, 0.006)),))
    plan = plan_shot(seen, 0, 4, CONFIG)
    assert plan.layout == "track"
    assert {x for _, x in plan.path} == {plan.path[0][1]}


def test_two_nearby_faces_are_both_kept_and_no_speaker_is_chosen() -> None:
    seen = samples(lambda t: (face(0.58, 0.06), face(0.66, 0.06)))
    plan = plan_shot(seen, 0, 4, CONFIG)
    assert plan.layout == "track"
    assert all(contains(plan, sample) for sample in seen)


def test_faces_too_far_apart_fall_back_instead_of_picking_one() -> None:
    plan = plan_shot(samples(lambda t: (face(0.05), face(0.8))), 0, 4, CONFIG)
    assert (plan.layout, plan.reason) == ("center", "faces_wider_than_crop")


def test_a_small_background_face_does_not_steer_the_crop() -> None:
    seen = samples(lambda t: (face(0.6, 0.1, 0.3), face(0.02, 0.03, 0.08)))
    plan = plan_shot(seen, 0, 4, CONFIG)
    assert plan.layout == "track"
    assert plan.path[0][1] > center_left(CONFIG)


def test_detections_alternating_between_people_are_unstable() -> None:
    seen = samples(lambda t: (face(0.05 if round(t * 4) % 2 else 0.8),))
    plan = plan_shot(seen, 0, 4, CONFIG)
    assert (plan.layout, plan.reason) == ("center", "unstable_face_track")


def test_missing_or_sparse_faces_fall_back_and_games_keep_the_whole_frame() -> None:
    assert plan_shot(samples(lambda t: ()), 0, 4, CONFIG).reason == "no_reliable_face"
    sparse = samples(lambda t: (face(0.7),) if round(t * 4) % 3 == 0 else ())
    assert plan_shot(sparse, 0, 4, CONFIG).reason == "no_reliable_face"
    games = CameraConfig(CROP, "fit", 0.18)
    assert plan_shot(samples(lambda t: ()), 0, 4, games).layout == "fit"
    # A small face-cam in a game is not enough to crop away the screen.
    facecam = samples(lambda t: (face(0.85, 0.06, 0.1),))
    assert plan_shot(facecam, 0, 4, games).layout == "fit"


def test_each_shot_is_planned_separately_and_slivers_are_merged() -> None:
    assert split_shots([2.0, 2.1, 3.95], 4.0) == [(0.0, 2.0), (2.0, 4.0)]
    seen = samples(lambda t: (face(0.45),) if t < 2 else (face(0.75),))
    plan = plan_clip(seen, [2.0], 4.0, CONFIG)
    assert [shot.layout for shot in plan.shots] == ["center", "track"]
    assert plan.counts() == {"center": 1, "track": 1}


@pytest.mark.parametrize("seed", range(60))
def test_plan_is_never_worse_than_the_centered_crop(seed: int) -> None:
    rng = random.Random(seed)
    start, speed = rng.uniform(0, 0.8), rng.uniform(-0.15, 0.15)
    width = rng.uniform(0.05, 0.18)
    two = rng.random() < 0.3
    gap = rng.uniform(0.0, 0.25)
    dropout = rng.random() * 0.3

    def faces(t: float) -> tuple[FaceBox, ...]:
        if rng.random() < dropout:
            return ()
        left = min(max(0.0, start + speed * t), 1 - width)
        found = [face(left, width)]
        if two and left + width + gap + width < 1:
            found.append(face(left + width + gap, width))
        return tuple(found)

    seen = samples(faces)
    plan = plan_shot(seen, 0, 4, CONFIG)
    center = ShotPlan(0, 4, "center", "")
    for sample in seen:
        if contains(center, sample):
            assert contains(plan, sample) or plan.layout == "center"
    if plan.layout == "track":
        assert all(contains(plan, sample) for sample in seen)
