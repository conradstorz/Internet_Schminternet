"""Tests for config.py — loading, defaults, and deep-merge behaviour."""

import os
import textwrap

import pytest
import yaml

from config import load_config


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
    assert "segments" in cfg["leds"]
