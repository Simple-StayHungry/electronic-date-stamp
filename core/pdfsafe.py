from __future__ import annotations
from pathlib import Path
import re
import shutil
import subprocess
import fitz

STRUCTURAL_MARKERS = (
    'cannot find object in xref', 'bad xref', 'xref', 'object out of range',
    'syntax error', 'format error', 'cannot load object', 'object not found',
    'cannot find page', 'broken xref', 'invalid xref',
)


def is_structural_error(exc: BaseException | str) -> bool:
    text = str(exc).lower()
    return any(x in text for x in STRUCTURAL_MARKERS)


def raw_signature_hint(path) -> bool:
    """Return True only for a *populated* PDF cryptographic signature.

    Merely seeing ``/ByteRange`` is too broad: some office / signing systems
    leave empty signature dictionaries or templates in an otherwise editable
    PDF. A real signed revision has four concrete byte-range numbers describing
    two non-empty signed segments separated by the signature contents.
    """
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError:
        return False
    if b'/ByteRange' not in data:
        return False
    size = len(data)
    for m in re.finditer(br'/ByteRange\s*\[([^\]]+)\]', data):
        nums = [int(x) for x in re.findall(br'\d+', m.group(1))[:4]]
        if len(nums) != 4:
            continue
        a, b, c, d = nums
        # Normal signatures cover [0,b) and [c,c+d), with the hex /Contents
        # stored in the non-zero gap. Empty placeholders usually contain zeros.
        if a != 0 or b <= 0 or c <= b or d <= 0:
            continue
        if c + d > size + 8:
            continue
        if c - b < 16:
            continue
        return True
    return False


def has_widget_signature(doc) -> bool:
    """Inspect reachable page widgets only; never enumerate every xref object."""
    for p in doc:
        try:
            widgets = p.widgets()
            if widgets is None:
                continue
            for w in widgets:
                try:
                    if w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE and w.field_value:
                        return True
                except Exception:
                    # An unreadable nonessential widget must not make upload fail.
                    continue
        except Exception:
            # Malformed /Annots arrays are common in PDFs exported by office /
            # signing systems. The raw ByteRange check remains the guard.
            continue
    return False


def repair_shadow(source, destination) -> str:
    """Create a temporary *analysis-only* structurally normalized copy.

    The user's source is never replaced. PyMuPDF rewrite is preferred because it
    preserves PDF coordinates. Ghostscript is a last-resort reader recovery path.
    Returns the recovery method label.
    """
    src, dst = Path(source), Path(destination)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    try:
        with fitz.open(src) as doc:
            if doc.needs_pass:
                raise ValueError('PDF 已加密，请先使用有权限的解密副本。')
            # clean + garbage rebuilds reachable objects and the xref table.
            doc.save(dst, garbage=4, clean=1, deflate=1, deflate_images=0,
                     deflate_fonts=0, no_new_id=1, preserve_metadata=1,
                     use_objstms=0)
        # Force a second open and a cheap page traversal before accepting it.
        with fitz.open(dst) as check:
            _ = len(check)
            for p in check:
                _ = p.rect
        return 'PyMuPDF 结构恢复'
    except Exception as first:
        dst.unlink(missing_ok=True)
        gs = shutil.which('gs')
        if not gs:
            raise ValueError(f'PDF 内部结构异常，自动恢复失败：{first}') from first
        cmd = [gs, '-q', '-dNOPAUSE', '-dBATCH', '-dSAFER', '-sDEVICE=pdfwrite',
               '-dCompatibilityLevel=1.7', '-dDetectDuplicateImages=true',
               '-dCompressFonts=true', f'-sOutputFile={dst}', str(src)]
        cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=90)
        if cp.returncode or not dst.is_file() or dst.stat().st_size < 100:
            detail = (cp.stderr or cp.stdout or str(first)).strip().splitlines()[-1:]
            raise ValueError('PDF 内部交叉引用表损坏，自动恢复失败。' + (' '+detail[0] if detail else '')) from first
        with fitz.open(dst) as check:
            _ = len(check)
            for p in check:
                _ = p.rect
        return 'Ghostscript 结构恢复'
