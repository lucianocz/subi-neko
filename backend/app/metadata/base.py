from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MediaType(str, Enum):
    TV = "TV"
    MOVIE = "Movie"
    OVA = "OVA"
    ONA = "ONA"
    SPECIAL = "Special"
    MUSIC = "Music"
    UNKNOWN = "Unknown"


class CharacterRole(str, Enum):
    MAIN = "MAIN"
    SUPPORTING = "SUPPORTING"
    BACKGROUND = "BACKGROUND"


class CharacterGender(str, Enum):
    MALE = "male"
    FEMALE = "female"
    NON_BINARY = "non_binary"
    OTHER = "other"


@dataclass
class SearchResult:
    provider_id: str
    title: str  # romaji
    title_native: str | None = None
    year: int | None = None
    media_type: MediaType = MediaType.UNKNOWN
    episode_count: int | None = None
    title_english: str | None = None


@dataclass
class SeriesDetails:
    provider_id: str
    title: str
    title_native: str | None = None
    title_synonyms: list[str] = field(default_factory=list)
    year: int | None = None
    media_type: MediaType = MediaType.UNKNOWN
    description: str | None = None
    episode_count: int | None = None


@dataclass
class Character:
    name: str
    provider_id: str | None = None
    name_native: str | None = None
    role: CharacterRole | None = None
    gender: CharacterGender | None = None
    description: str | None = None
    voice_actor: str | None = None
    character_type: str | None = None


@dataclass
class Episode:
    number: int
    title: str | None = None
    title_native: str | None = None
    air_date: str | None = None


class MetadataProvider(ABC):
    supports_search: bool = True
    supports_characters: bool = True
    supports_episodes: bool = False

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}

    @abstractmethod
    async def search(self, query: str) -> list[SearchResult]: ...

    @abstractmethod
    async def get_details(self, provider_id: str) -> SeriesDetails: ...

    @abstractmethod
    async def get_characters(self, provider_id: str) -> list[Character]: ...

    async def get_episodes(self, provider_id: str) -> list[Episode]:
        """Per-episode metadata (titles, air dates). Optional — providers
        without episode data return an empty list."""
        return []
