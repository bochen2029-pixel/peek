# peek — changelog

What changed, and why. Snapshots of every file before and after a change live in
`C:\Intellect_AI_tools\_snapshots\<stamp>_<name>\` (before/, after/, MANIFEST-*.txt, CHANGELOG.md).

## 0.3.0 — 2026-09-02 · the switchboard

**Why.** The box now has a family of compiled instruments (facet, everywho, vramtop, everywhen,
everywhere) and peek was the only thing a fresh agent reads first (`peek env`). peek's list of
"sovereign tools" was hardcoded and stale (no facet, no everywhere, no everywho), every organ
needed its own MCP registration, and the pipelines between the organs lived only in READMEs.
The brainstorm (session 2026-09-02) settled on: peek stays the Python / zero-dependency
switchboard, the organs stay compiled instruments, and what moves into peek is routing and
knowledge. Nothing in peek owns a number.

**What.**

- `ORGANS`, a registry: name, exe location(s), how to ask it about itself (`--about`, or
  everywhen's `about` subcommand), and a static card for organs that predate the contract or
  are absent. `organ_about()` normalises either into one shape.
- `peek env` now prints each present organ's card from its own `--about` (version, purpose,
  verbs with examples, the MCP registration line, health right now), then the question verbs.
  `peek env --json` is the machine manifest; `peek env --mcp` prints the one-line
  registrations (peek's aggregator first).
- `peek --mcp` (also `peek mcp`): one MCP stdio server. It spawns each organ's `--mcp` behind a
  pipe on first use, merges their `tools/list` into one catalogue (descriptions prefixed with
  the organ's name), forwards `tools/call` to the owner, and exposes peek's own verbs as tools:
  `peek_view` (with the screenshot attached as an image block), `peek_net`, `peek_fetch`,
  `peek_ports`, `peek_get`, `peek_sh`, `peek_sandbox`, `peek_env`, `peek_fleet`, `peek_stamp`,
  `peek_doctor`, `peek_when`. Children are stopped on exit.
- Question verbs, thin pass-throughs so an agent can ask without knowing which organ answers:
  `find` (facet), `who` (everywho), `gpu` (vramtop), `when` (everywhen search with a one-week
  default window), `grep` (everywhere), `open` (everywho --open, Stage 2).
- `peek fleet [--json]`: the fleet board — every coding-harness session on the box, from
  everywho's attributed processes, joined with vramtop's per-process VRAM, the listeners by pid,
  and everywhen's last-indexed time per session. This is the "muster" view built as JSON over
  JSON rather than as a new collector.
- `peek stamp [--json]`: `gpu_stamp` + `io_stamp` + listener count on one line, for logs.
- `peek doctor [--deep]`: browser, node, each organ's health, WSL, Docker; `--deep` runs the
  organs' selftests.
- `PEEK_VERSION` introduced (0.3.0); `peek -v`. Help text lists the new routes.

**Companion changes in the organs** (their own commits and devlogs carry the detail):
facet `--about`, everywho `--about`, vramtop `--about`, everywhen `about` — each emits the same
JSON shape: organ, version, path, purpose, verbs[{verb, what, example}], mcp{command, args,
tools, register}, health{ok, detail, …}, docs, tape{writes, reads}.

**Not changed.** `peek.mjs` (the Node engine) keeps its previous verbs; the manifest, the
aggregator and the question verbs are Python-engine only for now, as `ws / ports / get`
already were. The existing routes and their flags are untouched.
