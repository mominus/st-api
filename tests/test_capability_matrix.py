import json

import pytest

from app.services.capability_matrix import (
    build_claude_code_capability_matrix,
    parse_capability_overrides,
    validate_capability_overrides,
)


def test_build_claude_code_capability_matrix_applies_mapping_and_overrides():
    matrix = build_claude_code_capability_matrix(
        model="claude-sonnet-4-5",
        input_mapping={
            "system_prompt": "in-system",
            "metadata": "in-meta",
            "temperature": "in-temp",
            "thinking": "in-thinking",
        },
        capability_overrides={
            "tool_use": {
                "status": "native",
                "detail": "Workflow handles tool calls natively.",
                "mapped_field": "in-tools",
            },
            "image_input": {
                "status": "simulated",
                "detail": "Workflow accepts image payloads through a custom adapter.",
            },
        },
    )

    capabilities = matrix["capabilities"]

    assert matrix["profile"] == "claude_code"
    assert matrix["model"] == "claude-sonnet-4-5"
    assert capabilities["system_prompt"]["status"] == "native"
    assert capabilities["system_prompt"]["mapped_field"] == "in-system"
    assert capabilities["metadata"]["status"] == "native"
    assert capabilities["metadata"]["mapped_field"] == "in-meta"
    assert capabilities["temperature"]["status"] == "native"
    assert capabilities["temperature"]["mapped_field"] == "in-temp"
    assert capabilities["thinking"]["status"] == "simulated"
    assert capabilities["thinking"]["mapped_field"] == "in-thinking"
    assert capabilities["tool_use"]["status"] == "native"
    assert capabilities["tool_use"]["mapped_field"] == "in-tools"
    assert capabilities["image_input"]["status"] == "simulated"
    assert "custom adapter" in capabilities["image_input"]["detail"]
    assert sum(matrix["summary"].values()) == len(capabilities)
    assert matrix["summary"]["native"] == 4


def test_validate_capability_overrides_rejects_unknown_or_invalid_items():
    with pytest.raises(ValueError, match="Unknown capability override"):
        validate_capability_overrides({"not_real": "native"})

    with pytest.raises(ValueError, match="Invalid capability status"):
        validate_capability_overrides({"tool_use": "maybe"})

    with pytest.raises(ValueError, match="must be a string or object"):
        validate_capability_overrides({"tool_use": ["native"]})


def test_parse_capability_overrides_from_json_string_filters_invalid_entries():
    parsed = parse_capability_overrides(
        json.dumps(
            {
                "system_prompt": "native",
                "image_input": {
                    "status": "simulated",
                    "detail": "Special workflow support.",
                },
                "unknown_capability": "unsupported",
                "tool_use": ["bad-shape"],
            }
        )
    )

    assert parsed == {
        "system_prompt": {"status": "native"},
        "image_input": {
            "status": "simulated",
            "detail": "Special workflow support.",
        },
    }
