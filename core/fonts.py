from __future__ import annotations
from pathlib import Path
import os
import fitz

class FontResolver:
    """No substitutes and no fonts bundled with the application.

    System font first; a verified embedded font may be reused in memory only
    if it supplies every digit. A selected local font is validated in the same
    way. Font paths/bytes never appear in a downloadable report.
    """
    def __init__(self, selected: str | None = None):
        self.times = None
        self.source = "未找到 Times New Roman Regular"
        self.cjk = None
        self.cjk_source = ""
        self.cjk_real = False
        self._try_paths(selected)

    @staticmethod
    def validate(font: fitz.Font) -> bool:
        name = font.name.lower().replace(' ', '').replace('-', '')
        flags = font.flags
        return ('timesnewroman' in name and
                not any(flags.get(k,0) for k in ('bold','italic','fake-bold','fake-italic','substitute','never-embed')) and
                all(font.has_glyph(ord(c)) for c in '0123456789'))

    def _try_paths(self, selected):
        paths=[selected, os.environ.get('LUOKUAN_TIMES_PATH'),
          '/Applications/Microsoft Word.app/Contents/Resources/DFonts/Times New Roman.ttf',
          '/Applications/Microsoft Word.app/Contents/Resources/Fonts/Times New Roman.ttf',
          '/Library/Fonts/Microsoft/Times New Roman.ttf',
          '~/Library/Fonts/Microsoft/Times New Roman.ttf',
          '/Library/Fonts/Times New Roman.ttf', '~/Library/Fonts/Times New Roman.ttf',
          '/System/Library/Fonts/Supplemental/Times New Roman.ttf',
          str(Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/times.ttf')]
        for raw in paths:
            if not raw: continue
            p=Path(raw).expanduser()
            if not p.is_file(): continue
            try:
                f=fitz.Font(fontfile=str(p))
                if self.validate(f):
                    self.times=f; self.source='本机 Times New Roman Regular'; return
            except Exception: pass
        if selected: raise ValueError('所选字体不是可嵌入且包含全部数字的 Times New Roman Regular。')

    def use_document(self, doc):
        if self.times is not None: return
        seen=set()
        for p in doc:
            for meta in p.get_fonts(full=True):
                x=meta[0]
                if not x or x in seen: continue
                seen.add(x)
                if 'times' not in meta[3].lower(): continue
                try:
                    _,_,_,buf=doc.extract_font(x)
                    if not buf or len(buf)>20*1024*1024: continue
                    f=fitz.Font(fontbuffer=buf)
                    if self.validate(f):
                        self.times=f; self.source='原 PDF 内嵌 Times New Roman Regular'; return
                except Exception: continue

    def require_times(self):
        if self.times is None:
            raise ValueError('未找到真正的 Times New Roman Regular。可继续查看识别结果；生成前请在字体设置中选择本机字体文件。')
        return self.times

    def cjk_font(self, require_real: bool = False):
        """Return a metrics font for 年/月/日 without relying on substitution.

        A real system Song font is preferred. If unavailable, PyMuPDF's built-in
        Simplified-Chinese base font is used *only* for metrics; writer.py emits
        those glyphs through page.insert_text(fontname='china-s'), because
        TextWriter cannot create substitute CJK fonts on some PyMuPDF builds.
        """
        if self.cjk is not None:
            return self.cjk if (self.cjk_real or not require_real) else None
        for raw in ['/System/Library/Fonts/Supplemental/Songti.ttc',
                    '/System/Library/Fonts/STSong.ttf',
                    str(Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/simsun.ttc')]:
            p=Path(raw)
            if not p.exists(): continue
            try:
                f=fitz.Font(fontfile=str(p))
                if all(f.has_glyph(ord(c)) for c in '年月日'):
                    self.cjk=f; self.cjk_source='本机宋体'; self.cjk_real=True; return f
            except Exception:
                pass
        if require_real:
            return None
        try:
            self.cjk=fitz.Font('china-s')
            self.cjk_source='PDF 内置中文基础字体'
            self.cjk_real=False
            return self.cjk
        except Exception:
            return None

    def info(self):
        return {'ready': self.times is not None, 'name':'Times New Roman Regular',
                'source':self.source, 'cjk_source':self.cjk_source}
