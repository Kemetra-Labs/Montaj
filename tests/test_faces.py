import hashlib
import io
import os
from pathlib import Path

import pytest

from montaj import faces
from montaj.faces import VisionUnavailable, YuNetDetector, check_model, fetch_model


def test_missing_or_altered_model_is_refused(tmp_path: Path) -> None:
    with pytest.raises(VisionUnavailable, match="not found"):
        check_model(tmp_path / "absent.onnx")
    altered = tmp_path / "altered.onnx"
    altered.write_bytes(b"not the approved weight")
    with pytest.raises(VisionUnavailable, match="does not match"):
        check_model(altered)
    with pytest.raises(VisionUnavailable):
        YuNetDetector(altered)


def test_fetch_verifies_the_pinned_digest_before_saving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls: list[str] = []

    def opener(payload: bytes):  # type: ignore[no-untyped-def]
        def open_url(url: str) -> io.BytesIO:
            urls.append(url)
            return io.BytesIO(payload)

        return open_url

    target = tmp_path / "models" / "yunet.onnx"
    with pytest.raises(VisionUnavailable, match="SHA-256"):
        fetch_model(target, opener=opener(b"tampered"))
    assert not target.exists()
    payload = b"approved weight"
    monkeypatch.setattr(faces, "YUNET_SHA256", hashlib.sha256(payload).hexdigest())
    assert fetch_model(target, opener=opener(payload)) == target
    assert target.read_bytes() == payload
    assert urls[0] == faces.YUNET_URL and "f12e12798e8314f7" in urls[0]


@pytest.mark.skipif(
    not os.getenv("MONTAJ_TEST_YUNET_MODEL"),
    reason="Set MONTAJ_TEST_YUNET_MODEL to the approved YuNet weight to run.",
)
def test_real_yunet_loads_and_finds_nothing_in_a_blank_frame() -> None:
    np = pytest.importorskip("numpy")
    detector = YuNetDetector(Path(os.environ["MONTAJ_TEST_YUNET_MODEL"]))
    assert detector.detect(np.zeros((180, 320, 3), np.uint8)) == []
