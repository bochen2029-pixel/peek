#!/usr/bin/env python3
r"""peek - open ANY local URL in a real browser and get a screenshot + text + console back.

Sovereign, single file, pure Python stdlib (embedded CDP/WebSocket client - no pip,
no node, no external browser tool). It exists because agent "browser panes" refuse
localhost / LAN / private-IP URLs BY POLICY, in every project. You own the machine;
this does not ask permission. Any agent, any URL, one command:

    python C:/peek/peek.py http://127.0.0.1:3080/
    python C:/peek/peek.py https://192.168.1.112:8443/          # LAN + private CA: fine
    python C:/peek/peek.py http://localhost:8097/health --text  # text only
    python C:/peek/peek.py http://127.0.0.1:3080/ --js "document.title"
    python C:/peek/peek.py http://127.0.0.1:3080/ --keep         # leave it open, print CDP port
    python C:/peek/peek.py --attach 64993 --js "[...document.querySelectorAll('button')].map(b=>b.textContent)"

It mints a throwaway headless Chrome/Edge (isolated profile, private-CA trusted via
--ignore-certificate-errors), navigates, waits for load, captures a PNG + the visible
text + any console errors/exceptions, then kills the browser and sweeps the profile.
Nothing persists; your real Chrome is never touched. `--keep` holds it open (prints the
CDP port) so a follow-up `--attach <port> --js ...` can click through a flow.

Windows-first (this box). Set PEEK_BROWSER=<path to chrome.exe> to override discovery.
"""
import argparse
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHOTS = HERE / "_shots"
TEXTS = HERE / "_text"


def die(msg, code=1):
    print(f"peek: {msg}", file=sys.stderr)
    sys.exit(code)


def find_browser():
    """Chrome first, then Edge; PEEK_BROWSER overrides. Both speak the same CDP."""
    cands = [
        os.environ.get("PEEK_BROWSER"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    for name in ("chrome", "chrome.exe", "msedge", "msedge.exe"):
        p = shutil.which(name)
        if p:
            return p
    die("no Chrome or Edge found (set PEEK_BROWSER=path-to-chrome.exe)")


# --------------------------------------------------- minimal RFC6455 WS client
class WS:
    """Just enough WebSocket to drive CDP: masked text frames out, any frame in."""

    def __init__(self, url, timeout=30):
        if not url.startswith("ws://"):
            raise ConnectionError(f"unexpected ws url: {url}")
        hostport, _, path = url[5:].partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("ws handshake closed early")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n", 1)[0]:
            raise ConnectionError("ws upgrade rejected: " + buf.split(b"\r\n", 1)[0].decode("latin1"))
        self._rest = buf.split(b"\r\n\r\n", 1)[1]

    def _read(self, n):
        while len(self._rest) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("ws closed mid-frame")
            self._rest += chunk
        out, self._rest = self._rest[:n], self._rest[n:]
        return out

    def send(self, text):
        payload = text.encode()
        n = len(payload)
        hdr = bytearray([0x81])  # FIN + text opcode
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        self.sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self):
        while True:
            b0, b1 = self._read(2)
            opcode = b0 & 0x0F
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(length) if length else b""
            if opcode == 0x8:
                raise ConnectionError("ws closed by peer")
            if opcode == 0x9:  # ping: skip
                continue
            if opcode in (0x0, 0x1, 0x2):
                return payload.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class CDP:
    """Synchronous CDP: call() blocks for the matching id, buffering events meanwhile."""

    def __init__(self, ws_url):
        self.ws = WS(ws_url)
        self._id = 0
        self.events = []

    def call(self, method, params=None, timeout=60):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", str(msg["error"])))
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)
        raise TimeoutError(f"CDP {method} timed out")

    def eval(self, expr, timeout=60):
        r = self.call("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": True}, timeout)
        return r.get("result", {}).get("value")

    def console_lines(self):
        """Error/warning console output + uncaught exceptions seen so far."""
        out = []
        for e in self.events:
            m = e.get("method")
            p = e.get("params", {})
            if m == "Runtime.consoleAPICalled" and p.get("type") in ("error", "warning"):
                parts = [a.get("value", a.get("description", "")) for a in p.get("args", [])]
                out.append(f"[{p['type']}] " + " ".join(str(x) for x in parts))
            elif m == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails", {})
                txt = (d.get("exception", {}) or {}).get("description") or d.get("text", "exception")
                out.append("[exception] " + str(txt).splitlines()[0])
            elif m == "Log.entryAdded" and p.get("entry", {}).get("level") == "error":
                out.append("[log] " + str(p["entry"].get("text", "")))
        return out

    def close(self):
        self.ws.close()


def http_get_json(port, path):
    import urllib.request
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
        return json.loads(r.read().decode())


def page_ws(port, tries=60):
    for _ in range(tries):
        try:
            targets = http_get_json(port, "/json")
            pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            if pages:
                return pages[0]["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.1)
    die("browser exposed no page target")


def launch(url, headful):
    prof = Path(tempfile.mkdtemp(prefix="peek-"))
    exe = find_browser()
    cmd = [exe, "--remote-debugging-port=0", f"--user-data-dir={prof}",
           "--no-first-run", "--no-default-browser-check",
           "--disable-features=Translate,MediaRouter,OptimizationHints",
           "--window-size=1440,900",
           # Throwaway profile => trust private-CA endpoints (Caddy internal CA,
           # self-signed dev servers). The profile never outlives the session,
           # so the blast radius is one disposable browser.
           "--ignore-certificate-errors",
           url or "about:blank"]
    if not headful:
        cmd.insert(1, "--headless=new")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    portfile = prof / "DevToolsActivePort"
    for _ in range(200):
        if portfile.is_file():
            lines = portfile.read_text().splitlines()
            if lines and lines[0].strip().isdigit():
                return proc, prof, int(lines[0].strip())
        if proc.poll() is not None:
            shutil.rmtree(prof, ignore_errors=True)
            die("browser exited before exposing DevTools (bad browser path?)")
        time.sleep(0.1)
    proc.terminate()
    shutil.rmtree(prof, ignore_errors=True)
    die("browser never exposed its DevTools port within 20s")


def kill(proc, prof):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except OSError:
            pass
    if prof is not None:
        shutil.rmtree(prof, ignore_errors=True)


def report(cdp, args, url):
    title = ""
    where = url
    try:
        title = cdp.eval("document.title || ''") or ""
        where = cdp.eval("location.href") or url
    except Exception:
        pass

    if not args.text:  # screenshot unless text-only
        try:
            shot = cdp.call("Page.captureScreenshot",
                            {"format": "png", "captureBeyondViewport": bool(args.full)}, timeout=60)
            png = base64.b64decode(shot["data"])
            SHOTS.mkdir(exist_ok=True)
            out = SHOTS / f"peek_{time.strftime('%H%M%S')}.png"
            out.write_bytes(png)
            w = int.from_bytes(png[16:20], "big") if len(png) > 24 else 0
            h = int.from_bytes(png[20:24], "big") if len(png) > 24 else 0
            print(f"page:  {title}  --  {where}")
            print(f"shot:  {out}  ({w}x{h}, {len(png) // 1024} KB)")
        except Exception as e:
            print(f"page:  {title}  --  {where}")
            print(f"shot:  (failed: {e})")
    else:
        print(f"page:  {title}  --  {where}")

    if args.js:
        code = args.js[1:] if args.js.startswith("@") else args.js
        if args.js.startswith("@"):
            code = Path(args.js[1:]).read_text(encoding="utf-8")
        try:
            val = cdp.eval(code, timeout=120)
            print("\njs:")
            print(json.dumps(val, indent=2, default=str) if isinstance(val, (dict, list))
                  else ("(no value)" if val is None else val))
        except Exception as e:
            print(f"\njs:  (error: {e})")

    if not args.shot:  # text unless shot-only
        try:
            txt = cdp.eval("document.body ? document.body.innerText : '(no body)'") or ""
        except Exception:
            txt = ""
        snippet = txt.strip()
        if len(snippet) > args.max_chars:
            TEXTS.mkdir(exist_ok=True)
            tp = TEXTS / f"peek_{time.strftime('%H%M%S')}.txt"
            tp.write_text(txt, encoding="utf-8")
            print("\ntext:")
            print(snippet[:1500])
            print(f"\n[... {len(txt):,} chars total -- full: {tp}]")
        else:
            print("\ntext:")
            print(snippet or "(empty page)")

    errs = cdp.console_lines()
    if errs:
        print("\nconsole (errors/exceptions):")
        for line in errs[:20]:
            print("  " + line[:300])


def cmd_peek(args):
    if args.attach:
        cdp = CDP(page_ws(args.attach))
        try:
            cdp.call("Runtime.enable")
            cdp.call("Log.enable")
        except Exception:
            pass
        report(cdp, args, f"(attached :{args.attach})")
        cdp.close()
        print(f"\n[left running -- CDP port {args.attach} still yours]")
        return

    if not args.url:
        die("give a URL (or --attach PORT)")

    proc, prof, port = launch(args.url, args.headful)
    cdp = None
    try:
        cdp = CDP(page_ws(port))
        for m in ("Page.enable", "Runtime.enable", "Log.enable"):
            try:
                cdp.call(m)
            except Exception:
                pass
        end = time.time() + args.wait
        while time.time() < end:
            try:
                if cdp.eval("document.readyState", timeout=10) == "complete":
                    break
            except Exception:
                pass
            time.sleep(0.3)
        time.sleep(args.settle)  # let SPAs paint after 'complete'
        report(cdp, args, args.url)
    finally:
        if args.keep:
            if cdp:
                cdp.close()
            print(f"\n[kept alive -- CDP port {port} (pid {proc.pid}).")
            print(f"  drive: python C:/peek/peek.py --attach {port} --js \"...\"")
            print(f"  kill:  taskkill /PID {proc.pid} /F ]")
        else:
            if cdp:
                cdp.close()
            kill(proc, prof)


def run_fetch(argv):
    """Alternate route #1: raw HTTP, no browser. curl -L -k -c -b in one stdlib
    command, but it SHOWS the whole chain — every redirect hop, its status and
    Location, every Set-Cookie — then the final status, headers, and body. This
    is the tool that instantly explains a page 'stuck on a 303': you watch the
    token -> cookie -> 200 (or 401) dance hop by hop, without a browser that can
    hang on it. Supports methods, a body, and custom headers, so it also hits
    local JSON APIs and WebSocket-less endpoints agents otherwise can't reach."""
    import http.cookiejar
    import ssl
    import urllib.error
    import urllib.request
    ap = argparse.ArgumentParser(
        prog="peek fetch",
        description="Raw HTTP with full redirect+cookie visibility (no browser). The 303-handshake X-ray.")
    ap.add_argument("url")
    ap.add_argument("-X", "--method", default=None, help="HTTP method (default GET, or POST if --data)")
    ap.add_argument("--data", help="request body (string)")
    ap.add_argument("-H", "--header", action="append", default=[], metavar="K:V", help="request header (repeatable)")
    ap.add_argument("--head", action="store_true", help="print the chain + headers only, skip the body")
    ap.add_argument("--max-chars", type=int, default=8000, help="spill body to a file above this many chars")
    a = ap.parse_args(argv)

    hops = []

    class Recorder(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            hops.append((code, req.full_url, newurl, headers.get("Set-Cookie")))
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    cj = http.cookiejar.CookieJar()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # private CAs / self-signed dev certs: just work
    opener = urllib.request.build_opener(
        Recorder, urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPSHandler(context=ctx))
    body = a.data.encode() if a.data else None
    req = urllib.request.Request(a.url, data=body, method=a.method or ("POST" if body else "GET"))
    for h in a.header:
        k, _, v = h.partition(":")
        req.add_header(k.strip(), v.strip())

    t = time.time()
    try:
        resp = opener.open(req, timeout=30)
        content, status, final, rhdr = resp.read(), resp.status, resp.geturl(), resp.headers
    except urllib.error.HTTPError as e:
        content, status, final, rhdr = e.read(), e.code, e.url, e.headers
    except Exception as e:
        die(f"fetch failed: {e}")
    ms = int((time.time() - t) * 1000)

    for code, frm, to, sc in hops:
        cookie = f"   [Set-Cookie: {sc.split(';', 1)[0]}]" if sc else ""
        print(f"  {code}  {frm}\n        -> {to}{cookie}")
    print(f"final: {status}  {final}   ({ms} ms)")
    for k in ("content-type", "content-length", "location", "server"):
        if rhdr.get(k):
            print(f"  {k}: {rhdr.get(k)}")
    jar = [c.name for c in cj]
    if jar:
        print(f"  cookies set: {', '.join(jar)}")
    if not a.head:
        text = content.decode("utf-8", "replace")
        print("\nbody:")
        if len(text) > a.max_chars:
            TEXTS.mkdir(exist_ok=True)
            p = TEXTS / f"fetch_{time.strftime('%H%M%S')}.txt"
            p.write_text(text, encoding="utf-8")
            print(text[:a.max_chars])
            print(f"\n[... {len(text):,} chars total -- full: {p}]")
        else:
            print(text or "(empty body)")


def run_net(argv):
    """Alternate route #2: the request waterfall a page actually fires (CDP
    Network). Every URL the page requests, its status, and any that FAILED —
    the X-ray for a page that serves 200 but boots to a blank screen, because
    it shows the one asset/combo that 404'd or hung and killed the boot. Sees
    what browser-eyes (DOM/screenshot) cannot."""
    ap = argparse.ArgumentParser(
        prog="peek net",
        description="Capture the page's network requests + statuses + failures (why a 200 page renders blank).")
    ap.add_argument("url")
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--wait", type=float, default=15.0, metavar="S", help="max seconds to watch (default 15)")
    ap.add_argument("--settle", type=float, default=3.0, metavar="S", help="extra seconds for late XHR (default 3)")
    ap.add_argument("--all", action="store_true", help="list every request, not just problems + document/script")
    a = ap.parse_args(argv)

    proc, prof, port = launch("about:blank", a.headful)
    cdp = None
    try:
        cdp = CDP(page_ws(port))
        for m in ("Network.enable", "Page.enable", "Runtime.enable", "Log.enable"):
            try:
                cdp.call(m)
            except Exception:
                pass
        cdp.call("Page.navigate", {"url": a.url})
        end = time.time() + a.wait
        while time.time() < end:
            try:
                if cdp.eval("document.readyState", timeout=10) == "complete":
                    break
            except Exception:
                pass
            time.sleep(0.3)
        time.sleep(a.settle)
        try:
            title = cdp.eval("document.title || ''") or ""  # this read also drains trailing events
        except Exception:
            title = ""

        reqs = {}
        order = []
        for e in cdp.events:
            m, p = e.get("method"), e.get("params", {})
            rid = p.get("requestId")
            if m == "Network.requestWillBeSent" and rid:
                r = p.get("request", {})
                reqs[rid] = {"method": r.get("method", "?"), "url": r.get("url", ""),
                             "type": p.get("type", ""), "status": None, "err": None}
                order.append(rid)
            elif m == "Network.responseReceived" and rid in reqs:
                reqs[rid]["status"] = p.get("response", {}).get("status")
            elif m == "Network.loadingFailed" and rid in reqs:
                reqs[rid]["err"] = p.get("errorText", "failed")

        rows = [reqs[r] for r in order]
        bad = [r for r in rows if r["err"] or (r["status"] and r["status"] >= 400) or r["status"] is None]
        print(f"page:  {title!r}  --  {len(rows)} requests, {len(bad)} problem(s)")
        for r in rows:
            core = r["type"] in ("Document", "Script", "XHR", "Fetch")
            problem = r["err"] or (r["status"] and r["status"] >= 400) or r["status"] is None
            if not (a.all or core or problem):
                continue
            st = "ERR" if r["err"] else (str(r["status"]) if r["status"] else "...")
            flag = f"  <== {r['err']}" if r["err"] else (
                "  <== HANGING (no response)" if r["status"] is None else (
                    f"  <== {r['status']}" if r["status"] and r["status"] >= 400 else ""))
            print(f"  {st:>4} {r['method']:4} {r['url'][:118]}{flag}")
        errs = cdp.console_lines()
        if errs:
            print("\nconsole (errors/exceptions):")
            for line in errs[:20]:
                print("  " + line[:300])
    finally:
        if cdp:
            cdp.close()
        kill(proc, prof)


def run_view(argv):
    ap = argparse.ArgumentParser(
        prog="peek",
        description="Open any local/LAN URL in a throwaway browser; return screenshot + text + console. "
                    "No pane, no policy, no localhost block.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  python C:/peek/peek.py http://127.0.0.1:3080/\n"
               "  python C:/peek/peek.py https://192.168.1.112:8443/ --full\n"
               "  python C:/peek/peek.py http://127.0.0.1:3080/ --js \"document.title\" --text\n"
               "  python C:/peek/peek.py http://127.0.0.1:3080/ --keep\n"
               "  python C:/peek/peek.py --attach 64993 --js \"document.querySelector('button').click()\"\n")
    ap.add_argument("url", nargs="?", help="URL to open (any scheme/host - localhost, LAN, private IP)")
    ap.add_argument("--attach", type=int, metavar="PORT", help="drive an already-open --keep session by its CDP port")
    ap.add_argument("--js", metavar="CODE|@FILE", help="run JS in the page and print its return value")
    ap.add_argument("--headful", action="store_true", help="visible window instead of headless")
    ap.add_argument("--full", action="store_true", help="full-page screenshot (beyond viewport)")
    ap.add_argument("--keep", action="store_true", help="leave the browser running; print its CDP port")
    ap.add_argument("--text", action="store_true", help="skip the screenshot, text only")
    ap.add_argument("--shot", action="store_true", help="screenshot only, skip the text dump")
    ap.add_argument("--wait", type=float, default=12.0, metavar="S", help="max seconds to wait for load (default 12)")
    ap.add_argument("--settle", type=float, default=2.0, metavar="S", help="extra seconds for SPAs to paint (default 2)")
    ap.add_argument("--max-chars", type=int, default=100000, help="spill text to a file above this many chars")
    cmd_peek(ap.parse_args(argv))


def main():
    # Default verb is `view` (so `peek <url>` still works); `fetch` and `net`
    # are the alternate routes for when the browser view is the wrong lens or
    # the thing that's hanging.
    argv = sys.argv[1:]
    if argv and argv[0] == "fetch":
        return run_fetch(argv[1:])
    if argv and argv[0] == "net":
        return run_net(argv[1:])
    if argv and argv[0] == "view":
        return run_view(argv[1:])
    return run_view(argv)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
    except (ConnectionError, TimeoutError, RuntimeError) as e:
        die(str(e))
