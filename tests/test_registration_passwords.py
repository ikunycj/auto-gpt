from config import register as register_cfg
from registration.application.passwords import generate_registration_password, registration_password


def test_generated_registration_password_is_unique_and_complex():
    values = {generate_registration_password() for _ in range(8)}

    assert len(values) == 8
    for value in values:
        assert len(value) == 14
        assert any(ch.isupper() for ch in value)
        assert any(ch.islower() for ch in value)
        assert any(ch.isdigit() for ch in value)
        assert any(not ch.isalnum() for ch in value)


def test_configured_registration_password_is_used_verbatim(monkeypatch):
    monkeypatch.setattr(register_cfg, "REGISTER_PASSWORD", "Configured-Password-42!")

    assert registration_password() == "Configured-Password-42!"
