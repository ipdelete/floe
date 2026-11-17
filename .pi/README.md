# Pi project skills

## Impeccable

The official Pi skill bundle is installed at `skills/impeccable/`. Pi discovers
it automatically in this repository after project trust is granted. In an
existing Pi session, run `/reload`, then use:

```text
/skill:impeccable critique the dashboard at http://127.0.0.1:8080/
/skill:impeccable audit the dashboard's accessibility and mobile layout
/skill:impeccable polish the existing UI without changing its minimal white theme
```

Keep Floe's chosen direction: a small, read-only, server-rendered local monitor,
white theme, system fonts, ordinary links, native HTML disclosures, no application
JavaScript or frontend framework. Refinements should preserve that direction,
not introduce a new visual identity. See `../docs/web.md` and `../AGENTS.md` for
the product's behavior and implementation boundaries.

### Source and updates

- Upstream: <https://github.com/pbakaus/impeccable>
- Skill version: **4.9.2**
- Source commit: `d41d9860b0094dcb68030657da63461212835aaa`
- Copied directory: `.pi/skills/impeccable/`
- Upstream files are unchanged; `LICENSE` and `NOTICE.md` are included alongside
  them. No hooks, extensions, global Pi settings, or project npm dependencies
  were installed.

`.gitattributes` preserves the vendored bundle's bytes and upstream whitespace,
and marks it as third-party code for GitHub language statistics. Floe's own
files retain normal whitespace checks.

To update reproducibly, review a new upstream commit, replace this skill directory
with that commit's Pi bundle, retain its license/notices, and update the version
and commit above. Do not copy upstream's project instructions or other providers'
configurations into Floe.

The bundled launcher pins engine **0.1.25**. It locates an existing compatible
engine or downloads a platform binary on first use, verifies it against the
bundled checksum manifest, and caches it under `~/.impeccable/bin/`. This is a
development tool, not a Floe dependency. Its launcher requires a shell, `curl` or
`wget`, and a SHA-256 tool when a download is needed. No model call is needed for:

```bash
.pi/skills/impeccable/scripts/impeccable engine-probe
```

Installing the skill does not initialize Impeccable product/design records or
change the dashboard. Invoke `init` explicitly if you want its product-context
setup conversation; installing it alone does not run that workflow.
