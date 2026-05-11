from hashminer.config import Config, _apply_toml


def test_default_priority_fee_is_10_gwei():
    cfg = Config()
    assert cfg.gas.priority_gwei == 10.0
    assert cfg.gas.max_fee_gwei == 100.0
    assert cfg.gas.gas_limit == 250000


def test_toml_can_override_priority_fee():
    cfg = _apply_toml(Config(), {"gas": {"priority_gwei": 9.0}})
    assert cfg.gas.priority_gwei == 9.0
