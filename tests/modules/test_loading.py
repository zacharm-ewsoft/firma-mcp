"""How modules switch on and off from configuration."""

import pytest

from firma_mcp.errors import ConfigError
from firma_mcp.modules import all_modules, ksef, load_enabled, mail
from firma_mcp.modules.mail import env_key_for_box


def names(env) -> list[str]:
    return [module.name for module, _ in load_enabled(env)]


def test_every_module_enabled_with_full_configuration(env):
    assert names(env) == ["mail", "merit", "fakturownia", "ksef", "booking"]


def test_nothing_enabled_without_configuration():
    assert names({}) == []


def test_single_module(env):
    only_ksef = {k: v for k, v in env.items() if k.startswith("KSEF_")}
    assert names(only_ksef) == ["ksef"]


def test_booking_follows_its_requirements(env):
    assert "booking" not in names({k: v for k, v in env.items() if not k.startswith("KSEF_")})


def test_module_statuses():
    statuses = {module.name: module.status for module in all_modules()}
    assert statuses == {
        "mail": "production",
        "merit": "production",
        "ksef": "production",
        "booking": "production",
        "fakturownia": "experimental",
    }


def test_env_key_for_box():
    assert env_key_for_box("office@example.com") == "MAIL_PASSWORD__office_example_com"
    assert env_key_for_box("first-last@my-firm.example") == "MAIL_PASSWORD__first_last_my_firm_example"


def test_mail_load(env):
    config = mail.load(env)
    assert config.boxes == ("owner@example.com", "office@example.com")
    assert config.password("office@example.com") == "pw-office"
    assert (config.host, config.port) == ("mail.example.test", 993)
    with pytest.raises(ValueError, match="unknown mailbox"):
        config.password("someone@example.test")


def test_mail_missing_password_is_an_error(env):
    del env["MAIL_PASSWORD__office_example_com"]
    with pytest.raises(ConfigError, match="MAIL_PASSWORD__office_example_com"):
        mail.load(env)


def test_mail_host_without_boxes_is_an_error():
    with pytest.raises(ConfigError, match="MAIL_HOST and MAIL_BOXES"):
        mail.load({"MAIL_HOST": "mail.example.test"})


def test_ksef_load_validates_nip_and_env(env):
    assert ksef.load(env).nip == "1111111111"
    assert ksef.load({**env, "KSEF_NIP": "111-111-11-11"}).nip == "1111111111"
    with pytest.raises(ConfigError, match="10-digit"):
        ksef.load({**env, "KSEF_NIP": "123"})
    with pytest.raises(ConfigError, match="KSEF_ENV"):
        ksef.load({**env, "KSEF_ENV": "staging"})
