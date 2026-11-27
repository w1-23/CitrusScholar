# Workbench frontend notices

This Workbench frontend is licensed AGPL-3.0-only. See the repository's `LICENSE` and the Workbench's network-accessible corresponding-source disclosure. No Tavotto logos, themes, bundled assets, or icon libraries were taken. The static modules listed below contain portions of Tavotto authored by Tavotto contributors and modified on 2026-11-27. Their upstream license is unchanged.

Tavotto reference and source pin: [b6de338c3b8d104d64930f18828b9675acb261a5](https://github.com/Tavotto/Tavotto/tree/b6de338c3b8d104d64930f18828b9675acb261a5), [upstream license](https://github.com/Tavotto/Tavotto/blob/b6de338c3b8d104d64930f18828b9675acb261a5/LICENSE), [upstream notices](https://github.com/Tavotto/Tavotto/blob/b6de338c3b8d104d64930f18828b9675acb261a5/THIRD_PARTY_NOTICES.md).

| Pinned upstream file and symbols | Downstream file and adaptation |
| --- | --- |
| `web/src/components/ui/treeKeys.ts`, `TREE_NAV_KEYS`, `isTreeNavKey`, `treeKeyAction` | `wb/web/static/tavotto-primitives.mjs`. Removed TypeScript-only declarations. Used for an ARIA tree over the server manifest in `views.mjs`. |
| `web/src/canvas/Rulers.tsx`, `STEPS`, `pickStep` | `wb/web/static/tavotto-primitives.mjs`. Same spacing strategy, supplied with projected pixels per unit directly. `viewport.mjs` draws independent plain SVG ruler chrome (not figure SVG) against the actual image rectangle and scrollbar origin. |
| `web/src/lib/history.ts`, `HISTORY_LIMIT`, `push` | `wb/web/static/tavotto-primitives.mjs`. Removed generic types and adapted to opaque server version entries. No Immer or local document mutation implementation copied. |
| `web/src/canvas/interactions.ts`, `elementMove` own-element delta calculation | `wb/web/static/viewport.mjs`, `movePatch`. Used top-origin authoritative anchor delta for native position patch and kept separate bottom-origin axes positioning from this Workbench's Python adapter. No React, peer dragging, or SVG transforms copied. |
| `web/src/components/CanvasToolbar.tsx`, `web/src/components/left/LayerTree.tsx`, `web/src/components/ZoomControls.tsx` | Design references only. Bottom floating controls, hierarchical navigation and fit/readout pattern were implemented independently in `wb/web/index.html`, `wb/web/static/workbench.css` and associated browser modules. |

## Solar artwork

Solar Icon Set by 480 Design, [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), [original source](https://github.com/480-Design/Solar-icon-set). The Workbench extracts a subset of the same BoldDuotone symbols from Citrus Scholar Host `01_src/web/assets/file-solar-icons.svg`; geometry is unchanged, symbols are recolored with Citrus Scholar tokens. Host obtains them from `@solar-icons/static` 2.7.0 at [ab241e1bf578dcfe45b0197aee59bd9a08b5fe68](https://github.com/saoudi-h/solar-icons/tree/ab241e1bf578dcfe45b0197aee59bd9a08b5fe68). See the [package's pinned third-party notice](https://github.com/saoudi-h/solar-icons/blob/ab241e1bf578dcfe45b0197aee59bd9a08b5fe68/LICENSE-THIRD-PARTY) and the accompanying `solar-package-license.txt`. The symbol IDs and exact copied vectors are in `wb/web/static/solar.svg`.
