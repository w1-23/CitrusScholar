// SPDX-License-Identifier: AGPL-3.0-only
import {nativeElement,nativePatch,nativeWritable} from './viewport.mjs';

const specs = {text:'文字内容',fontsize:'字号（pt）',color:'颜色'};
export function suggestionFields(element) {
  const eligible = (element?.editable ?? []).filter(field => (
    (field.prop === 'text' && typeof field.value === 'string') ||
    (field.prop === 'fontsize' && Number.isFinite(field.value)) ||
    (field.prop === 'color' && /^#[0-9a-f]{6}$/i.test(field.value))
  ));
  return Object.keys(specs).flatMap(prop => eligible.filter(field => field.prop === prop));
}

export function suggestionValue(field, input) {
  if (field.prop === 'text') return input;
  if (field.prop === 'color') {
    if (/^#[0-9a-f]{6}$/i.test(input)) return input.toLowerCase();
    throw new Error('请输入六位十六进制颜色。');
  }
  const low = Number.isFinite(field.min) ? field.min : 3, high = Number.isFinite(field.max) ? field.max : 200;
  const value = Number(input);
  if (!input.trim() || !Number.isFinite(value) || value < low || value > high) throw new Error(`字号必须在 ${low}–${high} pt 之间。`);
  return value;
}

export function createAssistant({state,latest,save,message}) {
  const $ = id => document.getElementById(id);
  let context, draft;
  const clear = () => { draft = null; $('assistant-preview').hidden = true; $('assistant-confirm').disabled = true; };
  const element = () => latest() && nativeWritable(state.doc) && state.selected?.kind === 'native' && !state.locks.has(state.selected.id)
    ? nativeElement(state.doc,state.selected.id) : null;
  function renderField() {
    clear();
    const field = suggestionFields(element()).find(field => field.prop === $('assistant-action').value);
    const text = $('assistant-text'), value = $('assistant-value');
    const multiline = field?.prop === 'text';
    text.hidden = text.disabled = !multiline;
    value.hidden = value.disabled = !field || multiline;
    if (!field) return;
    $('assistant-value-caption').textContent = `建议${specs[field.prop]}`;
    if (multiline) text.value = field.value;
    else {
      value.type = field.prop === 'color' ? 'color' : 'number';
      value.step = 'any';
      value.value = String(field.value);
    }
  }
  function render() {
    const current = element(), fields = suggestionFields(current);
    const key = JSON.stringify([state.doc?.figure_id,state.doc?.revision,current?.gid,fields]);
    if (key === context) return;
    context = key;
    $('assistant-form').hidden = !fields.length;
    $('assistant-note').textContent = fields.length ? `所选对象：${current.label || current.gid}` : '请选择当前版本中可编辑文字、字号或颜色的原生对象。';
    $('assistant-action').replaceChildren(...fields.map(field => new Option(specs[field.prop],field.prop)));
    renderField();
  }
  $('assistant-action').addEventListener('change',renderField);
  for (const id of ['assistant-text','assistant-value']) $(id).addEventListener('input',clear);
  $('assistant-form').addEventListener('submit',event => {
    event.preventDefault();
    const current = element();
    const field = suggestionFields(current).find(field => field.prop === $('assistant-action').value);
    if (!field || state.pending) return;
    try {
      const after = suggestionValue(field,$(field.prop === 'text' ? 'assistant-text' : 'assistant-value').value);
      if (after === field.value || (field.prop === 'color' && after === field.value.toLowerCase())) { message('建议值和当前值相同。',false); return; }
      draft = {figure:state.doc.figure_id,revision:state.doc.revision,element:current,field,after};
      $('assistant-before').textContent = String(field.value) || '（空）';
      $('assistant-after').textContent = String(after) || '（空）';
      $('assistant-preview').hidden = false;
      $('assistant-confirm').disabled = false;
    } catch (error) { message(error.message,true); }
  });
  $('assistant-cancel').addEventListener('click',clear);
  $('assistant-confirm').addEventListener('click',() => {
    const current = element();
    if (!draft || state.pending || draft.figure !== state.doc?.figure_id || draft.revision !== state.doc?.revision || current?.gid !== draft.element.gid || current.identity !== draft.element.identity || !suggestionFields(current).some(field => field.prop === draft.field.prop && field.value === draft.field.value)) {
      clear();
      message('所选对象或版本已改变，请重新预览建议。',true);
      return;
    }
    const patch = nativePatch(current,draft.field.prop,draft.after);
    clear();
    save([patch]);
  });
  return {render};
}
