import pytest

from continuum.tools.manifest import ToolManifest

BASE = {"name": "x.put", "version": "1", "effect": "write",
        "args_schema": {"type": "object", "additionalProperties": False,
                        "properties": {"p": {"type": "string"}}},
        "operation_identity": {"fields": ["p"]}}


def test_valid_write_manifest():
    ToolManifest.model_validate(BASE)


@pytest.mark.parametrize(("patch", "message"), [
    ({"operation_identity": {"fields": []}}, "operation_identity"),
    ({"args_schema": {"type": "object", "properties": {"p": {"type": "string"}}}},
     "additionalProperties"),
    ({"reconciliation": {"method": "opaque", "negative_proof_is_safe": True}},
     "negative proof"),
    ({"reconciliation": {"method": "authoritative_query"}}, "check_tool"),
    ({"operation_identity": {"fields": ["missing"]}}, "not in schema"),
])
def test_unsafe_manifests_rejected(patch, message):
    with pytest.raises(ValueError, match=message):
        ToolManifest.model_validate({**BASE, **patch})
