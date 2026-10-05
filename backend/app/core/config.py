import logging
from pathlib import Path

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env from the project root regardless of working directory
_ROOT_ENV = Path(__file__).parents[3] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ROOT_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    debug: bool = False

    import_root: Path = Path("./media/import")
    output_root: Path = Path("./media/output")
    config_root: Path = Path("./config")

    # Replacement fonts for the QC preview (JASSUB/libass). Defaults to
    # ``<config_root>/fonts``; may be empty.
    fonts_root: Path | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.config_root / 'subi-neko.db'}"

    @property
    def fonts_dir(self) -> Path:
        return self.fonts_root if self.fonts_root is not None else self.config_root / "fonts"

    @property
    def cache_dir(self) -> Path:
        """Derived data (extracted MKV font attachments); safe to delete."""
        return self.config_root / "cache"

    def ensure_directories(self) -> None:
        """Create required directories if they don't exist."""
        for path in (self.import_root, self.output_root, self.config_root, self.fonts_dir):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()
