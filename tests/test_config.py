"""Tests for configuration parsing and the timezone helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from config import ConfigError, TargetConfig, load_settings, parse_interval
from timeutils import IST, ensure_utc, fmt_dual, humanise_delta, to_ist, utcnow


class TestParseInterval:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("hourly", 60),
            ("HOURLY", 60),
            ("daily", 1440),
            ("weekly", 10080),
            ("15min", 15),
            ("90", 90),
            ("90m", 90),
            ("2h", 120),
            ("0.5h", 30),
        ],
    )
    def test_accepted_forms(self, raw, expected):
        assert parse_interval(raw) == expected

    def test_rejects_nonsense(self):
        with pytest.raises(ConfigError):
            parse_interval("whenever")

    def test_rejects_sub_minute(self):
        with pytest.raises(ConfigError):
            parse_interval("0")


class TestTargetConfig:
    def test_normalises_handle(self):
        assert TargetConfig(username="  @Campus_Confessions ").username == "campus_confessions"

    def test_label_defaults_to_username(self):
        assert TargetConfig(username="foo").label == "foo"


class TestLoadSettings:
    def test_works_with_an_empty_environment(self, monkeypatch, tmp_path):
        for key in list(os_environ_keys()):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("IG_DATA_DIR", str(tmp_path))
        settings = load_settings()
        assert settings.scraper_backend == "demo"
        assert settings.scrape_interval_minutes == 60
        assert settings.targets == ()

    def test_targets_are_parsed_deduped_and_normalised(self, monkeypatch, tmp_path):
        monkeypatch.setenv("IG_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("IG_TARGETS", " @One , two,ONE ,, three ")
        settings = load_settings()
        assert [t.username for t in settings.targets] == ["one", "two", "three"]

    def test_boolean_parsing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("IG_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("IG_HEADLESS", "false")
        assert load_settings().headless is False
        monkeypatch.setenv("IG_HEADLESS", "yes")
        assert load_settings().headless is True

    def test_bad_boolean_is_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setenv("IG_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("IG_HEADLESS", "maybe")
        with pytest.raises(ConfigError):
            load_settings()

    def test_proxy_config_shape(self, monkeypatch, tmp_path):
        monkeypatch.setenv("IG_DATA_DIR", str(tmp_path))
        assert load_settings().proxy_config is None
        monkeypatch.setenv("IG_PROXY_SERVER", "http://proxy.test:8080")
        assert load_settings().proxy_config == {"server": "http://proxy.test:8080"}
        monkeypatch.setenv("IG_PROXY_USERNAME", "u")
        monkeypatch.setenv("IG_PROXY_PASSWORD", "p")
        assert load_settings().proxy_config == {
            "server": "http://proxy.test:8080",
            "username": "u",
            "password": "p",
        }

    def test_live_scrape_validation_requires_credentials(self, monkeypatch, tmp_path):
        monkeypatch.setenv("IG_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("IG_SCRAPER_BACKEND", "playwright")
        monkeypatch.setenv("IG_TARGETS", "somebody")
        monkeypatch.delenv("IG_USERNAME", raising=False)
        with pytest.raises(ConfigError, match="IG_USERNAME"):
            load_settings().validate_for_live_scrape()

    def test_live_scrape_validation_requires_targets(self, monkeypatch, tmp_path):
        monkeypatch.setenv("IG_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("IG_SCRAPER_BACKEND", "playwright")
        monkeypatch.setenv("IG_USERNAME", "me")
        monkeypatch.delenv("IG_TARGETS", raising=False)
        with pytest.raises(ConfigError, match="No targets"):
            load_settings().validate_for_live_scrape()

    def test_dashboard_write_flags_default_on_and_can_be_disabled(
        self, monkeypatch, tmp_path
    ):
        """A public deployment must be able to lock the dashboard read-only."""
        monkeypatch.setenv("IG_DATA_DIR", str(tmp_path))
        settings = load_settings()
        assert settings.enable_demo_seed is True
        assert settings.enable_write_actions is True

        monkeypatch.setenv("IG_ENABLE_DEMO_SEED", "false")
        monkeypatch.setenv("IG_ENABLE_WRITE_ACTIONS", "false")
        settings = load_settings()
        assert settings.enable_demo_seed is False
        assert settings.enable_write_actions is False

    def test_session_path_is_keyed_by_account(self, monkeypatch, tmp_path):
        monkeypatch.setenv("IG_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("IG_USERNAME", "someaccount")
        settings = load_settings()
        assert settings.session_state_path.name == "someaccount.storage.json"


def os_environ_keys():
    import os

    return [key for key in os.environ if key.startswith("IG_")]


# ---------------------------------------------------------------------------
# Timezone helpers
# ---------------------------------------------------------------------------
class TestTimeUtils:
    def test_utcnow_is_aware(self):
        assert utcnow().tzinfo is not None

    def test_ensure_utc_treats_naive_as_utc(self):
        naive = datetime(2026, 8, 24, 12, 0, 0)
        assert ensure_utc(naive).tzinfo == timezone.utc
        assert ensure_utc(naive).hour == 12

    def test_ensure_utc_converts_other_zones(self):
        ist_noon = datetime(2026, 8, 24, 12, 0, tzinfo=IST)
        assert ensure_utc(ist_noon).hour == 6
        assert ensure_utc(ist_noon).minute == 30

    def test_to_ist_applies_the_half_hour_offset(self):
        moment = datetime(2026, 8, 24, 18, 30, tzinfo=timezone.utc)
        local = to_ist(moment)
        assert (local.hour, local.minute) == (0, 0)
        assert local.day == 25, "the +5:30 offset rolls the date over"

    def test_none_passes_through(self):
        assert to_ist(None) is None
        assert ensure_utc(None) is None
        assert fmt_dual(None) == ""

    def test_fmt_dual_shows_both_zones(self):
        text = fmt_dual(datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc))
        assert "UTC" in text and "IST" in text
        assert "12:00:00" in text and "17:30:00" in text

    @pytest.mark.parametrize(
        "delta,expected",
        [
            (timedelta(seconds=30), "30s ago"),
            (timedelta(minutes=5), "5m ago"),
            (timedelta(hours=3, minutes=12), "3h 12m ago"),
            (timedelta(days=2, hours=4), "2d 4h ago"),
        ],
    )
    def test_humanise_delta(self, delta, expected):
        now = utcnow()
        assert humanise_delta(now - delta, now=now) == expected
