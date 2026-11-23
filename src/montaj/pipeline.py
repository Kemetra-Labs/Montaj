"""Cache keys and transition rules shared by the clip stages."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field

from montaj.models import CheckResult, Clip, ClipStage, ClipStatus


@dataclass(frozen=True, slots=True)
class LegacyRecipe:
    """An earlier render and caption recipe that reviewed clips stay pinned to."""

    cut: str
    captions: str | None = None


@dataclass(frozen=True, slots=True)
class ClipInputs:
    source_hash: str = ""
    transcript_hash: str = ""
    rules_hash: str = ""
    stage_options: dict[ClipStage, str] = field(default_factory=dict)
    legacy: tuple[LegacyRecipe, ...] = ()
    # The recipe a clip was last rendered and captioned with, by clip ID.
    recorded: Callable[[int], LegacyRecipe | None] | None = None


def input_hash(*parts: object) -> str:
    payload = json.dumps(
        parts, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _protected(clip: Clip) -> bool:
    return clip.reviewed_at is not None or clip.status in (
        ClipStatus.APPROVED,
        ClipStatus.EXPORTED,
        ClipStatus.POSTED,
    )


def _recipe(clip: Clip, values: ClipInputs) -> tuple[str | None, str | None]:
    """Keep a reviewed clip on the recipe it was rendered with.

    A new default engine must not make approved or exported output stale.
    The pin only holds while the stored hashes still match that older recipe,
    so a trim, a source change or an explicit upgrade moves the clip forward.
    """
    options = values.stage_options
    cut_option = options.get(ClipStage.CUT)
    caption_option = options.get(ClipStage.CAPTIONS)
    if not _protected(clip):
        return cut_option, caption_option
    candidates = list(values.legacy)
    if values.recorded is not None and clip.id is not None:
        if (recorded := values.recorded(clip.id)) is not None:
            candidates.insert(0, recorded)
    bounds = (clip.start_s, clip.end_s)
    stored_cut = clip.input_hashes.get(ClipStage.CUT)
    for recipe in candidates:
        if stored_cut == input_hash(values.source_hash, bounds, recipe.cut):
            cut_option = recipe.cut
            break
    cut_hash = input_hash(values.source_hash, bounds, cut_option)
    stored_captions = clip.input_hashes.get(ClipStage.CAPTIONS)
    for recipe in candidates:
        if recipe.captions is not None and stored_captions == input_hash(
            cut_hash, values.transcript_hash, recipe.captions
        ):
            caption_option = recipe.captions
            break
    return cut_option, caption_option


def effective_options(clip: Clip, inputs: ClipInputs) -> tuple[str | None, str | None]:
    """The cut and caption recipes this clip's fingerprints resolve to."""
    return _recipe(clip, inputs)


def _fingerprints(clip: Clip, inputs: ClipInputs | None) -> dict[ClipStage, str]:
    values = inputs or ClipInputs()
    options = values.stage_options
    bounds = (clip.start_s, clip.end_s)
    cut_option, caption_option = _recipe(clip, values)
    cut = input_hash(values.source_hash, bounds, cut_option)
    captions = input_hash(cut, values.transcript_hash, caption_option)
    draft = input_hash(
        (values.transcript_hash, bounds) if clip.draft_quotes_clip else None,
        clip.moment_type,
        options.get(ClipStage.DRAFT),
    )
    content = input_hash(
        captions,
        clip.hook_text,
        clip.hook_style,
        clip.hook_a,
        clip.hook_a_style,
        clip.hook_b,
        clip.hook_b_style,
        clip.caption,
        clip.hashtags,
        clip.chosen_hook,
    )
    check = input_hash(content, values.rules_hash, options.get(ClipStage.CHECK))
    return {
        ClipStage.CUT: cut,
        ClipStage.CAPTIONS: captions,
        ClipStage.DRAFT: draft,
        ClipStage.CHECK: check,
        ClipStage.REVIEW: content,
        ClipStage.EXPORT: input_hash(content, check, options.get(ClipStage.EXPORT)),
        ClipStage.POST_LOG: input_hash(content, options.get(ClipStage.POST_LOG)),
    }


def stage_input_hash(
    clip: Clip, stage: ClipStage, inputs: ClipInputs | None = None
) -> str:
    return _fingerprints(clip, inputs)[stage]


def is_stale(clip: Clip, stage: ClipStage, inputs: ClipInputs | None = None) -> bool:
    expected = stage_input_hash(clip, stage, inputs)
    match stage:
        case ClipStage.CUT:
            return clip.raw_path is None or clip.input_hashes.get(stage) != expected
        case ClipStage.CAPTIONS:
            return clip.final_path is None or clip.input_hashes.get(stage) != expected
        case ClipStage.CHECK:
            return clip.check_result is None or clip.check_input_hash != expected
        case ClipStage.REVIEW:
            return clip.reviewed_at is None or clip.reviewed_input_hash != expected
        case _:
            return clip.input_hashes.get(stage) != expected


def invalidate_review(clip: Clip, inputs: ClipInputs | None = None) -> Clip:
    """Require fresh approval when the exported content would differ."""
    if clip.status in (ClipStatus.APPROVED, ClipStatus.EXPORTED) and is_stale(
        clip, ClipStage.REVIEW, inputs
    ):
        return clip.model_copy(
            update={"status": ClipStatus.DRAFTED, "changed_since_review": True}
        )
    return clip


def next_stage(clip: Clip, inputs: ClipInputs | None = None) -> ClipStage | None:
    """Find the first pending clip stage after source-level ranking."""
    if clip.status in (ClipStatus.CANDIDATE, ClipStatus.POSTED):
        return None
    if clip.status == ClipStatus.REJECTED and (
        clip.reviewed_at is not None or clip.check_result is None
    ):
        return None
    for stage in (ClipStage.CUT, ClipStage.CAPTIONS, ClipStage.DRAFT, ClipStage.CHECK):
        if is_stale(clip, stage, inputs):
            return stage
    if clip.status == ClipStatus.REJECTED:
        return None
    if clip.status not in (ClipStatus.APPROVED, ClipStatus.EXPORTED) or is_stale(
        clip, ClipStage.REVIEW, inputs
    ):
        return ClipStage.REVIEW
    if clip.check_result == CheckResult.FAIL:
        return ClipStage.REVIEW
    if clip.status != ClipStatus.EXPORTED or is_stale(clip, ClipStage.EXPORT, inputs):
        return ClipStage.EXPORT
    return ClipStage.POST_LOG
