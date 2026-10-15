"""Phone-friendly page served at `/`. No data is embedded; it calls the API with the key the
user types once (kept in this browser's localStorage)."""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Anvil</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--ink:#1b1b1b;--mut:#666;--line:#ddd;--acc:#b4441c;--ok:#2f7d32;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--card:#1e1e1e;--ink:#eee;--mut:#9a9a9a;--line:#333;--acc:#e07a4f;--ok:#7bc67e;--bad:#f2867e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.4 system-ui,-apple-system,Segoe UI,sans-serif}
header{padding:14px 16px 6px;display:flex;align-items:center;gap:10px}h1{font-size:20px;margin:0}
.mut{color:var(--mut);font-size:13px}main{padding:0 16px 40px;max-width:720px;margin:0 auto}
nav{display:flex;gap:6px;padding:8px 16px;overflow-x:auto;max-width:720px;margin:0 auto}
nav button{flex:none}button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--ink);padding:8px 12px;border-radius:10px;cursor:pointer}
button.on,button.pri{background:var(--acc);border-color:var(--acc);color:#fff}button:disabled{opacity:.5}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin:10px 0}
input,textarea,select{width:100%;font:inherit;padding:9px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink);margin:4px 0 8px}
textarea{min-height:140px}label{font-size:13px;color:var(--mut)}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.tag{font-size:12px;padding:2px 7px;border-radius:99px;border:1px solid var(--line);color:var(--mut)}
a{color:var(--acc)}h3{margin:4px 0;font-size:16px}ul{margin:4px 0 4px 18px;padding:0}pre{white-space:pre-wrap;font-size:12px}
.err{color:var(--bad)}.ok{color:var(--ok)}
</style></head><body>
<header><h1>Anvil</h1><span class="mut" id="st">not connected</span></header>
<nav id="tabs"></nav>
<main id="main"></main>
<script>
const $=s=>document.querySelector(s);let KEY='';try{KEY=localStorage.getItem('anvilKey')||''}catch(e){}
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{'content-type':'application/json','authorization':'Bearer '+KEY},body:body?JSON.stringify(body):undefined});
 const j=await r.json().catch(()=>({success:false,error:'HTTP '+r.status}));if(!j.success)throw new Error(j.error||('HTTP '+r.status));return j.data}
const TABS={leads:'Leads',paste:'Paste a JD',sent:'Sent',learn:'Learned',key:'Key'};let tab='leads';
function nav(){$('#tabs').innerHTML=Object.entries(TABS).map(([k,v])=>`<button class="${k==tab?'on':''}" onclick="go('${k}')">${v}</button>`).join('')}
function go(t){tab=t;nav();({leads,paste,sent,learn,key:keyTab})[t]()}
function keyTab(){$('#main').innerHTML=`<div class="card"><label>API key (same as ANVIL_API_KEYS on the server)</label><input id="k" type="password" value="${esc(KEY)}"><button class="pri" onclick="saveKey()">Save</button></div>`}
function saveKey(){KEY=$('#k').value.trim();try{localStorage.setItem('anvilKey',KEY)}catch(e){}ping();go('leads')}
async function ping(){try{const s=await api('/api/v1/status');$('#st').textContent=s.authorized?`${s.genes} genes · state: ${s.state_backend}`+(s.ember_key?'':' · no Ember key'):'key needed'}catch(e){$('#st').textContent='key needed'}}
function busy(msg){$('#main').innerHTML=`<div class="card mut">${esc(msg)}</div>`}
function fail(e){$('#main').insertAdjacentHTML('afterbegin',`<div class="card err">${esc(e.message||e)}</div>`)}
async function leads(){busy('Loading leads from Ember…');try{const d=await api('/api/v1/leads');
 $('#main').innerHTML=`<p class="mut">${d.count} lead(s) ready to tailor</p>`+d.leads.map((l,i)=>`<div class="card"><h3>${esc(l.role)}</h3>
 <div class="row"><b>${esc(l.company)}</b><span class="tag">${esc(l.verdict)}</span>${l.anvil_status?`<span class="tag">${esc(l.anvil_status)}</span>`:''}</div>
 <div class="mut">${esc(l.location)} · ${esc(l.pay)} · <a href="${esc(l.url)}" target="_blank" rel="noopener">job page</a></div>
 <input placeholder="Optional notes (e.g. from the hiring manager's post)" id="n${i}">
 <div class="row"><button class="pri" onclick='make(${JSON.stringify({canonical_id:l.canonical_id})},"n${i}")'>Make CV</button>
 <label><input type="checkbox" id="d${i}" style="width:auto"> just preview (don't log as sent)</label></div></div>`).join('')}catch(e){$('#main').innerHTML='';fail(e)}}
async function make(body,notesId){const i=notesId.slice(1);body.notes=$('#'+notesId)?.value||undefined;body.dry_run=$('#d'+i)?.checked||false;
 busy('Reading the JD and company site, building the CV…');try{show(await api('/api/v1/tailor',body))}catch(e){$('#main').innerHTML='';fail(e)}}
function dl(u){return u+(u.includes('?')?'&':'?')+'key='+encodeURIComponent(KEY)}
function list(a,f){return a.length?'<ul>'+a.map(f).join('')+'</ul>':'<p class="mut">none</p>'}
function show(r){const d=r.jd_diff;$('#main').innerHTML=`<div class="card"><h3>${esc(r.role)} · ${esc(r.company)}</h3>
 <div class="mut">angle: ${esc(r.cluster)} · ${r.pages||1} page · ${r.logged?'logged as sent':'preview only'}</div>
 <div class="row" style="margin-top:8px"><a href="${dl(r.downloads.pdf)}" target="_blank"><button class="pri">PDF</button></a>
 <a href="${dl(r.downloads.docx)}"><button>Word</button></a><a href="${dl(r.downloads.html)}" target="_blank"><button>HTML</button></a></div></div>
 <div class="card"><h3>Exact matches</h3>${list(d.exact,x=>`<li>${esc(x.skill)}</li>`)}
 <h3>Implied safe swaps</h3>${list(d.implied,x=>`<li>${esc(x.skill)}: JD "${esc(x.jd_term)}", CV "${esc(x.cv_evidence)}"</li>`)}
 <h3>True gaps (interview prep, not on CV)</h3>${list(d.gaps,x=>`<li>${esc(x.skill)}${x.note?' · '+esc(x.note):''}</li>`)}</div>
 <div class="card"><details><summary>CV text</summary><pre>${esc(r.cv_text)}</pre></details></div>`}
function paste(){$('#main').innerHTML=`<div class="card"><label>Company</label><input id="pc"><label>Role</label><input id="pr">
 <label>Job URL (optional)</label><input id="pu"><label>Job description</label><textarea id="pj"></textarea>
 <label>Notes (optional)</label><input id="n_p"><label><input type="checkbox" id="d_p" style="width:auto"> just preview</label>
 <button class="pri" onclick="pasteGo()">Make CV</button></div>`}
function pasteGo(){make({company:$('#pc').value,role:$('#pr').value,url:$('#pu').value||undefined,jd_text:$('#pj').value},'n_p')}
async function sent(){busy('Loading…');try{const d=await api('/api/v1/applications');
 $('#main').innerHTML=`<p class="mut">Tap what the company did. Replies teach Anvil which CV versions work.</p>`+d.applications.map(a=>`<div class="card"><h3>${esc(a.role)}</h3>
 <div class="row"><b>${esc(a.company)}</b><span class="tag">${esc(a.outcome)}</span><span class="tag">${esc(a.cluster)}</span></div>
 <div class="mut">${esc((a.applied_at||'').slice(0,10))}</div><div class="row" style="margin-top:6px">${['callback','interview','offer','rejected'].map(o=>`<button onclick='fb(${JSON.stringify(a.canonical_id)},"${o}")'>${o}</button>`).join('')}</div></div>`).join('')}catch(e){$('#main').innerHTML='';fail(e)}}
async function fb(id,o){try{await api('/api/v1/feedback',{canonical_id:id,outcome:o});sent()}catch(e){fail(e)}}
async function learn(){busy('Loading…');try{const s=await api('/api/v1/stats');let h='';
 for(const [c,o] of Object.entries(s.applications_by_cluster))h+=`<div class="card"><h3>${esc(c)}</h3><div class="mut">${Object.entries(o).map(([k,v])=>esc(k)+' '+v).join(' · ')}</div>${(s.learned[c]||[]).length?list(s.learned[c],x=>`<li>${x.mean} · ${esc(x.text)}</li>`):''}</div>`;
 $('#main').innerHTML=h||'<div class="card mut">Nothing learned yet. Log some replies under Sent.</div>'}catch(e){$('#main').innerHTML='';fail(e)}}
nav();if(!KEY)go('key');else{ping();go('leads')}
</script></body></html>
"""
