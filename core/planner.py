from __future__ import annotations
from dataclasses import asdict
from datetime import date
import math
import numpy as np
from .models import Analysis, DateRow, Plan, Operation, KEYS

CN={c:str(i) for i,c in enumerate('〇一二三四五六七八九')}
CN.update({'○':'0','零':'0'})

def normalize_value(s):
    if not s: return None
    s=''.join(CN.get(c,c) for c in s)
    if '十' in s:
        a,b=s.split('十',1)
        if (not a or a.isdigit()) and (not b or b.isdigit()):
            return str(int(a or '1')*10+int(b or '0'))
    return str(int(s)) if s.isdigit() else None

def _op(row,key,txt,font_name,font,x,baseline,size,gap=None):
    width=float(font.text_length(txt,fontsize=size))
    # A field-sized mask, not a full horizontal date band.
    bbox=[x-.65,baseline-size*1.13-.5,x+width+.65,baseline+size*.32+.5]
    return Operation(row.id,row.page,key,txt,font_name,float(x),float(baseline),float(size),bbox,gap)


def _grid_op(row, index, digit, font, cell, size, dx=0.0, dy=0.0):
    """Optically center one Times digit inside a detected date cell."""
    gb=font.glyph_bbox(ord(digit))
    cx=(cell[0]+cell[2])/2+dx; cy=(cell[1]+cell[3])/2+dy
    x=cx-((gb.x0+gb.x1)/2)*size
    baseline=cy+((gb.y0+gb.y1)/2)*size
    pad=max(.18,min(cell[2]-cell[0],cell[3]-cell[1])*.025)
    bbox=[float(cell[0]+pad),float(cell[1]+pad),float(cell[2]-pad),float(cell[3]-pad)]
    return Operation(row.id,row.page,f'grid{index}',digit,'times',float(x),float(baseline),float(size),bbox,None)

def layout_row(row, stamp, fonts, adjustment=None):
    """Pure geometry. No pixel occupancy rules and no export side effects."""
    adj=adjustment or {}
    font=fonts.require_times()
    vals={'year':str(stamp.year),'month':str(stamp.month),'day':str(stamp.day)}
    if row.mode=='grid_date':
        cells=row.grid_cells
        if len(cells)!=8:
            return [],{},['方格日期必须稳定识别出 8 个数字格。']
        digits=stamp.strftime('%Y%m%d')
        dx=float(adj.get('dx',0));dy=float(adj.get('dy',0))
        if not all(math.isfinite(v) and abs(v)<=6 for v in (dx,dy)):
            return [],{},['方格日期偏移必须在正负 6pt 内。']
        widths=[c[2]-c[0] for c in cells];heights=[c[3]-c[1] for c in cells]
        # Fit against the widest Times digit and the actual glyph box, then use one
        # common size across all eight cells. The generous margins keep numbers off
        # scanned borders even when the grid is slightly skewed.
        unit_w=max(font.text_length(ch,fontsize=1) for ch in '0123456789')
        unit_h=max(font.glyph_bbox(ord(ch)).y1-font.glyph_bbox(ord(ch)).y0 for ch in '0123456789')
        fit_w=min(widths)*.68/max(unit_w,.01)
        fit_h=min(heights)*.64/max(unit_h,.01)
        size=float(adj.get('size') or min(18,max(5.5,min(fit_w,fit_h))))
        if not 4<=size<=24:
            return [],{},['方格过小或过大，无法安全排入日期数字。']
        ops=[_grid_op(row,i,digit,font,c,size,dx,dy) for i,(digit,c) in enumerate(zip(digits,cells))]
        issues=[]
        for op,c in zip(ops,cells):
            gb=font.glyph_bbox(ord(op.text))
            ink=[op.x+gb.x0*size,op.baseline-gb.y1*size,op.x+gb.x1*size,op.baseline-gb.y0*size]
            if ink[0]<c[0]+.15 or ink[2]>c[2]-.15 or ink[1]<c[1]+.15 or ink[3]>c[3]-.15:
                issues.append('方格内数字安全边距不足。')
                break
        return ops,{'size_source':'按方格自动适配','size':round(size,3),'grid':'YYYYMMDD','cells':8},list(dict.fromkeys(issues))
    if row.mode=='full_date':
        # Compliance boundary: free-form date labels do not provide an existing
        # numeric structure. The application never creates 年/月/日, separators,
        # or any other non-digit content. These rows are exported unchanged.
        return [], {'manual_only': True}, []
    a=row.anchors
    h=float(np.median([a[k][3]-a[k][1] for k in KEYS]))
    size=float(adj.get('size') or row.native_size or h/.915)
    if not math.isfinite(size) or not 4<=size<=32:
        return [],{},['字号不在可用范围内，请手动核对。']
    if row.native_baseline is not None:
        base=row.native_baseline
    else:
        base=float(np.median([a[k][3]-{'year':.110,'month':.113,'day':.072}[k]*size for k in KEYS]))
    base+=float(adj.get('dy',0))
    dx=float(adj.get('dx',0))
    if not all(math.isfinite(v) and abs(v)<=12 for v in (dx,float(adj.get('dy',0)))):
        return [],{},['手动偏移必须在正负 12pt 内。']
    issues=[];positions={};gaps=[]
    # Month/day live in the *actual* corridor between the surrounding Chinese
    # anchors. The evidence boxes intentionally reserve a safety margin and are
    # too narrow for compact source layouts such as “9月12日”.
    corridors={
        'month':(a['year'][2],a['month'][0]),
        'day':(a['month'][2],a['day'][0]),
    }
    # If the source corridor is only slightly tighter than the requested Times
    # digits, reduce the whole numeric run by at most 8%. This keeps year/month/day
    # the same size while avoiding false "cannot fit" blocks.
    if not adj.get('size'):
        fit=1.0
        for key in ('month','day'):
            raw=max(.1,corridors[key][1]-corridors[key][0])
            width=font.text_length(vals[key],fontsize=size)
            fit=min(fit,(raw-max(.25,size*.02)*2)/max(width,.1))
        if .92<=fit<1.0:
            size*=fit
            if row.native_baseline is None:
                base=float(np.median([a[k][3]-{'year':.110,'month':.113,'day':.072}[k]*size for k in KEYS]))+float(adj.get('dy',0))
    min_year_gap=max(2.6,.30*size)
    min_inner_gap=max(.18,.018*size)
    for key in ('month','day'):
        left,right=corridors[key]
        width=font.text_length(vals[key],fontsize=size)
        available=right-left
        if width+2*min_inner_gap>available+.12:
            issues.append(f'{dict(month="月份",day="日期")[key]}数字无法放入年月日之间的实际空白。')
        x=(left+right-width)/2
        positions[key]=x
        gap=a[key][0]-(x+width)
        if .25<=gap<=max(18,1.5*size):gaps.append(gap)
        old=row.fields[key].text_chars
        if old and all(c.visible for c in old):
            g=a[key][0]-max(c.bbox[2] for c in old)
            if .25<=g<=max(18,1.5*size):gaps.append(g)
    if row.reference_gap_em is not None:
        gaps.append(row.reference_gap_em*size)
    gap=max(min_year_gap,float(np.median(gaps)) if gaps else size*.36)
    gap=min(gap,max(10.5,.95*size))
    positions['year']=a['year'][0]-gap-font.text_length(vals['year'],fontsize=size)
    ops=[]
    for key in KEYS:
        op=_op(row,key,vals[key],'times',font,positions[key]+dx,base,size,
               a[key][0]-(positions[key]+dx+font.text_length(vals[key],fontsize=size)))
        if key!='year':
            left,right=corridors[key]
            if op.x<left-.15 or op.x+font.text_length(op.text,fontsize=size)>right+.15:
                issues.append(f'{key} 位置超出年月日之间的实际空白。')
            if op.anchor_gap<min_inner_gap-.15:
                issues.append(f'{key} 与后方汉字发生重叠。')
        elif op.anchor_gap<min_year_gap-.05:
            issues.append('year 与“年”的间距不足。')
        if op.x<.5:
            issues.append(f'{key} 越出页面边界。')
        ops.append(op)
    return ops,{'size_source':'手动' if adj.get('size') else ('同一行原生字体度量' if row.native_size else '图像字高估计'),
                'year_gap':round(gap,3),'size':round(size,3),'baseline':round(base,3)},list(dict.fromkeys(issues))

def make_plan(analysis: Analysis, stamp: date, fonts, decisions=None, page_dates=None):
    decisions=decisions or {}
    page_dates=page_dates or {}
    operations=[];row_data=[];pending=[];skipped=[];optional=[];warnings=[]
    for row in analysis.rows:
        raw_override=page_dates.get(row.page, page_dates.get(str(row.page)))
        row_stamp=date.fromisoformat(raw_override) if raw_override else stamp
        expected={'year':str(row_stamp.year),'month':str(row_stamp.month),'day':str(row_stamp.day)}
        decision=decisions.get(row.id,{})
        mode=decision.get('mode','auto')
        if mode not in ('auto','confirm','skip'):raise ValueError('无效的页面处理方式。')
        chosen=decision.get('fields',{})
        info={'id':row.id,'page':row.page,'mode':row.mode,'status':'preserved','reasons':[],
              'operations':[],'proposed':[],'geometry':{},'changed_keys':[], 'field_actions':{},
              'effective_date':row_stamp.isoformat(),'date_source':'page' if raw_override else 'default'}
        reasons=list(row.review_reasons)
        needs=[]
        if mode=='skip':
            info['status']='skipped';skipped.append(row.id);row_data.append(info);continue
        if row.mode=='grid_date':
            f=row.fields['grid'];action=chosen.get('grid','auto')
            if action not in ('auto','keep','fill_confirmed'):raise ValueError('无效的方格日期处理方式。')
            expected_grid=row_stamp.strftime('%Y%m%d')
            if mode=='skip' or action=='keep':
                info['status']='skipped' if mode=='skip' else 'preserved'
                info['field_actions']['grid']='keep';row_data.append(info);continue
            if f.state=='present':
                info['field_actions']['grid']='keep'
                if f.value==expected_grid:
                    info['status']='complete'
                else:
                    info['status']='review';info['reasons']=[f'方格内已有 {f.value}，与本次 {expected_grid} 不同；默认不覆盖。'];pending.append(row.id)
                row_data.append(info);continue
            if f.text_chars and action=='fill_confirmed':
                info['status']='review';info['reasons']=['方格内已有原生文字对象，不允许覆盖。'];pending.append(row.id);row_data.append(info);continue
            can_fill=(f.state=='empty' or (action=='fill_confirmed' and not f.text_chars))
            if not can_fill:
                info['status']='review';info['reasons']=list(dict.fromkeys(reasons+f.reasons+['方格内存在无法排除的原字；请保留原样或确认空白。']));pending.append(row.id);row_data.append(info);continue
            hard=[]
            if not row.geometry_ok:hard.append('方格几何检查失败，请重新定位。')
            if fonts.times is None:
                hard.append('缺少经过验证的 Times New Roman Regular。');proposed=[];geo={};layout_errors=[]
            else:
                proposed,geo,layout_errors=layout_row(row,row_stamp,fonts,decision.get('adjust'));hard.extend(layout_errors)
            approval_needed=bool(row.review_reasons) and mode!='confirm'
            all_reasons=list(dict.fromkeys(hard+(row.review_reasons if approval_needed else [])))
            info['geometry']=geo;info['changed_keys']=['grid'];info['proposed']=[asdict(x) for x in proposed]
            if all_reasons:
                info['status']='blocked' if hard else 'review';info['reasons']=all_reasons;pending.append(row.id)
            else:
                info['status']='write';info['field_actions']['grid']='fill';info['operations']=[asdict(x) for x in proposed];operations.extend(proposed)
            row_data.append(info);continue
        if row.mode=='full_date':
            # A label such as “日期：____” has no pre-existing numeric grammar.
            # Auto-filling it would require adding Chinese characters or separators,
            # which violates the tool's minimal-modification rule. Keep it untouched
            # and do not make the user confirm anything in the browser.
            info['status']='manual'
            info['field_actions']['full']='keep'
            info['reasons']=['此处只有自由日期栏，没有原有“年/月/日”、斜杠或数字格。程序仅允许新增阿拉伯数字，因此保持原样；导出后人工填写。']
            skipped.append(row.id)
            row_data.append(info)
            continue
        for key,f in row.fields.items():
            action=chosen.get(key,'auto')
            if action not in ('auto','keep','fill_confirmed'):raise ValueError('无效的字段动作。')
            if f.text_chars and action=='fill_confirmed':
                reasons.append(f'{key} 已有文字对象，不允许覆盖；只能保留。')
                info['field_actions'][key]='keep';continue
            if action=='keep':
                info['field_actions'][key]='keep';continue
            if f.state=='present':
                info['field_actions'][key]='keep'
                if key in expected and normalize_value(f.value)!=expected[key]:
                    reasons.append(f'{key} 原值 {f.value} 与本次 {expected[key]} 不同；请明确保留或跳过。')
            elif f.state=='protected':
                if action=='fill_confirmed':
                    # An explicit field confirmation is an assertion of blankness, never an erase.
                    needs.append(key);info['field_actions'][key]='fill_confirmed'
                else:
                    info['field_actions'][key]='keep_image'
            elif f.state=='empty':
                needs.append(key);info['field_actions'][key]='fill'
            elif f.state=='uncertain' and f.value is not None and normalize_value(f.value)==expected.get(key):
                # Hidden / weak OCR matching the requested value is a preservation
                # signal, never a reason to duplicate the date. Keep it silently.
                info['field_actions'][key]='keep_text'
            elif action=='fill_confirmed' and not f.text_chars:
                needs.append(key);info['field_actions'][key]='fill_confirmed'
            else:
                info['field_actions'][key]='review'
                reasons.append(f'{key} 仍有无法排除的黑色字形，请选择保留或补写。')
        if not needs:
            if any('未确认' in x or '不同' in x or '不允许' in x for x in reasons):
                info['status']='review';info['reasons']=list(dict.fromkeys(reasons));pending.append(row.id)
            else:
                info['status']='complete' if all(f.state=='present' for f in row.fields.values()) else 'preserved'
            row_data.append(info);continue
        hard=[]
        if not row.geometry_ok:hard.append('锚点几何检查失败，请重新框选，不允许直接确认绕过。')
        if fonts.times is None:
            hard.append('缺少经过验证的 Times New Roman Regular。')
            proposed=[];geo={};layout_errors=[]
        else:
            all_ops,geo,layout_errors=layout_row(row,row_stamp,fonts,decision.get('adjust'))
            proposed=[op for op in all_ops if op.key in needs]
            # Ignore a width issue in a kept native field, but never a collision of a written field.
            hard.extend(layout_errors)
        w,h=analysis.page_sizes[row.page-1]
        for op in proposed:
            if not all(math.isfinite(v) for v in op.bbox) or op.bbox[0]<0 or op.bbox[1]<0 or op.bbox[2]>w or op.bbox[3]>h:
                hard.append('计划文字超出可见页面边界。')
        # Row confirmation can resolve locator uncertainty, not unresolved field/conflict/QA issues.
        unresolved=[]
        for key,f in row.fields.items():
            action=chosen.get(key,'auto')
            if f.state=='uncertain' and action=='auto' and not (f.value is not None and normalize_value(f.value)==expected.get(key)):
                unresolved.append(f'{key} 仍有无法排除的黑色字形。')
            if f.state=='present' and key in expected and normalize_value(f.value)!=expected[key] and action!='keep':
                unresolved.append(f'{key} 原值不同，须明确选择保留。')
            if f.text_chars and action=='fill_confirmed':unresolved.append(f'{key} 已有原生文字，禁止覆盖。')
        # A low template score by itself should not create busywork when the
        # detected row is already corroborated by two native numeric fields and
        # every slot has decisive evidence. This commonly occurs under red seals:
        # the 年/月/日 template score drops, while existing 9 and 12 text objects
        # unambiguously validate the date row geometry.
        decisive = row.geometry_ok and all(f.state not in ('uncertain',) for f in row.fields.values())
        reason_set=set(row.review_reasons)
        # Locator confidence is not a user task by itself. If the field evidence is
        # decisive, a low template score can be accepted automatically. Geometry-
        # inferred anchors need independent repetition on >=2 peer pages before
        # becoming hands-off. Missing/fragmentary anchor ink remains reviewable.
        low_score_only = bool(reason_set) and reason_set <= {'图像匹配分数偏低。'} and decisive
        repeated_geometry = (bool(reason_set) and
                             reason_set <= {'图像匹配分数偏低。','有锚点仅由几何关系推断。'} and
                             decisive and len(row.references) >= 2)
        native_corroborated = (bool(reason_set) and
                               reason_set <= {'图像匹配分数偏低。','有锚点仅由几何关系推断。'} and
                               row.geometry_ok and
                               sum(1 for f in row.fields.values() if f.state=='present' and f.text_chars)>=2 and
                               all(f.state!='uncertain' for f in row.fields.values()))
        soft_locator_only = low_score_only or repeated_geometry or native_corroborated
        approval_needed=bool(row.review_reasons) and mode!='confirm' and not soft_locator_only
        all_reasons=list(dict.fromkeys(hard+unresolved+(row.review_reasons if approval_needed else [])))
        info['geometry']=geo;info['changed_keys']=needs
        info['proposed']=[asdict(x) for x in proposed]
        if all_reasons:
            info['status']='blocked' if hard else 'review';info['reasons']=all_reasons;pending.append(row.id)
        else:
            info['status']='write';info['operations']=[asdict(x) for x in proposed];operations.extend(proposed)
        row_data.append(info)
    # Hard compliance invariant: every emitted glyph must be an Arabic digit in
    # Times New Roman. This is intentionally stricter than the UI and protects
    # against future planner regressions re-introducing 年/月/日 or separators.
    bad=[op for op in operations if op.font!='times' or not op.text.isdigit()]
    if bad:
        raise ValueError('合规保护：程序只允许新增阿拉伯数字，禁止新增中文、标点或分隔符。')
    # No output may rely on two overlapping independent insertion rectangles.
    for i,a in enumerate(operations):
        for b in operations[i+1:]:
            if a.page!=b.page or a.row_id==b.row_id:continue
            if max(a.bbox[0],b.bbox[0])<min(a.bbox[2],b.bbox[2]) and max(a.bbox[1],b.bbox[1])<min(a.bbox[3],b.bbox[3]):
                raise ValueError('两条日期行的写入区域相互重叠，请取消重复的候选。')
    if any(f.state=='protected' for r in analysis.rows for f in r.fields.values()):
        warnings.append('部分图像中的原有字形仅被保留，未自动确认其数值。')
    missing_pages=sorted(set(range(1,analysis.page_count+1))-{r.page for r in analysis.rows})
    if missing_pages:warnings.append('未识别日期栏的物理页：'+','.join(map(str,missing_pages))+'；这些页不自动修改。')
    return Plan(stamp.isoformat(),analysis.source_hash,operations,row_data,pending,skipped,optional,warnings)
