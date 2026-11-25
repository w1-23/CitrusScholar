import { byId } from './dom.js';

const MANUAL_MIN = .5, MANUAL_MAX = 2;
const MOVABLE = new Set(['title', 'xlabel', 'ylabel', 'legend', 'annot', 'colorbar']);

export async function imageAsset(response, format) {
  const blob = await response.blob();
  const asset = { url: URL.createObjectURL(blob), svg: '', width: 0, height: 0 };
  try {
    if (format === 'svg') asset.svg = await blob.text();
    const image = new Image();
    image.src = asset.url;
    await image.decode();
    asset.width = image.naturalWidth;
    asset.height = image.naturalHeight;
    if (!asset.width || !asset.height) throw new Error('图件没有可读取的画布尺寸。');
    return asset;
  } catch (error) {
    URL.revokeObjectURL(asset.url);
    throw error;
  }
}

export class CanvasView {
  constructor(selectGroup, moveGroup) {
    this.asset = null;
    this.geometry = null;
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
    this.image.addEventListener('click', (event) => this.pickObject(event));
    this.image.addEventListener('pointerdown', (event) => this.startDrag(event));
    this.image.addEventListener('pointermove', (event) => this.updateDrag(event));
    this.image.addEventListener('pointerup', (event) => this.finishDrag(event));
    this.image.addEventListener('pointercancel', (event) => this.finishDrag(event, true));
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
    this.drag = null;
    byId('outline').style.transform = '';
    this.asset = asset;
    this.project = project;
    this.image.src = asset.url;
    this.image.alt = project.title;
    this.geometry = null;
    this.pickCycle = null;
    this.groupByGid = new Map();
    for (const group of project.groups) {
      for (const gid of group.pick_ids || []) this.groupByGid.set(gid, group.id);
    }
    byId('canvasEmpty').hidden = true;
    byId('canvasSheet').hidden = false;
    for (const id of ['fit', 'actualSize', 'canvasZoom', 'setCanvasZoom']) byId(id).disabled = false;
    this.renderStatus();
    this.layout();
    if (reset) this.recenter();
  }

  renderStatus() {
    const enabled = !!(this.asset?.svg && this.groupByGid.size && window.FigureHitScene);
    byId('picking').disabled = !enabled;
    byId('picking').setAttribute('aria-pressed', String(enabled && this.picking));
    this.canvas.dataset.picking = String(enabled && this.picking);
    byId('canvasStatus').textContent = !enabled || !this.picking ? '可从对象组选取。' : '点击选取对象，Alt + 点击切换重叠对象。可拖动标题、图例和标签。';
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
    this.outline();
  }

  focus(groupId) {
    this.selected = groupId;
    this.outline();
  }

  spatialIndex() {
    if (!this.geometry) {
      const svg = new DOMParser().parseFromString(this.asset.svg, 'image/svg+xml');
      this.geometry = new window.FigureHitScene(svg, this.project.hit_manifest, true);
    }
    return this.geometry;
  }

  selectionBounds(group) {
    const ids = new Set(group.pick_ids);
    const boxes = (this.project.hit_manifest?.objects || []).filter((row) => ids.has(row.gid) && row.bbox).map((row) => row.bbox);
    if (boxes.length && this.project.figure_size?.length) return { boxes, viewport: [0, 0, ...this.project.figure_size] };
    if (!ids.size || !this.asset?.svg || !window.FigureHitScene) return null;
    try {
      const index = this.spatialIndex();
      for (const item of index.primitives) {
        if (!item.bounds || !item.owners.some((id) => ids.has(id))) continue;
        const [x0, y0, x1, y1] = item.bounds;
        const points = [[x0,y0],[x1,y0],[x0,y1],[x1,y1]].map(([x, y]) => item.matrix.transformPoint({x, y}));
        boxes.push({ x0: Math.min(...points.map((p) => p.x)), y0: Math.min(...points.map((p) => p.y)),
          x1: Math.max(...points.map((p) => p.x)), y1: Math.max(...points.map((p) => p.y)) });
      }
      return { boxes, viewport: index.viewBox };
    } catch { return null; }
  }

  outline() {
    const outline = byId('outline');
    outline.hidden = true;
    const group = this.project?.groups.find((entry) => entry.id === this.selected);
    const region = group && this.selectionBounds(group);
    if (!region?.boxes.length) return;
    const { boxes, viewport: [x, y, width, height] } = region;
    const left = Math.max(x, Math.min(...boxes.map((box) => box.x0)));
    const top = Math.max(y, Math.min(...boxes.map((box) => box.y0)));
    const right = Math.min(x + width, Math.max(...boxes.map((box) => box.x1)));
    const bottom = Math.min(y + height, Math.max(...boxes.map((box) => box.y1)));
    if (right < left || bottom < top) return;
    outline.style.left = `${(left - x) / width * 100}%`;
    outline.style.top = `${(top - y) / height * 100}%`;
    outline.style.width = `${(right - left) / width * 100}%`;
    outline.style.height = `${(bottom - top) / height * 100}%`;
    outline.hidden = false;
  }

  hits(event) {
    if (byId('picking').disabled || !this.picking || !this.asset?.svg) return [];
    try {
      const index = this.spatialIndex();
      const point = index.point(event.clientX, event.clientY, this.image.getBoundingClientRect());
      return [...new Set(index.hits(point, false).map((gid) => this.groupByGid.get(gid)).filter(Boolean))];
    } catch {
      byId('canvasStatus').textContent = '此图暂不可从画布精确选取，可从对象组选取。';
      return [];
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
    if (event.button !== 0 || event.pointerType !== 'mouse' || event.altKey) return;
    const hit = this.hits(event)[0];
    const group = this.project?.groups.find((entry) => entry.id === hit);
    if (!group || !MOVABLE.has(group.kind) || !group.properties.includes('offset_px')) return;
    const rect = this.image.getBoundingClientRect();
    this.drag = { id: event.pointerId, x: event.clientX, y: event.clientY, target: group.id, rect, started: false };
    this.image.setPointerCapture(event.pointerId);
  }

  updateDrag(event) {
    const drag = this.drag;
    if (!drag || drag.id !== event.pointerId) return;
    const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    if (!drag.started && Math.hypot(dx, dy) < 4) return;
    if (!drag.started) {
      drag.started = true;
      this.focus(drag.target);
    }
    // Pointer deltas are viewport CSS pixels. The overlay transform uses local layout pixels.
    const viewportToLayout = this.image.offsetWidth / drag.rect.width;
    byId('outline').style.transform = `translate(${dx * viewportToLayout}px, ${dy * viewportToLayout}px)`;
  }

  finishDrag(event, cancel = false) {
    const drag = this.drag;
    if (!drag || drag.id !== event.pointerId) return;
    this.drag = null;
    byId('outline').style.transform = '';
    if (!drag.started || cancel) return;
    this.suppressClick = true;
    const [, , width, height] = this.geometry.viewBox;
    this.selectGroup(drag.target);
    this.moveGroup(drag.target, (event.clientX - drag.x) * width / drag.rect.width,
      (event.clientY - drag.y) * height / drag.rect.height);
  }
}
