const isId = (value) => typeof value === 'string' && /^[a-f0-9]{32}$/.test(value);

export function legacyEditorUrl(project) {
  if (!isId(project?.figure_id)) return '';
  const href = `/figures/legacy?project=${project.figure_id}`;
  return project.editor_url === href ? href : '';
}

export function sourceReturnUrl(project) {
  if (!isId(project?.figure_id)) return '';
  try {
    const handoff = JSON.parse(sessionStorage.getItem('citrus:figure-source') || 'null');
    if (handoff?.project === project.figure_id && isId(handoff.file)) {
      return `/?files=1&source=${handoff.file}`;
    }
  } catch { /* Storage is optional. */ }
  return '';
}
