"""
test_column_style.py - Tests for measuring Explorer's Size column.

Uses synthetic cell images so the checks are deterministic: a normal dark-theme cell,
a selected (highlighted) row, a blank folder cell, and a light-theme cell.
"""

import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")

from PIL import Image, ImageDraw
import column_style

CELL_W, CELL_H = 100, 19
ORIGIN = (0, 0)


def make_cell(bg, fg=None, text="1,479 KB", right_pad=8):
    """Builds a cell image with text right-aligned `right_pad` px from the edge."""
    img = Image.new("RGB", (CELL_W, CELL_H), bg)
    if fg is not None:
        draw = ImageDraw.Draw(img)
        # Block glyphs: predictable extents, no font dependency
        char_w, gap, height = 5, 2, 9
        total = len(text) * (char_w + gap) - gap
        x = CELL_W - right_pad - total
        top = (CELL_H - height) // 2
        for ch in text:
            if ch != " ":
                draw.rectangle([x, top, x + char_w - 1, top + height - 1], fill=fg)
            x += char_w + gap
    return img


class TestMeasureCell(unittest.TestCase):
    def test_dark_theme_cell(self):
        img = make_cell((25, 25, 25), (222, 222, 222))
        style = column_style.measure_cell(img, ORIGIN, (0, 0, CELL_W, CELL_H))
        self.assertIsNotNone(style)
        self.assertEqual(style.right_padding, 8)
        self.assertEqual(style.text_color, "#dedede")

    def test_light_theme_cell(self):
        img = make_cell((255, 255, 255), (26, 26, 26))
        style = column_style.measure_cell(img, ORIGIN, (0, 0, CELL_W, CELL_H))
        self.assertIsNotNone(style)
        self.assertEqual(style.text_color, "#1a1a1a")

    def test_padding_is_actually_measured(self):
        img = make_cell((25, 25, 25), (222, 222, 222), right_pad=20)
        style = column_style.measure_cell(img, ORIGIN, (0, 0, CELL_W, CELL_H))
        self.assertEqual(style.right_padding, 20)

    def test_blank_folder_cell_rejected(self):
        img = make_cell((25, 25, 25), None)
        self.assertIsNone(column_style.measure_cell(img, ORIGIN, (0, 0, CELL_W, CELL_H)))

    def test_selected_row_rejected(self):
        """A highlighted row: the selection colour dominates, so unhighlighted pixels
        would otherwise be mistaken for text (this produced padding=0, colour=bg)."""
        img = Image.new("RGB", (CELL_W, CELL_H), (0, 90, 158))   # selection blue
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, 20, CELL_H - 1], fill=(25, 25, 25))  # unhighlighted edge
        self.assertIsNone(column_style.measure_cell(img, ORIGIN, (0, 0, CELL_W, CELL_H)))

    def test_full_bleed_rejected(self):
        """Text touching the top and bottom edges isn't text -- it's a fill."""
        img = Image.new("RGB", (CELL_W, CELL_H), (25, 25, 25))
        ImageDraw.Draw(img).rectangle([60, 0, 91, CELL_H - 1], fill=(222, 222, 222))
        self.assertIsNone(column_style.measure_cell(img, ORIGIN, (0, 0, CELL_W, CELL_H)))

    def test_out_of_bounds_cell_rejected(self):
        img = make_cell((25, 25, 25), (222, 222, 222))
        self.assertIsNone(column_style.measure_cell(img, ORIGIN, (0, 0, CELL_W + 50, CELL_H)))


class TestSystemFont(unittest.TestCase):
    def test_returns_plausible_font(self):
        family, px = column_style.system_ui_font()
        self.assertTrue(family)
        self.assertGreaterEqual(px, 8)
        self.assertLessEqual(px, 48)

    def test_size_is_independent_of_glyph_shape(self):
        """Regression: font size was inferred from the glyph bounding box, so a comma
        or descender inflated it (a '1,652 KB' row measured 15px, '21 KB' 12px)."""
        tall = make_cell((25, 25, 25), (222, 222, 222), text="1,652 KB")
        short = make_cell((25, 25, 25), (222, 222, 222), text="21 KB")
        a = column_style.measure_cell(tall, ORIGIN, (0, 0, CELL_W, CELL_H))
        b = column_style.measure_cell(short, ORIGIN, (0, 0, CELL_W, CELL_H))
        self.assertEqual(a.font_px, b.font_px)


class TestFallbacks(unittest.TestCase):
    def test_defaults_are_the_measured_explorer_values(self):
        style = column_style.ColumnStyle()
        self.assertFalse(style.measured)
        self.assertEqual(style.right_padding, 8)
        self.assertEqual(style.text_color, "#dedede")


if __name__ == "__main__":
    unittest.main(verbosity=2)
