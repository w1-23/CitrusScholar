'use strict';
(function () {
  try { var theme=localStorage.getItem('citrus_theme'); if(['light','dark','cartoon'].includes(theme))document.documentElement.dataset.theme=theme; } catch (_) {}
  var epoch = 0, findingEpoch = 0, review = null, selected = null, controller = null;
  var activeReviewId = '', activeFindingId = '', pendingSaves = new Map();
  var labels = {pending:'待复核',needs_context:'需要更多材料',explained:'已有合理解释',confirmed_discrepancy:'人工确认差异',dismissed:'不采纳该线索'};
  var names = {exact_column:'列数值相同',reordered_column:'重排后数值相同',constant_offset:'恒定偏移关系',constant_ratio:'比例关系',affine:'线性变换关系',exact_block:'数值块相同',reordered_block:'重排后数值块相同',exact_full_image:'图片像素相同',exact_crop_correspondence:'图片区域像素相同',near_duplicate_geometric_candidate:'图片几何与灰度近似',reported_mean_differs_from_raw:'报告均值与原始数据不一致',reported_n_differs_from_raw:'报告样本量与原始数据不一致',reported_sd_differs_from_raw:'报告标准差与原始数据不一致',reported_se_differs_from_raw:'报告标准误与原始数据不一致',shared_decimal_tail:'跨文件小数尾部模式相同',within_image_geometric_repetition:'同图分离区域出现对应',panel_boundary_background_step:'面板边界与背景阶跃',dynamic_range_endpoint_accumulation:'灰度端点集中分布',image_source_provenance:'源文件声明字段清单（非异常判断）'};
  function node(id) { return document.getElementById(id); }
  function status(text) { node('ir-status').textContent = text; }
  function element(tag, text) { var el=document.createElement(tag); if(text!==undefined)el.textContent=text; return el; }
  function clearPrivate(){review=null;selected=null;findingEpoch+=1;node('ir-save').disabled=true;['ir-sources','ir-findings','ir-context','ir-history'].forEach(function(id){node(id).replaceChildren();});node('ir-title').textContent='选择一条线索';node('ir-summary').textContent='';node('ir-evidence').textContent='';node('ir-note').value='';node('ir-form').hidden=true;}
  async function api(path, options) {
    var requestEpoch=epoch;
    var response=await fetch(path,Object.assign({credentials:'same-origin',cache:'no-store',signal:controller.signal},options||{}));
    if(requestEpoch!==epoch){var stale=new Error('stale review response');stale.name='AbortError';throw stale;}
    if(response.status===401) { clearPrivate();node('ir-reviews').replaceChildren();throw new Error('登录已失效，请返回工作台重新登录。'); }
    if(!response.ok) { if(response.status===409)throw new Error('记录已改变。请重新读取后核对，再保存决定。'); var err=await response.json().catch(function(){return {};}); throw new Error(typeof err.detail==='string'?err.detail:'读取失败，请重新读取。'); }
    return response;
  }
  function newEpoch() { epoch+=1; if(controller)controller.abort(); controller=new AbortController(); return epoch; }
  function base(id) { return '/api/v2/integrity/reviews/'+encodeURIComponent(id); }
  function saveKey(id, finding) { return JSON.stringify([id, finding]); }
  function ownsFinding(owner) {
    return owner.review === epoch && owner.finding === findingEpoch && owner.item === selected;
  }
  async function openReview(id, preferredFinding, afterSave) {
    var current=newEpoch(); activeReviewId=id; activeFindingId=preferredFinding||''; clearPrivate();
    status('正在读取复核记录…');
    try {
      var response=await api(base(id));
      var data=await response.json();
      if(current!==epoch)return;
      if(data.review_id!==id)throw new Error('收到的复核记录与当前选择不符。');
      var items=data.report.common_findings||[];
      review=data;
      items.forEach(function(item,index){ var button=element('button',(index+1)+'. '+(names[item.rule]||item.summary||item.rule)+' · '+labels[item.review.state]); button.type='button'; button.dataset.finding=item.finding_id; button.onclick=function(){if(current===epoch)showFinding(item);}; node('ir-findings').append(button); });
      var chosen=items.find(function(item){return item.finding_id===preferredFinding;})||items[0];
      if(chosen)showFinding(chosen); else {node('ir-title').textContent='本次没有生成待复核线索'; node('ir-summary').textContent='未发现只对应本次已执行的方法和范围。';}
      var inventoryCount=items.filter(function(item){return item.severity==='information';}).length;
      status(afterSave ? '人工复核决定已保存。原始证据保持不变。' :
        (items.length-inventoryCount)+' 条自动线索，'+inventoryCount+' 份来源字段清单；人工复核不会改写原始文件或已发布报告。');
    } catch(error) {
      if(current===epoch&&error.name!=='AbortError')status((afterSave?'决定已保存，但重新读取失败。':'')+error.message);
    }
  }
  function addList(title,values) { var box=node('ir-context'); box.append(element('h3',title)); var list=element('ul'); (values||[]).forEach(function(value){list.append(element('li',value));}); box.append(list); }
  async function sourcePanel(item,loc,index,owner,id) {
    var pane=element('section');pane.className='ir-source';pane.append(element('h3',loc.source_name||loc.source_filename||('来源 '+(index+1))));
    pane.append(element('pre','SHA-256 '+(loc.sha256||loc.source_sha256)+'\n'+(loc.sheet||'')+' '+(loc.bbox?'像素范围 '+loc.bbox.join(', '):'原始行 '+(loc.rows||[]).slice(0,20).join(', '))));
    if(loc.source_download_url&&loc.source_download_url.startsWith('/api/')){var download=element('a','下载原始文件');download.href=loc.source_download_url;download.target='_blank';download.rel='noopener';pane.append(download);}
    node('ir-sources').append(pane);
    if(!loc.file_id){pane.append(element('p','原始文件未登记，无法预览；请依据位置核对原件。'));return;}
    var path=base(id)+'/findings/'+encodeURIComponent(item.finding_id)+'/sources/'+index;
    try {var response=await api(path);if(!ownsFinding(owner))return;
      if((response.headers.get('content-type')||'').startsWith('image/')) {var blob=await response.blob();if(!ownsFinding(owner))return; var image=element('img');image.alt='来源 '+(index+1)+' 的证据区域';var url=URL.createObjectURL(blob);image.onload=image.onerror=function(){URL.revokeObjectURL(url);};image.src=url;pane.append(image);}
      else {var data=await response.json();if(!ownsFinding(owner))return;var wrap=element('div');wrap.className='ir-table-wrap';var table=element('table');var head=element('tr');head.append(element('th','原行'));data.columns.forEach(function(col){head.append(element('th',col.name));});var thead=element('thead');thead.append(head);table.append(thead);var body=element('tbody');data.rows.forEach(function(row){var tr=element('tr');tr.append(element('th',String(row.row)));row.values.forEach(function(value){tr.append(element('td',value));});body.append(tr);});table.append(body);wrap.append(table);pane.append(wrap,element('p',data.coverage));}
    } catch(error){if(ownsFinding(owner)&&error.name!=='AbortError')pane.append(element('p',error.message));}
  }
  function showFinding(item) {
    selected=item;activeFindingId=item.finding_id;
    var owner={review:epoch,finding:++findingEpoch,item:item};
    var id=review.review_id;
    node('ir-save').disabled=pendingSaves.has(saveKey(id,item.finding_id));
    node('ir-title').textContent=names[item.rule]||item.rule;node('ir-summary').textContent=item.summary;node('ir-sources').replaceChildren();node('ir-context').replaceChildren();
    Array.from(node('ir-findings').children).forEach(function(button){button.setAttribute('aria-current',String(button.dataset.finding===item.finding_id));});
    addList('适用条件',item.applicability);addList('其他可能解释',item.counterexamples);node('ir-context').append(element('pre',JSON.stringify(item.coverage,null,2)));
    node('ir-evidence').textContent=JSON.stringify(item.evidence,null,2);node('ir-form').hidden=false;node('ir-state').value=item.review.state;node('ir-note').value=item.review.note;
    node('ir-history').replaceChildren();item.review_history.forEach(function(log){node('ir-history').append(element('li',log.created_at+' '+labels[log.state]+'\n'+log.note));});
    item.locations.forEach(function(loc,index){sourcePanel(item,loc,index,owner,id);});
  }
  node('ir-form').onsubmit=async function(event){
    event.preventDefault();
    if(!review||!selected)return;
    var item=selected, id=review.review_id, owner={review:epoch,finding:findingEpoch,item:item};
    var key=saveKey(id,item.finding_id), token={};
    if(pendingSaves.has(key))return;
    var body=JSON.stringify({state:node('ir-state').value,note:node('ir-note').value,expected_revision:item.review.revision});
    pendingSaves.set(key,token); node('ir-save').disabled=true;
    try {
      await api(base(id)+'/findings/'+encodeURIComponent(item.finding_id),{method:'PUT',headers:{'Content-Type':'application/json'},body:body});
      if(!ownsFinding(owner))return;
      pendingSaves.delete(key);
      await openReview(id,item.finding_id,true);
    } catch(error) {
      if(ownsFinding(owner)&&error.name!=='AbortError')status(error.message);
    } finally {
      if(pendingSaves.get(key)===token)pendingSaves.delete(key);
      if(review&&selected&&saveKey(review.review_id,selected.finding_id)===key)
        node('ir-save').disabled=pendingSaves.has(key);
    }
  };
  node('ir-reload').onclick=function(){if(activeReviewId)openReview(activeReviewId,activeFindingId);};
  node('ir-reviews').onchange=function(){openReview(this.value);};
  async function start(){var current=newEpoch();clearPrivate();node('ir-reviews').replaceChildren();try{var response=await api('/api/v2/integrity/reviews');var data=await response.json();if(current!==epoch)return;data.reviews.forEach(function(item){var option=element('option',item.created_at+' / '+item.review_id.slice(0,8));option.value=item.review_id;node('ir-reviews').append(option);});var id=new URLSearchParams(location.search).get('review')||(data.reviews[0]||{}).review_id;if(id){node('ir-reviews').value=id;await openReview(id);}else status('暂无复核记录。完成表格或图片审查后，可从审查回执打开证据。');}catch(error){if(current===epoch&&error.name!=='AbortError')status(error.message);}}
  window.addEventListener('pagehide',function(){newEpoch();activeReviewId=activeFindingId='';pendingSaves.clear();clearPrivate();node('ir-reviews').replaceChildren();});
  window.addEventListener('pageshow',function(event){if(event.persisted)start();});
  start();
}());
