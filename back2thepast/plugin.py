from __future__ import annotations

import json
import traceback
from dataclasses import dataclass
from datetime import datetime
import re
from typing import List, Optional
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QAction,
    QDockWidget,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QSpinBox,
)
from qgis.core import QgsProject, QgsRasterLayer, QgsLayerTreeGroup

CONFIG_URLS = [
    "https://s3-us-west-2.amazonaws.com/config.maptiles.arcgis.com/waybackconfig.json",
    "https://s3.amazonaws.com/config.maptiles.arcgis.com/waybackconfig.json",
]
USER_AGENT = "QGIS Back2thePast/1.3"
PLUGIN_GROUP_NAME = "Back2thePast"


@dataclass
class WaybackItem:
    key: str
    title: str
    tile_url: str
    layer_identifier: str
    metadata_url: str = ""
    item_id: str = ""
    release_num: Optional[int] = None
    release_date_label: str = ""
    release_date: Optional[datetime] = None

    @property
    def qgis_xyz_url(self) -> str:
        return (
            self.tile_url
            .replace("{level}", "{z}")
            .replace("{row}", "{y}")
            .replace("{col}", "{x}")
        )

    @property
    def display_label(self) -> str:
        if self.release_date_label:
            if self.release_num is not None:
                return f"{self.release_date_label} (ID: {self.release_num})"
            return self.release_date_label
        if self.release_num is not None:
            return f"Unknown date (ID: {self.release_num})"
        return f"Unknown date ({self.key})"

    @property
    def layer_name(self) -> str:
        suffix = self.release_date_label or (self.release_date.strftime("%Y-%m-%d") if self.release_date else (str(self.release_num) if self.release_num is not None else self.key))
        return f"Esri Wayback {suffix}"


class WaybackDock(QDockWidget):
    def __init__(self, plugin: "Back2thePastPlugin") -> None:
        super().__init__("Back2thePast")
        self.plugin = plugin
        self.items: List[WaybackItem] = []
        self._build_ui()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout()

        intro = QLabel(
            "Load Esri World Imagery Wayback releases ordered by date. "
            "Use Refresh to read the official Wayback configuration."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter:"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("e.g. 2025 or 2024-09")
        self.filter_edit.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self.filter_edit)
        layout.addLayout(filter_row)

        default_row = QHBoxLayout()
        default_row.addWidget(QLabel("Recent layers by default:"))
        self.recent_spin = QSpinBox()
        self.recent_spin.setRange(1, 50)
        self.recent_spin.setValue(10)
        self.recent_spin.valueChanged.connect(self._update_latest_button_text)
        default_row.addWidget(self.recent_spin)
        default_row.addStretch()
        layout.addLayout(default_row)

        button_row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh releases")
        self.refresh_btn.clicked.connect(self.refresh_items)
        button_row.addWidget(self.refresh_btn)

        self.load_recent_btn = QPushButton()
        self._update_latest_button_text()
        self.load_recent_btn.clicked.connect(self.load_latest)
        button_row.addWidget(self.load_recent_btn)

        self.load_selected_btn = QPushButton("Load selected")
        self.load_selected_btn.clicked.connect(self.load_selected)
        button_row.addWidget(self.load_selected_btn)
        layout.addLayout(button_row)

        tools_row = QHBoxLayout()
        self.clear_btn = QPushButton("Clear plugin layers")
        self.clear_btn.clicked.connect(self.clear_plugin_layers)
        tools_row.addWidget(self.clear_btn)
        self.reload_btn = QPushButton("Reload latest on open")
        self.reload_btn.clicked.connect(self.reload_latest_now)
        tools_row.addWidget(self.reload_btn)
        layout.addLayout(tools_row)

        self.status_label = QLabel("No releases loaded yet.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.ExtendedSelection)
        layout.addWidget(self.list_widget)

        footer = QLabel(
            "Authors: Félix González (CSIC), Diego Soto (UBU), Javier Bravo (Evenor)"
        )
        footer.setWordWrap(True)
        layout.addWidget(footer)

        root.setLayout(layout)
        self.setWidget(root)

    def _update_latest_button_text(self) -> None:
        if hasattr(self, 'load_recent_btn'):
            self.load_recent_btn.setText(f"Load latest {self.recent_spin.value()}")

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def refresh_items(self, autoload_default: bool = False, force_reload: bool = False) -> None:
        self._set_status("Reading Wayback release list…")
        self.refresh_btn.setEnabled(False)
        try:
            self.items = self.plugin.fetch_wayback_items(force_reload=force_reload)
            self.list_widget.clear()
            for item in self.items:
                label = item.display_label
                lw_item = QListWidgetItem(label)
                lw_item.setData(Qt.UserRole, item)
                if item.metadata_url:
                    lw_item.setToolTip(f"Layer: {item.layer_identifier}\nMetadata: {item.metadata_url}")
                else:
                    lw_item.setToolTip(f"Layer: {item.layer_identifier}")
                self.list_widget.addItem(lw_item)
            self._apply_filter()
            source = "cache" if self.plugin._cache_used_last_fetch else "service"
            self._set_status(f"Found {len(self.items)} Wayback releases (from {source}).")
            if autoload_default and self.items:
                self.load_latest()
        except Exception as exc:
            self._set_status("Could not retrieve Wayback releases.")
            QMessageBox.critical(
                self,
                "Back2thePast",
                f"Could not retrieve Wayback releases.\n\n{exc}\n\nTip: this can happen if the Esri config URL is blocked or temporarily unavailable.",
            )
        finally:
            self.refresh_btn.setEnabled(True)

    def _apply_filter(self) -> None:
        text = self.filter_edit.text().strip().lower()
        visible = 0
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            hay = item.text().lower()
            hidden = text not in hay if text else False
            item.setHidden(hidden)
            if not hidden:
                visible += 1
        if self.items:
            self._set_status(f"Showing {visible} of {len(self.items)} releases.")

    def _selected_wayback_items(self) -> List[WaybackItem]:
        out: List[WaybackItem] = []
        for lw_item in self.list_widget.selectedItems():
            wb = lw_item.data(Qt.UserRole)
            if wb:
                out.append(wb)
        return out

    def load_latest(self) -> None:
        if not self.items:
            self.refresh_items(autoload_default=False)
            if not self.items:
                return
        count = min(self.recent_spin.value(), len(self.items))
        added = self.plugin.add_layers(self.items[:count])
        self._set_status(f"Loaded {added} recent layers into the project.")

    def reload_latest_now(self) -> None:
        self.refresh_items(autoload_default=False, force_reload=True)
        if self.items:
            self.load_latest()

    def load_selected(self) -> None:
        selected = self._selected_wayback_items()
        if not selected:
            QMessageBox.information(self, "Back2thePast", "Select one or more releases first.")
            return
        added = self.plugin.add_layers(selected)
        self._set_status(f"Loaded {added} selected layers into the project.")

    def clear_plugin_layers(self) -> None:
        removed = self.plugin.clear_plugin_layers()
        self._set_status(f"Removed {removed} plugin layers from the project.")


class Back2thePastPlugin:
    def __init__(self, iface) -> None:
        self.iface = iface
        self.action: Optional[QAction] = None
        self.dock: Optional[WaybackDock] = None
        self._items_cache: Optional[List[WaybackItem]] = None
        self._cache_used_last_fetch: bool = False

    def initGui(self) -> None:
        import os
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        self.action = QAction(QIcon(icon_path), "Back2thePast", self.iface.mainWindow())
        self.action.setObjectName("Back2thePast")
        self.action.setToolTip("Back2thePast")
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu("&Back2thePast", self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self) -> None:
        if self.action:
            self.iface.removePluginMenu("&Back2thePast", self.action)
            self.iface.removeToolBarIcon(self.action)
        if self.dock:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None

    def run(self) -> None:
        if self.dock is None:
            self.dock = WaybackDock(self)
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock)
            self.dock.refresh_items(autoload_default=True)
        self.dock.show()
        self.dock.raise_()

    def _download_json(self, url: str) -> dict:
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=25) as response:
            payload = response.read().decode("utf-8")
        data = json.loads(payload)
        if not isinstance(data, (dict, list)):
            raise ValueError("Unexpected Wayback configuration format.")
        return data

    def fetch_wayback_items(self, force_reload: bool = False) -> List[WaybackItem]:
        if self._items_cache is not None and not force_reload:
            self._cache_used_last_fetch = True
            return list(self._items_cache)

        last_error = None
        data = None
        self._cache_used_last_fetch = False
        for url in CONFIG_URLS:
            try:
                data = self._download_json(url)
                break
            except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
                last_error = exc
                continue
        if data is None:
            raise RuntimeError(f"Failed to download Wayback configuration from known URLs. Last error: {last_error}")

        if isinstance(data, dict):
            iterable = data.items()
        else:
            iterable = enumerate(data)

        items: List[WaybackItem] = []
        for key, raw in iterable:
            if not isinstance(raw, dict):
                continue
            tile_url = raw.get("itemURL") or raw.get("tileUrl") or ""
            if not tile_url:
                continue
            title = raw.get("itemTitle") or raw.get("title") or str(key)
            layer_identifier = raw.get("layerIdentifier") or str(key)
            release_label = (raw.get("releaseDateLabel") or raw.get("releaseDate") or "").strip()
            release_dt = None
            raw_release_num = raw.get("releaseNum")
            release_num = None
            try:
                if raw_release_num is not None and str(raw_release_num).strip() != "":
                    release_num = int(raw_release_num)
            except Exception:
                release_num = None

            raw_release_datetime = raw.get("releaseDatetime") or raw.get("releaseDateTime") or raw.get("releaseTimestamp")
            if raw_release_datetime:
                try:
                    release_dt = datetime.utcfromtimestamp(int(raw_release_datetime) / 1000)
                except Exception:
                    release_dt = None

            if release_dt is None and release_label:
                for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y/%m"):
                    try:
                        release_dt = datetime.strptime(release_label, fmt)
                        break
                    except Exception:
                        pass

            if not release_label:
                title_match = re.search(r'(20\d{2}[-/]\d{2}[-/]\d{2})', title)
                if title_match:
                    release_label = title_match.group(1).replace('/', '-')
                else:
                    layer_match = re.search(r'WB_(20\d{2})_R(\d{2})', layer_identifier)
                    if layer_match:
                        release_label = f"{layer_match.group(1)}-R{layer_match.group(2)}"

            if release_dt is None and release_label:
                cleaned = release_label.replace('/', '-')
                if re.match(r'^20\d{2}-\d{2}-\d{2}$', cleaned):
                    try:
                        release_dt = datetime.strptime(cleaned, "%Y-%m-%d")
                        release_label = cleaned
                    except Exception:
                        pass

            if not release_label and release_dt is not None:
                release_label = release_dt.strftime("%Y-%m-%d")

            items.append(
                WaybackItem(
                    key=str(key),
                    title=title,
                    tile_url=tile_url,
                    layer_identifier=layer_identifier,
                    metadata_url=raw.get("metadataLayerUrl", ""),
                    item_id=raw.get("itemID", ""),
                    release_num=release_num,
                    release_date_label=release_label,
                    release_date=release_dt,
                )
            )

        items.sort(key=lambda x: (x.release_date or datetime.min, x.release_num or -1, x.title), reverse=True)
        self._items_cache = list(items)
        return items

    def _ensure_group(self) -> QgsLayerTreeGroup:
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(PLUGIN_GROUP_NAME)
        if group is None:
            group = root.insertGroup(0, PLUGIN_GROUP_NAME)
        return group

    def add_layers(self, items: List[WaybackItem]) -> int:
        project = QgsProject.instance()
        group = self._ensure_group()
        existing_names = {layer.name() for layer in project.mapLayers().values()}
        added = 0
        for item in items:
            name = item.layer_name
            if name in existing_names:
                continue
            url = quote(item.qgis_xyz_url, safe='/:?=&{}%,')
            uri = f"type=xyz&url={url}&zmin=0&zmax=23"
            layer = QgsRasterLayer(uri, name, "wms")
            if not layer.isValid():
                continue
            layer.setCustomProperty("back2thepast/managed", True)
            layer.setCustomProperty("back2thepast/release_label", item.release_date_label)
            project.addMapLayer(layer, False)
            group.insertLayer(0, layer)
            existing_names.add(name)
            added += 1
        return added

    def clear_plugin_layers(self) -> int:
        project = QgsProject.instance()
        to_remove = [
            layer.id()
            for layer in project.mapLayers().values()
            if layer.customProperty("back2thepast/managed", False)
        ]
        if to_remove:
            project.removeMapLayers(to_remove)
        root = project.layerTreeRoot()
        group = root.findGroup(PLUGIN_GROUP_NAME)
        if group is not None and len(group.children()) == 0:
            root.removeChildNode(group)
        return len(to_remove)
