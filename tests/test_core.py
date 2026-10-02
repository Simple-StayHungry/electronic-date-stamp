from __future__ import annotations
from dataclasses import replace,asdict
from datetime import date
from pathlib import Path
import copy
import json
import fitz
import numpy as np
import pytest
from core.models import DateRow,FieldEvidence,Analysis,Operation,Plan,file_hash
from core.fonts import FontResolver
from core.document import analyze,cross_check
from core.evidence import inspect_field,render,extract_characters
from core.planner import make_plan,layout_row
from core.writer import write_operations,build
from core.audit import audit_pdf

STAMP=date(2026,9,12)

def source_pdf(path,fonts,rotation=0,crop=False,values=None,residual=False):
    d=fitz.open()
    if rotation in (90,270):p=d.new_page(width=842,height=595)
    else:p=d.new_page(width=595,height=842)
    if crop:
        # A non-zero CropBox origin, maintained in the output.
        p=d[0];p.set_cropbox(fitz.Rect(15,20,p.rect.width-10,p.rect.height-12))
    p.set_rotation(rotation)
    anchors={'year':(320,400),'month':(368,400),'day':(416,400)}
    ops=[]
    for key,text in [('year','年'),('month','月'),('day','日')]:
        x,y=anchors[key];ops.append(Operation('source',1,key,text,'cjk',x,y,12,[x,y-15,x+15,y+4]))
    # An irrelevant header is intentionally far from the date.
    ops.append(Operation('source',1,'header','DOCUMENT','times',60,70,12,[60,55,150,75]))
    for key,text in (values or {}).items():
        x,y=anchors[key];width=fonts.times.text_length(text,fontsize=12)
        ops.append(Operation('source',1,key,text,'times',x-8-width,y,12,[x-8-width,y-15,x-8,y+4]))
    write_operations(d,ops,fonts,allow_non_digit=True)
    if residual:
        x=d.get_new_xref();d.update_object(x,'<<>>');d.update_stream(x,b'0.5 0 0 0.5 50 60 cm\n')
        old=d[0].get_contents();d.xref_set_key(d[0].xref,'Contents','['+' '.join(f'{z} 0 R' for z in old+[x])+']')
    d.save(path);d.close()
    return path

@pytest.mark.parametrize('rotation',[0,90,180,270])
@pytest.mark.parametrize('crop',[False,True])
def test_rotation_crop_and_exact_preservation(tmp_path,fonts,rotation,crop):
    src=source_pdf(tmp_path/'source.pdf',fonts,rotation,crop)
    a=analyze(src)
    assert len(a.rows)==1
    p=make_plan(a,STAMP,fonts)
    assert not p.pending,p.rows
    assert len(p.operations)==3
    out=tmp_path/'output.pdf'
    rep=build(src,out,p,fonts)
    assert rep['pass'] and rep['outside_pixels']==0
    assert all(x['pass'] for x in rep['fields'])
    a2=analyze(out);p2=make_plan(a2,STAMP,fonts)
    assert not p2.operations,'Second application must not duplicate dates.'

@pytest.mark.parametrize('rotation',[0,270])
def test_residual_graphics_state(tmp_path,fonts,rotation):
    src=source_pdf(tmp_path/'source.pdf',fonts,rotation,residual=True)
    a=analyze(src);plan=make_plan(a,STAMP,fonts)
    r=build(src,tmp_path/'out.pdf',plan,fonts)
    assert r['pass'] and r['outside_pixels']==0

@pytest.mark.parametrize('size',[8,10,12,16,20])
def test_full_blank_year_gap_is_scale_aware(fonts,size):
    h=size*.915
    anchors={'year':[300,300-h,300+h,300],
             'month':[300+size*3.8,300-h,300+size*3.8+h,300],
             'day':[300+size*7.6,300-h,300+size*7.6+h,300]}
    fields={
      'year':FieldEvidence('empty',None,'visual_blank',[300-5*h,300-h,299,303]),
      'month':FieldEvidence('empty',None,'visual_blank',[300+h+1,300-h,anchors['month'][0]-1,303]),
      'day':FieldEvidence('empty',None,'visual_blank',[anchors['month'][2]+1,300-h,anchors['day'][0]-1,303])}
    r=DateRow('r',1,'ymd',anchors,[240,275,450,310],.9,'test',fields,True)
    ops,geo,issues=layout_row(r,STAMP,fonts)
    assert not issues
    op=next(o for o in ops if o.key=='year')
    assert op.anchor_gap>=max(2.6,.32*op.size)-.001
    assert geo['year_gap']>2.4


def test_hidden_text_is_not_blank(tmp_path,fonts):
    src=source_pdf(tmp_path/'source.pdf',fonts)
    with fitz.open(src) as doc:
        doc[0].insert_text((288,400),'2026',fontname='tiro',fontsize=12,render_mode=3)
        hidden=tmp_path/'hidden.pdf';doc.save(hidden)
    a=analyze(hidden);row=a.rows[0]
    assert row.fields['year'].state=='uncertain'
    assert row.fields['year'].text_chars
    p=make_plan(a,STAMP,fonts,{row.id:{'mode':'confirm','fields':{'year':'fill_confirmed'}}})
    assert row.id in p.pending
    assert not p.operations


def test_wrong_existing_date_requires_explicit_preservation(tmp_path,fonts):
    src=source_pdf(tmp_path/'source.pdf',fonts,values={'year':'2025'})
    a=analyze(src);row=a.rows[0]
    p=make_plan(a,STAMP,fonts)
    assert p.pending and not p.operations
    p2=make_plan(a,STAMP,fonts,{row.id:{'mode':'confirm','fields':{'year':'keep'}}})
    assert not p2.pending and {o.key for o in p2.operations}=={'month','day'}


def test_invisible_digit_cannot_be_force_overwritten(tmp_path,fonts):
    src=source_pdf(tmp_path/'source.pdf',fonts,values={'month':'9'})
    a=analyze(src);row=a.rows[0]
    p=make_plan(a,STAMP,fonts,{row.id:{'mode':'confirm','fields':{'month':'fill_confirmed'}}})
    assert p.pending and not p.operations


def test_no_text_does_not_imply_blank():
    rgb=np.full((160,300,3),255,np.uint8)
    rgb[68:95,80:94]=0
    e=inspect_field(rgb,[],[20,15,50,40],12,'month')
    assert e.state=='protected'
    assert e.value is None


def test_edge_stroke_is_uncertain_not_a_known_digit():
    rgb=np.full((130,200,3),255,np.uint8)
    rgb[50:78,55:58]=0
    e=inspect_field(rgb,[],[20,15,40,35],12,'month')
    assert e.state in ('uncertain','empty')
    assert e.state!='present'


def test_occluded_empty_region_is_not_auto_blank():
    rgb=np.full((140,220,3),255,np.uint8)
    rgb[45:96,60:130]=[220,20,20]
    e=inspect_field(rgb,[],[20,15,50,36],12,'month')
    assert e.state=='uncertain' and e.red_ratio>.2


def test_font_validator_rejects_bold_and_missing_glyph():
    class Fake:
        name='Times New Roman Regular'
        flags={}
        def has_glyph(self,c):return 1
    f=Fake();assert FontResolver.validate(f)
    for k in ('bold','italic','fake-bold','fake-italic','substitute','never-embed'):
        f.flags={k:1};assert not FontResolver.validate(f)
    f.flags={};f.has_glyph=lambda c:0 if c==ord('9') else 1
    assert not FontResolver.validate(f)


def test_source_hash_and_no_overwrite_guard(tmp_path,fonts):
    src=source_pdf(tmp_path/'source.pdf',fonts)
    a=analyze(src);p=make_plan(a,STAMP,fonts)
    with pytest.raises(ValueError,match='不覆盖'):build(src,src,p,fonts)
    with open(src,'ab') as f:f.write(b'\n%changed')
    with pytest.raises(ValueError,match='发生变化'):build(src,tmp_path/'out.pdf',p,fonts)


def test_missing_field_fails_independent_audit(tmp_path,fonts):
    src=source_pdf(tmp_path/'source.pdf',fonts)
    a=analyze(src);p=make_plan(a,STAMP,fonts)
    out=tmp_path/'incomplete.pdf'
    with fitz.open(src) as d:
        write_operations(d,[o for o in p.operations if o.key!='month'],fonts);d.save(out)
    r=audit_pdf(src,out,p)
    assert not r['pass']
    assert not next(f for f in r['fields'] if f['key']=='month')['pass']


def test_non_date_change_fails_audit(tmp_path,fonts):
    src=source_pdf(tmp_path/'source.pdf',fonts);a=analyze(src);p=make_plan(a,STAMP,fonts)
    out=tmp_path/'bad.pdf'
    with fitz.open(src) as d:
        write_operations(d,p.operations,fonts)
        d[0].draw_rect(fitz.Rect(20,20,30,30),fill=(0,0,0));d.save(out)
    r=audit_pdf(src,out,p)
    assert not r['pass'] and r['outside_pixels']>0


def test_pending_prevents_implicit_partial_build(tmp_path,fonts):
    src=source_pdf(tmp_path/'source.pdf',fonts);a=analyze(src);p=make_plan(a,STAMP,fonts)
    p.pending.append('manual-review-row')
    with pytest.raises(ValueError,match='待确认'):build(src,tmp_path/'bad.pdf',p,fonts)
    assert not (tmp_path/'bad.pdf').exists()
    r=build(src,tmp_path/'partial.pdf',p,fonts,allow_partial=True)
    assert r['pending_rows']==['manual-review-row']


def test_free_date_label_is_manual_only(tmp_path,fonts):
    src=source_pdf(tmp_path/'source.pdf',fonts)
    a=analyze(src)
    r=DateRow('full',1,'full_date',{'label':[50,480,78,492]},[45,470,200,510],.9,'label',
              {'full':FieldEvidence('empty',None,'visual_blank',[82,480,180,495])},True,[])
    a.rows=[r]
    p=make_plan(a,STAMP,fonts)
    assert not p.pending and not p.operations
    assert p.rows[0]['status']=='manual'
    assert r.id in p.skipped
    assert '仅允许新增阿拉伯数字' in p.rows[0]['reasons'][0]


def test_multiple_native_date_rows(tmp_path,fonts):
    src=source_pdf(tmp_path/'source.pdf',fonts)
    with fitz.open(src) as d:
        ops=[]
        for k,c,x in [('year','年',300),('month','月',350),('day','日',400)]:
            ops.append(Operation('source2',1,k,c,'cjk',x,520,12,[x,503,x+14,523]))
        write_operations(d,ops,fonts,allow_non_digit=True);d.save(tmp_path/'multi.pdf')
    a=analyze(tmp_path/'multi.pdf');assert len(a.rows)==2
    p=make_plan(a,STAMP,fonts);assert len(p.operations)==6 and not p.pending


def test_donor_group_never_moves_anchor_coordinates(tmp_path,fonts):
    src=source_pdf(tmp_path/'source.pdf',fonts,values={'year':'2026','month':'9','day':'12'})
    r=analyze(src).rows[0];rows=[copy.deepcopy(r) for _ in range(3)]
    for i,row in enumerate(rows):row.id=f'r{i}';row.page=i+1
    anchors=copy.deepcopy([r.anchors for r in rows]);cross_check(rows)
    assert [r.anchors for r in rows]==anchors


def test_manual_geometry_is_not_bypassed_by_confirmation(tmp_path,fonts):
    src=source_pdf(tmp_path/'source.pdf',fonts);a=analyze(src);a.rows[0].geometry_ok=False
    p=make_plan(a,STAMP,fonts,{a.rows[0].id:{'mode':'confirm'}})
    assert not p.operations and p.pending


def test_plan_digest_changes_with_date_and_decisions(tmp_path,fonts):
    src=source_pdf(tmp_path/'source.pdf',fonts);a=analyze(src)
    p=make_plan(a,STAMP,fonts);p2=make_plan(a,date(2026,10,12),fonts)
    p3=make_plan(a,STAMP,fonts,{a.rows[0].id:{'mode':'skip'}})
    assert len({p.digest,p2.digest,p3.digest})==3



def test_page_specific_date_override_only_changes_target_page(tmp_path,fonts):
    src=source_pdf(tmp_path/'source.pdf',fonts)
    a1=analyze(src)
    row1=a1.rows[0]
    row2=copy.deepcopy(row1);row2.id='page-2';row2.page=2
    a=Analysis(a1.source_hash,[a1.page_sizes[0],a1.page_sizes[0]],[0,0],[row1,row2],False,[])
    p=make_plan(a,date(2026,9,8),fonts,page_dates={2:'2026-09-13'})
    by_page={1:{o.key:o.text for o in p.operations if o.page==1},2:{o.key:o.text for o in p.operations if o.page==2}}
    assert by_page[1]=={'year':'2026','month':'9','day':'8'}
    assert by_page[2]=={'year':'2026','month':'9','day':'13'}
    r1=next(r for r in p.rows if r['page']==1);r2=next(r for r in p.rows if r['page']==2)
    assert r1['effective_date']=='2026-09-08' and r1['date_source']=='default'
    assert r2['effective_date']=='2026-09-13' and r2['date_source']=='page'


def test_page_override_changes_plan_digest(tmp_path,fonts):
    src=source_pdf(tmp_path/'source.pdf',fonts);a=analyze(src)
    p1=make_plan(a,date(2026,9,8),fonts)
    p2=make_plan(a,date(2026,9,8),fonts,page_dates={1:'2026-09-13'})
    assert p1.digest!=p2.digest
    assert {o.key:o.text for o in p2.operations}=={'year':'2026','month':'9','day':'13'}

def test_signature_guard_does_not_enumerate_xref():
    from core.document import has_signatures
    class Page:
        def widgets(self):
            return []
    class Doc:
        def __iter__(self):
            return iter([Page()])
        def xref_length(self):
            raise AssertionError('signature discovery must not scan every xref object')
        def xref_get_key(self,*args):
            raise AssertionError('v21 must not call xref_get_key for signature discovery')
    assert has_signatures(Doc()) is False


def test_resilient_analysis_uses_shadow_only_after_structural_error(tmp_path,monkeypatch):
    import core.document as document
    src=tmp_path/'source.pdf';src.write_bytes(b'%PDF-1.4\n%broken-test\n')
    shadow=tmp_path/'shadow.pdf'
    calls=[]
    original_hash=file_hash(src)
    def fake_analyze(path,progress=None,assets=None):
        calls.append(str(path))
        if Path(path)==src:
            raise RuntimeError('code=7: cannot find object in xref (52 0 R)')
        return Analysis('shadow-hash',[[595.,842.]],[0],[],False,[])
    def fake_repair(source,destination):
        Path(destination).write_bytes(b'%PDF-1.4\n%repaired\n')
        return 'test recovery'
    monkeypatch.setattr(document,'analyze',fake_analyze)
    monkeypatch.setattr(document,'repair_shadow',fake_repair)
    a,used,method=document.analyze_resilient(src,shadow)
    assert calls==[str(src),str(shadow)]
    assert used==str(shadow) and method=='test recovery'
    assert a.source_hash==original_hash
    assert a.notes and '临时恢复副本' in a.notes[0]


def test_digit_grid_geometry_survives_red_seal():
    import cv2
    from core.evidence import detect_digit_grid_from_label, _candidate_from_detection, make_row
    from core.locator import Box, Detection
    rgb=np.full((260,500,3),255,np.uint8)
    label=Box(60,105,54,30)
    x0,d,top,bottom=125,22,107,137
    for k in range(9):
        cv2.line(rgb,(x0+k*d,top),(x0+k*d,bottom),(90,90,90),1)
    cv2.line(rgb,(x0,top),(x0+8*d,top),(90,90,90),1)
    cv2.line(rgb,(x0,bottom),(x0+8*d,bottom),(90,90,90),1)
    cv2.circle(rgb,(210,112),58,(210,30,30),3)
    g=detect_digit_grid_from_label(rgb,label)
    assert g and len(g['cells'])==8 and g['matched']>=4
    det=Detection(1,label,label,label,.9,'label',mode='full_date',label=label)
    c=_candidate_from_detection(det,rgb)[0]
    assert c['mode']=='grid_date'
    row=make_row(c,rgb,[],1,1)
    assert row.geometry_ok and row.fields['grid'].state=='empty'


def test_grid_layout_is_one_digit_per_cell():
    from core.evidence import _candidate_from_detection, make_row
    from core.locator import Box, Detection
    from core.planner import layout_row, make_plan
    class Resolver:
        def __init__(self): self.times=fitz.Font('Times-Roman')
        def require_times(self): return self.times
        def cjk_font(self,require_real=False): return None
    rgb=np.full((260,500,3),255,np.uint8)
    label=Box(60,105,54,30);x0,d,top,bottom=125,22,107,137
    import cv2
    for k in range(9):cv2.line(rgb,(x0+k*d,top),(x0+k*d,bottom),(80,80,80),1)
    cv2.line(rgb,(x0,top),(x0+8*d,top),(80,80,80),1);cv2.line(rgb,(x0,bottom),(x0+8*d,bottom),(80,80,80),1)
    det=Detection(1,label,label,label,.95,'label',mode='full_date',label=label)
    row=make_row(_candidate_from_detection(det,rgb)[0],rgb,[],1,1)
    r=Resolver();ops,geo,issues=layout_row(row,date(2026,9,13),r)
    assert not issues and ''.join(o.text for o in ops)=='20260913'
    assert len(ops)==8 and all(o.key.startswith('grid') for o in ops)
    a=Analysis('x',[[500.,260.]],[0],[row])
    p=make_plan(a,date(2026,9,13),r)
    assert not p.pending and p.rows[0]['status']=='write' and len(p.operations)==8


def test_free_date_layout_never_creates_non_digits():
    class Resolver:
        def __init__(self): self.times=fitz.Font('Times-Roman')
        def require_times(self): return self.times
    row=DateRow('full',1,'full_date',{'label':[50,100,80,115]},[45,92,220,125],.9,'label',
                {'full':FieldEvidence('empty',None,'visual_blank',[84,100,200,118])},True)
    ops,geo,issues=layout_row(row,date(2026,9,13),Resolver())
    assert not ops and geo.get('manual_only') and not issues


def test_writer_rejects_non_digit_by_default(tmp_path,fonts):
    d=fitz.open();d.new_page()
    op=Operation('x',1,'full','年','cjk',100,100,12,[95,80,120,110])
    with pytest.raises(ValueError,match='只允许写入'):
        write_operations(d,[op],fonts)
    d.close()


def test_build_rejects_non_digit_plan(tmp_path,fonts):
    src=source_pdf(tmp_path/'source.pdf',fonts)
    a=analyze(src)
    bad=Operation(a.rows[0].id,1,'x','年','cjk',100,100,12,[95,80,120,110])
    p=Plan(STAMP.isoformat(),file_hash(src),[bad],[],[],[],[],[])
    with pytest.raises(ValueError,match='非数字'):
        build(src,tmp_path/'bad.pdf',p,fonts)



def test_signed_document_still_auto_plans_rows(tmp_path,fonts):
    src=source_pdf(tmp_path/'source.pdf',fonts)
    a=analyze(src);a.signed=True
    p=make_plan(a,STAMP,fonts)
    assert not p.pending
    assert p.rows[0]['status']=='write'
    assert len(p.operations)==3


def test_repeated_geometry_is_cross_corroboration(tmp_path,fonts):
    src=source_pdf(tmp_path/'source.pdf',fonts)
    base=analyze(src).rows[0]
    rows=[copy.deepcopy(base) for _ in range(3)]
    for i,r in enumerate(rows,1):
        r.id=f'repeat-{i}';r.page=i;r.review_reasons=['有锚点仅由几何关系推断。','图像匹配分数偏低。']
        for f in r.fields.values():
            f.state='empty';f.value=None;f.text_chars=[]
    cross_check(rows)
    assert all(len(r.references)>=2 for r in rows)
    a=Analysis('x',[ [595.,842.] for _ in rows],[0]*3,rows,False,[])
    p=make_plan(a,STAMP,fonts)
    assert not p.pending and len(p.operations)==9


def test_signature_hint_requires_populated_byte_range(tmp_path):
    from core.pdfsafe import raw_signature_hint
    blank=tmp_path/'blank.pdf';blank.write_bytes(b'%PDF-1.4\n/ByteRange [0 0 0 0] /FT /Sig\n%%EOF')
    assert not raw_signature_hint(blank)
    real=tmp_path/'real.pdf'
    payload=bytearray(b'%PDF-1.4\n/ByteRange [0 20 60 40] /Type /Sig '+b'X'*120+b'\n%%EOF')
    real.write_bytes(bytes(payload))
    assert raw_signature_hint(real)


def test_interface_does_not_show_release_number():
    html=(Path(__file__).parents[1]/'static'/'index.html').read_text(encoding='utf-8')
    assert 'v22' not in html and 'v21' not in html
    assert '<div class="brand">电子落款</div>' in html


def _projection_fixture(day_width=20, include_date=True, dense_only=False):
    import cv2
    rgb=np.full((900,1200,3),255,np.uint8)
    # Large outer red seal used only as a spatial frame.
    cv2.circle(rgb,(760,430),145,(225,35,35),5)
    cv2.circle(rgb,(760,430),118,(235,75,75),2)
    # Dense company-name-like row: projection fallback must not accept this.
    for i in range(6):
        x=625+i*48
        cv2.rectangle(rgb,(x,365),(x+23,389),(55,55,55),-1)
    if include_date and not dense_only:
        for x,w in ((675,22),(755,22),(835,day_width)):
            cv2.rectangle(rgb,(x,485),(x+w,510),(48,48,48),-1)
            # split stroke to exercise within-glyph reconnection
            cv2.line(rgb,(x+3,492),(x+w-3,492),(255,255,255),2)
        # Blur slightly like a scanner, then restore red seal contrast.
        rgb=cv2.GaussianBlur(rgb,(3,3),.55)
        cv2.circle(rgb,(760,430),145,(225,35,35),3)
    return rgb


def test_blurry_seal_projection_fallback_finds_ymd_without_templates():
    from core.locator import DateRowLocator
    loc=DateRowLocator()
    rgb=_projection_fixture()
    seals=loc._seal_boxes(rgb)
    d=loc._detect_seal_projection_row(rgb,1,seals)
    assert d is not None
    assert d.year.x < d.month.x < d.day.x
    assert d.confidence >= .78
    assert '投影识别' in d.method


def test_blurry_seal_projection_accepts_narrow_day_anchor():
    from core.locator import DateRowLocator
    loc=DateRowLocator()
    rgb=_projection_fixture(day_width=8)
    d=loc._detect_seal_projection_row(rgb,1,loc._seal_boxes(rgb))
    assert d is not None
    assert d.day.w >= 7


def test_blurry_seal_projection_rejects_dense_company_row_without_date():
    from core.locator import DateRowLocator
    loc=DateRowLocator()
    rgb=_projection_fixture(include_date=False,dense_only=True)
    assert loc._detect_seal_projection_row(rgb,1,loc._seal_boxes(rgb)) is None
