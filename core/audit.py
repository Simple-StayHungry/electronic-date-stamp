from __future__ import annotations
from collections import Counter,defaultdict
import hashlib
import math
import fitz
import numpy as np
from .evidence import render, extract_characters


def _image_hashes(doc,page):
    values=[]
    for meta in page.get_images(full=True):
        if meta[0]: values.append(hashlib.sha256(doc.xref_stream_raw(meta[0]) or b'').hexdigest())
    return sorted(values)


def _original_text_signature(page):
    return Counter((c.text,round(c.bbox[0],2),round(c.bbox[1],2),round(c.bbox[2],2),round(c.bbox[3],2),
                    round(c.size,2),c.font,c.visible) for c in extract_characters(page))


def audit_pdf(source,output,plan,dpi=144,progress=None):
    """Independent read-back and raster audit per field, not only per date line."""
    errors=[]; pages=[];checks=[];groups=defaultdict(list)
    if any(op.font!='times' or not op.text.isdigit() for op in plan.operations):
        errors.append('生成计划包含非阿拉伯数字或非 Times New Roman 写入。')
    for op in plan.operations:groups[op.page].append(op)
    z=dpi/72
    with fitz.open(source) as a,fitz.open(output) as b:
        if len(a)!=len(b):return {'pass':False,'errors':['页数变化。'],'pages':[],'fields':[]}
        for pno in range(1,len(a)+1):
            pa,pb=a[pno-1],b[pno-1]
            if list(pa.rect)!=list(pb.rect) or list(pa.mediabox)!=list(pb.mediabox) or list(pa.cropbox)!=list(pb.cropbox) or pa.rotation!=pb.rotation:
                errors.append(f'第{pno}页尺寸、裁切或旋转发生变化。')
            if _image_hashes(a,pa)!=_image_hashes(b,pb):errors.append(f'第{pno}页原始图像流发生变化。')
            before_sig=_original_text_signature(pa);after_sig=_original_text_signature(pb)
            if before_sig-after_sig:errors.append(f'第{pno}页原有文字对象被删除或移动。')
            ra,rb=render(pa,dpi),render(pb,dpi)
            if ra.shape!=rb.shape:
                errors.append(f'第{pno}页渲染尺寸变化。');continue
            diff=np.any(ra!=rb,axis=2)
            protected=np.zeros(diff.shape,dtype=bool)
            chars=extract_characters(pb)
            field_info=[]
            for op in groups[pno]:
                r=op.bbox
                x0=max(0,int(math.floor(r[0]*z)));x1=min(diff.shape[1],int(math.ceil(r[2]*z)))
                y0=max(0,int(math.floor(r[1]*z)));y1=min(diff.shape[0],int(math.ceil(r[3]*z)))
                protected[y0:y1,x0:x1]=True
                area=int(diff[y0:y1,x0:x1].sum())
                # Verify exact new text at the planned baseline (including rotations).
                matched=[c for c in chars if c.visible and c.horizontal and
                         abs(c.origin[1]-op.baseline)<.30 and
                         op.x-.2<=c.origin[0]<=op.bbox[2] and
                         abs(c.size-op.size)<.20]
                matched.sort(key=lambda c:c.origin[0])
                extracted=''.join(c.text for c in matched)
                text_ok=extracted.startswith(op.text)
                # Ensure this is an additional field, not a read-back of pre-existing content.
                # The operation bbox contains antialias / audit padding and may
                # intentionally touch the following 年/月/日 anchor on compact source
                # layouts. Test pre-existing text only across the *actual inserted
                # glyph run*, otherwise a nearby '日' can be mistaken for overwritten
                # content even though the new digits stop before it.
                ink_right=(max((c.bbox[2] for c in matched),default=op.x)+.12)
                old=[c for c in extract_characters(pa) if abs(c.origin[1]-op.baseline)<.30 and
                     op.x-.2<=c.origin[0]<ink_right and c.text.strip()]
                additional=not old
                regular=all('bold' not in c.font.lower() and 'italic' not in c.font.lower() for c in matched)
                if op.font=='times':
                    regular=regular and all('timesnewroman' in c.font.lower().replace(' ','') for c in matched)
                ok=area>=3 and text_ok and regular and additional
                item={'page':pno,'row':op.row_id,'key':op.key,'text':op.text,'bbox':r,
                      'changed_pixels':area,'readback':extracted,'regular_font':regular,
                      'new_field':additional,'pass':ok}
                field_info.append(item);checks.append(item)
                if not ok:errors.append(f'第{pno}页 {op.key} 的逐字段可见性、字体或文字回读未通过。')
            outside=int((diff&~protected).sum())
            total=int(diff.sum())
            if outside:errors.append(f'第{pno}页有 {outside} 个像素在允许字段框外改变。')
            pages.append({'page':pno,'changed_pixels':total,'outside_pixels':outside,
                          'unchanged':total==0,'fields':len(field_info)})
            if progress:progress(pno,len(a))
    return {'pass':not errors,'dpi':dpi,'errors':errors,'pages':pages,'fields':checks,
            'changed_pages':[p['page'] for p in pages if p['changed_pixels']],
            'field_count':len(checks),'outside_pixels':sum(p['outside_pixels'] for p in pages)}
