"""Boss defaults keep search responsive while retaining bounded jitter."""

from jobagent.config import Settings


def test_boss_crawl_defaults_are_five_to_ten_seconds() -> None:
    settings = Settings(_env_file=None)

    assert settings.boss_crawl_rate_requests == 1
    assert settings.boss_crawl_rate_period_seconds == 5.0
    assert settings.boss_crawl_jitter_min_seconds == 5.0
    assert settings.boss_crawl_jitter_max_seconds == 10.0
