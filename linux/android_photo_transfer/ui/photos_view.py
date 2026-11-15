"""Photos and Videos tab: thumbnail grid (160x186 tiles) + list view toggle.

Also carries the shared LibraryView base that the Files tab subclasses:
sidebar (categories, Quick Access, folder tree), toolbar (title, search,
Show filter, sort, type filter, grid/list toggle), empty panel.
"""
from __future__ import annotations

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QRect,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .helpers import (
    ACCENT,
    ACCENT_LIGHT,
    DIVIDER,
    SIDEBAR,
    SUBTLE_TEXT,
    TEXT,
    TILE_BG,
)
from .viewmodels import SHOW_OPTIONS, SORT_OPTIONS, TYPE_OPTIONS, TreeNode

TILE_WIDTH = 160
TILE_HEIGHT = 186
TILE_IMAGE_HEIGHT = 122


# ---------------------------------------------------------------------------
# Grid model + delegate (virtualized: only visible tiles are painted)
# ---------------------------------------------------------------------------

class _TileModel(QAbstractListModel):
    def __init__(self, library):
        super().__init__()
        self._library = library

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self._library.visible_items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if row < 0 or row >= len(self._library.visible_items):
            return None
        vm = self._library.visible_items[row]
        if role == Qt.ItemDataRole.UserRole:
            return vm
        if role == Qt.ItemDataRole.DisplayRole:
            return vm.name
        if role == Qt.ItemDataRole.ToolTipRole:
            return vm.tooltip_text
        return None

    def refresh(self):
        self.beginResetModel()
        self.endResetModel()


class _TileDelegate(QStyledItemDelegate):
    """Paints one 160x186 tile: thumbnail, badges, checkbox, name, size."""

    def __init__(self, thumbnails, parent=None):
        super().__init__(parent)
        self._thumbnails = thumbnails
        self._placeholder_brush = QBrush(QColor(TILE_BG))
        self._checked_brush = QBrush(QColor(ACCENT_LIGHT))
        self._accent_pen = QPen(QColor(ACCENT), 2)
        self._hover_pen = QPen(QColor("#D6DCE2"), 2)
        self._transparent_pen = QPen(Qt.PenStyle.NoPen)

    def sizeHint(self, option, index):
        return QSize(TILE_WIDTH, TILE_HEIGHT)

    def paint(self, painter: QPainter, option, index):
        vm = index.data(Qt.ItemDataRole.UserRole)
        if vm is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(4, 4, -4, -4)

        # Tile background + border.
        if vm.is_checked:
            painter.setBrush(self._checked_brush)
            painter.setPen(self._accent_pen)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.setBrush(QBrush(QColor("#F4F6F8")))
            painter.setPen(self._hover_pen)
        else:
            painter.setBrush(QBrush(QColor("white")))
            painter.setPen(self._transparent_pen)
        painter.drawRoundedRect(rect, 6, 6)

        inner = rect.adjusted(5, 5, -5, -5)
        image_rect = QRect(inner.left(), inner.top(), inner.width(), TILE_IMAGE_HEIGHT)
        painter.setBrush(self._placeholder_brush)
        painter.setPen(self._transparent_pen)
        painter.drawRoundedRect(image_rect, 4, 4)

        pixmap = self._thumbnails.pixmap_for(vm.item)
        if pixmap is None and vm.item.kind in ("photo", "video"):
            pixmap = self._thumbnails.request(vm.item)
        if pixmap is not None and not pixmap.isNull():
            painter.save()
            painter.setClipRect(image_rect)
            scaled = pixmap.scaled(
                image_rect.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = image_rect.left() + (image_rect.width() - scaled.width()) // 2
            y = image_rect.top() + (image_rect.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
            painter.restore()
        else:
            # Type glyph placeholder (drawn text, no icon font dependency).
            glyph = {"photo": "Photo", "video": "Video"}.get(vm.item.kind, "File")
            painter.setPen(QPen(QColor("#A9AEB5")))
            font = QFont()
            font.setPointSize(11)
            painter.setFont(font)
            painter.drawText(image_rect, Qt.AlignmentFlag.AlignCenter, glyph)

        # Video badge, bottom right of the image.
        if vm.is_video:
            painter.setBrush(QBrush(QColor(0, 0, 0, 176)))
            painter.setPen(self._transparent_pen)
            badge = QRect(image_rect.right() - 52, image_rect.bottom() - 22, 47, 17)
            painter.drawRoundedRect(badge, 3, 3)
            painter.setPen(QPen(QColor("white")))
            font = QFont()
            font.setPointSize(9)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, "VIDEO")

        # Already-on-PC badge, top right of the image.
        if vm.already_transferred:
            painter.setBrush(QBrush(QColor("#2E9E44")))
            painter.setPen(self._transparent_pen)
            badge = QRect(image_rect.right() - 23, image_rect.top() + 5, 18, 18)
            painter.drawEllipse(badge)
            painter.setPen(QPen(QColor("white")))
            font = QFont()
            font.setPointSize(10)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, "\u2713")

        # Checkbox, top left of the tile.
        box = QRect(inner.left() + 1, inner.top() + 1, 18, 18)
        painter.setBrush(QBrush(QColor("white")))
        painter.setPen(QPen(QColor("#8A8A8A"), 1))
        painter.drawRoundedRect(box, 3, 3)
        if vm.is_checked:
            painter.setBrush(QBrush(QColor(ACCENT)))
            painter.setPen(self._transparent_pen)
            painter.drawRoundedRect(box, 3, 3)
            painter.setPen(QPen(QColor("white")))
            font = QFont()
            font.setPointSize(11)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, "\u2713")

        # Name + size below the image.
        font = QFont()
        font.setPointSize(11)
        painter.setFont(font)
        painter.setPen(QPen(QColor(TEXT)))
        name_rect = QRect(inner.left(), image_rect.bottom() + 7, inner.width(), 18)
        name = painter.fontMetrics().elidedText(vm.name, Qt.TextElideMode.ElideRight, inner.width())
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignCenter, name)
        font.setPointSize(10)
        painter.setFont(font)
        painter.setPen(QPen(QColor(SUBTLE_TEXT)))
        size_rect = QRect(inner.left(), name_rect.bottom() + 1, inner.width(), 16)
        painter.drawText(size_rect, Qt.AlignmentFlag.AlignCenter, f"({vm.size_text})")
        painter.restore()


# ---------------------------------------------------------------------------
# Library view (shared by both tabs)
# ---------------------------------------------------------------------------

class LibraryViewBase(QWidget):
    """Sidebar + toolbar + grid/list + empty panel for one LibraryViewModel."""

    def __init__(self, library, thumbnails, parent=None):
        super().__init__(parent)
        self.library = library
        self.thumbnails = thumbnails
        self._updating_sidebar = False

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)

        # ---- sidebar ------------------------------------------------------
        sidebar = QWidget()
        sidebar.setStyleSheet(f"background: {SIDEBAR}; border-right: 1px solid {DIVIDER};")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 8, 0, 8)
        sidebar_layout.setSpacing(0)

        section = "PHONE" if library.is_media else "FILE TYPES"
        sidebar_layout.addWidget(self._section_label(section))
        self.category_list = QListWidget()
        self.category_list.setStyleSheet("QListWidget { border: none; background: transparent; }"
                                         "QListWidget::item { padding: 6px 10px; border-radius: 4px; }"
                                         "QListWidget::item:selected { background: #E6E6E6; color: black; }")
        self.category_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.category_list.itemClicked.connect(self._on_category_clicked)
        sidebar_layout.addWidget(self.category_list)

        self.quick_label = self._section_label("QUICK ACCESS")
        sidebar_layout.addWidget(self.quick_label)
        self.quick_list = QListWidget()
        self.quick_list.setStyleSheet(self.category_list.styleSheet())
        self.quick_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.quick_list.itemClicked.connect(self._on_category_clicked)
        sidebar_layout.addWidget(self.quick_list)
        if library.is_media:
            self.quick_label.hide()
            self.quick_list.hide()

        sidebar_layout.addWidget(self._section_label("FOLDERS"))
        self.folder_tree = QTreeWidget()
        self.folder_tree.setHeaderHidden(True)
        self.folder_tree.setStyleSheet("QTreeWidget { border: none; background: transparent; }")
        self.folder_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.folder_tree.itemClicked.connect(self._on_folder_clicked)
        sidebar_layout.addWidget(self.folder_tree, 1)

        splitter.addWidget(sidebar)

        # ---- content --------------------------------------------------------
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(12, 10, 14, 8)
        toolbar.setSpacing(8)
        self.title_label = QLabel()
        title_font = QFont()
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        toolbar.addWidget(self.title_label, 1)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search...")
        self.search_box.setFixedWidth(200)
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(library.set_search_text)
        toolbar.addWidget(self.search_box)

        self.show_combo = QComboBox()
        for label, value in SHOW_OPTIONS:
            self.show_combo.addItem(label, value)
        self.show_combo.setMinimumWidth(124)
        self.show_combo.currentIndexChanged.connect(
            lambda i: library.set_transfer_filter(self.show_combo.itemData(i))
        )
        toolbar.addWidget(self.show_combo)

        self.sort_combo = QComboBox()
        for label, _, _ in SORT_OPTIONS:
            self.sort_combo.addItem(label)
        self.sort_combo.setMinimumWidth(120)
        self.sort_combo.currentIndexChanged.connect(library.set_sort_index)
        toolbar.addWidget(self.sort_combo)

        self.type_combo = QComboBox()
        for label, value in TYPE_OPTIONS:
            self.type_combo.addItem(label, value)
        self.type_combo.setMinimumWidth(124)
        self.type_combo.currentIndexChanged.connect(
            lambda i: library.set_type_filter(self.type_combo.itemData(i))
        )
        toolbar.addWidget(self.type_combo)

        self.grid_button = QPushButton("Grid")
        self.grid_button.setCheckable(True)
        self.grid_button.setToolTip("Thumbnail view")
        self.grid_button.clicked.connect(lambda: library.set_grid_view(True))
        toolbar.addWidget(self.grid_button)
        self.list_button = QPushButton("List")
        self.list_button.setCheckable(True)
        self.list_button.setToolTip("List view")
        self.list_button.clicked.connect(lambda: library.set_grid_view(False))
        toolbar.addWidget(self.list_button)
        if not library.is_media:
            self.type_combo.hide()
            self.grid_button.hide()
            self.list_button.hide()

        content_layout.addLayout(toolbar)

        # Grid view.
        self._tile_model = _TileModel(library)
        self.grid_view = QListView()
        self.grid_view.setModel(self._tile_model)
        self.grid_view.setItemDelegate(_TileDelegate(thumbnails, self.grid_view))
        self.grid_view.setViewMode(QListView.ViewMode.IconMode)
        self.grid_view.setFlow(QListView.Flow.LeftToRight)
        self.grid_view.setWrapping(True)
        self.grid_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid_view.setGridSize(QSize(TILE_WIDTH, TILE_HEIGHT))
        self.grid_view.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.grid_view.setMouseTracking(True)
        self.grid_view.setUniformItemSizes(True)
        self.grid_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.grid_view.setFrameShape(QFrame.Shape.NoFrame)
        self.grid_view.clicked.connect(self._on_grid_clicked)
        self.grid_view.doubleClicked.connect(self._on_grid_double_clicked)
        self.grid_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid_view.customContextMenuRequested.connect(self._on_grid_context_menu)
        content_layout.addWidget(self.grid_view, 1)

        # List view.
        self.list_view = QTableWidget(0, 7)
        self.list_view.setHorizontalHeaderLabels(
            ["", "Name", "Type", "Size", "Date modified", "Folder", "On PC"]
        )
        self.list_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list_view.setShowGrid(False)
        self.list_view.setAlternatingRowColors(False)
        self.list_view.verticalHeader().setVisible(False)
        self.list_view.horizontalHeader().setSectionsClickable(True)
        self.list_view.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self.list_view.cellClicked.connect(self._on_list_clicked)
        self.list_view.cellDoubleClicked.connect(self._on_list_double_clicked)
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_view.customContextMenuRequested.connect(self._on_list_context_menu)
        header = self.list_view.horizontalHeader()
        header.setSectionResizeMode(0, header.ResizeMode.Fixed)
        header.setSectionResizeMode(1, header.ResizeMode.Stretch)
        header.setSectionResizeMode(6, header.ResizeMode.Fixed)
        self.list_view.setColumnWidth(0, 38)
        self.list_view.setColumnWidth(2, 85)
        self.list_view.setColumnWidth(3, 78)
        self.list_view.setColumnWidth(4, 155)
        self.list_view.setColumnWidth(5, 185)
        self.list_view.setColumnWidth(6, 55)
        content_layout.addWidget(self.list_view, 1)

        # Empty panel.
        self.empty_label = QLabel()
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setStyleSheet(f"color: {SUBTLE_TEXT}; font-size: 15px;")
        self.empty_label.setMaximumWidth(460)
        empty_wrap = QWidget()
        empty_layout = QHBoxLayout(empty_wrap)
        empty_layout.addStretch(1)
        empty_layout.addWidget(self.empty_label)
        empty_layout.addStretch(1)
        content_layout.addWidget(empty_wrap, 1)
        self._empty_wrap = empty_wrap

        splitter.addWidget(content)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([232, 900])

        # ---- wiring ---------------------------------------------------------
        library.view_changed.connect(self._on_view_changed)
        library.counts_changed.connect(self._refresh_sidebar)
        library.state_changed.connect(self._on_state_changed)
        thumbnails.thumbnail_ready.connect(self._on_thumbnail_ready)
        self._refresh_sidebar()
        self._on_view_changed()
        self._on_state_changed()

    # -- sidebar ---------------------------------------------------------------
    @staticmethod
    def _section_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(f"color: {SUBTLE_TEXT}; font-size: 11px; font-weight: bold; padding: 6px 16px 4px 16px;")
        return label

    def _refresh_sidebar(self) -> None:
        self._updating_sidebar = True
        try:
            self._fill_category_list(self.category_list, self.library.categories, hide_empty=False)
            self._fill_category_list(self.quick_list, self.library.quick_access, hide_empty=True)
            self._rebuild_folder_tree()
        finally:
            self._updating_sidebar = False

    def _fill_category_list(self, widget: QListWidget, categories, hide_empty: bool) -> None:
        widget.clear()
        for cat in categories:
            if hide_empty and cat.count == 0:
                continue
            item = QListWidgetItem(f"{cat.name}  ({cat.count})")
            item.setData(Qt.ItemDataRole.UserRole, cat)
            widget.addItem(item)
            if cat is self.library.selected_category:
                widget.setCurrentItem(item)
        # Size the list to its contents so no scrollbar clips the counts and
        # the folder tree below gets the remaining space.
        if widget.count() > 0:
            row_height = widget.sizeHintForRow(0)
            widget.setFixedHeight(row_height * widget.count() + 2 * widget.frameWidth())
        else:
            widget.setFixedHeight(0)

    def _rebuild_folder_tree(self) -> None:
        selected = self.library.selected_folder
        self.folder_tree.clear()
        self._tree_items: dict[int, QTreeWidgetItem] = {}

        def add_node(node: TreeNode, parent_item=None):
            label = f"{node.name}  ({node.count})"
            item = QTreeWidgetItem(parent_item, [label]) if parent_item is not None \
                else QTreeWidgetItem(self.folder_tree, [label])
            item.setData(0, Qt.ItemDataRole.UserRole, node)
            item.setExpanded(True)
            self._tree_items[id(node)] = item
            if node is selected:
                self.folder_tree.setCurrentItem(item)
            for child in sorted(node.children.values(), key=lambda n: n.name.lower()):
                add_node(child, item)

        for root in sorted(self.library.folder_roots, key=lambda n: n.name.lower()):
            add_node(root)

    def _on_category_clicked(self, item: QListWidgetItem) -> None:
        if self._updating_sidebar:
            return
        cat = item.data(Qt.ItemDataRole.UserRole)
        if cat is not None:
            self.folder_tree.clearSelection()
            self.library.select_category(cat)

    def _on_folder_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if self._updating_sidebar:
            return
        node = item.data(0, Qt.ItemDataRole.UserRole)
        if node is not None:
            self.category_list.clearSelection()
            self.quick_list.clearSelection()
            self.library.select_folder(node)

    def expand_to_folder(self, node: TreeNode | None) -> None:
        """Expand the tree to a node and select it (context menu: Open Phone Folder)."""
        if node is None:
            return
        chain = []
        current = node
        while current is not None:
            chain.append(current)
            current = current.parent
        for ancestor in reversed(chain):
            item = self._tree_items.get(id(ancestor))
            if item is not None:
                item.setExpanded(True)
        item = self._tree_items.get(id(node))
        if item is not None:
            self.folder_tree.setCurrentItem(item)
            self.library.select_folder(node)

    # -- view updates --------------------------------------------------------------
    def _on_view_changed(self) -> None:
        self.title_label.setText(self.library.view_title)
        self._row_by_id = {id(vm.item): row for row, vm in enumerate(self.library.visible_items)}
        self._tile_model.refresh()
        self._refresh_list_view()
        self.grid_button.setChecked(self.library.is_grid_view)
        self.list_button.setChecked(not self.library.is_grid_view)
        self._apply_view_mode()

    def _on_state_changed(self) -> None:
        self._apply_view_mode()

    def _apply_view_mode(self) -> None:
        show_empty = self.library.show_empty_panel
        self._empty_wrap.setVisible(show_empty)
        self.empty_label.setText(self.library.empty_message)
        if show_empty:
            self.grid_view.hide()
            self.list_view.hide()
            return
        if self.library.is_grid_view:
            self.grid_view.show()
            self.list_view.hide()
        else:
            self.grid_view.hide()
            self.list_view.show()

    def _on_thumbnail_ready(self, _key: str) -> None:
        # Only the visible tiles need repainting; the viewport update is cheap.
        if self.grid_view.isVisible():
            self.grid_view.viewport().update()

    def _refresh_list_view(self) -> None:
        items = self.library.visible_items
        table = self.list_view
        table.setRowCount(len(items))
        for row, vm in enumerate(items):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked if vm.is_checked else Qt.CheckState.Unchecked)
            table.setItem(row, 0, check)

            name = QTableWidgetItem(vm.name)
            name.setData(Qt.ItemDataRole.UserRole, vm)
            table.setItem(row, 1, name)
            table.setItem(row, 2, QTableWidgetItem(vm.kind_text))
            table.setItem(row, 3, QTableWidgetItem(vm.size_text))
            table.setItem(row, 4, QTableWidgetItem(vm.date_text))
            table.setItem(row, 5, QTableWidgetItem(vm.folder_text))
            on_pc = QTableWidgetItem("\u2713" if vm.already_transferred else "")
            if vm.already_transferred:
                on_pc.setForeground(QBrush(QColor("#2E9E44")))
            table.setItem(row, 6, on_pc)
            if vm.is_checked:
                for col in range(7):
                    table.item(row, col).setBackground(QBrush(QColor(ACCENT_LIGHT)))

    # -- grid interaction ------------------------------------------------------------
    def _vm_at_grid_index(self, index: QModelIndex):
        if not index.isValid():
            return None
        return index.data(Qt.ItemDataRole.UserRole)

    def _on_grid_clicked(self, index: QModelIndex) -> None:
        from PySide6.QtWidgets import QApplication

        vm = self._vm_at_grid_index(index)
        if vm is None:
            return
        modifiers = QApplication.keyboardModifiers()
        self.library.on_tile_clicked(
            vm, bool(modifiers & Qt.KeyboardModifier.ShiftModifier), double_click=False
        )
        self._tile_model.refresh()

    def _on_grid_double_clicked(self, index: QModelIndex) -> None:
        vm = self._vm_at_grid_index(index)
        if vm is not None:
            self.library.on_tile_clicked(vm, shift=False, double_click=True)
            self._tile_model.refresh()

    def _on_grid_context_menu(self, pos) -> None:
        index = self.grid_view.indexAt(pos)
        vm = self._vm_at_grid_index(index)
        if vm is None:
            return
        self._show_context_menu(self.grid_view.mapToGlobal(pos), vm)

    # -- list interaction -------------------------------------------------------------
    def _on_header_clicked(self, section: int) -> None:
        header = self.list_view.horizontalHeaderItem(section)
        if header is not None:
            self.library.sort_by_column(header.text())

    def _vm_at_list_row(self, row: int):
        if 0 <= row < len(self.library.visible_items):
            return self.library.visible_items[row]
        return None

    def _on_list_clicked(self, row: int, _column: int) -> None:
        from PySide6.QtWidgets import QApplication

        vm = self._vm_at_list_row(row)
        if vm is None:
            return
        modifiers = QApplication.keyboardModifiers()
        self.library.on_tile_clicked(
            vm, bool(modifiers & Qt.KeyboardModifier.ShiftModifier), double_click=False
        )
        self._refresh_list_view()

    def _on_list_double_clicked(self, row: int, _column: int) -> None:
        vm = self._vm_at_list_row(row)
        if vm is not None:
            self.library.on_tile_clicked(vm, shift=False, double_click=True)
            self._refresh_list_view()

    def _on_list_context_menu(self, pos) -> None:
        row = self.list_view.rowAt(pos.y())
        vm = self._vm_at_list_row(row)
        if vm is None:
            return
        self._show_context_menu(self.list_view.mapToGlobal(pos), vm)

    # -- context menu ------------------------------------------------------------------
    def _show_context_menu(self, global_pos, vm) -> None:
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        preview = menu.addAction("Preview")
        preview.triggered.connect(lambda: self.library.open_preview(vm))
        transfer = menu.addAction("Transfer")
        transfer.triggered.connect(lambda: self.library.transfer_item(vm))
        toggle = menu.addAction("Select / Deselect")
        toggle.triggered.connect(lambda: self.library.toggle_checked([vm]))
        menu.addSeparator()
        show_folder = menu.addAction("Open Phone Folder")
        show_folder.triggered.connect(lambda: self.expand_to_folder(self.library.show_folder_of(vm)))
        menu.exec(global_pos)

    # -- thumbnail visibility probe ------------------------------------------------------
    def item_is_on_screen(self, item) -> bool:
        """Used by the thumbnail manager: only load thumbnails for visible tiles."""
        if not self.grid_view.isVisible():
            return True  # list view has no thumbnails; do not block the queue
        row = getattr(self, "_row_by_id", {}).get(id(item))
        if row is None:
            return False
        rect = self.grid_view.visualRect(self._tile_model.index(row, 0))
        return self.grid_view.viewport().rect().intersects(rect)

    def select_all_shortcut(self) -> None:
        self.library.select_all()


class PhotosView(LibraryViewBase):
    """Photos and Videos tab: thumbnail grid 160x186 tiles + list view toggle."""
