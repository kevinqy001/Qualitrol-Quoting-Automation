'use strict';

const TYPE_INFO = {
  power_transformer:{label:'Power Transformer',color:'#e68c00'},
  gis_bay:{label:'GIS Bay',color:'#1e6edc'},
  circuit_breaker:{label:'Circuit Breaker',color:'#d22828'},
  busbar:{label:'Busbar',color:'#9628be'},
};
const TYPE_ORDER=['power_transformer','gis_bay','circuit_breaker','busbar'];

const state={drawings:[],drawingId:null,session:null,anns:[],selectedId:null,
  layers:{},showLabels:false,scale:1,tx:0,ty:0,imgW:0,imgH:0,addMode:false};

const $=id=>document.getElementById(id);
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
const SVGNS='http://www.w3.org/2000/svg';
const api=(p,o)=>fetch(p,o).then(r=>r.json());

// ------------------------------------------------------------------ init
async function init(){
  const cfg=await api('/api/config');
  state.drawings=cfg.drawings;
  const dsel=$('drawingSelect');
  cfg.drawings.forEach(d=>dsel.add(new Option(d.title.split('—')[0].trim(),d.id)));
  $('engineBadge').textContent = cfg.claude.available
    ? `Claude · ${cfg.claude.transport}` : 'Sample + offline agent';
  bindEvents();
  await selectDrawing(cfg.drawings[0].id);
}

function bindEvents(){
  $('drawingSelect').onchange=e=>selectDrawing(e.target.value);
  $('detectBtn').onclick=runDetection;
  $('addBtn').onclick=toggleAddMode;
  $('zoomIn').onclick=()=>zoomAt(vwCx(),vwCy(),1.25);
  $('zoomOut').onclick=()=>zoomAt(vwCx(),vwCy(),0.8);
  $('fitBtn').onclick=fitToScreen;
  $('lblChk').onchange=e=>{state.showLabels=e.target.checked;renderOverlay();};
  $('acceptAllBtn').onclick=()=>bulkStatus('accepted');
  $('resetBtn').onclick=()=>bulkStatus('pending');
  $('chatSend').onclick=sendChat;
  $('chatInput').addEventListener('keydown',e=>{if(e.key==='Enter')sendChat();});
  $('edAccept').onclick=()=>setStatus(state.selectedId,'accepted');
  $('edReject').onclick=()=>setStatus(state.selectedId,'rejected');
  $('edDelete').onclick=()=>deleteSelected();
  $('edLabel').onchange=()=>saveEdit();
  $('edType').onchange=()=>saveEdit();
  document.querySelectorAll('.tab').forEach(t=>{t.onclick=()=>{
    document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
    t.classList.add('active');
    ['chat','detections','quote'].forEach(n=>$('tab-'+n).classList.toggle('hidden',n!==t.dataset.tab));
    if(t.dataset.tab==='quote')renderQuote();
  };});
  setupViewer();
}

// ------------------------------------------------------------------ session/data
async function selectDrawing(id){
  state.drawingId=id;$('drawingSelect').value=id;
  const s=await api('/api/sessions',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({drawing_id:id})});
  state.session=s;state.anns=s.annotations;state.imgW=s.width;state.imgH=s.height;
  state.selectedId=null;state.layers={};
  $('drawingTitle').textContent=s.title;$('drawingProject').textContent=s.project||'';
  $('sessionText').textContent='session '+s.id;
  $('editor').classList.add('hidden');
  const img=$('drawing');
  await new Promise(res=>{img.onload=res;img.src=`/web/drawings/${s.image}`;});
  $('overlay').setAttribute('viewBox',`0 0 ${s.width} ${s.height}`);
  $('overlay').setAttribute('width',s.width);$('overlay').setAttribute('height',s.height);
  img.width=s.width;img.height=s.height;
  initLayers();fitToScreen();renderAll();
  setText('statusText',`${state.anns.length} components · session ${s.id}`);
}

async function refresh(){
  const r=await api(`/api/sessions/${state.session.id}/annotations`);
  state.anns=r.annotations;initLayers();renderAll();
}

function initLayers(){
  new Set(state.anns.map(a=>a.type)).forEach(t=>{if(!(t in state.layers))state.layers[t]=true;});
}

// ------------------------------------------------------------------ render
function renderAll(){renderLayers();renderOverlay();renderList();updateReview();renderQuote();}

function renderLayers(){
  const ul=$('layers');ul.innerHTML='';const counts={};
  state.anns.forEach(a=>counts[a.type]=(counts[a.type]||0)+1);
  TYPE_ORDER.filter(t=>counts[t]).forEach(t=>{
    const li=document.createElement('li');
    li.className='layer'+(state.layers[t]?'':' off');
    li.innerHTML=`<span class="swatch" style="background:${TYPE_INFO[t].color}"></span>
      <span class="layer-name">${TYPE_INFO[t].label}</span><span class="layer-count">${counts[t]}</span>`;
    li.onclick=()=>{state.layers[t]=!state.layers[t];renderLayers();renderOverlay();};
    ul.appendChild(li);
  });
}

function renderOverlay(){
  const svg=$('overlay');svg.innerHTML='';const labels=[];
  state.anns.forEach(d=>{
    if(!state.layers[d.type])return;
    const [x,y,w,h]=d.bbox;
    const g=document.createElementNS(SVGNS,'g');
    g.setAttribute('class',`det ${d.status}`+(d.id===state.selectedId?' selected':''));
    const rect=document.createElementNS(SVGNS,'rect');
    rect.setAttribute('x',x);rect.setAttribute('y',y);rect.setAttribute('width',w);rect.setAttribute('height',h);
    rect.setAttribute('rx',4);
    rect.setAttribute('stroke',d.status==='rejected'?'#94a3b8':(TYPE_INFO[d.type]||{}).color||'#0b5fff');
    const title=document.createElementNS(SVGNS,'title');
    title.textContent=`${d.label} · ${(d.confidence*100)|0}%`;rect.appendChild(title);
    g.appendChild(rect);g.onclick=e=>{e.stopPropagation();select(d.id);};
    svg.appendChild(g);
    if(state.showLabels||d.id===state.selectedId)labels.push(d);
  });
  renderLabels(labels);
}

function renderLabels(list){
  const layer=$('labels');layer.innerHTML='';
  list.forEach(d=>{
    const [x,y]=d.bbox;const el=document.createElement('div');
    el.className='dlabel';el.style.left=x+'px';el.style.top=(y-26)+'px';
    el.style.background=(TYPE_INFO[d.type]||{}).color||'#0b5fff';el.textContent=d.label;
    layer.appendChild(el);
  });
  applyLabelScale();
}

function renderList(){
  const box=$('detectionList');box.innerHTML='';const byType={};
  state.anns.forEach(a=>(byType[a.type]||=[]).push(a));
  TYPE_ORDER.filter(t=>byType[t]).forEach(t=>{
    const title=document.createElement('div');title.className='det-group-title';
    title.innerHTML=`<span class="swatch" style="width:10px;height:10px;background:${TYPE_INFO[t].color}"></span>
      ${TYPE_INFO[t].label} · ${byType[t].length}`;box.appendChild(title);
    byType[t].forEach(d=>box.appendChild(detItem(d)));
  });
}

function detItem(d){
  const el=document.createElement('div');
  el.className='det-item'+(d.id===state.selectedId?' selected':'');
  el.innerHTML=`<span class="det-dot" style="background:${(TYPE_INFO[d.type]||{}).color}"></span>
    <div class="det-info"><div class="det-label">${d.label}</div>
    <div class="det-sub">${(d.confidence*100)|0}% · ${d.status} · ${d.source||''}</div></div>
    <div class="det-actions">
      <button class="ic-btn accept ${d.status==='accepted'?'on':''}">✓</button>
      <button class="ic-btn reject ${d.status==='rejected'?'on':''}">✗</button></div>`;
  el.onclick=()=>select(d.id);
  el.querySelector('.accept').onclick=e=>{e.stopPropagation();setStatus(d.id,d.status==='accepted'?'pending':'accepted');};
  el.querySelector('.reject').onclick=e=>{e.stopPropagation();setStatus(d.id,d.status==='rejected'?'pending':'rejected');};
  return el;
}

function updateReview(){
  const n=state.anns.length;
  const acc=state.anns.filter(a=>a.status==='accepted').length;
  const rej=state.anns.filter(a=>a.status==='rejected').length;
  $('progressBar').style.width=n?((acc+rej)/n*100)+'%':'0';
  setText('reviewCounts',`${acc} accepted · ${rej} rejected · ${n-acc-rej} pending`);
}

// ------------------------------------------------------------------ selection + editing
function select(id){
  state.selectedId=id;renderOverlay();renderList();
  const d=state.anns.find(x=>x.id===id);
  if(d){showEditor(d);locate(d);}
  const item=document.querySelector('.det-item.selected');
  if(item)item.scrollIntoView({block:'nearest',behavior:'smooth'});
}

function showEditor(d){
  $('editor').classList.remove('hidden');
  $('edLabel').value=d.label;$('edType').value=d.type;
  drawEvidence(d);
}

async function setStatus(id,status){
  const r=await api(`/api/sessions/${state.session.id}/annotations/${id}`,{method:'PATCH',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});
  const a=state.anns.find(x=>x.id===id);if(a&&r.annotation)Object.assign(a,r.annotation);
  renderAll();
}

async function saveEdit(){
  const id=state.selectedId;if(!id)return;
  const patch={label:$('edLabel').value,type:$('edType').value};
  await api(`/api/sessions/${state.session.id}/annotations/${id}`,{method:'PATCH',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(patch)});
  const a=state.anns.find(x=>x.id===id);if(a)Object.assign(a,patch);renderAll();
}

async function deleteSelected(){
  const id=state.selectedId;if(!id)return;
  await fetch(`/api/sessions/${state.session.id}/annotations/${id}`,{method:'DELETE'});
  state.selectedId=null;$('editor').classList.add('hidden');await refresh();
}

async function bulkStatus(status){
  await Promise.all(state.anns.map(a=>api(`/api/sessions/${state.session.id}/annotations/${a.id}`,
    {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})})));
  await refresh();
}

// ------------------------------------------------------------------ detection
async function runDetection(){
  setBusy(true,'Running Claude vision detection…');
  try{
    const r=await api(`/api/sessions/${state.session.id}/detect`,{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
    if(r.detail){alert('Detection: '+r.detail);}
    await refresh();
    setText('statusText',`Detection added ${r.count||0} components`);
  }catch(e){alert('Detection failed: '+e);}finally{setBusy(false);}
}

// ------------------------------------------------------------------ add-box
function toggleAddMode(){
  state.addMode=!state.addMode;
  $('addBtn').classList.toggle('active',state.addMode);
  $('viewer').classList.toggle('adding',state.addMode);
  $('hint').classList.toggle('hidden',!state.addMode);
}

// ------------------------------------------------------------------ chat
async function sendChat(){
  const input=$('chatInput');const text=input.value.trim();if(!text)return;
  input.value='';addBubble('user',text);
  const typing=addBubble('agent','…');
  try{
    const resp=await fetch(`/api/sessions/${state.session.id}/agent`,{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text})});
    const reader=resp.body.getReader();const dec=new TextDecoder();let buf='';let firstMsg=true;
    while(true){
      const {value,done}=await reader.read();if(done)break;
      buf+=dec.decode(value,{stream:true});let i;
      while((i=buf.indexOf('\n\n'))>=0){
        const chunk=buf.slice(0,i);buf=buf.slice(i+2);
        if(!chunk.startsWith('data: '))continue;
        const ev=JSON.parse(chunk.slice(6));
        if(ev.type==='tool'){addBubble('tool','⚙ '+(ev.name||'tool')+(ev.detail?': '+ev.detail:''));}
        else if(ev.type==='message'){if(firstMsg){typing.textContent=ev.text;firstMsg=false;}else addBubble('agent',ev.text);}
        else if(ev.type==='done'){if(ev.changed)await refresh();}
      }
    }
    if(firstMsg)typing.textContent='(no response)';
  }catch(e){typing.textContent='Error: '+e;}
}

function addBubble(role,text){
  const log=$('chatLog');const wrap=document.createElement('div');wrap.className='msg '+role;
  const b=document.createElement('div');b.className='bubble';b.textContent=text;
  wrap.appendChild(b);log.appendChild(wrap);log.scrollTop=log.scrollHeight;return b;
}

// ------------------------------------------------------------------ quote
async function renderQuote(){
  if(!state.session)return;
  const q=await api(`/api/sessions/${state.session.id}/boq`);
  const body=$('quoteBody');
  if(!q.products.length){body.innerHTML='<p class="muted small">Accept detections to build the BOQ.</p>';
    $('contrast').innerHTML='';return;}
  body.innerHTML=q.products.map(p=>`<div class="q-row">
    <div><div class="q-name">${p.name}</div><div class="q-basis">${p.basis}</div></div>
    <div class="q-qty">${p.qty}<small> ${p.unit}</small></div></div>`).join('');
  $('contrast').innerHTML=`<h4>⚠ Why grounding counts in the drawing matters</h4>
    <table><tr><th>Line</th><th>Text-only AI</th><th>Drawing take-off</th></tr>
    ${q.contrast.map(c=>`<tr><td>${c.line}</td><td class="wrong">${c.text_only_ai}</td>
      <td class="right">${c.drawing}</td></tr>`).join('')}</table>`;
  $('exportBtn').href=`/api/sessions/${state.session.id}/export.csv`;
}

// ------------------------------------------------------------------ viewer
function setupViewer(){
  const viewer=$('viewer');
  viewer.addEventListener('wheel',e=>{e.preventDefault();const r=viewer.getBoundingClientRect();
    zoomAt(e.clientX-r.left,e.clientY-r.top,e.deltaY<0?1.12:1/1.12);},{passive:false});
  let dragging=false,lx=0,ly=0,rubber=null,rstart=null;
  viewer.addEventListener('mousedown',e=>{
    if(e.target.closest('.det')&&!state.addMode)return;
    if(state.addMode){rstart=toImg(e);rubber=document.createElementNS(SVGNS,'rect');
      rubber.id='rubber';$('overlay').appendChild(rubber);return;}
    dragging=true;lx=e.clientX;ly=e.clientY;viewer.classList.add('panning');
    if(state.selectedId){state.selectedId=null;renderOverlay();renderList();$('editor').classList.add('hidden');}
  });
  window.addEventListener('mousemove',e=>{
    if(rubber&&rstart){const p=toImg(e);const x=Math.min(p.x,rstart.x),y=Math.min(p.y,rstart.y);
      rubber.setAttribute('x',x);rubber.setAttribute('y',y);
      rubber.setAttribute('width',Math.abs(p.x-rstart.x));rubber.setAttribute('height',Math.abs(p.y-rstart.y));return;}
    if(!dragging)return;state.tx+=e.clientX-lx;state.ty+=e.clientY-ly;lx=e.clientX;ly=e.clientY;applyTransform();
  });
  window.addEventListener('mouseup',async e=>{
    viewer.classList.remove('panning');dragging=false;
    if(rubber&&rstart){const p=toImg(e);
      const x=Math.min(p.x,rstart.x),y=Math.min(p.y,rstart.y),w=Math.abs(p.x-rstart.x),h=Math.abs(p.y-rstart.y);
      rubber.remove();rubber=null;const s=rstart;rstart=null;
      if(w>12&&h>12){await addBox(x,y,w,h);}
      toggleAddMode();}
  });
}

function toImg(e){const r=$('viewer').getBoundingClientRect();
  return {x:(e.clientX-r.left-state.tx)/state.scale,y:(e.clientY-r.top-state.ty)/state.scale};}

async function addBox(x,y,w,h){
  const label=prompt('Label for the new component?','New component')||'New component';
  const r=await api(`/api/sessions/${state.session.id}/annotations`,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({type:'gis_bay',label,bbox:[x,y,w,h],props:{}})});
  await refresh();if(r.added)select(r.added);
  const tab=document.querySelector('.tab[data-tab="detections"]');if(tab)tab.click();
}

function vwCx(){return $('viewer').clientWidth/2;}function vwCy(){return $('viewer').clientHeight/2;}
function zoomAt(px,py,f){const ns=clamp(state.scale*f,0.03,8),r=ns/state.scale;
  state.tx=px-(px-state.tx)*r;state.ty=py-(py-state.ty)*r;state.scale=ns;applyTransform();}
function fitToScreen(){const v=$('viewer');const s=Math.min(v.clientWidth/state.imgW,v.clientHeight/state.imgH)*0.95;
  state.scale=s;state.tx=(v.clientWidth-state.imgW*s)/2;state.ty=(v.clientHeight-state.imgH*s)/2;applyTransform();}
function locate(d){const v=$('viewer');const [x,y,w,h]=d.bbox;
  const s=clamp(Math.min(v.clientWidth/(w*3.2),v.clientHeight/(h*1.8)),0.05,3.5);
  state.scale=s;state.tx=v.clientWidth/2-(x+w/2)*s;state.ty=v.clientHeight/2-(y+h/2)*s;applyTransform();}
function applyTransform(){$('stage').style.transform=`translate(${state.tx}px,${state.ty}px) scale(${state.scale})`;
  setText('zoomLabel',Math.round(state.scale*100)+'%');applyLabelScale();}
function applyLabelScale(){$('labels').querySelectorAll('.dlabel').forEach(el=>el.style.transform=`scale(${1/state.scale})`);}

function drawEvidence(d){
  const c=$('evidenceCanvas'),ctx=c.getContext('2d');c.width=600;c.height=240;
  ctx.fillStyle='#fff';ctx.fillRect(0,0,c.width,c.height);
  const [x,y,w,h]=d.bbox;const pad=Math.max(w,h)*0.3+30;
  const sx=clamp(x-pad,0,state.imgW),sy=clamp(y-pad,0,state.imgH);
  const sw=Math.min(w+pad*2,state.imgW-sx),sh=Math.min(h+pad*2,state.imgH-sy);
  const s=Math.min(c.width/sw,c.height/sh),dw=sw*s,dh=sh*s,dx=(c.width-dw)/2,dy=(c.height-dh)/2;
  try{ctx.drawImage($('drawing'),sx,sy,sw,sh,dx,dy,dw,dh);}catch(_){}
  ctx.strokeStyle=(TYPE_INFO[d.type]||{}).color||'#0b5fff';ctx.lineWidth=3;
  ctx.strokeRect(dx+(x-sx)*s,dy+(y-sy)*s,w*s,h*s);
}

function setText(id,t){const e=$(id);if(e)e.textContent=t;}
function setBusy(on,msg){$('loading').classList.toggle('hidden',!on);if(msg)setText('loadingMsg',msg);$('detectBtn').disabled=on;}

init();
