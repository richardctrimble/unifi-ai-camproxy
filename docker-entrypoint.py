#!/usr/bin/env python3
"""
docker-entrypoint.py (runs as Python)

Bridges TrueNAS Scale (or any orchestrator that passes env vars) into
the config.yml format the app expects, then dispatches to the right
mode (ONVIF bridge by default, full spoof+inference if requested).

Logic:
  1. If /config/config.yml already exists AND UNIFI_HOST is set ->
     UNIFI_HOST is always applied (controller can move); credentials
     (username/password/api_key) are only seeded if the field is
     currently empty, so web-UI-saved values survive container upgrades.
     Cameras and other settings are never touched.
  2. If /config/config.yml already exists and no UNIFI_HOST is set ->
     use it as-is (standalone Docker, user-managed).
  3. If no config.yml but UNIFI_HOST is set -> generate a minimal
     config.yml with just the controller connection + web tool enabled.
  4. Otherwise -> crash with a helpful message.

Mode dispatch:
  - APP_IMAGE_VARIANT=onvif (default)  -> exec `python -m onvif_bridge.main`
  - APP_IMAGE_VARIANT=full             -> exec `python main.py /config/config.yml`
  - APP_IMAGE_VARIANT=detect           -> exec `python -m detect.main`
  - APP_IMAGE_VARIANT=detect-cuda      -> exec `python -m detect.main`
  - APP_IMAGE_VARIANT=lines            -> exec `python -m lines.main`

Environment variables (all optional if config.yml exists):
  UNIFI_HOST, UNIFI_USERNAME, UNIFI_PASSWORD, UNIFI_TOKEN, UNIFI_API_KEY
  ONVIF_USERNAME, ONVIF_PASSWORD     (bridge mode — fleet-wide ONVIF creds)
  ALARM_WEBHOOK_URL                  (bridge mode — Alarm Manager URL)
  WEB_TOOL_PORT
"""

import os
import sys
from pathlib import Path

import yaml

CONFIG_PATH = Path("/config/config.yml")


def apply_env_overrides():
    """Seed an existing config.yml from env vars where fields are missing.

    UNIFI_HOST always updates (controller location can legitimately change).
    Credentials (username, password, api_key, token) and ONVIF creds are only
    written when the config field is currently empty — once saved via the web
    UI or a previous run they are left untouched, so a container upgrade never
    silently overwrites them.
    Cameras, AI settings, web_tool, and everything else are always preserved.
    """
    host = os.environ.get("UNIFI_HOST")
    if not host:
        return  # no env overrides requested

    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as e:
        print(
            f"WARNING: Could not read {CONFIG_PATH} ({e}). "
            "Skipping env var overrides — fix the file or delete it to regenerate.",
            file=sys.stderr,
        )
        return

    unifi = cfg.setdefault("unifi", {})
    changed = False

    if unifi.get("host") != host:
        unifi["host"] = host
        changed = True

    for env_key, cfg_key in [
        ("UNIFI_USERNAME", "username"),
        ("UNIFI_PASSWORD", "password"),
        ("UNIFI_TOKEN", "token"),
        ("UNIFI_API_KEY", "api_key"),
    ]:
        val = os.environ.get(env_key)
        # Only seed the credential if the config field is currently empty —
        # once a value exists (set via the web UI or a previous run) we leave
        # it alone so a container upgrade doesn't silently overwrite it.
        if val and not unifi.get(cfg_key):
            unifi[cfg_key] = val
            changed = True

    # Bridge-mode-specific: same seed-only logic — don't overwrite saved values.
    onvif = cfg.setdefault("onvif", {})
    for env_key, cfg_key in [
        ("ONVIF_USERNAME", "username"),
        ("ONVIF_PASSWORD", "password"),
    ]:
        val = os.environ.get(env_key)
        if val and not onvif.get(cfg_key):
            onvif[cfg_key] = val
            changed = True
    if not onvif:
        cfg.pop("onvif", None)

    alarm_url = os.environ.get("ALARM_WEBHOOK_URL")
    if alarm_url:
        alarms = cfg.setdefault("alarms", {})
        if alarms.get("webhook_url") != alarm_url:
            alarms["webhook_url"] = alarm_url
            changed = True

    web_port = os.environ.get("WEB_TOOL_PORT")
    if web_port:
        web_cfg = cfg.setdefault("web_tool", {})
        try:
            port_int = int(web_port)
            if web_cfg.get("port") != port_int:
                web_cfg["port"] = port_int
                changed = True
        except ValueError:
            print(
                f"WARNING: WEB_TOOL_PORT={web_port!r} is not a valid integer, ignoring.",
                file=sys.stderr,
            )

    if changed:
        try:
            CONFIG_PATH.write_text(yaml.dump(cfg, default_flow_style=False, sort_keys=False))
        except OSError as e:
            print(
                f"WARNING: Could not write updated config to {CONFIG_PATH}: {e}",
                file=sys.stderr,
            )
            return
        print(f"Updated {CONFIG_PATH} with environment variable overrides")
    else:
        print(f"Using existing {CONFIG_PATH} (env vars match)")


def generate_config():
    """Build a minimal config.yml from environment variables."""
    host = os.environ.get("UNIFI_HOST", "")
    username = os.environ.get("UNIFI_USERNAME", "")
    password = os.environ.get("UNIFI_PASSWORD", "")
    token = os.environ.get("UNIFI_TOKEN", "")
    api_key = os.environ.get("UNIFI_API_KEY", "")
    onvif_user = os.environ.get("ONVIF_USERNAME", "")
    onvif_pwd = os.environ.get("ONVIF_PASSWORD", "")
    alarm_url = os.environ.get("ALARM_WEBHOOK_URL", "")
    web_port = int(os.environ.get("WEB_TOOL_PORT", "8091"))

    lines = []
    lines.append("# Auto-generated by docker-entrypoint.")
    lines.append("# Edit via the web UI at")
    lines.append(f"# http://<host>:{web_port}/")
    lines.append("")
    lines.append("unifi:")
    lines.append(f"  host: {_quote(host)}")
    if username:
        lines.append(f"  username: {_quote(username)}")
    if password:
        lines.append(f"  password: {_quote(password)}")
    if token:
        lines.append(f"  token: {_quote(token)}")
    if api_key:
        lines.append(f"  api_key: {_quote(api_key)}")
    lines.append("")
    if onvif_user or onvif_pwd:
        lines.append("onvif:")
        if onvif_user:
            lines.append(f"  username: {_quote(onvif_user)}")
        if onvif_pwd:
            lines.append(f"  password: {_quote(onvif_pwd)}")
        lines.append("")
    if alarm_url:
        lines.append("alarms:")
        lines.append(f"  webhook_url: {_quote(alarm_url)}")
        lines.append("")
    lines.append("web_tool:")
    lines.append("  enabled: true")
    lines.append(f"  port: {web_port}")
    lines.append("")
    lines.append("cameras: []")
    lines.append("")

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text("\n".join(lines) + "\n")
    print(f"Generated {CONFIG_PATH} — open http://<host>:{web_port}/")


def _quote(val: str) -> str:
    """YAML-safe quoting for a string value."""
    if not val:
        return '""'
    if any(c in val for c in ":#{}[]|>&*!%@`\"'\\,\n") or val.strip() != val:
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return f'"{val}"'


def main():
    if CONFIG_PATH.exists():
        apply_env_overrides()
    elif os.environ.get("UNIFI_HOST"):
        generate_config()
    else:
        print(
            f"ERROR: No {CONFIG_PATH} found and UNIFI_HOST not set.\n"
            "Either:\n"
            "  - Mount a config.yml at /config/config.yml, or\n"
            "  - Set UNIFI_HOST environment variable\n"
            "    (TrueNAS app catalog does this automatically).",
            file=sys.stderr,
        )
        sys.exit(1)

    # Hand off to the right mode entrypoint based on the image variant.
    variant = os.environ.get("APP_IMAGE_VARIANT", "onvif").lower()
    if variant in ("ai", "ai-cuda", "full", "detect", "detect-cuda", "lines"):
        # Local AI inference mode (person/vehicle detection + line crossing).
        # Legacy variant names kept as backward-compat aliases.
        os.execvp(sys.executable, [sys.executable, "main.py", str(CONFIG_PATH)])
    else:
        # Default: ONVIF bridge (variant == "onvif" or unrecognised)
        os.execvp(sys.executable, [sys.executable, "-m", "onvif_bridge.main"])


if __name__ == "__main__":
    main()
