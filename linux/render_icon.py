#!/usr/bin/env python3
"""Render the app icons from icons/app.svg to PNG (256, 128, 48 px).

Usage (from the port/ directory):
    ~/workspace/forge-v2-testenv/bin/python render_icon.py

Needs PySide6 with QtSvg (the Qt test python has it). If QtSvg is
missing, falls back to drawing the same glyph with QPainter so the PNGs
can still be produced. No network calls. Always renders from the SVG
when it can, so the PNGs never drift from the source.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ICONS_DIR = os.path.join(HERE, "icons")
SVG_PATH = os.path.join(ICONS_DIR, "app.svg")
SIZES = (256, 128, 48)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def render_with_qtsvg(size: int, out_path: str) -> bool:
    try:
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtSvg import QSvgRenderer
    except ImportError:
        return False
    renderer = QSvgRenderer(SVG_PATH)
    if not renderer.isValid():
        raise SystemExit(f"ERROR: {SVG_PATH} did not parse as SVG")
    image = QImage(QSize(size, size), QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    if not image.save(out_path):
        raise SystemExit(f"ERROR: could not save {out_path}")
    return True


def render_with_qpainter(size: int, out_path: str) -> None:
    """Fallback: draw the same phone-arrow-PC glyph directly."""
    from PySide6.QtCore import QPoint, QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen

    s = size / 512.0
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(0)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(s, s)
    teal = QColor("#14707f")
    white = QColor("#ffffff")
    gold = QColor("#ffd166")
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(teal)
    p.drawRoundedRect(QRectF(8, 8, 496, 496), 112, 112)
    thick = QPen(white, 18)
    thick.setCap(Qt.PenCapStyle.RoundCap)
    thick.setJoin(Qt.PenJoinStyle.RoundJoin)
    gold_pen = QPen(gold, 10)
    gold_pen.setCap(Qt.PenCapStyle.RoundCap)
    gold_pen.setJoin(Qt.PenJoinStyle.RoundJoin)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(thick)
    p.drawRoundedRect(QRectF(76, 116, 132, 280), 26, 26)  # phone
    p.drawLine(118, 150, 166, 150)
    p.drawLine(232, 256, 308, 256)  # arrow shaft
    p.drawPolyline([QPoint(284, 226), QPoint(318, 256), QPoint(284, 286)])
    p.drawRoundedRect(QRectF(330, 150, 128, 188), 16, 16)  # monitor
    p.drawLine(394, 338, 394, 374)
    p.drawLine(356, 378, 432, 378)
    p.setPen(gold_pen)
    p.drawRoundedRect(QRectF(98, 182, 88, 76), 10, 10)
    p.setBrush(gold)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QPoint(122, 206), 10, 10)
    p.drawEllipse(QPoint(142, 358), 12, 12)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(gold_pen)
    p.drawPolyline([QPoint(102, 250), QPoint(132, 222), QPoint(152, 240),
                    QPoint(168, 226), QPoint(182, 242)])
    p.end()
    if not image.save(out_path):
        raise SystemExit(f"ERROR: could not save {out_path}")


def main() -> int:
    if not os.path.isfile(SVG_PATH):
        print(f"ERROR: missing {SVG_PATH}")
        return 1
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("ERROR: PySide6 is needed to render the icons.")
        print("Run with the Qt test python:")
        print("  ~/workspace/forge-v2-testenv/bin/python render_icon.py")
        return 1
    app = QApplication.instance() or QApplication(sys.argv)
    for size in SIZES:
        out_path = os.path.join(ICONS_DIR, f"app-{size}.png")
        try:
            used_svg = render_with_qtsvg(size, out_path)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - fall back to QPainter
            print(f"QtSvg render failed for {size}px ({exc}); using QPainter")
            used_svg = False
            render_with_qpainter(size, out_path)
        if not used_svg:
            render_with_qpainter(size, out_path)
        print(f"Wrote {out_path} ({'SVG' if used_svg else 'QPainter fallback'})")
    app.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
