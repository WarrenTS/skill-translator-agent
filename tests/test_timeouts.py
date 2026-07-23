from pathlib import Path

from translator_agent.timeouts import (
    completion_token_budget,
    control_timeout_seconds,
    effective_batch_chars,
    provider_timeout_seconds,
)

from test_config import make_config


def test_provider_timeout_grows_with_source_and_target_count():
    short = provider_timeout_seconds(30, source_chars=100, target_count=1)
    long_multilingual = provider_timeout_seconds(
        30, source_chars=10_000, target_count=3
    )

    assert short == 30
    assert long_multilingual == 215


def test_effective_batch_budget_accounts_for_multilingual_output():
    assert effective_batch_chars(12_000, target_count=1) == 6_000
    assert effective_batch_chars(12_000, target_count=3) == 2_000


def test_completion_budget_scales_and_respects_hard_cap():
    assert completion_token_budget(100, 1, 8192) == 622
    assert completion_token_budget(6000, 1, 8192) == 7112
    assert completion_token_budget(6000, 3, 8192) == 8192


def test_control_timeout_uses_mounted_file_size(tmp_path: Path):
    source = tmp_path / "sample.txt"
    source.write_text("x" * 10_000)
    config = make_config(
        input_root=tmp_path,
        timeout_seconds=30,
        max_retries=0,
    )
    payload = {
        "source": {"kind": "file", "path": "sample.txt"},
        "targets": ["zh-TW", "zh-HK", "ja"],
    }

    assert control_timeout_seconds(config, payload) == 830


def test_control_timeout_prompt_strategy_skips_format_ladder_multiplier(tmp_path: Path):
    source = tmp_path / "sample.txt"
    source.write_text("x" * 10_000)
    config = make_config(
        input_root=tmp_path,
        timeout_seconds=30,
        max_retries=0,
        response_format_strategy="prompt",
    )
    payload = {
        "source": {"kind": "file", "path": "sample.txt"},
        "targets": ["zh-TW", "zh-HK", "ja"],
    }

    assert control_timeout_seconds(config, payload) == 280
