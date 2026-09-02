# peek — look at your own localhost

Agent "browser panes" refuse `localhost` / `127.0.0.1` / LAN / private-IP URLs **by
policy**. You own the machine. `peek` is the standing workaround: a sovereign, zero-
dependency tool that opens **any** URL in a throwaway browser you drive over CDP and
hands back a screenshot, the page text, console errors, the network waterfall, or the
raw HTTP redirect chain. No pip, no npm, no Playwright, no other tool. Any agent, any
session, any runtime.

```bat
peek http://127.0.0.1:3080/               REM if C:\peek is on PATH (it installs itself there)
python C:/peek/peek.py https://192.168.1.112:8443/    REM Python engine — zero deps
node   C:/peek/peek.mjs http://localhost:8097/health  REM Node engine  — native WS/fetch, zero deps
```

## Two engines, pick your runtime (identical behaviour)

| you're in… | call | needs |
|---|---|---|
| anything | `peek <url>` | C:\peek on PATH (auto-added for the user) |
| Python   | `python C:/peek/peek.py <url>` | Python 3 stdlib only |
| Node     | `node C:/peek/peek.mjs <url>` | Node 22+ (global WebSocket/fetch), no npm |

## The capability router — every route a harness might block

`peek` isn't just a browser. It's the standing answer to *"the harness won't let
me do X on my own machine."* Point an agent at it and X happens anyway.

```
peek env              MAP:       GPU, WSL + toolchain, Docker, live local services,
                                 and every verb below — the "what can I do here" briefing
peek <url>            eyes:      screenshot + visible text + console errors, then auto-kill
peek net <url>        waterfall: every request the page fires + status + failures
                                 (the answer to "serves 200 but renders blank")
peek fetch <url>      raw HTTP:  every redirect hop + Set-Cookie + headers + body, no browser
                                 (the answer to "stuck on a 303 / cookie / auth dance")
peek ws <url>         sockets:   open a ws://|wss:// endpoint, --send messages, print frames
peek ports [h:p]      network:   is host:port up? / list every local listener + owner
peek get <url> [out]  download:  save anything to a file (private CAs fine)
peek sh -- <cmd>      shell:     run a command in a throwaway WSL Linux shell (fresh temp cwd)
peek sandbox -- <cmd> sandbox:   run a command in an EPHEMERAL Docker container (--rm, isolated)
peek train [script]   GPU:       run in the fine-tuning conda env (auto-detected) with the GPU, streaming
```

`sh` / `sandbox` / `train` need WSL; `sandbox` also needs Docker in WSL; `train`
needs a conda env carrying unsloth/axolotl/trl (`peek env` shows all of this).
Everything else is browser/HTTP/socket and needs nothing but Chrome-or-Edge.

**Engine coverage:** both `peek.py` and `peek.mjs` do `view / net / fetch / env /
sh / sandbox / train`. `ws / ports / get`, the manifest, the question verbs and the
MCP aggregator below are Python-engine only for now.

## The switchboard (0.3) — one stop for an agent that has to get work done here

The box carries a family of compiled instruments, each answering one question:
[facet](https://github.com/bochen2029-pixel/facet) (which files, and where they went),
`everywhere` (which files contain this), `everywhen` (which sessions said it),
[vramtop](https://github.com/bochen2029-pixel/vramtop) (who holds the GPU),
[everywho](https://github.com/bochen2029-pixel/everywho) (who is touching what, right now).
peek does not absorb them; it fronts them. Every organ answers `--about` with one JSON
object (verbs, MCP command, health), and peek asks instead of guessing:

```
peek env                  the map, now built from the organs' own cards (version, verbs, MCP line, health)
peek env --json           the machine manifest for agents
peek env --mcp            the one-line registrations: peek's aggregator, then each organ

peek --mcp                ONE MCP server for all of it: each organ's tools (facet_query, io_snapshot,
                          gpu_stamp, …) proxied through their own --mcp, plus peek's verbs as tools
                          (peek_view returns the screenshot as an image, peek_sh, peek_sandbox, peek_fetch, …)
                          register once:  claude mcp add peek -- python C:/peek/peek.py --mcp

peek find <query> [--grep W]   which files, where they went, which contain W     (facet + everywhere)
peek who [--agents]            who is doing I/O right now, which session          (everywho)
peek gpu                       who holds the VRAM                                 (vramtop)
peek when <words> [--hours N]  which sessions said it, one-week default window    (everywhen)
peek grep <pattern> [paths]    which files contain it, at drive speed             (everywhere)
peek open PATH                 who has it open                                    (everywho, Stage 2)
peek fleet [--json]            every coding-harness session on the box: pids, cwd, I/O, VRAM, ports, last message
peek stamp [--json]            one receipt line: gpu_stamp + io_stamp + listener count
peek doctor [--deep]           is the box ready for agents; --deep runs the organs' selftests
```

The organs stay where they are and keep their own surfaces; peek owns no number. What
changed, and why, is in [CHANGELOG.md](CHANGELOG.md); before/after snapshots of every
edited file live under `C:\Intellect_AI_tools\_snapshots\`.

## Flags

```
--text                 skip the screenshot (text only)
--shot                 skip the text (screenshot only)
--full                 full-page capture (beyond the viewport)
--headful              visible window instead of headless
--js "CODE" | @file    run JS in the page, print its return value
--keep                 leave the browser running; print its CDP port  (py: then --attach PORT)
--all       (net)      list every request, not just problems + document/script
--head      (fetch)    chain + headers only, skip the body
-X M --data B -H "K:V" (fetch)  method / body / header, for hitting local JSON APIs
--wait S / --settle S  load + SPA-paint budgets
```

## Why it works where the pane doesn't

The pane is a policy fence in the agent host. `peek` launches its **own** headless
Chrome/Edge (isolated throwaway profile under a temp dir, `--ignore-certificate-errors`
so Caddy/self-signed dev certs just work) and speaks the DevTools Protocol to it
directly over a WebSocket implemented inline. Nothing about "which origins are
allowed" applies — it's your browser, your machine, your call. Your real Chrome
profile is never touched; the throwaway profile is swept on exit.

## Output

- Screenshots land in `C:\peek\_shots\`, big text spills to `C:\peek\_text\`.
- `console (errors/exceptions)` is printed when a page logs errors — the fast way
  to diagnose a black/blank page that serves 200 but fails to boot client-side.

## Config

- `PEEK_BROWSER=<path to chrome.exe>` overrides browser discovery (Chrome first,
  then Edge, then PATH).

## Driving a flow (hands, not just eyes)

```bat
REM open and hold it
python C:/peek/peek.py http://127.0.0.1:3080/ --keep
REM -> prints: CDP port 64993

REM then click / read against that same live page
python C:/peek/peek.py --attach 64993 --js "[...document.querySelectorAll('button')].map(b=>b.textContent)"
python C:/peek/peek.py --attach 64993 --js "document.querySelector('[data-voice-speak]').click()"
```

Pure stdlib. No install. Copy the folder to any Windows box and it runs.

## License

MIT — see [LICENSE](LICENSE). Do whatever you want with it.
