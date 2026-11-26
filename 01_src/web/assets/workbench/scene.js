// The original image remains inert. This separate, allowlisted SVG projection
// enables local edits only after its byte digest and renderer IDs are confirmed.
const namespace = 'http://www.w3.org/2000/svg';
const xml = 'http://www.w3.org/XML/1998/namespace';
const tags = new Set('svg g path rect circle ellipse line polyline polygon text tspan title desc defs linearGradient radialGradient stop clipPath marker'.split(' '));
const attributes = new Set(`viewBox preserveAspectRatio stroke-width stroke-linecap stroke-linejoin stroke-miterlimit
  stroke-dasharray stroke-dashoffset fill-opacity stroke-opacity font-family font-size font-weight font-style
  text-anchor dominant-baseline gradientUnits gradientTransform stop-color stop-opacity clipPathUnits markerWidth
  markerHeight markerUnits marker-start marker-mid marker-end marker-orient orient refX refY x y x1 y1 x2 y2
  cx cy r rx ry width height d points fill stroke opacity transform offset id clip-path clip-rule fill-rule
  textLength lengthAdjust visibility display`.split(/\s+/));
const drawable = new Set('path rect circle ellipse line polyline polygon text tspan'.split(' '));
const references = new Set('fill stroke clip-path marker-start marker-mid marker-end'.split(' '));
const idPattern = /^[a-zA-Z_][\w.:-]{0,140}$/;
const localUrl = /^url\(#([\w.:-]+)\)$/;
const highlight = 'drop-shadow(0 0 0.85px #087dc4) drop-shadow(0 0 1.25px #087dc4)';

function projection(text) {
  if (/<!DOCTYPE|<!ENTITY/i.test(text)) throw new Error('svg_declaration');
  const original = new DOMParser().parseFromString(text, 'image/svg+xml').documentElement;
  if (original.localName !== 'svg' || original.namespaceURI !== namespace || !original.hasAttribute('viewBox')) {
    throw new Error('svg_root');
  }
  const ids = new Map();
  const required = new Set();
  let count = 0;
  function copy(node, depth) {
    const tag = node.localName;
    if (++count > 40000 || depth > 64 || node.namespaceURI !== namespace || !tags.has(tag) || (tag === 'svg' && depth)) {
      throw new Error('svg_element');
    }
    const result = document.createElementNS(namespace, tag);
    for (const attribute of node.attributes) {
      const { name, value, namespaceURI } = attribute;
      if (namespaceURI === 'http://www.w3.org/2000/xmlns/') continue;
      if (namespaceURI === xml && name === 'xml:space' && /^(default|preserve)$/.test(value)) {
        result.setAttributeNS(xml, name, value);
        continue;
      }
      if (!namespaceURI && ['aria-label', 'role', 'version'].includes(name)) continue;
      if (namespaceURI || !attributes.has(name) || /[\\<>\x00-\x1f]/.test(value)) throw new Error('svg_attribute');
      if (name === 'id') {
        if (!idPattern.test(value) || ids.has(value)) throw new Error('svg_identity');
        ids.set(value, result);
      }
      // Parenthesized paint functions other than local fragments can only be
      // literal RGB/HSL colors. No XML, CSS sheets, scripts, or external URLs.
      if (references.has(name) && /[()]/.test(value)) {
        const match = localUrl.exec(value);
        if (match) required.add(match[1]);
        else if (!/^(?:rgb|rgba|hsl|hsla)\([\d.+%, /-]+\)$/i.test(value)) throw new Error('svg_reference');
      }
      result.setAttribute(name, value);
    }
    for (const child of node.childNodes) {
      if (child.nodeType === Node.ELEMENT_NODE) result.append(copy(child, depth + 1));
      else if ([Node.TEXT_NODE, Node.CDATA_SECTION_NODE].includes(child.nodeType)) result.append(child.textContent);
    }
    return result;
  }
  const svg = copy(original, 0);
  if ([...required].some((id) => !ids.has(id))) throw new Error('svg_reference');
  svg.style.cssText = 'display:block;width:100%;height:100%;pointer-events:none;color:black';
  return { svg, ids, original };
}

export class SvgScene {
  constructor(mount, text, contract) {
    const { svg, ids, original } = projection(text);
    this.root = mount.shadowRoot || mount.attachShadow({ mode: 'open' });
    this.svg = svg;
    this.ids = ids;
    this.groups = new Map();
    this.byNode = new Map();
    this.highlighted = [];
    this.live = new Map();
    this.offsets = new Map();
    this.paint = new WeakMap();
    const rules = document.createElement('style');
    rules.textContent = 'path,rect,circle,ellipse,line,polyline,polygon,text,tspan { pointer-events: visiblePainted; }';
    this.root.replaceChildren(rules, svg);
    for (const [group, entry] of Object.entries(contract)) {
      const nodes = entry.geometry.svgIds.map((id) => ids.get(id));
      if (!nodes.length || nodes.some((node) => !node || node.closest('defs,clipPath,marker'))) continue;
      for (const node of nodes) {
        if (this.byNode.has(node)) throw new Error('svg_ambiguous_object');
        this.byNode.set(node, group);
      }
      this.groups.set(group, nodes);
    }
    this.textGeometry = null;
    if (window.FigureHitScene) {
      const textGids = new Map();
      for (const [node, group] of this.byNode) {
        if (node.matches('text,tspan') || node.querySelector('text,tspan')) textGids.set(node.id, group);
      }
      if (textGids.size) {
        try {
          this.textGeometry = new window.FigureHitScene(original.ownerDocument, { objects: [...textGids.keys()].map((gid) => ({ gid })) }, false);
          this.textGids = textGids;
        } catch { /* Native character cells cannot prove visible glyph ink. */ }
      }
    }
  }

  reset() {
    this.root.replaceChildren();
  }

  get enabled() { return this.groups.size > 0; }

  ownersAt(x, y) {
    const result = new Set();
    const textHits = new Map();
    for (const node of this.root.elementsFromPoint(x, y)) {
      if (!this.svg.contains(node) || !drawable.has(node.localName)) continue;
      let group;
      let visible = true;
      for (let parent = node; parent && this.svg.contains(parent); parent = parent.parentElement) {
        group ??= this.byNode.get(parent);
        if (Number(this.paintStyle(parent).opacity) === 0) { visible = false; break; }
      }
      if (!group || !visible) continue;
      const paint = this.paintStyle(node);
      const painted = (color) => color !== 'none' && color !== 'transparent' && !/^rgba\([^)]*,\s*0\)$/.test(color);
      const fill = painted(paint.fill) && Number(paint.fillOpacity) > 0;
      const stroke = painted(paint.stroke) && Number(paint.strokeOpacity) > 0 && parseFloat(paint.strokeWidth) > 0;
      if (!fill && !stroke) continue;
      // The browser treats zero-alpha paint as painted for event targeting.
      // When only one channel is actually visible, confirm which ink was hit.
      if (!node.matches('text,tspan') && fill !== stroke &&
          ((fill && paint.stroke !== 'none') || (stroke && paint.fill !== 'none'))) {
        const local = new DOMPoint(x, y).matrixTransform(node.getScreenCTM().inverse());
        if (!(fill ? node.isPointInFill(local) : node.isPointInStroke(local))) continue;
      }
      if (node.matches('text,tspan')) {
        if (node.hasAttribute('textLength') || node.closest('text[textLength]')) continue;
        const [dx, dy] = this.offsets.get(group) || [0, 0];
        const key = `${dx},${dy}`;
        if (!textHits.has(key)) {
          const point = this.point(x, y);
          const original = point && { x: point.x - dx, y: point.y - dy };
          textHits.set(key, new Set((this.textGeometry?.hits(original, false) || []).map((gid) => this.textGids.get(gid))));
        }
        if (!textHits.get(key).has(group)) continue;
      }
      result.add(group);
    }
    return [...result];
  }

  paintStyle(node) {
    if (!this.paint.has(node)) this.paint.set(node, getComputedStyle(node));
    return this.paint.get(node);
  }

  focus(id) {
    for (const node of this.highlighted) node.style.removeProperty('filter');
    this.highlighted = this.groups.get(id) || [];
    for (const node of this.highlighted) node.style.filter = highlight;
  }

  point(x, y) {
    const matrix = this.svg.getScreenCTM();
    if (!matrix) return null;
    const point = new DOMPoint(x, y).matrixTransform(matrix.inverse());
    return Number.isFinite(point.x) && Number.isFinite(point.y) ? point : null;
  }

  position(id, x, y) {
    let targets = this.live.get(id);
    if (x === 0 && y === 0) {
      this.restorePosition(id, targets);
      return true;
    }
    if (!targets) {
      const root = this.svg.getScreenCTM();
      if (!root) return false;
      targets = (this.groups.get(id) || []).map((node) => {
        const parent = node.parentElement.getScreenCTM();
        return { node, matrix: parent?.inverse().multiply(root), base: node.getAttribute('transform') };
      });
      if (!targets.length || targets.some(({ matrix }) => !matrix || ![matrix.a, matrix.b, matrix.c, matrix.d].every(Number.isFinite))) return false;
      this.live.set(id, targets);
    }
    this.offsets.set(id, [x, y]);
    for (const { node, matrix, base } of targets) {
      const tx = matrix.a * x + matrix.c * y;
      const ty = matrix.b * x + matrix.d * y;
      node.setAttribute('transform', `translate(${tx} ${ty})${base ? ' ' + base : ''}`);
    }
    return true;
  }

  restorePosition(id, targets = []) {
    for (const { node, base } of targets) {
      if (base === null) node.removeAttribute('transform');
      else node.setAttribute('transform', base);
    }
    this.live.delete(id);
    this.offsets.delete(id);
  }

  restorePositions() {
    for (const [id, targets] of this.live) this.restorePosition(id, targets);
  }
}
