from continuum.core.redaction import REDACTED, Redactor


def test_secret_values_and_keys_are_redacted(monkeypatch):
    monkeypatch.setenv("PROVIDER_API_KEY", "sk-super-secret-123")
    r = Redactor.from_environment()
    out = r({"note": "token sk-super-secret-123 leaked", "api_key": "abc",
             "nested": [{"Authorization": "Bearer x"}], "count": 3})
    assert "sk-super-secret-123" not in str(out)
    assert out["api_key"] == REDACTED
    assert out["nested"][0]["Authorization"] == REDACTED
    assert out["count"] == 3
