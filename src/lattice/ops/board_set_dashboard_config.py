"""``board.set_dashboard_config``: the dashboard settings POST's rules.

``settings`` holds only the keys the dashboard's settings POST accepts
(``dashboard/server.py``), validated and merged into ``config["dashboard"]``
exactly as that handler does; a ``None`` (or, for ``background_image``, an
empty string) removes a setting. A key naming board configuration (workflow,
review, policy, hook, or any other top-level config key) is refused with
``FORBIDDEN``; any other unknown key with ``VALIDATION_ERROR``, as the POST
has always answered. Takes no actor: the POST never had one.
"""

from __future__ import annotations

from dataclasses import dataclass

from lattice.ops.base import OpContext, OpError, OpResult, operation
from lattice.storage.board_config import REMOVE, forbidden_config_key, update_config_key

DASHBOARD_SETTING_KEYS = frozenset(
    {
        "background_image",
        "column_width",
        "day_start_hour",
        "done_display",
        "font_size",
        "heat_map_enabled",
        "lane_colors",
        "lane_sort",
        "max_items_per_column",
        "theme",
        "voice",
    }
)

# Top-level board configuration keys, besides whatever the board's own
# config.json holds. Naming one in ``settings`` is an attempt to change board
# configuration, not a typo.
BOARD_CONFIG_KEYS = frozenset(
    {
        "auto_code_review_on_transition",
        "auto_plan_review_on_transition",
        "completion_policies",
        "dashboard",
        "dashboard_port",
        "default_actor",
        "default_priority",
        "default_status",
        "heartbeat",
        "hooks",
        "instance_id",
        "instance_name",
        "model",
        "plan_approval",
        "plan_review_mode",
        "project_code",
        "project_name",
        "project_type",
        "resources",
        "review_cycle_limit",
        "review_max_diff_chars",
        "review_max_diff_lines",
        "review_mode",
        "review_timeout_seconds",
        "schema_version",
        "subproject_code",
        "task_types",
        "workflow",
    }
)

# Settings removed by None; background_image is also removed by "".
_NULLABLE = (
    "theme",
    "voice",
    "column_width",
    "font_size",
    "done_display",
    "max_items_per_column",
    "day_start_hour",
)


@dataclass(frozen=True, kw_only=True)
class SetDashboardConfigParams:
    settings: dict


def _invalid(message: str) -> OpError:
    return OpError("VALIDATION_ERROR", message)


def _check_string_map(settings: dict, key: str, map_error: str) -> None:
    value = settings[key]
    if not isinstance(value, dict):
        raise _invalid(f"'{key}' must be an object")
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise _invalid(map_error)


def check_settings(settings: dict, config: dict) -> None:
    """Refuse *settings* the dashboard settings POST refuses, in its order."""
    forbidden = sorted(
        k
        for k in settings
        if k not in DASHBOARD_SETTING_KEYS and (k in BOARD_CONFIG_KEYS or k in config)
    )
    if forbidden:
        raise forbidden_config_key(forbidden[0])
    unknown = set(settings) - DASHBOARD_SETTING_KEYS
    if unknown:
        raise _invalid(f"Unknown keys: {', '.join(sorted(unknown))}")

    if "lane_colors" in settings:
        _check_string_map(settings, "lane_colors", "lane_colors keys and values must be strings")
    if "lane_sort" in settings:
        _check_string_map(settings, "lane_sort", "lane_sort keys and values must be strings")
    if "theme" in settings:
        theme = settings["theme"]
        if theme is not None and not isinstance(theme, str):
            raise _invalid("'theme' must be a string or null")
    if "background_image" in settings:
        bg = settings["background_image"]
        if bg is not None and not isinstance(bg, str):
            raise _invalid("'background_image' must be a string or null")
        if bg is not None and bg != "" and not bg.startswith(("http://", "https://")):
            raise _invalid("'background_image' must be an http or https URL")
    if "heat_map_enabled" in settings and not isinstance(settings["heat_map_enabled"], bool):
        raise _invalid("'heat_map_enabled' must be a boolean")
    if "done_display" in settings:
        dd = settings["done_display"]
        if dd is not None and dd not in ("all", "recent", "grouped"):
            raise _invalid("'done_display' must be 'all', 'recent', 'grouped', or null")
    if "day_start_hour" in settings:
        dsh = settings["day_start_hour"]
        if dsh is not None and (not isinstance(dsh, int) or dsh < 0 or dsh > 23):
            raise _invalid("'day_start_hour' must be an integer between 0 and 23, or null")
    if "voice" in settings and not isinstance(settings["voice"], str):
        raise _invalid("'voice' must be a string")
    if "column_width" in settings:
        cw = settings["column_width"]
        if cw is not None and (not isinstance(cw, (int, float)) or cw < 150 or cw > 800):
            raise _invalid("'column_width' must be a number between 150 and 800, or null")
    if "font_size" in settings:
        fs = settings["font_size"]
        if fs is not None and (not isinstance(fs, (int, float)) or fs < 6 or fs > 100):
            raise _invalid("'font_size' must be a number between 6 and 100, or null")


def merge_settings(dashboard: dict, settings: dict) -> dict:
    """*dashboard* with *settings* applied, as the POST merges them."""
    merged = dict(dashboard)
    for key in ("lane_colors", "lane_sort", "heat_map_enabled"):
        if key in settings:
            merged[key] = settings[key]
    if "background_image" in settings:
        bg = settings["background_image"]
        if bg is None or bg == "":
            merged.pop("background_image", None)
        else:
            merged["background_image"] = bg
    for key in _NULLABLE:
        if key in settings:
            if settings[key] is None:
                merged.pop(key, None)
            else:
                merged[key] = settings[key]
    return merged


@operation("board.set_dashboard_config")
class SetDashboardConfig:
    Params = SetDashboardConfigParams
    no_actor = True

    def run(self, ctx: OpContext, p: SetDashboardConfigParams) -> OpResult:
        check_settings(p.settings, ctx.config)

        def decide(config: dict) -> object:
            dashboard = merge_settings(config.get("dashboard", {}), p.settings)
            return dashboard if dashboard else REMOVE

        config, _ = update_config_key(ctx.lattice_dir, "dashboard", decide)
        return OpResult(value=config.get("dashboard", {}))
