// SPDX-License-Identifier: AGPL-3.0-only
// Tavotto, pinned at b6de338c3b8d104d64930f18828b9675acb261a5.
// Modified 2026-11-27 to remove TypeScript and React dependencies. See ../upstream-ui.md.
export const TREE_NAV_KEYS = ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End']

export function isTreeNavKey(key) {
  return TREE_NAV_KEYS.includes(key)
}

export function treeKeyAction(key, rows, index) {
  const row = rows[index]
  if (!row || rows.length === 0) return null
  const last = rows.length - 1
  switch (key) {
    case 'ArrowDown':
      return { type: 'focus', index: Math.min(last, index + 1) }
    case 'ArrowUp':
      return { type: 'focus', index: Math.max(0, index - 1) }
    case 'Home':
      return { type: 'focus', index: 0 }
    case 'End':
      return { type: 'focus', index: last }
    case 'ArrowRight': {
      if (row.expanded === false) return { type: 'expand' }
      if (row.expanded === true) {
        const next = rows[index + 1]
        return next && next.level > row.level ? { type: 'focus', index: index + 1 } : null
      }
      return null
    }
    case 'ArrowLeft': {
      if (row.expanded === true) return { type: 'collapse' }
      for (let i = index - 1; i >= 0; i--) {
        if (rows[i].level < row.level) return { type: 'focus', index: i }
      }
      return null
    }
    default:
      return null
  }
}

// From canvas/Rulers.tsx, projected through pixels per unit instead of ViewTransform.
const STEPS = [1, 2, 5, 10, 20, 50, 100, 200, 500];
export function pickStep(pixelsPerUnit) {
  for (const s of STEPS) if (s * pixelsPerUnit >= 8) return s;
  return STEPS[STEPS.length - 1];
}

// From lib/history.ts push(), storing immutable server revisions as entries.
export const HISTORY_LIMIT = 200;
export function push(past, entry, limit = HISTORY_LIMIT) {
  const next = [...past, entry];
  if (next.length > limit) next.splice(0, next.length - limit);
  return {past:next, future:[]};
}
