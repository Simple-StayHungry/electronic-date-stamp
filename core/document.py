from __future__ import annotations
from pathlib import Path
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
import fitz
from .models import Analysis, file_hash, KEYS
from .locator import DateRowLocator
from .evidence import extract_characters, native_candidates, visual_candidate, make_row, render
from .pdfsafe import raw_signature_hint, has_widget_signature, is_structural_error, repair_shadow

def has_signatures(doc, path=None):
    # PDF signatures must expose /ByteRange in the serialized file. Walking every
    # page widget is both redundant for our guard and pathologically slow on some
    # office-generated PDFs with malformed /Annots. Keep the upload path O(file).
    return bool(path and raw_signature_hint(path))


def analyze_resilient(path, recovery_path, progress=None, assets=None, recovery_progress=None):
    """Analyze the original PDF, falling back to an analysis-only repaired copy.

    The source hash always remains the original file hash. A repaired shadow is
    used only to read coordinates / render previews; it never silently replaces
    the uploaded source.
    """
    try:
        return analyze(path, progress=progress, assets=assets), str(path), None
    except Exception as exc:
        if not is_structural_error(exc):
            raise
        if recovery_progress:
            recovery_progress()
        method = repair_shadow(path, recovery_path)
        a = analyze(recovery_path, progress=progress, assets=assets)
        a.source_hash = file_hash(path)
        a.signed = a.signed or raw_signature_hint(path)
        a.notes.insert(0, '源 PDF 的交叉引用结构存在异常；识别使用临时恢复副本，上传原件未被改写。')
        return a, str(recovery_path), method

def _features(r):
    a=r.anchors;h=float(np.median([a[k][3]-a[k][1] for k in KEYS]))
    return np.array([(a['month'][0]-a['year'][0])/h,(a['day'][0]-a['month'][0])/h,
                     (a['year'][2]-a['year'][0])/h,(a['month'][2]-a['month'][0])/h,
                     (a['day'][2]-a['day'][0])/h,math.log(h)])

def cross_check(rows):
    """Conservative document grouping; never copies coordinates or semantic values.

    References supply optional *relative spacing* only, with >=2 consistent donors.
    A large absolute coordinate change, a different font scale, or a missing
    anchor is never repaired by blindly pasting another page's template.
    """
    for row in rows:
        row.group=None;row.references=[];row.reference_gap_em=None
    groups=[]
    for r in rows:
        if r.mode!='ymd' or not r.geometry_ok: continue
        f=_features(r);chosen=None
        for group in groups:
            centroid=np.median([_features(x) for x in group],axis=0)
            delta=abs(f-centroid)
            if max(delta[:2])<.24 and max(delta[2:5])<.20 and delta[5]<.13:
                chosen=group;break
        if chosen is None: chosen=[];groups.append(chosen)
        chosen.append(r)
    for i,group in enumerate(groups,1):
        for r in group:r.group=f'L{i}'
        # Repeated geometry is useful corroboration even when every date slot is
        # blank. It never copies coordinates or values; it only tells the planner
        # that two or more peer pages independently found the same layout.
        if len(group) >= 3:
            for r in group:
                r.references = sorted({p.page for p in group if p.id != r.id})
        donors=[]
        for r in group:
            if r.review_reasons or not all(f.state=='present' for f in r.fields.values()): continue
            if not all(f.value and f.value.isdigit() for f in r.fields.values()): continue
            f=r.fields['year']
            size=r.native_size
            if not size or not f.text_chars: continue
            right=max(c.bbox[2] for c in f.text_chars)
            gap=(r.anchors['year'][0]-right)/size
            if .22<=gap<=1.1:donors.append((r,gap))
        if len(donors)<2:continue
        vals=np.array([g for _,g in donors]);med=float(np.median(vals))
        if float(np.max(abs(vals-med)))>.16:continue
        for r in group:
            peers=[p.page for p,g in donors if p.id!=r.id]
            if len(peers)<2:continue
            r.references=sorted(set(peers));r.reference_gap_em=med

_TLS=threading.local()

def _worker_locator(assets=None):
    key=str(assets or '')
    if getattr(_TLS,'locator_key',None)!=key:
        _TLS.locator=DateRowLocator(assets_dir=assets)
        _TLS.locator_key=key
    return _TLS.locator

def _worker_doc(path):
    """Keep one read-only PyMuPDF document per analysis thread.

    Thread-local documents avoid reopening the whole PDF once per page. On 20-50 page office PDFs that
    overhead is noticeable and can dominate fast native-text pages. Each worker
    is serial, so a thread-local document is safe and cuts the common upload path
    to only a handful of opens.
    """
    key=str(path)
    if getattr(_TLS,'doc_key',None)!=key:
        old=getattr(_TLS,'doc',None)
        try:
            if old is not None: old.close()
        except Exception:
            pass
        _TLS.doc=fitz.open(path)
        _TLS.doc_key=key
    return _TLS.doc

def _analyze_page(path, idx, assets=None):
    locator=_worker_locator(assets)
    doc=_worker_doc(path)
    p=doc.load_page(idx)
    pno=idx+1
    psize=[float(p.rect.width),float(p.rect.height)]
    rotation=int(p.rotation)
    chars=extract_characters(p)
    cands=native_candidates(chars,pno,p.rect)
    rgb=render(p)
    if not cands:
        cands=visual_candidate(locator,rgb,pno)
        if not cands:
            # Rare difficult scans get a full-resolution matching pass; the
            # common path stays downsampled for speed and memory.
            cands=visual_candidate(locator,rgb,pno,full_quality=True)
    rows=[make_row(c,rgb,chars,pno,j+1) for j,c in enumerate(cands)]
    return idx,psize,rotation,rows

def analyze(path, progress=None, assets=None):
    """Parallel page analysis with a fast common path and high-quality fallback.

    Each worker opens its own PyMuPDF document; page objects never cross threads.
    OpenCV's own nested thread pool is disabled in locator.py, so two page workers
    give predictable speed on laptops without the old memory / stall behaviour.
    """
    with fitz.open(path) as doc:
        if doc.needs_pass: raise ValueError('PDF 已加密，请先使用有权限的解密副本。')
        total=len(doc)
        if not 1<=total<=300:raise ValueError('本版支持 1 至 300 页；大文件请分批处理。')
        signed=has_signatures(doc,path)
    workers=int(os.environ.get('LUOKUAN_ANALYSIS_WORKERS','4') or '4')
    workers=max(1,min(4,workers,total))
    results=[None]*total
    done=0
    with ThreadPoolExecutor(max_workers=workers,thread_name_prefix='date-scan') as pool:
        futures=[pool.submit(_analyze_page,str(path),idx,assets) for idx in range(total)]
        for fut in as_completed(futures):
            idx,psize,rotation,page_rows=fut.result()
            results[idx]=(psize,rotation,page_rows)
            done+=1
            if progress:progress(done,total)
    sizes=[];rotations=[];rows=[]
    for item in results:
        psize,rotation,page_rows=item
        sizes.append(psize);rotations.append(rotation);rows.extend(page_rows)
    rows.sort(key=lambda r:(r.page,r.region[1],r.region[0]))
    cross_check(rows)
    notes=['没有识别到日期栏的页面不会自动写入，可在页面预览中手动框选年月日。',
           '只有原件已经提供年月日、分隔符或数字格结构时才自动补数字；自由日期栏保持原样。',
           '页面扫描并行执行；重复打开 PDF 的开销已移除，困难页才升级高质量复核。',
           '保护原有黑色字形不等于确认原日期与本次日期相同。']
    if signed:notes.append('检测到有效 PDF 数字签名：日期仍自动识别与预览，但为避免签名失效，不生成修改件。请使用未签名原件。')
    return Analysis(file_hash(path),sizes,rotations,rows,signed,notes)
