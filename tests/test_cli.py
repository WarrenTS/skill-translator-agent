from translator_agent.cli import _control_timeout_seconds

from test_config import make_config


def test_control_timeout_scales_with_inline_input_targets_and_repairs():
    config = make_config(timeout_seconds=60, max_retries=1)
    payload = {
        "source": {"kind": "text", "text": "x" * 10_000},
        "targets": ["zh-TW", "zh-HK", "ja"],
    }

    assert _control_timeout_seconds(config, payload) == 1800


def test_control_timeout_without_repair_allows_one_provider_pass():
    config = make_config(timeout_seconds=60, max_retries=0)
    payload = {
        "source": {"kind": "text", "text": "short"},
        "targets": ["ja"],
    }

    assert _control_timeout_seconds(config, payload) == 185


def test_control_timeout_keeps_ten_second_minimum():
    config = make_config(timeout_seconds=1)

    assert _control_timeout_seconds(config) == 10
