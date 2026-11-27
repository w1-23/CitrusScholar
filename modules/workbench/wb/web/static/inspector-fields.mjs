// SPDX-License-Identifier: AGPL-3.0-only
const names = {
  text:'文字内容',label:'标签',fontfamily:'字体',fontsize:'字号',color:'文字颜色',weight:'字重',style:'字形',
  ha:'水平对齐',va:'垂直对齐',rotation:'旋转',linespacing:'行间距',labelpad:'标签间距',
  alpha:'不透明度',visible:'显示对象',zorder:'绘制顺序',size_mm:'图幅尺寸',position:'位置',facecolor:'填充色',
  edgecolor:'边线色',transparent:'透明背景',linewidth:'线宽',linestyle:'线型',markersize:'标记大小',marker:'标记形状',
  markerfacecolor:'标记填充色',markeredgecolor:'标记边线色',xlim:'横轴范围',ylim:'纵轴范围',xscale:'横轴刻度',yscale:'纵轴刻度',aspect:'宽高比',
  bbox_visible:'显示文字背景',bbox_facecolor:'背景填充色',bbox_alpha:'背景不透明度',bbox_edgecolor:'背景边线色',
  bbox_linewidth:'背景边线宽度',bbox_pad:'背景留白',bbox_rounded:'圆角背景',stroke_enabled:'启用文字描边',
  stroke_color:'描边颜色',stroke_width:'描边宽度',
};
const choices = {
  weight:{normal:'常规',bold:'粗体',light:'细体',medium:'中等',semibold:'半粗',black:'特粗'},
  style:{normal:'直体',italic:'斜体',oblique:'倾斜'},
  ha:{left:'居左',center:'居中',right:'居右'},va:{top:'顶部',center:'居中',bottom:'底部',baseline:'基线',center_baseline:'居中基线'},
  xscale:{linear:'线性',log:'对数',logit:'对数几率',symlog:'对称对数'},yscale:{linear:'线性',log:'对数',logit:'对数几率',symlog:'对称对数'},
};

function make(tag,text = '',cls = '') {
  return Object.assign(document.createElement(tag),{textContent:text,className:cls});
}

function compactField(spec,writable,textual) {
  const {prop,type,value} = spec;
  const labelText = prop === 'color' && !textual ? '颜色' : Object.hasOwn(names,prop) ? names[prop] : `参数 · ${prop}`;
  const label = make('label','','native-field');
  label.dataset.prop = prop;
  const caption = make('span',`${labelText}${typeof spec.unit === 'string' && spec.unit ? `（${spec.unit}）` : ''}`);
  let input, read;
  const options = Array.isArray(spec.options) ? spec.options.filter(item => typeof item === typeof value && ['string','number'].includes(typeof item)) : [];
  if (type === 'enum' && options.length) {
    input = make('select');
    const values = [...new Set([value,...options])];
    const missing = Array.isArray(spec.options_unavailable) ? spec.options_unavailable : [];
    const translated = Object.hasOwn(choices,prop) ? choices[prop] : {};
    input.append(...values.map((item,index) => {
      const label = Object.hasOwn(translated,item) ? translated[item] : String(item);
      const option = new Option(`${label}${missing.includes(item) ? '（环境未安装）' : ''}`,String(index));
      option.disabled = item !== value && missing.includes(item);
      return option;
    }));
    input.value = '0';
    read = () => values[Number(input.value)];
  } else if (typeof value === 'string') {
    const multiline = prop === 'text' || /[\r\n]/.test(value);
    input = make(multiline ? 'textarea' : 'input');
    if (multiline) { input.rows = 2; label.classList.add('native-field-wide'); }
    else input.type = type === 'color' && /^#[a-f0-9]{6}$/i.test(value) ? 'color' : 'text';
    input.value = value;
    const displayed = input.value;
    read = () => input.value === displayed && (input.type === 'color' || multiline) ? value : input.value;
  } else if (typeof value === 'boolean') {
    input = make('input'); input.type = 'checkbox'; input.checked = value;
    read = () => input.checked;
  } else if (typeof value === 'number') {
    input = make('input'); input.type = 'number'; input.step = 'any'; input.required = true; input.value = value;
    if (Number.isFinite(spec.min) && spec.min <= value) input.min = spec.min;
    if (Number.isFinite(spec.max) && spec.max >= value) input.max = spec.max;
    read = () => input.valueAsNumber;
  } else {
    input = make('input'); input.value = JSON.stringify(value ?? null); input.required = true;
    input.title = '使用 JSON 表示数组、对象或空值';
    label.classList.add('native-field-wide');
    read = () => JSON.parse(input.value);
  }
  input.disabled = !writable;
  label.append(caption,input);
  return {label,input,read,prop,value};
}

export function nativeEditor(pane, object, writable, onSave) {
  const props = object.editable ?? [];
  if (!props.length) { pane.append(make('p','此原生对象没有公开的可编辑属性。','muted')); return; }
  const textual = props.some(p => p.prop === 'text');
  const preferred = textual ? ['text','fontfamily','fontsize','color','weight','style','ha','va'] : ['label','color','facecolor','linewidth','alpha','position','size_mm','visible'];
  const fields = props.map(spec => compactField(spec,writable,textual));
  const primary = preferred.flatMap(prop => fields.filter(field => field.prop === prop));
  const rest = fields.filter(field => !preferred.includes(field.prop));
  const form = make('form','','native-editor');
  if (primary.length) {
    const grid = make('div','','native-field-grid');
    grid.append(...primary.map(field => field.label));
    form.append(make('h4',textual ? '文字与排版' : '常用属性','property-heading'),grid);
  }
  const error = make('p','','form-error');
  error.hidden = true;
  error.setAttribute('role','alert');
  if (writable) {
    const submit = make('button','保存对象修改','primary');
    submit.type = 'submit';
    form.append(submit,error);
  }
  if (rest.length) {
    const more = make('details','','native-more');
    more.open = !primary.length;
    more.append(make('summary','更多'));
    const rows = make('div','','native-field-grid');
    rows.append(...rest.map(field => field.label));
    more.append(rows);
    form.append(more);
    for (const field of rest) field.input.addEventListener('invalid',() => { more.open = true; });
  }
  form.addEventListener('submit',event => {
    event.preventDefault();
    if (!writable) return;
    try {
      const patches = fields.flatMap(field => {
        const value = field.read();
        return JSON.stringify(value) === JSON.stringify(field.value) ? [] : [{gid:object.gid,prop:field.prop,value,...(object.identity ? {identity:object.identity} : {})}];
      });
      onSave(patches,form);
    } catch {
      error.textContent = '请检查“更多”中的数组或对象，值必须使用有效的 JSON 格式。';
      error.hidden = false;
    }
  });
  pane.append(form);
}
