# -*- coding: utf-8 -*-
"""
SkyDeck by KDtime - один прозрачный оверлей для War Thunder.

Данные: http://127.0.0.1:8111/state + /indicators (официальный локальный API игры).
Оверлей - обычное окно поверх игры, без инжекта: RTSS не нужен
(игра должна идти в оконном / borderless режиме; для эксклюзивного
полноэкрана есть необязательный вывод в RTSS).

Управление:
  Ctrl+Shift+E - режим настройки (таскать виджеты, угол - размер, колесо - масштаб)
  Ctrl+Shift+H - показать / скрыть весь оверлей
  Ctrl+Shift+C - вернуть виртуальную ручку (режим «Мышь») в центр
  Иконка в трее - то же самое + выход
По умолчанию окно скрыто от захвата экрана (OBS «Захват окна/экрана» его не видит).
"""
import sys
import math
import json
import time
import ctypes
import ctypes.wintypes
import http.client
import os
import re
import struct
import threading
import subprocess
import urllib.request
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QRectF, QRect, QPointF, QTimer, QObject
from PySide6.QtGui import (QPainter, QColor, QPen, QBrush, QFont, QFontMetricsF,
                           QIcon, QPixmap, QAction)
from PySide6.QtWidgets import (QApplication, QWidget, QFrame, QSystemTrayIcon, QMenu,
                               QCheckBox, QPushButton, QVBoxLayout, QHBoxLayout,
                               QGridLayout, QLabel, QSlider, QColorDialog, QComboBox, QDoubleSpinBox,
                               QScrollArea)

try:
    from rtss import RtssOsd, format_osd
except Exception:
    RtssOsd, format_osd = None, None

IS_WIN = sys.platform == "win32"
WT_HOST, WT_PORT = "127.0.0.1", 8111  # именно 127.0.0.1: localhost -> ::1, игра слушает только IPv4
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
APP_NAME = "SkyDeck"
APP_AUTHOR = "KDtime"
APP_TITLE = "%s by %s" % (APP_NAME, APP_AUTHOR)
APP_VERSION = "1.2.0"
EXE_NAME = APP_NAME + ".exe"
# Репозиторий GitHub с файлом-обновлением, формат "логин/репозиторий" (или "update_repo" в конфиге).
# Пусто - проверка обновлений отключена.
UPDATE_REPO = "KDtime/Deck-by-KDtime"
RAW_BASE = "https://raw.githubusercontent.com"
UPDATE_BRANCH = "main"        # ветка репозитория, откуда берётся обновление
UPDATE_FILE = "skydeck.py"    # файл-обновление в корне репозитория
CFG_PATH = APP_DIR / "skydeck_config.json"
LEGACY_CFG_PATH = APP_DIR / "wtrti_config.json"  # старое имя - подхватится один раз

DEFAULT_CFG = {
    "opacity": 0.95,
    "bg": True,                  # тёмная подложка под приборами
    "hide_from_capture": True,   # скрыть оверлей от OBS / скриншотов
    "rtss": False,               # необязательный вывод в RTSS
    "screen": -1,                # номер монитора, -1 = основной
    "axis_zero": [0.0, 0.0, 0.0],  # поправка нуля: элероны, тангаж, педали
    "stick_deadzone": 0.0,       # мёртвая зона РУС, доля хода 0..0.3
    "colors": {},                # ключ виджета -> "#rrggbb"
    "stick_source": "api",       # "api" - данные игры, "mouse" - виртуальная ручка от мыши
    "mouse_sens": 100,           # чувствительность виртуальной ручки, %
    "mouse_invert_y": False,
    "mouse_autocenter": False,   # плавный возврат в центр, когда мышь стоит
    "g_zones": {"start": 2.0, "target": 3.0, "limit": 4.5, "good": "#50e682", "bad": "#ff6446"},
    "auto_update_check": True,
    "update_repo": "",
    "poll_ms": 25,
    "poll_ms_idle": 1000,
    "layout": {},                # ключ -> [x, y, w, h] в долях экрана
    "visible": {},               # ключ -> bool
}


def load_cfg():
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    try:
        path = CFG_PATH if CFG_PATH.exists() else LEGACY_CFG_PATH
        if path.exists():
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        pass
    gz = dict(DEFAULT_CFG["g_zones"])
    gz.update(cfg.get("g_zones") if isinstance(cfg.get("g_zones"), dict) else {})
    cfg["g_zones"] = gz
    for k in ("layout", "visible", "colors"):
        if not isinstance(cfg.get(k), dict):
            cfg[k] = {}
    return cfg


def save_cfg(cfg):
    try:
        CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- WinAPI ---
if IS_WIN:
    _u32 = ctypes.windll.user32
    _u32.GetWindowLongW.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
    _u32.GetWindowLongW.restype = ctypes.c_long
    _u32.SetWindowLongW.argtypes = [ctypes.wintypes.HWND, ctypes.c_int, ctypes.c_long]
    _u32.SetWindowLongW.restype = ctypes.c_long
    _u32.SetWindowDisplayAffinity.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.DWORD]
    _u32.SetWindowDisplayAffinity.restype = ctypes.wintypes.BOOL
    _u32.RegisterHotKey.argtypes = [ctypes.wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
    _u32.UnregisterHotKey.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]

    class RAWINPUTDEVICE(ctypes.Structure):
        _fields_ = [("usUsagePage", ctypes.c_ushort), ("usUsage", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_uint32), ("hwndTarget", ctypes.wintypes.HWND)]

    _u32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), ctypes.c_uint, ctypes.c_uint]
    _u32.RegisterRawInputDevices.restype = ctypes.wintypes.BOOL
    _u32.GetRawInputData.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
                                     ctypes.POINTER(ctypes.c_uint), ctypes.c_uint]
    _u32.GetRawInputData.restype = ctypes.c_uint

RAW_HDR = ctypes.sizeof(ctypes.c_void_p) * 2 + 8  # sizeof(RAWINPUTHEADER)


def parse_raw_mouse(raw):
    """Разбор RAWINPUT мыши: (dx, dy) относительного движения или None."""
    if len(raw) < RAW_HDR + 24:
        return None
    if struct.unpack_from("<I", raw, 0)[0] != 0:  # RIM_TYPEMOUSE
        return None
    flags, _b, _rb, dx, dy, _ex = struct.unpack_from("<HxxIIiiI", raw, RAW_HDR)
    if flags & 1:  # абсолютные координаты - не нужны
        return None
    return dx, dy


def win_click_through(hwnd, on):
    if not IS_WIN:
        return
    GWL_EXSTYLE, WS_EX_TRANSPARENT, WS_EX_LAYERED, WS_EX_TOOLWINDOW = -20, 0x20, 0x80000, 0x80
    ex = _u32.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_LAYERED | WS_EX_TOOLWINDOW
    ex = (ex | WS_EX_TRANSPARENT) if on else (ex & ~WS_EX_TRANSPARENT)
    _u32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)


def win_exclude_capture(hwnd, on):
    """WDA_EXCLUDEFROMCAPTURE (Windows 10 2004+): окно не попадает в запись/скриншоты."""
    if not IS_WIN:
        return False
    return bool(_u32.SetWindowDisplayAffinity(hwnd, 0x11 if on else 0))


# ---------------------------------------------------------------- данные ---
class _FastConn:
    """Сервер WT рвёт keep-alive после каждого ответа - при обрыве сразу повтор."""

    def __init__(self, path):
        self.path, self.conn = path, None

    def _fresh(self):
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass
        self.conn = http.client.HTTPConnection(WT_HOST, WT_PORT, timeout=0.5)

    def _once(self):
        if self.conn is None:
            self._fresh()
        self.conn.request("GET", self.path)
        r = self.conn.getresponse()
        body = r.read()
        if r.status != 200:
            raise OSError(f"HTTP {r.status}")
        return json.loads(body)

    def get(self):
        try:
            return self._once()
        except Exception:
            try:
                self._fresh()
                return self._once()
            except Exception:
                self.conn = None
                return None


def _f(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def merge(state, ind):
    """/state + /indicators -> плоский словарь для виджетов."""
    d = {"online": False, "valid": False, "in_flight": False}
    if not state and not ind:
        return d
    d["online"] = True
    s = state if isinstance(state, dict) else {}
    i = ind if isinstance(ind, dict) else {}
    if not (s.get("valid") or i.get("valid")):
        return d
    d["valid"] = d["in_flight"] = True

    d["ias"] = _f(s.get("IAS, km/h", i.get("speed", 0)))
    d["tas"] = _f(s.get("TAS, km/h", 0))
    d["alt"] = _f(s.get("H, m", i.get("altitude_hour", 0)))
    d["vario"] = _f(s.get("Vy, m/s", i.get("vario", 0)))
    d["mach"] = _f(s.get("M", i.get("mach", 0)))
    d["aoa"] = _f(s.get("AoA, deg", 0))
    d["ny"] = _f(s.get("Ny", i.get("g_meter", 1)), 1.0)
    d["compass"] = _f(i.get("compass", 0))

    d["stick_el"] = _f(i.get("stick_elevator", _f(s.get("elevator, %", 0)) / 100.0))
    d["stick_ai"] = _f(i.get("stick_ailerons", _f(s.get("aileron, %", 0)) / 100.0))
    d["pedals"] = _f(i.get("pedals", _f(s.get("rudder, %", 0)) / 100.0))

    thrs = [max(0.0, _f(s[f"throttle {n}, %"]) / 100.0) for n in (1, 2, 3, 4) if f"throttle {n}, %" in s]
    if not thrs:
        t = _f(i.get("throttle", 0))
        thrs = [max(0.0, t)]
    d["thr_avg"] = sum(thrs) / len(thrs)

    d["rpm"] = _f(s.get("RPM 1", i.get("rpm_min", 0)))
    d["power"] = _f(s.get("power 1, hp", 0))
    mf, mf0 = s.get("Mfuel, kg"), s.get("Mfuel0, kg")
    if mf is None:
        mf = _f(i.get("fuel", 0))
        mf0 = max(mf, 1.0)
    d["fuel_pct"] = 100.0 * _f(mf) / max(_f(mf0, 1.0), 1.0)

    d["flaps"] = _f(s["flaps, %"]) if "flaps, %" in s else _f(i.get("flaps", 0)) * 100.0
    d["gear"] = _f(s["gear, %"]) / 100.0 if "gear, %" in s else _f(i.get("gears", 0))
    d["airbrake"] = _f(s["airbrake, %"]) / 100.0 if "airbrake, %" in s else _f(i.get("airbrake_lever", 0))
    # поля, которых может не быть: ищем по имени (какие есть - зависит от игры и техники)
    for src in (s, i):
        for k, v in src.items():
            kl = str(k).lower()
            if "smoke" in kl and "smoke_val" not in d:
                d["smoke_val"] = v
            elif "flare" in kl and "flare_n" not in d:
                d["flare_n"] = v
            elif "chaff" in kl and "chaff_n" not in d:
                d["chaff_n"] = v
            elif "bomb" in kl and ("bay" in kl or "door" in kl) and "bay_val" not in d:
                d["bay_val"] = v
    return d


def demo_data(t=None):
    t = time.time() if t is None else t
    thr = 0.75 + 0.35 * math.sin(t * 0.3)
    return {
        "online": False, "valid": False, "in_flight": False, "demo": True,
        "ias": 420, "tas": 445, "alt": 2500, "vario": 8.5, "mach": 0.35, "aoa": 4.2,
        "ny": 2.1, "compass": (t * 20) % 360,
        "stick_el": math.sin(t * 0.9) * 0.6, "stick_ai": math.cos(t * 0.7) * 0.7,
        "pedals": math.sin(t * 0.5) * 0.8, "thr_avg": max(0.0, min(1.1, thr)),
        "rpm": 2400, "power": 1200, "fuel_pct": 53.0,
        "flaps": (math.sin(t * 0.4) + 1) * 50, "gear": (math.sin(t * 0.5) + 1) / 2,
        "airbrake": (math.sin(t * 0.35) + 1) / 2,
        "smoke_val": 1 if math.sin(t * 0.5) > 0 else 0, "flare_n": 24, "chaff_n": 12,
        "bay_val": (math.sin(t * 0.4) + 1) / 2,
    }


class WTPoller(QThread):
    updated = Signal(dict)

    def __init__(self, cfg):
        super().__init__()
        self.cfg, self._run = cfg, True
        self.pool = ThreadPoolExecutor(max_workers=2)
        self._t_prev, self._hz = time.perf_counter(), 0.0

    def stop(self):
        self._run = False

    def run(self):
        c_state, c_ind = _FastConn("/state"), _FastConn("/indicators")
        while self._run:
            t0 = time.perf_counter()
            f1, f2 = self.pool.submit(c_state.get), self.pool.submit(c_ind.get)
            try:
                state, ind = f1.result(timeout=1.0), f2.result(timeout=1.0)
            except Exception:
                state, ind = None, None
            ping = (time.perf_counter() - t0) * 1000.0
            data = merge(state, ind)
            data["_raw"] = (state, ind)
            now = time.perf_counter()
            dt, self._t_prev = now - self._t_prev, now
            if dt > 0:
                self._hz = self._hz * 0.85 + (1.0 / dt) * 0.15 if self._hz else 1.0 / dt
            data["_hz"], data["_ping"] = self._hz, ping
            self.updated.emit(data)
            if data.get("in_flight"):
                self.msleep(max(20, int(self.cfg.get("poll_ms", 25))))
            else:
                self.msleep(max(200, int(self.cfg.get("poll_ms_idle", 1000))))


# ------------------------------------------------------------- рисование ---
C_TXT, C_DIM = QColor(216, 236, 255), QColor(130, 160, 190)
C_ACC, C_OK = QColor(120, 200, 255), QColor(80, 230, 130)
C_WARN, C_BAD = QColor(255, 190, 60), QColor(255, 100, 70)
C_BAR, C_LINE = QColor(28, 38, 52, 220), QColor(120, 200, 255, 180)
FONT = "Consolas"


def fit_text(p, text, rect, color, bold=True, flags=Qt.AlignCenter):
    """Текст максимального размера, который помещается в rect."""
    if rect.width() < 4 or rect.height() < 4:
        return
    f = QFont(FONT)
    f.setBold(bold)
    px = max(6, int(rect.height() * 0.92))
    f.setPixelSize(px)
    w = QFontMetricsF(f).horizontalAdvance(text)
    if w > rect.width():
        f.setPixelSize(max(6, int(px * rect.width() / w)))
    p.setFont(f)
    p.setPen(color)
    p.drawText(rect, int(flags), text)


def mix(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return QColor(int(c1.red() + (c2.red() - c1.red()) * t),
                  int(c1.green() + (c2.green() - c1.green()) * t),
                  int(c1.blue() + (c2.blue() - c1.blue()) * t))


def g_color(g, base, z):
    """Цвет перегрузки: база -> зелёный (к цели) -> красный (к пределу). Пороги и цвета настраиваются."""
    g = abs(_f(g))
    lo, tg, hi = sorted((_f(z.get("start", 2.0)), _f(z.get("target", 3.0)), _f(z.get("limit", 4.5))))
    good, bad = QColor(z.get("good", "#50e682")), QColor(z.get("bad", "#ff6446"))
    if g <= lo:
        return base
    if g <= tg:
        return mix(base, good, (g - lo) / max(tg - lo, 1e-6))
    if g < hi:
        return mix(good, bad, (g - tg) / max(hi - tg, 1e-6))
    return bad


def ask_color(initial, parent, title, live=None):
    dlg = QColorDialog(QColor(initial), parent)
    dlg.setWindowTitle(title)
    dlg.setOption(QColorDialog.DontUseNativeDialog, True)
    dlg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    if live:
        dlg.currentColorChanged.connect(lambda c: live(c.name()))
    return dlg.currentColor().name() if dlg.exec() else None


def px(x, y, w, h):
    """Раскладка по умолчанию задана в пикселях экрана 1920x1080."""
    return (x / 1920.0, y / 1080.0, w / 1920.0, h / 1080.0)


# ---------------------------------------------------------------- виджеты ---
class HudWidget(QWidget):
    GRIP = 18

    def __init__(self, key, title, ov):
        super().__init__(ov)
        self.key, self.title, self.ov, self.d = key, title, ov, {}
        self._start = None
        self._resize = False
        self.setMouseTracking(True)

    def draw(self, p, r):
        pass

    def col(self, default):
        """Цвет виджета: заданный пользователем или по умолчанию."""
        c = self.ov.cfg.get("colors", {}).get(self.key)
        if c:
            q = QColor(c)
            if q.isValid():
                return q
        return QColor(default)

    def contextMenuEvent(self, e):
        if not self.ov.editing:
            return
        m = QMenu(self)
        a_pick = m.addAction("Цвет...")
        a_reset = m.addAction("Сбросить цвет")
        act = m.exec(e.globalPos())
        if act is a_pick:
            self.ov.pick_color(self.key)
        elif act is a_reset:
            self.ov.set_color(self.key, None)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.ov.cfg.get("bg", True):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(8, 14, 24, 150))
            p.drawRoundedRect(r, 6, 6)
        try:
            self.draw(p, r)
        except Exception:
            pass
        if self.ov.editing:
            p.setBrush(Qt.NoBrush)
            pen = QPen(QColor(255, 220, 90, 230), 1.5, Qt.DashLine)
            p.setPen(pen)
            p.drawRoundedRect(r, 6, 6)
            g = self.GRIP
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 220, 90, 230))
            p.drawPolygon([QPointF(r.right(), r.bottom() - g), QPointF(r.right(), r.bottom()),
                           QPointF(r.right() - g, r.bottom())])
            fm = QFont(FONT)
            fm.setPixelSize(11)
            fm.setBold(True)
            p.setFont(fm)
            tw = QFontMetricsF(fm).horizontalAdvance(self.title) + 8
            p.setBrush(QColor(0, 0, 0, 190))
            p.drawRoundedRect(QRectF(2, 2, tw, 15), 3, 3)
            p.setPen(QColor(255, 220, 90))
            p.drawText(QRectF(2, 2, tw, 15), Qt.AlignCenter, self.title)

    # -- перетаскивание / размер / колесо (только в режиме настройки) --
    def _in_grip(self, pos):
        return pos.x() > self.width() - self.GRIP and pos.y() > self.height() - self.GRIP

    def mousePressEvent(self, e):
        if not self.ov.editing or e.button() != Qt.LeftButton:
            return
        self._resize = self._in_grip(e.position().toPoint())
        self._start = e.globalPosition().toPoint()
        self._geom = QRect(self.geometry())
        self.raise_()

    def mouseMoveEvent(self, e):
        if not self.ov.editing:
            return
        if self._start is None:
            self.setCursor(Qt.SizeFDiagCursor if self._in_grip(e.position().toPoint()) else Qt.SizeAllCursor)
            return
        delta = e.globalPosition().toPoint() - self._start
        g = QRect(self._geom)
        if self._resize:
            g.setWidth(max(40, g.width() + delta.x()))
            g.setHeight(max(30, g.height() + delta.y()))
        else:
            g.translate(delta)
        self.setGeometry(self.ov.clamp(g))

    def mouseReleaseEvent(self, e):
        self._start = None

    def wheelEvent(self, e):
        if not self.ov.editing:
            return
        f = 1.06 if e.angleDelta().y() > 0 else 1 / 1.06
        g = self.geometry()
        ng = QRect(0, 0, max(40, int(g.width() * f)), max(30, int(g.height() * f)))
        ng.moveCenter(g.center())
        self.setGeometry(self.ov.clamp(ng))


class GaugeWidget(HudWidget):
    def __init__(self, key, title, unit, fmt, ov):
        super().__init__(key, title, ov)
        self.unit, self.fmt = unit, fmt

    def draw(self, p, r):
        head = QRectF(r.left() + 4, r.top() + 2, r.width() - 8, r.height() * 0.30)
        val = QRectF(r.left() + 4, r.top() + r.height() * 0.30, r.width() - 8, r.height() * 0.66)
        fit_text(p, f"{self.title}, {self.unit}" if self.unit else self.title, head, C_DIM)
        try:
            txt = self.fmt(self.d)
        except Exception:
            txt = "-"
        base = self.col(C_TXT)
        if self.key == "thr":  # плавное покраснение: 100% -> 110% (малый -> полный форсаж)
            base = mix(base, C_BAD, (_f(self.d.get("thr_avg", 0)) - 1.0) / 0.10)
        elif self.key == "g":  # зоны перегрузки: приближение к цели - зеленеет, дальше - краснеет
            base = g_color(self.d.get("ny", 1.0), base, self.ov.cfg.get("g_zones", {}))
        fit_text(p, txt, val, base)


def apply_dz(v, dz):
    """Мёртвая зона с пересчётом: за её пределами ход снова 0..1."""
    v = max(-1.0, min(1.0, v))
    if abs(v) <= dz:
        return 0.0
    return math.copysign((abs(v) - dz) / (1.0 - dz), v)


class StickWidget(HudWidget):
    def draw(self, p, r):
        rad = max(3.0, min(min(r.width(), r.height()) * 0.02, 8.0))  # точка маленькая, не растёт с окном
        side = min(r.width(), r.height()) - 2 * (rad + 2)
        cxm, cym = r.center().x(), r.center().y()
        ox, oy = cxm - side / 2, cym - side / 2
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(C_LINE, 2))
        p.drawRect(QRectF(ox, oy, side, side))
        p.setPen(QPen(QColor(120, 200, 255, 90), 1))
        p.drawLine(QPointF(ox, cym), QPointF(ox + side, cym))
        p.drawLine(QPointF(cxm, oy), QPointF(cxm, oy + side))
        dz = max(0.0, min(0.5, _f(self.ov.cfg.get("stick_deadzone", 0.0))))
        if dz > 0:  # мёртвая зона - пунктирный квадрат в центре
            half = side / 2 * dz
            p.setPen(QPen(QColor(255, 255, 255, 80), 1, Qt.DashLine))
            p.drawRect(QRectF(cxm - half, cym - half, 2 * half, 2 * half))
        if self.ov.cfg.get("stick_source") == "mouse":  # виртуальная ручка: просто следует за мышью
            x, y = apply_dz(self.ov.vx, dz), apply_dz(self.ov.vy, dz)
        else:
            z = self.ov.cfg.get("axis_zero") or [0.0, 0.0, 0.0]
            x = apply_dz(self.d.get("stick_ai", 0) - z[0], dz)
            y = apply_dz(self.d.get("stick_el", 0) - z[1], dz)
        # нулевая точка: неподвижное кольцо в центре
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 170), 1.5))
        p.drawEllipse(QPointF(cxm, cym), rad + 3, rad + 3)
        # граница - по центру точки: до края доходит её центр
        c = self.col(C_OK)
        p.setPen(QPen(QColor(c.red(), c.green(), c.blue(), 190), 1))
        p.setBrush(QColor(c.red(), c.green(), c.blue(), 110))
        p.drawEllipse(QPointF(cxm + x * side / 2, cym + y * side / 2), rad, rad)


class PedalsWidget(HudWidget):
    def draw(self, p, r):
        h, w = r.height(), r.width()
        top, bar_h = r.top() + h * 0.24, h * 0.40
        mid = r.center().x()
        z = self.ov.cfg.get("axis_zero") or [0.0, 0.0, 0.0]
        v = max(-1.0, min(1.0, self.d.get("pedals", 0) - z[2]))
        fit_text(p, "ПЕДАЛИ", QRectF(r.left(), r.top() + 2, w, h * 0.20), C_DIM)
        p.setPen(Qt.NoPen)
        p.setBrush(C_BAR)
        p.drawRect(QRectF(r.left() + 4, top, w - 8, bar_h))
        x = mid + v * (w / 2 - 6)
        if abs(x - mid) >= 1:
            p.setBrush(self.col(C_WARN))
            p.drawRect(QRectF(min(mid, x), top, abs(x - mid), bar_h))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(C_LINE, 2))
        p.drawRect(QRectF(r.left() + 4, top, w - 8, bar_h))
        p.setPen(QPen(QColor(255, 255, 255, 230), 2))
        p.drawLine(QPointF(mid, top - 3), QPointF(mid, top + bar_h + 3))
        fit_text(p, f"{v:+.2f}", QRectF(r.left(), top + bar_h + 2, w, h * 0.30), C_TXT)


class ThrottleWidget(HudWidget):
    VMAX = 1.1

    def draw(self, p, r):
        w, h = r.width(), r.height()
        top, area_h = r.top() + h * 0.11, h * 0.62
        x0, bw = r.left() + 8, w - 16
        bot = top + area_h
        v = max(0.0, min(self.VMAX, self.d.get("thr_avg", 0)))
        yv = bot - v / self.VMAX * area_h
        y100 = bot - 1.0 / self.VMAX * area_h
        fit_text(p, "РУД", QRectF(r.left(), r.top() + 2, w, h * 0.09), C_DIM)
        p.setPen(Qt.NoPen)
        p.setBrush(C_BAR)
        p.drawRect(QRectF(x0, top, bw, area_h))
        p.setBrush(self.col(QColor(80, 180, 255)))
        fy = max(yv, y100)
        p.drawRect(QRectF(x0, fy, bw, bot - fy))
        if v > 1.0:
            p.setBrush(QColor(255, 120, 40))
            p.drawRect(QRectF(x0, yv, bw, y100 - yv))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(C_LINE, 2))
        p.drawRect(QRectF(x0, top, bw, area_h))
        p.setPen(QPen(QColor(255, 120, 40, 230), 2))
        p.drawLine(QPointF(x0 - 3, y100), QPointF(x0 + bw + 3, y100))
        fit_text(p, f"{v * 100:.0f}%", QRectF(r.left(), bot + 3, w, h * 0.13), C_TXT)
        if v > 1.005:
            fit_text(p, "ФОРСАЖ", QRectF(r.left(), bot + h * 0.15, w, h * 0.09), QColor(255, 140, 60))


class GearWidget(HudWidget):
    """Три лампы стойки шасси: серые - убраны, жёлтые - в пути, зелёные - выпущены."""

    def draw(self, p, r):
        g = max(0.0, min(1.0, self.d.get("gear", 0)))
        if g <= 0.02:
            col, txt, fill = C_DIM, "УБРАНО", False
        elif g >= 0.98:
            col, txt, fill = self.col(C_OK), "ВЫПУЩЕНО", True
        else:
            col, txt, fill = C_WARN, "В ПУТИ", True
        area = QRectF(r.left(), r.top() + 2, r.width(), r.height() * 0.64)
        s = min(area.width() / 3.4, area.height() / 2.3)
        cx = area.center().x()
        pts = [QPointF(cx, area.top() + s * 0.65),
               QPointF(cx - s * 1.35, area.bottom() - s * 0.65),
               QPointF(cx + s * 1.35, area.bottom() - s * 0.65)]
        for pt in pts:
            p.setPen(QPen(col, max(2.0, s * 0.10)))
            p.setBrush(QBrush(col) if fill else QBrush(C_BAR))
            p.drawEllipse(pt, s * 0.55, s * 0.55)
        fit_text(p, f"ШАССИ {txt}", QRectF(r.left() + 2, r.top() + r.height() * 0.68, r.width() - 4, r.height() * 0.28), col)


class FlapsWidget(HudWidget):
    """Профиль крыла: закрылок отклоняется вниз пропорционально положению."""

    def draw(self, p, r):
        pct = max(0.0, min(100.0, self.d.get("flaps", 0)))
        area = QRectF(r.left() + 10, r.top() + 4, r.width() - 20, r.height() * 0.62)
        th = max(5.0, area.height() * 0.20)
        cy = area.center().y() - area.height() * 0.15
        hinge_x = area.left() + area.width() * 0.62
        base = self.col(C_ACC)
        col = mix(base, C_WARN, pct / 100.0)
        p.setPen(Qt.NoPen)
        p.setBrush(base)
        p.drawRoundedRect(QRectF(area.left(), cy - th / 2, hinge_x - area.left(), th), th / 2, th / 2)
        flap_len = area.right() - hinge_x
        p.setPen(QPen(QColor(255, 255, 255, 50), 1, Qt.DashLine))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(hinge_x, cy - th / 2, flap_len, th))
        p.save()
        p.translate(hinge_x, cy)
        p.rotate(pct / 100.0 * 45.0)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(QRectF(0, -th / 2, flap_len, th), th / 3, th / 3)
        p.restore()
        fit_text(p, f"ЗАКРЫЛКИ {pct:.0f}%", QRectF(r.left() + 2, r.top() + r.height() * 0.68, r.width() - 4, r.height() * 0.28),
                 col if pct > 1 else C_DIM)


class AirbrakeWidget(HudWidget):
    """Фюзеляж + щиток, поднимающийся вверх."""

    def draw(self, p, r):
        v = max(0.0, min(1.0, self.d.get("airbrake", 0)))
        area = QRectF(r.left() + 10, r.top() + 4, r.width() - 20, r.height() * 0.62)
        th = max(6.0, area.height() * 0.26)
        cy = area.bottom() - th * 0.9
        base = self.col(C_ACC)
        p.setPen(Qt.NoPen)
        p.setBrush(base)
        p.drawRoundedRect(QRectF(area.left(), cy - th / 2, area.width(), th), th / 2, th / 2)
        hinge_x = area.left() + area.width() * 0.42
        plate = area.width() * 0.30
        col = mix(base, C_BAD, v)
        p.save()
        p.translate(hinge_x, cy - th / 2)
        p.rotate(-v * 70.0)
        p.setBrush(col)
        p.drawRoundedRect(QRectF(0, -th * 0.18, plate, th * 0.36), th * 0.15, th * 0.15)
        p.restore()
        fit_text(p, f"ТОРМОЗ {v * 100:.0f}%", QRectF(r.left() + 2, r.top() + r.height() * 0.68, r.width() - 4, r.height() * 0.28),
                 col if v > 0.01 else C_DIM)


class AoaWidget(HudWidget):
    """Вертикальная шкала угла атаки с маркером."""
    LO, HI = -10.0, 30.0

    def draw(self, p, r):
        a = _f(self.d.get("aoa", 0))
        col = self.col(C_ACC)
        w, h = r.width(), r.height()
        fit_text(p, "УГОЛ АТАКИ", QRectF(r.left(), r.top() + 2, w, h * 0.08), C_DIM)
        top, bot = r.top() + h * 0.14, r.bottom() - h * 0.20
        x = r.left() + w * 0.34

        def y_of(v):
            t = (max(self.LO, min(self.HI, v)) - self.LO) / (self.HI - self.LO)
            return bot - t * (bot - top)

        p.setPen(QPen(C_LINE, 2))
        p.drawLine(QPointF(x, top), QPointF(x, bot))
        for v in range(int(self.LO), int(self.HI) + 1, 10):
            yy = y_of(v)
            p.setPen(QPen(C_LINE, 2))
            p.drawLine(QPointF(x - 6, yy), QPointF(x + 6, yy))
            fit_text(p, f"{v}", QRectF(x + 10, yy - h * 0.04, w * 0.45, h * 0.08), C_DIM, False,
                     Qt.AlignLeft | Qt.AlignVCenter)
        ym, s = y_of(a), max(6.0, min(w * 0.16, 14.0))
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawPolygon([QPointF(x - 4, ym), QPointF(x - 4 - s, ym - s * 0.6), QPointF(x - 4 - s, ym + s * 0.6)])
        fit_text(p, f"{a:.1f}°", QRectF(r.left(), bot + h * 0.06, w, h * 0.12), col)


class SmokeWidget(HudWidget):
    """Дым вкл/выкл. Работает, если игра отдаёт такое поле в API (ищем по имени 'smoke')."""

    def draw(self, p, r):
        v = self.d.get("smoke_val")
        if v is None:
            fit_text(p, "ДЫМ: нет данных", QRectF(r.left() + 4, r.top() + 2, r.width() - 8, r.height() - 4), C_DIM)
            return
        on = bool(_f(v))
        col = self.col(C_OK) if on else C_DIM
        rad = min(r.height() * 0.24, r.width() * 0.12)
        c = QPointF(r.left() + rad + 8, r.center().y())
        p.setPen(QPen(col, 2))
        p.setBrush(QBrush(col) if on else QBrush(C_BAR))
        p.drawEllipse(c, rad, rad)
        fit_text(p, "ДЫМ ВКЛ" if on else "ДЫМ ВЫКЛ",
                 QRectF(c.x() + rad + 6, r.top() + 4, r.right() - c.x() - rad - 10, r.height() - 8), col)


class BombBayWidget(HudWidget):
    """Бомболюк открыт/закрыт. Работает, если игра отдаёт поле с 'bomb' + 'bay'/'door' в названии."""

    def draw(self, p, r):
        v = self.d.get("bay_val")
        if v is None:
            fit_text(p, "БОМБОЛЮК: нет данных", QRectF(r.left() + 4, r.top() + 2, r.width() - 8, r.height() - 4), C_DIM)
            return
        x = _f(v)
        x = x / 100.0 if x > 1.0 else x
        x = max(0.0, min(1.0, x))
        col = C_DIM if x <= 0.02 else self.col(C_WARN)
        txt = "ЗАКРЫТ" if x <= 0.02 else ("ОТКРЫТ" if x >= 0.98 else "ОТКРЫВАЕТСЯ")
        L = min(r.height() * 0.85, r.width() * 0.34)
        cx, cy = r.left() + 6 + L / 2, r.center().y() - L * 0.1
        th = max(3.0, L * 0.12)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        for sgn in (-1, 1):  # две створки: закрыты - горизонтально, открыты - вниз
            p.save()
            p.translate(cx + sgn * L / 2, cy)
            p.rotate(-x * 70.0 * sgn)
            p.drawRoundedRect(QRectF(0 if sgn < 0 else -L / 2, -th / 2, L / 2, th), th / 3, th / 3)
            p.restore()
        fit_text(p, "ЛЮК " + txt, QRectF(cx + L / 2 + 6, r.top() + 4, r.right() - cx - L / 2 - 10, r.height() - 8), col)


class CountersWidget(HudWidget):
    """ЛТЦ и диполи (число). Работает, если игра отдаёт поля 'flare' / 'chaff'."""

    def draw(self, p, r):
        fl, ch = self.d.get("flare_n"), self.d.get("chaff_n")
        if fl is None and ch is None:
            fit_text(p, "ЛТЦ / диполи: нет данных", QRectF(r.left() + 4, r.top() + 2, r.width() - 8, r.height() - 4), C_DIM)
            return
        col = self.col(C_TXT)
        h2 = r.height() / 2
        for n, (name, v) in enumerate((("ЛТЦ", fl), ("ДИПОЛИ", ch))):
            txt = "%s  %s" % (name, "-" if v is None else "%d" % _f(v))
            fit_text(p, txt, QRectF(r.left() + 4, r.top() + n * h2 + 1, r.width() - 8, h2 - 2), col)


# Описание виджетов: ключ, название в меню, фабрика, раскладка по умолчанию, видим по умолчанию
GAUGES = [
    ("ias", "IAS", "км/ч", lambda d: f"{d['ias']:.0f}", px(30, 30, 150, 70), True),
    ("tas", "TAS", "км/ч", lambda d: f"{d['tas']:.0f}", px(190, 30, 150, 70), False),
    ("mach", "Mach", "", lambda d: f"{d['mach']:.2f}", px(350, 30, 150, 70), False),
    ("alt", "Высота", "м", lambda d: f"{d['alt']:.0f}", px(510, 30, 170, 70), True),
    ("vario", "Вариометр", "м/с", lambda d: f"{d['vario']:+.1f}", px(30, 110, 150, 70), False),
    ("aoa", "Угол атаки", "°", lambda d: f"{d['aoa']:.1f}", px(190, 110, 150, 70), False),
    ("g", "Перегрузка", "G", lambda d: f"{d['ny']:.1f}", px(350, 110, 150, 70), False),
    ("compass", "Курс", "°", lambda d: f"{d['compass']:.0f}", px(510, 110, 170, 70), False),
    ("rpm", "RPM", "", lambda d: f"{d['rpm']:.0f}", px(30, 190, 150, 70), False),
    ("power", "Мощность", "л.с.", lambda d: f"{d['power']:.0f}", px(190, 190, 150, 70), False),
    ("fuel", "Топливо", "%", lambda d: f"{d['fuel_pct']:.0f}", px(350, 190, 150, 70), False),
    ("thr", "РУД", "%", lambda d: f"{d['thr_avg'] * 100:.0f}", px(510, 190, 170, 70), False),
]
SPECIAL = [
    ("gear", "Шасси", GearWidget, px(860, 930, 170, 120), True),
    ("flaps", "Закрылки", FlapsWidget, px(1040, 930, 210, 120), True),
    ("airbrake", "Тормоз", AirbrakeWidget, px(1260, 930, 210, 120), True),
    ("stick", "РУС", StickWidget, px(30, 830, 200, 200), True),
    ("pedals", "Педали", PedalsWidget, px(240, 950, 300, 90), True),
    ("throttle", "РУД", ThrottleWidget, px(1800, 690, 90, 330), True),
    ("aoa_ind", "Индикатор УА", AoaWidget, px(700, 30, 100, 230), False),
    ("smoke", "Дым", SmokeWidget, px(1480, 990, 170, 56), False),
    ("cm", "ЛТЦ / диполи", CountersWidget, px(1480, 910, 170, 70), False),
    ("bay", "Бомболюк", BombBayWidget, px(1660, 910, 200, 70), False),
]


# ------------------------------------------------------------ обновления ---
UPDATE_DIR = APP_DIR / "update"   # сюда кладётся скачанное обновление; exe при старте берёт его, если оно новее
MAX_CODE = 2_000_000


def parse_ver(v):
    nums = [int(x) for x in re.findall(r"\d+", str(v))[:4]]
    return tuple(nums + [0] * (4 - len(nums)))


def code_version(text):
    m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else ""


def validate_code(text):
    """Скачанный код: не пустой, компилируется, это SkyDeck и в нём есть версия."""
    if not text or len(text) > MAX_CODE:
        raise ValueError("файл пустой или слишком большой")
    if not code_version(text):
        raise ValueError("в файле нет APP_VERSION")
    if 'APP_NAME = "SkyDeck"' not in text or "def main" not in text:
        raise ValueError("это не файл SkyDeck")
    compile(text, "skydeck.py", "exec")


def relaunch():
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI") and k != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    flags = (0x00000008 | 0x00000200) if IS_WIN else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([sys.executable, "--updated"], env=env, close_fds=True, creationflags=flags, cwd=str(APP_DIR))


class Updater(QObject):
    """Обновление = свежий skydeck.py из репозитория GitHub. Сам exe не меняется и раздаётся лично."""
    checked = Signal(dict)
    progress = Signal(str)
    installed = Signal(bool, str)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def repo(self):
        return str(self.cfg.get("update_repo") or UPDATE_REPO).strip()

    def url(self):
        return "%s/%s/%s/%s" % (RAW_BASE, self.repo(), UPDATE_BRANCH, UPDATE_FILE)

    def check(self):
        threading.Thread(target=lambda: self.checked.emit(self.check_sync()), daemon=True).start()

    def check_sync(self):
        res = {"ok": False, "newer": False, "error": ""}
        if not self.repo():
            res["error"] = "Репозиторий обновлений не задан."
            return res
        try:
            req = urllib.request.Request("%s?t=%d" % (self.url(), int(time.time())),  # t - обход кэша
                                         headers={"User-Agent": "%s/%s" % (APP_NAME, APP_VERSION)})
            with urllib.request.urlopen(req, timeout=8) as r:
                text = r.read(MAX_CODE + 1).decode("utf-8")
            validate_code(text)
        except Exception as e:
            res["error"] = "Не удалось получить обновление: %s" % e
            return res
        ver = code_version(text)
        res.update(ok=True, version=ver, newer=parse_ver(ver) > parse_ver(APP_VERSION), code=text)
        return res

    def install(self, info):
        threading.Thread(target=lambda: self.installed.emit(*self._install(info)), daemon=True).start()

    def _install(self, info):
        if not getattr(sys, "frozen", False):
            return False, "Обновление на лету работает только в собранном exe."
        self.progress.emit("Устанавливаю...")
        return self.save_update(str(info.get("code", "")), UPDATE_DIR)

    def save_update(self, text, dest_dir):
        try:
            validate_code(text)
            dest_dir.mkdir(parents=True, exist_ok=True)
            tmp = dest_dir / "skydeck.py.new"
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, dest_dir / "skydeck.py")
            bad = dest_dir / "skydeck.py.bad"
            if bad.exists():
                bad.unlink()
            return True, "Обновлено до версии %s. Перезапускаю..." % code_version(text)
        except Exception as e:
            return False, "Ошибка обновления: %s" % e


def _rollback_update(path, err):
    """Обновление не запустилось - откладываем его в сторону и пишем причину в лог."""
    try:
        path.replace(path.with_name("skydeck.py.bad"))
        (APP_DIR / "update_error.log").write_text(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + str(err),
                                                  encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------- панель ---
class Panel(QFrame):
    """Панель настройки; тащится за заголовок."""

    def __init__(self, ov):
        super().__init__(ov)
        self.ov = ov
        self.setObjectName("panel")
        self.setStyleSheet(
            "QFrame#panel{background:rgba(14,20,30,238);border:1px solid #3a5a7a;border-radius:8px;}"
            "QLabel{color:#cfe6ff;font:12px Consolas;} QCheckBox{color:#cfe6ff;font:12px Consolas;}"
            "QPushButton{background:#1d2b3d;color:#cfe6ff;border:1px solid #3a5a7a;border-radius:4px;"
            "padding:5px 10px;font:12px Consolas;} QPushButton:hover{background:#27405c;}"
            "QComboBox,QDoubleSpinBox{background:#1d2b3d;color:#cfe6ff;border:1px solid #3a5a7a;"
            "border-radius:4px;padding:2px 4px;font:12px Consolas;}"
            "QComboBox QAbstractItemView{background:#1d2b3d;color:#cfe6ff;selection-background-color:#27405c;}")
        cfg = ov.cfg
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 10)
        root.setSpacing(6)
        self.title = QLabel(APP_TITLE + " - режим настройки  (тащи за заголовок)")
        self.title.setStyleSheet("color:#ffdc5a;font:bold 13px Consolas;")
        root.addWidget(self.title)
        root.addWidget(self._note("Виджет: тянуть - двигать, жёлтый угол - размер, колесо - масштаб, ПКМ - цвет"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(2)
        self.boxes = {}
        for n, (key, title) in enumerate(ov.menu_items):
            cb = QCheckBox(title)
            cb.setChecked(ov.is_visible(key))
            cb.toggled.connect(lambda on, k=key: ov.set_visible(k, on))
            self.boxes[key] = cb
            grid.addWidget(cb, n // 4, n % 4)
        root.addLayout(grid)
        row = QHBoxLayout()
        row.addWidget(QLabel("Прозрачность"))
        sl = QSlider(Qt.Horizontal)
        sl.setRange(30, 100)
        sl.setValue(int(cfg.get("opacity", 0.95) * 100))
        sl.valueChanged.connect(ov.set_opacity)
        row.addWidget(sl, 1)
        root.addLayout(row)
        self.cb_bg = QCheckBox("Тёмная подложка под приборами")
        self.cb_bg.setChecked(bool(cfg.get("bg", True)))
        self.cb_bg.toggled.connect(ov.set_bg)
        self.cb_cap = QCheckBox("Скрыть от записи / OBS")
        self.cb_cap.setChecked(bool(cfg.get("hide_from_capture", True)))
        self.cb_cap.toggled.connect(ov.set_hide_capture)
        root.addWidget(self.cb_bg)
        root.addWidget(self.cb_cap)
        if RtssOsd is not None:
            self.cb_rtss = QCheckBox("RTSS OSD (если игра в эксклюзивном полноэкране)")
            self.cb_rtss.setChecked(bool(cfg.get("rtss", False)))
            self.cb_rtss.toggled.connect(ov.set_rtss)
            root.addWidget(self.cb_rtss)
        self.status = QLabel("...")
        root.addWidget(self.status)
        btns = QHBoxLayout()
        b_reset = QPushButton("Сбросить раскладку")
        b_reset.clicked.connect(ov.reset_layout)
        b_done = QPushButton("Готово")
        b_done.clicked.connect(lambda: ov.set_editing(False))
        b_quit = QPushButton("Выход")
        b_quit.clicked.connect(QApplication.quit)
        for b in (b_reset, b_done, b_quit):
            btns.addWidget(b)
        root.addLayout(btns)
        self.btn_more = QPushButton("Ещё настройки \u25be")
        self.btn_more.clicked.connect(self.toggle_more)
        root.addWidget(self.btn_more)
        self.more = QFrame()
        self._build_more()
        self.more_scroll = QScrollArea()  # доп. настройки прокручиваются, панель не вылезает за экран
        self.more_scroll.setWidget(self.more)
        self.more_scroll.setWidgetResizable(True)
        self.more_scroll.setFrameShape(QFrame.NoFrame)
        self.more_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.more_scroll.setMaximumHeight(max(260, int(ov.height() * 0.40)))
        self.more_scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}"
                                       "QScrollArea > QWidget > QWidget{background:transparent;}")
        self.more_scroll.hide()
        root.addWidget(self.more_scroll)
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        self.msg.setStyleSheet("color:#ffdc5a;font:11px Consolas;")
        root.addWidget(self.msg)
        foot = self._note("%s  v%s" % (APP_TITLE, APP_VERSION))
        foot.setAlignment(Qt.AlignRight)
        root.addWidget(foot)
        self.adjustSize()
        self._drag = None

    # -- помощники --
    def _note(self, text):
        lb = QLabel(text)
        lb.setStyleSheet("color:#8fb8dd;font:11px Consolas;")
        return lb

    def _hdr(self, text):
        lb = QLabel(text)
        lb.setStyleSheet("color:#ffdc5a;font:bold 12px Consolas;border-top:1px solid #2c4660;padding-top:5px;")
        return lb

    def _paint_btn(self, btn, hexcolor):
        btn.setStyleSheet("QPushButton{background:%s;color:#000;border:1px solid #3a5a7a;border-radius:4px;"
                          "padding:5px 10px;font:12px Consolas;}" % hexcolor)

    def toggle_more(self):
        show = not self.more_scroll.isVisible()
        self.more_scroll.setVisible(show)
        self.btn_more.setText("Меньше настроек \u25b4" if show else "Ещё настройки \u25be")
        self.fit()

    def fit(self):
        self.adjustSize()
        self.move(self.ov.clamp(QRect(self.pos(), self.size())).topLeft())

    def set_update_status(self, text, show_install=False, version=""):
        self.upd_status.setText(text)
        self.btn_install.setVisible(bool(show_install))
        if version:
            self.btn_install.setText("Обновить до %s" % version)
        if self.more_scroll.isVisible():
            self.fit()

    # -- доп. настройки --
    def _build_more(self):
        ov, cfg = self.ov, self.ov.cfg
        v = QVBoxLayout(self.more)
        v.setContentsMargins(0, 4, 0, 0)
        v.setSpacing(5)

        v.addWidget(self._hdr("РУС"))
        row = QHBoxLayout()
        row.addWidget(QLabel("Источник"))
        self.cmb = QComboBox()
        self.cmb.addItems(["Из игры (API)", "Мышь (виртуальная ручка)"])
        self.cmb.setCurrentIndex(1 if cfg.get("stick_source") == "mouse" else 0)
        self.cmb.currentIndexChanged.connect(ov.set_stick_source)
        row.addWidget(self.cmb, 1)
        v.addLayout(row)
        self.sens_label = QLabel("Чувствительность мыши: %d%%" % cfg.get("mouse_sens", 100))
        v.addWidget(self.sens_label)
        sens = QSlider(Qt.Horizontal)
        sens.setRange(10, 300)
        sens.setValue(int(cfg.get("mouse_sens", 100)))
        sens.valueChanged.connect(ov.set_mouse_sens)
        v.addWidget(sens)
        cb_inv = QCheckBox("Инвертировать вертикаль (мышь)")
        cb_inv.setChecked(bool(cfg.get("mouse_invert_y", False)))
        cb_inv.toggled.connect(ov.set_mouse_invert)
        cb_ac = QCheckBox("Плавный возврат в центр, когда мышь стоит")
        cb_ac.setChecked(bool(cfg.get("mouse_autocenter", False)))
        cb_ac.toggled.connect(ov.set_mouse_autocenter)
        v.addWidget(cb_inv)
        v.addWidget(cb_ac)
        v.addWidget(self._note("Режим «Мышь»: Ctrl+Shift+C - ручка в центр"))
        self.dz_label = QLabel("Мёртвая зона РУС: %d%%" % round(cfg.get("stick_deadzone", 0.0) * 100))
        v.addWidget(self.dz_label)
        dz = QSlider(Qt.Horizontal)
        dz.setRange(0, 30)
        dz.setValue(round(cfg.get("stick_deadzone", 0.0) * 100))
        dz.valueChanged.connect(ov.set_deadzone)
        v.addWidget(dz)
        zrow = QHBoxLayout()
        b_zero = QPushButton("Обнулить РУС и педали")
        b_zero.setToolTip("Режим API: ручку и педали в центр (в бою) и нажать")
        b_zero.clicked.connect(ov.calibrate_axes)
        b_unzero = QPushButton("Сбросить ноль")
        b_unzero.clicked.connect(ov.clear_axes)
        zrow.addWidget(b_zero)
        zrow.addWidget(b_unzero)
        v.addLayout(zrow)

        v.addWidget(self._hdr("Перегрузка (G): зоны"))
        grow = QHBoxLayout()
        for key, label in (("start", "Начало"), ("target", "Цель"), ("limit", "Предел")):
            grow.addWidget(QLabel(label))
            sb = QDoubleSpinBox()
            sb.setRange(0.0, 15.0)
            sb.setDecimals(1)
            sb.setSingleStep(0.1)
            sb.setValue(float(cfg["g_zones"][key]))
            sb.valueChanged.connect(lambda val, k=key: ov.set_gzone(k, val))
            grow.addWidget(sb)
        v.addLayout(grow)
        crow = QHBoxLayout()
        self.b_gc_good = QPushButton("Цвет цели")
        self.b_gc_bad = QPushButton("Цвет предела")
        self._paint_btn(self.b_gc_good, cfg["g_zones"]["good"])
        self._paint_btn(self.b_gc_bad, cfg["g_zones"]["bad"])
        self.b_gc_good.clicked.connect(lambda: ov.pick_g_color("good"))
        self.b_gc_bad.clicked.connect(lambda: ov.pick_g_color("bad"))
        crow.addWidget(self.b_gc_good)
        crow.addWidget(self.b_gc_bad)
        v.addLayout(crow)
        v.addWidget(self._note("До «Начала» - обычный цвет виджета (ПКМ), к «Цели» зеленеет, к «Пределу» краснеет"))

        v.addWidget(self._hdr("Обновления"))
        v.addWidget(QLabel("Версия: %s" % APP_VERSION))
        self.btn_check = QPushButton("Проверить обновления")
        self.btn_check.clicked.connect(lambda: ov.check_updates(True))
        v.addWidget(self.btn_check)
        self.upd_status = QLabel("")
        self.upd_status.setWordWrap(True)
        self.upd_status.setMaximumWidth(380)
        v.addWidget(self.upd_status)
        self.btn_install = QPushButton("Обновить")
        self.btn_install.clicked.connect(ov.install_update)
        self.btn_install.hide()
        v.addWidget(self.btn_install)
        cb_auto = QCheckBox("Проверять обновления при запуске")
        cb_auto.setChecked(bool(cfg.get("auto_update_check", True)))
        cb_auto.toggled.connect(ov.set_auto_update)
        v.addWidget(cb_auto)

        v.addWidget(self._hdr("Служебное"))
        b_dump = QPushButton("Сохранить дамп API (api_dump.json)")
        b_dump.setToolTip("В бою: сохраняет всё, что отдаёт игра - чтобы найти нужные поля")
        b_dump.clicked.connect(ov.dump_api)
        v.addWidget(b_dump)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.position().y() < 34:
            self._drag = e.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(self.ov.clamp(QRect(e.globalPosition().toPoint() - self._drag, self.size())).topLeft())

    def mouseReleaseEvent(self, e):
        self._drag = None


# --------------------------------------------------------------- оверлей ---
class Overlay(QWidget):
    HK_EDIT, HK_HIDE, HK_CENTER = 1, 2, 3

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.editing = False
        self.hidden_all = False
        self.live = False
        self._miss = 99
        self.data = demo_data()
        self.rtss = RtssOsd() if (RtssOsd and cfg.get("rtss")) else None
        self._rtss_t = 0.0
        self.vx = self.vy = 0.0       # виртуальная ручка (режим «Мышь»)
        self._vdirty, self._vmove = False, 0.0
        self.tray = None
        self.update_info = None
        self._manual_check = False
        self.setWindowTitle(APP_TITLE)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool |
                            Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowOpacity(cfg.get("opacity", 0.95))

        screens = QApplication.screens()
        idx = cfg.get("screen", -1)
        scr = screens[idx] if isinstance(idx, int) and 0 <= idx < len(screens) else QApplication.primaryScreen()
        g = scr.geometry()
        # на 1 px ниже экрана: окно ровно в размер экрана Windows считает «полноэкранным»
        self.setGeometry(g.x(), g.y(), g.width(), g.height() - 1)

        self.widgets, self.menu_items, self.defaults = {}, [], {}
        for key, title, unit, fmt, geom, vis in GAUGES:
            self._add(GaugeWidget(key, title, unit, fmt, self), title, geom, vis)
        for key, title, cls, geom, vis in SPECIAL:
            self._add(cls(key, title, self), title, geom, vis)
        self.apply_layout()
        self.panel = Panel(self)
        self.panel.move(int(self.width() / 2 - self.panel.width() / 2), int(self.height() * 0.28))
        self.panel.hide()

        self.updater = Updater(cfg)
        self.updater.checked.connect(self.on_update_checked, Qt.QueuedConnection)
        self.updater.progress.connect(lambda t: self.panel.set_update_status(t, False), Qt.QueuedConnection)
        self.updater.installed.connect(self.on_update_installed, Qt.QueuedConnection)

        self.demo_timer = QTimer(self)
        self.demo_timer.timeout.connect(self._demo_tick)
        self.demo_timer.start(33)

    def _add(self, w, title, geom, vis):
        self.widgets[w.key] = w
        self.menu_items.append((w.key, title))
        self.defaults[w.key] = (geom, vis)

    # -- раскладка --
    def is_visible(self, key):
        v = self.cfg["visible"].get(key)
        return self.defaults[key][1] if v is None else bool(v)

    def apply_layout(self, reset=False):
        W, H = self.width(), self.height()
        for key, w in self.widgets.items():
            fr = None if reset else self.cfg["layout"].get(key)
            if not (isinstance(fr, (list, tuple)) and len(fr) == 4):
                fr = self.defaults[key][0]
            g = QRect(int(fr[0] * W), int(fr[1] * H), max(40, int(fr[2] * W)), max(30, int(fr[3] * H)))
            w.setGeometry(self.clamp(g))
        self.refresh_visibility()

    def save_layout(self):
        W, H = max(1, self.width()), max(1, self.height())
        for key, w in self.widgets.items():
            g = w.geometry()
            self.cfg["layout"][key] = [round(g.x() / W, 5), round(g.y() / H, 5),
                                       round(g.width() / W, 5), round(g.height() / H, 5)]
        save_cfg(self.cfg)

    def reset_layout(self):
        self.cfg["layout"] = {}
        self.cfg["visible"] = {}
        self.apply_layout(reset=True)
        for key, cb in self.panel.boxes.items():
            cb.blockSignals(True)
            cb.setChecked(self.is_visible(key))
            cb.blockSignals(False)

    def clamp(self, g):
        g = QRect(g)
        g.setWidth(min(g.width(), self.width()))
        g.setHeight(min(g.height(), self.height()))
        g.moveLeft(max(0, min(g.left(), self.width() - g.width())))
        g.moveTop(max(0, min(g.top(), self.height() - g.height())))
        return g

    def set_visible(self, key, on):
        self.cfg["visible"][key] = bool(on)
        self.refresh_visibility()

    def refresh_visibility(self):
        show = (self.live or self.editing) and not self.hidden_all
        for key, w in self.widgets.items():
            w.setVisible(show and self.is_visible(key))

    # -- настройки --
    def set_opacity(self, v):
        self.cfg["opacity"] = v / 100.0
        self.setWindowOpacity(v / 100.0)

    def set_bg(self, on):
        self.cfg["bg"] = bool(on)
        self.update_all()

    def set_hide_capture(self, on):
        self.cfg["hide_from_capture"] = bool(on)
        if IS_WIN:
            win_exclude_capture(int(self.winId()), on)

    def set_rtss(self, on):
        self.cfg["rtss"] = bool(on)
        if on and RtssOsd and self.rtss is None:
            self.rtss = RtssOsd()
        if not on and self.rtss:
            self.rtss.release()

    def update_all(self):
        for w in self.widgets.values():
            w.update()

    def set_deadzone(self, v):
        self.cfg["stick_deadzone"] = v / 100.0
        self.panel.dz_label.setText("Мёртвая зона РУС: %d%%" % v)
        self.update_all()

    def set_color(self, key, value):
        if value:
            self.cfg["colors"][key] = value
        else:
            self.cfg["colors"].pop(key, None)
        self.widgets[key].update()

    def pick_color(self, key):
        w = self.widgets[key]
        old = self.cfg["colors"].get(key)
        dlg = QColorDialog(w.col(C_ACC), self)
        dlg.setWindowTitle("Цвет: " + w.title)
        dlg.setOption(QColorDialog.DontUseNativeDialog, True)
        dlg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        dlg.currentColorChanged.connect(lambda c, k=key: self.set_color(k, c.name()))  # живой предпросмотр
        if dlg.exec():
            self.set_color(key, dlg.currentColor().name())
        else:
            self.set_color(key, old)

    def dump_api(self):
        """Записать сырые /state и /indicators в файл - чтобы увидеть, какие поля есть у игры."""
        raw = getattr(self, "last_raw", None)
        if not raw or (raw[0] is None and raw[1] is None):
            self.panel.msg.setText("Нет данных: игра не запущена или нет боя.")
            return
        path = APP_DIR / "api_dump.json"
        try:
            path.write_text(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                                        "state": raw[0], "indicators": raw[1]},
                                       ensure_ascii=False, indent=2), encoding="utf-8")
            self.panel.msg.setText("Сохранено: " + str(path))
        except Exception as e:
            self.panel.msg.setText("Ошибка записи: %s" % e)

    def calibrate_axes(self):
        """Запомнить текущее положение ручки и педалей как ноль (делать в бою, ручка в центре)."""
        if not self.live:
            self.panel.msg.setText("Обнулить можно только в бою: ручку и педали в центр.")
            return
        d = self.data
        self.cfg["axis_zero"] = [round(_f(d.get("stick_ai")), 4), round(_f(d.get("stick_el")), 4),
                                 round(_f(d.get("pedals")), 4)]
        save_cfg(self.cfg)
        self.panel.msg.setText("Ноль сохранён: %+.3f / %+.3f / %+.3f" % tuple(self.cfg["axis_zero"]))
        self.update_all()

    def clear_axes(self):
        self.cfg["axis_zero"] = [0.0, 0.0, 0.0]
        save_cfg(self.cfg)
        self.panel.msg.setText("Ноль сброшен.")
        self.update_all()

    # -- режимы --
    def set_editing(self, on):
        if on == self.editing:
            return
        self.editing = on
        if self.hidden_all and on:
            self.hidden_all = False
        if IS_WIN:
            win_click_through(int(self.winId()), not on)
        if on:
            self.panel.show()
            self.panel.raise_()
        else:
            self.panel.hide()
            self.save_layout()
        self.refresh_visibility()
        self.update()
        self.update_all()

    def toggle_edit(self):
        self.set_editing(not self.editing)

    def toggle_hidden(self):
        self.hidden_all = not self.hidden_all
        if self.hidden_all and self.editing:
            self.set_editing(False)
        self.refresh_visibility()

    def post_show(self):
        """Вызвать после show(): стили окна и хоткеи нужен готовый hwnd."""
        if IS_WIN:
            hwnd = int(self.winId())
            win_click_through(hwnd, not self.editing)
            ok = win_exclude_capture(hwnd, bool(self.cfg.get("hide_from_capture", True)))
            if not ok and self.cfg.get("hide_from_capture", True):
                print("Не удалось скрыть окно от захвата (нужна Windows 10 2004+).")
            _u32.RegisterHotKey(hwnd, self.HK_EDIT, 0x0002 | 0x0004, 0x45)  # Ctrl+Shift+E
            _u32.RegisterHotKey(hwnd, self.HK_HIDE, 0x0002 | 0x0004, 0x48)  # Ctrl+Shift+H
            _u32.RegisterHotKey(hwnd, self.HK_CENTER, 0x0002 | 0x0004, 0x43)  # Ctrl+Shift+C
            # сырой ввод мыши (только чтение движения, без хуков в игру), приходит и когда окно не в фокусе
            rid = RAWINPUTDEVICE(0x01, 0x02, 0x00000100, hwnd)  # RIDEV_INPUTSINK
            _u32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE))

    def shutdown(self):
        try:
            if IS_WIN:
                hwnd = int(self.winId())
                _u32.UnregisterHotKey(hwnd, self.HK_EDIT)
                _u32.UnregisterHotKey(hwnd, self.HK_HIDE)
                _u32.UnregisterHotKey(hwnd, self.HK_CENTER)
            if self.rtss:
                self.rtss.release()
        except Exception:
            pass
        self.save_layout()

    def nativeEvent(self, eventType, message):
        try:
            if IS_WIN and bytes(eventType) == b"windows_generic_MSG":
                msg = ctypes.wintypes.MSG.from_address(int(message))
                if msg.message == 0x0312:  # WM_HOTKEY
                    if msg.wParam == self.HK_EDIT:
                        self.toggle_edit()
                        return True, 0
                    if msg.wParam == self.HK_HIDE:
                        self.toggle_hidden()
                        return True, 0
                    if msg.wParam == self.HK_CENTER:
                        self.center_stick()
                        return True, 0
                elif msg.message == 0x00FF:  # WM_INPUT
                    self._raw_input(msg.lParam)
                    return False, 0  # DefWindowProc обязан освободить ввод
        except Exception:
            pass
        return super().nativeEvent(eventType, message)

    # -- данные --
    def on_data(self, d):
        if d.get("_raw"):
            self.last_raw = d["_raw"]
        if d.get("in_flight"):
            self._miss, self.data = 0, d
        else:
            self._miss += 1
        live = self._miss < 6  # гистерезис: короткие сбои не гасят приборы
        if live != self.live:
            if live:
                self.vx = self.vy = 0.0  # новый вылет - ручка в центр
            self.live = live
            self.refresh_visibility()
        if self.panel.isVisible():
            state = "в бою" if self.live else ("игра запущена, ждём бой" if d.get("online") else "игра не найдена - демо")
            self.panel.status.setText(f"Связь: {state}   {d.get('_hz', 0):.0f} Гц  {d.get('_ping', 0):.0f} мс")
        if self.live:
            self._push(self.data)
            self._rtss(self.data)

    def _demo_tick(self):
        if self.editing and not self.live:
            self._push(demo_data())
        if self.cfg.get("stick_source") == "mouse":
            if self.cfg.get("mouse_autocenter") and (self.vx or self.vy) and \
                    time.perf_counter() - self._vmove > 0.15:
                self.vx = 0.0 if abs(self.vx) < 0.003 else self.vx * 0.88
                self.vy = 0.0 if abs(self.vy) < 0.003 else self.vy * 0.88
                self._vdirty = True
            if self._vdirty:
                self._vdirty = False
                self.widgets["stick"].update()

    def center_stick(self):
        self.vx = self.vy = 0.0
        self.widgets["stick"].update()

    def on_mouse_delta(self, dx, dy):
        if self.cfg.get("stick_source") != "mouse" or not self.live or self.editing:
            return
        k = float(self.cfg.get("mouse_sens", 100)) / 100.0 / 1500.0  # 1500 отсчётов мыши - полный ход при 100%
        sy = -1.0 if self.cfg.get("mouse_invert_y") else 1.0
        self.vx = max(-1.0, min(1.0, self.vx + dx * k))
        self.vy = max(-1.0, min(1.0, self.vy + dy * k * sy))
        self._vdirty, self._vmove = True, time.perf_counter()

    def _raw_input(self, lparam):
        if self.cfg.get("stick_source") != "mouse" or not self.live or self.editing:
            return
        size = ctypes.c_uint(0)
        if _u32.GetRawInputData(ctypes.c_void_p(lparam), 0x10000003, None, ctypes.byref(size), RAW_HDR) != 0:
            return
        buf = ctypes.create_string_buffer(size.value)
        if _u32.GetRawInputData(ctypes.c_void_p(lparam), 0x10000003, buf, ctypes.byref(size), RAW_HDR) != size.value:
            return
        mv = parse_raw_mouse(buf.raw)
        if mv:
            self.on_mouse_delta(*mv)

    # -- настройки РУС / G / обновлений --
    def set_stick_source(self, idx):
        self.cfg["stick_source"] = "mouse" if idx == 1 else "api"
        self.center_stick()

    def set_mouse_sens(self, v):
        self.cfg["mouse_sens"] = int(v)
        self.panel.sens_label.setText("Чувствительность мыши: %d%%" % v)

    def set_mouse_invert(self, on):
        self.cfg["mouse_invert_y"] = bool(on)

    def set_mouse_autocenter(self, on):
        self.cfg["mouse_autocenter"] = bool(on)

    def set_gzone(self, key, val):
        self.cfg["g_zones"][key] = round(float(val), 2)
        self.widgets["g"].update()

    def pick_g_color(self, key):
        old = self.cfg["g_zones"].get(key)
        res = ask_color(old, self, "Цвет перегрузки", live=lambda h: self._set_g_color(key, h))
        self._set_g_color(key, res or old)

    def _set_g_color(self, key, hexcolor):
        self.cfg["g_zones"][key] = hexcolor
        self.panel._paint_btn(self.panel.b_gc_good if key == "good" else self.panel.b_gc_bad, hexcolor)
        self.widgets["g"].update()

    def set_auto_update(self, on):
        self.cfg["auto_update_check"] = bool(on)

    def check_updates(self, manual=True):
        self._manual_check = manual
        self.panel.set_update_status("Проверяю...", False)
        self.updater.check()

    def on_update_checked(self, info):
        ok_new = bool(info.get("ok") and info.get("newer") and info.get("code"))
        self.update_info = info if ok_new else None
        if not info.get("ok"):
            text = info.get("error", "Ошибка")
        elif info.get("newer"):
            text = "Доступна версия %s (у тебя %s)." % (info.get("version"), APP_VERSION)
        else:
            text = "Установлена последняя версия (%s)." % APP_VERSION
        self.panel.set_update_status(text, ok_new, info.get("version", ""))
        if self.tray and (info.get("newer") or self._manual_check):
            self.tray.showMessage(APP_TITLE, text, QSystemTrayIcon.Information, 5000)
        self._manual_check = False

    def install_update(self):
        if self.update_info:
            self.panel.set_update_status("Устанавливаю...", False)
            self.updater.install(self.update_info)

    def on_update_installed(self, ok, msg):
        self.panel.set_update_status(msg, False)
        if ok:
            self.save_layout()
            relaunch()
            QTimer.singleShot(300, QApplication.quit)

    def _push(self, d):
        for key, w in self.widgets.items():
            if w.isVisible():
                w.d = d
                w.update()

    def _rtss(self, d):
        if self.rtss and format_osd and self.cfg.get("rtss"):
            now = time.perf_counter()
            if now - self._rtss_t >= 0.05:
                self._rtss_t = now
                self.rtss.update(format_osd(d))

    def paintEvent(self, e):
        if self.editing:
            p = QPainter(self)
            p.fillRect(self.rect(), QColor(0, 0, 0, 80))  # почти прозрачный фон ловит клики в режиме настройки


def make_icon():
    pm = QPixmap(32, 32)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(20, 40, 60))
    p.setPen(QPen(C_ACC, 2))
    p.drawEllipse(3, 3, 26, 26)
    p.setPen(QPen(C_OK, 3))
    p.drawLine(9, 20, 16, 9)
    p.drawLine(16, 9, 23, 20)
    p.end()
    return QIcon(pm)


def main():
    if "--updated" in sys.argv:
        time.sleep(1.5)  # дать старому экземпляру закрыться и освободить хоткеи
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    cfg = load_cfg()
    first_run = not cfg["layout"]
    ov = Overlay(cfg)
    ov.show()
    ov.post_show()

    tray = QSystemTrayIcon(make_icon(), app)
    tray.setToolTip(APP_TITLE)
    ov.tray = tray
    menu = QMenu()
    a_edit = QAction("Режим настройки  (Ctrl+Shift+E)", menu)
    a_edit.triggered.connect(ov.toggle_edit)
    a_hide = QAction("Показать / скрыть  (Ctrl+Shift+H)", menu)
    a_hide.triggered.connect(ov.toggle_hidden)
    a_upd = QAction("Проверить обновления", menu)
    a_upd.triggered.connect(lambda: ov.check_updates(True))
    a_quit = QAction("Выход", menu)
    a_quit.triggered.connect(app.quit)
    for a in (a_edit, a_hide, a_upd, a_quit):
        menu.addAction(a)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: ov.toggle_edit() if reason == QSystemTrayIcon.Trigger else None)
    tray.show()

    poller = WTPoller(cfg)
    poller.updated.connect(ov.on_data, Qt.QueuedConnection)
    poller.start()

    def cleanup():
        poller.stop()
        poller.wait(1500)
        ov.shutdown()
        save_cfg(cfg)

    app.aboutToQuit.connect(cleanup)
    if first_run:
        ov.set_editing(True)
    if cfg.get("auto_update_check", True) and ov.updater.repo():
        QTimer.singleShot(4000, lambda: ov.check_updates(False))
    sys.exit(app.exec())


def launch():
    """Точка входа. В собранном exe: если рядом лежит update\\skydeck.py новее встроенного - запускаем его,
    иначе встроенную версию. Сбойное обновление откладывается в сторону, exe остаётся рабочим."""
    if getattr(sys, "frozen", False):
        path = UPDATE_DIR / "skydeck.py"
        if path.exists():
            mod = None
            try:
                text = path.read_text(encoding="utf-8")
                validate_code(text)
                if parse_ver(code_version(text)) > parse_ver(APP_VERSION):
                    import importlib.util
                    spec = importlib.util.spec_from_file_location("skydeck_update", str(path))
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules["skydeck_update"] = mod
                    spec.loader.exec_module(mod)
                else:
                    path.unlink()  # устарело: встроенная версия уже не старее
            except Exception:
                _rollback_update(path, traceback.format_exc())
                mod = None
            if mod is not None:
                try:
                    mod.main()
                except Exception:
                    _rollback_update(path, traceback.format_exc())
                    relaunch()
                sys.exit(0)
    main()


if __name__ == "__main__":
    launch()
