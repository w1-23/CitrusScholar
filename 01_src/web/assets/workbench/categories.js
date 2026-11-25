const categories = {
  series: ['series', '数据系列'], sync: ['sync', '共享图例同步'], vector: ['vector', '原生矢量对象'],
  title: ['text', '标题与标签'], xlabel: ['text', '标题与标签'], ylabel: ['text', '标题与标签'],
  annot: ['text', '标题与标签'], ticks: ['axis', '坐标轴与刻度'], spine: ['axis', '坐标轴与刻度'],
  grid: ['axis', '坐标轴与刻度'], legend: ['guide', '图例与色标'], colorbar: ['guide', '图例与色标'],
};

export function displayGroups(groups) {
  const source = new Map(groups.map((group) => [group.id, group]));
  const root = groups.find((group) => group.kind === 'figure');
  const branches = new Map();
  const rows = [];
  for (const group of groups) {
    if (group.kind === 'panel') { rows.push({ ...group, parent: null }); continue; }
    const category = categories[group.kind];
    if (!category || (group.parent && !['figure', 'panel'].includes(source.get(group.parent)?.kind))) {
      rows.push(group);
      continue;
    }
    const panel = source.get(group.parent);
    const parent = panel?.kind === 'panel' ? panel.id : null;
    const key = `category:${parent || 'root'}:${category[0]}`;
    if (!branches.has(key)) {
      const branch = { id: key, parent, label: category[1], kind: 'category', styleKind: category[0],
        pick_ids: [], values: {}, properties: [], members: [] };
      branches.set(key, branch);
      rows.push(branch);
    }
    const branch = branches.get(key);
    branch.members.push(group);
    rows.push({ ...group, parent: key });
  }
  if (root) {
    const row = rows.find((group) => group.id === root.id);
    rows.splice(rows.indexOf(row), 1);
    rows.unshift(row);
  }
  for (const branch of branches.values()) {
    // A category edit means all members. Never silently omit a read-only one.
    if (branch.members.length >= 2) {
      const [first, ...rest] = branch.members;
      branch.properties = first.properties.filter((prop) => rest.every((member) => member.properties.includes(prop)));
      // A batch should not accidentally give each independent object the same name/text.
      branch.properties = branch.properties.filter((prop) => !['label', 'text', 'offset_px'].includes(prop));
    }
  }
  return rows;
}

export function categoryValues(branch, values) {
  const [first, ...rest] = branch.members.map(values);
  return Object.fromEntries(branch.properties.filter((prop) => first[prop] !== undefined &&
    rest.every((member) => JSON.stringify(member[prop]) === JSON.stringify(first[prop])))
    .map((prop) => [prop, first[prop]]));
}
