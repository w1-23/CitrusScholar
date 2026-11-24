import { byId } from './controls.js';

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
    this.zoom = 1;
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
    byId('fit').addEventListener('click', () => this.resize(0));
    byId('picking').addEventListener('click', () => {
      this.picking = !this.picking;
      this.pickCycle = null;
      this.renderStatus();
    });
    new ResizeObserver(() => this.layout()).observe(this.canvas);
  }

  show(asset, project, reset = false) {
    if (this.asset) URL.revokeObjectURL(this.asset.url);
    if (reset) {
      this.zoom = 1;
      byId('fit').textContent = '适应';
    }
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
      for (const gid of group.pick_ids) this.groupByGid.set(gid, group.id);
    }
    byId('canvasEmpty').hidden = true;
    byId('frame').hidden = false;
    for (const id of ['zoomOut', 'zoomIn', 'fit']) byId(id).disabled = false;
    this.renderStatus();
    this.layout();
  }

  renderStatus() {
    const enabled = !!(this.asset?.svg && this.groupByGid.size && window.FigureHitScene);
    byId('picking').disabled = !enabled;
    byId('picking').setAttribute('aria-pressed', String(enabled && this.picking));
    this.canvas.dataset.picking = String(enabled && this.picking);
    byId('canvasStatus').textContent = !enabled || !this.picking ? '可从对象组选取。' : '点击选组。Alt + 点击切换重叠组。可拖动标题、图例和标签。';
  }

  resize(factor) {
    this.zoom = factor ? Math.min(6, Math.max(.3, this.zoom * factor)) : 1;
    byId('fit').textContent = this.zoom === 1 ? '适应' : `${Math.round(this.zoom * 100)}%`;
    this.pickCycle = null;
    this.layout();
  }

  layout() {
    if (!this.asset) return;
    const style = getComputedStyle(this.canvas);
    const ratio = this.project.figure_size?.[0] / this.project.figure_size?.[1] || this.asset.width / this.asset.height;
    const available = this.canvas.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const height = this.canvas.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom);
    const width = Math.max(1, Math.min(available, height * ratio, this.asset.width) * this.zoom);
    this.image.style.width = `${width}px`;
    this.image.style.height = `${width / ratio}px`;
    this.outline();
  }

  focus(groupId) {
    this.selected = groupId;
    this.outline();
  }

  outline() {
    const outline = byId('outline');
    outline.hidden = true;
    const group = this.project?.groups.find((entry) => entry.id === this.selected);
    if (!group || !this.project.figure_size?.length) return;
    const ids = new Set(group.pick_ids);
    const boxes = this.project.hit_manifest.objects.filter((row) => ids.has(row.gid) && row.bbox).map((row) => row.bbox);
    if (!boxes.length) return;
    const [width, height] = this.project.figure_size;
    const left = Math.min(...boxes.map((box) => box.x0));
    const top = Math.min(...boxes.map((box) => box.y0));
    const right = Math.max(...boxes.map((box) => box.x1));
    const bottom = Math.max(...boxes.map((box) => box.y1));
    outline.style.left = `${left / width * 100}%`;
    outline.style.top = `${top / height * 100}%`;
    outline.style.width = `${(right - left) / width * 100}%`;
    outline.style.height = `${(bottom - top) / height * 100}%`;
    outline.hidden = false;
  }

  hits(event) {
    if (byId('picking').disabled || !this.picking || !this.asset?.svg) return [];
    try {
      if (!this.geometry) {
        const svg = new DOMParser().parseFromString(this.asset.svg, 'image/svg+xml');
        this.geometry = new window.FigureHitScene(svg, this.project.hit_manifest, true);
      }
      const point = this.geometry.point(event.clientX, event.clientY, this.image.getBoundingClientRect());
      return [...new Set(this.geometry.hits(point, false).map((gid) => this.groupByGid.get(gid)).filter(Boolean))];
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
    byId('outline').style.transform = `translate(${dx}px, ${dy}px)`;
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
