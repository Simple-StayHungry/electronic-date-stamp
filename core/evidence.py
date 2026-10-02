from __future__ import annotations
import math
import numpy as np
import cv2
import fitz
from .models import Character, DateRow, FieldEvidence, KEYS, union_rect
from .locator import DateRowLocator, Box

SCALE = 200 / 72
NUMERIC = set('0123456789〇○零一二三四五六七八九十')

def render(page, dpi=200):
    z=dpi/72
    if page.rect.width*z * page.rect.height*z > 45_000_000:
        raise ValueError('页面尺寸过大，超出本地安全渲染上限。请先拆分或检查页面尺寸。')
    pix=page.get_pixmap(matrix=fitz.Matrix(z,z), colorspace=fitz.csRGB, alpha=False)
    return np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width,3).copy()

def extract_characters(page):
    """Trace identifies invisible OCR and normalizes rotation before any test.

    Positive text evidence protects content; missing text never authorizes a write.
    """
    chars=[]
    matrix=page.rotation_matrix
    for sp in page.get_texttrace():
        visible=sp.get('type',3) in (0,1,2) and sp.get('opacity',1)>0.3
        dx,dy=sp.get('dir',(1.,0.))
        vec=fitz.Point(dx,dy)*matrix - fitz.Point(0,0)*matrix
        horizontal=abs(vec.y)<.07 and vec.x>.9
        for item in sp.get('chars',[]):
            try:
                c=chr(item[0])
                if not c.strip(): continue
                r=fitz.Rect(item[3])*matrix
                p=fitz.Point(item[2])*matrix
                chars.append(Character(c,list(r),list(p),float(sp['size']),str(sp.get('font','')),
                                       visible,horizontal))
            except (ValueError,OverflowError,IndexError): continue
    return chars

def native_candidates(chars, page_no, page_rect):
    """Character-level recognition works even when '9月12日' shares one span.

    Does not trust the existence of a text layer as proof it is correct or visible.
    Multiple isolated native date rows on a page are supported.
    """
    candidates=[]
    ys=[c for c in chars if c.text=='年' and c.horizontal]
    ms=[c for c in chars if c.text=='月' and c.horizontal]
    ds=[c for c in chars if c.text=='日' and c.horizontal]
    for y in ys:
        for m in ms:
            h=(y.size+m.size)/2
            if not 4<=h<=32 or not y.cx < m.cx: continue
            if abs(y.origin[1]-m.origin[1])>max(1.4,h*.16): continue
            if m.cx-y.cx>h*10: continue
            for d in ds:
                if not m.cx<d.cx or d.cx-m.cx>h*10: continue
                if abs(y.origin[1]-d.origin[1])>max(1.4,h*.16): continue
                if min(m.bbox[0]-y.bbox[2],d.bbox[0]-m.bbox[2])<h*.1: continue
                region=[max(0,y.bbox[0]-h*5),y.bbox[1]-h*.4,
                        min(page_rect.width,d.bbox[2]+h*.6),d.bbox[3]+h*.4]
                if any(abs(r['anchors']['year'][0]-y.bbox[0])<h and
                       abs(r['baseline']-y.origin[1])<h for r in candidates): continue
                same_line=[c for c in chars if abs(c.origin[1]-y.origin[1])<h*.2 and c.visible]
                before=''.join(c.text for c in sorted(same_line,key=lambda c:c.cx) if c.cx<y.cx-h*4.5)
                after=''.join(c.text for c in same_line if c.cx>d.cx+h)
                isolated=(not after and (len(before)<=3 or any(w in before for w in ('日期','签署','落款','签订'))))
                reasons=[]
                if not isolated: reasons.append('日期位于其他文字行中，须确认是落款而非正文。')
                if not all(c.visible for c in (y,m,d)): reasons.append('年月日来自隐藏文字层，须核对页面图像。')
                candidates.append({'anchors':{'year':list(y.bbox),'month':list(m.bbox),'day':list(d.bbox)},
                     'region':region,'score':.99,'method':'原生字符坐标',
                     'size':float(np.median([y.size,m.size,d.size])),
                     'baseline':float(np.median([y.origin[1],m.origin[1],d.origin[1]])),
                     'reasons':reasons,'native':True,'mode':'ymd'})
    return sorted(candidates,key=lambda r:r['baseline'])



def _cluster_axis(items, tol):
    """Merge near-identical line positions while retaining their vertical spans."""
    if not items:
        return []
    items=sorted(items,key=lambda x:x[0])
    groups=[]
    for item in items:
        if not groups or item[0]-groups[-1][-1][0]>tol:
            groups.append([item])
        else:
            groups[-1].append(item)
    out=[]
    for g in groups:
        weights=[max(1.0,float(z[3])) for z in g]
        x=float(np.average([z[0] for z in g],weights=weights))
        out.append((x,min(z[1] for z in g),max(z[2] for z in g),sum(weights)))
    return out

def detect_digit_grid_from_label(rgb, label):
    """Detect an 8-cell YYYYMMDD lattice immediately after a 日期 label.

    The detector intentionally uses straight-line geometry rather than OCR. Red
    seals may erase several separators; a robust lattice fit reconstructs missing
    boundaries only when at least four independent verticals agree on one period.
    Coordinates returned here are 200-dpi pixels.
    """
    H,W=rgb.shape[:2]
    lh=max(8.0,float(label.h))
    sx0=max(0,int(label.x+label.w*.30))
    sx1=min(W,int(label.x+label.w+lh*12.5))
    cy=float(label.cy)
    sy0=max(0,int(cy-lh*1.05)); sy1=min(H,int(cy+lh*1.25))
    if sx1-sx0<lh*3 or sy1-sy0<lh:
        return None
    crop=rgb[sy0:sy1,sx0:sx1]
    gray=cv2.cvtColor(crop,cv2.COLOR_RGB2GRAY)
    # Scanned table lines can be light gray / brown. Preserve them instead of
    # de-reding the whole region; straight-line morphology rejects curved seal ink.
    bw=(gray<238).astype(np.uint8)*255
    vk=max(6,int(round(lh*.42)))
    vertical=cv2.morphologyEx(bw,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(1,vk)))
    n,_,stats,cents=cv2.connectedComponentsWithStats((vertical>0).astype(np.uint8),8)
    raw=[]
    for st,ce in zip(stats[1:],cents[1:]):
        x,y,w,hh,area=map(int,st)
        if hh<lh*.42 or hh>lh*1.85: continue
        if w>max(7,lh*.34): continue
        if area<max(8,lh*.28): continue
        ax=float(ce[0]+sx0)
        if ax<label.x+label.w*.25: continue
        raw.append((ax,float(y+sy0),float(y+hh+sy0),float(hh)))
    lines=_cluster_axis(raw,max(1.6,lh*.075))
    if len(lines)<4:
        return None
    xs=np.array([q[0] for q in lines],dtype=float)
    # Candidate cell periods from observed separator distances. Missing separators
    # are allowed, so also test integer subdivisions of wider gaps.
    periods=[]
    for i in range(len(xs)):
        for j in range(i+1,len(xs)):
            delta=xs[j]-xs[i]
            for div in (1,2,3,4):
                d=delta/div
                if lh*.30<=d<=lh*1.25:
                    periods.append(float(d))
    if not periods:
        return None
    periods=sorted(periods)
    # Reduce nearly identical hypotheses to keep this path cheap.
    uniq=[]
    for d in periods:
        if not uniq or abs(d-uniq[-1])>max(.7,d*.035): uniq.append(d)
    best=None
    for d in uniq:
        tol=max(2.0,d*.16)
        for anchor in xs:
            for idx in range(9):
                start=anchor-idx*d
                end=start+8*d
                if start<label.x+label.w*.80 or start>label.x+label.w+lh*2.8: continue
                if end>W-1 or end-start<lh*2.8: continue
                used=[];residual=[];matched_lines=[]
                for k in range(9):
                    target=start+k*d
                    j=int(np.argmin(abs(xs-target)))
                    err=abs(xs[j]-target)
                    if err<=tol and j not in used:
                        used.append(j);residual.append(err);matched_lines.append((k,lines[j]))
                if len(matched_lines)<4: continue
                ids=[k for k,_ in matched_lines]
                if max(ids)-min(ids)<5: continue
                # At least one separator must anchor the left half and one the right half.
                if min(ids)>2 or max(ids)<6: continue
                ytops=[q[1][1] for q in matched_lines]; ybots=[q[1][2] for q in matched_lines]
                top=float(np.median(ytops)); bottom=float(np.median(ybots))
                height=bottom-top
                if not (lh*.45<=height<=lh*1.65): continue
                if not (.34<=d/max(height,.1)<=1.35): continue
                # Horizontal support. Broken rails are fine; only require some long
                # straight evidence near either the top or bottom of the cells.
                hk=max(8,int(round(d*1.35)))
                horizontal=cv2.morphologyEx(bw,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(hk,1)))
                nh,_,sh,_=cv2.connectedComponentsWithStats((horizontal>0).astype(np.uint8),8)
                hsupport=0.0
                for z in sh[1:]:
                    xx,yy,ww,hh,area=map(int,z); ay=yy+sy0
                    if ww<d*1.5 or hh>max(6,lh*.3): continue
                    if abs(ay-top)<=max(4,lh*.25) or abs(ay-bottom)<=max(4,lh*.25):
                        overlap=max(0,min(xx+ww+sx0,end)-max(xx+sx0,start))
                        hsupport=max(hsupport,overlap/max(end-start,1))
                meanerr=float(np.mean(residual))/max(d,.1)
                score=len(matched_lines)*1.15+(max(ids)-min(ids))*.18+min(1.0,hsupport)*1.4-meanerr*3.0
                if best is None or score>best[0]:
                    best=(score,start,d,top,bottom,len(matched_lines),meanerr,hsupport)
    if best is None:
        return None
    score,start,d,top,bottom,nmatch,meanerr,hsupport=best
    # Conservative publication threshold. Four matches are enough only with good
    # span and low residual; five or more usually survives a heavy red seal.
    if score<5.1 or (nmatch==4 and meanerr>.09):
        return None
    cells=[]
    inset=max(.25,d*.015)
    for i in range(8):
        cells.append([float(start+i*d+inset),float(top+inset),float(start+(i+1)*d-inset),float(bottom-inset)])
    conf=float(min(.99,.66+.045*nmatch+.10*min(1.0,hsupport)-.20*meanerr))
    return {'cells':cells,'confidence':conf,'matched':nmatch,'period':d,'top':top,'bottom':bottom}

def _candidate_from_detection(d, rgb):
    if d is None: return []
    if d.mode=='full_date':
        b=d.label
        label=[b.x/SCALE,b.y/SCALE,b.x1/SCALE,b.y1/SCALE]
        grid=detect_digit_grid_from_label(rgb,b)
        if grid is not None:
            cells=[[v/SCALE for v in cell] for cell in grid['cells']]
            reg=union_rect([label]+cells)
            reg=[max(0,reg[0]-3),max(0,reg[1]-4),min(rgb.shape[1]/SCALE,reg[2]+4),reg[3]+4]
            reasons=[]
            if grid['confidence']<.84:
                reasons.append('方格线受遮挡较多，已按重复间距重建，请核对预览。')
            return [{'anchors':{'label':label,'grid':union_rect(cells)},'cells':cells,'region':reg,
                     'score':float(grid['confidence']),'method':f"日期方格几何识别（{grid['matched']} 条分隔线印证）",
                     'size':None,'baseline':None,'reasons':reasons,'native':False,'mode':'grid_date'}]
        return [{'anchors':{'label':label},'region':[max(0,label[0]-3),max(0,label[1]-8),
                  min(rgb.shape[1]/SCALE,label[2]+95),label[3]+8],
                 'score':float(d.confidence),'method':'日期标签识别（自由日期栏）','size':None,'baseline':None,
                 'reasons':[],'native':False,'mode':'full_date'}]
    anchors={}
    reasons=[]
    for k in KEYS:
        core=getattr(d,k)
        # The date-line detector gives a glyph envelope. Include ALL ink in
        # that envelope, including short disconnected horizontal strokes.
        x0=max(0,int(core.x)); y0=max(0,int(core.y))
        x1=min(rgb.shape[1],int(math.ceil(core.x1))); y1=min(rgb.shape[0],int(math.ceil(core.y1)))
        block=rgb[y0:y1,x0:x1]
        gray,_=DateRowLocator._remove_red(block)
        n,_,stats,_=cv2.connectedComponentsWithStats((gray<175).astype(np.uint8),8)
        bits=[q for q in stats[1:] if int(q[4])>=3]
        if bits:
            xx=min(q[0] for q in bits); yy=min(q[1] for q in bits)
            xe=max(q[0]+q[2] for q in bits); ye=max(q[1]+q[3] for q in bits)
            ink=Box(float(x0+xx),float(y0+yy),float(xe-xx),float(ye-yy))
        else:
            ink=core
            reasons.append(f'{k} 锚点在去红图像中缺少独立墨迹，须核对。')
        if ink.w < core.h*.26 or ink.w>core.h*1.55 or ink.h<core.h*.38:
            reasons.append(f'{dict(year="年",month="月",day="日")[k]}字边界不充分。')
        anchors[k]=[ink.x/SCALE,ink.y/SCALE,ink.x1/SCALE,ink.y1/SCALE]
    h=float(np.median([r[3]-r[1] for r in anchors.values()]))
    reg=[max(0,anchors['year'][0]-5*h),min(r[1] for r in anchors.values())-8,
         min(rgb.shape[1]/SCALE,anchors['day'][2]+8),max(r[3] for r in anchors.values())+8]
    # A locally revalidated repeated layout is already corroborated by three
    # independent glyph matches, so it does not inherit the old "inferred" review.
    if d.method!='同版式局部复核':
        if '推断' in d.method or '几何' in d.method:
            reasons.append('有锚点仅由几何关系推断。')
        if float(d.confidence)<.64:
            reasons.append('图像匹配分数偏低。')
    return [{'anchors':anchors,'region':reg,'score':float(d.confidence),'method':d.method,
             'size':None,'baseline':None,'reasons':reasons,'native':False,'mode':'ymd'}]

def visual_candidate(locator, rgb, page_no, full_quality=False):
    return _candidate_from_detection(locator.detect_page(rgb,page_no,full_quality=full_quality),rgb)

def visual_candidate_near(locator, rgb, page_no, anchors):
    return _candidate_from_detection(locator.detect_near(rgb,page_no,anchors),rgb)

def inspect_field(rgb, chars, bbox, h, key):
    """Four outcomes: known text, verifiably blank, protected image ink, uncertain.

    Pixels can establish blankness under explicit conditions, but cannot establish
    the semantic value of a digit. Fragmentary or occluded evidence remains uncertain.
    """
    x0,y0,x1,y1=bbox
    H,W=rgb.shape[:2]
    a,b=max(0,int(x0*SCALE)),max(0,int(y0*SCALE))
    c,d=min(W,int(math.ceil(x1*SCALE))),min(H,int(math.ceil(y1*SCALE)))
    crop=rgb[b:d,a:c]
    if c-a<3 or d-b<3:
        return FieldEvidence('uncertain',None,'geometry',bbox,['数字区域太窄或越界。'])
    gray,red=DateRowLocator._remove_red_aggressive(crop)
    hp=max(6,h*SCALE)
    rr=float((red>0).mean())
    # Tight vertical band: fonts have ascender/descender boxes, not all are glyph ink.
    candidates=[ch for ch in chars if ch.text in NUMERIC and
                x0-.25<=ch.cx<=x1+.25 and y0-.5<=ch.cy<=y1+.5]
    candidates.sort(key=lambda ch:ch.cx)
    # Deduplicate duplicated OCR layers with the same value and coordinates.
    unique=[]
    for ch in candidates:
        if not any(ch.text==o.text and abs(ch.cx-o.cx)<.35 and abs(ch.cy-o.cy)<.35 for o in unique): unique.append(ch)
    candidates=unique
    mask=(gray<175).astype(np.uint8)
    ratio=float(mask.mean())
    if candidates:
        value=''.join(ch.text for ch in candidates)
        if all(ch.visible for ch in candidates) and mask.sum()>=4:
            return FieldEvidence('present',value,'visible_text',bbox,['原有文字对象与可见墨迹互相印证；禁止覆盖。'],candidates,ratio,rr)
        return FieldEvidence('uncertain',value,'text_only',bbox,
                    ['文字层存在内容，但无法充分印证可见性；保留原字，须确认。'],candidates,ratio,rr)
    n,_,stats,_=cv2.connectedComponentsWithStats(mask,8)
    glyphs=[]; fragments=[]
    for s in stats[1:]:
        x,y,w,hh,area=map(int,s)
        if area<max(3,hp*.07): continue
        if hh>=hp*.42 and w>=hp*.12 and area>=max(10,hp*hp*.016):
            glyphs.append((x,y,w,hh,area))
        elif area>=max(6,hp*.2): fragments.append((x,y,w,hh,area))
    # Strokes touching only a slot edge are normally leakage from 年/月/日. They
    # are not evidence of a filled number. This removes a common false review.
    if glyphs:
        edge_only=all((g[0]<=2 or g[0]+g[2]>=crop.shape[1]-2) and g[2]<hp*.34 for g in glyphs)
        if edge_only:
            glyphs=[]
        else:
            return FieldEvidence('protected',None,'image_ink',bbox,
                   ['原图存在完整黑色字形，自动保留，不覆盖。'],[],ratio,rr)
    weak=((gray<215)&(red==0)).astype(np.uint8)
    # Ignore a two-pixel border: anti-aliased anchor strokes commonly leak into
    # the numeric slot after geometric cropping.
    if weak.shape[0]>4 and weak.shape[1]>4:
        weak[:2,:]=0;weak[-2:,:]=0;weak[:,:2]=0;weak[:,-2:]=0
    nw,_,sw,_=cv2.connectedComponentsWithStats(weak,8)
    weak_big=any(int(q[3])>hp*.42 and int(q[4])>max(9,hp*hp*.02) and int(q[2])>hp*.10 for q in sw[1:])
    # Red seal coverage alone is no longer a reason to ask the user. If aggressive
    # de-red leaves no digit-sized black/gray component, the slot is blank. A very
    # high coverage still remains uncertain because the source image contains too
    # little independent evidence.
    if not fragments and not weak_big and ratio<.032 and rr<.72:
        source='visual_blank_under_seal' if rr>=.18 else 'visual_blank'
        why='去除红章后未见数字级黑色墨迹。' if rr>=.18 else '两档亮度检查未见数字级墨迹。'
        return FieldEvidence('empty',None,source,bbox,[why],[],ratio,rr)
    # Tiny residual fragments without a digit-height component are also blank when
    # they occupy only a very small fraction of the slot.
    frag_area=sum(f[4] for f in fragments)
    if not weak_big and ratio<.018 and frag_area<max(14,hp*hp*.018) and rr<.72:
        return FieldEvidence('empty',None,'visual_blank_residual',bbox,
                    ['仅有少量残余笔画，不构成数字字形。'],[],ratio,rr)
    return FieldEvidence('uncertain',None,'ambiguous_image',bbox,
               ['存在无法排除的黑色字形或重度遮挡，才需要确认。'],[],ratio,rr)

def make_row(candidate, rgb, chars, page_no, index):
    a=candidate['anchors']; mode=candidate['mode']
    native=candidate.get('native',False)
    reasons=list(candidate['reasons'])
    if mode=='grid_date':
        cells=candidate.get('cells') or []
        if len(cells)!=8:
            fields={'grid':FieldEvidence('uncertain',None,'grid_geometry',candidate['region'],['方格数量不是 8，需重新定位。'])}
            return DateRow(f'p{page_no}-r{index}',page_no,mode,a,candidate['region'],candidate['score'],
                           candidate['method'],fields,False,reasons+['方格数量不是 8。'],grid_cells=cells,grid_style='YYYYMMDD')
        ws=[c[2]-c[0] for c in cells];hs=[c[3]-c[1] for c in cells]
        cy=[(c[1]+c[3])/2 for c in cells]
        geometry=(min(ws)>2 and min(hs)>3 and max(ws)/max(.1,min(ws))<1.18 and
                  max(hs)/max(.1,min(hs))<1.18 and max(cy)-min(cy)<max(1.6,np.median(hs)*.14))
        states=[];vals=[];all_chars=[];ratios=[];reds=[];why=[]
        for i,c in enumerate(cells):
            w=c[2]-c[0];h=c[3]-c[1]
            insetx=max(.7,w*.16); insety=max(.5,h*.16)
            inner=[c[0]+insetx,c[1]+insety,c[2]-insetx,c[3]-insety]
            e=inspect_field(rgb,chars,inner,h,f'grid{i}')
            states.append(e.state);vals.append(e.value);all_chars.extend(e.text_chars);ratios.append(e.ink_ratio);reds.append(e.red_ratio)
            if e.state not in ('empty','present'): why.extend(e.reasons)
        if all(x=='empty' for x in states):
            field=FieldEvidence('empty',None,'grid_blank',union_rect(cells),['8 个日期格均未见数字级黑色墨迹。'],[],float(np.mean(ratios)),float(np.mean(reds)))
        elif all(x=='present' and v and len(v)==1 for x,v in zip(states,vals)):
            field=FieldEvidence('present',''.join(vals),'grid_text',union_rect(cells),['8 个方格均有原生数字，禁止覆盖。'],all_chars,float(np.mean(ratios)),float(np.mean(reds)))
        elif any(x=='present' for x in states):
            shown=''.join((v if (st=='present' and v) else '?') for st,v in zip(states,vals))
            field=FieldEvidence('uncertain',shown,'grid_partial',union_rect(cells),['方格中已有部分数字，默认保留并要求核对。'],all_chars,float(np.mean(ratios)),float(np.mean(reds)))
        elif any(x in ('protected','uncertain') for x in states):
            field=FieldEvidence('uncertain',None,'grid_image',union_rect(cells),list(dict.fromkeys(why or ['方格内存在无法排除的原有字形。'])),all_chars,float(np.mean(ratios)),float(np.mean(reds)))
        else:
            field=FieldEvidence('uncertain',None,'grid_unknown',union_rect(cells),['方格状态无法确定。'],all_chars,float(np.mean(ratios)),float(np.mean(reds)))
        if not geometry: reasons.append('方格尺寸或基线不一致，需重新定位。')
        return DateRow(f'p{page_no}-r{index}',page_no,mode,a,candidate['region'],candidate['score'],
                       candidate['method'],{'grid':field},geometry,reasons,grid_cells=cells,grid_style='YYYYMMDD')
    if mode=='full_date':
        b=a['label'];h=b[3]-b[1]
        rect=[b[2]+max(2,h*.35),b[1]-h*.15,
              min(rgb.shape[1]/SCALE,b[2]+h*9),b[3]+h*.15]
        fields={'full':inspect_field(rgb,chars,rect,h,'full')}
        return DateRow(f'p{page_no}-r{index}',page_no,mode,a,candidate['region'],candidate['score'],
                       candidate['method'],fields,True,reasons)
    h=float(np.median([b[3]-b[1] for b in a.values()]))
    cy=float(np.median([(b[1]+b[3])/2 for b in a.values()]))
    side=max(.65,h*.10)
    y0,y1=cy-h*.64,cy+h*.62
    slots={'year':[max(0,a['year'][0]-h*5.0),y0,a['year'][0]-side,y1],
           'month':[a['year'][2]+side,y0,a['month'][0]-side,y1],
           'day':[a['month'][2]+side,y0,a['day'][0]-side,y1]}
    fields={k:inspect_field(rgb,chars,b,h,k) for k,b in slots.items()}
    centers=[(a[k][1]+a[k][3])/2 for k in KEYS]
    geometry=(h>=3 and h<=35 and max(centers)-min(centers)<max(2.6,h*.32) and
              a['year'][2]<a['month'][0] and a['month'][2]<a['day'][0] and
              all(b[2]>b[0] for b in slots.values()))
    if not geometry: reasons.append('锚点顺序、尺寸或基线检查不通过。')
    # Prefer same-row existing numeric type size and baseline, but never learn bold weight.
    numeric=[ch for f in fields.values() for ch in f.text_chars if ch.visible and ch.horizontal]
    size=candidate.get('size'); baseline=candidate.get('baseline')
    if size is None and numeric:
        sizes=[c.size for c in numeric]; bases=[c.origin[1] for c in numeric]
        if max(sizes)/max(.1,min(sizes))<1.12 and max(bases)-min(bases)<1.2:
            size=float(np.median(sizes));baseline=float(np.median(bases))
    return DateRow(f'p{page_no}-r{index}',page_no,mode,a,candidate['region'],candidate['score'],
                   candidate['method'],fields,geometry,reasons,size,baseline)
