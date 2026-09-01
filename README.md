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
sh / sandbox / train`. `ws / ports / get` are Python-engine only for now.

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
