"""`register` is the wire name for character-voice register; the Python
attribute is `speech_register` so it doesn't shadow BaseModel.register."""
import subprocess
import sys

import pytest

from app.api.routes.projects import CharacterVoiceApiOut, CharacterVoiceUpdateIn
from app.llm.schemas import CharacterVoiceOut, StyleBibleResponse, strict_json_schema


def test_models_import_without_shadow_warning():
    code = (
        "import warnings; warnings.simplefilter('error');"
        "import app.llm.schemas, app.api.routes.projects"
    )
    result = subprocess.run([sys.executable, "-W", "error", "-c", code], capture_output=True, text=True)
    assert "shadows an attribute" not in result.stderr, result.stderr


def test_llm_schema_exposes_register():
    for schema in (CharacterVoiceOut.model_json_schema(), strict_json_schema(CharacterVoiceOut)):
        assert "register" in schema["properties"]
        assert "speech_register" not in schema["properties"]
        assert "register" in schema["required"]
        assert schema["additionalProperties"] is False
    defs = strict_json_schema(StyleBibleResponse)["$defs"]["CharacterVoiceOut"]
    assert "register" in defs["properties"] and "speech_register" not in defs["properties"]


def test_llm_payload_validates_register():
    voice = CharacterVoiceOut.model_validate({"name": "A", "voice_note": "blunt", "register": "tykani"})
    assert voice.speech_register == "tykani"
    assert voice.model_dump(by_alias=True)["register"] == "tykani"
    assert voice.model_dump()["speech_register"] == "tykani"  # internal dicts unchanged
    with pytest.raises(Exception):
        CharacterVoiceOut.model_validate({"name": "A", "voice_note": "x", "speech_register": "y"})


@pytest.mark.parametrize("value", ["formal", None])
def test_api_out_serializes_as_register(value):
    out = CharacterVoiceApiOut(
        id=1, character_id=2, character_name="A", voice_note=None,
        register=value, origin="llm", locked=False,
    )
    assert out.speech_register == value
    for dumped in (out.model_dump(mode="json", by_alias=True), __import__("json").loads(out.model_dump_json(by_alias=True))):
        assert dumped["register"] == value
        assert "speech_register" not in dumped
    # also by python name, as the route now does
    assert CharacterVoiceApiOut(
        id=1, character_id=2, character_name="A", voice_note=None,
        speech_register=value, origin="llm", locked=False,
    ).speech_register == value


def test_api_in_accepts_register_and_explicit_null():
    body = CharacterVoiceUpdateIn.model_validate({"register": "vykani"})
    assert body.speech_register == "vykani"
    nulled = CharacterVoiceUpdateIn.model_validate({"register": None})
    assert nulled.speech_register is None and "speech_register" in nulled.model_fields_set
    assert CharacterVoiceUpdateIn.model_validate({}).model_fields_set == set()
    assert "register" in CharacterVoiceUpdateIn.model_json_schema()["properties"]


def test_api_openapi_uses_register():
    from app.main import app

    schemas = app.openapi()["components"]["schemas"]
    for name in ("CharacterVoiceUpdateIn", "CharacterVoiceApiOut"):
        props = schemas[name]["properties"]
        assert "register" in props and "speech_register" not in props
