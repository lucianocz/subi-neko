"""Disjoint live accounting over existing attempt records, including retries."""
from sqlalchemy import case, func, select

from app.db.models import LlmCall


def usage_statement(project_id: int):
    category = case(
        ((LlmCall.file_id.is_(None) & LlmCall.subtitle_chunk_id.is_(None)), "Other"),
        else_="Episodes",
    ).label("category")
    return (
        select(category, LlmCall.model, func.count(LlmCall.id).label("calls"),
               func.coalesce(func.sum(LlmCall.prompt_tokens), 0).label("prompt_tokens"),
               func.coalesce(func.sum(LlmCall.completion_tokens), 0).label("completion_tokens"),
               func.sum(LlmCall.cost_usd).label("cost_usd"))
        .where(LlmCall.project_id == project_id)
        .group_by(category, LlmCall.model).order_by(category, LlmCall.model)
    )
