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


def test_dictionary_word_url_passwords_never_corrupt_ordinary_text(monkeypatch):
    """The README's local DSN is continuum:continuum: redacting the bare word everywhere
    turned every brief's paths and commands into `[REDACTED]` (found by a handoff trial)."""
    from continuum.core.redaction import Redactor

    monkeypatch.setenv("CONTINUUM_DATABASE_URL",
                       "postgresql://continuum:continuum@localhost:5432/continuum")
    r = Redactor.from_environment()
    text = "edit src/continuum/core/redaction.py then run `continuum coding check`"
    assert r.text(text) == text
    # the credential is still hidden where it appears as a credential
    assert "continuum:continuum@" not in r.text("dsn=postgresql://continuum:continuum@db/x")


def test_secret_like_url_passwords_are_redacted_everywhere(monkeypatch):
    from continuum.core.redaction import Redactor

    monkeypatch.setenv("APP_DB_URL", "postgresql://u:S3cretPw0rd@db/x")
    assert "S3cretPw0rd" not in Redactor.from_environment().text("pw is S3cretPw0rd")
