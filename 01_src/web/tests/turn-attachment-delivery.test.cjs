"use strict";
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {browser, flush} = require('./frontend-harness.cjs');

const input = {file_id:'input-id', filename:'question.pdf'};
const terminal = {display_status:'completed'};
const row = (rid, attachments) => ({
  turn_request_id:rid, job_id:'job-'+rid, terminal_snapshot:terminal,
  user:{content:'Prompt '+rid, request_id:rid, attachments}, answer:{content:'Answer '+rid}
});
const continuationRequests = b => b.requests.filter(req => req.url.includes('/continuation/'));
const assistant = b => b.get('chat-container').querySelectorAll('.bot');
const continuation = node => node.querySelector('.turn-explicit-continuation');

function history(b, rows) {
  b.ctx.renderHistoryMessages([], b.ctx.normalizeHistoryTurns(rows));
}

test('history attachments remain usable without implicit reuse or eligibility probes', async () => {
  const b = browser();
  const previews = [];
  b.ctx.openAccountFilePreview = file => previews.push(file.file_id);
  history(b, [row('none', []), row('source', [input])]);
  assert.equal(assistant(b).some(continuation), false);
  assert.equal(continuationRequests(b).length, 0);
  const chip = b.get('chat-container').querySelectorAll('.user').at(-1).querySelector('.sent-attachment-reference');
  assert.equal(chip.querySelector('.sent-attachment-name').textContent, input.filename);
  chip.onclick();
  assert.deepEqual(previews, [input.file_id]);

  b.get('user-input').value = '独立问题';
  await b.ctx.sendMsg();
  await flush();
  const request = b.requests.find(r => r.opts.method === 'POST' && r.url.endsWith('/turn-queue'));
  const body = JSON.parse(request.opts.body);
  assert.equal(Object.hasOwn(body, 'continue_from'), false);
  assert.deepEqual(body.file_refs, []);
  assert.equal(continuationRequests(b).length, 0);
});

test('an authorized same-turn card replaces only its duplicate answer link', () => {
  const b = browser();
  const content = b.ctx.addMessage('bot','');
  content.dataset.sessionId = 'session';
  content.dataset.requestId = 'card-turn';
  const answer = b.document.createElement('div');
  answer.className = 'turn-answer';
  content.appendChild(answer);
  const url = '/api/v2/session/session/artifacts/art-id';
  const alias = '/api/v2/files/out-id/download';
  for (const [href, text] of [[url,'分析结果'],[alias,'原文件'],['https://doi.org/10.0/example','参考文献'],['/api/v2/files/other/download','其他来源'],['https://external.test'+alias,'外站']]) {
    const anchor = b.document.createElement('a');
    anchor.setAttribute('href',href);
    anchor.textContent = text;
    answer.appendChild(anchor);
  }
  const file = {file_id:'out-id', artifact_id:'art-id', filename:'report.pdf', source_session_id:'session', turn_request_id:'card-turn'};
  b.ctx.appendTurnGeneratedFiles(content,[{...file,turn_request_id:'other-turn'}]);
  assert.equal(answer.querySelectorAll('a').length,5);
  b.ctx.appendTurnGeneratedFiles(content,[file]);
  assert.equal(answer.querySelectorAll('a').length,5);
  assert.equal(assistant(b)[0].querySelector('.turn-file-card').querySelector('a'),null);
  b.ctx.renderArtifactDelivery(content,{schema_version:'artifact-delivery/1',status:'verified',artifacts:[{asset_ref:'art-id',url}]});
  b.ctx.appendTurnGeneratedFiles(content,[file]);
  b.ctx.appendTurnGeneratedFiles(content,[file]);
  const bot = assistant(b)[0];
  assert.equal(bot.querySelectorAll('.turn-file-card').length,1);
  assert.equal(bot.querySelector('.turn-files-heading').textContent,'本轮产出');
  assert.equal(bot.querySelector('.turn-file-card').querySelector('a').href, url);
  assert.match(answer.textContent,/分析结果/);
  assert.deepEqual(Array.from(answer.querySelectorAll('a'), a => a.getAttribute('href')),
    ['https://doi.org/10.0/example','/api/v2/files/other/download','https://external.test'+alias]);
});


test('treats missing formats and uncertain publication as different states', () => {
  const b = browser();
  assert.equal(b.ctx.artifactDeliveryText({status:'partial',verified_file_ids:['png','pdf'],missing_formats:['svg']}),
    '部分文件已交付 · 缺少 SVG · 已核实 2 个文件');
  assert.equal(b.ctx.artifactDeliveryText({status:'unknown',missing_formats:['svg']}),'正在确认交付');
});


test('execution narration interleaves same-turn operation rows and deduplicates replays', () => {
  const b = browser();
  const content = b.ctx.addMessage('bot','');
  const process = b.ctx.CitrusTaskCards.createProcessView({container:content, clean:value=>String(value || ""),
    toolName:()=> '读取表格', statusText:()=> '已完成', elapsed:String});
  const note = (event_id,text) => ({type:'execution_narration',visibility:'public',event_id,text});
  const first = note('narration:table_read','我先查看表格');
  process.narration(first);
  process.action({tool_call_id:'read-1', display_name:'读取表格', status:'running'});
  process.action({tool_call_id:'read-1', display_name:'读取表格', status:'complete'});
  process.narration(note('narration:data_check','再检查缺失值'));
  process.narration(first);
  assert.deepEqual(Array.from(process.element.children).filter(row=>!row.hidden).map(row=>row.textContent),
    ['我先查看表格','读取表格 · 已完成','再检查缺失值']);
});


test('verified formats expose one exact Workbench action for their common render', () => {
  const b = browser();
  const content = b.ctx.addMessage('bot','');
  const sid = 'session-one', project = 'c'.repeat(32);
  content.dataset.sessionId = sid;
  content.dataset.requestId = 'turn';
  const files = ['png','pdf'].map((fmt, i) => ({file_id:'out-'+i, artifact_id:String(i+1).repeat(32),
    filename:'plot.'+fmt, source_session_id:sid, turn_request_id:'turn'}));
  b.ctx.appendTurnGeneratedFiles(content,files);
  assert.equal(assistant(b)[0].querySelector('.turn-file-workbench'), null);
  b.ctx.renderArtifactDelivery(content,{schema_version:'artifact-delivery/1',status:'verified',
    artifacts:files.map(file=>({asset_ref:file.artifact_id,
      url:`/api/v2/session/${sid}/artifacts/${file.artifact_id}`, workbench_project_ref:project}))});
  b.ctx.appendTurnGeneratedFiles(content,files);
  b.ctx.appendTurnGeneratedFiles(content,files);
  const links = assistant(b)[0].querySelectorAll('.turn-file-workbench');
  assert.equal(links.length,1);
  assert.equal(links[0].href,`/figures?session_ref=${sid}&frozen_project=${project}`);
});
