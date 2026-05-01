"""Tests for plugin manifest — required fields, version sync, and Claude Code schema conformance.

Schema reference: https://code.claude.com/docs/en/plugins-reference.md#user-configuration

The userConfig validator catches unknown keys (like `enum`) that broke install
in claude-browser-bridge 3.3.0. Keep the whitelist tight.
"""

import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

USER_CONFIG_COMMON_KEYS = {"type", "title", "description", "default", "required", "sensitive"}
USER_CONFIG_TYPE_KEYS = {
    "string": USER_CONFIG_COMMON_KEYS | {"multiple"},
    "number": USER_CONFIG_COMMON_KEYS | {"min", "max"},
    "boolean": USER_CONFIG_COMMON_KEYS,
    "directory": USER_CONFIG_COMMON_KEYS,
    "file": USER_CONFIG_COMMON_KEYS,
}


def _read_json(path):
    with open(path) as f:
        return json.load(f)


@pytest.fixture
def plugin_json():
    return _read_json(os.path.join(ROOT, ".claude-plugin", "plugin.json"))


@pytest.fixture
def package_json():
    return _read_json(os.path.join(ROOT, "package.json"))


def test_required_fields(plugin_json):
    for field in ["name", "description", "version", "author", "keywords"]:
        assert field in plugin_json, f"Missing required field: {field}"


def test_name(plugin_json):
    assert plugin_json["name"] == "session-bridge"


def test_semver(plugin_json):
    assert re.match(r"^\d+\.\d+\.\d+$", plugin_json["version"]), \
        f"Version {plugin_json['version']} is not semver"


def test_version_matches_package_json(plugin_json, package_json):
    assert plugin_json["version"] == package_json["version"], (
        f"plugin.json version {plugin_json['version']} != "
        f"package.json version {package_json['version']}"
    )


def test_mcp_server_config(plugin_json):
    assert "mcpServers" in plugin_json
    assert "session-bridge" in plugin_json["mcpServers"]
    server = plugin_json["mcpServers"]["session-bridge"]
    assert server["command"] == "node"


def test_user_config_types(plugin_json):
    """Every userConfig entry must declare a recognized type."""
    for name, entry in plugin_json.get("userConfig", {}).items():
        assert "type" in entry, f"userConfig.{name} missing 'type'"
        assert entry["type"] in USER_CONFIG_TYPE_KEYS, (
            f"userConfig.{name}.type={entry['type']!r} is not a recognized "
            f"type. Valid: {sorted(USER_CONFIG_TYPE_KEYS)}"
        )


def test_user_config_schema_strict(plugin_json):
    """userConfig entries must only use keys from the official schema.

    Catches regressions like an `enum` field — the Claude Code manifest schema
    does not support `enum`, `pattern`, or arbitrary JSON Schema keywords.
    """
    for name, entry in plugin_json.get("userConfig", {}).items():
        allowed = USER_CONFIG_TYPE_KEYS.get(entry.get("type"), USER_CONFIG_COMMON_KEYS)
        unknown = set(entry.keys()) - allowed
        assert not unknown, (
            f"userConfig.{name} contains unknown keys: {sorted(unknown)}. "
            f"Allowed for type={entry.get('type')!r}: {sorted(allowed)}. "
            f"See https://code.claude.com/docs/en/plugins-reference.md#user-configuration"
        )
