"""GET /projects/{id}/styles: usage-first ordering (events, then files, then name, then id)."""
from __future__ import annotations

from datetime import datetime

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.api.routes import projects as projects_routes
from app.db.models import File, Project, SubtitleEvent, SubtitleStyle, file_subtitle_styles
from tests.test_publish import env  # noqa: F401


def _now() -> str:
    return datetime.utcnow().isoformat()


@pytest_asyncio.fixture
async def client(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr("app.api.routes.projects.AsyncSessionLocal", env.asess)
    app = FastAPI()
    app.include_router(projects_routes.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


def _seed(env, spec: dict[str, tuple[int, int]]) -> int:
    """spec: style name -> (file_count, events per file)."""
    with env.sync() as s:
        p = Project(name="P", source_directory="series", anime_provider="t", anime_external_id="1",
                    created_at=_now(), updated_at=_now())
        s.add(p)
        s.flush()
        files = []
        line = 0
        for i in range(max(f for f, _ in spec.values())):
            f = File(project_id=p.id, filename=f"ep{i}.mkv", relative_path=f"ep{i}.mkv",
                     created_at=_now(), updated_at=_now())
            s.add(f)
            s.flush()
            files.append(f)
        for name, (n_files, n_events) in spec.items():
            style = SubtitleStyle(project_id=p.id, source_style_hash=f"h-{name}", style_name=name,
                                  font_name="Arial", font_size=20.0, created_at=_now(), updated_at=_now())
            s.add(style)
            s.flush()
            for f in files[:n_files]:
                s.execute(file_subtitle_styles.insert().values(file_id=f.id, subtitle_style_id=style.id))
                for _ in range(n_events):
                    line += 1
                    s.add(SubtitleEvent(
                        file_id=f.id, line_index=line, event_type="dialogue",
                        layer=0, start_ms=0, end_ms=1, original_start_ms=0, original_end_ms=1,
                        style=name, source_text="x", created_at=_now(), updated_at=_now()))
        s.commit()
        return p.id


async def _names(client, pid):
    r = await client.get(f"/api/projects/{pid}/styles")
    assert r.status_code == 200
    return [(x["style_name"], x["event_count"], x["file_count"]) for x in r.json()]


@pytest.mark.asyncio
async def test_sorted_by_events_then_files_then_name(client, env):  # noqa: F811
    pid = _seed(env, {
        "ManyFilesNoEvents": (8, 0),   # 8 files, 0 events
        "Big": (1, 100),               # 100 events / 1 file
        "Wide": (8, 6),                # 48 events / 8 files
        "TieB": (2, 10),               # 20 events / 2 files
        "TieA": (2, 10),               # exact tie with TieB -> name
        "TieWide": (4, 5),             # 20 events / 4 files -> before TieA/TieB
        "Zero": (1, 0),
    })
    rows = await _names(client, pid)
    assert [r[0] for r in rows] == ["Big", "Wide", "TieWide", "TieA", "TieB", "ManyFilesNoEvents", "Zero"]
    assert [r[1] for r in rows] == sorted((r[1] for r in rows), reverse=True)
