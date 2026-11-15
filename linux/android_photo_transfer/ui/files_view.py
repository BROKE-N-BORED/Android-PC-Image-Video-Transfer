"""Files tab: file-type filter + Quick Access + folder tree + sortable list.

The Files tab shows everything that is not a photo or video, always as a
details list (there are no thumbnails to show). It reuses the shared
LibraryViewBase from photos_view.py; the LibraryViewModel kind drives the
sidebar categories, Quick Access and the forced list view.
"""
from __future__ import annotations

from .photos_view import LibraryViewBase


class FilesView(LibraryViewBase):
    """Files tab: file-type filter, Quick Access, folder tree, details list."""

    def __init__(self, library, thumbnails, parent=None):
        super().__init__(library, thumbnails, parent)
        # The Files tab always uses the details list.
        self.library.is_grid_view = False
        self._apply_view_mode()
