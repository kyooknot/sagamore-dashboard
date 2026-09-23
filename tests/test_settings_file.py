"""The optional YAML config layer.

These are deliberately pure tests of `settings_file.load()` against an explicit
path — they never re-import `app.config`, because that module's dataclass field
defaults are evaluated at class-creation time and re-importing it mid-suite
would make test order matter.
"""

from __future__ import annotations

import os
import textwrap

import pytest

from app import settings_file

pytest.importorskip("yaml", reason="PyYAML is optional; the loader no-ops without it")


def _write(tmp_path, body: str):
    path = tmp_path / "sagamore.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_file_supplies_values_the_environment_lacks(tmp_path, monkeypatch):
    monkeypatch.delenv("HA_URL", raising=False)
    path = _write(tmp_path, """
        home_assistant:
          url: http://ha.test:8123
    """)
    applied = settings_file.load(path)
    assert applied["HA_URL"] == "http://ha.test:8123"


def test_an_explicit_environment_variable_always_wins(tmp_path, monkeypatch):
    """Secrets belong in the environment; the file must never override one."""
    monkeypatch.setenv("HA_TOKEN", "from-env")
    path = _write(tmp_path, """
        home_assistant:
          token: from-file
    """)
    applied = settings_file.load(path)
    assert os.environ["HA_TOKEN"] == "from-env"   # the file did not overwrite it
    assert "HA_TOKEN" not in applied              # and load() reports it as untouched


def test_lists_become_the_comma_form_the_app_already_parses(tmp_path, monkeypatch):
    monkeypatch.delenv("PVE_NODES", raising=False)
    monkeypatch.delenv("HA_IGNORE_DEVICES", raising=False)
    path = _write(tmp_path, """
        proxmox:
          nodes: [alpha, beta]
        ignore_devices: [plex_client, cast_]
    """)
    applied = settings_file.load(path)
    assert applied["PVE_NODES"] == "alpha,beta"
    assert applied["HA_IGNORE_DEVICES"] == "plex_client,cast_"


def test_booleans_and_numbers_render_as_the_environment_would_carry_them(tmp_path, monkeypatch):
    monkeypatch.delenv("PVE_VERIFY_TLS", raising=False)
    monkeypatch.delenv("UNIFI_NEW_DEVICE_DAYS", raising=False)
    path = _write(tmp_path, """
        proxmox:
          verify_tls: true
        unifi:
          new_device_days: 14
    """)
    applied = settings_file.load(path)
    assert applied["PVE_VERIFY_TLS"] == "true"       # not "True"
    assert applied["UNIFI_NEW_DEVICE_DAYS"] == "14"


def test_favorites_translate_to_the_pipe_form(tmp_path, monkeypatch):
    """YAML is the nice way to write them; the app reads Name|url|icon."""
    monkeypatch.delenv("FAVORITES", raising=False)
    path = _write(tmp_path, """
        favorites:
          - name: Plex
            url: https://plex.test
            icon: plex
          - name: Proxmox
            url: https://pve.test
            icon: proxmox
    """)
    applied = settings_file.load(path)
    assert applied["FAVORITES"] == "Plex|https://plex.test|plex,Proxmox|https://pve.test|proxmox"


def test_a_favorite_missing_its_url_is_skipped_not_fatal(tmp_path, monkeypatch):
    monkeypatch.delenv("FAVORITES", raising=False)
    path = _write(tmp_path, """
        favorites:
          - name: Fine
            url: https://fine.test
            icon: ok
          - name: Broken
    """)
    applied = settings_file.load(path)
    assert applied["FAVORITES"] == "Fine|https://fine.test|ok"


def test_malformed_yaml_is_ignored_rather_than_fatal(tmp_path):
    """A dashboard that refuses to boot is worse than one on defaults."""
    path = tmp_path / "bad.yaml"
    path.write_text("this: [is: not: valid: yaml\n", encoding="utf-8")
    assert settings_file.load(path) == {}


def test_a_missing_file_is_a_no_op(tmp_path):
    assert settings_file.load(tmp_path / "nope.yaml") == {}


def test_a_yaml_file_that_is_not_a_mapping_is_ignored(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert settings_file.load(path) == {}
