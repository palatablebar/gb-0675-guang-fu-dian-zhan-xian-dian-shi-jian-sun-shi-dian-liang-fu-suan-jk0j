const solar={busy:false};const status=document.querySelector('#feedback');document.querySelector('#recompute').addEventListener('click',async()=>{if(solar.busy)return;solar.busy=true;status.textContent='复算中…';try{const dataset=document.querySelector('#dataset').value;const response=await fetch(dataset===document.body.dataset.source?'/api/runs/'+document.body.dataset.runId+'/rerun':'/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dataset})});const run=await response.json();if(!response.ok)throw Error(run.detail);const url=new URL(location.href);url.searchParams.set('run_id',run.manifest.run_id);url.searchParams.set('dataset',dataset);location.href=url;}catch(error){status.textContent=error.message;}finally{solar.busy=false;}});
const filePicker=document.querySelector('#csv-file');
if(filePicker){
 const editor=document.querySelector('#csv-text');
 filePicker.addEventListener('change',async()=>{const file=filePicker.files[0];if(!file)return;if(file.size>2000000){editor.value='';alert('文件超过2MB，请缩小后导入');return;}const bytes=await file.arrayBuffer();try{editor.value=new TextDecoder('utf-8',{fatal:true}).decode(bytes);}catch(e){editor.value=new TextDecoder('gb18030',{fatal:true}).decode(bytes);}});
 document.querySelector('#import-csv').addEventListener('click',async event=>{event.target.disabled=true;try{const response=await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csv_text:editor.value})});const data=await response.json();if(!response.ok)throw Error(data.detail);location.href=location.pathname+'?run_id='+data.manifest.run_id;}catch(e){editor.setCustomValidity(e.message);editor.reportValidity();}finally{event.target.disabled=false;}});
 editor.addEventListener('input',()=>editor.setCustomValidity(''));
}
const basisEditor=document.querySelector('#basis-editor');
if(basisEditor){
 const runId=document.body.dataset.runId;
 const statusEl=document.querySelector('#feedback');
 const tbody=document.querySelector('#basis-versions tbody');
 const renderVersions=async()=>{
  const response=await fetch('/api/runs/'+runId+'/basis');if(!response.ok)return;
  const data=await response.json();document.querySelector('#basis-count').textContent=data.versions.length+' 个版本';
  tbody.innerHTML='';
  data.versions.forEach(version=>{
   const tr=document.createElement('tr');
   const cells=[version.basis_sha.slice(0,12),(version.affected_events||[]).join(' + ')||'原始',version.active?'生效':'历史'];
   cells.forEach(text=>{const td=document.createElement('td');td.textContent=text;tr.appendChild(td);});
   const act=document.createElement('td');
   if(!version.active){const button=document.createElement('button');button.textContent='切回';button.className='secondary';
    button.addEventListener('click',()=>switchVersion(version.basis_sha,button));act.appendChild(button);}
   tr.appendChild(act);tbody.appendChild(tr);
  });
 };
 const reload=()=>{const url=new URL(location.href);location.href=url;};
 const switchVersion=async sha=>{
  if(!confirm('切换后将按该依据版本重新展示结算，确认？'))return;
  statusEl.textContent='切换中…';
  const response=await fetch('/api/runs/'+runId+'/basis/'+sha+'/activate',{method:'POST'});
  const data=await response.json();
  if(!response.ok){statusEl.textContent='切换失败，已保持旧版：'+data.detail;return;}
  reload();
 };
 document.querySelector('#submit-basis').addEventListener('click',async event=>{
  event.target.disabled=true;statusEl.textContent='提交依据…';
  try{
   const basis=JSON.parse(document.querySelector('#basis-text').value);
   const response=await fetch('/api/runs/'+runId+'/basis',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({attribution:basis})});
   const data=await response.json();
   if(!response.ok){statusEl.textContent='提交失败，已保持旧版：'+data.detail;return;}
   statusEl.textContent=(data.duplicate?'该依据已存在，未新增版本。':'重算 '+data.recomputed_segments+' 段，保留 '+data.untouched_segments+' 段。');
   setTimeout(reload,600);
  }catch(error){statusEl.textContent='依据 JSON 无法解析：'+error.message;}
  finally{event.target.disabled=false;}
 });
 document.querySelector('#switch-basis').addEventListener('click',()=>{
  fetch('/api/runs/'+runId+'/basis').then(r=>r.json()).then(data=>{
   const previous=data.versions.filter(v=>!v.active).pop();
   if(!previous){statusEl.textContent='没有可切回的历史版本。';return;}
   switchVersion(previous.basis_sha);
  });
 });
 renderVersions();
}
