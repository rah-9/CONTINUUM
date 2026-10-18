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


def test_well_known_credentials_are_redacted_even_when_not_in_the_environment():
    """Chat capture stores what the user pasted: a colleague's key must not be kept."""
    r = Redactor()
    secrets = ["sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz012345",
               "sk-proj-AbCdEfGhIjKlMnOpQrStUv0123",
               "ghp_" + "a1B2" * 9, "github_pat_" + "A1b2_" * 10,
               "AKIAIOSFODNN7EXAMPLE", "xoxb-1234567890-abcdefghij",
               "AIza" + "B" * 35,
               "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
               "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"]
    for secret in secrets:
        out = r.text(f"here: {secret} ok")
        assert secret not in out and REDACTED in out and out.endswith(" ok"), secret
    for line in ('password = "hunter2hunter2x"', "API_KEY=Zq81kd93Jfa02Lm",
                 'token: "a8f9c0d1e2f3a4b5"'):
        assert REDACTED in r.text(line), line


def test_pattern_redaction_leaves_code_and_prose_alone():
    r = Redactor()
    for text in ("token = get_token(request)", "password_hash = hash_password(pw)",
                 "the secret is to keep functions small", "task-0123 sk-learn notes",
                 "sk-learn-pipeline-transformer-steps",
                 "def refresh_token_expiry(): ...", "AKIA is an AWS prefix",
                 "eyJ is base64 for {\"", "auth = AuthConfiguration.from_settings()"):
        assert r.text(text) == text, text
