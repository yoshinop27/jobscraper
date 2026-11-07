"""Settings = config.toml (defaults) overlaid with data/settings.json (changes made in the web UI)."""
from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.toml"
OVERRIDES = ROOT / "data" / "settings.json"

# What the web UI may change: section -> {key: type}
EDITABLE = {
    "search": {"roles": list, "cities": list, "include_remote": bool, "keep_unknown_location": bool,
               "min_hourly": float, "other_companies": str},
    "poll": {"fast_minutes": float, "slow_minutes": float, "lists_minutes": float},
    "notify": {"macos": bool, "ntfy_topic": str},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def overrides() -> dict:
    try:
        return json.loads(OVERRIDES.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_overrides(data: dict) -> None:
    OVERRIDES.parent.mkdir(exist_ok=True)
    OVERRIDES.write_text(json.dumps(data, indent=1))


def load_config() -> dict:
    with open(CONFIG, "rb") as f:
        base = tomllib.load(f)
    return _merge(base, {k: v for k, v in overrides().items() if k in EDITABLE})


def _coerce(name: str, typ: type, value):
    if typ is list:
        if not isinstance(value, list):
            raise ValueError(f"{name} must be a list")
        return value
    return typ(value)


def _validated(section: str, vals) -> dict:
    allowed = EDITABLE.get(section)
    if not allowed or not isinstance(vals, dict):
        raise ValueError(f"unknown section {section!r}")
    out = {}
    for k, v in vals.items():
        if k not in allowed:
            raise ValueError(f"{section}.{k} can't be changed here")
        out[k] = _coerce(f"{section}.{k}", allowed[k], v)
    return out


def save_settings(changes: dict) -> dict:
    """Validate UI changes against EDITABLE and persist them. Returns the new effective config."""
    cur = overrides()
    for section, vals in changes.items():
        for k, v in _validated(section, vals).items():
            cur.setdefault(section, {})[k] = v
    _write_overrides(cur)
    return load_config()


def get_flag(name: str, default=None):
    return overrides().get("_ui", {}).get(name, default)


def set_flag(name: str, value) -> None:
    cur = overrides()
    cur.setdefault("_ui", {})[name] = value
    _write_overrides(cur)
