from __future__ import annotations
from collections import defaultdict
from pathlib import Path
from dataclasses import asdict
import os
import json
import shutil
import tempfile
import fitz
from .models import file_hash, VERSION
from .document import has_signatures
from .pdfsafe import raw_signature_hint, is_structural_error, repair_shadow


def isolate_original_state(page):
    """Append wrapper streams; never modify a possibly shared original stream."""
    doc=page.parent
    original=list(page.get_contents())
    if not original:return
    head=doc.get_new_xref();doc.update_object(head,'<<>>');doc.update_stream(head,b'q\n')
    tail=doc.get_new_xref();doc.update_object(tail,'<<>>');doc.update_stream(tail,b'\nQ\n')
    refs=[head]+original+[tail]
    doc.xref_set_key(page.xref,'Contents','['+' '.join(f'{x} 0 R' for x in refs)+']')


def write_operations(doc,operations,fonts,allow_non_digit=False):
    groups=defaultdict(list)
    for op in operations:
        if not allow_non_digit and (op.font!='times' or not op.text.isdigit()):
            raise ValueError('合规保护：电子落款只允许写入 Times New Roman 阿拉伯数字。')
        groups[op.page].append(op)
    for pno,ops in groups.items():
        page=doc[pno-1]
        isolate_original_state(page)
        rot=page.rotation
        signed=rot if rot<=180 else rot-360
        derot=page.derotation_matrix
        original_crop=fitz.Rect(page.cropbox)
        raw_crop=doc.xref_get_key(page.xref,'CropBox')
        raw_rotation=doc.xref_get_key(page.xref,'Rotate')
        # TextWriter and non-zero CropBox origins must not share mixed coordinate
        # systems. Convert through PDF coordinates, compose on the full MediaBox,
        # and restore the original boxes before any save or render.
        page.set_rotation(0)
        crop_to_pdf=~page.transformation_matrix
        page.set_cropbox(page.mediabox)
        pdf_to_media=page.transformation_matrix
        try:
            for op in ops:
                pt=fitz.Point(op.x,op.baseline)*derot*crop_to_pdf*pdf_to_media
                kwargs={'color':(0,0,0),'render_mode':0,'overlay':True}
                if rot:kwargs['morph']=(pt,fitz.Matrix(signed))
                if op.font=='times':
                    font=fonts.require_times()
                    writer=fitz.TextWriter(page.rect)
                    writer.append(pt,op.text,font=font,fontsize=op.size)
                    writer.write_text(page,**kwargs)
                elif op.font=='cjk' and not fonts.cjk_real:
                    # PyMuPDF TextWriter may raise "substitute font creation is not
                    # implemented" for CJK base fonts. page.insert_text supports the
                    # PDF built-in Simplified-Chinese font directly and is stable
                    # across macOS / Windows without shipping a font file.
                    page.insert_text(pt,op.text,fontname='china-s',fontsize=op.size,**kwargs)
                else:
                    font=fonts.cjk_font(require_real=True)
                    if font is None:raise ValueError('无法建立中文年月日字体资源。')
                    writer=fitz.TextWriter(page.rect)
                    writer.append(pt,op.text,font=font,fontsize=op.size)
                    writer.write_text(page,**kwargs)
        finally:
            page.set_cropbox(original_crop)
            page.set_rotation(rot)
            # Preserve whether a box was inherited rather than explicitly set.
            doc.xref_set_key(page.xref,'CropBox',raw_crop[1] if raw_crop[0]!='null' else 'null')
            doc.xref_set_key(page.xref,'Rotate',raw_rotation[1] if raw_rotation[0]!='null' else 'null')



def build(source,destination,plan,fonts,allow_partial=False,progress=None):
    """Transactional build: immutable source -> temporary PDF -> audit -> publish.

    An output is downloadable only after QA. Failed output and stale previous
    output are not returned as success. A pending row needs explicit partial consent.
    """
    from .audit import audit_pdf
    src=Path(source).resolve();dest=Path(destination).resolve()
    if src==dest:raise ValueError('不覆盖原文件。请选择新的输出文件名。')
    if file_hash(src)!=plan.source_hash:raise ValueError('源文件在识别后发生变化，请重新上传。')
    if plan.pending and not allow_partial:raise ValueError('仍有待确认日期栏；请先确认，或明确选择只导出已确认部分。')
    if any(op.font!='times' or not op.text.isdigit() for op in plan.operations):
        raise ValueError('合规保护：生成计划包含非数字内容，已阻止导出。')
    if not plan.operations:raise ValueError('没有已确认的待补字段，不生成空的“成功”结果。')
    dest.parent.mkdir(parents=True,exist_ok=True)
    fd,tmpname=tempfile.mkstemp(prefix='.luokuan-',suffix='.pdf',dir=dest.parent)
    os.close(fd);tmp=Path(tmpname);tmp.unlink()
    try:
        with fitz.open(src) as doc:
            if raw_signature_hint(src) or has_signatures(doc, src):raise ValueError('文件含数字签名，不支持生成修改件。')
            write_operations(doc,plan.operations,fonts)
            doc.save(tmp,garbage=0,deflate=True)
        report=audit_pdf(src,tmp,plan,progress=progress)
        if not report['pass']:
            raise ValueError('生成校验未通过，未发布成品：'+'；'.join(report['errors'][:6]))
        report.update({'version':VERSION,'source_sha256':plan.source_hash,
                       'plan_sha256':plan.digest,'output_sha256':file_hash(tmp),
                       'date':plan.date,
                       'page_date_overrides':{str(x['page']):x['effective_date'] for x in plan.rows if x.get('date_source')=='page'},
                       'font':fonts.info(),'pending_rows':plan.pending,
                       'skipped_rows':plan.skipped,'manual_rows':[x['id'] for x in plan.rows if x.get('status')=='manual'],
                       'optional_unselected_rows':plan.optional,
                       'warnings':plan.warnings,'operations':[asdict(x) for x in plan.operations],
                       'scope':'校验通过只表示此次写入与保全通过，不代表未选页面也已完成。'})
        os.replace(tmp,dest)
        report_path=dest.with_suffix('.audit.json')
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        return report
    finally:
        tmp.unlink(missing_ok=True)
