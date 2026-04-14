from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import unrealsdk
from mods_base import CoopSupport, Game, build_mod, command, get_pc, keybind, open_in_mod_dir
from unrealsdk import logging
from unrealsdk.unreal import FGbxDefPtr, UObject, WrappedInlineStruct, WrappedStruct

try:
    import blimgui as _blimgui
except Exception as exc:
    _blimgui = None
    BLIMGUI_IMPORT_ERROR = str(exc)
else:
    BLIMGUI_IMPORT_ERROR = None

KIND = Literal[
    "rewards",
    "criteria_preset",
    "manufacturer",
    "rarity",
    "itempool",
    "itempoollist",
    "inv",
    "ui_mission_reward_type",
    "ui_mission_reward_tier",
]
GRANT_MODE = Literal["GiveReward", "GiveRewardAllPlayers"]
WINDOW_TITLE = "BL4 Reward Generator"
MOD_DIR = Path(__file__).resolve().parent
DATA_PATH = MOD_DIR / "data" / "merge.json"
ALL_CATEGORY_KEY = "__all__"
LOOKUP_KINDS: tuple[KIND, ...] = (
    "rewards",
    "criteria_preset",
    "manufacturer",
    "rarity",
    "itempool",
    "itempoollist",
    "inv",
    "ui_mission_reward_type",
    "ui_mission_reward_tier",
)
HANDLE_RE = re.compile(
    r"(?i)\b("
    r"rewards|criteria_preset|manufacturer|rarity|itempool|itempoollist|inv|"
    r"ui_mission_reward_type|ui_mission_reward_tier"
    r")'([^']+)'"
)
MAX_GENERATE_COUNT = 999
REWARDS_DEF_SCRIPT_PATHS = (
    "/Script/GbxGame.GbxRewardsDef",
    "/Script/OakGame.GbxRewardsDef",
)


def _log_info(message: str) -> None:
    logging.info(f"[BL4RewardGenerator] {message}")


def _log_warning(message: str) -> None:
    logging.warning(f"[BL4RewardGenerator] {message}")


def _log_error(message: str) -> None:
    logging.error(f"[BL4RewardGenerator] {message}")


def _normalize_text(value: Any) -> str:
    return str(value).strip().lower()


def _maybe_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return repr(value)


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _localized_tail(value: Any) -> str | None:
    text = _maybe_str(value)
    if not text:
        return None
    if "," not in text:
        return text.strip()
    return text.split(",", 2)[-1].strip() or text.strip()


def _parse_handle(value: str) -> tuple[KIND, str] | None:
    match = HANDLE_RE.search(value.strip())
    if match is None:
        return None
    return cast(KIND, match.group(1).lower()), _normalize_text(match.group(2))


def _normalize_handle(value: str) -> str | None:
    parsed = _parse_handle(value)
    if parsed is None:
        return None
    kind, key = parsed
    return f"{kind}'{key}'"


def _runtime_handle(kind: KIND, key: str) -> str:
    return f"{kind}'{key}'"


def _dedupe_preserve_order(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _extract_handles(value: Any) -> list[str]:
    return _dedupe_preserve_order([match.group(0) for match in HANDLE_RE.finditer(_safe_json(value))])


def _format_weight(value: Any) -> str:
    if isinstance(value, Mapping):
        constant = _maybe_str(value.get("constant"))
        if constant:
            return constant
        attribute = _maybe_str(value.get("attribute"))
        if attribute:
            return attribute
    if isinstance(value, (int, float)):
        return str(value)
    return _safe_json(value)


def _structtype_short(value: Any) -> str | None:
    text = _maybe_str(value)
    if not text:
        return None
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    if text.endswith("'"):
        text = text[:-1]
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text.strip() or None


def _field_label(name: str) -> str:
    return name.replace("_", " ").strip().title()


def _unwrap_struct_like(value: Any) -> WrappedStruct | None:
    if isinstance(value, WrappedStruct):
        return value
    if isinstance(value, WrappedInlineStruct):
        try:
            return value._experimental_instance
        except Exception:
            return None
    instance = getattr(value, "_experimental_instance", None)
    if isinstance(instance, WrappedStruct):
        return instance
    return None


def _parse_serials_from_multiline(value: str) -> list[str]:
    out: list[str] = []
    for line in value.splitlines():
        for part in line.split(","):
            serial = part.strip()
            if serial:
                out.append(serial)
    return out


def _parse_serials_from_csv_args(values: Sequence[str]) -> list[str]:
    joined = " ".join(str(value) for value in values)
    return [part.strip() for part in joined.split(",") if part.strip()]


def _entry_category_key(data: Mapping[str, Any]) -> str:
    ident = _maybe_str(data.get("packagecategoryident"))
    if ident:
        return _normalize_text(ident)
    category = _localized_tail(data.get("packagecategory"))
    if category:
        return _normalize_text(category)
    return "<none>"


def _entry_category_label(data: Mapping[str, Any]) -> str:
    category = _localized_tail(data.get("packagecategory"))
    ident = _maybe_str(data.get("packagecategoryident"))
    if category:
        return category
    if ident:
        return ident
    return "Uncategorized"


def _entry_runtime_name(kind: KIND, source_key: str, data: Mapping[str, Any]) -> str:
    for field_name in (
        kind,
        "rewards",
        "criteria_preset",
        "manufacturer",
        "internalname",
        "displayname",
        "name",
    ):
        text = _maybe_str(data.get(field_name))
        if text:
            return text
    return source_key


def _entry_display_name(kind: KIND, source_key: str, data: Mapping[str, Any]) -> str:
    if kind == "rewards":
        return (
            _localized_tail(data.get("packagename"))
            or _localized_tail(data.get("displayname"))
            or _maybe_str(data.get("rewards"))
            or source_key
        )
    if kind == "criteria_preset":
        return _maybe_str(data.get("criteria_preset")) or source_key
    if kind in {"manufacturer", "rarity", "ui_mission_reward_type", "ui_mission_reward_tier"}:
        return (
            _localized_tail(data.get("displayname"))
            or _maybe_str(data.get("internalname"))
            or _maybe_str(data.get(kind))
            or source_key
        )
    return (
        _localized_tail(data.get("displayname"))
        or _maybe_str(data.get("internalname"))
        or _maybe_str(data.get(kind))
        or _maybe_str(data.get("name"))
        or source_key
    )


def _gather_aliases(
    kind: KIND,
    source_key: str,
    data: Mapping[str, Any],
    runtime_name: str,
    display_name: str,
) -> tuple[str, ...]:
    aliases = {
        _normalize_text(source_key),
        _normalize_text(runtime_name),
        _normalize_text(display_name),
        _normalize_text(_runtime_handle(kind, source_key)),
    }
    reward = data.get("reward")
    if isinstance(reward, Mapping):
        unique_name = _maybe_str(reward.get("uniquename"))
        if unique_name:
            aliases.add(_normalize_text(unique_name))
    return tuple(sorted(alias for alias in aliases if alias))


def _extract_reward_data_items(entry: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    reward = entry.get("reward")
    if not isinstance(reward, Mapping):
        return []
    rewarddata = reward.get("rewarddata")
    if not isinstance(rewarddata, list):
        return []
    return [item for item in rewarddata if isinstance(item, Mapping)]


def _extract_reward_type_names(entry: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    for item in _extract_reward_data_items(entry):
        struct_name = _structtype_short(item.get("structtype"))
        if struct_name:
            names.append(struct_name)
    return _dedupe_preserve_order(names)


def _extract_reward_criteria_handles(entry: Mapping[str, Any]) -> list[str]:
    handles: list[str] = []
    for item in _extract_reward_data_items(entry):
        inventory = item.get("inventoryitemselectiondata")
        if not isinstance(inventory, Mapping):
            continue
        criteria = inventory.get("criteria")
        if not isinstance(criteria, Mapping):
            continue
        preset = _maybe_str(criteria.get("preset"))
        if not preset:
            continue
        normalized = _normalize_handle(preset)
        if normalized is not None:
            handles.append(normalized)
        else:
            handles.append(_runtime_handle("criteria_preset", preset))
    return _dedupe_preserve_order(handles)


def _extract_criteria_rows(entry: Mapping[str, Any]) -> list[tuple[str, list[str], str | None]]:
    criteria = entry.get("criteria")
    if not isinstance(criteria, Mapping):
        return []
    criteria_root = criteria.get("criteria")
    if not isinstance(criteria_root, Mapping):
        return []
    groups = criteria_root.get("name")
    if not isinstance(groups, list):
        return []

    out: list[tuple[str, list[str], str | None]] = []
    for group in groups:
        if not isinstance(group, Mapping) or not group:
            continue
        group_name = next(iter(group.keys()))
        group_data = group.get(group_name)
        if not isinstance(group_data, Mapping):
            continue
        tags_blob = group_data.get("tags")
        tag_values: list[str] = []
        if isinstance(tags_blob, list):
            for item in tags_blob:
                if isinstance(item, Mapping):
                    for value in item.values():
                        text = _maybe_str(value)
                        if text:
                            tag_values.append(text)
        tag_values = _dedupe_preserve_order(tag_values)

        weight_text: str | None = None
        tagweights = group_data.get("tagweights")
        if isinstance(tagweights, Mapping):
            pairs = tagweights.get("pairs")
            if isinstance(pairs, Mapping):
                formatted: list[str] = []
                for pair in pairs.values():
                    if not isinstance(pair, Mapping):
                        continue
                    key = _maybe_str(pair.get("key")) or "?"
                    formatted.append(f"{key}={_format_weight(pair.get('value'))}")
                if formatted:
                    weight_text = ", ".join(formatted)
        out.append((group_name, tag_values, weight_text))
    return out


def _extract_reward_icon_links(entry: Mapping[str, Any]) -> list[str]:
    reward = entry.get("reward")
    if not isinstance(reward, Mapping):
        return []
    customdata = reward.get("customdata")
    if not isinstance(customdata, Mapping):
        return []
    icons = customdata.get("ui_reward_icons")
    if not isinstance(icons, list):
        return []
    out: list[str] = []
    for icon in icons:
        if not isinstance(icon, Mapping):
            continue
        for field_name in ("ui_rewardtype", "ui_rewardtier", "ui_maxrewardtier"):
            normalized = _normalize_handle(_maybe_str(icon.get(field_name)) or "")
            if normalized:
                out.append(normalized)
    return _dedupe_preserve_order(out)


def _summarize_reward_value(value: Any) -> str:
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        handle = _maybe_str(value.get("handle"))
        if handle:
            return _normalize_handle(handle) or handle
        tag_name = _maybe_str(value.get("tagname"))
        if tag_name:
            return tag_name
        constant = _maybe_str(value.get("constant"))
        if constant:
            data_table = value.get("datatablevalue")
            if isinstance(data_table, Mapping):
                row = _maybe_str(data_table.get("rowname"))
                column = _maybe_str(data_table.get("columnname"))
                if row and column:
                    return f"{constant} [{row}.{column}]"
                if row:
                    return f"{constant} [{row}]"
            return constant
        if "structtype" in value and len(value) == 1:
            struct_name = _structtype_short(value.get("structtype"))
            if struct_name:
                return struct_name
        parts: list[str] = []
        for key, item in value.items():
            if key == "structtype":
                continue
            summary = _summarize_reward_value(item)
            if summary:
                parts.append(f"{key}={summary}")
            if len(parts) >= 4:
                break
        if parts:
            return ", ".join(parts)
    if isinstance(value, list):
        parts = [_summarize_reward_value(item) for item in value[:4]]
        if len(value) > 4:
            parts.append(f"... ({len(value)} total)")
        return ", ".join(part for part in parts if part)
    return _safe_json(value)


@dataclass(slots=True)
class CoreEntry:
    kind: KIND
    key: str
    source_key: str
    runtime_name: str
    display_name: str
    handle: str
    aliases: tuple[str, ...]
    data: dict[str, Any]


@dataclass(slots=True)
class ChildLink:
    kind: KIND
    handle: str
    label: str
    via: str


@dataclass(slots=True)
class AnalysisResult:
    direct_links: tuple[ChildLink, ...]
    reward_types: tuple[str, ...]
    criteria_rows: tuple[tuple[str, tuple[str, ...], str | None], ...]


@dataclass(slots=True)
class UiState:
    filter_text: str = ""
    category_filter: str = ALL_CATEGORY_KEY
    status_text: str = "Ready."
    error_text: str = ""
    selected_index: int = 0
    generate_count: int = 1
    grant_mode: GRANT_MODE = "GiveReward"
    serial_generate_count: int = 1
    serial_grant_mode: GRANT_MODE = "GiveReward"
    serial_value: str = ""
    reward_display_name: str = "Item Serial Reward"
    reward_description: str = "Generated by BL4 Reward Generator"
    reward_category: str = "TraitMissions_Weekly_Rewards"
    reward_category_ident: str = "weekly_wildcard"
    reward_unique_name: str = "RewardPackage_WeeklyTraitMission_GearReward"


class BL4RewardGeneratorController:
    def __init__(self) -> None:
        self.ui = UiState()
        self.window_owned = False
        self.data_path = DATA_PATH
        self.tables: dict[str, dict[str, Any]] = {}
        self.entries_by_kind: dict[KIND, dict[str, CoreEntry]] = {kind: {} for kind in LOOKUP_KINDS}
        self.entries_by_handle: dict[str, CoreEntry] = {}
        self.filtered_rewards: list[CoreEntry] = []
        self.analysis_cache: dict[str, AnalysisResult] = {}
        self.category_options: list[tuple[str, str, int]] = [(ALL_CATEGORY_KEY, "All Categories", 0)]

    def on_enable(self) -> None:
        self.reload_data()

    def on_disable(self) -> None:
        self.close_window()

    def ensure_data_loaded(self) -> None:
        if self.entries_by_kind["rewards"]:
            return
        self.reload_data()

    def _serial_category_entries(self) -> list[tuple[str, str, int]]:
        return [entry for entry in self.category_options if entry[0] != ALL_CATEGORY_KEY]

    def _sync_serial_category_defaults(self) -> None:
        serial_entries = self._serial_category_entries()
        if not serial_entries:
            self.ui.reward_category = ""
            self.ui.reward_category_ident = ""
            return

        valid_keys = {key for key, _label, _count in serial_entries}
        if self.ui.reward_category_ident not in valid_keys:
            if "weekly_wildcard" in valid_keys:
                preferred_key = "weekly_wildcard"
            elif self.ui.category_filter in valid_keys:
                preferred_key = self.ui.category_filter
            else:
                preferred_key = serial_entries[0][0]
            self.ui.reward_category_ident = preferred_key

        selected_entry = next(
            ((key, label, count) for key, label, count in serial_entries if key == self.ui.reward_category_ident),
            serial_entries[0],
        )
        self.ui.reward_category_ident = selected_entry[0]
        self.ui.reward_category = selected_entry[1]

    def reload_data(self) -> None:
        self.data_path = DATA_PATH
        self.tables.clear()
        self.entries_by_handle.clear()
        self.analysis_cache.clear()
        self.category_options = [(ALL_CATEGORY_KEY, "All Categories", 0)]
        for kind in self.entries_by_kind:
            self.entries_by_kind[kind].clear()
        self.filtered_rewards = []

        try:
            with open_in_mod_dir(self.data_path, binary=True) as file:
                raw_data = json.loads(file.read().decode("utf-8"))
            tables = raw_data.get("tables")
            if not isinstance(tables, Mapping):
                raise ValueError("Lookup file is missing a top-level 'tables' mapping.")

            self.tables = {
                str(name): dict(table_data)
                for name, table_data in tables.items()
                if isinstance(table_data, Mapping)
            }

            rewards_table = self.tables.get("rewards")
            rewards_entries = rewards_table.get("entries") if isinstance(rewards_table, Mapping) else None
            if not isinstance(rewards_entries, Mapping):
                raise ValueError("Lookup file is missing rewards.entries mapping.")

            category_counts: dict[str, int] = {}
            category_labels: dict[str, str] = {}

            for kind in LOOKUP_KINDS:
                table = self.tables.get(kind)
                entries = table.get("entries") if isinstance(table, Mapping) else None
                if not isinstance(entries, Mapping):
                    continue
                for source_key, raw_entry in entries.items():
                    if not isinstance(source_key, str) or not isinstance(raw_entry, Mapping):
                        continue
                    data = dict(raw_entry)
                    runtime_name = _entry_runtime_name(kind, source_key, data)
                    display_name = _entry_display_name(kind, source_key, data)
                    entry = CoreEntry(
                        kind=kind,
                        key=_normalize_text(source_key),
                        source_key=source_key,
                        runtime_name=runtime_name,
                        display_name=display_name,
                        handle=_runtime_handle(kind, source_key),
                        aliases=_gather_aliases(kind, source_key, data, runtime_name, display_name),
                        data=data,
                    )
                    self.entries_by_kind[kind][entry.key] = entry
                    self.entries_by_handle[_normalize_text(entry.handle)] = entry
                    if kind == "rewards":
                        category_key = _entry_category_key(data)
                        category_counts[category_key] = category_counts.get(category_key, 0) + 1
                        category_labels.setdefault(category_key, _entry_category_label(data))

            self.category_options = [(ALL_CATEGORY_KEY, "All Categories", sum(category_counts.values()))]
            for category_key in sorted(category_counts, key=lambda key: category_labels[key].lower()):
                self.category_options.append((category_key, category_labels[category_key], category_counts[category_key]))
            if self.ui.category_filter != ALL_CATEGORY_KEY and self.ui.category_filter not in category_counts:
                self.ui.category_filter = ALL_CATEGORY_KEY
            self._sync_serial_category_defaults()

            self.apply_filter()
            reward_count = len(self.entries_by_kind["rewards"])
            self.ui.error_text = ""
            self.ui.status_text = f"Loaded {reward_count} rewards from {self.data_path.name}."
            _log_info(self.ui.status_text)
        except Exception as exc:
            self.ui.error_text = str(exc)
            self.ui.status_text = "Failed to load data."
            _log_error(self.ui.error_text)

    def _open_window(self) -> bool:
        if _blimgui is None:
            self.ui.error_text = f"blimgui import failed: {BLIMGUI_IMPORT_ERROR}"
            _log_error(self.ui.error_text)
            return False
        try:
            if not self.window_owned or not _blimgui.is_window_open():
                self.window_owned = True
                _blimgui.set_draw_callback(self.draw_ui)
                _blimgui.create_window(WINDOW_TITLE, width=1480, height=940)
            self.ui.error_text = ""
            return True
        except Exception as exc:
            message = str(exc)
            if "already initialized" in message.lower():
                if self.window_owned and _blimgui.is_window_open():
                    self.ui.error_text = ""
                    return True
                self.window_owned = False
                self.ui.error_text = "Another BLImgui window is already active. Close it first."
                _log_warning(self.ui.error_text)
                return False
            self.window_owned = False
            self.ui.error_text = f"Failed to open reward generator window: {message}"
            _log_error(self.ui.error_text)
            return False

    def toggle_window(self) -> None:
        if self.window_owned and _blimgui is not None and _blimgui.is_window_open():
            self.close_window()
            return
        self.ensure_data_loaded()
        if self._open_window():
            if self.ui.error_text:
                return
            reward_count = len(self.entries_by_kind["rewards"])
            self.ui.status_text = f"Opened reward generator window. Loaded {reward_count} rewards."

    def close_window(self) -> None:
        if _blimgui is None:
            self.window_owned = False
            return
        try:
            if self.window_owned and _blimgui.is_window_open():
                _blimgui.close_window()
        except Exception as exc:
            _log_warning(f"Failed to close reward generator window: {exc}")
        self.window_owned = False

    def _find_rewards_def_struct(self) -> UObject | None:
        for class_name in ("ScriptStruct", "Object"):
            for object_path in REWARDS_DEF_SCRIPT_PATHS:
                try:
                    resolved = unrealsdk.find_object(class_name, object_path)
                except Exception:
                    resolved = None
                if isinstance(resolved, UObject):
                    return resolved
        try:
            for candidate in unrealsdk.find_all("ScriptStruct", False) or []:
                if getattr(candidate, "Name", None) == "GbxRewardsDef":
                    return candidate
        except Exception:
            pass
        return None

    def _build_reward_def_ptr_candidates(self, reward_name: str) -> tuple[list[tuple[str, FGbxDefPtr]], list[str]]:
        rewards_def_struct = self._find_rewards_def_struct()
        candidates: list[tuple[str, FGbxDefPtr]] = []
        errors: list[str] = []

        try:
            reward_def = FGbxDefPtr()
            reward_def._experimental_name = reward_name
            candidates.append(("name-only", reward_def))
        except Exception as exc:
            errors.append(f"FGbxDefPtr(name-only) -> {exc}")

        if rewards_def_struct is not None:
            try:
                reward_def = FGbxDefPtr()
                reward_def._experimental_name = reward_name
                reward_def._experimental_ref = rewards_def_struct
                candidates.append(("name+ref", reward_def))
            except Exception as exc:
                errors.append(f"FGbxDefPtr(name+ref) -> {exc}")
        else:
            errors.append("GbxRewardsDef struct not found")

        return candidates, errors

    def _get_runtime_pc(self) -> UObject | None:
        pc = get_pc()
        if pc is not None:
            return pc
        for class_name in ("OakPlayerController", "PlayerController"):
            try:
                objects = unrealsdk.find_all(class_name, False) or []
            except Exception:
                continue
            for obj in objects:
                if obj is not None:
                    return obj
        return None

    def _get_class_default_object(self, class_name: str) -> UObject:
        try:
            class_object = unrealsdk.find_class(class_name)
        except Exception as exc:
            raise RuntimeError(f"{class_name} class not found: {exc}") from exc
        if class_object is None:
            raise RuntimeError(f"{class_name} class not found.")
        default_object = getattr(class_object, "ClassDefaultObject", None)
        if not isinstance(default_object, UObject):
            raise RuntimeError(f"{class_name}.ClassDefaultObject is not available.")
        return default_object

    def _get_rewards_blueprint_functions(self) -> UObject:
        return self._get_class_default_object("GbxRewards_BlueprintFunctions")

    def _reward_name_candidates(self, entry: CoreEntry) -> list[str]:
        reward = entry.data.get("reward")
        unique_name = reward.get("uniquename") if isinstance(reward, Mapping) else None
        candidates = [
            _maybe_str(unique_name) or "",
            entry.runtime_name,
            entry.source_key,
        ]
        return _dedupe_preserve_order([candidate for candidate in candidates if candidate.strip()])

    def _grant_contexts(self, mode: GRANT_MODE) -> list[UObject]:
        pc = self._get_runtime_pc()
        pawn = getattr(pc, "Pawn", None) if pc is not None else None
        player_state = getattr(pc, "PlayerState", None) if pc is not None else None
        if player_state is None and pc is not None:
            player_state = getattr(pc, "MyOakPlayerState", None)
        game_instance = getattr(pc, "GameInstance", None) if pc is not None else None

        if mode == "GiveRewardAllPlayers":
            candidates = [pc, game_instance, pawn, player_state]
        else:
            candidates = [player_state, pc, pawn]

        out: list[UObject] = []
        seen_ids: set[int] = set()
        for candidate in candidates:
            if not isinstance(candidate, UObject):
                continue
            object_id = id(candidate)
            if object_id in seen_ids:
                continue
            seen_ids.add(object_id)
            out.append(candidate)
        return out

    def _get_rewards_managers(self) -> list[UObject]:
        pc = self._get_runtime_pc()
        out: list[UObject] = []
        seen_ids: set[int] = set()

        def add_manager(candidate: Any) -> None:
            if not isinstance(candidate, UObject):
                return
            if getattr(candidate.Class, "Name", None) != "GbxRewardsManager":
                return
            object_id = id(candidate)
            if object_id in seen_ids:
                return
            seen_ids.add(object_id)
            out.append(candidate)

        if pc is not None:
            for attr_name in ("RewardsManager", "GbxRewardsManager", "MyRewardsManager"):
                add_manager(getattr(pc, attr_name, None))
        try:
            managers = unrealsdk.find_all("GbxRewardsManager", False) or []
        except Exception:
            managers = []
        if pc is not None:
            for manager in managers:
                if getattr(manager, "Outer", None) is pc:
                    add_manager(manager)
        for manager in managers:
            add_manager(manager)
        return out

    def _get_rewards_manager(self) -> UObject | None:
        managers = self._get_rewards_managers()
        return managers[0] if managers else None

    def _get_reward_packages(self, manager: UObject | None = None) -> list[Any]:
        if manager is None:
            manager = self._get_rewards_manager()
        if manager is None:
            return []
        packages = getattr(manager, "packages", None)
        if packages is None:
            return []
        try:
            return list(packages)
        except Exception:
            return []

    def _get_reward_def_name(self, reward_def: Any) -> str:
        for attr_name in ("_experimental_name", "name", "Name"):
            text = _maybe_str(getattr(reward_def, attr_name, None))
            if text:
                return text
        return ""

    def _get_package_identity(self, package: Any) -> tuple[Any, ...]:
        ticks = None
        time_received = getattr(package, "TimeReceived", None)
        if time_received is not None:
            ticks = getattr(time_received, "Ticks", None)
        reward_def = getattr(package, "RewardsDef", None)
        reward_name = self._get_reward_def_name(reward_def)
        contents = getattr(package, "contents", None)
        try:
            content_count = len(contents) if contents is not None else 0
        except Exception:
            content_count = 0
        return (ticks, reward_name, content_count)

    def _get_reward_package_label(self, package: Any) -> str:
        reward_def = getattr(package, "RewardsDef", None)
        reward_name = self._get_reward_def_name(reward_def) or "<unknown>"
        time_received = getattr(package, "TimeReceived", None)
        ticks = getattr(time_received, "Ticks", None) if time_received is not None else None
        return f"{reward_name} (ticks={ticks})" if ticks is not None else reward_name

    def _find_new_reward_packages(
        self,
        manager: UObject | None,
        before_identities: set[tuple[Any, ...]],
        before_count: int,
    ) -> list[Any]:
        after_packages = self._get_reward_packages(manager)
        if len(after_packages) > before_count:
            return after_packages[before_count:]
        new_packages = [
            package
            for package in after_packages
            if self._get_package_identity(package) not in before_identities
        ]
        if new_packages:
            return new_packages
        return after_packages[-1:] if after_packages else []

    def _collect_reward_override_targets(self, package: Any) -> list[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        seen_ids: set[int] = set()

        def add_target(label: str, target: Any) -> None:
            if target is None:
                return
            target_id = id(target)
            if target_id in seen_ids:
                return
            seen_ids.add(target_id)
            out.append((label, target))

        add_target("package", package)

        reward_def = getattr(package, "RewardsDef", None)
        add_target("package.RewardsDef", reward_def)
        rewards_def_instance = _unwrap_struct_like(reward_def)
        add_target("package.RewardsDef.instance", rewards_def_instance)

        reward_set = getattr(rewards_def_instance, "reward", None) if rewards_def_instance is not None else None
        add_target("package.RewardsDef.instance.reward", reward_set)

        rewarddata = getattr(reward_set, "rewarddata", None) if reward_set is not None else None
        first_reward_data = None
        if rewarddata is not None:
            try:
                if len(rewarddata) > 0:
                    first_reward_data = rewarddata[0]
            except Exception:
                first_reward_data = None
        add_target("package.RewardsDef.instance.reward.rewarddata.0", first_reward_data)
        first_reward_data_instance = _unwrap_struct_like(first_reward_data)
        add_target("package.RewardsDef.instance.reward.rewarddata.0.instance", first_reward_data_instance)

        display_data = getattr(first_reward_data_instance, "DisplayData", None) if first_reward_data_instance is not None else None
        add_target("package.RewardsDef.instance.reward.rewarddata.0.DisplayData", display_data)
        display_data_instance = _unwrap_struct_like(display_data)
        add_target("package.RewardsDef.instance.reward.rewarddata.0.DisplayData.instance", display_data_instance)

        contents = getattr(package, "contents", None)
        first_contents = None
        if contents is not None:
            try:
                if len(contents) > 0:
                    first_contents = contents[0]
            except Exception:
                first_contents = None
        add_target("package.contents.0", first_contents)
        return out

    def _try_write_string_override(
        self,
        targets: Sequence[tuple[str, Any]],
        attr_names: Sequence[str],
        value: str,
    ) -> tuple[list[str], list[str]]:
        changed: list[str] = []
        errors: list[str] = []
        if not value:
            return changed, errors
        for label, target in targets:
            for attr_name in attr_names:
                if not hasattr(target, attr_name):
                    continue
                try:
                    setattr(target, attr_name, value)
                    changed.append(f"{label}.{attr_name}")
                except Exception as exc:
                    errors.append(f"{label}.{attr_name} -> {exc}")
        return changed, errors

    def _try_write_serial_numbers(self, package: Any, values: Sequence[str]) -> tuple[list[str], list[str]]:
        changed: list[str] = []
        errors: list[str] = []
        serial_values = [value.strip() for value in values if str(value).strip()]
        if not serial_values:
            return changed, errors
        contents = getattr(package, "contents", None)
        if contents is None:
            return changed, ["package.contents -> missing"]
        try:
            if len(contents) < 1:
                return changed, ["package.contents -> empty"]
        except Exception as exc:
            return changed, [f"package.contents -> {exc}"]
        try:
            first_contents = contents[0]
        except Exception as exc:
            return changed, [f"package.contents.0 -> {exc}"]
        serial_numbers = getattr(first_contents, "SerialNumbers", None)
        if serial_numbers is None:
            return changed, ["package.contents.0.SerialNumbers -> missing"]

        if hasattr(serial_numbers, "clear") and hasattr(serial_numbers, "append"):
            try:
                serial_numbers.clear()
                changed.append("package.contents.0.SerialNumbers.clear")
                for index, serial_value in enumerate(serial_values):
                    serial_numbers.append(serial_value)
                    changed.append(f"package.contents.0.SerialNumbers.append[{index}]")
                return changed, errors
            except Exception as exc:
                errors.append(f"package.contents.0.SerialNumbers.clear/append -> {exc}")

        try:
            existing_len = len(serial_numbers)
        except Exception as exc:
            errors.append(f"package.contents.0.SerialNumbers.len -> {exc}")
            existing_len = 0

        for index, serial_value in enumerate(serial_values):
            if index < existing_len:
                try:
                    serial_numbers[index] = serial_value
                    changed.append(f"package.contents.0.SerialNumbers[{index}]")
                    continue
                except Exception as exc:
                    errors.append(f"package.contents.0.SerialNumbers[{index}] -> {exc}")
            if hasattr(serial_numbers, "append"):
                try:
                    serial_numbers.append(serial_value)
                    changed.append(f"package.contents.0.SerialNumbers.append[{index}]")
                    continue
                except Exception as exc:
                    errors.append(f"package.contents.0.SerialNumbers.append[{index}] -> {exc}")
        return changed, errors

    def _apply_created_reward_overrides(
        self,
        package: Any,
        serial_values: Sequence[str],
        reward_display_name: str,
        reward_description: str,
        reward_category: str,
        reward_category_ident: str,
        reward_unique_name: str,
    ) -> tuple[list[str], list[str]]:
        targets = self._collect_reward_override_targets(package)
        changed: list[str] = []
        errors: list[str] = []

        serial_changed, serial_errors = self._try_write_serial_numbers(package, serial_values)
        changed.extend(serial_changed)
        errors.extend(serial_errors)

        name_changed, name_errors = self._try_write_string_override(
            targets,
            ("packagename", "DisplayName", "ItemName", "CustomName", "NameOverride", "InventoryName"),
            reward_display_name,
        )
        changed.extend(name_changed)
        errors.extend(name_errors)

        description_changed, description_errors = self._try_write_string_override(
            targets,
            ("PackageDescription", "Description", "ItemDescription", "DescriptionOverride"),
            reward_description,
        )
        changed.extend(description_changed)
        errors.extend(description_errors)

        category_changed, category_errors = self._try_write_string_override(
            targets,
            ("PackageCategory", "Category"),
            reward_category,
        )
        changed.extend(category_changed)
        errors.extend(category_errors)

        ident_changed, ident_errors = self._try_write_string_override(
            targets,
            ("PackageCategoryIdent", "CategoryIdent"),
            reward_category_ident,
        )
        changed.extend(ident_changed)
        errors.extend(ident_errors)

        unique_changed, unique_errors = self._try_write_string_override(
            targets,
            ("UniqueName",),
            reward_unique_name,
        )
        changed.extend(unique_changed)
        errors.extend(unique_errors)

        return changed, errors

    def apply_filter(self) -> None:
        query = self.ui.filter_text.strip().lower()
        self.ui.selected_index = 0
        entries = sorted(
            self.entries_by_kind["rewards"].values(),
            key=lambda entry: entry.display_name.lower(),
        )
        if self.ui.category_filter != ALL_CATEGORY_KEY:
            entries = [
                entry
                for entry in entries
                if _entry_category_key(entry.data) == self.ui.category_filter
            ]
        if query:
            entries = [entry for entry in entries if self._entry_matches(entry, query)]
        self.filtered_rewards = entries
        self.analysis_cache.clear()

    def refresh(self) -> None:
        self.apply_filter()
        self.ui.status_text = f"Refreshed reward results for '{self.ui.filter_text.strip() or '*'}'."

    def _entry_matches(self, entry: CoreEntry, query: str) -> bool:
        if query in _normalize_text(entry.display_name):
            return True
        if query in _normalize_text(entry.source_key):
            return True
        if query in _normalize_text(entry.runtime_name):
            return True
        if query in _normalize_text(entry.handle):
            return True
        return any(query in alias for alias in entry.aliases)

    def get_filtered_results(self) -> list[CoreEntry]:
        return self.filtered_rewards

    def get_selected_entry(self) -> CoreEntry | None:
        filtered = self.get_filtered_results()
        if not filtered:
            return None
        self.ui.selected_index = max(0, min(self.ui.selected_index, len(filtered) - 1))
        return filtered[self.ui.selected_index]

    def select_entry(self, index: int) -> None:
        filtered = self.get_filtered_results()
        if not filtered:
            self.ui.selected_index = 0
            return
        self.ui.selected_index = max(0, min(index, len(filtered) - 1))

    def _resolve_entry(self, handle_or_name: str) -> CoreEntry | None:
        entry = self.entries_by_handle.get(_normalize_text(handle_or_name))
        if entry is not None:
            return entry
        normalized = _normalize_handle(handle_or_name)
        if normalized is not None:
            entry = self.entries_by_handle.get(_normalize_text(normalized))
            if entry is not None:
                return entry
        needle = _normalize_text(handle_or_name)
        for candidate in self.entries_by_kind["rewards"].values():
            if needle in candidate.aliases:
                return candidate
        return None

    def _direct_links(self, entry: CoreEntry) -> tuple[ChildLink, ...]:
        raw_handles = _extract_handles(entry.data)
        raw_handles.extend(_extract_reward_criteria_handles(entry.data))
        raw_handles.extend(_extract_reward_icon_links(entry.data))
        raw_handles = _dedupe_preserve_order(raw_handles)

        out: list[ChildLink] = []
        for raw_handle in raw_handles:
            normalized = _normalize_handle(raw_handle)
            parsed = _parse_handle(raw_handle)
            if normalized is None or parsed is None:
                continue
            linked = self._resolve_entry(normalized)
            linked_label = raw_handle
            if linked is None:
                table = self.entries_by_kind.get(parsed[0])
                if table is not None:
                    linked = table.get(parsed[1])
            if linked is not None:
                linked_label = linked.display_name
            out.append(
                ChildLink(
                    kind=parsed[0],
                    handle=normalized,
                    label=linked_label,
                    via="handle-scan",
                )
            )
        return tuple(out)

    def analyze(self, entry: CoreEntry) -> AnalysisResult:
        cached = self.analysis_cache.get(entry.handle)
        if cached is not None:
            return cached
        result = AnalysisResult(
            direct_links=self._direct_links(entry),
            reward_types=tuple(_extract_reward_type_names(entry.data)),
            criteria_rows=tuple(
                (name, tuple(tags), weight_text)
                for name, tags, weight_text in _extract_criteria_rows(entry.data)
            ),
        )
        self.analysis_cache[entry.handle] = result
        return result

    def _describe_link_handle(self, handle: str) -> str:
        linked = self._resolve_entry(handle)
        if linked is None:
            parsed = _parse_handle(handle)
            if parsed is not None:
                linked = self.entries_by_kind.get(parsed[0], {}).get(parsed[1])
        if linked is None:
            return handle
        return f"{linked.display_name} [{linked.kind}]"

    def _reward_resolution_blocks(self, entry: CoreEntry) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for index, item in enumerate(_extract_reward_data_items(entry.data), start=1):
            lines: list[tuple[str, str]] = []
            inventory = item.get("inventoryitemselectiondata")
            if isinstance(inventory, Mapping):
                item_blob = inventory.get("item")
                if isinstance(item_blob, Mapping):
                    handle = _maybe_str(item_blob.get("handle"))
                    if handle:
                        normalized = _normalize_handle(handle) or handle
                        lines.append(("Item Handle", self._describe_link_handle(normalized)))
                criteria = inventory.get("criteria")
                if isinstance(criteria, Mapping):
                    preset = _maybe_str(criteria.get("preset"))
                    if preset:
                        normalized = _normalize_handle(preset) or preset
                        lines.append(("Criteria Preset", self._describe_link_handle(normalized)))

            for field_name, raw_value in item.items():
                if field_name in {"inventoryitemselectiondata", "structtype"}:
                    continue
                lines.append((_field_label(field_name), _summarize_reward_value(raw_value)))

            blocks.append(
                {
                    "index": index,
                    "type_name": _structtype_short(item.get("structtype")) or "<unknown>",
                    "lines": lines,
                }
            )
        return blocks

    def _reward_icon_rows(self, entry: CoreEntry) -> list[dict[str, str]]:
        reward = entry.data.get("reward")
        if not isinstance(reward, Mapping):
            return []
        customdata = reward.get("customdata")
        if not isinstance(customdata, Mapping):
            return []
        icons = customdata.get("ui_reward_icons")
        if not isinstance(icons, list):
            return []

        out: list[dict[str, str]] = []
        for icon in icons:
            if not isinstance(icon, Mapping):
                continue
            type_text = _summarize_reward_value(icon.get("ui_rewardtype"))
            tier_text = _summarize_reward_value(icon.get("ui_rewardtier"))
            max_tier_text = _summarize_reward_value(icon.get("ui_maxrewardtier"))
            tooltip = _summarize_reward_value(icon.get("itemcarddata"))
            cell = _summarize_reward_value(icon.get("itemcelldata"))
            out.append(
                {
                    "type": type_text,
                    "tier": tier_text,
                    "max_tier": max_tier_text,
                    "tooltip": tooltip,
                    "cell": cell,
                }
            )
        return out

    def _grant_reward(self, entry: CoreEntry, amount: int, mode: GRANT_MODE) -> str:
        helper = self._get_rewards_blueprint_functions()
        function = getattr(helper, mode, None)
        if not callable(function):
            raise RuntimeError(f"{mode} is not callable on GbxRewards_BlueprintFunctions.")

        contexts = self._grant_contexts(mode)
        if not contexts:
            raise RuntimeError("No valid runtime context was found.")

        reward_names = self._reward_name_candidates(entry)
        if not reward_names:
            raise RuntimeError("No reward name candidates were found.")

        attempt_errors: list[str] = []
        chosen_reward_name: str | None = None
        chosen_context: UObject | None = None
        chosen_variant: str | None = None

        for reward_name in reward_names:
            def_ptr_candidates, def_ptr_errors = self._build_reward_def_ptr_candidates(reward_name)
            attempt_errors.extend(def_ptr_errors[: max(0, 8 - len(attempt_errors))])
            if not def_ptr_candidates:
                continue
            for context in contexts:
                for variant_name, reward_def_ptr in def_ptr_candidates:
                    try:
                        function(reward_def_ptr, context)
                        chosen_reward_name = reward_name
                        chosen_context = context
                        chosen_variant = variant_name
                        break
                    except Exception as exc:
                        if len(attempt_errors) < 8:
                            attempt_errors.append(
                                f"{mode}[{variant_name}]({reward_name}, {type(context).__name__}) -> {exc}"
                            )
                    try:
                        function(context, reward_def_ptr)
                        chosen_reward_name = reward_name
                        chosen_context = context
                        chosen_variant = f"{variant_name}:swapped"
                        break
                    except Exception as exc:
                        if len(attempt_errors) < 8:
                            attempt_errors.append(
                                f"{mode}[{variant_name}:swapped]({type(context).__name__}, {reward_name}) -> {exc}"
                            )
                if chosen_context is not None:
                    break
            if chosen_context is not None:
                break

        if chosen_reward_name is None or chosen_context is None:
            if not attempt_errors:
                raise RuntimeError("No valid reward invocation was found.")
            raise RuntimeError("; ".join(attempt_errors))

        for _ in range(amount - 1):
            rebuilt_candidates, rebuilt_errors = self._build_reward_def_ptr_candidates(chosen_reward_name)
            if not rebuilt_candidates:
                raise RuntimeError(
                    f"Failed to rebuild FGbxDefPtr for {chosen_reward_name}: "
                    + ("; ".join(rebuilt_errors) if rebuilt_errors else "unknown error")
                )
            rebuilt_ptr = next((ptr for variant, ptr in rebuilt_candidates if variant == chosen_variant), None)
            if rebuilt_ptr is None:
                rebuilt_ptr = rebuilt_candidates[0][1]
            if chosen_variant is not None and chosen_variant.endswith(":swapped"):
                function(chosen_context, rebuilt_ptr)
            else:
                function(rebuilt_ptr, chosen_context)

        variant_note = f", variant={chosen_variant}" if chosen_variant else ""
        return f"{mode}(FGbxDefPtr<GbxRewardsDef>, {type(chosen_context).__name__}{variant_note})"

    def generate_entry(self, entry: CoreEntry, count: int, mode: GRANT_MODE | None = None) -> None:
        if entry.kind != "rewards":
            self.ui.error_text = "Only rewards entries can be generated."
            return
        try:
            count = max(1, min(MAX_GENERATE_COUNT, count))
            grant_mode = mode or self.ui.grant_mode
            note = self._grant_reward(entry, count, grant_mode)
            self.ui.error_text = ""
            self.ui.status_text = f"Generated x{count} {entry.display_name}"
        except Exception as exc:
            self.ui.error_text = f"Generate failed: {exc}"
            _log_error(self.ui.error_text)

    def generate_by_name(self, reward_name: str, count: int, mode: GRANT_MODE = "GiveReward") -> None:
        self.ensure_data_loaded()
        entry = self._resolve_entry(reward_name)
        if entry is None:
            raise RuntimeError(f"Reward not found: {reward_name}")
        self.generate_entry(entry, count, mode)
        if self.ui.error_text:
            raise RuntimeError(self.ui.error_text)

    def create_and_override_reward(
        self,
        reward_name: str,
        count: int,
        mode: GRANT_MODE,
        serial_values: Sequence[str],
        reward_display_name: str,
        reward_description: str,
        reward_category: str,
        reward_category_ident: str,
        reward_unique_name: str,
    ) -> None:
        self.ensure_data_loaded()
        entry = self._resolve_entry(reward_name)
        if entry is None or entry.kind != "rewards":
            raise RuntimeError(f"Reward not found: {reward_name}")

        managers = self._get_rewards_managers() if mode == "GiveRewardAllPlayers" else []
        if not managers:
            local_manager = self._get_rewards_manager()
            if local_manager is not None:
                managers = [local_manager]

        manager_snapshots: list[tuple[UObject | None, set[tuple[Any, ...]], int]] = []
        if managers:
            for manager in managers:
                before_packages = self._get_reward_packages(manager)
                manager_snapshots.append(
                    (
                        manager,
                        {self._get_package_identity(package) for package in before_packages},
                        len(before_packages),
                    )
                )
        else:
            manager_snapshots.append((None, set(), 0))

        count = max(1, min(MAX_GENERATE_COUNT, count))
        self._grant_reward(entry, count, mode)
        targeted_packages: list[tuple[UObject | None, list[Any]]] = []
        for manager, before_identities, before_count in manager_snapshots:
            new_packages = self._find_new_reward_packages(manager, before_identities, before_count)
            if new_packages:
                targeted_packages.append((manager, new_packages))

        if not targeted_packages:
            raise RuntimeError(
                "Reward was granted, but no reward package was discovered for post-editing."
            )

        all_changed: list[str] = []
        all_errors: list[str] = []
        all_packages: list[Any] = []
        for _manager, new_packages in targeted_packages:
            all_packages.extend(new_packages)
            for package in new_packages:
                changed, errors = self._apply_created_reward_overrides(
                    package,
                    serial_values,
                    reward_display_name.strip(),
                    reward_description.strip(),
                    reward_category.strip(),
                    reward_category_ident.strip(),
                    reward_unique_name.strip(),
                )
                all_changed.extend(changed)
                all_errors.extend(errors)

        status = (
            f"Generated x{count} {entry.display_name}"
        )
        if all_changed:
            status = f"{status} and applied overrides."
        else:
            status = f"{status} and no writable serial/reward string fields were found on the created package."
        if all_errors:
            preview = "; ".join(all_errors[:4])
            if len(all_errors) > 4:
                preview = f"{preview}; ..."
            status = f"{status} Errors: {preview}"

        self.ui.error_text = ""
        self.ui.status_text = status

    def draw_ui(self) -> None:
        if _blimgui is None:
            return
        imgui = _blimgui.imgui
        visible, open_state = imgui.begin(WINDOW_TITLE, True)
        if open_state is False:
            self.close_window()
            imgui.end()
            return
        if not visible:
            imgui.end()
            return

        if self.ui.error_text:
            imgui.text_colored((1.0, 0.45, 0.45, 1.0), self.ui.error_text)
        else:
            imgui.text_colored((0.45, 0.95, 0.55, 1.0), self.ui.status_text)

        if hasattr(imgui, "begin_tab_bar") and hasattr(imgui, "end_tab_bar"):
            if imgui.begin_tab_bar("##bl4_reward_generator_tabs"):
                if imgui.begin_tab_item("Rewards")[0]:
                    self._draw_rewards_tab(imgui)
                    imgui.end_tab_item()
                if imgui.begin_tab_item("Item Serial")[0]:
                    self._draw_item_serial_tab(imgui)
                    imgui.end_tab_item()
                imgui.end_tab_bar()
        else:
            self._draw_rewards_tab(imgui)
        imgui.end()

    def _draw_rewards_tab(self, imgui: Any) -> None:
        imgui.separator_text("Rewards")
        imgui.text("Category")
        imgui.same_line()
        selected_category_label = next(
            (f"{label} ({count})" for key, label, count in self.category_options if key == self.ui.category_filter),
            "All Categories",
        )
        if hasattr(imgui, "begin_combo") and hasattr(imgui, "end_combo"):
            if imgui.begin_combo("##bl4_reward_generator_category", selected_category_label):
                for key, label, count in self.category_options:
                    entry_label = f"{label} ({count})##bl4_reward_generator_category_{key}"
                    clicked, _selected = imgui.selectable(entry_label, key == self.ui.category_filter)
                    if clicked and key != self.ui.category_filter:
                        self.ui.category_filter = key
                        self.refresh()
                imgui.end_combo()
        else:
            imgui.text_wrapped(selected_category_label)

        imgui.text("Filter Rewards")
        imgui.same_line()
        imgui.set_next_item_width(-1)
        changed, self.ui.filter_text = imgui.input_text("##bl4_reward_generator_filter", self.ui.filter_text)
        if changed:
            self.refresh()
        imgui.same_line()
        if imgui.button("Clear##bl4_reward_generator_filter_clear"):
            self.ui.filter_text = ""
            self.refresh()
        imgui.same_line()
        if imgui.button("Reload Data##bl4_reward_generator_reload"):
            self.reload_data()

        avail = imgui.get_content_region_avail()
        filtered = self.get_filtered_results()
        selected = self.get_selected_entry()
        top_flags = (
            getattr(imgui, "TableFlags_Resizable", 0)
            | getattr(imgui, "TableFlags_BordersInnerV", 0)
            | getattr(imgui, "TableFlags_SizingStretchProp", 0)
        )
        if imgui.begin_table("##bl4_reward_generator_main", 2, top_flags, (0, avail.y)):
            width_stretch = getattr(imgui, "TableColumnFlags_WidthStretch", 0)
            if width_stretch:
                imgui.table_setup_column("Reward List", width_stretch, 0.42)
                imgui.table_setup_column("Reward Details", width_stretch, 0.58)
            else:
                imgui.table_setup_column("Reward List")
                imgui.table_setup_column("Reward Details")
            imgui.table_next_row()
            imgui.table_set_column_index(0)
            imgui.begin_child("bl4_reward_generator_results", (0, 0))
            self._draw_results_panel(imgui, filtered)
            imgui.end_child()
            imgui.table_set_column_index(1)
            imgui.begin_child("bl4_reward_generator_details", (0, 0))
            self._draw_details_panel(imgui, selected)
            imgui.end_child()
            imgui.end_table()

    def _draw_item_serial_tab(self, imgui: Any) -> None:
        self._sync_serial_category_defaults()
        imgui.text("Reward Name")
        imgui.same_line()
        imgui.set_next_item_width(-1)
        _changed, self.ui.reward_unique_name = imgui.input_text(
            "##bl4_reward_generator_reward_unique_name",
            self.ui.reward_unique_name,
        )

        if imgui.button(
            "GiveReward##bl4_reward_generator_serial_mode_give"
            if self.ui.serial_grant_mode != "GiveReward"
            else "[GiveReward]##bl4_reward_generator_serial_mode_give"
        ):
            self.ui.serial_grant_mode = "GiveReward"
        imgui.same_line()
        if imgui.button(
            "GiveRewardAllPlayers##bl4_reward_generator_serial_mode_give_all"
            if self.ui.serial_grant_mode != "GiveRewardAllPlayers"
            else "[GiveRewardAllPlayers]##bl4_reward_generator_serial_mode_give_all"
        ):
            self.ui.serial_grant_mode = "GiveRewardAllPlayers"

        _changed, self.ui.serial_generate_count = imgui.input_int(
            "Amount##bl4_reward_generator_serial_amount",
            self.ui.serial_generate_count,
        )
        self.ui.serial_generate_count = max(1, min(MAX_GENERATE_COUNT, self.ui.serial_generate_count))

        imgui.separator_text("Custom Values")
        imgui.text("Item Serial (1 per line)")
        _changed, self.ui.serial_value = imgui.input_text_multiline(
            "##bl4_reward_generator_serial_value",
            self.ui.serial_value,
            size=(0, 72),
        )
        imgui.text("Display / Package Name")
        imgui.same_line()
        imgui.set_next_item_width(-1)
        _changed, self.ui.reward_display_name = imgui.input_text(
            "##bl4_reward_generator_reward_display_name",
            self.ui.reward_display_name,
        )
        imgui.text("Package Description")
        imgui.same_line()
        imgui.set_next_item_width(-1)
        _changed, self.ui.reward_description = imgui.input_text(
            "##bl4_reward_generator_reward_description",
            self.ui.reward_description,
        )
        imgui.text("Package Category")
        imgui.same_line()
        selected_category_label = self.ui.reward_category or "No Categories Loaded"
        if hasattr(imgui, "begin_combo") and hasattr(imgui, "end_combo"):
            if imgui.begin_combo("##bl4_reward_generator_reward_category", selected_category_label):
                for key, label, _count in self._serial_category_entries():
                    clicked, _selected = imgui.selectable(
                        f"{label}##bl4_reward_generator_serial_category_{key}",
                        key == self.ui.reward_category_ident,
                    )
                    if clicked and key != self.ui.reward_category_ident:
                        self.ui.reward_category_ident = key
                        self._sync_serial_category_defaults()
                imgui.end_combo()
        else:
            imgui.text_wrapped(selected_category_label)
        imgui.text("Category Ident")
        imgui.same_line()
        imgui.set_next_item_width(-1)
        imgui.text_wrapped(self.ui.reward_category_ident or "-")

        if imgui.button("Generate##bl4_reward_generator_create_serialized"):
            try:
                self.create_and_override_reward(
                    self.ui.reward_unique_name.strip() or "RewardPackage_WeeklyTraitMission_GearReward",
                    self.ui.serial_generate_count,
                    self.ui.serial_grant_mode,
                    _parse_serials_from_multiline(self.ui.serial_value),
                    self.ui.reward_display_name,
                    self.ui.reward_description,
                    self.ui.reward_category,
                    self.ui.reward_category_ident,
                    self.ui.reward_unique_name,
                )
            except Exception as exc:
                self.ui.error_text = f"Generation failed: {exc}"
                _log_error(self.ui.error_text)

    def _draw_results_panel(self, imgui: Any, filtered: Sequence[CoreEntry]) -> None:
        imgui.separator_text(f"Reward List ({len(filtered)})")
        flags = (
            getattr(imgui, "TableFlags_Borders", 0)
            | getattr(imgui, "TableFlags_RowBg", 0)
            | getattr(imgui, "TableFlags_ScrollY", 0)
            | getattr(imgui, "TableFlags_Resizable", 0)
            | getattr(imgui, "TableFlags_SizingStretchProp", 0)
        )
        if imgui.begin_table("##bl4_reward_generator_table", 1, flags, (0, 0)):
            imgui.table_setup_column("Name")
            imgui.table_headers_row()
            for index, entry in enumerate(filtered):
                imgui.table_next_row()
                imgui.table_set_column_index(0)
                clicked, _ = imgui.selectable(
                    f"{entry.display_name}##bl4_reward_generator_result_{index}_{entry.kind}_{entry.source_key}",
                    index == self.ui.selected_index,
                )
                if clicked:
                    self.select_entry(index)
            imgui.end_table()

    def _draw_details_panel(self, imgui: Any, selected: CoreEntry | None) -> None:
        if selected is None:
            imgui.text_disabled("No entry selected.")
            return

        analysis = self.analyze(selected)
        resolution_blocks = self._reward_resolution_blocks(selected)
        icon_rows = self._reward_icon_rows(selected)

        imgui.separator_text("Generate")
        if imgui.button(
            "GiveReward##bl4_reward_generator_mode_give"
            if self.ui.grant_mode != "GiveReward"
            else "[GiveReward]##bl4_reward_generator_mode_give"
        ):
            self.ui.grant_mode = "GiveReward"
        imgui.same_line()
        if imgui.button(
            "GiveRewardAllPlayers##bl4_reward_generator_mode_give_all"
            if self.ui.grant_mode != "GiveRewardAllPlayers"
            else "[GiveRewardAllPlayers]##bl4_reward_generator_mode_give_all"
        ):
            self.ui.grant_mode = "GiveRewardAllPlayers"
        _changed, self.ui.generate_count = imgui.input_int("Amount", self.ui.generate_count)
        self.ui.generate_count = max(1, min(MAX_GENERATE_COUNT, self.ui.generate_count))
        if imgui.button("Generate##bl4_reward_generator_generate"):
            self.generate_entry(selected, self.ui.generate_count, self.ui.grant_mode)

        imgui.separator_text("Info")
        imgui.text_wrapped(f"Name: {selected.display_name}")
        imgui.text_wrapped(f"Runtime: {selected.runtime_name}")
        imgui.text_wrapped(f"Handle: {selected.handle}")
        reward = selected.data.get("reward")
        unique_name = _maybe_str(reward.get("uniquename")) if isinstance(reward, Mapping) else None
        package_name = _localized_tail(selected.data.get("packagename"))
        package_category = _localized_tail(selected.data.get("packagecategory"))
        package_description = _localized_tail(selected.data.get("packagedescription"))
        package_ident = _maybe_str(selected.data.get("packagecategoryident"))
        always_as_package = _safe_bool(selected.data.get("balwaysgiveaspackage"))
        if unique_name:
            imgui.text_wrapped(f"Unique Name: {unique_name}")
        if package_name:
            imgui.text_wrapped(f"Package: {package_name}")
        if package_category:
            imgui.text_wrapped(f"Category: {package_category}")
        if package_description:
            imgui.text_wrapped(f"Description: {package_description}")
        if package_ident:
            imgui.text_wrapped(f"Category Ident: {package_ident}")
        if always_as_package:
            imgui.text_wrapped("Always Give As Package: true")
        if analysis.reward_types:
            imgui.text_wrapped(f"Reward Types: {', '.join(analysis.reward_types)}")

        imgui.separator_text("Reward Data")
        if not resolution_blocks:
            imgui.text_disabled("No rewarddata entries found.")
        for block in resolution_blocks:
            imgui.text_colored((0.8, 0.85, 1.0, 1.0), f"Entry {block['index']}: {block['type_name']}")
            if not block["lines"]:
                imgui.text_disabled("No additional inputs.")
            for label, value in block["lines"]:
                imgui.text_wrapped(f"{label}: {value}")

        if analysis.criteria_rows:
            imgui.separator_text("Criteria")
            for group_name, tags, weight_text in analysis.criteria_rows:
                line = ", ".join(tags) if tags else "-"
                if weight_text:
                    line = f"{line} [{weight_text}]"
                imgui.text_wrapped(f"{group_name}: {line}")

        if icon_rows:
            imgui.separator_text("Reward Icons")
            for index, icon in enumerate(icon_rows, start=1):
                imgui.text_colored((0.8, 0.85, 1.0, 1.0), f"Icon {index}")
                if icon["type"] and icon["type"] != "None":
                    imgui.text_wrapped(f"Type: {icon['type']}")
                if icon["tier"] and icon["tier"] != "None":
                    imgui.text_wrapped(f"Tier: {icon['tier']}")
                if icon["max_tier"] and icon["max_tier"] != "None":
                    imgui.text_wrapped(f"Max Tier: {icon['max_tier']}")
                if icon["tooltip"] and icon["tooltip"] != "None":
                    imgui.text_wrapped(f"Tooltip: {icon['tooltip']}")
                if icon["cell"] and icon["cell"] != "None":
                    imgui.text_wrapped(f"Cell: {icon['cell']}")

        imgui.separator_text("Links")
        if not analysis.direct_links:
            imgui.text_disabled("No direct linked handles resolved.")
            return

        flags = (
            getattr(imgui, "TableFlags_Borders", 0)
            | getattr(imgui, "TableFlags_RowBg", 0)
            | getattr(imgui, "TableFlags_Resizable", 0)
            | getattr(imgui, "TableFlags_SizingStretchProp", 0)
            | getattr(imgui, "TableFlags_ScrollX", 0)
        )
        if imgui.begin_table("##bl4_reward_generator_links", 3, flags):
            width_stretch = getattr(imgui, "TableColumnFlags_WidthStretch", 0)
            if width_stretch:
                imgui.table_setup_column("Type", width_stretch, 0.20)
                imgui.table_setup_column("Name", width_stretch, 0.45)
                imgui.table_setup_column("Handle", width_stretch, 0.35)
            else:
                imgui.table_setup_column("Type")
                imgui.table_setup_column("Name")
                imgui.table_setup_column("Handle")
            imgui.table_headers_row()
            for link in analysis.direct_links:
                imgui.table_next_row()
                imgui.table_set_column_index(0)
                imgui.text(link.kind)
                imgui.table_set_column_index(1)
                imgui.text_wrapped(link.label)
                imgui.table_set_column_index(2)
                imgui.text_wrapped(link.handle)
            imgui.end_table()


CONTROLLER = BL4RewardGeneratorController()


def _toggle_reward_generator() -> None:
    CONTROLLER.toggle_window()


REWARD_GENERATOR_KEY = keybind(
    "Toggle BL4 Reward Generator",
    "F7",
    callback=_toggle_reward_generator,
    display_name="Toggle BL4 Reward Generator",
    description="Opens the BL4 Reeward generator window.",
)


@command("bl4_reward_generator_gui", description="Open or close the BL4 reward generator window.")
def bl4_reward_generator_gui(_args: argparse.Namespace) -> None:
    CONTROLLER.toggle_window()


@command("bl4_reward_generator_reload", description="Reload the bundled BL4 reward lookup data.")
def bl4_reward_generator_reload(_args: argparse.Namespace) -> None:
    CONTROLLER.reload_data()


@command(
    "bl4_reward_generator_create",
    description="Create a reward by name: bl4_reward_generator_create <reward_name> <amount> [--all-players].",
)
def bl4_reward_generator_create(args: argparse.Namespace) -> None:
    try:
        count = max(1, min(MAX_GENERATE_COUNT, int(args.amount)))
        mode: GRANT_MODE = "GiveRewardAllPlayers" if bool(getattr(args, "all_players", False)) else "GiveReward"
        CONTROLLER.generate_by_name(args.reward_name, count, mode)
        _log_info(CONTROLLER.ui.status_text)
    except Exception as exc:
        _log_error(f"Create failed: {exc}")


bl4_reward_generator_create.add_argument("reward_name", help="Reward name, source key, runtime name, unique name, or rewards'...'.")
bl4_reward_generator_create.add_argument(
    "amount",
    nargs="?",
    default=1,
    type=int,
    help=f"Amount to create. Default: 1, max: {MAX_GENERATE_COUNT}.",
)
bl4_reward_generator_create.add_argument(
    "--all-players",
    action="store_true",
    help="Use GiveRewardAllPlayers instead of GiveReward.",
)


@command(
    "bl4_reward_generator_serial",
    description="Create a custom reward with specified ItemSerial/s.",
)
def bl4_reward_generator_serial(args: argparse.Namespace) -> None:
    try:
        serials = _parse_serials_from_csv_args(args.serials)
        if not serials:
            raise RuntimeError("At least one serial is required.")
        count = max(1, min(MAX_GENERATE_COUNT, int(getattr(args, "amount", 1))))
        mode: GRANT_MODE = "GiveRewardAllPlayers" if bool(getattr(args, "all_players", False)) else "GiveReward"
        CONTROLLER.ensure_data_loaded()
        CONTROLLER._sync_serial_category_defaults()
        reward_name = CONTROLLER.ui.reward_unique_name.strip() or "RewardPackage_WeeklyTraitMission_GearReward"
        CONTROLLER.create_and_override_reward(
            reward_name,
            count,
            mode,
            serials,
            CONTROLLER.ui.reward_display_name,
            CONTROLLER.ui.reward_description,
            CONTROLLER.ui.reward_category,
            CONTROLLER.ui.reward_category_ident,
            reward_name,
        )
        _log_info(CONTROLLER.ui.status_text)
    except Exception as exc:
        _log_error(f"Serial create failed: {exc}")


bl4_reward_generator_serial.add_argument(
    "serials",
    nargs="+",
    help="Comma-separated serial list. Example: bl4_reward_generator_serial serial_a, serial_b",
)
bl4_reward_generator_serial.add_argument(
    "--amount",
    default=1,
    type=int,
    help=f"Amount to create. Default: 1, max: {MAX_GENERATE_COUNT}.",
)
bl4_reward_generator_serial.add_argument(
    "--all-players",
    action="store_true",
    help="Use GiveRewardAllPlayers instead of GiveReward.",
)


build_mod(
    name="BL4 Reward Generator",
    author="Cr4nkSt4r",
    description="BL4 reward browser and generator.",
    supported_games=Game.BL4,
    coop_support=CoopSupport.ClientSide,
    keybinds=[REWARD_GENERATOR_KEY],
    commands=[
        bl4_reward_generator_gui,
        bl4_reward_generator_reload,
        bl4_reward_generator_create,
        bl4_reward_generator_serial,
    ],
    on_enable=CONTROLLER.on_enable,
    on_disable=CONTROLLER.on_disable,
)
