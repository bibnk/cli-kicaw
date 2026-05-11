from __future__ import annotations

from hashminer.config import Config, _apply_toml
from hashminer.gpu import GpuFarm, GpuWorker


class DummyDevice:
    index = 0
    platform_name = "test"
    name = "dummy"
    type = "GPU"
    is_gpu = True
    compute_units = 1
    global_mem_mb = 1024
    max_work_group_size = 1024
    _device = object()


def test_default_batch_target_ms_is_500():
    assert Config().batch_target_ms == 500.0


def test_config_accepts_gpu_batch_target_ms_from_toml():
    cfg = _apply_toml(Config(), {"gpu": {"local_size": 256, "batch_target_ms": 250.0}})

    assert cfg.local_size == 256
    assert cfg.batch_target_ms == 250.0


def test_gpu_farm_passes_batch_target_ms_to_workers(monkeypatch):
    captured = {}

    def fake_init(self, info, *, kernel_src=None, local_size=None, max_results=4096, batch_target_ms=None):
        captured["local_size"] = local_size
        captured["batch_target_ms"] = batch_target_ms

    monkeypatch.setattr(GpuWorker, "__init__", fake_init)

    GpuFarm([DummyDevice()], local_size=256, batch_target_ms=250.0)

    assert captured == {"local_size": 256, "batch_target_ms": 250.0}
