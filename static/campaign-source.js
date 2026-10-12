document.addEventListener('DOMContentLoaded', async()=>{
  const form=document.getElementById('campaign-source-form'); if(!form)return;
  const status=document.getElementById('campaign-source-state');
  const url='/api/brands/'+window.marketingBrand+'/campaign-source'; let digest;
  try {
    const response=await fetch(url);const data=await response.json();
    if(!response.ok||!data.ok)throw new Error(data.error||'Source setup unavailable');
    digest=data.digest;Object.entries(data.config).forEach(([key,value])=>{if(form.elements.namedItem(key))form.elements.namedItem(key).value=value;});
    status.textContent=data.config.base_url?'Source reference saved. Collect a campaign audience preview to verify it.':'No campaign source configured. Delivery is unavailable.';
    form.querySelector('button').disabled=false;
  }catch(error){status.textContent=error.message;}
  form.addEventListener('submit',async event=>{
    event.preventDefault();const button=form.querySelector('button');button.disabled=true;
    try{const result=await marketingPost(url,{digest,config:{schema_version:1,...Object.fromEntries(new FormData(form))}});digest=result.digest;status.textContent='Source reference saved. Fresh previews and exact approvals are still required. No delivery scheduled.';}
    catch(error){status.textContent=error.message;}finally{button.disabled=false;}
  });
});
