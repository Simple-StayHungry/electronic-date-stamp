from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any
import hashlib
import json

VERSION = "22.1.0"
KEYS = ("year", "month", "day")
LABELS = {"year": "年份", "month": "月份", "day": "日期", "full": "自由日期栏", "grid": "方格日期"}

@dataclass
class Character:
    text: str
    bbox: list[float]
    origin: list[float]
    size: float
    font: str
    visible: bool
    horizontal: bool = True
    @property
    def cx(self): return (self.bbox[0] + self.bbox[2]) / 2
    @property
    def cy(self): return (self.bbox[1] + self.bbox[3]) / 2

@dataclass
class FieldEvidence:
    state: str  # present / empty / protected / uncertain; missing text alone is not empty.
    value: str | None
    source: str
    bbox: list[float]
    reasons: list[str] = field(default_factory=list)
    text_chars: list[Character] = field(default_factory=list)
    ink_ratio: float = 0.0
    red_ratio: float = 0.0

@dataclass
class DateRow:
    id: str
    page: int
    mode: str
    anchors: dict[str, list[float]]  # Visible page coordinates, pt, [x0,y0,x1,y1].
    region: list[float]
    score: float
    method: str
    fields: dict[str, FieldEvidence]
    geometry_ok: bool
    review_reasons: list[str] = field(default_factory=list)
    native_size: float | None = None
    native_baseline: float | None = None
    group: str | None = None
    references: list[int] = field(default_factory=list)
    reference_gap_em: float | None = None
    grid_cells: list[list[float]] = field(default_factory=list)
    grid_style: str | None = None

@dataclass
class Analysis:
    source_hash: str
    page_sizes: list[list[float]]
    rotations: list[int]
    rows: list[DateRow]
    signed: bool = False
    notes: list[str] = field(default_factory=list)
    @property
    def page_count(self): return len(self.page_sizes)

@dataclass
class Operation:
    row_id: str
    page: int
    key: str
    text: str
    font: str
    x: float
    baseline: float
    size: float
    bbox: list[float]
    anchor_gap: float | None = None

@dataclass
class Plan:
    date: str
    source_hash: str
    operations: list[Operation]
    rows: list[dict[str, Any]]
    pending: list[str]
    skipped: list[str]
    optional: list[str]
    warnings: list[str]
    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True,
                                        ensure_ascii=False).encode()).hexdigest()

def file_hash(path) -> str:
    h=hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda:f.read(1024*1024), b''): h.update(b)
    return h.hexdigest()

def union_rect(rects):
    rects=list(rects)
    return [min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects)]
