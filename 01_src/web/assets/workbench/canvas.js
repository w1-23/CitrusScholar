import { byId } from './dom.js';
import { SvgScene } from './scene.js?v=1';
import { hexDigest } from './sha256.js?v=1';

const MANUAL_MIN = .5, MANUAL_MAX = 2;
const MOVABLE = new Set(['title', 'xlabel', 'ylabel', 'legend', 'annot', 'colorbar']);

export function untilAborted(pending, signal) {
  if (!signal) return pending;
  return new Promise((resolve, reject) => {
    const abort = () => reject(signal.reason);
    Promise.resolve(pending).then(resolve, reject).finally(() => signal.removeEventListener('abort', abort));
    if (signal.aborted) abort();
    else signal.addEventListener('abort', abort, { once: true });
  });
}

export async function imageAsset(response, format, expectedDigest, signal) {
  signal?.throwIfAborted();
  const blob = await response.blob();
  signal?.throwIfAborted();
  const asset = { url: null, svg: '', digest: null, width: 0, height: 0 };
  if (expectedDigest) {
    const bytes = await blob.arrayBuffer();
    signal?.throwIfAborted();
    const digest = await hexDigest(bytes);
    signal?.throwIfAborted();
    if (digest !== expectedDigest) throw new Error('预览内容与已登记的摘要不一致。');
    asset.digest = expectedDigest;
    if (format === 'svg') asset.svg = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  }
  const image = new Image();
  asset.url = URL.createObjectURL(blob);
  try {
    image.src = asset.url;
    await untilAborted(image.decode(), signal);
    signal?.throwIfAborted();
    asset.width = image.naturalWidth;
    asset.height = image.naturalHeight;
    if (!asset.width || !asset.height) throw new Error('图件没有可读取的画布尺寸。');
    return asset;
  } catch (error) {
    image.removeAttribute('src');
    URL.revokeObjectURL(asset.url);
    throw error;
  }
}

function verifiedObjects(project, asset) {
  const contract = project.object_contract;
  if (!asset.svg || !asset.digest || contract?.renderKey !== asset.digest ||
      contract.revision !== project.render_version || !contract.objects ||
      typeof contract.objects !== 'object' || Array.isArray(contract.objects)) return null;
  const groups = new Set(project.groups.map((group) => group.id));
  const entries = Object.entries(contract.objects);
  if (!entries.length || entries.some(([id, entry]) => !groups.has(id) || entry?.objectId !== id ||
      entry.revision !== contract.revision || entry.renderKey !== contract.renderKey ||
      !Array.isArray(entry.geometry?.svgIds))) return null;
  return contract.objects;
}

export class CanvasView {
  constructor(selectGroup, moveGroup) {
    this.asset = null;
    this.scene = null;
    this.project = null;
    this.selected = null;
    this.zoom = null;
    this.picking = true;
    this.pickCycle = null;
    this.selectGroup = selectGroup;
    this.moveGroup = moveGroup;
    this.drag = null;
    this.suppressClick = false;
    this.canvas = byId('canvas');
    this.image = byId('preview');
    this.mount = byId('inkScene');
    this.idle = Promise.resolve();
    this.offsets = new Map();
    this.image.addEventListener('click', (event) => this.pickObject(event));
    this.image.addEventListener('pointerdown', (event) => this.startDrag(event));
    this.image.addEventListener('pointermove', (event) => this.updateDrag(event));
    this.image.addEventListener('pointerup', (event) => this.finishDrag(event));
    this.image.addEventListener('pointercancel', (event) => this.finishDrag(event, true));
    this.image.addEventListener('lostpointercapture', (event) => this.finishDrag(event, true));
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape' || !this.drag) return;
      event.preventDefault();
      this.finishDrag(null, true);
    });
    byId('zoomOut').addEventListener('click', () => this.resize(1 / 1.3));
    byId('zoomIn').addEventListener('click', () => this.resize(1.3));
    byId('fit').addEventListener('click', () => this.setZoom(null));
    byId('actualSize').addEventListener('click', () => this.setZoom(1));
    byId('canvasZoomForm').addEventListener('submit', (event) => {
      event.preventDefault();
      const input = byId('canvasZoom');
      if (input.checkValidity()) this.setZoom(input.valueAsNumber / 100);
    });
    byId('picking').addEventListener('click', () => {
      this.picking = !this.picking;
      this.pickCycle = null;
      this.renderStatus();
    });
    new ResizeObserver(() => this.layout()).observe(this.canvas);
    window.addEventListener('citrus:scalechange', () => this.layout());
  }

  show(asset, project, reset = false) {
    if (this.asset) URL.revokeObjectURL(this.asset.url);
    if (reset) this.zoom = null;
    this.finishDrag(null, true);
    this.scene?.reset();
    this.scene = null;
    this.offsets.clear();
    this.asset = asset;
    this.project = project;
    this.image.src = asset.url;
    this.image.alt = project.title;
    this.pickCycle = null;
    const objects = verifiedObjects(project, asset);
    if (objects) {
      try { this.scene = new SvgScene(this.mount, asset.svg, objects); }
      catch { this.mount.shadowRoot?.replaceChildren(); }
    }
    this.image.classList.toggle('interactive-preview', !!this.scene?.enabled);
    byId('canvasEmpty').hidden = true;
    byId('canvasSheet').hidden = false;
    for (const id of ['fit', 'actualSize', 'canvasZoom', 'setCanvasZoom']) byId(id).disabled = false;
    this.renderStatus();
    this.layout();
    if (reset) this.recenter();
  }

  renderStatus() {
    const enabled = !!this.scene?.enabled;
    byId('picking').disabled = !enabled;
    byId('picking').setAttribute('aria-pressed', String(enabled && this.picking));
    this.canvas.dataset.picking = String(enabled && this.picking);
    byId('canvasStatus').textContent = enabled ?
      this.picking ? '点击选取对象，Alt + 点击切换重叠对象。支持移动的对象可直接拖动。' : '画布选取已关闭，可从对象组选择。' :
      '无法从此画布精确选取。';
  }

  resize(factor) {
    const current = this.zoom ?? this.fitScale;
    const next = Math.round(current * factor * 100) / 100;
    this.setZoom(Math.max(MANUAL_MIN, Math.min(MANUAL_MAX, next)));
  }

  setZoom(scale) {
    if (!this.asset || scale !== null && (!Number.isFinite(scale) || scale < MANUAL_MIN || scale > MANUAL_MAX)) return;
    this.zoom = scale;
    this.pickCycle = null;
    this.layout();
    this.recenter();
  }

  recenter() {
    this.canvas.scrollLeft = (this.canvas.scrollWidth - this.canvas.clientWidth) / 2;
    this.canvas.scrollTop = (this.canvas.scrollHeight - this.canvas.clientHeight) / 2;
  }

  layout() {
    if (!this.asset) return;
    const style = getComputedStyle(this.canvas);
    const width = this.canvas.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const height = this.canvas.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom);
    this.fitScale = Math.max(.01, Math.min(6, width / this.asset.width, height / this.asset.height));
    const scale = this.zoom ?? this.fitScale;
    byId('zoomOut').disabled = this.zoom !== null && scale <= MANUAL_MIN;
    byId('zoomIn').disabled = this.zoom !== null && scale >= MANUAL_MAX;
    const zoomInput = byId('canvasZoom');
    if (document.activeElement !== zoomInput) zoomInput.value = this.zoom === null ? '' : Math.round(scale * 100);
    this.image.style.width = `${this.asset.width * scale}px`;
    this.image.style.height = `${this.asset.height * scale}px`;
    byId('fit').setAttribute('aria-pressed', String(this.zoom === null));
    byId('actualSize').setAttribute('aria-pressed', String(this.zoom === 1));
    byId('zoomLevel').textContent = `${Math.round(scale * 100)}%`;
  }

  focus(groupId) {
    this.selected = groupId;
    this.scene?.focus(groupId);
  }

  hits(event) {
    return this.picking && this.scene?.enabled ? this.scene.ownersAt(event.clientX, event.clientY) : [];
  }

  setOffsets(commands, savedProject) {
    if (!this.scene) return;
    this.offsets.clear();
    this.scene.restorePositions();
    const draft = new Map(commands.filter((entry) => entry.property === 'offset_px').map((entry) => [entry.group_id, entry.value]));
    const saved = new Map(savedProject.groups.map((group) => [group.id, group.values.offset_px]));
    for (const group of this.project.groups) {
      if (!group.properties.includes('offset_px')) continue;
      const value = draft.get(group.id) || saved.get(group.id) || [0, 0];
      const original = group.values.offset_px || [0, 0];
      const delta = [value[0] - original[0], value[1] - original[1]];
      if (delta.every((component) => component === 0)) continue;
      this.offsets.set(group.id, delta);
      this.scene.position(group.id, ...delta);
    }
  }

  pickObject(event) {
    if (this.suppressClick) { this.suppressClick = false; return; }
    const hits = this.hits(event);
    if (!hits.length) { this.pickCycle = null; return; }
    const last = this.pickCycle;
    const key = hits.join(' ');
    const index = event.altKey && last?.key === key && Math.hypot(event.clientX - last.x, event.clientY - last.y) < 4
      ? (last.index + 1) % hits.length : 0;
    this.pickCycle = { key, index, x: event.clientX, y: event.clientY };
    this.selectGroup(hits[index]);
  }

  startDrag(event) {
    this.suppressClick = false;
    if (event.button !== 0 || event.altKey || !event.isPrimary || this.drag) return;
    const hit = this.hits(event)[0];
    const group = this.project?.groups.find((entry) => entry.id === hit);
    if (!group || !MOVABLE.has(group.kind) || !group.properties.includes('offset_px')) return;
    const start = this.scene.point(event.clientX, event.clientY);
    if (!start) return;
    this.drag = { id: event.pointerId, x: event.clientX, y: event.clientY,
      start, target: group.id, started: false, frame: null };
    this.idle = new Promise((resolve) => { this.drag.done = resolve; });
    this.image.setPointerCapture(event.pointerId);
  }

  updateDrag(event) {
    const drag = this.drag;
    if (!drag || drag.id !== event.pointerId) return;
    if (!drag.started && Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 4) return;
    if (!drag.started) {
      drag.started = true;
      this.selectGroup(drag.target, false);
    }
    drag.event = event;
    if (drag.frame !== null) return;
    drag.frame = requestAnimationFrame(() => {
      drag.frame = null;
      const delta = this.displacement(drag, drag.event);
      if (delta) this.scene.position(drag.target, ...delta.total);
    });
  }

  displacement(drag, event) {
    const point = this.scene.point(event.clientX, event.clientY);
    if (!point) return null;
    const delta = [point.x - drag.start.x, point.y - drag.start.y];
    const former = this.offsets.get(drag.target) || [0, 0];
    const base = this.project.groups.find((entry) => entry.id === drag.target).values.offset_px || [0, 0];
    const limit = (n) => Math.max(-200, Math.min(200, Math.round(n * 10) / 10));
    const value = delta.map((component, index) => limit(base[index] + former[index] + component));
    return { value, total: value.map((component, index) => component - base[index]) };
  }

  finishDrag(event, cancel = false) {
    const drag = this.drag;
    if (!drag || (event && drag.id !== event.pointerId)) return;
    this.drag = null;
    if (drag.frame !== null) cancelAnimationFrame(drag.frame);
    if (this.image.hasPointerCapture(drag.id)) this.image.releasePointerCapture(drag.id);
    if (drag.started) {
      this.suppressClick = true;
      const delta = cancel ? null : this.displacement(drag, event);
      if (!delta || !this.moveGroup(drag.target, delta.value)) {
        this.scene.position(drag.target, ...(this.offsets.get(drag.target) || [0, 0]));
      }
    }
    drag.done();
  }
}
