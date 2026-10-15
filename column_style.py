"""
column_style.py - Matches the overlay's text to Explorer's own Size column.

Rather than hardcoding a padding and colour that only look right on one theme and
one DPI, this captures the window and measures a row Explorer already drew a size
into, then copies the values it finds.
"""

import ctypes
from collections import Counter
from dataclasses import dataclass
from typing import Optional, Tuple

import win32gui
import win32ui
from PIL import Image

# Renders windows composited by DWM, and works even when the window is occluded --
# a plain screen grab would capture whatever is covering it instead.
PW_RENDERFULLCONTENT = 2

# Measured from Explorer at 100% scaling; used until a live sample succeeds.
FALLBACK_RIGHT_PADDING = 8
FALLBACK_TEXT_DARK = "#dedede"
FALLBACK_TEXT_LIGHT = "#1a1a1a"
FALLBACK_FONT_PX = 12

# A cell must have at least this much text in it to be worth measuring
MIN_INK_PIXELS = 40
# How far a pixel must sit from the cell background to count as text
INK_THRESHOLD = 60
# Guards against measuring something that isn't plain text -- a selected (highlighted)
# row, a partially captured cell, or an icon
MAX_INK_FRACTION = 0.35
MAX_GLYPH_HEIGHT_FRACTION = 0.8
MIN_RIGHT_PADDING = 1
MAX_RIGHT_PADDING_FRACTION = 0.6


@dataclass
class ColumnStyle:
    right_padding: int = FALLBACK_RIGHT_PADDING
    text_color: str = FALLBACK_TEXT_DARK
    font_px: int = FALLBACK_FONT_PX
    font_family: str = "Segoe UI"
    measured: bool = False


class LOGFONTW(ctypes.Structure):
    _fields_ = [
        ("lfHeight", ctypes.c_long), ("lfWidth", ctypes.c_long),
        ("lfEscapement", ctypes.c_long), ("lfOrientation", ctypes.c_long),
        ("lfWeight", ctypes.c_long), ("lfItalic", ctypes.c_byte),
        ("lfUnderline", ctypes.c_byte), ("lfStrikeOut", ctypes.c_byte),
        ("lfCharSet", ctypes.c_byte), ("lfOutPrecision", ctypes.c_byte),
        ("lfClipPrecision", ctypes.c_byte), ("lfQuality", ctypes.c_byte),
        ("lfPitchAndFamily", ctypes.c_byte), ("lfFaceName", ctypes.c_wchar * 32),
    ]


class NONCLIENTMETRICSW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint), ("iBorderWidth", ctypes.c_int),
        ("iScrollWidth", ctypes.c_int), ("iScrollHeight", ctypes.c_int),
        ("iCaptionWidth", ctypes.c_int), ("iCaptionHeight", ctypes.c_int),
        ("lfCaptionFont", LOGFONTW), ("iSmCaptionWidth", ctypes.c_int),
        ("iSmCaptionHeight", ctypes.c_int), ("lfSmCaptionFont", LOGFONTW),
        ("iMenuWidth", ctypes.c_int), ("iMenuHeight", ctypes.c_int),
        ("lfMenuFont", LOGFONTW), ("lfStatusFont", LOGFONTW),
        ("lfMessageFont", LOGFONTW), ("iPaddedBorderWidth", ctypes.c_int),
    ]


SPI_GETNONCLIENTMETRICS = 0x0029


def system_ui_font(hwnd: int = 0) -> Tuple[str, int]:
    """
    The UI font Explorer draws list text with, as (family, pixel size).

    Deriving the size from captured glyphs is unreliable -- a comma or a descender
    inflates the measured height -- so the size comes from the system metrics, which
    is where Explorer gets it too. Queried at the window's own DPI when possible.
    """
    metrics = NONCLIENTMETRICSW()
    metrics.cbSize = ctypes.sizeof(NONCLIENTMETRICSW)
    ok = False
    try:
        dpi = 0
        if hwnd and hasattr(ctypes.windll.user32, "GetDpiForWindow"):
            dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
        if dpi and hasattr(ctypes.windll.user32, "SystemParametersInfoForDpi"):
            ok = bool(ctypes.windll.user32.SystemParametersInfoForDpi(
                SPI_GETNONCLIENTMETRICS, metrics.cbSize, ctypes.byref(metrics), 0, dpi))
        if not ok:
            ok = bool(ctypes.windll.user32.SystemParametersInfoW(
                SPI_GETNONCLIENTMETRICS, metrics.cbSize, ctypes.byref(metrics), 0))
    except Exception:
        ok = False

    if not ok:
        return "Segoe UI", FALLBACK_FONT_PX

    font = metrics.lfMessageFont
    # lfHeight is negative when it expresses character height in pixels
    px = abs(int(font.lfHeight)) or FALLBACK_FONT_PX
    return (font.lfFaceName or "Segoe UI"), max(8, min(48, px))


def capture_window(hwnd: int) -> Optional[Tuple[Image.Image, Tuple[int, int]]]:
    """Captures a window's own pixels. Returns (image, screen origin of the image)."""
    bmp = mem = src = None
    wdc = 0
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            return None

        wdc = win32gui.GetWindowDC(hwnd)
        src = win32ui.CreateDCFromHandle(wdc)
        mem = src.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(src, width, height)
        mem.SelectObject(bmp)

        if not ctypes.windll.user32.PrintWindow(hwnd, mem.GetSafeHdc(), PW_RENDERFULLCONTENT):
            return None

        info = bmp.GetInfo()
        img = Image.frombuffer(
            "RGB", (info["bmWidth"], info["bmHeight"]),
            bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1
        )
        return img, (left, top)
    except Exception:
        return None
    finally:
        try:
            if bmp is not None:
                win32gui.DeleteObject(bmp.GetHandle())
            if mem is not None:
                mem.DeleteDC()
            if src is not None:
                src.DeleteDC()
            if wdc:
                win32gui.ReleaseDC(hwnd, wdc)
        except Exception:
            pass


def measure_cell(img: Image.Image, origin: Tuple[int, int],
                 cell: Tuple[int, int, int, int]) -> Optional[ColumnStyle]:
    """
    Measures one Size cell: how far its text sits from the right edge, what colour
    it is, and how tall the glyphs are. Returns None if the cell holds no text.
    """
    ox, oy = origin
    box = (cell[0] - ox, cell[1] - oy, cell[2] - ox, cell[3] - oy)
    if box[0] < 0 or box[1] < 0 or box[2] > img.width or box[3] > img.height:
        return None
    if box[2] - box[0] <= 0 or box[3] - box[1] <= 0:
        return None

    crop = img.crop(box)
    width, height = crop.size

    # One flat C-level read and a single pass over it. Indexing through load() and
    # then walking the cell a second time to compute distances cost ~1.5ms per cell,
    # which is charged four times per measurement.
    # getdata() is deprecated from Pillow 11 and gone in 14; get_flattened_data is
    # its replacement and does not exist on older versions.
    _flat = getattr(crop, "get_flattened_data", None)
    pixels = list(_flat()) if _flat is not None else list(crop.getdata())
    counts = Counter(pixels)
    background = counts.most_common(1)[0][0]
    bg_r, bg_g, bg_b = background[0], background[1], background[2]

    ink = []
    for index, colour in enumerate(pixels):
        dist = (abs(colour[0] - bg_r) + abs(colour[1] - bg_g) + abs(colour[2] - bg_b))
        if dist > INK_THRESHOLD:
            ink.append((index % width, index // width, colour, dist))

    if len(ink) < MIN_INK_PIXELS:
        return None

    # A selected row is filled with the highlight colour, which then counts as the
    # "background" and makes every normal pixel look like text. Text covers only a
    # small part of a cell, so a large ink area means this is not a plain text cell.
    if len(ink) > width * height * MAX_INK_FRACTION:
        return None

    xs = [p[0] for p in ink]
    ys = [p[1] for p in ink]

    # Real text has clear space above and below it inside the cell; a highlight or a
    # partially captured cell runs to the edges.
    if min(ys) <= 0 or max(ys) >= height - 1:
        return None

    glyph_height = max(ys) - min(ys) + 1
    if glyph_height > height * MAX_GLYPH_HEIGHT_FRACTION:
        return None

    right_padding = width - 1 - max(xs)
    if right_padding < MIN_RIGHT_PADDING or right_padding > width * MAX_RIGHT_PADDING_FRACTION:
        return None

    # The fully-opaque glyph pixels, not the antialiased edges
    strongest = max(ink, key=lambda p: p[3])[2]

    return ColumnStyle(
        right_padding=right_padding,
        text_color="#{:02x}{:02x}{:02x}".format(strongest[0], strongest[1], strongest[2]),
        measured=True,
    )


def measure_from_rows(hwnd: int, cells: list) -> Optional[ColumnStyle]:
    """
    Measures Explorer's Size column using the first of `cells` that contains text.

    `cells` are screen rects of Size cells, ideally from file rows -- folder rows are
    blank, which is the whole reason this app exists.
    """
    if not cells:
        return None
    captured = capture_window(hwnd)
    if captured is None:
        return None
    img, origin = captured

    found = [s for s in (measure_cell(img, origin, c) for c in cells) if s is not None]
    if not found:
        return None

    family, font_px = system_ui_font(hwnd)

    # Agree across rows rather than trusting one, so an odd row (a selection that
    # slipped past the guards, a cell mid-repaint) cannot set the style on its own.
    return ColumnStyle(
        right_padding=_median([s.right_padding for s in found]),
        text_color=Counter(s.text_color for s in found).most_common(1)[0][0],
        font_px=font_px,
        font_family=family,
        measured=True,
    )


def _median(values: list) -> int:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]
