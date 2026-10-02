#!/usr/bin/env python3
"""Local-only UI for the evidence-driven date tool. No external API calls."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse,parse_qs,unquote,quote
from collections import Counter
import atexit
import base64
import copy
import io
import json
import math
import os
import secrets
import shutil
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
import fitz
from PIL import Image

from core.models import VERSION, Operation, union_rect
from core.fonts import FontResolver
from core.document import analyze, analyze_resilient, cross_check
from core.evidence import render, extract_characters, make_row
from core.planner import make_plan
from core.writer import build, write_operations

BASE=Path(__file__).resolve().parent
WORK=Path(tempfile.mkdtemp(prefix='luokuan-'))
os.chmod(WORK,0o700)
TOKEN=secrets.token_urlsafe(32)
TASKS={}
STATE=threading.RLock()
MUTATIONS=threading.RLock()
PDF_LOCK=threading.RLock()  # Planning/build mutation guard; independent render documents use ASSET_SEM.
ASSET_SEM=threading.BoundedSemaphore(4)
EXECUTOR=ThreadPoolExecutor(max_workers=1,thread_name_prefix='pdf-worker')
FONT_OVERRIDE=os.environ.get('LUOKUAN_TIMES_PATH')
MAX_UPLOAD=80*1024*1024


def cleanup():
    EXECUTOR.shutdown(wait=False,cancel_futures=True)
    shutil.rmtree(WORK,ignore_errors=True)
atexit.register(cleanup)


def image_bytes(rgb,max_width=None):
    im=Image.fromarray(rgb)
    if max_width and im.width>max_width:
        im=im.resize((max_width,round(im.height*max_width/im.width)),Image.Resampling.LANCZOS)
    buf=io.BytesIO();im.save(buf,format='PNG');return buf.getvalue()


def uri_image(rgb):return 'data:image/png;base64,'+base64.b64encode(image_bytes(rgb)).decode('ascii')


def task_of(tid):
    with STATE:
        t=TASKS.get(tid)
        if not t:raise ValueError('任务不存在或已关闭，请重新上传。')
        t['seen']=time.time()
        return t


def progress(t,phase):
    def update(i,n):
        if t.get('cancelled'):raise ValueError('任务已取消。')
        with STATE:t.update(phase=phase,done=i,total=n)
    return update


def invalidate(t):
    t['out']=None;t['report']=None;t['last_plan']=None
    t['revision']+=1


def ensure_page_asset(t,pno,kind):
    """Render only what is visible in the browser; cache per task."""
    if kind=='thumb':
        dest=t['dir']/f'thumb-{pno}.jpg';dpi=42;quality=76
    else:
        dest=t['dir']/f'page-{pno}.jpg';dpi=112;quality=90
    if dest.is_file():return dest
    # Every request opens its own document, so page objects never cross threads.
    # A small semaphore prevents a thumbnail burst from exhausting RAM while no
    # longer blocking the main-page preview behind one global PDF lock.
    with ASSET_SEM:
        if dest.is_file():return dest
        with fitz.open(t.get('analysis_source',t['source'])) as d:
            if not 1<=pno<=len(d):raise ValueError('页码越界。')
            page=d[pno-1];z=dpi/72
            pix=page.get_pixmap(matrix=fitz.Matrix(z,z),colorspace=fitz.csRGB,alpha=False)
            im=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
            if kind=='thumb':
                im.thumbnail((150,210),Image.Resampling.LANCZOS)
            im.save(dest,format='JPEG',quality=quality,optimize=True)
    return dest


def run_analysis(t):
    try:
        with STATE:t.update(state='analyzing',phase='识别日期',done=0,total=0,error='')
        with PDF_LOCK:
            recovery=t['dir']/'analysis-recovered.pdf'
            def recovering():
                with STATE:t.update(phase='修复 PDF 结构',done=0,total=0)
            a,analysis_source,recovery_method=analyze_resilient(
                t['source'],recovery,progress=progress(t,'识别日期'),recovery_progress=recovering)
            resolver=FontResolver(FONT_OVERRIDE)
            # Do not render 32 full-page previews during upload. They are generated
            # lazily when the browser actually asks for a visible page/thumbnail.
            # Font discovery stops immediately when the system font is available.
            if resolver.times is None:
                with fitz.open(analysis_source) as d:
                    resolver.use_document(d)
            t['analysis']=a;t['analysis_source']=analysis_source;t['recovery_method']=recovery_method
            t['original_rows']=copy.deepcopy(a.rows);t['fonts']=resolver
        phase='就绪' if not recovery_method else '就绪 · 已恢复 PDF 结构'
        with STATE:t.update(state='ready',phase=phase,done=a.page_count,total=a.page_count)
    except Exception as exc:
        msg=str(exc)
        low=msg.lower()
        if 'xref' in low or 'cannot find object' in low or 'object out of range' in low:
            msg='PDF 内部交叉引用结构损坏，自动恢复仍未成功。请换原始导出的 PDF，或把该 PDF 发给我检查。'
        with STATE:t.update(state='error',error=msg)
        traceback.print_exc()


def serialize_task(t):
    if t['state']!='ready':raise ValueError('任务尚未就绪。')
    with PDF_LOCK:
        plan=make_plan(t['analysis'],date.fromisoformat(t['date']),t['fonts'],t['decisions'],t.get('page_dates',{}))
    by_id={x['id']:x for x in plan.rows}
    rs=[]
    for r in t['analysis'].rows:
        j=asdict(r)
        for f in j['fields'].values():
            # Do not send font internals; exact evidence and source are in the detail panel.
            f['has_text']=bool(f.pop('text_chars',[]))
        j['plan']=by_id[r.id];j['decision']=t['decisions'].get(r.id,{})
        rs.append(j)
    counts=Counter(x['status'] for x in plan.rows)
    return {'id':t['id'],'name':t['name'],'version':VERSION,'revision':t['revision'],
            'date':t['date'],'page_dates':{str(k):v for k,v in sorted(t.get('page_dates',{}).items())},'font':t['fonts'].info(),'page_sizes':t['analysis'].page_sizes,
            'rows':rs,'counts':dict(counts),'total':t['analysis'].page_count,
            'plan_hash':plan.digest,'write_count':len(plan.operations),'pending':plan.pending,
            'optional':plan.optional,'warnings':plan.warnings,'signed':t['analysis'].signed,
            'result':t.get('report') and {k:t['report'][k] for k in ('pass','changed_pages','field_count','outside_pixels','pending_rows','manual_rows','optional_unselected_rows')},
            'download_ready':bool(t.get('out')),'structure_recovered':bool(t.get('recovery_method')),'recovery_method':t.get('recovery_method')}


def run_build(t,plan,allow_partial):
    try:
        out=t['dir']/'result.pdf'
        with PDF_LOCK:
            rep=build(t['source'],out,plan,t['fonts'],allow_partial,progress(t,'逐字段校验'))
        with STATE:
            t.update(state='ready',phase='校验通过',out=str(out),report=rep,last_plan=plan.digest,
                     error='',done=t['analysis'].page_count)
    except Exception as exc:
        with STATE:t.update(state='ready',error=str(exc),out=None,report=None,phase='生成未通过')
        traceback.print_exc()


class Handler(BaseHTTPRequestHandler):
    server_version='Luokuan'
    def log_message(self,*args):pass
    def valid_host(self):
        host=self.headers.get('Host','')
        return host in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}')
    def headers_out(self,ctype,length,code=200,name=None):
        self.send_response(code)
        self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(length))
        self.send_header('Cache-Control','no-store, max-age=0')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if name:self.send_header('Content-Disposition',f"attachment; filename*=UTF-8''{quote(name)}")
        self.end_headers()
    def send_json(self,data,code=200):
        b=json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf-8')
        self.headers_out('application/json; charset=utf-8',len(b),code)
        self.wfile.write(b)
    def send_file(self,p,ctype,name=None):
        p=Path(p)
        if not p.is_file():return self.send_json({'error':'文件未就绪。'},404)
        b=p.read_bytes();self.headers_out(ctype,len(b),name=name);self.wfile.write(b)
    def read_body(self,limit=MAX_UPLOAD):
        n=int(self.headers.get('Content-Length','0'))
        if not 0<n<=limit:raise ValueError('请求为空或超过大小上限。')
        b=self.rfile.read(n)
        if len(b)!=n:raise ValueError('上传不完整，请重试。')
        return b
    def do_GET(self):
        try:
            if not self.valid_host():return self.send_json({'error':'仅接受本机地址。'},403)
            u=urlparse(self.path);q=parse_qs(u.query);p=u.path
            if p in ('/','/index.html'):return self.send_file(BASE/'static/index.html','text/html; charset=utf-8')
            if p in ('/static/app.js','/static/style.css'):
                return self.send_file(BASE/p[1:],'text/javascript; charset=utf-8' if p.endswith('.js') else 'text/css; charset=utf-8')
            if p=='/api/info':return self.send_json({'version':VERSION,'token':TOKEN,'local_only':True})
            t=task_of((q.get('id') or [''])[0])
            if p=='/api/status':
                return self.send_json({k:t.get(k) for k in ('state','phase','done','total','error','revision')})
            if p=='/api/document':return self.send_json(serialize_task(t))
            if p in ('/api/page','/api/thumb'):
                pno=int((q.get('page') or ['0'])[0])
                if not 1<=pno<=t.get('analysis').page_count:raise ValueError('页码越界。')
                kind='page' if p=='/api/page' else 'thumb'
                return self.send_file(ensure_page_asset(t,pno,kind),'image/jpeg')
            if p=='/api/download':
                if t['state']!='ready' or not t.get('out'):raise ValueError('当前设置尚未生成通过校验的文件。')
                stamp=date.fromisoformat(t['date']);suffix=f'【电子落款{stamp.year}年{stamp.month}月{stamp.day}日】'
                if t['report'].get('pending_rows'):suffix+='【部分处理】'
                manual_count=len(t['report'].get('manual_rows') or [])
                if manual_count:suffix+=f'【需人工补日期{manual_count}处】'
                if (q.get('kind') or ['pdf'])[0]=='report':
                    return self.send_file(Path(t['out']).with_suffix('.audit.json'),'application/json; charset=utf-8',Path(t['name']).stem+suffix+'_校验.json')
                return self.send_file(t['out'],'application/pdf',Path(t['name']).stem+suffix+'.pdf')
            return self.send_json({'error':'接口不存在。'},404)
        except (ValueError,KeyError,TypeError) as exc:return self.send_json({'error':str(exc)},400)
        except (BrokenPipeError,ConnectionResetError):return
        except Exception as exc:
            traceback.print_exc();return self.send_json({'error':'处理失败：'+str(exc)},500)
    def do_POST(self):
        with MUTATIONS:
            return self._do_POST()
    def _do_POST(self):
        try:
            if not self.valid_host() or self.headers.get('X-Session-Token')!=TOKEN:
                return self.send_json({'error':'会话失效，请刷新当前网页。'},403)
            origin=self.headers.get('Origin')
            if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'):
                return self.send_json({'error':'跨站请求已拒绝。'},403)
            path=urlparse(self.path).path
            if path=='/api/upload':return self.upload()
            if path=='/api/font':return self.select_font()
            body=json.loads(self.read_body(1024*1024).decode())
            t=task_of(body.get('id',''))
            if path=='/api/close':
                with STATE:t['cancelled']=True
                if t['state'] in ('ready','error'):shutil.rmtree(t['dir'],ignore_errors=True)
                with STATE:TASKS.pop(t['id'],None)
                return self.send_json({'ok':True})
            if t['state']!='ready':return self.send_json({'error':'正在处理，请等待当前任务结束。'},409)
            if body.get('revision')!=t['revision']:
                return self.send_json({'error':'页面设置已更新，请重新加载。'},409)
            if path=='/api/settings':
                if 'date' in body:
                    date.fromisoformat(body['date']);t['date']=body['date']
                    # Overrides equal to the new default are redundant. Keeping only
                    # real exceptions makes the UI and audit state deterministic.
                    t['page_dates']={p:d for p,d in t.get('page_dates',{}).items() if d!=t['date']}
                if 'page_date' in body:
                    pno=int(body.get('page',0))
                    if not 1<=pno<=t['analysis'].page_count:raise ValueError('页码越界。')
                    value=body.get('page_date')
                    if value in (None,''):
                        t.setdefault('page_dates',{}).pop(pno,None)
                    else:
                        date.fromisoformat(value)
                        if value==t['date']:t.setdefault('page_dates',{}).pop(pno,None)
                        else:t.setdefault('page_dates',{})[pno]=value
                if 'row' in body:
                    rid=body['row']
                    if rid not in {r.id for r in t['analysis'].rows}:raise ValueError('日期栏不存在。')
                    decision=body.get('decision',{})
                    # Validate a candidate settings change before committing it.
                    ds=copy.deepcopy(t['decisions']);ds[rid]=decision
                    with PDF_LOCK:make_plan(t['analysis'],date.fromisoformat(t['date']),t['fonts'],ds,t.get('page_dates',{}))
                    t['decisions']=ds
                with STATE:invalidate(t)
                return self.send_json(serialize_task(t))
            if path=='/api/manual':return self.manual(t,body)
            if path=='/api/manual_grid':return self.manual_grid(t,body)
            if path=='/api/reset_row':
                rid=body.get('row')
                original=next((r for r in t['original_rows'] if r.id==rid),None)
                t['analysis'].rows=[r for r in t['analysis'].rows if r.id!=rid]
                if original:t['analysis'].rows.append(copy.deepcopy(original))
                t['decisions'].pop(rid,None);cross_check(t['analysis'].rows);invalidate(t)
                return self.send_json(serialize_task(t))
            if path=='/api/reset':
                t['analysis'].rows=copy.deepcopy(t['original_rows']);t['decisions']={}
                with STATE:invalidate(t)
                return self.send_json(serialize_task(t))
            if path=='/api/preview':return self.preview(t,body)
            if path=='/api/build':
                with PDF_LOCK:plan=make_plan(t['analysis'],date.fromisoformat(t['date']),t['fonts'],t['decisions'],t.get('page_dates',{}))
                if plan.digest!=body.get('plan_hash'):raise ValueError('预览计划已变化，请按新预览重新生成。')
                partial=bool(body.get('allow_partial',False))
                if plan.pending and not partial:raise ValueError('有待确认日期栏，请先确认。')
                if not plan.operations:raise ValueError('没有已确认的待补字段。')
                with STATE:t.update(state='building',phase='写入计划',done=0,total=t['analysis'].page_count,out=None,report=None,error='')
                EXECUTOR.submit(run_build,t,plan,partial)
                return self.send_json({'ok':True})
            return self.send_json({'error':'接口不存在。'},404)
        except (ValueError,KeyError,TypeError,OverflowError) as exc:return self.send_json({'error':str(exc)},400)
        except (BrokenPipeError,ConnectionResetError):return
        except Exception as exc:
            traceback.print_exc();return self.send_json({'error':'处理失败：'+str(exc)},500)
    def upload(self):
        # Lazy cleanup of inactive finished tasks. Nothing is stored in the program directory.
        for tid,t in list(TASKS.items()):
            if t['state'] in ('ready','error') and time.time()-t['seen']>7200:
                shutil.rmtree(t['dir'],ignore_errors=True);TASKS.pop(tid,None)
        if len(TASKS)>=6:raise ValueError('打开的文件过多，请先关闭当前文件或重启工具。')
        data=self.read_body()
        if not data[:1024].lstrip().startswith(b'%PDF-'):raise ValueError('不是有效的 PDF 文件。')
        name=Path(unquote(self.headers.get('X-File-Name','document.pdf')).replace('\\','/')).name
        if not name.lower().endswith('.pdf'):raise ValueError('只接受 PDF 文件。')
        name=name[:200]
        sd=self.headers.get('X-Date') or date.today().isoformat();date.fromisoformat(sd)
        tid=uuid.uuid4().hex;folder=WORK/tid;folder.mkdir(mode=0o700)
        source=folder/'source.pdf';source.write_bytes(data)
        t={'id':tid,'dir':folder,'source':str(source),'name':name,'state':'queued','phase':'等待识别',
           'done':0,'total':0,'error':'','revision':1,'date':sd,'page_dates':{},'decisions':{},'seen':time.time(),
           'out':None,'report':None,'analysis_source':str(source),'recovery_method':None}
        with STATE:TASKS[tid]=t
        EXECUTOR.submit(run_analysis,t)
        return self.send_json({'id':tid,'name':name})
    def select_font(self):
        global FONT_OVERRIDE
        data=self.read_body(20*1024*1024)
        path=WORK/(uuid.uuid4().hex+'.ttf');path.write_bytes(data)
        try:
            with PDF_LOCK:resolver=FontResolver(str(path))
        except Exception:
            path.unlink(missing_ok=True);raise
        FONT_OVERRIDE=str(path)
        tid=self.headers.get('X-Task-Id')
        if tid:
            t=task_of(tid)
            if t['state']!='ready':raise ValueError('请等当前文件处理结束再选择字体。')
            t['fonts']=resolver;invalidate(t)
        return self.send_json({'ok':True,'font':resolver.info()})
    def manual(self,t,body):
        pno=int(body['page']);a=t['analysis']
        if not 1<=pno<=a.page_count:raise ValueError('页码越界。')
        anchors=body.get('anchors',{})
        if set(anchors)!={'year','month','day'}:raise ValueError('需要依次框选年、月、日三个字。')
        w,h=a.page_sizes[pno-1]
        for r in anchors.values():
            if len(r)!=4 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in r):raise ValueError('锚点坐标无效。')
            if not(0<=r[0]<r[2]<=w and 0<=r[1]<r[3]<=h and 3<=r[2]-r[0]<=45 and 3<=r[3]-r[1]<=45):
                raise ValueError('请紧贴单个年月日汉字框选，框的宽高需在 3 至 45pt 之间。')
        rid=body.get('row');existing=next((r for r in a.rows if r.id==rid),None)
        if rid and (not existing or existing.page!=pno):raise ValueError('替换的日期栏不存在。')
        region=union_rect(anchors.values());region=[max(0,region[0]-70),max(0,region[1]-12),min(w,region[2]+12),min(h,region[3]+12)]
        candidate={'anchors':anchors,'region':region,'mode':'ymd','score':1.,'method':'手动框选锚点',
                   'size':None,'baseline':None,'reasons':[],'native':False}
        with PDF_LOCK,fitz.open(t.get('analysis_source',t['source'])) as doc:
            row=make_row(candidate,render(doc[pno-1]),extract_characters(doc[pno-1]),pno,len(a.rows)+1)
        if not row.geometry_ok:raise ValueError('三个框不在同一行，或顺序不是年月日，请重新框选。')
        if existing:
            row.id=existing.id;a.rows[a.rows.index(existing)]=row
        else:a.rows.append(row)
        t['decisions'][row.id]={'mode':'confirm','fields':{}}
        cross_check(a.rows);invalidate(t)
        return self.send_json(serialize_task(t))
    def manual_grid(self,t,body):
        pno=int(body['page']);a=t['analysis']
        if not 1<=pno<=a.page_count:raise ValueError('页码越界。')
        box=body.get('grid')
        if not isinstance(box,list) or len(box)!=4 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in box):
            raise ValueError('方格范围无效。')
        x0,y0,x1,y1=map(float,box);w,h=a.page_sizes[pno-1]
        bw,bh=x1-x0,y1-y0
        if not(0<=x0<x1<=w and 0<=y0<y1<=h and 48<=bw<=360 and 5<=bh<=70 and bw/bh>=3.2):
            raise ValueError('请只框住一整排 8 个日期格。')
        cellw=bw/8.0
        cells=[[x0+i*cellw,y0,x0+(i+1)*cellw,y1] for i in range(8)]
        rid=body.get('row');existing=next((r for r in a.rows if r.id==rid),None)
        if rid and (not existing or existing.page!=pno):raise ValueError('替换的日期栏不存在。')
        region=[max(0,x0-8),max(0,y0-8),min(w,x1+8),min(h,y1+8)]
        candidate={'anchors':{'grid':[x0,y0,x1,y1]},'region':region,'mode':'grid_date','score':1.,
                   'method':'手动框选 8 格日期栏','cells':cells,'reasons':[],'native':False}
        with PDF_LOCK,fitz.open(t.get('analysis_source',t['source'])) as doc:
            row=make_row(candidate,render(doc[pno-1]),extract_characters(doc[pno-1]),pno,len(a.rows)+1)
        if not row.geometry_ok:raise ValueError('方格范围未能形成 8 个规则日期格，请重新框选。')
        if existing:
            row.id=existing.id;a.rows[a.rows.index(existing)]=row
        else:a.rows.append(row)
        t['decisions'][row.id]={'mode':'auto','fields':{}}
        cross_check(a.rows);invalidate(t)
        return self.send_json(serialize_task(t))

    def preview(self,t,body):
        row=next((r for r in t['analysis'].rows if r.id==body.get('row')),None)
        if not row:raise ValueError('日期栏不存在。')
        with PDF_LOCK:
            plan=make_plan(t['analysis'],date.fromisoformat(t['date']),t['fonts'],t['decisions'],t.get('page_dates',{}))
            info=next(x for x in plan.rows if x['id']==row.id)
            ops=[Operation(**x) for x in info['proposed']]
            region=union_rect([row.region]+[o.bbox for o in ops])
            w,h=t['analysis'].page_sizes[row.page-1]
            region=[max(0,region[0]-5),max(0,region[1]-3),min(w,region[2]+5),min(h,region[3]+3)]
            dpi=250;z=dpi/72
            with fitz.open(t.get('analysis_source',t['source'])) as d:
                before=d[row.page-1].get_pixmap(matrix=fitz.Matrix(z,z),clip=fitz.Rect(region),colorspace=fitz.csRGB,alpha=False)
                b0=base64.b64encode(before.tobytes('png')).decode()
                if ops:write_operations(d,ops,t['fonts'])
                after=d[row.page-1].get_pixmap(matrix=fitz.Matrix(z,z),clip=fitz.Rect(region),colorspace=fitz.csRGB,alpha=False)
                b1=base64.b64encode(after.tobytes('png')).decode()
        return self.send_json({'before':'data:image/png;base64,'+b0,'after':'data:image/png;base64,'+b1,
                 'plan_hash':plan.digest,'row':row.id,'status':info['status'],'geometry':info['geometry'],
                 'tentative':info['status']!='write','revision':t['revision']})


def main():
    port=int(os.environ.get('LUOKUAN_PORT','0'))
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    url=f'http://127.0.0.1:{server.server_port}/'
    print(f'电子落款\n{url}\n仅在本机处理。关闭此窗口可停止服务。',flush=True)
    if not os.environ.get('LUOKUAN_NO_BROWSER'):threading.Timer(.6,lambda:webbrowser.open(url)).start()
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()

if __name__=='__main__':main()
