'use strict';
const $ = id => document.getElementById(id);
const STATUS = {write:'自动填入',review:'需检查',blocked:'需定位',manual:'人工处理',complete:'已有日期',preserved:'保留原字',optional:'可选',skipped:'不处理'};
const PRIORITY = {blocked:0,review:1,manual:2,optional:3,write:4,complete:5,preserved:6,skipped:7};
const FIELD = {year:'年份',month:'月份',day:'日期',full:'自由日期栏',grid:'方格日期'};
const state = {token:'',id:null,doc:null,page:1,row:null,mutating:false,building:false,manual:null,preview:null,previewTicket:0,pollTicket:0};
let toastTimer;
const today = () => { const d=new Date();return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; };
$('stampDate').value=today();
function toast(message,error=false){clearTimeout(toastTimer);$('toast').textContent=message;$('toast').classList.toggle('error',error);$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,error?11000:5500);}
async function api(path,body,options={}){
  const headers={'X-Session-Token':state.token,...(options.headers||{})};
  let data=body;
  if(body!==undefined && !(body instanceof Blob)){headers['Content-Type']='application/json';data=JSON.stringify(body);}
  const res=await fetch(path,{method:body===undefined?'GET':'POST',headers,body:data,cache:'no-store'});
  const result=await res.json();
  if(!res.ok || result.error)throw new Error(result.error||`请求失败 ${res.status}`);
  return result;
}
function taskBody(extra={}){return {id:state.id,revision:state.doc.revision,...extra};}
function activeRow(){return state.doc?.rows.find(r=>r.id===state.row)||null;}
function pageOverride(page=state.page){return state.doc?.page_dates?.[String(page)]||null;}
function effectiveDate(page=state.page){return pageOverride(page)||state.doc?.date||$('stampDate').value||today();}
function setScreen(name){for(const k of ['welcome','loading','workspace'])$(k).hidden=k!==name;}
function setFreeze(on){state.mutating=on;for(const e of document.querySelectorAll('#workspace button,#workspace select,#workspace input'))e.disabled=on;}
function node(tag,className,text){const e=document.createElement(tag);if(className)e.className=className;if(text!==undefined)e.textContent=text;return e;}
async function openFile(file){
  if(!file)return;if(!file.name.toLowerCase().endsWith('.pdf'))return toast('请选择 PDF 文件。',true);
  if(file.size>80*1024*1024)return toast('单个 PDF 不能超过 80MB。',true);
  if(state.building)return toast('请等待当前生成任务完成。',true);
  const old=state.id;
  state.pollTicket++;state.previewTicket++;state.preview=null;state.manual=null;
  if(old){api('/api/close',{id:old}).catch(()=>{});}
  setScreen('loading');$('loadName').textContent=file.name;$('phase').textContent='读取文件';$('bar').style.width='0%';$('percent').textContent='';
  try{
    const t=await api('/api/upload',file,{headers:{'Content-Type':'application/pdf','X-File-Name':encodeURIComponent(file.name),'X-Date':$('stampDate').value||today()}});
    state.id=t.id;state.doc=null;state.page=1;state.row=null;
    await poll(false);
  }catch(e){setScreen('welcome');toast(e.message,true);}
}
async function poll(building){
  const ticket=++state.pollTicket;
  while(ticket===state.pollTicket){
    const s=await api(`/api/status?id=${encodeURIComponent(state.id)}`);
    if(ticket!==state.pollTicket)return;
    if(s.state==='error')throw new Error(s.error||'识别失败。');
    const percent=s.total?Math.round(100*s.done/s.total):0;
    if(building){$('actionSummary').textContent=`${s.phase||'生成中'}${s.total?` ${s.done} / ${s.total}`:''}`;}
    else{$('phase').textContent=s.phase||'识别日期';$('bar').style.width=percent+'%';$('percent').textContent=s.total?`${s.done} / ${s.total}`:'';}
    if(s.state==='ready'){
      state.building=false;setFreeze(false);
      const d=await api(`/api/document?id=${encodeURIComponent(state.id)}`);
      receiveDocument(d,!state.doc);
      if(s.error)toast(s.error,true);
      else if(building)toast('写入及逐字段校验通过。');
      return;
    }
    await new Promise(r=>setTimeout(r,350));
  }
}
function receiveDocument(doc,initial=false){
  state.doc=doc;setScreen('workspace');
  if(initial){const ordered=[...doc.rows].sort((a,b)=>(PRIORITY[a.plan.status]-PRIORITY[b.plan.status])||a.page-b.page);state.page=ordered[0]?.page||1;state.row=ordered[0]?.id||null;}
  $('filename').textContent=doc.name;$('filename').title=doc.name;$('stampDate').value=doc.date;
  const c=doc.counts;const write=c.write||0;const review=(c.review||0)+(c.blocked||0);const manual=c.manual||0;const optional=c.optional||0;
  const overrideCount=Object.keys(doc.page_dates||{}).length;
  $('summary').textContent=`${doc.total} 页 · ${write} 栏自动处理${review?` · ${review} 栏需确认`:''}${manual?` · ${manual} 栏人工处理`:''}${optional?` · ${optional} 栏可选`:''}${overrideCount?` · ${overrideCount} 页特殊日期`:''}`;
  $('fontButton').textContent=doc.font.ready?'Times New Roman':'设置字体';$('fontButton').title=doc.font.source;$('fontSource').textContent=doc.font.source;
  $('actionSummary').textContent=doc.download_ready?`已校验 ${doc.result.changed_pages.length} 页 · ${doc.result.field_count} 次数字写入`:(review?`${write} 栏已自动规划 · ${review} 栏需确认`:`${write} 栏已自动规划${doc.write_count?` · ${doc.write_count} 个数字待写入 · 可直接生成`:''}`);
  $('auditSummary').textContent=doc.download_ready?`字段框外改动 ${doc.result.outside_pixels}${manual?` · ${manual} 栏保持原样待人工处理`:''}${doc.result.pending_rows.length?' · 仅处理已确认部分':''}`:(doc.signed?'检测到有效 PDF 数字签名：已自动识别日期，但为避免签名失效不生成修改件，请使用未签名原件。':(manual?`${manual} 栏自由日期保持原样，导出后人工填写`:(optional?`${optional} 栏默认保留`:'仅补数字 · 原件不覆盖')));
  $('download').hidden=!doc.download_ready;$('reportDownload').hidden=!doc.download_ready;
  $('generate').textContent=doc.download_ready?'重新生成':'生成 PDF';$('generate').disabled=!doc.write_count||doc.signed||state.mutating;
  drawPageList();showPage(state.page,state.row);
}
function pageStatus(page){const rs=state.doc.rows.filter(r=>r.page===page);return rs.length?[...rs].sort((a,b)=>PRIORITY[a.plan.status]-PRIORITY[b.plan.status])[0].plan.status:'none';}
function drawPageList(){
  $('pageList').replaceChildren();
  for(let p=1;p<=state.doc.total;p++){
    const status=pageStatus(p);
    if($('pageFilter').value==='attention'&&!['review','blocked','manual','optional'].includes(status))continue;
    const b=node('button','page-item');b.type='button';b.classList.toggle('selected',p===state.page);b.dataset.page=p;b.setAttribute('aria-label',`第 ${p} 页，${STATUS[status]||'未识别'}`);
    const img=node('img');img.src=`/api/thumb?id=${state.id}&page=${p}`;img.loading='lazy';img.alt=`第 ${p} 页缩略图`;
    const cap=node('div','page-caption');cap.append(node('span','',String(p).padStart(2,'0')),node('span',['review','blocked','manual','optional'].includes(status)?'attention':'',STATUS[status]||'未识别'));
    b.append(img,cap);b.addEventListener('click',()=>{if(!state.manual&&!state.mutating&&!state.building)showPage(p);});$('pageList').append(b);
  }
}
function showPage(p,rowId=null){
  state.page=Math.min(state.doc.total,Math.max(1,p));
  const rs=state.doc.rows.filter(r=>r.page===state.page);
  state.row=rs.find(r=>r.id===rowId)?.id||[...rs].sort((a,b)=>PRIORITY[a.plan.status]-PRIORITY[b.plan.status])[0]?.id||null;
  $('pageTitle').textContent=`第 ${state.page} 页`;
  const url=`/api/page?id=${state.id}&page=${state.page}`;if(!$('pageImage').src.endsWith(url))$('pageImage').src=url;
  const [w,h]=state.doc.page_sizes[state.page-1];$('overlay').setAttribute('viewBox',`0 0 ${w} ${h}`);
  for(const b of $('pageList').querySelectorAll('.page-item'))b.classList.toggle('selected',Number(b.dataset.page)===state.page);
  $('rowSelect').hidden=rs.length<2;$('rowSelect').replaceChildren();rs.forEach((r,i)=>{const o=node('option','',`日期栏 ${i+1}`);o.value=r.id;$('rowSelect').append(o);});$('rowSelect').value=state.row||'';
  drawInspector();drawOverlay();sizePaper();
}
function fieldStatus(f,key=null){if(key==='grid'){const compact=effectiveDate().replaceAll('-','');if(f.state==='present')return `已有 ${f.value}`;if(f.state==='empty')return `8 格 · 自动填写 ${compact}`;if(f.state==='protected')return '方格内有原字，保留';return f.value?`已见 ${f.value}，待核对`:'方格内有遮挡，待核对';}if(f.state==='present')return `已有 ${f.value}`;if(f.state==='empty')return '空白，待补';if(f.state==='protected')return '原图有字形，保留';return f.value?`文字层有 ${f.value}，待确认`:'状态待确认';}
function drawInspector(){
  const r=activeRow();$('noRow').hidden=!!r;$('rowPanel').hidden=!r;
  $('rowStatus').textContent=r?STATUS[r.plan.status]:'未识别';$('rowStatus').className='status '+(r?.plan.status||'');
  if(!r){$('pageDateControl').hidden=true;return;}
  const interactive=['review','blocked','optional'].includes(r.plan.status);
  $('fields').replaceChildren();
  for(const [key,f] of Object.entries(r.fields)){
    const line=node('div','field'+(f.state==='present'?' native':''));
    const label=node('div','field-label');label.append(node('strong','',FIELD[key]),node('span','',fieldStatus(f,key)));line.append(label);
    // Normal auto-planned rows stay read-only: the inspector should confirm the
    // result, not force the user to answer three redundant dropdown questions.
    if(interactive && r.plan.status!=='blocked'){
      const sel=node('select');sel.setAttribute('aria-label',FIELD[key]+'处理方式');
      for(const [value,text] of [['auto','按识别结果'],['keep','保留原样'],['fill_confirmed','确认空白并补写']]){
        const o=node('option','',text);o.value=value;if(value==='fill_confirmed'&&f.has_text)o.disabled=true;sel.append(o);
      }
      sel.value=r.decision?.fields?.[key]||'auto';
      sel.addEventListener('change',async()=>{
        if(sel.value==='fill_confirmed'&&f.state!=='empty'&&!window.confirm('这会新增数字，不会擦除原字。请确认这一格确实没有日期数字。')){sel.value=r.decision?.fields?.[key]||'auto';return;}
        const d=structuredClone(r.decision||{});d.fields={...(d.fields||{}),[key]:sel.value};await saveDecision(d);
      });
      line.append(sel);
    }
    $('fields').append(line);
  }
  const reasons=r.plan.reasons||[];$('rowReasons').hidden=!reasons.length;$('rowReasons').textContent=reasons.join(' ');
  const manualOnly=r.plan.status==='manual';
  const actionBox=$('confirmRow').parentElement;
  actionBox.hidden=manualOnly||!['review','blocked','optional'].includes(r.plan.status);
  $('rowLinks').hidden=manualOnly;
  $('comparison').hidden=manualOnly;
  $('detailsPanel').hidden=manualOnly;
  const hasDigitRow=state.doc.rows.some(x=>x.page===state.page&&x.mode!=='full_date');
  $('pageDateControl').hidden=!hasDigitRow;
  if(hasDigitRow){
    const override=pageOverride();
    $('pageDate').value=effectiveDate();
    $('pageDateMode').textContent=override?'单页日期':'跟随默认';
    $('pageDateMode').classList.toggle('override',!!override);
    $('resetPageDate').hidden=!override;
  }
  $('confirmRow').hidden=r.plan.status==='blocked';
  $('confirmRow').textContent='按预览处理';
  $('confirmRow').disabled=state.mutating;
  $('relocate').textContent=r.mode==='grid_date'?'重框方格':'重新定位';
  $('skipRow').textContent=r.plan.status==='blocked'?'暂不处理':'不处理';
  $('skipRow').disabled=r.plan.status==='skipped'||state.mutating;
  $('sizeAdjust').value=r.decision?.adjust?.size||'';$('dxAdjust').value=r.decision?.adjust?.dx||0;$('dyAdjust').value=r.decision?.adjust?.dy||0;
  $('evidence').replaceChildren();
  const geo=r.plan.geometry||{};
  const entries=[r.method,geo.size?`字号 ${geo.size}pt；${geo.size_source}。`:'',geo.year_gap?`年份至“年”的留白 ${geo.year_gap}pt。`:'',r.references.length?`同版式交叉参照：${r.references.join('、')} 页。只参考相对字距，不复制坐标。`:'未套用其他页面的绝对坐标。'];
  for(const [k,f] of Object.entries(r.fields))entries.push(`${FIELD[k]}：${f.reasons.join(' ')}`);
  entries.filter(Boolean).forEach(t=>$('evidence').append(node('p','',t)));
  if(r.mode==='grid_date')$('evidence').append(node('p','','方格日期按 YYYYMMDD 排列，每格只写一个 Times New Roman 数字。'));
  if(manualOnly){
    $('fields').replaceChildren();
    const line=node('div','field');const label=node('div','field-label');label.append(node('strong','','自由日期栏'),node('span','','保持原样 · 导出后人工填写'));line.append(label);$('fields').append(line);
    state.preview=null;$('before').removeAttribute('src');$('after').removeAttribute('src');$('enlargePreview').disabled=true;
    return;
  }
  refreshPreview();
}
async function saveDecision(decision){
  if(state.mutating||state.building)return;setFreeze(true);
  try{const doc=await api('/api/settings',taskBody({row:state.row,decision}));setFreeze(false);receiveDocument(doc);}
  catch(e){setFreeze(false);toast(e.message,true);receiveDocument(state.doc);}
}
function drawOverlay(){
  const svg=$('overlay');svg.replaceChildren();
  function rect(box,cls){const el=document.createElementNS('http://www.w3.org/2000/svg','rect');for(const [k,v] of Object.entries({x:box[0],y:box[1],width:box[2]-box[0],height:box[3]-box[1]}))el.setAttribute(k,String(v));el.setAttribute('class',cls);svg.append(el);}
  if(state.manual){
    for(const b of Object.values(state.manual.anchors))rect(b,'anchor-outline');
    if(state.manual.grid)rect(state.manual.grid,'anchor-outline');
    if(state.manual.drag)rect(state.manual.drag,'anchor-outline');
    return;
  }
  const r=activeRow();if(!r)return;rect(r.region,'row-outline');
  const proposed=r.plan.proposed||[];
  if(r.mode==='full_date'&&proposed.length){
    const b=[Math.min(...proposed.map(x=>x.bbox[0])),Math.min(...proposed.map(x=>x.bbox[1])),Math.max(...proposed.map(x=>x.bbox[2])),Math.max(...proposed.map(x=>x.bbox[3]))];
    rect(b,'field-outline');
  }else for(const op of proposed)rect(op.bbox,'field-outline');
}
function sizePaper(){
  const scroll=$('paperScroll');if(scroll.clientWidth<10)return;
  const style=getComputedStyle(scroll);const width=scroll.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight);
  $('paper').style.width=Math.round(width*Number($('zoom').value))+'px';
}
async function refreshPreview(){
  const r=activeRow();const ticket=++state.previewTicket;state.preview=null;
  $('before').removeAttribute('src');$('after').removeAttribute('src');$('enlargePreview').disabled=true;
  if(!r||r.plan.status==='manual')return;if(!state.doc.font.ready && r.plan.proposed?.length){$('previewNote').textContent='设置字体后可预览。';return;}
  $('previewNote').textContent='渲染中…';
  try{
    const preview=await api('/api/preview',taskBody({row:r.id}));
    if(ticket!==state.previewTicket)return;
    state.preview=preview;$('before').src=preview.before;$('after').src=preview.after;
    $('afterLabel').textContent=preview.tentative?'当前预览':'写入后';
    $('previewNote').textContent=preview.tentative?(r.plan.status==='optional'?'候选默认不写。':(['complete','preserved','skipped'].includes(r.plan.status)?'原内容保持不变。':'确认后才会写入。')):'与最终生成使用同一份坐标计划。';
    $('enlargePreview').disabled=false;
  }catch(e){if(ticket===state.previewTicket){$('previewNote').textContent=e.message;}}
}
function beginManual(){
  if(state.mutating||state.building)return;
  const r=activeRow();
  const kind=r?.mode==='grid_date'?'grid':'ymd';
  state.manual={kind,anchors:{},key:0,start:null,drag:null,row:state.row,grid:null};
  $('overlay').classList.add('manual');$('cancelManual').hidden=false;$('saveManual').hidden=true;$('manualHint').hidden=false;
  $('zoom').value='2';sizePaper();
  if(kind==='grid'){
    $('manualHint').textContent='框住整排 8 个日期格';
    toast('只需拖一次：框住整排 8 个日期格。');
  }else{
    $('manualHint').textContent='框选“年”';
    toast('依次紧贴“年”“月”“日”三个字拖出矩形框。');
  }
  drawOverlay();
}
function cancelManual(){state.manual=null;$('overlay').classList.remove('manual');$('manualHint').hidden=true;$('cancelManual').hidden=true;$('saveManual').hidden=true;drawOverlay();}
function eventPoint(e){const r=$('overlay').getBoundingClientRect();const [w,h]=state.doc.page_sizes[state.page-1];return [Math.max(0,Math.min(w,(e.clientX-r.left)/r.width*w)),Math.max(0,Math.min(h,(e.clientY-r.top)/r.height*h))];}
$('overlay').addEventListener('pointerdown',e=>{if(!state.manual)return;if(state.manual.kind==='ymd'&&state.manual.key>=3)return;if(state.manual.kind==='grid'&&state.manual.grid)return;e.preventDefault();state.manual.start=eventPoint(e);$('overlay').setPointerCapture(e.pointerId);});
$('overlay').addEventListener('pointermove',e=>{if(!state.manual?.start)return;const a=state.manual.start,b=eventPoint(e);state.manual.drag=[Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.max(a[0],b[0]),Math.max(a[1],b[1])];drawOverlay();});
$('overlay').addEventListener('pointerup',e=>{
  const m=state.manual;if(!m?.start)return;const b=eventPoint(e),a=m.start;const box=[Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.max(a[0],b[0]),Math.max(a[1],b[1])];m.start=null;m.drag=null;
  const w=box[2]-box[0],h=box[3]-box[1];
  if(m.kind==='grid'){
    if(w<48||h<5||w>360||h>70||w/h<3.2){toast('请只框住一整排 8 个日期格，不要包含上方文字或印章主体。',true);drawOverlay();return;}
    m.grid=box;$('manualHint').textContent='检查方格范围后保存';$('saveManual').hidden=false;drawOverlay();return;
  }
  if(w<3||h<3||w>45||h>45){toast('请紧贴一个汉字框选，框不能过小或覆盖整行。',true);drawOverlay();return;}
  const keys=['year','month','day'];m.anchors[keys[m.key]]=box;m.key++;
  $('manualHint').textContent=m.key<3?`框选“${['年','月','日'][m.key]}”`:'检查三个框后保存';$('saveManual').hidden=m.key!==3;drawOverlay();
});
$('saveManual').addEventListener('click',async()=>{
  const m=state.manual;if(!m)return;
  if(m.kind==='grid'&&!m.grid)return;
  if(m.kind==='ymd'&&m.key!==3)return;
  try{
    const path=m.kind==='grid'?'/api/manual_grid':'/api/manual';
    const body=m.kind==='grid'?{page:state.page,row:m.row,grid:m.grid}:{page:state.page,row:m.row,anchors:m.anchors};
    const doc=await api(path,taskBody(body));cancelManual();receiveDocument(doc);toast(m.kind==='grid'?'方格范围已更新。':'已保存本栏定位。');
  }catch(e){toast(e.message,true);}
});
$('cancelManual').addEventListener('click',cancelManual);
async function generate(partial=false){
  if(state.building||state.mutating)return;
  if(!state.doc.font.ready){$('fontDialog').showModal();return;}
  if(state.doc.pending.length&&!partial){$('partialText').textContent=`${state.doc.pending.length} 栏尚未确认。继续导出时，这些栏将保持原样，不会被记为已完成。`;$('partialDialog').showModal();return;}
  state.building=true;setFreeze(true);$('download').hidden=true;$('reportDownload').hidden=true;
  try{await api('/api/build',taskBody({plan_hash:state.doc.plan_hash,allow_partial:partial}));await poll(true);}
  catch(e){state.building=false;setFreeze(false);receiveDocument(state.doc);toast(e.message,true);}
}
function download(kind){const a=document.createElement('a');a.href=`/api/download?id=${state.id}&kind=${kind}`;a.download='';document.body.append(a);a.click();a.remove();}
$('generate').addEventListener('click',()=>generate(false));$('buildPartial').addEventListener('click',()=>{$('partialDialog').close();generate(true);});$('download').addEventListener('click',()=>download('pdf'));$('reportDownload').addEventListener('click',()=>download('report'));
function confirmDecision(r){
  const d=structuredClone(r.decision||{});d.mode='confirm';d.fields={...(d.fields||{})};
  for(const [key,f] of Object.entries(r.fields||{})){
    // One click must actually resolve the row. Unknown image-only occupancy is
    // the exact case the user is confirming as blank; native text is never overwritten.
    if(f.state==='uncertain' && !f.has_text)d.fields[key]='fill_confirmed';
    else if(f.has_text || f.state==='present' || f.state==='protected')d.fields[key]='keep';
  }
  return d;
}
$('confirmRow').addEventListener('click',()=>{const r=activeRow();if(!r)return;saveDecision(confirmDecision(r));});
$('skipRow').addEventListener('click',()=>{const r=activeRow();if(r)saveDecision({...structuredClone(r.decision||{}),mode:'skip'});});
$('autoRow').addEventListener('click',async()=>{const r=activeRow();if(!r)return;if(r.method==='手动框选锚点'){try{receiveDocument(await api('/api/reset_row',taskBody({row:r.id})));}catch(e){toast(e.message,true);}}else saveDecision({mode:'auto',fields:{}});});
$('applyAdjust').addEventListener('click',()=>{const r=activeRow();if(!r)return;const size=$('sizeAdjust').value?Number($('sizeAdjust').value):null,dx=Number($('dxAdjust').value||0),dy=Number($('dyAdjust').value||0);if((size!==null&&(!Number.isFinite(size)||size<4||size>32))||![dx,dy].every(v=>Number.isFinite(v)&&Math.abs(v)<=12))return toast('请检查字号和偏移范围。',true);saveDecision({...structuredClone(r.decision||{}),adjust:{size,dx,dy},mode:'confirm'});});
$('relocate').addEventListener('click',beginManual);$('addManual').addEventListener('click',beginManual);
$('stampDate').addEventListener('change',async()=>{if(!state.doc||!$('stampDate').value)return;setFreeze(true);try{const d=await api('/api/settings',taskBody({date:$('stampDate').value}));setFreeze(false);receiveDocument(d);}catch(e){setFreeze(false);toast(e.message,true);}});
$('pageDate').addEventListener('change',async()=>{
  if(!state.doc||!$('pageDate').value||state.mutating||state.building)return;
  const page=state.page,value=$('pageDate').value;setFreeze(true);
  try{const d=await api('/api/settings',taskBody({page,page_date:value}));setFreeze(false);receiveDocument(d);toast(value===d.date?'本页已恢复默认日期。':'本页日期已单独设置。');}
  catch(e){setFreeze(false);toast(e.message,true);receiveDocument(state.doc);}
});
$('resetPageDate').addEventListener('click',async()=>{
  if(!state.doc||state.mutating||state.building)return;const page=state.page;setFreeze(true);
  try{const d=await api('/api/settings',taskBody({page,page_date:null}));setFreeze(false);receiveDocument(d);toast('本页已跟随默认日期。');}
  catch(e){setFreeze(false);toast(e.message,true);receiveDocument(state.doc);}
});
$('pageFilter').addEventListener('change',drawPageList);$('rowSelect').addEventListener('change',()=>{state.row=$('rowSelect').value;drawInspector();drawOverlay();});$('zoom').addEventListener('change',sizePaper);$('pageImage').addEventListener('load',sizePaper);new ResizeObserver(()=>{if(state.doc)sizePaper();}).observe($('paperScroll'));
$('fontButton').addEventListener('click',()=>$('fontDialog').showModal());$('chooseFont').addEventListener('click',()=>$('fontInput').click());
$('fontInput').addEventListener('change',async()=>{const f=$('fontInput').files[0];if(!f)return;try{const r=await api('/api/font',f,{headers:{'Content-Type':'application/octet-stream',...(state.id?{'X-Task-Id':state.id}:{})}});$('fontSource').textContent=r.font.source;$('fontDialog').close();if(state.id)receiveDocument(await api(`/api/document?id=${state.id}`));toast('Times New Roman Regular 已通过检查。');}catch(e){toast(e.message,true);}finally{$('fontInput').value='';}});
$('enlargePreview').addEventListener('click',()=>{if(!state.preview)return;$('largeBefore').src=state.preview.before;$('largeAfter').src=state.preview.after;$('previewTitle').textContent=`第 ${state.page} 页 · 日期栏`;$('previewDialog').showModal();});
for(const btn of document.querySelectorAll('.close-dialog'))btn.addEventListener('click',()=>btn.closest('dialog').close());
$('chooseFile').addEventListener('click',e=>{e.stopPropagation();$('fileInput').click();});$('drop').addEventListener('click',()=>$('fileInput').click());$('drop').addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();$('fileInput').click();}});$('replaceFile').addEventListener('click',()=>$('fileInput').click());$('fileInput').addEventListener('change',()=>{openFile($('fileInput').files[0]);$('fileInput').value='';});
for(const name of ['dragenter','dragover'])document.addEventListener(name,e=>{e.preventDefault();if(!$('welcome').hidden)$('drop').classList.add('drag');});
document.addEventListener('dragleave',e=>{if(!e.relatedTarget)$('drop').classList.remove('drag');});document.addEventListener('drop',e=>{e.preventDefault();$('drop').classList.remove('drag');if(e.dataTransfer.files.length)openFile(e.dataTransfer.files[0]);});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&state.manual)cancelManual();if(!state.doc||state.manual||state.mutating||state.building||['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName)||document.querySelector('dialog[open]'))return;if(e.key==='ArrowRight'||e.key==='ArrowDown'){e.preventDefault();showPage(state.page+1);}if(e.key==='ArrowLeft'||e.key==='ArrowUp'){e.preventDefault();showPage(state.page-1);}});
api('/api/info').then(info=>{state.token=info.token;if(info.version!=='22.1.0')toast('检测到旧的本机服务，请关闭旧终端并从当前文件夹重新启动。',true);}).catch(e=>toast('无法连接本机服务，请使用启动脚本打开网页。'+e.message,true));
