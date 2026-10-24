from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, computed_field
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
    subineko_web_url: str = ""
    subineko_web_token: str = Field(default="", repr=False)
    tmdb_api_token: str = Field(default="", repr=False)

    @property
    def web_publishing_available(self) -> bool:
        try:
            url = urlsplit(self.subineko_web_url.strip())
            return bool(url.scheme in ("http", "https") and url.hostname and not url.username
                        and not url.password and not url.query and not url.fragment
                        and url.path in ("", "/") and (url.port is None or 0 < url.port <= 65535)
                        and self.subineko_web_token.strip()
                        and all(33 <= ord(c) <= 126 for c in self.subineko_web_token))
        except ValueError:
            return False

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
    def brand_dir(self) -> Path:
        """Branding ASS templates (``*.ass``); may be empty."""
        return self.config_root / "brand"

    @property
    def cache_dir(self) -> Path:
        """Derived data (extracted MKV font attachments); safe to delete."""
        return self.config_root / "cache"

    def ensure_directories(self) -> None:
        """Create required directories if they don't exist."""
        for path in (self.import_root, self.output_root, self.config_root, self.fonts_dir, self.brand_dir):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()
