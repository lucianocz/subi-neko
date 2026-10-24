from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identity = Annotated[
    str, Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$")
]
Name = Annotated[str, Field(min_length=1, max_length=255, pattern=r"\S")]
Number = Annotated[int, Field(ge=0, le=2147483647, strict=True)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TitleMetadata(StrictModel):
    title_english: Name
    title_romaji: Name | None = None
    year: Annotated[int, Field(ge=1, le=9999, strict=True)] | None = None
    original_language: Annotated[str, Field(pattern=r"^[a-z]{2,3}$")]
    original_description: Annotated[str, Field(max_length=10000)] | None = None
    description: Annotated[str, Field(max_length=10000)] | None = None

    @model_validator(mode="before")
    @classmethod
    def legacy_description(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            legacy = value.pop("description_english", None)
            if "description" not in value:
                value["description"] = legacy
            if "original_description" not in value:
                value["original_description"] = value["description"]
        return value

    def website_payload(self):
        return {
            **self.model_dump(exclude={"original_description", "description"}),
            "description_english": self.description,
        }


class Season(StrictModel):
    external_id: Identity
    season_number: Number
    name_english: Name | None = None
    year: Annotated[int, Field(ge=1, le=9999, strict=True)] | None = None


class WebConfig(StrictModel):
    tmdb_title_id: Identity
    media_type: Literal["tv", "movie"]
    episode_structure: Literal["SEASONED", "FLAT"] = "SEASONED"
    order_kind: Literal["standard", "episode_group"] = "standard"
    group_id: Identity | None = None
    season: Season | None = None
    metadata: TitleMetadata
    poster_path: (
        Annotated[
            str, Field(pattern=r"^/[A-Za-z0-9_-]+\.(jpg|png|webp)$", max_length=255)
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def valid_structure(self):
        if self.media_type == "movie" and (
            self.episode_structure != "FLAT" or self.order_kind != "standard"
        ):
            raise ValueError("Movies require FLAT standard order")
        if (self.order_kind == "episode_group") != bool(self.group_id):
            raise ValueError("Only episode group order requires group_id")
        if self.episode_structure == "FLAT" and self.season:
            raise ValueError("FLAT titles cannot have a season")
        return self

    def identity(self):
        return (
            self.tmdb_title_id,
            self.media_type,
            self.episode_structure,
            self.order_kind,
            self.group_id,
            self.season.external_id if self.season else None,
        )


class Mapping(StrictModel):
    file_id: int
    tmdb_episode_id: Identity | None = None
    episode_number: Number
    name_english: Name | None = None


class SaveRequest(StrictModel):
    version: int = Field(ge=0)
    config: WebConfig
    mappings: list[Mapping] | None = Field(default=None, max_length=1000)


class PublishRequest(StrictModel):
    version: int = Field(ge=0)
    file_ids: list[int] = Field(min_length=1, max_length=1000)
    release_label: Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]
    changelog: Annotated[str, Field(max_length=4000)] | None = None
