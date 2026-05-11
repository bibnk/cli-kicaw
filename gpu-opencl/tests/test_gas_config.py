from hashminer.config import Config, _apply_toml


def test_default_priority_fee_is_6_gwei():
    assert Config().gas.priority_gwei == 6.0


def test_toml_can_override_priority_fee():
    cfg = _apply_toml(Config(), {"gas": {"priority_gwei": 9.0}})
    assert cfg.gas.priority_gwei == 9.0
