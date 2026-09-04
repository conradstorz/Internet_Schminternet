"""Tests for config.py — loading, defaults, and deep-merge behaviour."""

import os
import textwrap

import pytest
import yaml

from config import DEFAULT_CONFIG, load_config


def test_defaults_when_file_missing():
    """load_config returns built-in defaults when the config file doesn't exist."""
    cfg = load_config("__nonexistent_config__.yaml")
    assert "monitors" in cfg
    assert cfg["monitors"]["ping"]["count"] == 5
    assert cfg["monitors"]["dns"]["test_domain"] == "google.com"
    assert cfg["leds"]["enabled"] is False


def test_user_values_override_defaults(tmp_path):
    """Values in config.yaml override their corresponding defaults."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        textwrap.dedent("""\
            monitors:
              ping:
                count: 10
                targets:
                  - "192.168.1.1"
            web:
              port: 9090
        """),
        encoding="utf-8",
    )
    cfg = load_config(str(cfg_file))
    assert cfg["monitors"]["ping"]["count"] == 10
    assert cfg["monitors"]["ping"]["targets"] == ["192.168.1.1"]
    assert cfg["web"]["port"] == 9090


def test_deep_merge_preserves_sibling_defaults(tmp_path):
    """Overriding one key under a section leaves sibling keys at their defaults."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("web:\n  port: 7777\n", encoding="utf-8")
    cfg = load_config(str(cfg_file))
    # Sibling key 'host' should still be the default
    assert cfg["web"]["host"] == "0.0.0.0"
    assert cfg["web"]["port"] == 7777


def test_leds_section(tmp_path):
    """LED config is merged correctly."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "leds:\n  enabled: true\n  count: 30\n", encoding="utf-8"
    )
    cfg = load_config(str(cfg_file))
    assert cfg["leds"]["enabled"] is True
    assert cfg["leds"]["count"] == 30
    # Other LED keys still present
    assert "weights" in cfg["leds"]
    assert cfg["leds"]["orientation"] == "top_down"
    assert cfg["leds"]["overall"] is True


def test_leds_default_weights_orientation_and_overall():
    """New rank-sorted LED strip config: speedtest is downweighted by default."""
    leds = DEFAULT_CONFIG["leds"]
    assert leds["weights"] == {"ping": 1.0, "dns": 1.0, "http": 1.0, "speedtest": 0.25}
    assert leds["orientation"] == "top_down"
    assert leds["overall"] is True
    assert "segments" not in leds


def test_leds_weights_override_leaves_siblings_at_default(tmp_path):
    """Overriding one weight leaves the other default weights (and
    orientation) untouched, thanks to the deep merge."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        textwrap.dedent("""\
            leds:
              weights:
                speedtest: 0.5
        """),
        encoding="utf-8",
    )
    cfg = load_config(str(cfg_file))
    assert cfg["leds"]["weights"] == {"ping": 1.0, "dns": 1.0, "http": 1.0, "speedtest": 0.5}
    assert cfg["leds"]["orientation"] == "top_down"


def test_degraded_alert_keys_default_to_none():
    """Every monitor has both degraded-alert keys, defaulting to None (feature off)."""
    for name, monitor in DEFAULT_CONFIG["monitors"].items():
        assert "degraded_alert_minutes" in monitor, f"{name} missing minutes key"
        assert "degraded_alert_cycles" in monitor, f"{name} missing cycles key"
        assert monitor["degraded_alert_minutes"] is None
        assert monitor["degraded_alert_cycles"] is None


def test_degraded_alert_minutes_override(tmp_path):
    """Setting degraded_alert_minutes leaves the cycles key and thresholds untouched."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        textwrap.dedent("""\
            monitors:
              ping:
                degraded_alert_minutes: 10
        """),
        encoding="utf-8",
    )
    cfg = load_config(str(cfg_file))
    ping = cfg["monitors"]["ping"]
    assert ping["degraded_alert_minutes"] == 10
    assert ping["degraded_alert_cycles"] is None
    assert ping["thresholds"] == DEFAULT_CONFIG["monitors"]["ping"]["thresholds"]


class TestLedAnimationDefaults:
    def test_animation_defaults_are_present(self):
        from config import DEFAULT_CONFIG

        animation = DEFAULT_CONFIG["leds"]["animation"]
        assert animation["enabled"] is True
        assert animation["period_seconds"] == 4.0
        assert animation["min_brightness"] == 0.7
        assert animation["fps"] == 25

    def test_user_config_can_override_one_animation_key(self, tmp_path):
        # Deep merge: overriding period_seconds must leave the other
        # animation keys at their defaults rather than replacing the dict.
        from config import load_config

        path = tmp_path / "config.yaml"
        path.write_text(
            "leds:\n"
            "  animation:\n"
            "    period_seconds: 10.0\n",
            encoding="utf-8",
        )
        config = load_config(str(path))
        animation = config["leds"]["animation"]
        assert animation["period_seconds"] == 10.0
        assert animation["min_brightness"] == 0.7
        assert animation["fps"] == 25
        assert animation["enabled"] is True
