"""Settings for the CLI and pipeline stages."""

from os import getenv
from pathlib import Path

from pydantic import BaseModel, ConfigDict, SecretStr


class Settings(BaseModel):
    """Read settings on demand so callers can start without credentials."""

    model_config = ConfigDict(frozen=True)

    data_dir: Path = Path("data")
    deepgram_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    deepgram_model: str = "nova-3"
    claude_model: str | None = None
    font_path: Path | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        """Load exported environment variables, treating blanks as unset."""
        deepgram_key = getenv("DEEPGRAM_API_KEY")
        anthropic_key = getenv("ANTHROPIC_API_KEY")
        font_path = getenv("MONTAJ_FONT_PATH")
        defaults = cls()

        return cls(
            data_dir=Path(getenv("MONTAJ_DATA_DIR") or defaults.data_dir),
            deepgram_api_key=SecretStr(deepgram_key) if deepgram_key else None,
            anthropic_api_key=SecretStr(anthropic_key) if anthropic_key else None,
            deepgram_model=getenv("MONTAJ_DEEPGRAM_MODEL") or defaults.deepgram_model,
            claude_model=getenv("MONTAJ_CLAUDE_MODEL") or None,
            font_path=Path(font_path) if font_path else None,
        )
