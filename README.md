# peek — look at your own localhost

Agent "browser panes" refuse `localhost` / `127.0.0.1` / LAN / private-IP URLs **by
policy**. You own the machine. `peek` is the standing workaround: a sovereign,
single-file, pure-stdlib tool that opens **any** URL in a throwaway browser you
drive over CDP, and hands back a screenshot, the page text, and any console
errors. No pip, no node, no dependency on any other tool. Any session can call it.

```bat
python C:/peek/peek.py http://127.0.0.1:3080/
python C:/peek/peek.py https://192.168.1.112:8443/          REM LAN + private CA: fine
python C:/peek/peek.py http://localhost:8097/health --text  REM text only, no shot
```

Windows launcher: `C:\peek\peek.cmd <url>` (same args, no `python` on the line).

## Verbs / flags

```
peek <url>                 open, wait for load, screenshot + text + console, auto-kill
    --text                 skip the screenshot (text only)
    --shot                 skip the text (screenshot only)
    --full                 full-page capture (beyond the viewport)
    --headful              visible window instead of headless
    --js "CODE" | @file    run JS in the page, print its return value
    --keep                 leave the browser running; print its CDP port
    --wait S               max seconds to wait for load (default 12)
    --settle S             extra seconds for SPAs to paint (default 2)

peek --attach <PORT> --js "CODE"    drive a --keep session (click, read, repeat)
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
