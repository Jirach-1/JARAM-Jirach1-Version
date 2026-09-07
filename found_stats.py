from __future__ import annotations

import json
import os
import time
import sys
import threading
import faulthandler
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from biomes import biome_meta, biome_names


FOUND_STATS_DEBUG = str(os.environ.get("JARAM_FOUND_STATS_DEBUG", "")).strip().lower() in {"1", "true", "yes", "on"}


class FoundStatsMixin:
    def _found_stats_path(self) -> Path:
        try:
            return Path(self.config_manager.config_dir) / "found_stats.json"
        except Exception:
            return Path("found_stats.json")

    def _load_found_stats_from_disk(self) -> dict:
        stats = {"biomes_total": {}, "merchants_total": {}, "biome_events": [], "merchant_events": []}
        p = self._found_stats_path()
        try:
            if not p.exists():
                return stats
            raw = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            return stats
        if not isinstance(raw, dict):
            return stats

        bt = raw.get("biomes_total")
        if isinstance(bt, dict):
            for k, v in bt.items():
                if not isinstance(k, str):
                    continue
                b = k.strip().upper()
                if not b:
                    continue
                try:
                    stats["biomes_total"][b] = int(v)
                except Exception:
                    continue

        mt = raw.get("merchants_total")
        if isinstance(mt, dict):
            for k, v in mt.items():
                if not isinstance(k, str):
                    continue
                m = k.strip().title()
                if not m:
                    continue
                try:
                    stats["merchants_total"][m] = int(v)
                except Exception:
                    continue

        evs = raw.get("biome_events")
        if isinstance(evs, list):
            stats["biome_events"] = evs
        mevs = raw.get("merchant_events")
        if isinstance(mevs, list):
            stats["merchant_events"] = mevs

        return stats

    def _get_found_stats_snapshot(self) -> dict:
        # Prefer live MultiScope (most up-to-date), fall back to persisted file.
        try:
            wt = getattr(self, "worker_thread", None)
            ms = getattr(wt, "ms", None) if wt else None
            if ms:
                return ms.get_found_stats_snapshot()
        except Exception:
            pass

        stats = self._load_found_stats_from_disk()
        bt = stats.get("biomes_total", {})
        mt = stats.get("merchants_total", {})
        try:
            b_total = sum(int(v) for v in bt.values())
        except Exception:
            b_total = 0
        try:
            m_total = sum(int(v) for v in mt.values())
        except Exception:
            m_total = 0
        return {
            "biomes_total": bt,
            "merchants_total": mt,
            "biomes_total_count": b_total,
            "merchants_total_count": m_total,
        }

    def _refresh_extras_found_counters(self) -> None:
        act = getattr(self, "_extras_found_counter_action", None)
        if not act:
            return
        try:
            snap = self._get_found_stats_snapshot()
            b = int(snap.get("biomes_total_count", 0) or 0)
            m = int(snap.get("merchants_total_count", 0) or 0)
            act.setText(f"All-time found: Biomes {b} | Merchants {m}")
        except Exception:
            pass

    def show_found_stats_window(self) -> None:
        dlg = QDialog(self)
        
        # --- Hitch watchdog (prints stacks when GUI thread stalls) ---
        if FOUND_STATS_DEBUG:
            try:
                faulthandler.enable()
            except Exception:
                pass

            dlg._ui_heartbeat = time.perf_counter()
            dlg._hitch_watchdog_stop = False
            dlg._hitch_watchdog_last_dump = 0.0

            def _stop_hitch_watchdog(*_):
                try:
                    dlg._hitch_watchdog_stop = True
                except Exception:
                    pass

            try:
                dlg.finished.connect(_stop_hitch_watchdog)
            except Exception:
                pass

            def _hitch_watchdog():
                # Runs off the GUI thread. If the GUI thread doesn't "heartbeat" for a while,
                # dump thread stacks (including main thread) so we can see what's blocking.
                while True:
                    try:
                        if getattr(dlg, "_hitch_watchdog_stop", True):
                            return
                        time.sleep(0.05)

                        if not dlg.isVisible() or dlg.isMinimized():
                            continue

                        hb = float(getattr(dlg, "_ui_heartbeat", 0.0))
                        gap_ms = (time.perf_counter() - hb) * 1000.0
                        if gap_ms >= 120.0:  # tune threshold: 80..200
                            nowp = time.perf_counter()
                            last = float(getattr(dlg, "_hitch_watchdog_last_dump", 0.0))
                            if (nowp - last) >= 1.0:  # throttle dumps
                                dlg._hitch_watchdog_last_dump = nowp
                                print(f"[UIHitchDump] gap={gap_ms:.1f}ms dumping stacks...")
                                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
                    except Exception:
                        # Never let the watchdog crash the app.
                        continue

            dlg._hitch_watchdog_thread = threading.Thread(target=_hitch_watchdog, daemon=True)
            dlg._hitch_watchdog_thread.start()

        dlg.setWindowTitle("Found Stats")
        dlg.resize(720, 520)
        layout = QVBoxLayout(dlg)

        top = QHBoxLayout()
        top.addWidget(QLabel("Time Range:"))
        range_combo = QComboBox()
        range_combo.addItem("All Time", None)
        range_combo.addItem("Last 24 Hours", 24 * 3600)
        range_combo.addItem("Last 7 Days", 7 * 24 * 3600)
        range_combo.addItem("Last 30 Days", 30 * 24 * 3600)
        top.addWidget(range_combo)
        top.addStretch()
        refresh_btn = QPushButton("Refresh")
        top.addWidget(refresh_btn)
        layout.addLayout(top)

        tabs = QTabWidget()
        layout.addWidget(tabs)

        biome_tab = QWidget()
        biome_layout = QVBoxLayout(biome_tab)
        biome_total_lbl = QLabel("")
        biome_layout.addWidget(biome_total_lbl)
        biome_table = QTableWidget(0, 2)
        biome_table.setHorizontalHeaderLabels(["Biome", "Count"])
        biome_table.verticalHeader().setVisible(False)
        biome_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        biome_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        biome_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        bheader = biome_table.horizontalHeader()
        bheader.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        bheader.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        biome_table.setColumnWidth(1, 180)
        biome_layout.addWidget(biome_table)
        tabs.addTab(biome_tab, "Biomes")

        merch_tab = QWidget()
        merch_layout = QVBoxLayout(merch_tab)
        merch_total_lbl = QLabel("")
        merch_layout.addWidget(merch_total_lbl)
        merch_table = QTableWidget(0, 2)
        merch_table.setHorizontalHeaderLabels(["Merchant", "Count"])
        merch_table.verticalHeader().setVisible(False)
        merch_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        merch_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        merch_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        mheader = merch_table.horizontalHeader()
        mheader.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        mheader.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        merch_table.setColumnWidth(1, 180)
        merch_layout.addWidget(merch_table)
        tabs.addTab(merch_tab, "Merchants")

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject)
        layout.addWidget(bb)

        def _counts_from_disk(window_seconds: Optional[float]) -> tuple[dict, dict]:
            stats = self._load_found_stats_from_disk()
            biome_counts: Dict[str, int] = {}
            merchant_counts: Dict[str, int] = {}
            if window_seconds is None:
                bt = stats.get("biomes_total") if isinstance(stats.get("biomes_total"), dict) else {}
                mt = stats.get("merchants_total") if isinstance(stats.get("merchants_total"), dict) else {}
                for k, v in bt.items():
                    if not isinstance(k, str):
                        continue
                    try:
                        biome_counts[k.strip().upper()] = int(v)
                    except Exception:
                        continue
                for k, v in mt.items():
                    if not isinstance(k, str):
                        continue
                    try:
                        merchant_counts[k.strip().title()] = int(v)
                    except Exception:
                        continue
                return biome_counts, merchant_counts

            try:
                now_ts = time.time()
                cutoff = now_ts - float(window_seconds)
            except Exception:
                cutoff = 0.0

            evs = stats.get("biome_events", []) if isinstance(stats, dict) else []
            for ev in (evs or []):
                if not isinstance(ev, dict):
                    continue
                try:
                    ts = float(ev.get("ts", 0))
                except Exception:
                    continue
                if ts < cutoff:
                    continue
                b = ev.get("biome")
                if not isinstance(b, str):
                    continue
                biome = b.strip().upper()
                if not biome or biome == "NORMAL":
                    continue
                biome_counts[biome] = biome_counts.get(biome, 0) + 1

            mevs = stats.get("merchant_events", []) if isinstance(stats, dict) else []
            for ev in (mevs or []):
                if not isinstance(ev, dict):
                    continue
                try:
                    ts = float(ev.get("ts", 0))
                except Exception:
                    continue
                if ts < cutoff:
                    continue
                m = ev.get("merchant")
                if not isinstance(m, str):
                    continue
                merch = m.strip().title()
                if not merch:
                    continue
                merchant_counts[merch] = merchant_counts.get(merch, 0) + 1

            return biome_counts, merchant_counts

        def _get_counts(window_seconds: Optional[float]) -> tuple[dict, int, dict, int]:
            if window_seconds is None:
                snap = self._get_found_stats_snapshot()
                bt = snap.get("biomes_total") if isinstance(snap.get("biomes_total"), dict) else {}
                mt = snap.get("merchants_total") if isinstance(snap.get("merchants_total"), dict) else {}
                b_total = int(snap.get("biomes_total_count", 0) or 0)
                m_total = int(snap.get("merchants_total_count", 0) or 0)
                return bt, b_total, mt, m_total

            try:
                wt = getattr(self, "worker_thread", None)
                ms = getattr(wt, "ms", None) if wt else None
                if ms:
                    bout = ms.get_biomes_found_counts(window_seconds)
                    mout = ms.get_merchants_found_counts(window_seconds)
                    bcounts = bout.get("counts", {}) if isinstance(bout, dict) else {}
                    mcounts = mout.get("counts", {}) if isinstance(mout, dict) else {}
                    btotal = int(bout.get("total", 0) or 0) if isinstance(bout, dict) else 0
                    mtotal = int(mout.get("total", 0) or 0) if isinstance(mout, dict) else 0
                    return bcounts, btotal, mcounts, mtotal
            except Exception:
                pass

            bcounts, mcounts = _counts_from_disk(window_seconds)
            try:
                btotal = sum(int(v) for v in (bcounts or {}).values())
            except Exception:
                btotal = 0
            try:
                mtotal = sum(int(v) for v in (mcounts or {}).values())
            except Exception:
                mtotal = 0
            return bcounts, btotal, mcounts, mtotal

        # Cached + allocation-light for animation hot paths.
        import zlib
        from functools import lru_cache

        @lru_cache(maxsize=65536)
        def _u32_from_str(s: str) -> int:
            try:
                return int(zlib.crc32(s.encode("utf-8")) & 0xFFFFFFFF)
            except Exception:
                return 0

        def _stable_u32(text: object) -> int:
            try:
                return _u32_from_str(str(text))
            except Exception:
                return 0

        def _mix_u32(x: int) -> int:
            # Fast bit-mix (Murmur3-ish finalizer).
            x &= 0xFFFFFFFF
            x ^= (x >> 16) & 0xFFFFFFFF
            x = (x * 0x7FEB352D) & 0xFFFFFFFF
            x ^= (x >> 15) & 0xFFFFFFFF
            x = (x * 0x846CA68B) & 0xFFFFFFFF
            x ^= (x >> 16) & 0xFFFFFFFF
            return int(x & 0xFFFFFFFF)

        # Pre-hash effect prefixes so paint() can stay allocation-free.
        P_DROPX = _stable_u32("dropx")
        P_DROPSP = _stable_u32("dropsp")
        P_DROPY = _stable_u32("dropy")
        P_DROPL = _stable_u32("dropl")

        P_SNA = _stable_u32("sna")
        P_SNX = _stable_u32("snx")
        P_SNSP = _stable_u32("snsp")
        P_SNY = _stable_u32("sny")
        P_SND = _stable_u32("snd")
        P_SNR = _stable_u32("snr")

        P_SDSP = _stable_u32("sdsp")
        P_SDX = _stable_u32("sdx")
        P_SDY = _stable_u32("sdy")

        P_EX = _stable_u32("ex")
        P_ESP = _stable_u32("esp")
        P_EY = _stable_u32("ey")
        P_EL = _stable_u32("el")

        P_HSX = _stable_u32("hsx")
        P_HSS = _stable_u32("hss")
        P_HSY = _stable_u32("hsy")
        P_HSD = _stable_u32("hsd")
        P_HSL = _stable_u32("hsl")
        P_HZX = _stable_u32("hzx")
        P_HZY = _stable_u32("hzy")
        P_HZSP = _stable_u32("hzsp")
        P_HZR = _stable_u32("hzr")
        P_HZA = _stable_u32("hza")

        P_BRX = _stable_u32("brx")
        P_BRW = _stable_u32("brw")
        P_BRA = _stable_u32("bra")
        P_INCX = _stable_u32("incx")
        P_INCY = _stable_u32("incy")
        P_INCSP = _stable_u32("incsp")
        P_INCL = _stable_u32("incl")
        P_INCJ = _stable_u32("incj")

        P_STSPX = _stable_u32("stspx")
        P_STSPY = _stable_u32("stspy")
        P_STX = _stable_u32("stx")
        P_STY = _stable_u32("sty")
        P_STSZ = _stable_u32("stsz")
        P_STGA = _stable_u32("stga")
        P_STGR = _stable_u32("stgr")

        P_CSP = _stable_u32("csp")
        P_CY = _stable_u32("cy")
        P_CX = _stable_u32("cx")

        P_GX = _stable_u32("gx")
        P_GY = _stable_u32("gy")
        P_GR = _stable_u32("gr")
        P_GRH = _stable_u32("grh")

        P_COY = _stable_u32("coY")
        P_COX1 = _stable_u32("coX1")
        P_COX2 = _stable_u32("coX2")

        P_WSP = _stable_u32("wsp")
        P_WX = _stable_u32("wx")
        P_WY = _stable_u32("wy")
        P_WL = _stable_u32("wl")

        P_DX = _stable_u32("dx")
        P_DY = _stable_u32("dy")
        P_DR = _stable_u32("dr")

        P_HPS = _stable_u32("hps")
        P_HPO = _stable_u32("hpo")
        P_HCX = _stable_u32("hcx")
        P_HW0 = _stable_u32("hw0")
        P_HH0 = _stable_u32("hh0")
        P_HW1 = _stable_u32("hw1")
        P_HH1 = _stable_u32("hh1")
        P_HSPX = _stable_u32("hspx")
        P_HSPY = _stable_u32("hspy")

        P_GPW = _stable_u32("gpw")
        P_GPH = _stable_u32("gph")
        P_GBY = _stable_u32("gby")
        P_GBH = _stable_u32("gbh")
        P_GBX = _stable_u32("gbx")
        P_GBW = _stable_u32("gbw")

        P_PX = _stable_u32("px")
        P_PSP = _stable_u32("psp")
        P_PY = _stable_u32("py")

        P_NX = _stable_u32("nx")
        P_NSP = _stable_u32("nsp")
        P_NY = _stable_u32("ny")
        P_EGX = _stable_u32("egx")
        P_EGY = _stable_u32("egy")
        P_EGSP = _stable_u32("egsp")
        P_EGD = _stable_u32("egd")
        P_EGW = _stable_u32("egw")
        P_EGH = _stable_u32("egh")
        P_EGC = _stable_u32("egc")
        P_EGS = _stable_u32("egs")
        P_SGSP = _stable_u32("sgsp")
        P_SGR = _stable_u32("sgr")
        P_SGPH = _stable_u32("sgph")

        P_M = _stable_u32("m")
        P_RS = _stable_u32("rs")
        P_RO = _stable_u32("ro")
        P_SA = _stable_u32("sa")
        P_BS = _stable_u32("bs")
        P_BO = _stable_u32("bo")
        P_HB = _stable_u32("hb")
        P_HP = _stable_u32("hp")

        P_BIT = _stable_u32("bit")
        P_SWAP = _stable_u32("swap")
        P_JIT = _stable_u32("jitter")
        P_GUST = _stable_u32("gust")
        P_COR = _stable_u32("cor")
        P_NUL = _stable_u32("nul")
        P_RGB = _stable_u32("rgb")
        P_GLITCH_PX = _stable_u32("glitch_px")
        P_BAR = _stable_u32("bar")

        P_MSPD = _stable_u32("mspd")
        P_WP_E = _stable_u32("wp:e")
        P_WP_A = _stable_u32("wp:a")
        P_WP_B = _stable_u32("wp:b")
        P_WP_S = _stable_u32("wp:s")
        P_PO = _stable_u32("po")
        P_ST = _stable_u32("st")

        def _clamp01(x: float) -> float:
            try:
                v = float(x)
            except Exception:
                return 0.0
            if v < 0.0:
                return 0.0
            if v > 1.0:
                return 1.0
            return v

        def _qcolor_from_int(color_int: int) -> QColor:
            v = int(color_int) & 0xFFFFFF
            return QColor((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF)

        from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem
        from PySide6.QtGui import QFontMetrics, QPalette
        import math

        class _AnimatedBiomeNameDelegate(QStyledItemDelegate):
            def __init__(self, parent=None):
                super().__init__(parent)
                self._t0 = time.time()

                # --- Reuse objects to avoid per-frame GC / stutter ---
                # Points (for drawText / drawLine / drawEllipse)
                self._pt = QPointF()
                self._p0 = QPointF()
                self._p1 = QPointF()

                # Colors reused inside per-glyph loop
                self._col = QColor()
                self._glow = QColor()
                self._trail = QColor()
                self._drip = QColor()
                self._flame = QColor()
                self._spark = QColor()

                # Constant “glitch split” colors (no need to re-create per glyph)
                self._glitch_r = QColor(255, 60, 60, 170)
                self._glitch_g = QColor(60, 255, 120, 170)
                self._glitch_b = QColor(70, 120, 255, 170)

                # Constant “rainy splash” brush color
                self._splash = QColor(190, 220, 255, 70)

            def paint(self, painter, option, index) -> None:
                t0_perf = time.perf_counter() if FOUND_STATS_DEBUG else 0.0
                try:
                    opt = QStyleOptionViewItem(option)
                    self.initStyleOption(opt, index)
                except Exception:
                    return super().paint(painter, option, index)

                raw_text = str(opt.text or "")
                biome_raw = index.data(Qt.ItemDataRole.UserRole)
                biome_key = (
                    str(biome_raw).strip().upper()
                    if isinstance(biome_raw, str) and str(biome_raw).strip()
                    else raw_text.strip().upper()
                )
                if not biome_key or biome_key == "NORMAL":
                    return super().paint(painter, option, index)

                # Draw default item background/selection without text, then custom-paint animated text.
                text_palette = QPalette(opt.palette)
                opt.text = ""
                try:
                    opt.features &= ~QStyleOptionViewItem.ViewItemFeature.HasDisplay
                except Exception:
                    pass
                try:
                    opt.palette.setColor(QPalette.ColorRole.Text, QColor(0, 0, 0, 0))
                    opt.palette.setColor(QPalette.ColorRole.HighlightedText, QColor(0, 0, 0, 0))
                except Exception:
                    pass
                super().paint(painter, opt, index)

                widget = opt.widget
                cell_rect = opt.rect
                try:
                    style = widget.style() if widget else QApplication.style()
                    text_rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, widget)
                except Exception:
                    text_rect = cell_rect

                painter.save()
                # Clip to the full cell to avoid cutting off animated glyph/glow pixels.
                painter.setClipRect(cell_rect)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
                painter.setFont(opt.font)

                fm = QFontMetrics(opt.font)
                try:
                    max_w = max(0, int(text_rect.width()))
                except Exception:
                    max_w = 0
                text = fm.elidedText(biome_key, Qt.TextElideMode.ElideRight, max_w)
                if not text:
                    painter.restore()
                    return

                try:
                    color_int, _thumb = biome_meta(biome_key)
                except Exception:
                    color_int = 0x3BA55D
                base = _qcolor_from_int(int(color_int or 0x3BA55D))
                seed = _stable_u32(biome_key)

                hue0 = base.hueF()
                if hue0 < 0:
                    hue0 = (seed % 360) / 360.0
                sat0 = max(0.15, float(base.saturationF()))
                val0 = max(0.20, float(base.valueF()))

                try:
                    if opt.state & QStyle.StateFlag.State_Selected:
                        ht = text_palette.color(QPalette.ColorRole.HighlightedText)
                        base = QColor(
                            int(base.red() * 0.45 + ht.red() * 0.55),
                            int(base.green() * 0.45 + ht.green() * 0.55),
                            int(base.blue() * 0.45 + ht.blue() * 0.55),
                        )
                        h2 = base.hueF()
                        if h2 >= 0:
                            hue0 = float(h2)
                        sat0 = max(0.15, float(base.saturationF()))
                        val0 = max(0.20, float(base.valueF()))
                except Exception:
                    pass

                # Effect profiles (creative liberties): biome-themed animations.
                is_windy = biome_key == "WINDY"
                is_rainy = biome_key == "RAINY"
                is_snowy = biome_key == "SNOWY"
                is_sand = biome_key == "SAND STORM"
                is_hell = biome_key == "HELL"
                is_starfall = biome_key == "STARFALL"
                is_heaven = biome_key == "HEAVEN"
                is_corruption = biome_key == "CORRUPTION"
                is_null = biome_key == "NULL"
                is_cyber = biome_key == "CYBERSPACE"
                is_glitch = biome_key == "GLITCHED"
                is_dream = biome_key == "DREAMSPACE"
                is_blazing = biome_key == "BLAZING SUN"
                is_incinerator = biome_key == "INCINERATOR"
                is_grave = biome_key == "GRAVEYARD"
                is_pumpkin = biome_key == "PUMPKIN MOON"
                is_blood = biome_key == "BLOOD RAIN"
                is_aurora = biome_key == "AURORA"
                is_eggland = biome_key == "EGGLAND"
                is_singularity = biome_key == "SINGULARITY"

                is_void = is_null or is_corruption
                is_celestial = is_starfall or is_aurora or is_heaven

                def _u32(p: int, a: int = 0, b: int = 0, c: int = 0) -> int:
                    x = (int(seed) ^ int(p)) & 0xFFFFFFFF
                    x ^= (int(a) * 0x9E3779B1) & 0xFFFFFFFF
                    x ^= (int(b) * 0x85EBCA77) & 0xFFFFFFFF
                    x ^= (int(c) * 0xC2B2AE3D) & 0xFFFFFFFF
                    return _mix_u32(x)

                def _r01(p: int, a: int = 0, b: int = 0, c: int = 0) -> float:
                    return float(_u32(p, a, b, c) & 0xFFFF) / 65535.0

                t = float(time.time() - self._t0)
                phase0 = (seed % 1000) / 1000.0 * (math.pi * 2.0)

                # Default motion/visual parameters (then overridden per biome theme).
                speed = 2.8 + ((seed >> 1) % 100) / 70.0
                amp = 2.0 + ((seed >> 9) % 100) / 120.0
                hue_amp = 0.05
                shimmer = 0.55
                glow_alpha = 90
                alpha_main = 255

                if is_windy:
                    speed *= 1.25
                    amp *= 1.55
                    hue_amp = 0.07
                    shimmer = 0.55
                    glow_alpha = 80
                elif is_rainy:
                    speed *= 0.95
                    amp *= 1.00
                    hue_amp = 0.025
                    shimmer = 0.35
                    glow_alpha = 70
                elif is_snowy:
                    speed *= 0.75
                    amp *= 0.90
                    hue_amp = 0.02
                    shimmer = 0.40
                    glow_alpha = 75
                elif is_sand:
                    speed *= 1.35
                    amp *= 1.20
                    hue_amp = 0.03
                    shimmer = 0.45
                    glow_alpha = 70
                elif is_hell:
                    speed *= 1.90
                    amp *= 1.10
                    hue_amp = 0.04
                    shimmer = 0.75
                    glow_alpha = 110
                elif is_blazing:
                    speed *= 1.55
                    amp *= 1.05
                    hue_amp = 0.06
                    shimmer = 1.00
                    glow_alpha = 140
                elif is_incinerator:
                    # Blazing Sun pushed into a furnace-overload profile.
                    speed *= 2.35
                    amp *= 1.45
                    hue_amp = 0.035
                    shimmer = 1.40
                    glow_alpha = 195
                elif is_starfall:
                    speed *= 0.90
                    amp *= 1.00
                    hue_amp = 0.11
                    shimmer = 0.95
                    glow_alpha = 120
                elif is_heaven:
                    speed *= 0.70
                    amp *= 0.85
                    hue_amp = 0.06
                    shimmer = 0.85
                    glow_alpha = 120
                elif is_aurora:
                    speed *= 0.72
                    amp *= 0.95
                    hue_amp = 0.14
                    shimmer = 1.00
                    glow_alpha = 125
                elif is_eggland:
                    speed *= 0.86
                    amp *= 1.05
                    hue_amp = 0.08
                    shimmer = 0.95
                    glow_alpha = 120
                elif is_singularity:
                    speed *= 1.18
                    amp *= 1.18
                    hue_amp = 0.15
                    shimmer = 1.05
                    glow_alpha = 140
                elif is_cyber:
                    speed *= 1.10
                    amp *= 0.85
                    hue_amp = 0.12
                    shimmer = 0.80
                    glow_alpha = 110
                elif is_dream:
                    speed *= 0.90
                    amp *= 0.95
                    hue_amp = 0.09
                    shimmer = 0.80
                    glow_alpha = 105
                elif is_grave:
                    speed *= 0.80
                    amp *= 0.95
                    hue_amp = 0.02
                    shimmer = 0.45
                    glow_alpha = 70
                elif is_pumpkin:
                    speed *= 1.15
                    amp *= 1.05
                    hue_amp = 0.07
                    shimmer = 0.75
                    glow_alpha = 95
                elif is_blood:
                    speed *= 0.95
                    amp *= 0.95
                    hue_amp = 0.015
                    shimmer = 0.55
                    glow_alpha = 90
                elif is_corruption:
                    speed *= 1.10
                    amp *= 1.05
                    hue_amp = 0.10
                    shimmer = 0.55
                    glow_alpha = 90
                elif is_null:
                    speed *= 0.65
                    amp *= 0.80
                    hue_amp = 0.01
                    shimmer = 0.30
                    glow_alpha = 55
                    alpha_main = 220

                if is_glitch:
                    speed *= 2.60
                    amp *= 1.35
                    hue_amp = max(hue_amp, 0.14)
                    shimmer = max(shimmer, 0.75)
                    glow_alpha = max(glow_alpha, 130)

                rx0 = float(cell_rect.x())
                ry0 = float(cell_rect.y())
                rw0 = max(1.0, float(cell_rect.width()))
                rh0 = max(1.0, float(cell_rect.height()))
                rbottom = ry0 + rh0

                # --- Background "atmosphere" per biome (behind the text) --------
                try:
                    if is_rainy or is_blood:
                        col = QColor(base)
                        if is_blood:
                            col = QColor(170, 20, 20)
                        col.setAlpha(95 if is_rainy else 130)
                        painter.setPen(col)
                        drops = 22 if is_rainy else 16
                        for k in range(drops):
                            x0 = rx0 + rw0 * _r01(P_DROPX, k)
                            sp = (35.0 if is_rainy else 28.0) + (55.0 * _r01(P_DROPSP, k))
                            y0 = (t * sp + (rh0 + 20.0) * _r01(P_DROPY, k)) % (rh0 + 18.0) - 14.0
                            ln = (7.0 if is_rainy else 10.0) + (10.0 * _r01(P_DROPL, k))
                            painter.drawLine(QPointF(x0, ry0 + y0), QPointF(x0, ry0 + y0 + ln))
                            if is_blood and (k % 3) == 0:
                                painter.drawLine(QPointF(x0 + 1.0, ry0 + y0), QPointF(x0 + 1.0, ry0 + y0 + ln))
                            if is_rainy and (k % 5) == 0:
                                painter.drawLine(QPointF(x0 - 1.0, ry0 + y0 + ln), QPointF(x0 + 2.0, ry0 + y0 + ln + 1.0))

                    if is_snowy:
                        flake = QColor(230, 245, 255)
                        painter.setPen(QColor(0, 0, 0, 0))
                        for k in range(18):
                            flake.setAlpha(70 + int(110 * _r01(P_SNA, k)))
                            painter.setBrush(flake)
                            x0 = rx0 + rw0 * _r01(P_SNX, k)
                            sp = 10.0 + 18.0 * _r01(P_SNSP, k)
                            y0 = (t * sp + (rh0 + 16.0) * _r01(P_SNY, k)) % (rh0 + 16.0) - 8.0
                            drift = math.sin(t * 0.85 + k * 1.7 + phase0) * (3.0 + 2.0 * _r01(P_SND, k))
                            rad = 1.2 + 2.2 * _r01(P_SNR, k)
                            painter.drawEllipse(QPointF(x0 + drift, ry0 + y0), rad, rad)
                        painter.setBrush(Qt.BrushStyle.NoBrush)

                    if is_sand:
                        dust = QColor(210, 180, 120, 95)
                        painter.setPen(dust)
                        for k in range(30):
                            sp = 45.0 + 60.0 * _r01(P_SDSP, k)
                            x0 = (rx0 + (t * sp) + (rw0 + 30.0) * _r01(P_SDX, k)) % (rw0 + 30.0) - 15.0
                            y0 = ry0 + rh0 * _r01(P_SDY, k) + math.sin(t * 2.2 + k + phase0) * 1.8
                            painter.drawPoint(QPointF(x0, y0))
                            if (k % 3) == 0:
                                painter.drawLine(QPointF(x0, y0), QPointF(x0 + 7.5, y0 + 1.1))

                    if is_hell or is_blazing or is_incinerator:
                        if is_incinerator:
                            ember = QColor(255, 105, 24, 235)
                            ember_count = 36
                        else:
                            ember = QColor(255, 160, 40, 185 if is_hell else 200)
                            ember_count = 22 if is_hell else 15
                        painter.setPen(ember)
                        for k in range(ember_count):
                            x0 = rx0 + rw0 * _r01(P_EX, k)
                            if is_incinerator:
                                sp = 32.0 + 78.0 * _r01(P_ESP, k)
                            else:
                                sp = (18.0 if is_hell else 14.0) + 40.0 * _r01(P_ESP, k)
                            y0 = rbottom - ((t * sp + (rh0 + 20.0) * _r01(P_EY, k)) % (rh0 + 20.0))
                            painter.drawPoint(QPointF(x0, y0))
                            streak_mod = 2 if is_incinerator else 3
                            if (k % streak_mod) == 0:
                                extra_len = 5.0 if is_incinerator else 0.0
                                painter.drawLine(
                                    QPointF(x0, y0),
                                    QPointF(x0, y0 - (4.0 + extra_len + 9.0 * _r01(P_EL, k))),
                                )

                        if is_hell:
                            # A restrained crimson haze drifting behind Hell's sparks.
                            painter.setPen(QColor(0, 0, 0, 0))
                            haze = QColor(150, 18, 10)
                            for k in range(5):
                                travel = rw0 + 70.0
                                haze_p = (t * (0.035 + 0.030 * _r01(P_HZSP, k)) + _r01(P_HZX, k)) % 1.0
                                haze_x = rx0 - 35.0 + haze_p * travel
                                haze_y = ry0 + rh0 * (0.30 + 0.48 * _r01(P_HZY, k))
                                haze_y += math.sin(t * 0.42 + k * 1.35 + phase0) * 1.2
                                haze_rx = 24.0 + 18.0 * _r01(P_HZR, k)
                                haze_ry = 5.0 + 3.5 * _r01(P_HZR, k, 1)
                                haze.setAlpha(12 + int(12.0 * _r01(P_HZA, k)))
                                painter.setBrush(haze)
                                painter.drawEllipse(QPointF(haze_x, haze_y), haze_rx, haze_ry)
                            painter.setBrush(Qt.BrushStyle.NoBrush)

                            # Extra sparks for HELL.
                            spark = QColor(255, 80, 20, 150)
                            painter.setPen(spark)
                            for k in range(18):
                                x0 = rx0 + rw0 * _r01(P_HSX, k)
                                sp = 24.0 + 70.0 * _r01(P_HSS, k)
                                y0 = rbottom - ((t * sp + (rh0 + 28.0) * _r01(P_HSY, k)) % (rh0 + 28.0))
                                drift = math.sin(t * 1.9 + k * 1.2 + phase0) * (1.0 + 1.4 * _r01(P_HSD, k))
                                ln = 3.0 + 11.0 * _r01(P_HSL, k)
                                painter.drawLine(QPointF(x0 + drift, y0), QPointF(x0 + drift, y0 - ln))

                        if is_blazing or is_incinerator:
                            # Blazing sunlight; Incinerator turns it into dense furnace beams.
                            ray_count = 12 if is_incinerator else 8
                            for k in range(ray_count):
                                x_center = rx0 + rw0 * _r01(P_BRX, k)
                                ray_drift = 9.0 if is_incinerator else 6.0
                                x_center += math.sin(t * (0.62 if is_incinerator else 0.35) + k * 0.9 + phase0) * ray_drift
                                if is_incinerator:
                                    w = 7.0 + 18.0 * _r01(P_BRW, k)
                                else:
                                    w = 10.0 + 22.0 * _r01(P_BRW, k)

                                if is_incinerator:
                                    blast = abs(math.sin(t * 2.15 + k * 0.72 + phase0))
                                    a0 = 42 + int(82.0 * _r01(P_BRA, k) + 54.0 * blast)
                                else:
                                    a0 = 18 + int(55.0 * _r01(P_BRA, k) + 30.0 * abs(math.sin(t * 0.85 + k + phase0)))
                                a1 = max(0, int(a0 * 0.45))
                                a2 = max(0, int(a0 * 0.18))

                                h1 = int(rh0 * 0.45)
                                h2 = int(rh0 * 0.35)
                                h3 = max(0, int(rh0) - (h1 + h2))

                                xl = x_center - (w * 0.50)
                                if is_incinerator:
                                    col0 = QColor(255, 105, 30, a0)
                                    col1 = QColor(240, 45, 8, a1)
                                    col2 = QColor(150, 8, 0, a2)
                                else:
                                    col0 = QColor(255, 220, 120, a0)
                                    col1 = QColor(255, 200, 90, a1)
                                    col2 = QColor(255, 180, 60, a2)
                                if h1 > 0:
                                    painter.fillRect(QRect(int(xl), int(ry0), int(w), h1), col0)
                                if h2 > 0:
                                    painter.fillRect(QRect(int(xl), int(ry0) + h1, int(w), h2), col1)
                                if h3 > 0:
                                    painter.fillRect(QRect(int(xl), int(ry0) + h1 + h2, int(w), h3), col2)

                                cw = max(2.0, w * 0.30)
                                cxl = x_center - (cw * 0.50)
                                if is_incinerator:
                                    cc0 = QColor(255, 246, 195, int(a0 * 0.78))
                                    cc1 = QColor(255, 175, 70, int(a1 * 0.72))
                                    cc2 = QColor(255, 75, 20, int(a2 * 0.65))
                                else:
                                    cc0 = QColor(255, 245, 190, int(a0 * 0.55))
                                    cc1 = QColor(255, 230, 150, int(a1 * 0.55))
                                    cc2 = QColor(255, 210, 120, int(a2 * 0.55))
                                if h1 > 0:
                                    painter.fillRect(QRect(int(cxl), int(ry0), int(cw), h1), cc0)
                                if h2 > 0:
                                    painter.fillRect(QRect(int(cxl), int(ry0) + h1, int(cw), h2), cc1)
                                if h3 > 0:
                                    painter.fillRect(QRect(int(cxl), int(ry0) + h1 + h2, int(cw), h3), cc2)

                        if is_incinerator:
                            # Fast white-hot ejecta and rising blast fronts sell the overload.
                            for k in range(18):
                                x0 = rx0 + rw0 * _r01(P_INCX, k)
                                sp = 54.0 + 105.0 * _r01(P_INCSP, k)
                                y0 = rbottom - ((t * sp + (rh0 + 30.0) * _r01(P_INCY, k)) % (rh0 + 30.0))
                                lean = 1.5 + 5.5 * _r01(P_INCL, k)
                                hot = QColor(255, 242, 188, 155 + int(85.0 * _r01(P_BRA, k)))
                                painter.setPen(hot)
                                painter.drawLine(QPointF(x0, y0), QPointF(x0 + lean, y0 - 3.0 - lean))

                            for k in range(3):
                                p = (t * (0.85 + 0.12 * k) + _r01(P_INCY, k, 91)) % 1.0
                                y0 = rbottom - p * (rh0 + 4.0)
                                fade = 1.0 - p
                                band = QColor(255, 70 + 35 * k, 15, int(75.0 * fade))
                                painter.fillRect(QRect(int(rx0), int(y0), int(rw0), 1 + (k % 2)), band)

                    if is_starfall:
                        star = QColor(220, 235, 255, 155)
                        painter.setPen(star)
                        for k in range(22):
                            spx = 28.0 + 38.0 * _r01(P_STSPX, k)
                            spy = 10.0 + 18.0 * _r01(P_STSPY, k)
                            x0 = rx0 + ((t * spx + (rw0 + 60.0) * _r01(P_STX, k)) % (rw0 + 60.0) - 30.0)
                            y0 = ry0 + ((t * spy + (rh0 + 30.0) * _r01(P_STY, k)) % (rh0 + 30.0) - 15.0)
                            sz = 2.2 + 2.3 * _r01(P_STSZ, k)
                            painter.drawLine(QPointF(x0 - sz, y0), QPointF(x0 + sz, y0))
                            painter.drawLine(QPointF(x0, y0 - sz), QPointF(x0, y0 + sz))
                            if (k % 2) == 0:
                                glow = QColor(200, 235, 255, 45 + int(60 * _r01(P_STGA, k)))
                                painter.setPen(QColor(0, 0, 0, 0))
                                painter.setBrush(glow)
                                rr = 1.8 + 3.6 * _r01(P_STGR, k)
                                painter.drawEllipse(QPointF(x0, y0), rr, rr)
                                painter.setBrush(Qt.BrushStyle.NoBrush)
                                painter.setPen(star)
                            if (k % 5) == 0:
                                painter.drawPoint(QPointF(x0 + 1.0, y0 + 1.0))

                    if is_aurora:
                        segs = 20
                        prev = None
                        for s in range(segs):
                            fx = rx0 + rw0 * (float(s) / float(segs - 1))
                            fy = ry0 + (rh0 * 0.50) + math.sin(t * 0.80 + s * 0.70 + phase0) * (rh0 * 0.22)
                            band = (float(s) / float(max(1, segs - 1))) - 0.5
                            h = hue0 + (band * 0.06) + (0.03 * math.sin(t * 0.65 + s * 0.55 + phase0))
                            if h < 0.0:
                                h += 1.0
                            elif h > 1.0:
                                h -= 1.0
                            c = QColor.fromHsvF(h, 0.88, 1.0)
                            c.setAlpha(60)
                            painter.setPen(c)
                            if prev is not None:
                                painter.drawLine(prev, QPointF(fx, fy))
                                painter.drawLine(QPointF(prev.x(), prev.y() + 1.0), QPointF(fx, fy + 1.0))
                            prev = QPointF(fx, fy)

                    if is_cyber:
                        scan = int((t * 80.0 + float(seed % 1000)) % max(1.0, rh0))
                        scan_y = int(ry0) + scan
                        glow = QColor(90, 210, 255, 85)
                        painter.fillRect(QRect(int(rx0), int(scan_y), int(rw0), 3), glow)
                        painter.fillRect(QRect(int(rx0), int(scan_y) + 6, int(rw0), 2), QColor(90, 210, 255, 40))
                        # Digital rain (0/1).
                        text_font = painter.font()
                        digit_font = painter.font()
                        try:
                            if float(digit_font.pointSizeF()) > 0:
                                digit_font.setPointSizeF(max(6.0, float(digit_font.pointSizeF()) * 0.78))
                        except Exception:
                            pass
                        try:
                            digit_font.setBold(True)
                        except Exception:
                            pass
                        painter.setFont(digit_font)

                        for k in range(18):
                            den = rh0 + 26.0
                            sp = 28.0 + 36.0 * _r01(P_CSP, k)
                            yraw = (t * sp + den * _r01(P_CY, k)) % den
                            fall = float(yraw) / float(max(1.0, den))

                            x0 = rx0 + rw0 * _r01(P_CX, k)
                            x0 = rx0 + round((x0 - rx0) / 8.0) * 8.0
                            y0 = ry0 + yraw - 10.0

                            frame = int(t * 10.0)
                            b = _u32(P_BIT, frame, k)
                            head = "01"[int(b & 1)]
                            t1 = "01"[int((b >> 1) & 1)]

                            head_alpha = int(40 + 150.0 * (1.0 - fall) * (1.0 - fall))
                            head_col = QColor(90, 210, 255, max(0, min(255, head_alpha)))
                            trail_col = QColor(90, 210, 255, max(0, min(255, int(head_alpha * 0.45))))

                            painter.setPen(head_col)
                            painter.drawText(QPointF(x0, y0), head)
                            painter.setPen(trail_col)
                            painter.drawText(QPointF(x0, y0 - 9.0), t1)

                        painter.setFont(text_font)

                    if is_grave:
                        mist = QColor(130, 160, 130, 55)
                        painter.setPen(QColor(0, 0, 0, 0))
                        painter.setBrush(mist)
                        for k in range(6):
                            x0 = rx0 + rw0 * _r01(P_GX, k)
                            y0 = ry0 + rh0 * (0.70 + 0.22 * _r01(P_GY, k)) + math.sin(t * 0.7 + k + phase0) * 1.2
                            painter.drawEllipse(QPointF(x0, y0), 18.0 + 10.0 * _r01(P_GR, k), 6.0 + 3.0 * _r01(P_GRH, k))
                        painter.setBrush(Qt.BrushStyle.NoBrush)

                    if is_corruption:
                        frame = int(t * 7.0)
                        if (frame % 2) == 0:
                            scratch = QColor(160, 60, 210, 95)
                            painter.setPen(scratch)
                            for k in range(6):
                                y0 = ry0 + rh0 * _r01(P_COY, frame, k)
                                x1 = rx0 + rw0 * _r01(P_COX1, frame, k)
                                x2 = min(rx0 + rw0, x1 + (20.0 + 60.0 * _r01(P_COX2, frame, k)))
                                painter.drawLine(QPointF(x1, y0), QPointF(x2, y0))

                    if is_windy:
                        gust = QColor(base)
                        gust.setAlpha(90)
                        painter.setPen(gust)
                        for k in range(16):
                            sp = 55.0 + 75.0 * _r01(P_WSP, k)
                            x0 = (rx0 + (t * sp) + (rw0 + 60.0) * _r01(P_WX, k)) % (rw0 + 60.0) - 30.0
                            y0 = ry0 + rh0 * _r01(P_WY, k) + math.sin(t * 1.6 + k + phase0) * 1.2
                            ln = 12.0 + 22.0 * _r01(P_WL, k)
                            bend = math.sin(t * 2.0 + k * 1.3 + phase0) * 2.2
                            painter.drawLine(QPointF(x0, y0), QPointF(x0 + ln, y0 + bend))
                            if (k % 4) == 0:
                                painter.drawLine(QPointF(x0 + ln * 0.35, y0 + bend * 0.35), QPointF(x0 + ln * 0.35 + 4.0, y0 + bend * 0.35 - 1.0))

                    if is_dream:
                        painter.setPen(QColor(0, 0, 0, 0))
                        for k in range(18):
                            tw = 0.5 + 0.5 * math.sin(t * 1.20 + k * 2.10 + phase0)
                            h = hue0 + 0.03 * math.sin(t * 0.55 + k + phase0)
                            if h < 0.0:
                                h += 1.0
                            elif h > 1.0:
                                h -= 1.0
                            c = QColor.fromHsvF(h, 0.35, 1.0)
                            c.setAlpha(45 + int(95 * tw))
                            painter.setBrush(c)
                            x0 = rx0 + rw0 * _r01(P_DX, k)
                            y0 = ry0 + rh0 * _r01(P_DY, k) + math.sin(t * 0.6 + k + phase0) * 1.5
                            r = 0.9 + 1.8 * _r01(P_DR, k)
                            painter.drawEllipse(QPointF(x0, y0), r, r)
                        painter.setBrush(Qt.BrushStyle.NoBrush)

                    if is_eggland:
                        painter.setPen(QColor(0, 0, 0, 0))
                        egg_palette = (
                            QColor(255, 214, 224, 110),
                            QColor(255, 241, 188, 110),
                            QColor(205, 234, 255, 110),
                            QColor(216, 245, 216, 110),
                        )
                        for k in range(14):
                            egg = QColor(egg_palette[int(_u32(P_EGC, k) % len(egg_palette))])
                            tw = 0.5 + 0.5 * math.sin(t * 1.15 + k + phase0)
                            egg.setAlpha(65 + int(85 * tw))
                            painter.setBrush(egg)
                            x0 = rx0 + rw0 * _r01(P_EGX, k)
                            sp = 8.0 + 14.0 * _r01(P_EGSP, k)
                            y0 = (t * sp + (rh0 + 26.0) * _r01(P_EGY, k)) % (rh0 + 26.0) - 12.0
                            drift = math.sin(t * 0.92 + k * 1.24 + phase0) * (1.8 + 1.7 * _r01(P_EGD, k))
                            ew = 2.0 + 2.5 * _r01(P_EGW, k)
                            eh = 3.2 + 2.8 * _r01(P_EGH, k)
                            cx = x0 + drift
                            cy = ry0 + y0
                            painter.drawEllipse(QPointF(cx, cy), ew, eh)
                            if (k % 3) == 0:
                                stripe = QColor(255, 255, 255, 45 + int(70 * _r01(P_EGS, k)))
                                painter.setBrush(stripe)
                                painter.drawEllipse(QPointF(cx, cy - 0.8), max(0.8, ew * 0.45), max(0.7, eh * 0.22))
                                painter.setBrush(egg)
                        painter.setBrush(Qt.BrushStyle.NoBrush)

                    if is_singularity:
                        core_cx = rx0 + (rw0 * 0.50) + math.sin(t * 0.45 + phase0) * 4.0
                        core_cy = ry0 + (rh0 * 0.50) + math.cos(t * 0.60 + phase0) * 1.5
                        core_rx = max(7.0, rw0 * 0.09)
                        core_ry = max(4.5, rh0 * 0.16)

                        painter.setPen(QColor(0, 0, 0, 0))
                        painter.setBrush(QColor(34, 8, 4, 140))
                        painter.drawEllipse(QPointF(core_cx, core_cy), core_rx, core_ry)
                        painter.setBrush(Qt.BrushStyle.NoBrush)

                        for ring_idx in range(2):
                            ring = QColor(255, 95 + (ring_idx * 35), 45 + (ring_idx * 20), 75 - (ring_idx * 20))
                            painter.setPen(ring)
                            ring_rx = core_rx * (1.65 + ring_idx * 0.55) + math.sin(t * (0.90 + ring_idx * 0.35) + phase0) * 1.2
                            ring_ry = core_ry * (1.30 + ring_idx * 0.35) + math.cos(t * (0.75 + ring_idx * 0.25) + phase0) * 0.8
                            painter.drawEllipse(QPointF(core_cx, core_cy), ring_rx, ring_ry)

                        mote = QColor(255, 170, 95, 90)
                        painter.setPen(mote)
                        for k in range(20):
                            ang = (t * (0.55 + 0.85 * _r01(P_SGSP, k))) + ((math.pi * 2.0) * _r01(P_SGPH, k))
                            orbit = max(6.0, rw0 * (0.12 + 0.35 * _r01(P_SGR, k)))
                            spiral = 0.55 + 0.45 * math.sin(t * 0.75 + k * 0.85 + phase0)
                            ox = math.cos(ang) * orbit * spiral
                            oy = math.sin(ang * 1.28) * max(4.0, orbit * 0.30)
                            x0 = core_cx + ox
                            y0 = core_cy + oy
                            trail_x = core_cx + (ox * 0.72)
                            trail_y = core_cy + (oy * 0.72)
                            painter.drawLine(QPointF(x0, y0), QPointF(trail_x, trail_y))
                            if (k % 4) == 0:
                                painter.drawPoint(QPointF(x0, y0))

                    if is_heaven:
                        # Falling light blocks that pulse outward, then dissolve at the bottom.
                        travel = rh0 + 60.0
                        fade_start = (rh0 + 30.0) / max(1.0, travel)

                        for k in range(10):
                            sp = 0.26 + 0.16 * _r01(P_HPS, k)
                            p = (t * sp + _r01(P_HPO, k)) % 1.0

                            end = 0.0
                            if p > fade_start:
                                end = (p - fade_start) / max(1e-6, (1.0 - fade_start))
                                if end < 0.0:
                                    end = 0.0
                                elif end > 1.0:
                                    end = 1.0
                            fade = 1.0 - end
                            if fade <= 0.001:
                                continue

                            pulse = 0.5 + 0.5 * math.sin(t * 2.20 + k * 1.70 + phase0)
                            grow = (p * p) * 0.65 + (end * end) * (0.65 + 0.35 * pulse)

                            cx = rx0 + rw0 * _r01(P_HCX, k)
                            cy = (ry0 - 30.0) + (p * travel) + (math.sin(t * 0.90 + k * 1.10 + phase0) * 0.7)

                            w0 = 7.0 + 8.0 * _r01(P_HW0, k)
                            h0 = 14.0 + 10.0 * _r01(P_HH0, k)
                            w = w0 + grow * (18.0 + 14.0 * _r01(P_HW1, k))
                            h = h0 + grow * (22.0 + 16.0 * _r01(P_HH1, k))

                            hh = 0.13 + 0.02 * math.sin(t * 0.75 + k * 0.9 + phase0)
                            if hh < 0.0:
                                hh += 1.0
                            elif hh > 1.0:
                                hh -= 1.0

                            a = fade * fade
                            inner = QColor.fromHsvF(hh, 0.22, 1.0)
                            inner.setAlpha(int(190.0 * a))
                            outer = QColor.fromHsvF(hh, 0.18, 1.0)
                            outer.setAlpha(int(125.0 * a))

                            painter.fillRect(
                                QRect(int(cx - (w * 0.50) - 2.0), int(cy - (h * 0.50) - 2.0), int(w + 4.0), int(h + 4.0)),
                                outer,
                            )
                            painter.fillRect(QRect(int(cx - (w * 0.50)), int(cy - (h * 0.50)), int(w), int(h)), inner)

                        painter.setPen(QColor(255, 255, 255, 130))
                        for k in range(18):
                            spx = 18.0 + 24.0 * _r01(P_HSPX, k)
                            spy = 10.0 + 14.0 * _r01(P_HSPY, k)
                            x0 = rx0 + ((t * spx + (rw0 + 30.0) * _r01(P_HSX, k)) % (rw0 + 30.0) - 15.0)
                            y0 = ry0 + ((t * spy + (rh0 + 20.0) * _r01(P_HSY, k)) % (rh0 + 20.0) - 10.0)
                            painter.drawPoint(QPointF(x0, y0))

                    if is_glitch:
                        frame = int(t * 24.0)
                        for k in range(70):
                            gseed = _u32(P_GLITCH_PX, frame, k)
                            x0 = rx0 + rw0 * (float(gseed & 0xFFFF) / 65535.0)
                            y0 = ry0 + rh0 * (float((gseed >> 16) & 0xFFFF) / 65535.0)
                            ci = int((gseed >> 6) % 3)
                            if ci == 0:
                                c = QColor(255, 60, 60, 150)
                            elif ci == 1:
                                c = QColor(70, 120, 255, 150)
                            else:
                                c = QColor(60, 255, 120, 150)
                            w = 1 + int(3 * _r01(P_GPW, k))
                            h = 1 + int(2 * _r01(P_GPH, k))
                            painter.fillRect(QRect(int(x0), int(y0), int(w), int(h)), c)

                        # Horizontal "tear" bars.
                        for b in range(6):
                            if ((frame + b + (seed % 17)) % 2) != 0:
                                continue
                            y0 = ry0 + rh0 * _r01(P_GBY, frame, b)
                            hh = 1 + int(3 * _r01(P_GBH, frame, b))
                            x0 = rx0 + rw0 * (_r01(P_GBX, frame, b) - 0.12)
                            ww = rw0 * (0.55 + 0.55 * _r01(P_GBW, frame, b))
                            bseed = _u32(P_BAR, frame, b)
                            ci = int((bseed >> 5) % 3)
                            if ci == 0:
                                cc = QColor(255, 60, 60, 80)
                            elif ci == 1:
                                cc = QColor(70, 120, 255, 80)
                            else:
                                cc = QColor(60, 255, 120, 80)
                            painter.fillRect(QRect(int(x0), int(y0), int(ww), int(hh)), cc)

                    if is_pumpkin:
                        ember = QColor(255, 140, 50, 150)
                        painter.setPen(ember)
                        for k in range(14):
                            x0 = rx0 + rw0 * _r01(P_PX, k)
                            sp = 10.0 + 18.0 * _r01(P_PSP, k)
                            y0 = rbottom - ((t * sp + (rh0 + 20.0) * _r01(P_PY, k)) % (rh0 + 20.0))
                            painter.drawPoint(QPointF(x0, y0))
                            if (k % 4) == 0:
                                painter.drawLine(QPointF(x0, y0), QPointF(x0, y0 - 4.0))

                    if is_null:
                        mote = QColor(140, 120, 180, 70)
                        painter.setPen(mote)
                        for k in range(12):
                            x0 = rx0 + rw0 * _r01(P_NX, k)
                            sp = 6.0 + 10.0 * _r01(P_NSP, k)
                            y0 = (t * sp + (rh0 + 16.0) * _r01(P_NY, k)) % (rh0 + 16.0) - 8.0
                            painter.drawPoint(QPointF(x0, ry0 + y0))
                except Exception:
                    pass

                # --- Text motion baseline --------------------------------------
                global_dx = math.sin(t * (0.55 + 0.25 * shimmer) + phase0) * (1.0 + 1.0 * shimmer)
                global_dy = math.sin(t * (0.35 + 0.20 * shimmer) + phase0 * 0.7) * (0.7 + 0.7 * shimmer)
                if is_windy:
                    global_dx += math.sin(t * 1.60 + phase0) * 2.0
                    global_dy += math.cos(t * 1.10 + phase0 * 0.8) * 0.8
                if is_rainy or is_blood:
                    global_dy += math.sin(t * 2.20 + phase0) * 0.6
                if is_sand:
                    global_dx += math.sin(t * 1.10 + phase0) * 1.2
                if is_hell or is_blazing or is_incinerator:
                    if is_incinerator:
                        global_dx += math.sin(t * 5.20 + phase0) * 1.55
                        global_dy += math.sin(t * 4.35 + phase0) * 0.85
                    else:
                        global_dx += math.sin(t * 3.20 + phase0) * 0.9
                        global_dy += math.sin(t * 2.70 + phase0) * 0.4
                if is_cyber:
                    global_dx *= 0.35
                    global_dy *= 0.15
                if is_null:
                    global_dx *= 0.20
                    global_dy *= 0.25
                if is_grave:
                    global_dx *= 0.45
                    global_dy *= 0.75
                if is_pumpkin:
                    global_dy += abs(math.sin(t * 1.60 + phase0)) * 0.9
                if is_eggland:
                    global_dx += math.sin(t * 0.95 + phase0) * 0.9
                    global_dy += math.sin(t * 1.20 + phase0) * 0.45
                if is_singularity:
                    global_dx += math.sin(t * 1.35 + phase0) * 0.85
                    global_dy += math.cos(t * 1.05 + phase0) * 0.55
                if is_glitch:
                    # Fast whole-word shake (doesn't change per-letter spacing).
                    global_dx += math.sin(t * 6.20 + phase0) * 0.9
                    global_dy += math.cos(t * 5.70 + phase0) * 0.7

                baseline0 = float(cell_rect.y()) + (float(cell_rect.height()) + fm.ascent() - fm.descent()) / 2.0
                glyph_h = float(fm.ascent() + fm.descent())
                slack = (float(cell_rect.height()) - glyph_h) / 2.0
                max_shift = max(0.0, slack - 2.0)
                try:
                    text_w = float(fm.horizontalAdvance(text))
                except Exception:
                    text_w = 0.0
                tracking = 0.0
                if is_glitch:
                    tracking = 0.8
                effective_w = float(text_w) + (tracking * max(0, len(text) - 1))
                x = float(text_rect.x()) + (float(text_rect.width()) - effective_w) / 2.0 + global_dx

                sweep_speed = 1.4
                if is_cyber:
                    sweep_speed = 2.6
                elif is_incinerator:
                    sweep_speed = 3.35
                elif is_blazing:
                    sweep_speed = 2.2
                elif is_starfall:
                    sweep_speed = 2.0
                elif is_aurora:
                    sweep_speed = 1.0
                elif is_singularity:
                    sweep_speed = 1.8
                sweep_pos = ((t * sweep_speed) + float((seed >> 16) % 13)) % float(len(text) + 10) - 5.0

                scan_y = ry0 + ((t * 70.0 + float(seed % 997)) % rh0)

                shadow = QColor(0, 0, 0, 150)
                if is_void:
                    shadow = QColor(10, 0, 20, 170)
                if is_hell or is_blood or is_incinerator:
                    shadow = QColor(30, 0, 0, 180)
                if is_singularity:
                    shadow = QColor(32, 6, 2, 185)
                if is_cyber:
                    shadow = QColor(0, 20, 20, 170)

                wave_motion = is_windy or is_dream or is_aurora
                
                pt = self._pt
                p0 = self._p0
                p1 = self._p1

                col = self._col
                glow = self._glow
                trail = self._trail
                drip_col = self._drip
                flame_col = self._flame
                spark_col = self._spark

                glitch_r = self._glitch_r
                glitch_g = self._glitch_g
                glitch_b = self._glitch_b
                splash = self._splash

                for i, ch in enumerate(text):
                    w = float(fm.horizontalAdvance(ch))
                    if w <= 0:
                        continue
                    if ch.isspace():
                        x += w + (tracking if i < (len(text) - 1) else 0.0)
                        continue

                    draw_ch = ch
                    if is_glitch:
                        swap_frame = int(t * 9.0)
                        sseed = _u32(P_SWAP, swap_frame, i)
                        if (sseed & 0xFF) < 60:
                            charset = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789#$%&@?/"
                            draw_ch = charset[int((sseed >> 8) % len(charset))]
                            if ((sseed >> 20) & 1) != 0:
                                draw_ch = str(draw_ch).lower()

                    # Phase for subtle sat shimmer (kept consistent regardless of the motion style).
                    phase = (t * speed) + phase0 + (float(i) * 0.12)
                    ph = phase0 + (math.pi * 2.0) * _r01(P_M, i)

                    dx = 0.0
                    dy = 0.0
                    if wave_motion:
                        wave_phase = (t * speed) + (i * 0.62) + phase0
                        dy = math.sin(wave_phase) * amp
                        dx = math.cos(wave_phase * 0.85) * (amp * 0.35)

                    jx = 0.0
                    jy = 0.0

                    if is_glitch:
                        frame = int(t * 30.0)
                        jseed = _u32(P_JIT, frame, i)
                        jx = (((jseed & 0xFF) / 255.0) - 0.5) * 2.0 * 2.6
                        jy = ((((jseed >> 8) & 0xFF) / 255.0) - 0.5) * 2.0 * 2.2
                        dx += jx
                        dy += jy

                    if is_windy:
                        # Keep the classic wavy motion for WINDY.
                        dx += math.sin(t * 1.80 + i * 0.90 + phase0) * 1.5
                        dy += math.cos(t * 1.30 + i * 0.55 + phase0) * 0.9
                    elif is_rainy:
                        # Droplet bob (no wave).
                        p = (t * (0.95 + 0.35 * _r01(P_RS, i)) + _r01(P_RO, i)) % 1.0
                        drop = 0.5 - 0.5 * math.cos((math.pi * 2.0) * p)
                        dy += drop * (amp * 1.05)
                        dx += math.sin(t * 1.40 + ph) * 0.35
                    elif is_snowy:
                        # Floaty drift (no wave).
                        a = 0.55 + 0.75 * _r01(P_SA, i)
                        dx += math.sin(t * 0.55 + ph) * (amp * 0.55 * a)
                        dy += math.sin(t * 0.40 + ph * 0.7) * (amp * 0.35 * a)
                    elif is_sand:
                        # Gusty, granular jitter.
                        frame = int(t * 12.0)
                        jseed = _u32(P_GUST, frame, i)
                        dx += (((jseed & 0xFF) / 255.0) - 0.5) * (amp * 1.6)
                        dy += ((((jseed >> 8) & 0xFF) / 255.0) - 0.5) * (amp * 0.55)
                        dx += math.sin(t * 2.50 + phase0) * 0.7
                    elif is_hell:
                        # Upward flame flicker (no wave).
                        flame = abs(math.sin(t * 6.10 + ph))
                        dy -= flame * (amp * 1.55)
                        dx += math.sin(t * 7.20 + ph) * (amp * 0.22)
                    elif is_blazing:
                        # Heat shimmer + flare (no wave).
                        flare = abs(math.sin(t * 4.20 + ph))
                        dy -= flare * (amp * 1.25)
                        dx += math.sin(t * 6.30 + ph) * (amp * 0.28)
                    elif is_incinerator:
                        # Furnace blast: sharper lift, violent shimmer, and brief thermal jolts.
                        flare = abs(math.sin(t * 7.60 + ph))
                        surge = abs(math.sin(t * 3.10 + phase0))
                        dy -= (flare * amp * 1.85) + (surge * amp * 0.35)
                        dx += math.sin(t * 9.40 + ph) * (amp * 0.38)
                        inc_frame = int(t * 18.0)
                        inc_seed = _u32(P_INCJ, inc_frame, i)
                        dx += (((inc_seed & 0xFF) / 255.0) - 0.5) * (amp * 0.45)
                        dy += ((((inc_seed >> 8) & 0xFF) / 255.0) - 0.5) * (amp * 0.20)
                    elif is_starfall:
                        # Starry twinkle jitter (no wave).
                        dy += math.sin(t * 1.60 + ph) * (amp * 0.35)
                        dx += math.cos(t * 1.10 + ph) * (amp * 0.18)
                    elif is_heaven:
                        # Gentle "breathing" float (no wave).
                        dy += math.sin(t * 0.90 + ph) * (amp * 0.55)
                        dx += math.sin(t * 0.55 + ph) * (amp * 0.25)
                    elif is_aurora:
                        # Keep the wavy motion for AURORA (ribbon-like).
                        dy += math.sin(t * 1.10 + i * 0.22 + phase0) * 1.4
                        dx += math.sin(t * 0.60 + i * 0.18 + phase0) * 0.7
                    elif is_cyber:
                        # Digital stepping (no wave).
                        step = (int(t * 12.0 + i * 0.60 + (seed % 97)) % 3) - 1
                        dx += float(step) * 0.9
                        step2 = (int(t * 8.0 + i * 0.45 + (seed % 71)) % 3) - 1
                        dy += float(step2) * 0.35
                    elif is_dream:
                        # Keep the wavy motion for DREAMSPACE, but floatier.
                        dx += math.sin(t * 1.10 + i * 0.35 + phase0) * 1.1
                        dy += math.sin(t * 0.85 + i * 0.28 + phase0) * 1.2
                    elif is_eggland:
                        # Pastel hop: light buoyant bounce.
                        hop = 0.5 - 0.5 * math.cos(t * 1.85 + ph)
                        dy -= hop * (amp * 0.80)
                        dx += math.sin(t * 1.00 + ph) * (amp * 0.26)
                    elif is_singularity:
                        # Spiral orbit with a mild inward pull toward the center letters.
                        center_offset = float(i) - (float(len(text) - 1) * 0.50)
                        pull = max(0.0, 1.0 - (abs(center_offset) / max(1.0, float(len(text)) * 0.50)))
                        spin = (t * 1.75) + (center_offset * 0.58) + phase0
                        dx += math.sin(spin) * (amp * (0.30 + 0.45 * pull))
                        dy += math.cos(spin * 1.18) * (amp * (0.55 + 0.30 * pull))
                        dx -= center_offset * 0.14 * (0.55 + 0.45 * math.sin(t * 1.20 + ph))
                    elif is_corruption:
                        # Corrupted crawl + occasional snap (no wave).
                        frame = int(t * 10.0)
                        cseed = _u32(P_COR, frame, i)
                        dx += (((cseed & 0xFF) / 255.0) - 0.5) * (amp * 0.85)
                        dy += ((((cseed >> 8) & 0xFF) / 255.0) - 0.5) * (amp * 0.55)
                        if ((frame + i + (seed % 11)) % 23) == 0:
                            dx += (-2.0 if (seed & (1 << (i % 16))) else 2.0)
                    elif is_null:
                        # Almost still: tiny tremble.
                        frame = int(t * 2.0)
                        nseed = _u32(P_NUL, frame, i)
                        dx += (((nseed & 0xFF) / 255.0) - 0.5) * 0.25
                        dy += ((((nseed >> 8) & 0xFF) / 255.0) - 0.5) * 0.18
                    elif is_grave:
                        # Misty drift (no wave).
                        dx += math.sin(t * 0.55 + ph) * (amp * 0.28)
                        dy += math.sin(t * 0.45 + ph) * (amp * 0.55)
                    elif is_pumpkin:
                        # Word bounce (no per-letter wave).
                        bounce = abs(math.sin(t * 2.60 + phase0))
                        dy -= bounce * (amp * 0.95)
                        dx += math.sin(t * 1.10 + phase0) * (amp * 0.22)
                    elif is_blood:
                        # Viscous drip (no wave).
                        p = (t * (0.35 + 0.20 * _r01(P_BS, i)) + _r01(P_BO, i)) % 1.0
                        drop = 0.5 - 0.5 * math.cos((math.pi * 2.0) * p)
                        dy += drop * (amp * 1.20)
                        dx += math.sin(t * 0.65 + ph) * 0.25

                    px = x + dx + (jx * 0.20)
                    total_dy = global_dy + dy
                    if total_dy > max_shift:
                        total_dy = max_shift
                    elif total_dy < -max_shift:
                        total_dy = -max_shift
                    py = baseline0 + total_dy

                    # Color + brightness (theme-specific).
                    try:
                        denom = float(max(1, len(text) - 1))
                        pos = (float(i) / denom) - 0.5
                    except Exception:
                        pos = 0.0

                    hue_bias = (_r01(P_HB, i) - 0.5) * 0.04  # ±0.02
                    hue_grad = 0.0
                    if is_aurora:
                        hue_grad = pos * 0.14
                    elif is_dream:
                        hue_grad = pos * 0.06
                    elif is_windy:
                        hue_grad = pos * 0.06
                    elif is_cyber:
                        hue_grad = pos * 0.08
                    elif is_starfall:
                        hue_grad = pos * 0.05
                    elif is_eggland:
                        hue_grad = pos * 0.09
                    elif is_singularity:
                        hue_grad = pos * 0.10

                    hue_phase = (math.pi * 2.0) * _r01(P_HP, i)
                    hue = hue0 + hue_grad + hue_bias + (hue_amp * math.sin(t * 1.10 + hue_phase + (phase0 * 0.15)))
                    if hue < 0.0:
                        hue += 1.0
                    elif hue > 1.0:
                        hue -= 1.0
                    if is_cyber:
                        hue = 0.55 + (pos * 0.06) + (0.04 * math.sin(t * 1.10 + hue_phase + (phase0 * 0.20)))
                    sat = _clamp01(sat0 * (0.82 + 0.30 * math.sin(phase + 1.1)))
                    val = _clamp01(val0 * (0.78 + 0.58 * math.sin(t * 1.55 + i * 0.23)))

                    if is_snowy:
                        sat = _clamp01(sat * 0.70)
                        val = _clamp01(val * 1.12)
                    if is_grave or is_null:
                        sat = _clamp01(sat * 0.55)
                        val = _clamp01(val * 0.95)
                    if is_corruption:
                        hue = (0.78 + 0.05 * math.sin(t * 1.6 + i * 0.2 + phase0)) % 1.0
                        sat = _clamp01(sat * 0.95)
                        val = _clamp01(val * 0.92)
                    if is_hell or is_blazing or is_incinerator:
                        if is_incinerator:
                            heat = abs(math.sin(t * 3.40 + i * 0.19 + phase0))
                            white_hot = heat * heat * heat
                            hue = (0.008 + 0.105 * heat + (0.004 * i)) % 1.0
                            sat = _clamp01(max(sat, 0.88) * (1.0 - 0.58 * white_hot))
                            val = _clamp01(max(val, 0.68 + 0.45 * heat))
                        else:
                            heat = abs(math.sin(t * (2.4 if is_hell else 2.0) + i * 0.12 + phase0))
                            hue = (0.02 + 0.10 * heat + (0.006 * i)) % 1.0
                            sat = _clamp01(max(sat, 0.75))
                            val = _clamp01(max(val, 0.55 + 0.50 * heat))
                    if is_blood:
                        hue = (0.0 + 0.01 * math.sin(t * 1.1 + i * 0.15 + phase0)) % 1.0
                        sat = _clamp01(max(sat, 0.80))
                        val = _clamp01(val * 0.95)
                    if is_heaven:
                        halo = 0.5 + 0.5 * math.sin(t * 0.9 + phase0)
                        hue = (0.13 + 0.04 * halo + 0.004 * i) % 1.0
                        sat = _clamp01(sat * 0.85)
                        val = _clamp01(val * 1.10)
                    if is_pumpkin:
                        mix = 0.5 + 0.5 * math.sin(t * 1.4 + phase0)
                        hue = hue0 + (0.05 * (mix - 0.5)) + (pos * 0.03)
                        if hue < 0.0:
                            hue += 1.0
                        elif hue > 1.0:
                            hue -= 1.0
                        sat = _clamp01(max(sat, 0.55))
                        val = _clamp01(val * (1.02 + 0.10 * mix))
                    if is_eggland:
                        tone = _r01(P_EGC, i)
                        hue = (0.05 + (0.38 * tone) + (0.04 * math.sin(t * 1.25 + phase0 + i * 0.18))) % 1.0
                        sat = _clamp01(sat * 0.58)
                        val = _clamp01(val * 1.15)
                    if is_singularity:
                        well = 0.5 + 0.5 * math.sin(t * 1.35 + phase0 + i * 0.20)
                        hue = (0.01 + (0.07 * well) + (pos * 0.02)) % 1.0
                        sat = _clamp01(max(sat * 0.95, 0.72))
                        val = _clamp01(max(val * 0.92, 0.55 + 0.38 * well))
                    if is_glitch:
                        # Hard swap between RGB primaries + white (no hue drifting).
                        rgb_frame = int(t * 8.0)
                        pick = int(_u32(P_RGB, rgb_frame, i) % 4)
                        if pick == 3:
                            # White: saturation-free (hue is irrelevant).
                            hue = 0.0
                            sat = 0.0
                            val = 1.0
                        else:
                            hue = (0.0, 2.0 / 3.0, 1.0 / 3.0)[pick]  # red, blue, green
                            sat = _clamp01(max(sat, 0.95))
                            val = _clamp01(max(val, 0.75))

                    # Highlight/sweep (stars, aurora ribbon, cyber scanline, sun flare).
                    hi = 0.0
                    if is_starfall or is_aurora or is_heaven or is_cyber or is_blazing or is_incinerator or is_eggland or is_singularity:
                        radius = 3.0
                        if is_aurora:
                            radius = 4.6
                        elif is_starfall:
                            radius = 3.8
                        elif is_cyber:
                            radius = 2.2
                        elif is_incinerator:
                            radius = 4.2
                        elif is_eggland:
                            radius = 4.0
                        elif is_singularity:
                            radius = 3.6
                        dist = abs(float(i) - float(sweep_pos))
                        hi = max(0.0, 1.0 - dist / radius)
                        hi *= hi
                    if is_incinerator:
                        furnace_pulse = 0.5 + 0.5 * math.sin(t * 4.60 + phase0 + i * 0.08)
                        hi = max(hi, 0.42 * furnace_pulse * furnace_pulse)
                    if is_singularity:
                        center_hi = max(0.0, 1.0 - abs(pos) * 2.4)
                        center_hi *= 0.45 + 0.55 * (0.5 + 0.5 * math.sin(t * 1.45 + phase0))
                        hi = max(hi, center_hi)
                    if is_cyber:
                        scan_hi = max(0.0, 1.0 - abs(py - scan_y) / 5.0)
                        scan_hi *= scan_hi
                        hi = max(hi, scan_hi)

                    if hi > 0.0:
                        val = _clamp01(val * (1.0 + (shimmer * 0.90) * hi))
                        sat = _clamp01(sat * (1.0 + 0.35 * hi))

                    alpha = int(alpha_main)
                    if is_null:
                        alpha = int(alpha_main * (0.55 + 0.30 * (0.5 + 0.5 * math.sin(t * 0.6 + phase0 + i * 0.3))))
                    if is_grave:
                        alpha = int(alpha_main * (0.78 + 0.18 * (0.5 + 0.5 * math.sin(t * 0.8 + phase0 + i * 0.12))))

                    # reuse a single QColor object
                    col.setHsvF(hue, sat, val, 1.0)
                    col.setAlpha(max(0, min(255, alpha)))

                    # Shadow/backing for readability.
                    painter.setPen(shadow)
                    pt.setX(px + 1.0); pt.setY(py + 1.0)
                    painter.drawText(pt, draw_ch)

                    # reuse glow QColor instead of QColor(col)
                    glow.setRgb(col.red(), col.green(), col.blue(), 255)
                    glow.setAlpha(max(0, min(255, int(glow_alpha + 70.0 * hi))))
                    painter.setPen(glow)

                    pt.setX(px - 1.0); pt.setY(py)
                    painter.drawText(pt, draw_ch)
                    pt.setX(px + 1.0); pt.setY(py)
                    painter.drawText(pt, draw_ch)
                    pt.setX(px); pt.setY(py - 1.0)
                    painter.drawText(pt, draw_ch)
                    pt.setX(px); pt.setY(py + 1.0)
                    painter.drawText(pt, draw_ch)

                    # Dreamspace: faint pastel afterimage.
                    if is_dream:
                        trail.setRgb(col.red(), col.green(), col.blue(), 255)
                        trail.setAlpha(max(0, min(255, int(alpha * 0.35))))
                        painter.setPen(trail)

                        pt.setX(px - 2.0); pt.setY(py - 1.0)
                        painter.drawText(pt, draw_ch)
                        pt.setX(px + 2.0); pt.setY(py + 1.0)
                        painter.drawText(pt, draw_ch)
                    if is_eggland and hi > 0.25:
                        spark_col.setRgb(255, 255, 255, min(180, 70 + int(120.0 * hi)))
                        painter.setPen(spark_col)
                        p0.setX(px + (w * 0.50)); p0.setY(py - 4.0)
                        painter.drawPoint(p0)
                    if is_singularity and hi > 0.20:
                        spark_col.setRgb(255, 215, 170, min(190, 75 + int(115.0 * hi)))
                        painter.setPen(spark_col)
                        cx = px + (w * 0.48)
                        cy = py - 4.0
                        p0.setX(cx - 1.8); p0.setY(cy)
                        p1.setX(cx + 1.8); p1.setY(cy)
                        painter.drawLine(p0, p1)
                        if hi > 0.50:
                            p0.setX(cx); p0.setY(cy - 1.6)
                            painter.drawPoint(p0)

                    # Glitched: chromatic split.
                    if is_glitch:
                        split = 1.25
                        painter.setPen(glitch_r)
                        pt.setX(px - split); pt.setY(py)
                        painter.drawText(pt, draw_ch)

                        painter.setPen(glitch_g)
                        pt.setX(px); pt.setY(py - 1.0)
                        painter.drawText(pt, draw_ch)

                        painter.setPen(glitch_b)
                        pt.setX(px + split); pt.setY(py)
                        painter.drawText(pt, draw_ch)

                    # Blood Rain: drip accents.
                    if is_blood:
                        drip_len = 2.0 + 8.0 * abs(math.sin(t * 2.1 + i * 0.45 + phase0))
                        drip_x = px + (w * 0.35)
                        drip_top = py + 1.0
                        drip_bottom = min(rbottom - 1.0, drip_top + drip_len)
                        drip_col.setRgb(150, 20, 20, min(200, 80 + int(120.0 * hi)))
                        painter.setPen(drip_col)

                        p0.setX(drip_x); p0.setY(drip_top)
                        p1.setX(drip_x); p1.setY(drip_bottom)
                        painter.drawLine(p0, p1)

                    # Rainy: tiny splash dot near the scanline highlight.
                    if is_rainy and hi > 0.25:
                        painter.setPen(QColor(0, 0, 0, 0))   # (this one is fine; not per-glyph-critical)
                        painter.setBrush(splash)
                        pt.setX(px + w * 0.40); pt.setY(py + 3.0)
                        painter.drawEllipse(pt, 1.2, 1.2)

                    # Hell/Blazing/Incinerator: flame lick.
                    if is_hell or is_blazing or is_incinerator:
                        flame_rate = 14.0 if is_incinerator else 10.0
                        flame_mod = 2 if is_incinerator else 3
                        if ((i + int(t * flame_rate)) % flame_mod) == 0:
                            if is_incinerator:
                                flame_h = 4.0 + 9.0 * abs(math.sin(t * 8.5 + i + phase0))
                                flame_col.setRgb(255, 72, 16, 220)
                            else:
                                flame_h = 2.0 + 5.0 * abs(math.sin(t * 5.0 + i + phase0))
                                flame_col.setRgb(255, 140, 50, 120 if is_hell else 160)
                            painter.setPen(flame_col)
                            p0.setX(px + w * 0.45); p0.setY(py - 2.0)
                            p1.setX(px + w * 0.45); p1.setY(py - 2.0 - flame_h)
                            painter.drawLine(p0, p1)
                            if is_incinerator:
                                flame_col.setRgb(255, 244, 190, 175)
                                painter.setPen(flame_col)
                                p0.setX(px + w * 0.45); p0.setY(py - 2.0)
                                p1.setX(px + w * 0.45); p1.setY(py - 4.0 - flame_h * 0.55)
                                painter.drawLine(p0, p1)

                    # Starfall: sparkle when highlighted.
                    if is_starfall and hi > 0.55:
                        spark_col.setRgb(235, 245, 255, 110)
                        painter.setPen(spark_col)

                        cx = px + w * 0.50
                        cy = py - 6.0

                        p0.setX(cx - 2.0); p0.setY(cy)
                        p1.setX(cx + 2.0); p1.setY(cy)
                        painter.drawLine(p0, p1)

                        p0.setX(cx); p0.setY(cy - 2.0)
                        p1.setX(cx); p1.setY(cy + 2.0)
                        painter.drawLine(p0, p1)

                    # Main glyph.
                    painter.setPen(col)
                    pt.setX(px); pt.setY(py)
                    painter.drawText(pt, draw_ch)

                    x += w + (tracking if i < (len(text) - 1) else 0.0)
                    if x > float(text_rect.right()):
                        break

                painter.restore()
                if FOUND_STATS_DEBUG:
                    # ---- Paint spike debugger (stdout only) ----
                    dt_ms = (time.perf_counter() - t0_perf) * 1000.0
                    if dt_ms >= 8.0:  # adjust threshold: try 6.0 / 8.0 / 12.0
                        nowp = time.perf_counter()
                        lastp = getattr(self, "_dbg_last_print", 0.0)
                        if (nowp - lastp) >= 0.50:  # throttle: max ~2 prints/sec
                            self._dbg_last_print = nowp
                            print(
                                f"[BiomePaintSpike] biome={biome_key} row={index.row()} dt={dt_ms:.1f}ms "
                                f"rect={cell_rect.width()}x{cell_rect.height()} text_len={len(text)} "
                                f"glitch={is_glitch} cyber={is_cyber} rainy={is_rainy} snowy={is_snowy} "
                                f"hell={is_hell} blazing={is_blazing} incinerator={is_incinerator} "
                                f"starfall={is_starfall} eggland={is_eggland} "
                                f"singularity={is_singularity}"
                            )
                    # accumulate stats for summary printing
                    self._dbg_acc_ms = getattr(self, "_dbg_acc_ms", 0.0) + dt_ms
                    self._dbg_acc_n = getattr(self, "_dbg_acc_n", 0) + 1
                    self._dbg_max_ms = max(getattr(self, "_dbg_max_ms", 0.0), dt_ms)



        class _WalkingMerchantPeopleDelegate(QStyledItemDelegate):
            def __init__(self, parent=None):
                super().__init__(parent)
                self._t0 = time.time()
                self._label_sway: dict[str, float] = {}
                self._label_sway_last: dict[str, float] = {}

            def paint(self, painter, option, index) -> None:
                try:
                    opt = QStyleOptionViewItem(option)
                    self.initStyleOption(opt, index)
                except Exception:
                    return super().paint(painter, option, index)

                raw_text = str(opt.text or "")
                raw = raw_text.strip()

                widget = opt.widget
                try:
                    base_style = widget.style() if widget else QApplication.style()
                except Exception:
                    base_style = QApplication.style()

                # Draw background/selection/focus WITHOUT text; we render text ourselves above the walkers.
                drew_base = False
                try:
                    painter.save()
                    try:
                        opt_bg = QStyleOptionViewItem(opt)
                        opt_bg.text = ""
                        base_style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt_bg, painter, widget)
                        drew_base = True
                    finally:
                        painter.restore()
                except Exception:
                    drew_base = False
                if not drew_base:
                    return super().paint(painter, opt, index)

                if not raw:
                    return

                try:
                    merchant_name = raw.split()[-1].strip().title() if raw else raw
                    seed = _stable_u32(merchant_name)

                    is_jester = merchant_name == "Jester"
                    is_mari = merchant_name == "Mari"
                    is_rin = merchant_name == "Rin"

                    shirt = QColor(120, 210, 255)
                    accent = QColor(255, 255, 255, 0)
                    if is_jester:
                        shirt = QColor("#A352FF")
                        accent = QColor(0, 0, 0, 210)  # dots
                    elif is_mari:
                        shirt = QColor("#7A4A2A")  # brown hoodie
                        accent = QColor(225, 205, 170, 220)  # drawstrings
                    elif is_rin:
                        shirt = QColor(230, 125, 62)
                        accent = QColor(255, 220, 185, 220)

                    def _u32(p: int, a: int = 0, b: int = 0, c: int = 0) -> int:
                        x = (int(seed) ^ int(p)) & 0xFFFFFFFF
                        x ^= (int(a) * 0x9E3779B1) & 0xFFFFFFFF
                        x ^= (int(b) * 0x85EBCA77) & 0xFFFFFFFF
                        x ^= (int(c) * 0xC2B2AE3D) & 0xFFFFFFFF
                        return _mix_u32(x)

                    def _r01(p: int, a: int = 0, b: int = 0, c: int = 0) -> float:
                        return float(_u32(p, a, b, c) & 0xFFFF) / 65535.0

                    try:
                        text_rect = base_style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, widget)
                    except Exception:
                        text_rect = opt.rect

                    painter.save()
                    try:
                        painter.setClipRect(opt.rect)
                        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
                        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

                        fm = QFontMetrics(opt.font)
                        try:
                            text = fm.elidedText(raw, Qt.TextElideMode.ElideRight, max(0, int(text_rect.width())))
                        except Exception:
                            text = raw

                        chars = list(text)
                        char_widths: list[float] = []
                        for ch in chars:
                            try:
                                cw = float(fm.horizontalAdvance(ch))
                            except Exception:
                                cw = float(fm.horizontalAdvance(" "))
                            char_widths.append(cw)
                        text_w = float(sum(char_widths)) if char_widths else 0.0
                        ascent = float(fm.ascent())
                        descent = float(fm.descent())
                        text_h = float(max(1.0, ascent + descent))

                        align = getattr(opt, "displayAlignment", Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
                        tx = int(text_rect.x())
                        if align & Qt.AlignmentFlag.AlignRight:
                            tx = int(text_rect.x() + text_rect.width() - int(text_w))
                        elif align & Qt.AlignmentFlag.AlignHCenter:
                            tx = int(text_rect.x() + (text_rect.width() - int(text_w)) / 2)

                        cell = opt.rect.adjusted(1, 1, -1, -1)

                        # Pixel-sprite walkers (tiny + readable).
                        grid_w, grid_h = 9, 12
                        scale = 1
                        pw = int(grid_w * scale)
                        ph = int(grid_h * scale)
                        if cell.width() < pw or cell.height() < ph:
                            return

                        # Pin the label to the top (ignore the style's vertical-centering), so walkers can pass under it.
                        ty = int(cell.y()) + 1
                        if ty + int(text_h) >= cell.bottom():
                            ty = int(cell.y())

                        text_box = QRect(tx, ty, max(1, int(text_w)), max(1, int(text_h)))
                        walk_top = int(text_box.bottom()) + 3
                        walk_rect = QRect(cell.x(), walk_top, cell.width(), max(0, cell.bottom() - walk_top + 1))
                        path = walk_rect if (walk_rect.width() >= pw and walk_rect.height() >= ph) else cell

                        avail_w = int(path.width() - pw)
                        avail_h = int(path.height() - ph)
                        if avail_w < 0 or avail_h < 0:
                            return

                        under_top = int(text_box.bottom()) + 1

                        now = float(time.time())
                        t = float(now - self._t0)
                        count_val = 0
                        try:
                            count_idx = index.siblingAtColumn(1)
                            count_val = int(str(count_idx.data() or "0").strip() or "0")
                        except Exception:
                            count_val = 0
                        count_val = max(0, int(count_val))

                        # Scale walkers with the merchant count:
                        # - +1 per 10 up to 100
                        # - then +1 per 100 thereafter
                        # - cap at 20 total walkers
                        extra = min(10, count_val // 10)
                        if count_val > 100:
                            extra += (count_val - 100) // 100
                        people = min(20, int(extra))
                        hop_rate = 0.08 + 0.04 * _r01(P_MSPD)  # lower = slower movement
                        trigger_strength = 0.0

                        def _mul_rgb(c: QColor, f: float) -> QColor:
                            return QColor(
                                max(0, min(255, int(c.red() * f))),
                                max(0, min(255, int(c.green() * f))),
                                max(0, min(255, int(c.blue() * f))),
                                c.alpha(),
                            )

                        def _draw_person(ox: int, oy: int, face_right: bool, frame: int, person_idx: int) -> None:
                            def px(gx: int, gy: int, col: QColor, w: int = 1, h: int = 1) -> None:
                                if w <= 0 or h <= 0:
                                    return
                                if face_right:
                                    rx = gx
                                else:
                                    rx = grid_w - gx - w
                                painter.fillRect(
                                    QRect(ox + (rx * scale), oy + (gy * scale), w * scale, h * scale),
                                    col,
                                )

                            shadow = QColor(0, 0, 0, 55)
                            px(2, 11, shadow, 5, 1)

                            left_down = frame == 0
                            right_down = not left_down
                            left_leg_h = 3 if left_down else 2
                            right_leg_h = 3 if right_down else 2
                            left_foot_y = 11 if left_down else 10
                            right_foot_y = 11 if right_down else 10
                            if frame == 0:
                                left_arm_y, right_arm_y = 5, 4
                            else:
                                left_arm_y, right_arm_y = 4, 5

                            if is_jester:
                                suit = QColor("#A352FF")
                                suit_dark = _mul_rgb(suit, 0.78)
                                bell = QColor("#FFD34D")
                                mask = QColor(250, 250, 250, 245)
                                dot = QColor(0, 0, 0, 210)
                                eye = QColor(0, 0, 0, 235)

                                # Head/hood + jester horns.
                                px(1, 0, bell, 1, 1)
                                px(7, 0, bell, 1, 1)
                                px(1, 1, suit, 1, 1)
                                px(7, 1, suit, 1, 1)
                                px(2, 1, suit, 5, 3)

                                # White mask (face) + features.
                                px(3, 2, mask, 3, 2)
                                px(2, 2, mask, 1, 1)
                                px(6, 2, mask, 1, 1)
                                px(3, 2, eye, 1, 1)
                                px(5, 2, eye, 1, 1)

                                # Torso + arms.
                                px(3, 4, suit_dark, 3, 1)
                                px(2, 5, suit, 5, 3)
                                px(1, left_arm_y, suit, 1, 3)
                                px(7, right_arm_y, suit, 1, 3)

                                # Legs + feet.
                                px(2, 8, suit_dark, 2, left_leg_h)
                                px(5, 8, suit_dark, 2, right_leg_h)
                                px(2, left_foot_y, suit, 2, 1)
                                px(5, right_foot_y, suit, 2, 1)

                                # Polka dots.
                                # Keep these in a simple grid (not random) so Jester reads consistently.
                                # Anchor the pattern to the torso so it doesn't "break" on leg/foot frames.
                                for dx in (2, 4, 6):
                                    for dy in (5, 6, 7):
                                        px(dx, dy, dot, 1, 1)

                            elif is_mari:
                                skin = QColor(235, 210, 195, 245)
                                hair = QColor(18, 18, 18, 245)
                                hair2 = QColor(60, 60, 60, 215)
                                hoodie = QColor("#7A4A2A")
                                hoodie_dark = _mul_rgb(hoodie, 0.72)
                                pants = QColor(hoodie)
                                shoes = QColor(18, 18, 26, 245)
                                bag = QColor(150, 115, 75, 235)
                                bag_dark = _mul_rgb(bag, 0.75)
                                eye = QColor(20, 20, 30, 220)

                                # Backpack on back (draw behind).
                                px(0, 5, bag_dark, 2, 3)
                                px(0, 5, bag, 2, 2)

                                # Hood (draw behind head).
                                px(1, 0, hoodie_dark, 7, 1)
                                px(1, 1, hoodie_dark, 1, 3)
                                px(7, 1, hoodie_dark, 1, 3)

                                # Head (skin) + hair.
                                px(2, 1, skin, 5, 3)
                                px(2, 0, hair, 5, 1)
                                px(2, 1, hair, 1, 3)
                                px(6, 1, hair, 1, 3)
                                px(3, 2, eye, 1, 1)
                                px(5, 2, eye, 1, 1)
                                px(6, 0, hair2, 1, 1)

                                # Torso + arms (hoodie).
                                px(3, 4, hoodie_dark, 3, 1)
                                px(2, 5, hoodie, 5, 3)
                                px(1, left_arm_y, hoodie, 1, 3)
                                px(7, right_arm_y, hoodie, 1, 3)
                                px(1, left_arm_y + 2, skin, 1, 1)
                                px(7, right_arm_y + 2, skin, 1, 1)

                                # Hoodie details.
                                px(3, 5, accent, 1, 2)
                                px(5, 5, accent, 1, 2)
                                px(3, 7, hoodie_dark, 3, 1)

                                # Legs + feet.
                                px(2, 8, pants, 2, left_leg_h)
                                px(5, 8, pants, 2, right_leg_h)
                                px(2, left_foot_y, shoes, 2, 1)
                                px(5, right_foot_y, shoes, 2, 1)

                            elif is_rin:
                                fur = QColor(230, 125, 62, 245)
                                fur_dark = _mul_rgb(fur, 0.72)
                                fur_light = _mul_rgb(fur, 1.16)
                                ear_inner = QColor(255, 220, 190, 235)
                                muzzle = QColor(250, 238, 225, 240)
                                eye = QColor(18, 18, 24, 235)
                                paw = QColor(110, 70, 48, 220)
                                tail_tip = QColor(248, 246, 240, 240)

                                # Tail (draw behind body): larger + fluffy tip.
                                px(0, 4, fur_dark, 3, 4)
                                px(0, 5, fur, 2, 2)
                                px(0, 7, tail_tip, 2, 1)
                                px(1, 8, tail_tip, 1, 1)

                                # Body + chest.
                                px(2, 5, fur, 5, 3)
                                px(3, 5, fur_light, 2, 1)
                                px(3, 6, muzzle, 2, 1)

                                # Head + ears + face (more pronounced).
                                px(3, 2, fur, 4, 3)
                                px(3, 0, fur_dark, 1, 2)
                                px(6, 0, fur_dark, 1, 2)
                                px(3, 1, ear_inner, 1, 1)
                                px(6, 1, ear_inner, 1, 1)
                                px(5, 2, fur_light, 1, 1)   # brow highlight
                                px(6, 3, muzzle, 2, 2)      # snout
                                px(4, 3, eye, 1, 1)
                                px(5, 3, eye, 1, 1)
                                px(7, 4, eye, 1, 1)         # nose

                                # Legs + paws (short fox proportions).
                                fox_leg_y = 9
                                fox_left_h = 2 if left_down else 1
                                fox_right_h = 2 if right_down else 1
                                fox_mid_h = 1
                                px(2, fox_leg_y, fur_dark, 1, fox_left_h)
                                px(4, fox_leg_y, fur_dark, 1, fox_mid_h)
                                px(6, fox_leg_y, fur_dark, 1, fox_right_h)
                                px(2, fox_leg_y + fox_left_h, paw, 1, 1)
                                px(4, fox_leg_y + fox_mid_h, paw, 1, 1)
                                px(6, fox_leg_y + fox_right_h, paw, 1, 1)

                            else:
                                skin = QColor(230, 205, 175, 235)
                                pants = QColor(40, 40, 55, 220)
                                shoes = _mul_rgb(pants, 0.70)
                                eye = QColor(20, 20, 30, 220)
                                shirt_dark = _mul_rgb(shirt, 0.78)

                                px(2, 1, skin, 5, 3)
                                px(4, 4, skin, 1, 1)
                                px(3, 2, eye, 1, 1)
                                px(5, 2, eye, 1, 1)
                                px(3, 4, shirt_dark, 3, 1)
                                px(2, 5, shirt, 5, 3)
                                px(1, left_arm_y, shirt_dark, 1, 3)
                                px(7, right_arm_y, shirt_dark, 1, 3)
                                px(1, left_arm_y + 2, skin, 1, 1)
                                px(7, right_arm_y + 2, skin, 1, 1)
                                px(2, 8, pants, 2, left_leg_h)
                                px(5, 8, pants, 2, right_leg_h)
                                px(2, left_foot_y, shoes, 2, 1)
                                px(5, right_foot_y, shoes, 2, 1)

                        def _waypoint(person_idx: int, seg_i: int) -> tuple[float, float]:
                            edge_bias = _r01(P_WP_E, person_idx, seg_i)
                            a = _r01(P_WP_A, person_idx, seg_i)
                            b = _r01(P_WP_B, person_idx, seg_i)
                            if edge_bias < 0.72:
                                side = int(_r01(P_WP_S, person_idx, seg_i) * 4.0) & 3
                                if side == 0:
                                    return (a * float(avail_w), 0.0)
                                if side == 1:
                                    return (a * float(avail_w), float(avail_h))
                                if side == 2:
                                    return (0.0, a * float(avail_h))
                                return (float(avail_w), a * float(avail_h))
                            return (a * float(avail_w), b * float(avail_h))

                        last = float(self._label_sway_last.get(merchant_name, now))
                        dt = max(0.0, now - last)
                        dt = min(dt, 0.05)
                        self._label_sway_last[merchant_name] = now
                        sway = float(self._label_sway.get(merchant_name, 0.0))
                        sway *= math.exp(-dt * 0.12)

                        text_left = float(text_box.left())
                        text_right = float(text_box.right())
                        x_thresh = max((float(text_box.width()) * 0.06), float(pw) * 0.48)
                        y_thresh = float(ph) * 11.0

                        for k in range(people):
                            phase = (float(k) / float(max(1, people))) + (_r01(P_PO, k) - 0.5) * 0.10
                            tt = (t * hop_rate) + (phase * 6.0)
                            seg = int(math.floor(tt))
                            u = float(tt - float(seg))
                            u = u * u * (3.0 - 2.0 * u)  # smoothstep

                            ax, ay = _waypoint(k, seg)
                            bx, by = _waypoint(k, seg + 1)

                            x0 = float(path.x()) + (ax + (bx - ax) * u)
                            y0 = float(path.y()) + (ay + (by - ay) * u)
                            vx = (bx - ax)

                            # Walk cycle + tiny bob.
                            frame = int((t * 5.0 + float(k) * 1.7 + (_r01(P_ST, k) * 10.0)) % 2.0)
                            bob = -1 if frame == 0 else 0

                            ox = int(round(x0))
                            oy = int(round(y0)) + bob

                            walker_rect = QRect(ox, oy, pw, ph)
                            if walker_rect.bottom() >= under_top:
                                dy = float(walker_rect.top() - under_top)
                                dy_pos = max(0.0, dy)
                                wr_left = float(walker_rect.left())
                                wr_right = float(walker_rect.right())
                                if wr_right < text_left:
                                    dx = text_left - wr_right
                                elif wr_left > text_right:
                                    dx = wr_left - text_right
                                else:
                                    dx = 0.0

                                x_prox = max(0.0, 1.0 - (dx / max(1.0, x_thresh)))
                                y_prox = max(0.0, 1.0 - (dy_pos / max(1.0, y_thresh)))
                                strength = x_prox * y_prox
                                trigger_strength = max(trigger_strength, strength)

                            if abs(vx) > 0.05:
                                face_right = vx > 0.0
                            else:
                                face_right = ((seed >> (k & 15)) & 1) == 1

                            _draw_person(ox, oy, face_right, frame, k)

                        target = max(0.0, min(1.0, math.sqrt(max(0.0, trigger_strength)) * 1.35))
                        if target > 0.0:
                            sway = max(sway, target)
                        self._label_sway[merchant_name] = sway

                        # Draw label on top; sway when a walker passes underneath.
                        try:
                            painter.save()
                            try:
                                painter.setFont(opt.font)
                                if opt.state & QStyle.StateFlag.State_Selected:
                                    painter.setPen(opt.palette.color(QPalette.ColorRole.HighlightedText))
                                else:
                                    painter.setPen(opt.palette.color(QPalette.ColorRole.Text))

                                base_x = float(tx)
                                base_y = float(ty) + ascent

                                s = max(0.0, min(1.0, float(sway)))
                                ease = math.sqrt(s)
                                ph0 = (float(seed & 0xFFFF) / 65535.0) * (2.0 * math.pi)
                                amp_x = 3.3 * ease
                                amp_y = 5.2 * ease
                                x_cursor = base_x
                                for i, ch in enumerate(chars):
                                    if i < len(char_widths):
                                        cw = char_widths[i]
                                    else:
                                        try:
                                            cw = float(fm.horizontalAdvance(ch))
                                        except Exception:
                                            cw = float(fm.horizontalAdvance(" "))
                                    wob = math.sin((t * 10.0) + (i * 0.65) + ph0)
                                    wob2 = math.sin((t * 6.0) + (i * 0.9) + (ph0 * 0.7))
                                    painter.drawText(QPointF(x_cursor + (wob2 * amp_x * 0.38), base_y + (wob * amp_y * 0.38)), ch)
                                    x_cursor += cw
                            finally:
                                painter.restore()
                        except Exception:
                            pass
                    finally:
                        painter.restore()
                except Exception:
                    # Never break table painting from animation overlay.
                    return


        def _set_biome_table(rows: list[tuple[str, int]]) -> None:
            biome_table.setRowCount(len(rows))
            for r, (biome, count) in enumerate(rows):
                b = str(biome or "").strip().upper()
                c = int(count or 0)
                name_item = QTableWidgetItem(b)
                name_item.setData(Qt.ItemDataRole.UserRole, b)

                try:
                    _color_int, thumb = biome_meta(b)
                except Exception:
                    thumb = ""
                try:
                    dur = biome_duration(b)
                except Exception:
                    dur = None

                f = name_item.font()
                f.setBold(c > 0)
                name_item.setFont(f)

                biome_table.setItem(r, 0, name_item)

                count_item = QTableWidgetItem(str(int(c)))
                count_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                biome_table.setItem(r, 1, count_item)

        def _set_merchant_table(rows: list[tuple[str, int]]) -> None:
            merch_table.setRowCount(len(rows))
            colors = {"Jester": QColor("#A352FF"), "Mari": QColor("#FF82AB"), "Rin": QColor(230, 125, 62)}
            for r, (name, count) in enumerate(rows):
                n = str(name or "").strip().title()
                c = int(count or 0)
                name_item = QTableWidgetItem(n)
                name_item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
                if n in colors:
                    name_item.setForeground(colors[n])
                f = merch_table.font()
                f.setBold(c > 0)
                name_item.setFont(f)
                merch_table.setItem(r, 0, name_item)
                it = QTableWidgetItem(str(int(c)))
                it.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                merch_table.setItem(r, 1, it)

        biome_delegate = _AnimatedBiomeNameDelegate(biome_table)
        biome_table.setItemDelegateForColumn(0, biome_delegate)
        merch_people_delegate = _WalkingMerchantPeopleDelegate(merch_table)
        merch_table.setItemDelegateForColumn(0, merch_people_delegate)
        anim_timer = QTimer(dlg)
        anim_timer.setInterval(16)
        try:
            anim_timer.setTimerType(Qt.TimerType.PreciseTimer)
        except Exception:
            pass
        def _tick() -> None:
            dlg._ui_heartbeat = time.perf_counter()
            try:
                if not dlg.isVisible() or dlg.isMinimized():
                    return
            except Exception:
                pass
            if FOUND_STATS_DEBUG:
                # ---- UI hitch detector: how late is the timer firing? ----
                nowp = time.perf_counter()
                lastp = getattr(dlg, "_dbg_last_tick", None)
                dlg._dbg_last_tick = nowp
                if lastp is not None:
                    gap_ms = (nowp - lastp) * 1000.0
                    # expected is ~16ms; anything big here = event loop blocked
                    if gap_ms >= 40.0:
                        print(f"[UIHitch] tick_gap={gap_ms:.1f}ms (expected ~{anim_timer.interval()}ms)")


            try:
                cw = tabs.currentWidget()
            except Exception:
                cw = None
            if cw is biome_tab:
                biome_table.viewport().update()
            elif cw is merch_tab:
                merch_table.viewport().update()
            else:
                biome_table.viewport().update()
                merch_table.viewport().update()
                
            if FOUND_STATS_DEBUG:
                nowp = time.perf_counter()
                lastp = getattr(dlg, "_dbg_last_sum", 0.0)
                if nowp - lastp >= 1.0:
                    dlg._dbg_last_sum = nowp
                    n = getattr(biome_delegate, "_dbg_acc_n", 0)
                    if n:
                        total = getattr(biome_delegate, "_dbg_acc_ms", 0.0)
                        mx = getattr(biome_delegate, "_dbg_max_ms", 0.0)
                        print(f"[BiomePaintSum] paints={n} total={total:.1f}ms avg={total/n:.2f}ms max={mx:.1f}ms")
                    biome_delegate._dbg_acc_n = 0
                    biome_delegate._dbg_acc_ms = 0.0
                    biome_delegate._dbg_max_ms = 0.0


        anim_timer.timeout.connect(_tick)
        anim_timer.start()
        dlg._found_stats_biome_delegate = biome_delegate
        dlg._found_stats_merch_people_delegate = merch_people_delegate
        dlg._found_stats_biome_anim_timer = anim_timer

        def _refresh() -> None:
            window_seconds = range_combo.currentData()
            bcounts, btotal, mcounts, mtotal = _get_counts(window_seconds)

            try:
                all_biomes = [b for b in biome_names() if str(b).strip().upper() != "NORMAL"]
            except Exception:
                all_biomes = []
            all_biomes = [str(b).strip().upper() for b in all_biomes if str(b).strip()]
            known_biomes = set(all_biomes)

            biome_rows: list[tuple[str, int]] = []
            for b in all_biomes:
                try:
                    c = int((bcounts or {}).get(b, 0) or 0)
                except Exception:
                    c = 0
                biome_rows.append((b, c))
            for b, v in (bcounts or {}).items():
                if not isinstance(b, str):
                    continue
                key = b.strip().upper()
                if not key or key == "NORMAL" or key in known_biomes:
                    continue
                try:
                    c = int(v)
                except Exception:
                    continue
                biome_rows.append((key, c))
            biome_idx = {b: i for i, b in enumerate(all_biomes)}
            biome_rows.sort(key=lambda kv: (-kv[1], biome_idx.get(kv[0], 10**9), kv[0]))
            _set_biome_table(biome_rows)

            # Normalize merchant keys to title case so canonical rows (Jester/Mari/Rin)
            # always pick up existing counts, even from legacy mixed-case data.
            mcounts_norm: dict[str, int] = {}
            for k, v in (mcounts or {}).items():
                if not isinstance(k, str):
                    continue
                name = k.strip().title()
                if not name:
                    continue
                try:
                    mcounts_norm[name] = mcounts_norm.get(name, 0) + int(v)
                except Exception:
                    continue

            merchant_rows: list[tuple[str, int]] = []
            for name in ("Jester", "Mari", "Rin"):
                try:
                    c = int((mcounts_norm or {}).get(name, 0) or 0)
                except Exception:
                    c = 0
                merchant_rows.append((name, c))
            for name, v in (mcounts_norm or {}).items():
                if not name or name in ("Jester", "Mari", "Rin"):
                    continue
                try:
                    c = int(v)
                except Exception:
                    continue
                merchant_rows.append((name, c))
            merch_idx = {"Jester": 0, "Mari": 1, "Rin": 2}
            merchant_rows.sort(key=lambda kv: (-kv[1], merch_idx.get(kv[0], 10**9), kv[0]))
            _set_merchant_table(merchant_rows)

            biome_total_lbl.setText(f"Total biomes: {btotal}")
            merch_total_lbl.setText(f"Total merchants: {mtotal}")

        range_combo.currentIndexChanged.connect(lambda *_args: _refresh())
        refresh_btn.clicked.connect(_refresh)
        _refresh()
        dlg.exec()

    def show_all_time_found_breakdown(self) -> None:
        snap = self._get_found_stats_snapshot()
        bt = snap.get("biomes_total") if isinstance(snap.get("biomes_total"), dict) else {}
        mt = snap.get("merchants_total") if isinstance(snap.get("merchants_total"), dict) else {}
        b_total = int(snap.get("biomes_total_count", 0) or 0)
        m_total = int(snap.get("merchants_total_count", 0) or 0)

        def _fmt_counts(d: dict) -> str:
            items = []
            for k, v in (d or {}).items():
                try:
                    items.append((str(k), int(v)))
                except Exception:
                    continue
            items.sort(key=lambda kv: (-kv[1], kv[0]))
            return "\n".join(f"{k}: {v}" for k, v in items) if items else "(none)"

        details = (
            "Biomes (all time):\n"
            f"{_fmt_counts(bt)}\n\n"
            "Merchants (all time):\n"
            f"{_fmt_counts(mt)}"
        )

        msg = QMessageBox(self)
        msg.setWindowTitle("All-Time Found Breakdown")
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText(f"Biomes found (all time): {b_total}\nMerchants found (all time): {m_total}")
        msg.setDetailedText(details)
        msg.exec()

    def show_biomes_found_window(self, window_seconds: float, title: str) -> None:
        counts = {}
        total = 0
        try:
            wt = getattr(self, "worker_thread", None)
            ms = getattr(wt, "ms", None) if wt else None
            if ms:
                out = ms.get_biomes_found_counts(window_seconds)
                counts = out.get("counts", {}) if isinstance(out, dict) else {}
                total = int(out.get("total", 0) or 0) if isinstance(out, dict) else 0
            else:
                stats = self._load_found_stats_from_disk()
                evs = stats.get("biome_events", []) if isinstance(stats, dict) else []
                now_ts = time.time()
                cutoff = now_ts - float(window_seconds)
                for ev in (evs or []):
                    if not isinstance(ev, dict):
                        continue
                    try:
                        ts = float(ev.get("ts", 0))
                    except Exception:
                        continue
                    if ts < cutoff:
                        continue
                    b = ev.get("biome")
                    if not isinstance(b, str):
                        continue
                    biome = b.strip().upper()
                    if not biome or biome == "NORMAL":
                        continue
                    counts[biome] = int(counts.get(biome, 0)) + 1
                total = sum(int(v) for v in counts.values())
        except Exception:
            counts = {}
            total = 0

        items = []
        for k, v in (counts or {}).items():
            try:
                items.append((str(k), int(v)))
            except Exception:
                continue
        items.sort(key=lambda kv: (-kv[1], kv[0]))
        detail = "\n".join(f"{k}: {v}" for k, v in items) if items else "(none)"

        msg = QMessageBox(self)
        msg.setWindowTitle(f"Biomes Found — {title}")
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText(f"Biomes found ({title}): {total}")
        msg.setDetailedText(detail)
        msg.exec()
