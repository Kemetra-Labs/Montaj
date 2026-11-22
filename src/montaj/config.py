"""Settings for the CLI and pipeline stages."""

from os import getenv
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class RetrySettings(BaseModel):
    attempts: int = Field(default=4, ge=1, le=20)
    base_delay: float = Field(default=1.0, ge=0, allow_inf_nan=False)
    max_delay: float = Field(default=30.0, ge=0, allow_inf_nan=False)

    @classmethod
    def from_env(cls) -> "RetrySettings":
        values = {
            field: value
            for field, name in (
                ("attempts", "MONTAJ_API_ATTEMPTS"),
                ("base_delay", "MONTAJ_API_BASE_DELAY"),
                ("max_delay", "MONTAJ_API_MAX_DELAY"),
            )
            if (value := getenv(name))
        }
        return cls.model_validate(values)


class Settings(BaseModel):
    """Read settings on demand so callers can start without credentials."""

    model_config = ConfigDict(frozen=True)

    data_dir: Path = Path("data")
    deepgram_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    deepgram_model: str = "nova-3"
    deepgram_language: str = "en"
    claude_model: str | None = None
    font_path: Path | None = None
    retry: RetrySettings = Field(default_factory=RetrySettings)
    max_cost_per_run_usd: float = Field(default=0.5, ge=0, allow_inf_nan=False)
    max_cost_per_source_usd: float = Field(default=2.0, ge=0, allow_inf_nan=False)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "montaj.db"

    @property
    def resolved_font_path(self) -> Path:
        if self.font_path is not None:
            return self.font_path
        return (
            Path(__file__).with_name("assets") / "fonts" / "NotoSansArabic-Regular.ttf"
        )

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
            deepgram_language=getenv("MONTAJ_DEEPGRAM_LANGUAGE")
            or defaults.deepgram_language,
            claude_model=getenv("MONTAJ_CLAUDE_MODEL") or None,
            font_path=Path(font_path) if font_path else None,
            retry=RetrySettings.from_env(),
            max_cost_per_run_usd=float(
                getenv("MONTAJ_MAX_COST_PER_RUN_USD") or defaults.max_cost_per_run_usd
            ),
            max_cost_per_source_usd=float(
                getenv("MONTAJ_MAX_COST_PER_SOURCE_USD")
                or defaults.max_cost_per_source_usd
            ),
        )
