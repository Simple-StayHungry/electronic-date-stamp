#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Image candidate provider, extracted from the supplied v14 engine.

This module locates possible anchors. It does not authorize writes, classify
fields as complete, choose fonts, or export PDFs. Its visual templates remain
heuristic; review and hard checks are implemented outside this provider.
"""
from __future__ import annotations

import base64
import io
import math
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import cv2
# The tool parallelizes at the document level; nested OpenCV thread pools make
# repeated template matching slower and can stall on long PDFs.
try:
    cv2.setNumThreads(1)
    cv2.ocl.setUseOpenCL(False)
except Exception:
    pass
import fitz
import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# 版式常量 —— 全部由实测标定，修改候选定位器后必须重跑 tests/ 和回归样本回放
# ---------------------------------------------------------------------------
DPI = 200.0
PX_PER_PT = DPI / 72.0
CJK = ("年", "月", "日")
CJK_MAP = {"year":"年", "month":"月", "day":"日"}

# 三字组合接受阈值。旧版 0.72 会卡掉真栏位（本批实测真栏位 0.665 / 0.700）
TRIPLE_ACCEPT = 0.66
# 月/日 两字 + 年位置推断（年被红章压住时的兜底）
PAIR_ACCEPT = 0.76
# 年→月、月→日 之间必须留出「能塞下数字的空白格」，否则一律判为正文
GAP_MIN_RATIO = 0.45
# 几何兜底：得分阈值，以及必须被模板匹配印证的字数
GEOM_ACCEPT = 0.70
GEOM_MIN_CORROBORATION = 2

# 宋体「月」的墨迹高 = 0.915 em（实测：上沿 −0.802em，下沿 +0.113em）
CJK_INK_EM = 0.915
# 宋体 月/日/年 墨迹底沿相对基线的偏移（em），用于还原真实基线
CJK_INK_BASELINE_EM = {"年": 0.110, "月": 0.113, "日": 0.072}
# Times New Roman 数字墨迹高 = 0.685 em
TIMES_DIGIT_INK_EM = 0.685
# Word 里同磅值下 数字墨迹高 / 宋体汉字墨迹高（= 0.685 / 0.915），供交叉校验
DIGIT_TO_CJK_INK_RATIO = 0.745

# 数字与相邻汉字之间的最小留白（× 汉字墨迹高）
SIDE_GAP_RATIO = 0.10
# 年份与“年”之间采用右锚定排版。年份本质上没有固定“格宽”：
# 不同模板会给“年”左侧留 18pt、25pt、35pt 甚至更宽。
# 旧版把年份硬塞进由 年/月/日 间距反推的固定格，换模板就会误判“放不下”。
YEAR_RIGHT_GAP_RATIO = 0.15
YEAR_GAP_MIN_EM = 0.10
YEAR_GAP_MAX_EM = 0.55
# 判断年份是否已存在时，要兼容“2026”和“二〇二六”两种写法。
# 后者宽度接近 4 个汉字，因此检测窗必须比 Times 四位数字更宽。
YEAR_OCCUPANCY_CJK_WIDTH = 4.35

DATE_LABEL_ACCEPT = 0.74
DATE_LABEL_SCALE_MIN = 0.72
DATE_LABEL_SCALE_MAX = 1.38
DATE_LABEL_SCALE_STEPS = 27

@dataclass
class Box:
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def x1(self) -> float:
        return self.x + self.w

    @property
    def y1(self) -> float:
        return self.y + self.h


@dataclass
class Detection:
    """一个落款栏的识别结果。坐标为 200dpi 渲染像素。"""

    page: int
    year: Box
    month: Box
    day: Box
    confidence: float
    method: str
    fill: Dict[str, bool] = field(default_factory=lambda: {"year": True,
                                                           "month": True,
                                                           "day": True})
    year_ink: Optional[Box] = None      # 各汉字实际墨迹框（用于精确排字）
    month_ink: Optional[Box] = None
    day_ink: Optional[Box] = None
    mode: str = "ymd"                 # ymd | full_date
    label: Optional[Box] = None         # full_date 模式下“日期”标签位置
    year_gap_px: Optional[float] = None  # 同行“数字→月/日”实测间距，用于年份自适应排版

    @property
    def fill_year(self) -> bool:
        return bool(self.fill.get("year", True))

    @property
    def row_cy(self) -> float:
        if self.mode == "full_date" and self.label is not None:
            return float(self.label.cy)
        return float(np.median([self.year.cy, self.month.cy, self.day.cy]))

    @property
    def row_cy_pt(self) -> float:
        return self.row_cy / PX_PER_PT

    @property
    def char_h(self) -> float:
        if self.mode == "full_date" and self.label is not None:
            return float(self.label.h)
        return float(np.median([self.year.h, self.month.h, self.day.h]))

    @property
    def spacing(self) -> float:
        if self.mode == "full_date" and self.label is not None:
            return max(24.0, float(self.label.w) * 1.4)
        return ((self.month.x - self.year.x) + (self.day.x - self.month.x)) / 2.0

    def as_json(self) -> dict:
        # 注意：Box 的坐标常常是 numpy.int64，json.dumps 无法序列化，
        # 必须在出口统一转成原生类型。
        def f(b: "Box") -> list:
            return [float(b.x), float(b.y), float(b.w), float(b.h)]

        return {
            "page": int(self.page),
            "method": str(self.method),
            "mode": str(self.mode),
            "label": f(self.label) if self.label is not None else None,
            "confidence": round(float(self.confidence), 3),
            "fill_year": bool(self.fill_year),
            "fill": {str(k): bool(v) for k, v in self.fill.items()},
            "row_cy_pt": round(float(self.row_cy_pt), 1),
            "row_cy": float(self.row_cy),
            "year": f(self.year),
            "month": f(self.month),
            "day": f(self.day),
            "year_gap_pt": round(float(self.year_gap_px / PX_PER_PT), 2) if self.year_gap_px is not None else None,
        }


@dataclass
class BuildResult:
    output_pdf: str
    changed_pages: List[int]
    skipped_pages: List[int]
    font_name: str
    font_size_pt: Optional[float]
    warnings: List[str] = field(default_factory=list)

    @property
    def failed_pages(self) -> List[int]:
        return [int(w.split("第 ")[1].split(" 页")[0])
                for w in self.warnings if "没有实际变化" in w]


class DateRowLocator:
    VERSION = "22.1.0-candidate-provider"

    def __init__(self, assets_dir: Optional[str] = None, dpi: float = DPI,
                 times_path: Optional[str] = None):
        self.dpi = dpi
        self.px_per_pt = dpi / 72.0
        base = Path(__file__).resolve().parent
        self.assets_dir = Path(assets_dir) if assets_dir else base.parent / "assets"
        self._times_path = Path(times_path) if times_path else None
        self.templates = self._load_templates()
        label_path = self.assets_dir / "DATE_LABEL.png"
        self.date_label_template = (np.array(Image.open(label_path).convert("L"))
                                    if label_path.exists() else None)

    # ------------------------------------------------------------------
    # 模板
    # ------------------------------------------------------------------
    def _load_templates(self) -> Dict[str, List[np.ndarray]]:
        bank: Dict[str, List[np.ndarray]] = {c: [] for c in CJK}
        mapping = {str(ord(c)): c for c in CJK}
        for p in sorted(self.assets_dir.glob("*.png")):
            m = re.search(r"_(\d+)\.png$", p.name)
            if not m or m.group(1) not in mapping:
                continue
            bank[mapping[m.group(1)]].append(np.array(Image.open(p).convert("L")))
        if any(not bank[c] for c in CJK):
            raise RuntimeError("日期识别模板缺失，请重新解压完整工具包。")
        return bank

    # ------------------------------------------------------------------
    # 渲染 / 预处理
    # ------------------------------------------------------------------
    def render_page(self, page: fitz.Page, dpi: Optional[float] = None) -> np.ndarray:
        z = (dpi or self.dpi) / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
        return np.array(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))

    @staticmethod
    def _remove_red(rgb: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Conservative de-red used for locating 年/月/日 glyphs.

        Keep dark mixed pixels because the black anchor glyph may be underneath a
        seal. Field blankness uses the stronger variant below.
        """
        ri, gi, bi = [rgb[:, :, i].astype(np.int16) for i in range(3)]
        red = (ri > 135) & (ri > gi + 30) & (ri > bi + 30)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).copy()
        gray[red] = 255
        return gray, (red.astype(np.uint8) * 255)

    @staticmethod
    def _remove_red_aggressive(rgb: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Stronger seal removal for deciding whether a numeric slot is blank."""
        ri, gi, bi = [rgb[:, :, i].astype(np.int16) for i in range(3)]
        rgb_red = ((ri > 82) & (ri - gi > 18) & (ri - bi > 16) &
                   (ri > gi * 1.12) & (ri > bi * 1.10))
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
        h, sat, val = [hsv[:, :, i] for i in range(3)]
        hsv_red = (((h <= 12) | (h >= 172)) & (sat >= 48) & (val >= 55))
        red = rgb_red | hsv_red
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).copy()
        gray[red] = 255
        return gray, (red.astype(np.uint8) * 255)

    @staticmethod
    def _seal_boxes(rgb: np.ndarray) -> List[Tuple[int, int, int, int, int]]:
        ri, gi, bi = [rgb[:, :, i].astype(np.int16) for i in range(3)]
        mask = ((ri > 140) & (ri > gi + 30) & (ri > bi + 30)).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8), 1)
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        out = []
        for i in range(1, n):
            x, y, w, h, area = [int(v) for v in stats[i]]
            if area > 700 and w > 60 and h > 60:
                out.append((area, x, y, w, h))
        return sorted(out, reverse=True)

    @staticmethod
    def largest_ink(block: np.ndarray) -> Optional[Box]:
        """块内最大连通块的墨迹框 —— 排除红章弧线与噪点。"""
        if block.size == 0:
            return None
        g = np.array(Image.fromarray(block).convert("L"))
        ri, gi, bi = [block[:, :, k].astype(np.int16) for k in range(3)]
        red = (ri > 135) & (ri > gi + 30) & (ri > bi + 30)
        m = ((g < 175) & (~red)).astype(np.uint8)
        n, _, stats, _ = cv2.connectedComponentsWithStats(m, 8)
        if n <= 1:
            return None
        k = max(range(1, n), key=lambda i: stats[i][4])
        x, y, w, h, area = [int(v) for v in stats[k]]
        if area < 20:
            return None
        return Box(float(x), float(y), float(w), float(h))

    @staticmethod
    def glyph_ink(block: np.ndarray, core: Box, pad: int = 6) -> Optional[Box]:
        """返回一个汉字的完整墨迹框，而不是只取最大连通块。

        扫描件里的宋体/仿宋汉字经常被阈值切成多个彼此不相连的竖画、横画。
        只取最大块会把“月/日/年”的左沿估得过于靠右，继而让数字单元格
        吃进汉字自身的左竖画，误判成“这个数字已经存在”。

        这里仅合并中心仍落在模板命中框内、且具有字级高度的黑色块。这样能
        补齐同一汉字的离散笔画，又不会把左侧日期数字或红章弧线并进来。
        """
        if block.size == 0:
            return None
        g = np.array(Image.fromarray(block).convert("L"))
        ri, gi, bi = [block[:, :, k].astype(np.int16) for k in range(3)]
        red = (ri > 135) & (ri > gi + 30) & (ri > bi + 30)
        m = ((g < 175) & (~red)).astype(np.uint8)
        n, _, stats, cents = cv2.connectedComponentsWithStats(m, 8)
        if n <= 1:
            return None

        cx0, cx1 = float(pad), float(pad + core.w)
        cy0, cy1 = float(pad), float(pad + core.h)
        keep = []
        for i in range(1, n):
            x, y, w, h, area = [int(v) for v in stats[i][:5]]
            cx, cy = [float(v) for v in cents[i]]
            if area < 8:
                continue
            if h < max(4.0, core.h * 0.20):
                continue
            if not (cx0 - 1.0 <= cx <= cx1 + 1.0 and cy0 - 2.0 <= cy <= cy1 + 2.0):
                continue
            keep.append((x, y, w, h, area))

        if not keep:
            return DateRowLocator.largest_ink(block)
        x0 = min(v[0] for v in keep)
        y0 = min(v[1] for v in keep)
        x1 = max(v[0] + v[2] for v in keep)
        y1 = max(v[1] + v[3] for v in keep)
        return Box(float(x0), float(y0), float(x1 - x0), float(y1 - y0))

    # ------------------------------------------------------------------
    # 模板匹配
    # ------------------------------------------------------------------
    def _search_rois(self, rgb: np.ndarray, seals=None):
        h, w = rgb.shape[:2]
        seals = seals if seals is not None else self._seal_boxes(rgb)
        if seals is None:
            seals = []
        rois, taken = [], []
        for area, x, y, sw, sh in seals:
            if any(abs((x + sw / 2) - (tx + tw / 2)) < 0.5 * max(sw, tw)
                   and abs((y + sh / 2) - (ty + th / 2)) < 0.5 * max(sh, th)
                   for _, tx, ty, tw, th in taken):
                continue
            taken.append((area, x, y, sw, sh))
            rois.append((max(0, int(x - 170)), max(0, int(y + sh * 0.40)),
                         min(w, int(x + sw + 300)), min(h, int(y + sh + 190))))
            if len(rois) >= 2:
                break
        return rois, seals, taken

    def _roi_groups(self, rgb: np.ndarray, seals) -> List[List[tuple]]:
        """分级搜索区：先搜最可能的位置，没结果再放大范围。

        落款栏绝大多数在盖章行附近或页面下部，先搜这两处可以省掉一大半模板匹配。
        """
        h, w = rgb.shape[:2]
        seal_rois, _, _ = self._search_rois(rgb, seals)
        primary = list(seal_rois)
        primary.append((int(w * 0.08), int(h * 0.50), int(w * 0.96), int(h * 0.99)))
        secondary = [(int(w * 0.05), int(h * 0.06), int(w * 0.97), int(h * 0.55))]
        return [primary, secondary]

    @staticmethod
    def _dedupe(matches: Sequence[tuple], max_n: int = 30) -> List[tuple]:
        keep = []
        for v in sorted(matches, reverse=True):
            if all(abs(v[1] - k[1]) > 10 or abs(v[2] - k[2]) > 10 for k in keep):
                keep.append(v)
            if len(keep) >= max_n:
                break
        return keep

    def _match_char(self, gray: np.ndarray, roi: tuple,
                    tmpls: Sequence[np.ndarray], full_quality: bool=False) -> List[tuple]:
        x0, y0, x1, y1 = [int(v) for v in roi]
        crop0 = gray[y0:y1, x0:x1]
        if crop0.size == 0:
            return []
        # Template matching dominates both startup time and memory. Matching on a
        # 0.68x working image preserves 20px+ Chinese glyphs at the source 200dpi
        # while cutting result-matrix area by more than half. Coordinates are
        # mapped back to source pixels before any geometric decision.
        down = 1.0 if full_quality else (.68 if min(crop0.shape[:2]) >= 90 else .82)
        if down < .999:
            crop=cv2.resize(crop0,None,fx=down,fy=down,interpolation=cv2.INTER_AREA)
        else:
            crop=crop0
        ink = (crop < 175).astype(np.uint8) * 255
        out = []
        for tid, tmpl in enumerate(tmpls):
            th, tw = tmpl.shape
            for sc in (0.86, 1.0, 1.14):
                nw0, nh0 = max(8, int(round(tw * sc))), max(8, int(round(th * sc)))
                nw=max(6,int(round(nw0*down)));nh=max(6,int(round(nh0*down)))
                if nw >= crop.shape[1] or nh >= crop.shape[0]:
                    continue
                tr = cv2.resize(tmpl, (nw, nh),
                                interpolation=cv2.INTER_AREA if down*sc < 1 else cv2.INTER_CUBIC)
                ti = (tr < 175).astype(np.uint8) * 255
                if int(ti.sum()) < 255 * 8:
                    continue
                dist = cv2.matchTemplate(ink, ti, cv2.TM_SQDIFF_NORMED)
                flat = dist.ravel()
                if flat.size == 0:
                    continue
                k = min(10, flat.size)
                idx = np.argpartition(flat, k - 1)[:k]
                vals = []
                for ind in idx:
                    yy, xx = np.unravel_index(ind, dist.shape)
                    d = float(dist[yy, xx])
                    if d < 0.58:
                        vals.append((d, xx, yy))
                vals.sort()
                local = []
                sep=max(5,int(round(8*down)))
                for d, xx, yy in vals:
                    if all(abs(xx-kx)>sep or abs(yy-ky)>sep for _,kx,ky in local):
                        local.append((d,xx,yy))
                    if len(local)>=3:break
                for d,xx,yy in local:
                    sx=x0+xx/down;sy=y0+yy/down
                    out.append((1.0-d,float(sx),float(sy),float(nw/down),float(nh/down),sc,tid))
                del dist
        return sorted(out, reverse=True)[:36]

    def _char_matches(self, rgb: np.ndarray, rois: Sequence[tuple],
                      seals=None, full_quality: bool=False) -> Tuple[np.ndarray, Dict[str, List[tuple]]]:
        gray, _ = self._remove_red(rgb)
        matches = {c: [] for c in CJK}
        for roi in rois:
            for c in CJK:
                matches[c].extend(self._match_char(gray, roi, self.templates[c], full_quality=full_quality))
        for c in CJK:
            matches[c] = self._dedupe(matches[c], 28)
        return gray, matches

    @staticmethod
    def _seal_relation(xmid: float, cy: float, seals: Sequence[tuple]):
        best, best_data = 0.0, None
        for _, x, y, w, h in seals[:2]:
            dx = abs(xmid - (x + w / 2)) / (w + 1)
            dy = abs(cy - (y + h * 0.95)) / (h + 1)
            rel = math.exp(-1.25 * dx - 1.6 * dy)
            if rel > best:
                best, best_data = rel, (x, y, w, h, dx, dy)
        return best, best_data

    # ------------------------------------------------------------------
    # 判据：年→月、月→日 之间必须留得下数字
    # ------------------------------------------------------------------
    @staticmethod
    def _blank_ratios(a: tuple, b: tuple, c: tuple) -> Tuple[float, float]:
        """三个字形各以 (x, w, h) 传入。

        返回 (月.x−年.x)−年.w 与 (日.x−月.x)−月.w，按汉字高归一化。
        真落款栏实测 0.88–2.57；正文相邻字 ≈ 0.1。这是区分真假栏位最干净的判据。
        """
        hh = max(1.0, (a[2] + b[2] + c[2]) / 3.0)
        g1 = ((b[0] - a[0]) - a[1]) / hh
        g2 = ((c[0] - b[0]) - b[1]) / hh
        return g1, g2

    # ------------------------------------------------------------------
    # 检测
    # ------------------------------------------------------------------
    def _detect_native_row(self, page: fitz.Page, pno: int) -> Optional[Detection]:
        """PDF 自带文字层（扫描件一般不适用）"""
        words = page.get_text("words")
        hits = {c: [] for c in CJK}
        for w in words:
            t = str(w[4]).strip()
            if t in hits:
                hits[t].append(w)
        if not all(hits[c] for c in CJK):
            return None
        best = None
        for yw in hits["年"]:
            for mw in hits["月"]:
                for dw in hits["日"]:
                    yc = (yw[1] + yw[3]) / 2
                    mc = (mw[1] + mw[3]) / 2
                    dc = (dw[1] + dw[3]) / 2
                    if max(abs(yc - mc), abs(yc - dc), abs(mc - dc)) > 8:
                        continue
                    if not (yw[0] < mw[0] < dw[0]):
                        continue
                    s1, s2 = mw[0] - yw[0], dw[0] - mw[0]
                    if s1 <= 0 or s2 <= 0:
                        continue
                    sc = -abs(math.log(s1 / s2))
                    if best is None or sc > best[0]:
                        best = (sc, yw, mw, dw)
        if not best:
            return None
        _, yw, mw, dw = best
        s = self.px_per_pt

        def b(w):
            return Box(w[0] * s, w[1] * s, (w[2] - w[0]) * s, (w[3] - w[1]) * s)

        return Detection(pno, b(yw), b(mw), b(dw), 0.99, "PDF文字层")

    def _detect_image_row(self, rgb: np.ndarray, pno: int, matches,
                          seals) -> Optional[Detection]:
        h_img = rgb.shape[0]
        best = None
        for a in matches["年"]:
            ay = a[2] + a[4] / 2
            for b in matches["月"]:
                by = b[2] + b[4] / 2
                hh = (a[4] + b[4]) / 2
                if abs(ay - by) > max(11, 0.45 * hh):
                    continue
                sp1 = b[1] - a[1]
                if not 30 <= sp1 <= 230:
                    continue
                for c in matches["日"]:
                    cy3 = c[2] + c[4] / 2
                    if max(abs(ay - cy3), abs(by - cy3)) > max(11, 0.45 * hh):
                        continue
                    sp2 = c[1] - b[1]
                    if not 30 <= sp2 <= 230:
                        continue
                    ratio = sp1 / max(1e-6, sp2)
                    if not 0.55 <= ratio <= 1.70:
                        continue
                    g1, g2 = self._blank_ratios((a[1], a[3], a[4]),
                                                (b[1], b[3], b[4]),
                                                (c[1], c[3], c[4]))
                    if min(g1, g2) < GAP_MIN_RATIO:      # ← 新增：正文一律出局
                        continue
                    avg = (a[0] + b[0] + c[0]) / 3
                    xmid = (a[1] + c[1] + c[3]) / 2
                    cymid = (ay + by + cy3) / 3
                    rel, _ = self._seal_relation(xmid, cymid, seals)
                    score = avg + 0.04 * (1 - min(1, abs(math.log(ratio)) / 0.5)) + 0.09 * rel
                    if cymid > h_img * 0.70:
                        score += 0.015
                    if best is None or score > best[0]:
                        best = (score, a, b, c)
        if best and best[0] >= TRIPLE_ACCEPT:
            score, a, b, c = best
            return Detection(pno, Box(a[1], a[2], a[3], a[4]),
                             Box(b[1], b[2], b[3], b[4]),
                             Box(c[1], c[2], c[3], c[4]),
                             min(1.0, score / 1.10), "年月日三字识别（含空白格校验）")
        return None

    def _detect_pair_row(self, rgb: np.ndarray, pno: int, matches,
                         seals) -> Optional[Detection]:
        """年 被红章压住时的兜底：只认 月/日，按间距推断 年 的位置。"""
        pairs = []
        for b in matches["月"]:
            by = b[2] + b[4] / 2
            for c in matches["日"]:
                cy = c[2] + c[4] / 2
                hh = (b[4] + c[4]) / 2
                if abs(by - cy) > max(10, 0.42 * hh):
                    continue
                sp = c[1] - b[1]
                if not 45 <= sp <= 180:
                    continue
                avg = (b[0] + c[0]) / 2
                xmid = (b[1] + c[1] + c[3]) / 2
                cymid = (by + cy) / 2
                rel, rel_data = self._seal_relation(xmid, cymid, seals)
                if not seals or rel < 0.30:
                    continue
                pairs.append((avg + 0.12 * rel, b, c, rel_data))
        if not pairs:
            return None
        score, b, c, _ = max(pairs, key=lambda z: z[0])
        if score < PAIR_ACCEPT:
            return None
        sp = c[1] - b[1]
        yw = max(b[3] * 1.18, c[3] * 1.35)
        yh = max(b[4], c[4])
        return Detection(pno, Box(b[1] - sp, (b[2] + c[2]) / 2, yw, yh),
                         Box(b[1], b[2], b[3], b[4]), Box(c[1], c[2], c[3], c[4]),
                         min(0.93, score / 1.05), "月日识别+年位置推断")

    def _detect_seal_projection_row(self, rgb: np.ndarray, pno: int,
                                    seals) -> Optional[Detection]:
        """Fast fallback for blurry scanned 年/月/日 rows underneath a red seal.

        Some scanned stamp pages retain the date grammar but the three Chinese
        anchors are too faint / seal-occluded for character-template matching.
        This path intentionally does *not* OCR or guess any date value. It only
        accepts a sparse lower row near a sizeable red seal when that row contains
        exactly three date-anchor-sized black groups with plausible spacing.

        The constraints are deliberately narrow so ordinary company-name text or
        body copy cannot become an auto-write target. Existing numeric ink is
        still classified later by evidence.py and is never overwritten here.
        """
        if not seals:
            return None
        gray, _ = self._remove_red_aggressive(rgb)
        H, W = gray.shape

        def runs(mask: np.ndarray, minimum: int = 2):
            out=[]; start=None
            for i, on in enumerate(mask):
                if bool(on) and start is None:
                    start=i
                if (not bool(on) or i==len(mask)-1) and start is not None:
                    end=i if not bool(on) else i+1
                    if end-start>=minimum:
                        out.append((start,end))
                    start=None
            return out

        def merge(groups, max_gap, max_span=None):
            out=[]
            for g in groups:
                g=list(g)
                if out and g[0]-out[-1][1] <= max_gap and (max_span is None or g[1]-out[-1][0] <= max_span):
                    out[-1][1]=g[1]
                    out[-1][2]=max(out[-1][2],g[2])
                    out[-1][3]+=g[3]
                else:
                    out.append(g)
            return out

        best=None
        # A second red component is commonly the star / inner seal. Only the
        # largest outer seals are useful as a coordinate frame.
        for _, sx, sy, sw, sh in seals[:2]:
            if sw < 90 or sh < 90:
                continue
            x0=max(0,int(round(sx-sw*.55)))
            x1=min(W,int(round(sx+sw*1.70)))
            y0=max(0,int(round(sy+sh*.06)))
            y1=min(H,int(round(sy+sh*1.12)))
            if x1-x0 < 180 or y1-y0 < 70:
                continue
            ink=(gray[y0:y1,x0:x1] < 180).astype(np.uint8)
            row_counts=ink.sum(axis=1).astype(float)
            smooth=np.convolve(row_counts,np.ones(5,dtype=float)/5.0,mode='same')
            # At 200dpi this is ~8 pixels for a 700px ROI; scale by ROI width so
            # the test remains stable on slightly different page sizes.
            row_thr=max(6.0,(x1-x0)*.010)
            row_runs=runs(smooth>row_thr,3)
            # Prefer lower lines (the company name is usually above the date row),
            # but score every plausible line rather than blindly taking the last.
            for rs,re in reversed(row_runs):
                by0=y0+rs; by1=y0+re
                bh=by1-by0
                peak=float(smooth[rs:re].max()) if re>rs else 0.0
                if not (10 <= bh <= 40 and peak >= max(10.0,row_thr*1.15)):
                    continue
                # Date anchors must sit inside / immediately below the outer seal,
                # not in arbitrary text elsewhere on the page.
                ry=((by0+by1)/2.0-sy)/max(1.0,sh)
                if not .24 <= ry <= 1.03:
                    continue
                band=(gray[max(0,by0-3):min(H,by1+3),x0:x1] < 180).astype(np.uint8)
                col_counts=band.sum(axis=0).astype(float)
                csm=np.convolve(col_counts,np.ones(3,dtype=float)/3.0,mode='same')
                raw=[]
                for cs,ce in runs(csm>1.5,2):
                    raw.append([x0+cs,x0+ce,float(csm[cs:ce].max()),float(csm[cs:ce].sum())])
                # First close antialiasing cracks, then reconnect split pieces of a
                # single Chinese glyph. The latter span cap prevents joining a
                # whole numeric field or two separate anchors.
                groups=merge(raw,3,None)
                groups=merge(groups,16,36)
                chars=[g for g in groups if 7 <= g[1]-g[0] <= 36 and g[3] >= 14]
                if len(chars) != 3:
                    continue
                a,b,c=chars
                d1=b[0]-a[0]; d2=c[0]-b[0]
                if not (36 <= d1 <= 165 and 36 <= d2 <= 165):
                    continue
                ratio=d1/max(1.0,d2)
                if not .52 <= ratio <= 1.88:
                    continue
                h=max(8.0,float(bh))
                g1=(b[0]-a[1])/h; g2=(c[0]-b[1])/h
                if min(g1,g2) < .38:
                    continue
                xmid=(a[0]+c[1])/2.0
                rx=(xmid-sx)/max(1.0,sw)
                if not .12 <= rx <= 1.22:
                    continue
                # Reject dense prose. In the local date span there may be one wide
                # group immediately before 年 (an existing four-digit year), but no
                # additional Chinese-sized groups.
                local=[g for g in groups if a[0]-95 <= g[0] and g[1] <= c[1]+35 and g[3] >= 10]
                extras=[g for g in local if g not in chars]
                if len(extras) > 1:
                    continue
                if extras:
                    q=extras[0]
                    if not (q[1] <= a[0]-2 and 38 <= q[1]-q[0] <= 86 and a[0]-q[1] <= 24):
                        continue
                # The horizontal projection gives the complete row envelope.
                # Use it as the vertical glyph envelope because a red seal may
                # erase most of one anchor (commonly 年) while leaving only two
                # narrow black fragments. Tightening to those fragments would
                # shrink the inferred font size and incorrectly fail geometry.
                boxes=[Box(float(g[0]),float(by0),float(g[1]-g[0]),float(bh)) for g in chars]
                balance=math.exp(-abs(math.log(max(.01,ratio)))/.75)
                seal_center=math.exp(-abs(rx-.62)/.80-abs(ry-.62)/.85)
                sparse=max(0.0,1.0-min(1.0,float(row_counts[rs:re].sum())/max(1.0,(x1-x0)*bh*.18)))
                conf=min(.94,.78+.07*balance+.05*seal_center+.04*sparse)
                score=conf+.025*ry
                if best is None or score>best[0]:
                    year_gap=None
                    if extras:
                        year_gap=float(a[0]-extras[0][1])
                    best=(score,Detection(pno,boxes[0],boxes[1],boxes[2],conf,
                          "印章区年月日横行投影识别（模糊扫描兜底）",year_gap_px=year_gap))
        return None if best is None else best[1]

    def _detect_geometric_row(self, rgb: np.ndarray, pno: int,
                              matches) -> Optional[Detection]:
        """模板全失败时的几何兜底。

        v2 的致命缺陷：这里只找「三个等高、等距的墨块」，不验证内容，
        于是把封面公司名标题当成了落款栏。本版要求候选行必须被
        年/月/日 模板匹配**印证至少 2 个字**，否则一律拒绝。
        """
        gray, _ = self._remove_red(rgb)
        H, W = gray.shape
        mask = (gray < 165).astype(np.uint8) * 255
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        comps = []
        for i in range(1, n):
            x, y, w, h, area = [int(v) for v in stats[i]]
            if not (14 <= h <= 65 and 6 <= w <= 65 and area >= 18):
                continue
            if area / max(1.0, w * h) < 0.05:
                continue
            comps.append((x, y, w, h, area, x + w / 2.0, y + h / 2.0))
        if len(comps) < 3:
            return None
        comps.sort(key=lambda q: q[0])
        seals = self._seal_boxes(rgb)
        best = None
        N = len(comps)
        for ia, a in enumerate(comps):
            ay = a[6]
            for ib in range(ia + 1, N):
                b = comps[ib]
                dx1 = b[0] - a[0]
                if dx1 > 220:
                    break
                if dx1 < 38 or abs(b[6] - ay) > 10:
                    continue
                for ic in range(ib + 1, N):
                    c = comps[ic]
                    dx2 = c[0] - b[0]
                    if dx2 > 220:
                        break
                    if dx2 < 38:
                        continue
                    if abs(c[6] - ay) > 10 or abs(c[6] - b[6]) > 10:
                        continue
                    ratio = dx1 / max(1.0, dx2)
                    if not 0.55 <= ratio <= 1.75:
                        continue
                    hs = (a[3], b[3], c[3])
                    if max(hs) / max(1.0, min(hs)) > 1.8:
                        continue
                    g1, g2 = self._blank_ratios((a[0], a[2], a[3]),
                                                (b[0], b[2], b[3]),
                                                (c[0], c[2], c[3]))
                    if min(g1, g2) < GAP_MIN_RATIO:
                        continue
                    avgsp = (dx1 + dx2) / 2.0
                    ymid = (a[6] + b[6] + c[6]) / 3.0
                    xlo = a[0] - avgsp * 0.7
                    xhi = c[0] + c[2] + avgsp * 0.7
                    ytol = max(12.0, ((a[3] + b[3] + c[3]) / 3.0) * 0.45)
                    nearby = sum(1 for q in comps
                                 if xlo <= q[0] <= xhi and abs(q[6] - ymid) <= ytol)
                    isolation = max(0.0, 1.0 - max(0, nearby - 3) / 6.0)
                    xmid = (a[0] + c[0] + c[2]) / 2.0
                    rel = 0.0
                    for _, sx, sy, sw, sh in seals[:4]:
                        ddx = abs(xmid - (sx + sw / 2.0)) / (sw + 1.0)
                        ddy = abs(ymid - (sy + sh * 0.95)) / (sh + 1.0)
                        rel = max(rel, math.exp(-1.1 * ddx - 1.4 * ddy))
                    sp_score = math.exp(-abs(math.log(max(1.0, avgsp) / 80.0)) / 1.2)
                    pos = 0.15 * (xmid / W) + 0.10 * (ymid / H)
                    score = 0.40 * isolation + 0.25 * rel + 0.20 * sp_score + pos
                    if best is None or score > best[0]:
                        best = (score, a, b, c, nearby)
        if best is None:
            return None
        score, a, b, c, nearby = best
        if score < GEOM_ACCEPT or nearby > 5:
            return None
        cand = Box(a[0], a[1], a[2], a[3]), Box(b[0], b[1], b[2], b[3]), Box(c[0], c[1], c[2], c[3])
        # 必须被模板匹配印证
        if self._corroboration(cand, matches) < GEOM_MIN_CORROBORATION:
            return None
        return Detection(pno, cand[0], cand[1], cand[2],
                         min(0.94, score), "几何行（已通过模板印证）")

    @staticmethod
    def _corroboration(boxes: Tuple[Box, Box, Box],
                       matches: Dict[str, List[tuple]]) -> int:
        """候选行的三个字位置附近，有几个能对上 年/月/日 模板。"""
        hit = 0
        for ch, box in zip(CJK, boxes):
            pad = max(14.0, box.h * 1.2)
            for m in matches.get(ch, []):
                if (box.x - pad <= m[1] <= box.x1 + pad
                        and box.y - pad <= m[2] <= box.y1 + pad):
                    hit += 1
                    break
        return hit

    # ------------------------------------------------------------------
    # 年份格是否已有内容
    # ------------------------------------------------------------------
    def _has_existing_year(self, gray: np.ndarray, d: Detection) -> bool:
        """（保留）整行左侧是否有年份墨迹；现由 mark_fill 逐格判断取代。"""
        h = d.char_h
        cy = d.row_cy
        x0 = max(0, int(d.year.x - max(d.spacing, h * 2.0) * 1.10))
        x1 = max(x0 + 1, int(d.year.x - h * 0.30))
        y0 = max(0, int(cy - h * 0.85))
        y1 = min(gray.shape[0], int(cy + h * 0.85))
        crop = gray[y0:y1, x0:x1]
        if crop.size == 0:
            return False
        m = (crop < 150).astype(np.uint8)
        n, _, stats, _ = cv2.connectedComponentsWithStats(m, 8)
        big = [int(s[4]) for s in stats[1:]
               if s[3] >= h * 0.42 and s[2] >= h * 0.12 and s[4] >= 25]
        return len(big) >= 2

    def _mark_ink_boxes(self, rgb: np.ndarray, d: Detection) -> None:
        """把各汉字框换成实际完整墨迹框，排字与占用判定才准。"""
        for attr, box in (("year_ink", d.year), ("month_ink", d.month), ("day_ink", d.day)):
            pad = 6
            y0 = max(0, int(box.y) - pad); y1 = int(box.y1) + pad
            x0 = max(0, int(box.x) - pad); x1 = int(box.x1) + pad
            ink = self.glyph_ink(rgb[y0:y1, x0:x1], box, pad=pad)
            if ink is None:
                setattr(d, attr, box)
            else:
                setattr(d, attr, Box(ink.x + x0, ink.y + y0, ink.w, ink.h))

    def _detect_date_label(self, rgb: np.ndarray, pno: int, seals) -> Optional[Detection]:
        """常规年月日识别失败时，识别红章附近的“日期”标签。"""
        tpl0 = self.date_label_template
        if tpl0 is None or tpl0.size == 0 or not seals:
            return None
        gray, _ = self._remove_red(rgb)
        H, W = gray.shape
        best = None
        for scale in np.linspace(DATE_LABEL_SCALE_MIN, DATE_LABEL_SCALE_MAX, DATE_LABEL_SCALE_STEPS):
            tpl = cv2.resize(tpl0, None, fx=float(scale), fy=float(scale), interpolation=cv2.INTER_CUBIC)
            th, tw = tpl.shape[:2]
            if th >= H or tw >= W:
                continue
            res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(res)
            x, y = loc
            cx, cy = x + tw / 2.0, y + th / 2.0
            seal_rel = 0.0
            for _, sx, sy, sw, sh in seals[:4]:
                nx = abs(cx - (sx + sw / 2.0)) / max(1.0, sw)
                ny = abs(cy - (sy + sh * 0.72)) / max(1.0, sh)
                seal_rel = max(seal_rel, math.exp(-1.7 * nx - 1.9 * ny))
            total = float(score) + 0.05 * seal_rel
            if best is None or total > best[0]:
                best = (total, float(score), seal_rel, Box(float(x), float(y), float(tw), float(th)))
        if best is None:
            return None
        _, score, seal_rel, box = best
        if score < DATE_LABEL_ACCEPT or seal_rel < 0.34:
            return None

        fake = Box(box.x, box.y, box.w, box.h)
        return Detection(
            pno, fake, fake, fake, min(0.99, score), "日期标签识别（人工处理）",
            fill={"full": True}, year_ink=fake, month_ink=fake, day_ink=fake,
            mode="full_date", label=box,
        )

    # ------------------------------------------------------------------
    # 同版式快速定位
    # ------------------------------------------------------------------
    def detect_near(self, rgb: np.ndarray, pno: int, anchors_pt: Dict[str, List[float]],
                    pad_em: float = 2.2) -> Optional[Detection]:
        """Validate a previously seen layout in three tiny local windows.

        This does *not* copy coordinates blindly. Each 年/月/日 glyph must be
        independently re-matched on the current page. It is substantially faster
        than sliding every template over a large page ROI and is therefore used as
        the first path for repeated stamp-page layouts.
        """
        if not all(k in anchors_pt for k in CJK_MAP):
            return None
        gray, _ = self._remove_red(rgb)
        found = {}
        scores = []
        H, W = gray.shape
        for key, ch in CJK_MAP.items():
            b = anchors_pt[key]
            x0, y0, x1, y1 = [float(v) * self.px_per_pt for v in b]
            hh = max(8.0, y1 - y0)
            pad = max(12.0, hh * pad_em)
            roi = (max(0, int(x0 - pad)), max(0, int(y0 - pad * .65)),
                   min(W, int(x1 + pad)), min(H, int(y1 + pad * .65)))
            matches = self._match_char(gray, roi, self.templates[ch])
            if not matches:
                return None
            best = matches[0]
            score, x, y, w, h = best[:5]
            # A local reuse path is intentionally stricter than global discovery.
            if score < .57:
                return None
            exp_cx=(x0+x1)/2; exp_cy=(y0+y1)/2
            if abs((x+w/2)-exp_cx)>pad*.80 or abs((y+h/2)-exp_cy)>pad*.55:
                return None
            found[key]=Box(float(x),float(y),float(w),float(h));scores.append(float(score))
        a,b,c=found['year'],found['month'],found['day']
        medh=float(np.median([a.h,b.h,c.h]))
        if medh<=0 or max(abs(a.cy-b.cy),abs(a.cy-c.cy),abs(b.cy-c.cy))>max(9.,medh*.42):
            return None
        if not (a.x < b.x < c.x):
            return None
        g1,g2=self._blank_ratios((a.x,a.w,a.h),(b.x,b.w,b.h),(c.x,c.w,c.h))
        if min(g1,g2)<GAP_MIN_RATIO*.82:
            return None
        d=Detection(pno,a,b,c,min(.96,float(np.mean(scores))),"同版式局部复核")
        self._finish(rgb,gray,d)
        return d

    # ------------------------------------------------------------------
    # 主检测入口
    # ------------------------------------------------------------------
    def detect_page(self, rgb: np.ndarray, pno: int,
                    matches_provider=None, full_quality: bool=False) -> Optional[Detection]:
        """单页检测：分级搜索区，先搜最可能的位置，没结果再放大范围。"""
        seals = self._seal_boxes(rgb)
        # Blurry stamp-page scans are common and expensive template matching is
        # least reliable exactly there. A strict projection-based fast path only
        # accepts a sparse three-anchor row tied to the red seal.
        d = self._detect_seal_projection_row(rgb, pno, seals)
        if d is not None:
            gray, _ = self._remove_red(rgb)
            self._finish(rgb, gray, d)
            return d
        gray, last_matches = None, {c: [] for c in CJK}
        for rois in self._roi_groups(rgb, seals):
            gray, matches = self._char_matches(rgb, rois, full_quality=full_quality)
            for c in CJK:
                last_matches[c].extend(matches[c])
            d = self._detect_image_row(rgb, pno, matches, seals)
            if d is None:
                d = self._detect_pair_row(rgb, pno, matches, seals)
            if d is not None:
                self._finish(rgb, gray, d)
                return d
            if matches_provider:
                matches_provider(pno, matches)
        d = self._detect_geometric_row(rgb, pno, last_matches)
        if d is not None:
            self._finish(rgb, gray, d)
            return d
        return self._detect_date_label(rgb, pno, seals)

    def _finish(self, rgb: np.ndarray, gray: np.ndarray, d: Detection) -> None:
        self._mark_ink_boxes(rgb, d)
        # Candidate geometry only. Field decisions belong to evidence.py.

