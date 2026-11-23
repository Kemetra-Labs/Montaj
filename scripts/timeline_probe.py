"""Write a standalone OTIO pilot from synthetic review manifests."""

import argparse
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, Self
from urllib.parse import quote

from pydantic import BaseModel, Field, field_validator, model_validator


class HandoffClip(BaseModel):
    clip_id: int = Field(gt=0, strict=True)
    source_id: int = Field(gt=0, strict=True)
    approved: Literal[True]
    path: str
    media_frames: int = Field(gt=0, strict=True)
    in_frame: int = Field(ge=0, strict=True)
    out_frame: int = Field(gt=0, strict=True)

    @field_validator("approved", mode="before")
    @classmethod
    def require_approval(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Synthetic clip must be marked approved.")
        return value

    @field_validator("path")
    @classmethod
    def relative_path(cls, path: str) -> str:
        parsed = PurePosixPath(path)
        if (
            not parsed.parts
            or parsed.is_absolute()
            or any(
                PureWindowsPath(part).is_reserved()
                or part == ".."
                or part[-1] in ". "
                or any(char in '<>:"\\|?*' or ord(char) < 32 for char in part)
                for part in parsed.parts
            )
        ):
            raise ValueError(
                "Use a portable relative media path inside the handoff folder."
            )
        return parsed.as_posix()

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if not self.in_frame < self.out_frame <= self.media_frames:
            raise ValueError(
                "Clip in/out must describe a non-empty range inside the media."
            )
        return self


class Handoff(BaseModel):
    fps: int = Field(gt=0, strict=True)
    clips: list[HandoffClip] = Field(min_length=1)


def write_timeline(handoff: Handoff, output: Path) -> None:
    if output.suffix != ".otio":
        raise ValueError("Use an .otio output file.")
    root = output.parent.resolve()
    for clip in handoff.clips:
        media = (root / clip.path).resolve()
        if not media.is_relative_to(root):
            raise ValueError(f"Media escapes the handoff folder: {clip.path}")
        if not media.is_file():
            raise ValueError(f"Missing media: {clip.path}")
    try:
        import opentimelineio as otio
    except ImportError as exc:
        raise ValueError(
            "Install OpenTimelineIO in a disposable environment to run this pilot."
        ) from exc

    def time_range(start: int, duration: int):
        return otio.opentime.TimeRange(
            otio.opentime.RationalTime(start, handoff.fps),
            otio.opentime.RationalTime(duration, handoff.fps),
        )

    timeline = otio.schema.Timeline(name=output.stem)
    for kind in (otio.schema.TrackKind.Video, otio.schema.TrackKind.Audio):
        track = otio.schema.Track(name=f"Reviewed clips ({kind.lower()})", kind=kind)
        timeline.tracks.append(track)
        for clip in handoff.clips:
            track.append(
                otio.schema.Clip(
                    name=PurePosixPath(clip.path).name,
                    media_reference=otio.schema.ExternalReference(
                        target_url=quote(clip.path),
                        available_range=time_range(0, clip.media_frames),
                    ),
                    source_range=time_range(
                        clip.in_frame, clip.out_frame - clip.in_frame
                    ),
                    metadata={
                        "montaj": {"clip_id": clip.clip_id, "source_id": clip.source_id}
                    },
                )
            )
    encoded = otio.adapters.write_to_string(timeline, adapter_name="otio_json")
    with output.open("x", encoding="utf-8") as stream:
        stream.write(encoded)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        write_timeline(
            Handoff.model_validate_json(args.manifest.read_bytes()), args.output
        )
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Timeline probe failed: {exc}\n")


if __name__ == "__main__":
    main()
