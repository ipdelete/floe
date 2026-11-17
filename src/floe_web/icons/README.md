# Lucide SVG subset

These six SVGs are vendored unchanged from `lucide-static@1.74.0`:

- `arrow-left`
- `check`
- `circle-dashed`
- `clock-3`
- `refresh-cw`
- `x`

Source: <https://lucide.dev/> · <https://github.com/lucide-icons/lucide>

The included upstream `LICENSE` contains Lucide's ISC license and the MIT
notice for Feather-derived icons. Keep it with redistributed assets.

Floe reads these packaged assets and renders them inline, adding
`aria-hidden="true"` and `focusable="false"`. CSS sizes them to 1rem (16px at
the default text size); their upstream 24-unit viewBox, stroke width of 2,
and `currentColor` are preserved. Text labels remain visible. There is no
Lucide runtime dependency, icon font, browser script, animation, or CDN.

To update, review a pinned `lucide-static` release, replace only the six named
SVGs and its license, and update the version above. Do not copy the full pack
or add a frontend package manager to Floe.
