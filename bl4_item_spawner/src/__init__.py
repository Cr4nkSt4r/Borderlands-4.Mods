from __future__ import annotations

import argparse
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import unrealsdk
from mods_base import CoopSupport, ENGINE, Game, build_mod, command, get_pc, keybind, open_in_mod_dir
from unrealsdk import logging
from unrealsdk.unreal import IGNORE_STRUCT, UObject

try:
    import blimgui as _blimgui
except Exception as exc:
    _blimgui = None
    BLIMGUI_IMPORT_ERROR = str(exc)
else:
    BLIMGUI_IMPORT_ERROR = None

CORE_KIND = Literal["itempool", "itempoollist"]
VISIBLE_KIND = Literal["itempool", "itempoollist"]
PATTERN_MODE = Literal["circle", "custom"]

WINDOW_TITLE = "BL4 Item Spawner"
MOD_DIR = Path(__file__).resolve().parent
DATA_PATH = MOD_DIR / "data" / "merge.json"
HANDLE_RE = re.compile(r"(?i)\b(itempool|itempoollist)'([^']+)'")
MAX_ITEM_LEVEL = 999999
MAX_SPAWN_COUNT = 9999


def _log_info(message: str) -> None:
    logging.info(f"[BL4ItemSpawner] {message}")


def _log_warning(message: str) -> None:
    logging.warning(f"[BL4ItemSpawner] {message}")


def _log_error(message: str) -> None:
    logging.error(f"[BL4ItemSpawner] {message}")


def _normalize_text(value: Any) -> str:
    return str(value).strip().lower()


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return repr(value)


def _make_vector(x: float, y: float, z: float) -> Any:
    return unrealsdk.make_struct("Vector", X=x, Y=y, Z=z)


def _make_rotator(pitch: float, yaw: float, roll: float = 0.0) -> Any:
    return unrealsdk.make_struct("Rotator", Pitch=pitch, Yaw=yaw, Roll=roll)


ZERO_VECTOR = _make_vector(0.0, 0.0, 0.0)
DOWN_TO_GROUND = _make_vector(0.0, 0.0, -1600.0)


def _parse_handle(value: str) -> tuple[CORE_KIND, str] | None:
    match = HANDLE_RE.search(value.strip())
    if match is None:
        return None
    return cast(CORE_KIND, match.group(1).lower()), _normalize_text(match.group(2))


def _canonical_handle(kind: CORE_KIND, key: str) -> str:
    return f"{kind}'{_normalize_text(key)}'"


def _normalize_handle(value: str) -> str | None:
    parsed = _parse_handle(value)
    if parsed is None:
        return None
    kind, key = parsed
    return _canonical_handle(kind, key)


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


def _maybe_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _entry_display_name(kind: CORE_KIND, source_key: str, data: Mapping[str, Any]) -> str:
    for candidate in (
        data.get("displayname"),
        data.get("display_name"),
        data.get("name"),
        data.get("title"),
        data.get(kind),
        source_key,
    ):
        text = _maybe_str(candidate)
        if text:
            return text
    return source_key


def _gather_aliases(kind: CORE_KIND, source_key: str, data: Mapping[str, Any]) -> tuple[str, ...]:
    aliases = {
        _normalize_text(source_key),
        _canonical_handle(kind, source_key),
        _normalize_text(_entry_display_name(kind, source_key, data)),
    }
    return tuple(sorted(alias for alias in aliases if alias))


@dataclass(slots=True)
class CoreEntry:
    kind: CORE_KIND
    key: str
    source_key: str
    display_name: str
    handle: str
    aliases: tuple[str, ...]
    data: dict[str, Any]


@dataclass(slots=True)
class ChildLink:
    kind: CORE_KIND
    handle: str
    raw_handle: str
    label: str
    via: str


@dataclass(slots=True)
class AnalysisResult:
    direct_links: tuple[ChildLink, ...]
    issues: tuple[str, ...]


@dataclass(slots=True)
class UiState:
    filter_text: str = ""
    applied_filter_text: str = ""
    active_view: VISIBLE_KIND = "itempool"
    live_refresh: bool = True
    status_text: str = "Ready."
    error_text: str = ""
    selected_index: int = 0
    spawn_count: int = 1
    spawn_level: int = 60
    manual_itempool: str = ""
    show_custom_layout: bool = False
    pattern_mode: PATTERN_MODE = "custom"
    circle_base_radius: int = 200
    circle_ring_spacing: int = 150
    circle_max_per_ring: int = 35
    circle_height_offset: int = -40
    custom_base_forward: int = 50
    custom_base_right: int = 0
    custom_base_up: int = 50
    custom_step_forward: int = 0
    custom_step_right: int = 0
    custom_step_up: int = 0


class BL4ItemSpawnerController:
    def __init__(self) -> None:
        self.ui = UiState()
        self.window_owned = False
        self.data_path = DATA_PATH
        self.tables: dict[str, dict[str, Any]] = {}
        self.entries_by_kind: dict[CORE_KIND, dict[str, CoreEntry]] = {
            "itempool": {},
            "itempoollist": {},
        }
        self.entries_by_handle: dict[str, CoreEntry] = {}
        self.filtered_records: dict[VISIBLE_KIND, list[CoreEntry]] = {
            "itempool": [],
            "itempoollist": [],
        }
        self.analysis_cache: dict[str, AnalysisResult] = {}

    def _register_entry_handle(self, entry: CoreEntry) -> None:
        self.entries_by_handle[entry.handle] = entry
        normalized = _normalize_handle(entry.handle)
        if normalized is not None and normalized != entry.handle:
            self.entries_by_handle[normalized] = entry

    def on_enable(self) -> None:
        self.reload_data()

    def on_disable(self) -> None:
        self.close_window()

    def reload_data(self) -> None:
        self.tables.clear()
        self.entries_by_handle.clear()
        self.analysis_cache.clear()
        for kind in self.entries_by_kind:
            self.entries_by_kind[kind].clear()

        try:
            with open_in_mod_dir(self.data_path, binary=True) as file:
                raw_data = json.loads(file.read().decode("utf-8"))
            tables = raw_data.get("tables")
            if not isinstance(tables, Mapping):
                raise ValueError("Merged lookup is missing a top-level 'tables' mapping.")
            self.tables = {
                str(name): dict(table_data)
                for name, table_data in tables.items()
                if isinstance(table_data, Mapping)
            }
            for kind in ("itempool", "itempoollist"):
                table = self.tables.get(kind)
                entries = table.get("entries") if isinstance(table, Mapping) else None
                if not isinstance(entries, Mapping):
                    raise ValueError(f"Missing {kind}.entries in merged lookup.")
                for source_key, raw_entry in entries.items():
                    if not isinstance(source_key, str) or not isinstance(raw_entry, Mapping):
                        continue
                    data = dict(raw_entry)
                    entry = CoreEntry(
                        kind=cast(CORE_KIND, kind),
                        key=_normalize_text(source_key),
                        source_key=source_key,
                        display_name=_entry_display_name(cast(CORE_KIND, kind), source_key, data),
                        handle=_canonical_handle(cast(CORE_KIND, kind), source_key),
                        aliases=_gather_aliases(cast(CORE_KIND, kind), source_key, data),
                        data=data,
                    )
                    self.entries_by_kind[cast(CORE_KIND, kind)][entry.key] = entry
                    self._register_entry_handle(entry)

            self.ui.spawn_level = 60
            self.apply_filter(force=True)
            self.ui.error_text = ""
            self.ui.status_text = (
                f"Loaded {len(self.entries_by_kind['itempool'])} itempools and "
                f"{len(self.entries_by_kind['itempoollist'])} itempoollists."
            )
            _log_info(self.ui.status_text)
        except Exception as exc:
            self.ui.error_text = str(exc)
            self.ui.status_text = "Failed to load BL4 item spawner data."
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
            self.ui.error_text = f"Failed to open item spawner window: {message}"
            _log_error(self.ui.error_text)
            return False

    def toggle_window(self) -> None:
        if self.window_owned and _blimgui is not None and _blimgui.is_window_open():
            self.close_window()
            return
        if self._open_window():
            self.ui.status_text = "Opened item spawner window."

    def close_window(self) -> None:
        if _blimgui is None:
            self.window_owned = False
            return
        try:
            if self.window_owned and _blimgui.is_window_open():
                _blimgui.close_window()
        except Exception as exc:
            _log_warning(f"Failed to close item spawner window: {exc}")
        self.window_owned = False

    def apply_filter(self, *, force: bool = False) -> None:
        query = self.ui.filter_text.strip().lower()
        if not force and query == self.ui.applied_filter_text:
            return
        self.ui.applied_filter_text = query
        self.ui.selected_index = 0
        for kind in ("itempool", "itempoollist"):
            entries = sorted(
                self.entries_by_kind[cast(CORE_KIND, kind)].values(),
                key=lambda entry: entry.display_name.lower(),
            )
            if query:
                entries = [entry for entry in entries if self._entry_matches(entry, query)]
            self.filtered_records[cast(VISIBLE_KIND, kind)] = entries
        self.analysis_cache.clear()

    def refresh(self) -> None:
        self.apply_filter(force=True)
        self.ui.status_text = f"Refreshed cached results for '{self.ui.filter_text.strip() or '*'}'."

    def _entry_matches(self, entry: CoreEntry, query: str) -> bool:
        if query in _normalize_text(entry.display_name):
            return True
        if query in _normalize_text(entry.source_key):
            return True
        if query in _normalize_text(entry.handle):
            return True
        return any(query in alias for alias in entry.aliases)

    def get_filtered_results(self) -> list[CoreEntry]:
        return self.filtered_records[self.ui.active_view]

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

    def _resolve_entry(self, handle: str) -> CoreEntry | None:
        entry = self.entries_by_handle.get(handle)
        if entry is not None:
            return entry
        normalized = _normalize_handle(handle)
        if normalized is None:
            return None
        entry = self.entries_by_handle.get(normalized)
        if entry is not None:
            return entry
        parsed = _parse_handle(normalized)
        if parsed is None:
            return None
        kind, key = parsed
        return self.entries_by_kind[kind].get(key)

    def _entry_runtime_name(self, entry: CoreEntry) -> str:
        for key in (entry.kind, "itempool", "itempoollist"):
            value = _maybe_str(entry.data.get(key))
            if value:
                return value
        return entry.source_key

    def _extract_itempool_children(self, entry: CoreEntry) -> list[ChildLink]:
        items = entry.data.get("items")
        if not isinstance(items, list):
            return []
        out: list[ChildLink] = []
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                continue
            for raw_handle in _extract_handles(item.get("item")):
                normalized = _normalize_handle(raw_handle)
                parsed = _parse_handle(raw_handle)
                if normalized is None or parsed is None:
                    continue
                linked = self._resolve_entry(normalized)
                out.append(
                    ChildLink(
                        kind=parsed[0],
                        handle=normalized,
                        raw_handle=raw_handle,
                        label=linked.display_name if linked is not None else raw_handle,
                        via=f"items[{index}]",
                    )
                )
        return out

    def _extract_itempoollist_children(self, entry: CoreEntry) -> list[ChildLink]:
        itempools = entry.data.get("itempools")
        if not isinstance(itempools, list):
            return []
        out: list[ChildLink] = []
        for index, item in enumerate(itempools):
            if not isinstance(item, Mapping):
                continue
            pool_blob = item.get("itempool")
            if not isinstance(pool_blob, Mapping):
                continue
            pool_item = pool_blob.get("item")
            for raw_handle in _extract_handles(pool_item):
                normalized = _normalize_handle(raw_handle)
                parsed = _parse_handle(raw_handle)
                if normalized is None or parsed is None:
                    continue
                linked = self._resolve_entry(normalized)
                out.append(
                    ChildLink(
                        kind=parsed[0],
                        handle=normalized,
                        raw_handle=raw_handle,
                        label=linked.display_name if linked is not None else raw_handle,
                        via=f"itempools[{index}]",
                    )
                )
        return out

    def _direct_links(self, entry: CoreEntry) -> tuple[ChildLink, ...]:
        if entry.kind == "itempool":
            return tuple(self._extract_itempool_children(entry))
        if entry.kind == "itempoollist":
            return tuple(self._extract_itempoollist_children(entry))
        return ()

    def analyze(self, entry: CoreEntry) -> AnalysisResult:
        cached = self.analysis_cache.get(entry.handle)
        if cached is not None:
            return cached
        direct_links = self._direct_links(entry)
        issues = self._resolve_links(entry.handle, direct_links, {entry.handle})
        result = AnalysisResult(direct_links=direct_links, issues=tuple(_dedupe_preserve_order(issues)))
        self.analysis_cache[entry.handle] = result
        return result

    def _resolve_links(
        self,
        root_handle: str,
        links: Sequence[ChildLink],
        trail: set[str],
    ) -> list[str]:
        issues: list[str] = []
        for link in links:
            linked_entry = self._resolve_entry(link.handle)
            if linked_entry is None:
                issues.append(f"Missing linked entry: {link.handle}")
                continue
            if linked_entry.handle in trail:
                issues.append(f"Cycle detected at {linked_entry.display_name}")
                continue
            child_issues = self._resolve_links(
                root_handle,
                self._direct_links(linked_entry),
                trail | {linked_entry.handle},
            )
            issues.extend(child_issues)
        return issues

    def _draw_custom_layout_controls(self, imgui: Any) -> None:
        if imgui.button(f"{'[Circle]' if self.ui.pattern_mode == 'circle' else 'Circle'}##bl4_item_spawner_pattern_circle"):
            self.ui.pattern_mode = "circle"
        imgui.same_line()
        if imgui.button(f"{'[Custom]' if self.ui.pattern_mode == 'custom' else 'Custom'}##bl4_item_spawner_pattern_custom"):
            self.ui.pattern_mode = "custom"

        if self.ui.pattern_mode == "circle":
            _changed, self.ui.circle_base_radius = imgui.input_int("Base Radius", self.ui.circle_base_radius)
            self.ui.circle_base_radius = max(0, self.ui.circle_base_radius)
            _changed, self.ui.circle_ring_spacing = imgui.input_int("Ring Spacing", self.ui.circle_ring_spacing)
            self.ui.circle_ring_spacing = max(0, self.ui.circle_ring_spacing)
            _changed, self.ui.circle_max_per_ring = imgui.input_int("Max Per Ring", self.ui.circle_max_per_ring)
            self.ui.circle_max_per_ring = max(1, self.ui.circle_max_per_ring)
            _changed, self.ui.circle_height_offset = imgui.input_int("Height Offset", self.ui.circle_height_offset)
            return

        _changed, self.ui.custom_base_forward = imgui.input_int("Base Forward", self.ui.custom_base_forward)
        _changed, self.ui.custom_base_right = imgui.input_int("Base Right", self.ui.custom_base_right)
        _changed, self.ui.custom_base_up = imgui.input_int("Base Up", self.ui.custom_base_up)
        _changed, self.ui.custom_step_forward = imgui.input_int("Next Forward", self.ui.custom_step_forward)
        _changed, self.ui.custom_step_right = imgui.input_int("Next Right", self.ui.custom_step_right)
        _changed, self.ui.custom_step_up = imgui.input_int("Next Up", self.ui.custom_step_up)

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

        imgui.separator_text("Filter")
        imgui.set_next_item_width(-1)
        changed, self.ui.filter_text = imgui.input_text("##bl4_item_spawner_filter", self.ui.filter_text)
        if changed and self.ui.live_refresh:
            self.apply_filter(force=True)

        if imgui.button("Apply Filter"):
            self.refresh()
        imgui.same_line()
        changed, self.ui.live_refresh = imgui.checkbox("Live Refresh##bl4_item_spawner_live", self.ui.live_refresh)
        if changed:
            if self.ui.live_refresh:
                self.refresh()
            else:
                self.ui.status_text = "Manual refresh enabled. Click Apply Filter or Refresh to update cached results."
        if not self.ui.live_refresh:
            imgui.same_line()
            if imgui.button("Refresh"):
                self.refresh()
        imgui.same_line()
        if imgui.button("Reload Data"):
            self.reload_data()

        counts = {kind: len(self.filtered_records[kind]) for kind in ("itempool", "itempoollist")}
        for index, kind in enumerate(("itempool", "itempoollist")):
            if index:
                imgui.same_line()
            if imgui.button(f"{kind} ({counts[cast(VISIBLE_KIND, kind)]})"):
                self.ui.active_view = cast(VISIBLE_KIND, kind)
                self.ui.selected_index = 0

        avail = imgui.get_content_region_avail()
        results_height = max(220.0, min(320.0, avail.y * 0.34))
        filtered = self.get_filtered_results()
        selected = self.get_selected_entry()

        imgui.begin_child("bl4_item_spawner_results", (0, results_height))
        self._draw_results_panel(imgui, filtered)
        imgui.end_child()

        imgui.separator()

        imgui.begin_child("bl4_item_spawner_details", (0, 0))
        self._draw_details_panel(imgui, selected)
        imgui.end_child()
        imgui.end()

    def _draw_results_panel(self, imgui: Any, filtered: Sequence[CoreEntry]) -> None:
        imgui.separator_text(f"{self.ui.active_view} results ({len(filtered)})")
        flags = (
            getattr(imgui, "TableFlags_Borders", 0)
            | getattr(imgui, "TableFlags_RowBg", 0)
            | getattr(imgui, "TableFlags_ScrollY", 0)
            | getattr(imgui, "TableFlags_Resizable", 0)
            | getattr(imgui, "TableFlags_SizingStretchProp", 0)
        )
        if imgui.begin_table("##bl4_item_spawner_table", 1, flags, (0, 0)):
            imgui.table_setup_column("Name")
            imgui.table_headers_row()
            for index, entry in enumerate(filtered):
                imgui.table_next_row()
                imgui.table_set_column_index(0)
                clicked, _ = imgui.selectable(entry.display_name, index == self.ui.selected_index)
                if clicked:
                    self.select_entry(index)
            imgui.end_table()

    def _draw_details_panel(self, imgui: Any, selected: CoreEntry | None) -> None:
        analysis = self.analyze(selected) if selected is not None else None

        if selected is None:
            imgui.text_disabled("No browser entry selected. Manual itempool spawn is still available below.")

        imgui.separator_text("Spawn")
        _changed, self.ui.spawn_count = imgui.input_int("Amount", self.ui.spawn_count)
        self.ui.spawn_count = max(1, min(MAX_SPAWN_COUNT, self.ui.spawn_count))
        _changed, self.ui.spawn_level = imgui.input_int("Level", self.ui.spawn_level)
        self.ui.spawn_level = max(1, min(MAX_ITEM_LEVEL, self.ui.spawn_level))
        _changed, self.ui.show_custom_layout = imgui.checkbox("Custom Pattern", self.ui.show_custom_layout)
        if self.ui.show_custom_layout:
            self._draw_custom_layout_controls(imgui)

        imgui.text("Enter name manually:")
        imgui.set_next_item_width(-1)
        _changed, self.ui.manual_itempool = imgui.input_text("##bl4_item_spawner_manual_itempool", self.ui.manual_itempool)
        if imgui.button("Spawn"):
            self.spawn_selected_or_manual(selected, self.ui.spawn_count, self.ui.spawn_level)
        if not self.ui.manual_itempool.strip() and (selected is None or selected.kind != "itempool"):
            imgui.same_line()
            imgui.text_disabled("Select an itempool or enter one manually.")

        imgui.separator_text("Links")
        if selected is None or analysis is None:
            imgui.text_disabled("Select an itempool or itempoollist entry to browse links.")
            return

        if not analysis.direct_links:
            imgui.text_disabled("No direct children resolved.")
        elif imgui.begin_table(
            "##bl4_item_spawner_links",
            3,
            getattr(imgui, "TableFlags_Borders", 0)
            | getattr(imgui, "TableFlags_RowBg", 0)
            | getattr(imgui, "TableFlags_Resizable", 0)
            | getattr(imgui, "TableFlags_SizingStretchProp", 0)
            | getattr(imgui, "TableFlags_ScrollX", 0),
        ):
            width_stretch = getattr(imgui, "TableColumnFlags_WidthStretch", 0)
            if width_stretch:
                imgui.table_setup_column("Type", width_stretch, 0.20)
                imgui.table_setup_column("Name", width_stretch, 0.70)
                imgui.table_setup_column("Action", width_stretch, 0.10)
            else:
                imgui.table_setup_column("Type")
                imgui.table_setup_column("Name")
                imgui.table_setup_column("Action")
            imgui.table_headers_row()
            for index, link in enumerate(analysis.direct_links):
                linked = self._resolve_entry(link.handle)
                imgui.table_next_row()
                imgui.table_set_column_index(0)
                imgui.text(link.kind)
                imgui.table_set_column_index(1)
                imgui.text_wrapped(link.label)
                imgui.table_set_column_index(2)
                rendered_action = False
                if link.kind == "itempool":
                    rendered_action = True
                    if imgui.small_button(f"Spawn##bl4_item_spawner_spawn_{index}"):
                        self.spawn_link(link, self.ui.spawn_count, self.ui.spawn_level)
                if linked is not None:
                    if rendered_action:
                        imgui.same_line()
                    rendered_action = True
                    if imgui.small_button(f"Open##bl4_item_spawner_open_{index}"):
                        self.ui.active_view = cast(VISIBLE_KIND, linked.kind)
                        self.apply_filter()
                        try:
                            self.ui.selected_index = self.get_filtered_results().index(linked)
                        except ValueError:
                            self.ui.selected_index = 0
                if not rendered_action:
                    imgui.text_disabled("-")
            imgui.end_table()
        if analysis.issues:
            for issue in analysis.issues:
                imgui.text_colored((1.0, 0.7, 0.35, 1.0), issue)

    def _resolve_pool_entry(self, pool_name: str) -> CoreEntry | None:
        entry = self._resolve_entry(pool_name)
        if entry is not None and entry.kind == "itempool":
            return entry
        normalized_key = _normalize_text(pool_name)
        entry = self.entries_by_kind["itempool"].get(normalized_key)
        if entry is not None:
            return entry
        for candidate in self.entries_by_kind["itempool"].values():
            if normalized_key in {
                candidate.key,
                _normalize_text(candidate.source_key),
                _normalize_text(candidate.display_name),
                _normalize_text(self._entry_runtime_name(candidate)),
            }:
                return candidate
        return None

    def _ensure_pool_spawn_safe(self, pool_name: str) -> None:
        entry = self._resolve_pool_entry(pool_name)
        if entry is None:
            return
        analysis = self.analyze(entry)
        for issue in analysis.issues:
            if "Cycle detected" in issue:
                raise RuntimeError(
                    f"Blocked recursive pool '{self._entry_runtime_name(entry)}': {issue}"
                )

    def spawn_entry(self, entry: CoreEntry, count: int, level: int | None) -> None:
        try:
            if entry.kind == "itempool":
                self._spawn_from_pool(self._entry_runtime_name(entry), count, level)
            else:
                raise RuntimeError(f"Cannot spawn entry kind '{entry.kind}'.")
            self.ui.error_text = ""
            self.ui.status_text = f"Spawned {entry.display_name} x{count}."
        except Exception as exc:
            self.ui.error_text = f"Spawn failed: {exc}"
            _log_error(self.ui.error_text)

    def spawn_link(self, link: ChildLink, count: int, level: int | None) -> None:
        try:
            if link.kind == "itempool":
                linked = self._resolve_entry(link.handle)
                pool_name = self._entry_runtime_name(linked) if linked is not None else link.raw_handle
                self._spawn_from_pool(pool_name, count, level)
            else:
                raise RuntimeError(f"Cannot spawn child kind '{link.kind}'.")
            self.ui.error_text = ""
            self.ui.status_text = f"Spawned {link.label} x{count}."
        except Exception as exc:
            self.ui.error_text = f"Spawn failed: {exc}"
            _log_error(self.ui.error_text)

    def spawn_manual_itempool(self, pool_name: str, count: int, level: int) -> None:
        try:
            candidate = pool_name.strip()
            if not candidate:
                raise RuntimeError("Manual itempool is empty.")
            entry = self._resolve_pool_entry(candidate)
            if entry is None:
                raise RuntimeError(f"Could not resolve itempool '{candidate}'.")
            self._spawn_from_pool(self._entry_runtime_name(entry), count, level)
            self.ui.manual_itempool = ""
            self.ui.error_text = ""
            self.ui.status_text = f"Spawned {entry.display_name} x{count}."
        except Exception as exc:
            self.ui.error_text = f"Spawn failed: {exc}"
            _log_error(self.ui.error_text)

    def spawn_selected_or_manual(self, selected: CoreEntry | None, count: int, level: int) -> None:
        manual_itempool = self.ui.manual_itempool.strip()
        if manual_itempool:
            self.spawn_manual_itempool(manual_itempool, count, level)
            return
        if selected is None or selected.kind != "itempool":
            self.ui.error_text = "Select an itempool or enter one manually."
            return
        self.spawn_entry(selected, count, level)

    def _get_world(self) -> UObject | None:
        viewport = getattr(ENGINE, "GameViewport", None)
        world = getattr(viewport, "World", None)
        if world is not None:
            return world
        for class_name in ("OakPlayerController", "PlayerController"):
            try:
                objects = unrealsdk.find_all(class_name, False) or []
            except Exception:
                continue
            for obj in objects:
                if obj is None:
                    continue
                candidate = getattr(obj, "World", None)
                if candidate is not None:
                    return candidate
                pawn = getattr(obj, "Pawn", None)
                candidate = getattr(pawn, "World", None) if pawn is not None else None
                if candidate is not None:
                    return candidate
        return None

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

    def _get_spawn_transform_from_pc(self, pc: UObject) -> Any | None:
        pawn = getattr(pc, "Pawn", None)
        if pawn is None:
            return None
        player_location = pawn.K2_GetActorLocation()
        for getter_name in ("K2_GetActorTransform", "GetActorTransform", "GetTransform"):
            getter = getattr(pawn, getter_name, None)
            if callable(getter):
                try:
                    transform = getter()
                    setattr(transform, "Translation", _make_vector(player_location.X, player_location.Y, player_location.Z))
                    return transform
                except Exception:
                    continue
        return None

    def _get_player_location_rotation(self, pc: UObject) -> tuple[Any, Any] | None:
        pawn = getattr(pc, "Pawn", None)
        if pawn is None:
            return None
        return pawn.K2_GetActorLocation(), pawn.K2_GetActorRotation()

    def _build_spawn_offsets(self, amount: int) -> list[tuple[float, float, float]]:
        amount = max(1, amount)
        if self.ui.pattern_mode == "circle":
            out: list[tuple[float, float, float]] = []
            max_per_ring = max(1, self.ui.circle_max_per_ring)
            for index in range(amount):
                ring = index // max_per_ring
                pos_in_ring = index % max_per_ring
                angle_offset = (2.0 * math.pi / max_per_ring) * pos_in_ring if pos_in_ring != 0 else 0.0
                radius = float(self.ui.circle_base_radius + ring * self.ui.circle_ring_spacing)
                out.append(
                    (
                        radius * math.cos(angle_offset),
                        radius * math.sin(angle_offset),
                        float(self.ui.circle_height_offset),
                    )
                )
            return out

        return [
            (
                float(self.ui.custom_base_forward + index * self.ui.custom_step_forward),
                float(self.ui.custom_base_right + index * self.ui.custom_step_right),
                float(self.ui.custom_base_up + index * self.ui.custom_step_up),
            )
            for index in range(amount)
        ]

    def _spawn_pose_from_offsets(
        self,
        player_location: Any,
        player_rotation: Any,
        forward_offset: float,
        right_offset: float,
        up_offset: float,
    ) -> tuple[Any, Any]:
        yaw_rad = math.radians(player_rotation.Yaw)
        forward_x = math.cos(yaw_rad)
        forward_y = math.sin(yaw_rad)
        right_x = -math.sin(yaw_rad)
        right_y = math.cos(yaw_rad)

        new_x = player_location.X + forward_x * forward_offset + right_x * right_offset
        new_y = player_location.Y + forward_y * forward_offset + right_y * right_offset
        new_z = player_location.Z + up_offset
        location = _make_vector(new_x, new_y, new_z)

        direction_x = new_x - player_location.X
        direction_y = new_y - player_location.Y
        direction_z = new_z - player_location.Z
        direction_length = math.sqrt(direction_x * direction_x + direction_y * direction_y + direction_z * direction_z) or 0.0001
        direction_x /= direction_length
        direction_y /= direction_length
        direction_z /= direction_length
        yaw = math.degrees(math.atan2(direction_y, direction_x))
        horizontal_length = math.sqrt(direction_x * direction_x + direction_y * direction_y)
        pitch = math.degrees(math.atan2(direction_z, horizontal_length))
        return location, _make_rotator(pitch, yaw, 0.0)

    def _build_spawn_poses(self, pc: UObject, amount: int) -> list[tuple[Any, Any]]:
        player_pose = self._get_player_location_rotation(pc)
        if player_pose is None:
            return []
        player_location, player_rotation = player_pose
        return [
            self._spawn_pose_from_offsets(player_location, player_rotation, forward, right, up)
            for forward, right, up in self._build_spawn_offsets(amount)
        ]

    def _stabilize_pickup(self, pickup: UObject, location: Any, rotation: Any) -> None:
        pickup.K2_TeleportTo(location, rotation or IGNORE_STRUCT)
        pickup.K2_AddActorWorldOffset(DOWN_TO_GROUND, True, IGNORE_STRUCT, True)
        comp = pickup.RootPrimitiveComponent
        if comp is None:
            return
        comp.SetLinearDamping(1000)
        comp.SetAngularDamping(1000)

        set_velocity = getattr(comp, "SetPhysicsLinearVelocity", None)
        if callable(set_velocity):
            set_velocity(ZERO_VECTOR, False)

        for angular_name in (
            "SetPhysicsAngularVelocityInDegrees",
            "SetAllPhysicsAngularVelocityInDegrees",
            "SetPhysicsAngularVelocity",
        ):
            set_angular_velocity = getattr(comp, angular_name, None)
            if not callable(set_angular_velocity):
                continue
            try:
                set_angular_velocity(ZERO_VECTOR, False)
            except TypeError:
                try:
                    set_angular_velocity(ZERO_VECTOR)
                except Exception:
                    pass
            except Exception:
                pass
            break

        set_simulate_physics = getattr(comp, "SetSimulatePhysics", None)
        if callable(set_simulate_physics):
            set_simulate_physics(False)

    def _pool_name_formats(self, pool_name: str) -> list[str]:
        formats = [pool_name]
        normalized = pool_name.strip()
        if normalized.startswith("itempool'") and normalized.endswith("'"):
            formats.append(normalized.replace("itempool'", "").rstrip("'"))
        return _dedupe_preserve_order(formats)

    def _get_pool_store(self) -> UObject:
        configs = unrealsdk.find_all("NexusConfigStoreItemPool", False)
        if not configs:
            raise RuntimeError("NexusConfigStoreItemPool not found.")
        return list(configs)[-1]

    def _spawn_from_pool(self, pool_name: str, count: int, level: int | None) -> None:
        resolved_level = max(1, min(MAX_ITEM_LEVEL, 60 if level is None else level))
        self._ensure_pool_spawn_safe(pool_name)
        config = self._get_pool_store()
        world = self._get_world()
        pc = self._get_runtime_pc()
        if world is None or pc is None:
            raise RuntimeError("Player or world is not available.")
        spawn_transform = self._get_spawn_transform_from_pc(pc)
        if spawn_transform is None:
            raise RuntimeError("Could not derive a spawn transform.")

        poses = self._build_spawn_poses(pc, count)
        if not poses:
            raise RuntimeError("Could not build spawn positions.")

        for location, rotation in poses:
            try:
                setattr(spawn_transform, "Translation", location)
                setattr(spawn_transform, "Rotation", rotation)
            except Exception:
                pass
            spawned = False
            for candidate in self._pool_name_formats(pool_name):
                try:
                    config.SpawnInventoryFromItemPool(world, spawn_transform, resolved_level, candidate)
                    spawned = True
                    break
                except Exception:
                    continue
            if not spawned:
                raise RuntimeError(f"SpawnInventoryFromItemPool failed for '{pool_name}'.")


CONTROLLER = BL4ItemSpawnerController()


def _toggle_item_spawner() -> None:
    CONTROLLER.toggle_window()


ITEM_SPAWNER_KEY = keybind(
    "Toggle BL4 Item Spawner",
    "F9",
    callback=_toggle_item_spawner,
    display_name="Toggle BL4 Item Spawner",
    description="Opens the BL4 itempool/itempoollist spawner window.",
)


@command("bl4_item_spawner_gui", description="Open or close the BL4 item spawner window.")
def bl4_item_spawner_gui(_args: argparse.Namespace) -> None:
    CONTROLLER.toggle_window()


@command("bl4_item_spawner_reload", description="Reload the bundled BL4 itempool/itempoollist lookup data.")
def bl4_item_spawner_reload(_args: argparse.Namespace) -> None:
    CONTROLLER.reload_data()


build_mod(
    name="BL4 Item Spawner",
    author="Cr4nkSt4r",
    description="BL4 itempool and itempoollist browser with itempool spawning.",
    supported_games=Game.BL4,
    coop_support=CoopSupport.ClientSide,
    keybinds=[ITEM_SPAWNER_KEY],
    commands=[bl4_item_spawner_gui, bl4_item_spawner_reload],
    on_enable=CONTROLLER.on_enable,
    on_disable=CONTROLLER.on_disable,
)
