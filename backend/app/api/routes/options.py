from typing import Annotated

from fastapi import APIRouter, Body

from app.db import options as options_store

router = APIRouter(prefix="/options", tags=["options"])

# Secrets are never sent to the browser in full: GET returns a mask and
# PATCH treats the same mask as "unchanged" so a round-tripped options form
# can't clobber the stored value.
_SECRET_KEYS = {"OPENAI_API_KEY"}


def _mask_secret(value: str | None) -> str | None:
    if not value:
        return value
    tail = value[-4:] if len(value) >= 12 else ""
    return f"••••••••{tail}"


@router.get("", response_model=dict[str, str | None])
async def get_options() -> dict[str, str | None]:
    opts = await options_store.asnapshot()
    return {
        "TARGET_LANG_NAME": opts.target_lang_name,
        "TARGET_LANG_CODE": opts.target_lang_code,
        "CHUNK_SIZE": str(opts.chunk_size),
        "PREPEND_CONTEXT_SIZE": str(opts.prepend_context_size),
        "LOOKAHEAD_CONTEXT_SIZE": str(opts.lookahead_context_size),
        "OPENAI_API_BASE": opts.openai_api_base,
        "OPENAI_API_KEY": _mask_secret(opts.openai_api_key),
        "OPENAI_MODEL_CHEAP": opts.openai_model_cheap,
        "OPENAI_MODEL_BETTER": opts.openai_model_better,
        "LLM_STRUCTURED_OUTPUTS": opts.llm_structured_outputs,
        "LLM_PRICES_JSON": opts.llm_prices_json,
        "LLM_MAX_COMPLETION_TOKENS": str(opts.llm_max_completion_tokens),
        "TRANSLATE_KARAOKE": "1" if opts.translate_karaoke else "0",
        "REQUIRE_STYLE_BIBLE": "1" if opts.require_style_bible else "0",
        "CPS_LIMIT": str(opts.cps_limit),
        "MAX_ROW_CHARS": str(opts.max_row_chars),
        "AUTO_LINE_BREAK": "1" if opts.auto_line_break else "0",
        "AUTO_ACCEPT_POLICY": opts.auto_accept_policy,
        "AUTO_MAPPING_ACCEPT_THRESHOLD": str(opts.auto_mapping_accept_threshold),
        "TRANSLATION_CONFIDENCE_FLAG_THRESHOLD": str(opts.translation_confidence_flag_threshold),
        "LOG_LEVEL": opts.log_level,
        "JOB_WORKER_COUNT": str(opts.job_worker_count),
        "TRANSLATION_PROMPT": opts.translation_prompt,
        "REPAIR_PROMPT": opts.repair_prompt,
        "POLISH_PROMPT": opts.polish_prompt,
        "FINAL_QA_PROMPT": opts.final_qa_prompt,
        "SIGN_TRANSLATION_PROMPT": opts.sign_translation_prompt,
        "SONG_TRANSLATION_PROMPT": opts.song_translation_prompt,
        "ANALYZE_PROMPT": opts.analyze_prompt,
        "MAPPING_PROMPT": opts.mapping_prompt,
        "STYLE_BIBLE_PROMPT": opts.style_bible_prompt,
        "STYLE_BIBLE_UPDATE_PROMPT": opts.style_bible_update_prompt,
    }


@router.patch("", status_code=204)
async def patch_options(
    body: Annotated[dict[str, str | None], Body()],
) -> None:
    for key, value in body.items():
        if key in _SECRET_KEYS:
            current = await options_store.aget(key)
            if value == _mask_secret(current):
                continue  # round-tripped mask — not a new secret
        await options_store.aset(key, value or None)
