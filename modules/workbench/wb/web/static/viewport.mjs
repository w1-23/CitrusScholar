// SPDX-License-Identifier: AGPL-3.0-only
import {pickStep} from './tavotto-primitives.mjs';

const finite = (a, length) => Array.isArray(a) && a.length === length && a.every(Number.isFinite);
const clamp = (n, limit = 1) => Math.max(0, Math.min(limit, n));
const covers = (box, point) => point.x >= box[0] && point.x <= box[0]+box[2] && point.y >= box[1] && point.y <= box[1]+box[3];

export function visibleBounds(elements) {
  const bounds = elements.map(el => {
    const b = el.bbox, c = el.clip_bbox;
    if (!finite(b,4) || b[2] < 0 || b[3] < 0) return null;
    const left = Math.max(0, b[0], finite(c,4) ? c[0] : 0);
    const top = Math.max(0, b[1], finite(c,4) ? c[1] : 0);
    const right = Math.min(1, b[0]+b[2], finite(c,4) ? c[0]+c[2] : 1);
    const bottom = Math.min(1, b[1]+b[3], finite(c,4) ? c[1]+c[3] : 1);
    return right < left || bottom < top ? null : [left,top,right,bottom];
  }).filter(Boolean);
  if (!bounds.length) return null;
  const x = Math.min(...bounds.map(b => b[0])), y = Math.min(...bounds.map(b => b[1]));
  return [x,y,Math.max(...bounds.map(b => b[2]))-x,Math.max(...bounds.map(b => b[3]))-y];
}

export const nativeWritable = doc => !!doc?.native_manifest && doc.editability !== 'preview_only' && doc.native_editability !== 'preview_only';
export const nativeElement = (doc,id) => doc?.native_manifest?.elements?.find(el => el.gid === id);
export function nativeText(doc, id) {
  if (!nativeWritable(doc)) return null;
  const element = nativeElement(doc,id);
  const field = element?.editable?.find(field => field.prop === 'text' && typeof field.value === 'string');
  return field ? {element,field} : null;
}
export const nativePatch = (element, prop, value) => ({gid:element.gid, prop, value, ...(element.identity ? {identity:element.identity} : {})});

export function moveTarget(doc, id, point, locks = new Set()) {
  if (!nativeWritable(doc)) return null;
  const element = nativeElement(doc,id);
  const box = element && visibleBounds([element]);
  if (!box || (point && !covers(box,point)) || locks.has(id)) return null;
  if (element.draggable === true && ['pos_frac','loc_frac'].includes(element.drag_prop) && finite(element.anchor,2)) {
    return {element, box, value:element.anchor, prop:element.drag_prop};
  }
  const target = element.geom_gid ? nativeElement(doc,element.geom_gid) : element;
  const field = target?.editable?.find(field => field.prop === 'position' && finite(field.value,4));
  return target && field && !locks.has(target.gid) ? {element:target, box, value:field.value, prop:'position'} : null;
}

export function movePatch(target, dx, dy) {
  if (!target || !Number.isFinite(dx) || !Number.isFinite(dy)) return null;
  // Tavotto canvas/interactions.ts elementMove uses top-origin anchor fractions.
  // Native axes position has bottom origin (as in wb/native_groups.py).
  const [x,y,...rest] = target.value;
  const round = n => Math.round(n * 10000) / 10000;
  const value = [round(x+dx),round(y+(target.prop === 'position' ? -dy : dy)),...rest];
  return value[0] === round(x) && value[1] === round(y) ? null : nativePatch(target.element,target.prop,value);
}

export function rulerTicks(origin, viewportSize, paperSize, units) {
  if (![origin, viewportSize, paperSize, units].every(Number.isFinite) || !(viewportSize > 0 && paperSize > 0 && units > 0)) return [];
  const pxPerUnit = paperSize / units;
  const step = pickStep(pxPerUnit);
  const start = Math.max(0, Math.ceil(-origin / pxPerUnit / step));
  const end = Math.min(Math.floor(units / step),Math.floor((viewportSize-origin) / pxPerUnit / step));
  return Array.from({length:Math.min(1500, Math.max(0,end-start+1))}, (_, i) => {
    const tick = start+i;
    return {px:origin+tick*step*pxPerUnit, label:tick % 5 === 0 ? String(tick*step) : ''};
  });
}

export function drawRulers(stage, image, rulers, doc, box, pointer) {
  const rect = image.getBoundingClientRect(), viewport = stage.getBoundingClientRect();
  const mm = doc?.native_manifest?.size_mm;
  const units = finite(mm,2) && mm.every(n => n > 0) ? mm : [100,100];
  const ns = 'http://www.w3.org/2000/svg';
  function node(name, attrs, text) {
    const el = document.createElementNS(ns,name);
    for (const [key,value] of Object.entries(attrs)) el.setAttribute(key,String(value));
    if (text !== undefined) el.textContent = text;
    return el;
  }
  rulers.forEach((svg,index) => {
    const {clientWidth:w,clientHeight:h} = svg;
    if (!w || !h) return;
    svg.setAttribute('viewBox',`0 0 ${w} ${h}`);
    if (!doc || !image.complete || !image.naturalWidth) { svg.replaceChildren(); return; }
    const horizontal = index === 0;
    const origin = horizontal ? rect.left - viewport.left : rect.top - viewport.top;
    const length = horizontal ? rect.width : rect.height;
    const shade = (first, size, type) => node('rect',horizontal ? {x:first,y:0,width:size,height:h,class:type} : {x:0,y:first,width:w,height:size,class:type});
    const children = [shade(origin,length,'sheet')];
    if (box) children.push(shade(origin+box[index]*length,box[index+2]*length,'span'));
    const paths = [];
    for (const {px,label} of rulerTicks(origin,horizontal ? w : h,length,units[index])) {
      const mark = Math.floor(px)+.5, tick = label !== '' ? 7 : 3;
      paths.push(horizontal ? `M ${mark} ${h-tick} V ${h}` : `M ${w-tick} ${mark} H ${w}`);
      if (label !== '' && mark+20 < (horizontal ? w : h)) {
        children.push(node('text',horizontal ? {x:mark+2,y:10} : {transform:`translate(11 ${mark+2}) rotate(90)`},label));
      }
    }
    if (paths.length) children.push(node('path',{d:paths.join(' ')}));
    if (pointer) children.push(shade(origin+clamp(pointer[horizontal ? 'x' : 'y'])*length,1,'mark'));
    svg.replaceChildren(...children);
  });
  return finite(mm,2) && mm.every(n => n > 0) ? 'mm' : '%';
}
