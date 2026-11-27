const isId = (value) => typeof value === 'string' && /^[a-f0-9]{32}$/.test(value);

export function legacyEditorUrl(project) {
  if (!isId(project?.figure_id)) return '';
  const href = `/figures/legacy?project=${project.figure_id}`;
  return project.editor_url === href ? href : '';
}

export function sessionReturnUrl(project) {
  const session = project?.return_session_id;
  return typeof session === 'string' && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(session)
    ? `/?figure_session=${session}` : '/';
}
