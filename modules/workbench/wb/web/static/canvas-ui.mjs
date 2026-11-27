// SPDX-License-Identifier: AGPL-3.0-only
import {previewPoint} from './api.mjs';
import {$, make} from './views.mjs';
import {drawRulers, movePatch, moveTarget, nativePatch, nativeText, nativeWritable, visibleBounds} from './viewport.mjs';

export function createCanvas({state, latest, pick, save, error}) {
  const stage = $('stage'), paper = $('paper'), image = $('preview'), inline = $('inline-form');
  let box, pointer, drag, lastDragAt = 0, raf = 0, editor, pickTask;
  const currentId = () => state.selected?.kind === 'native' ? state.selected.id : null;
  const currentText = () => latest() && !state.locks.has(currentId()) ? nativeText(state.doc,currentId()) : null;
  const fraction = event => image.complete && image.naturalWidth ? previewPoint(image.getBoundingClientRect(),event.clientX,event.clientY) : null;
  const canMove = point => latest() ? moveTarget(state.doc,currentId(),point,state.locks) : null;
  function ruler() {
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      $('ruler-unit').textContent = drawRulers(stage,image,[$('ruler-x'),$('ruler-y')],state.doc,box,pointer);
    });
  }
  function placeOutline(bounds) {
    const overlay = $('outline');
    overlay.hidden = !bounds;
    if (!bounds) return;
    ['left','top','width','height'].forEach((prop,i) => overlay.style[prop] = `${bounds[i]*100}%`);
  }
  function selection() {
    const native = state.doc?.native_manifest;
    const group = state.selected?.kind === 'group' ? native?.groups?.find(g => g.gid === state.selected.id) : null;
    const ids = new Set(group?.members ?? (currentId() !== 'figure' ? [currentId()] : []));
    box = visibleBounds((native?.elements ?? []).filter(el => ids.has(el.gid)));
    paper.dataset.move = !!canMove();
    placeOutline(box);
    ruler();
  }
  function size() {
    if (state.doc && image.complete && image.naturalWidth && image.naturalHeight) {
      const w = Math.max(1,stage.clientWidth-52), h = Math.max(1,stage.clientHeight-98);
      paper.style.width = `${Math.max(1,Math.floor(Math.min(w,h*image.naturalWidth/image.naturalHeight)*state.zoom))}px`;
    }
    ruler();
    details();
  }
  function details() {
    const doc = state.doc;
    const mm = doc?.native_manifest?.size_mm;
    const entries = doc ? [
      ['图件',doc.name],['版本',String(doc.revision)],['绘图语言',doc.source?.language ?? '未知'],
      ['面板',String(doc.panels?.length ?? 0)],['预览像素',image.complete && image.naturalWidth ? `${image.naturalWidth} × ${image.naturalHeight}` : '未载入'],
      ['物理尺寸',Array.isArray(mm) ? mm.map(n => Number(n.toFixed(1))).join(' × ')+' mm' : '未提供'],['屏幕缩放',`${Math.round(state.zoom*100)}% · 相对于适合画布`],
    ] : [];
    $('canvas-details').replaceChildren(...entries.map(([key,value]) => {
      const row = make('div'); row.append(make('dt',key),make('dd',value)); return row;
    }));
  }
  function properties() {
    const pane = $('physical-controls');
    const element = state.doc?.native_manifest?.elements?.find(el => el.gid === 'figure' && el.role === 'figure');
    const fields = new Map((element?.editable ?? []).map(field => [field.prop,field.value]));
    const size = fields.get('size_mm'), color = fields.get('facecolor'), transparent = fields.get('transparent');
    const writable = latest() && nativeWritable(state.doc);
    const form = make('form','','form-fields');
    const specs = [];
    function addField(title,type,value,attrs = {}, target = form) {
      const label = make('label',title), input = make('input');
      Object.assign(input,{type,disabled:!writable,...attrs});
      if (type === 'checkbox') input.checked = value; else input.value = value;
      label.append(input); target.append(label); return input;
    }
    if (Array.isArray(size) && size.length === 2 && size.every(n => Number.isFinite(n) && n > 0)) {
      const row = make('div','','fields');
      const limits = {min:1,max:Math.max(400,...size),step:'any',required:true};
      const w = addField('宽度（mm）','number',size[0],{...limits,id:'canvas-width'},row);
      const h = addField('高度（mm）','number',size[1],{...limits,id:'canvas-height'},row);
      form.append(row);
      specs.push(['size_mm',size,() => [w.valueAsNumber,h.valueAsNumber]]);
    }
    if (typeof color === 'string' && /^#[a-f0-9]{6}$/i.test(color)) {
      const input = addField('原生背景色','color',color,{id:'canvas-color'});
      specs.push(['facecolor',color,() => input.value]);
    }
    if (typeof transparent === 'boolean') {
      const input = addField('透明背景','checkbox',transparent,{id:'canvas-transparent'});
      specs.push(['transparent',transparent,() => input.checked]);
    }
    pane.replaceChildren();
    if (!specs.length) {
      if (state.doc) pane.append(make('p','当前图件没有服务端可写的物理画布属性。','muted'));
      return;
    }
    pane.append(make('h3','图幅与背景','subsection'));
    if (writable) form.append(Object.assign(make('button','保存画布设置','primary'),{type:'submit',id:'canvas-save'}));
    form.addEventListener('submit',event => {
      event.preventDefault();
      if (!writable || state.pending) return;
      const patches = specs.flatMap(([prop,old,read]) => {
        const value = read();
        return JSON.stringify(value) === JSON.stringify(old) ? [] : [nativePatch(element,prop,value)];
      });
      if (patches.length) save(patches,form); else error('没有可保存的画布修改。',false);
    });
    pane.append(form);
  }
  function closeInline(focus = false) {
    editor = null;
    inline.hidden = true;
    $('inline-text').value = '';
    const note = inline.querySelector('.form-error');
    if (note) { note.textContent = ''; note.hidden = true; }
    if (focus) stage.focus({preventScroll:true});
  }
  function openInline() {
    const text = currentText();
    if (!text || state.pending || !box) return;
    editor = {figure:state.doc.figure_id, revision:state.doc.revision, ...text};
    ['left','top'].forEach((prop,i) => inline.style[prop] = `${Math.max(0,Math.min(i === 0 ? 1-250/paper.clientWidth : 1-115/paper.clientHeight,box[i]))*100}%`);
    $('inline-text').value = text.field.value;
    $('inline-paste').hidden = !navigator.clipboard?.readText;
    inline.hidden = false;
    $('inline-text').focus({preventScroll:true});
    $('inline-text').select();
  }
  function endDrag(event, commit) {
    if (drag?.pointer !== event.pointerId) return;
    const gesture = drag;
    drag = null;
    if (image.hasPointerCapture(event.pointerId)) image.releasePointerCapture(event.pointerId);
    delete paper.dataset.dragging;
    placeOutline(box);
    if (!gesture.moved) return;
    lastDragAt = performance.now();
    if (!commit || gesture.revision !== state.doc?.revision || gesture.figure !== state.doc.figure_id) return;
    const delta = [event.clientX-gesture.x,event.clientY-gesture.y];
    const patch = movePatch(gesture.target,delta[0]/gesture.width,delta[1]/gesture.height);
    if (patch) save([patch]);
  }
  function buttons(disabled) {
    $('inline-open').disabled = disabled || !currentText() || !box;
    const element = state.doc?.native_manifest?.elements?.find(el => el.gid === currentId());
    const normalized = state.selected?.kind === 'text' ? state.doc?.objects?.find(el => el.id === state.selected.id)?.properties?.text : null;
    const text = element?.editable?.find(field => field.prop === 'text')?.value ?? normalized;
    $('copy-text').disabled = disabled || typeof text !== 'string' || !navigator.clipboard?.writeText;
    return text;
  }
  $('copy-text').addEventListener('click',async () => {
    const text = buttons(state.pending);
    if (typeof text !== 'string' || state.pending) return;
    try { await navigator.clipboard.writeText(text); error('所选文字已复制。',false); }
    catch { error('浏览器未允许写入剪贴板。'); }
  });
  $('inline-open').addEventListener('click',openInline);
  $('inline-paste').addEventListener('click',async () => {
    const draft = editor;
    if (!draft || state.pending) return;
    try {
      const text = await navigator.clipboard.readText();
      if (draft !== editor || state.pending) return;
      const input = $('inline-text');
      input.setRangeText(text,input.selectionStart,input.selectionEnd,'end');
      input.focus({preventScroll:true});
    } catch { error('浏览器未允许读取剪贴板。可使用系统粘贴快捷键。'); }
  });
  $('inline-cancel').addEventListener('click',() => closeInline(true));
  inline.addEventListener('submit',async event => {
    event.preventDefault();
    if (!editor || state.pending || editor.figure !== state.doc?.figure_id || editor.revision !== state.doc?.revision || !latest()) return;
    const value = $('inline-text').value;
    if (value === editor.field.value) { closeInline(true); return; }
    await save([nativePatch(editor.element,'text',value)],inline);
  });
  inline.addEventListener('keydown',event => {
    if (event.key === 'Escape' && !state.pending) { event.preventDefault(); closeInline(true); }
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); inline.requestSubmit(); }
  });
  image.addEventListener('pointerdown',event => {
    if (event.button !== 0 || event.altKey || state.pending || drag || !inline.hidden) return;
    const point = fraction(event), target = point && canMove(point);
    if (!target) return;
    const rect = image.getBoundingClientRect();
    drag = {pointer:event.pointerId,target,x:event.clientX,y:event.clientY,width:rect.width,height:rect.height,revision:state.doc.revision,figure:state.doc.figure_id,moved:false};
    image.setPointerCapture(event.pointerId);
  });
  image.addEventListener('pointermove',event => {
    pointer = fraction(event); ruler();
    if (drag?.pointer !== event.pointerId) return;
    const dx = event.clientX-drag.x, dy = event.clientY-drag.y;
    if (!drag.moved && Math.hypot(dx,dy) < 4) return;
    drag.moved = true;
    paper.dataset.dragging = 'true';
    const [x,y,w,h] = drag.target.box;
    placeOutline([x+dx/drag.width,y+dy/drag.height,w,h]);
  });
  image.addEventListener('pointerup',event => endDrag(event,true));
  for (const event of ['pointercancel','lostpointercapture']) image.addEventListener(event,evt => endDrag(evt,false));
  image.addEventListener('pointerleave',() => { pointer = null; ruler(); });
  image.addEventListener('click',event => {
    if (performance.now()-lastDragAt < 240 && lastDragAt) return;
    if (!inline.hidden && !state.pending) closeInline();
    if (event.detail < 2) pickTask = pick(fraction(event),event.altKey);
  });
  image.addEventListener('dblclick',async event => {
    if (event.altKey || (lastDragAt && performance.now()-lastDragAt < 240)) return;
    await pickTask;
    openInline();
  });
  for (const [id,mode] of [['grid-toggle','grid'],['ruler-toggle','rulers']]) {
    $(id).addEventListener('click',() => {
      const on = $(id).getAttribute('aria-pressed') !== 'true';
      $(id).setAttribute('aria-pressed',String(on));
      (mode === 'grid' ? paper : $('viewport')).dataset[mode] = String(on);
      if (mode === 'rulers') size();
    });
  }
  new ResizeObserver(size).observe(stage);
  stage.addEventListener('scroll',ruler,{passive:true});
  image.addEventListener('load',size);
  return {closeInline, selection, size, details, buttons, properties};
}
