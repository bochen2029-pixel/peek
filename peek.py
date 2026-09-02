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
PEEK_VERSION = "0.3.0"   # 0.3: the switchboard — organs answer --about, peek env asks, peek --mcp aggregates, question verbs


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
        if url.startswith("wss://"):
            body, secure, default_port = url[6:], True, 443
        elif url.startswith("ws://"):
            body, secure, default_port = url[5:], False, 80
        else:
            raise ConnectionError(f"unexpected ws url: {url}")
        hostport, _, path = body.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or default_port)), timeout=timeout)
        if secure:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE  # private CAs / self-signed: just work
            self.sock = ctx.wrap_socket(self.sock, server_hostname=host)
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


def run_ws(argv):
    """Alternate route #3: WebSockets. Agents routinely can't open a ws:// or
    wss:// endpoint from a harness. This connects (private CAs fine), sends any
    messages you give it, and prints the frames that come back. The way to poke
    a local realtime API, a dev-server HMR socket, a game/agent bus, etc."""
    ap = argparse.ArgumentParser(prog="peek ws", description="Open a ws://|wss:// endpoint, send messages, print frames.")
    ap.add_argument("url")
    ap.add_argument("--send", action="append", default=[], metavar="MSG", help="message to send after connect (repeatable)")
    ap.add_argument("--count", type=int, default=10, help="stop after N received frames (default 10)")
    ap.add_argument("--wait", type=float, default=6.0, metavar="S", help="stop after S seconds idle (default 6)")
    a = ap.parse_args(argv)
    try:
        ws = WS(a.url, timeout=max(10.0, a.wait))
    except Exception as e:
        die(f"ws connect failed: {e}")
    print(f"connected: {a.url}")
    for m in a.send:
        ws.send(m)
        print(f"  -> {m[:200]}")
    ws.sock.settimeout(a.wait)
    got = 0
    while got < a.count:
        try:
            frame = ws.recv()
        except socket.timeout:
            print(f"  (idle {a.wait}s, stopping)")
            break
        except ConnectionError as e:
            print(f"  (closed: {e})")
            break
        got += 1
        print(f"  <- {frame[:400]}")
    ws.close()
    print(f"done: {got} frame(s) received")


def run_ports(argv):
    """Alternate route #4: reachability. `ports host:port` says up/down (+ms);
    bare `ports` lists everything LISTENING locally with its owning process.
    The 'is the thing even running / who has that port' answer, no netstat
    literacy required."""
    ap = argparse.ArgumentParser(prog="peek ports", description="Is a host:port up? Or: what is listening locally?")
    ap.add_argument("target", nargs="?", help="host:port to test (omit to list all local listeners)")
    a = ap.parse_args(argv)
    if a.target and ":" in a.target:
        host, _, port = a.target.rpartition(":")
        host = host or "127.0.0.1"
        t = time.time()
        try:
            with socket.create_connection((host, int(port)), timeout=4):
                print(f"UP    {host}:{port}   ({int((time.time() - t) * 1000)} ms)")
        except OSError as e:
            print(f"DOWN  {host}:{port}   ({e.__class__.__name__}: {e})")
        return
    # list local listeners via netstat, resolve PID -> image name
    try:
        ns = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=15).stdout
        tl = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=15).stdout
    except Exception as e:
        die(f"could not read sockets: {e}")
    names = {}
    for line in tl.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[1].strip('"').isdigit():
            names[parts[1].strip('"')] = parts[0].strip('"')
    rows = []
    for line in ns.splitlines():
        f = line.split()
        if len(f) >= 5 and f[0] == "TCP" and f[3] == "LISTENING":
            local, pid = f[1], f[4]
            rows.append((local, pid, names.get(pid, "?")))
    rows.sort(key=lambda r: (0 if r[0].startswith(("127.", "0.0.0.0", "[::")) else 1, r[0]))
    print(f"{len(rows)} listening TCP socket(s):")
    for local, pid, name in rows:
        print(f"  {local:28} pid {pid:6} {name}")


def run_get(argv):
    """Alternate route #5: download. Save ANY url to a file (private CAs fine),
    when the harness won't let an agent fetch or the browser won't save it."""
    import ssl
    import urllib.request
    ap = argparse.ArgumentParser(prog="peek get", description="Download any URL to a file (ignores cert errors).")
    ap.add_argument("url")
    ap.add_argument("out", nargs="?", help="output path (default: basename of the URL, or download.bin)")
    a = ap.parse_args(argv)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    out = a.out or (a.url.rstrip("/").split("/")[-1].split("?")[0] or "download.bin")
    t = time.time()
    try:
        with urllib.request.urlopen(a.url, timeout=60, context=ctx) as r:
            data = r.read()
    except Exception as e:
        die(f"download failed: {e}")
    Path(out).write_bytes(data)
    print(f"saved: {Path(out).resolve()}  ({len(data):,} bytes, {int((time.time() - t) * 1000)} ms)")


def _wsl_sh(script, distro="Ubuntu-24.04", timeout=30):
    """Run a bash snippet in WSL and return its stdout (empty on any failure)."""
    try:
        return subprocess.run(["wsl", "-d", distro, "-u", "root", "--exec", "bash", "-lc", script],
                              capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def _ft_env_probe(force_env=None):
    """Bash that echoes the fine-tuning conda env's python (first with unsloth/
    axolotl/trl), or a forced env's python if it exists. One env per line."""
    import shlex
    head = (f'[ -x {shlex.quote(force_env)} ] && {{ echo {shlex.quote(force_env)}; exit 0; }}; '
            if force_env else '')
    return head + (
        "for MF in /root/miniforge3 /root/miniconda3 /root/anaconda3 /root/mambaforge "
        "$HOME/miniforge3 $HOME/miniconda3 $HOME/anaconda3; do [ -x \"$MF/bin/conda\" ] || continue; "
        "for py in $MF/envs/*/bin/python $MF/bin/python; do [ -x \"$py\" ] || continue; "
        "for x in unsloth axolotl trl; do [ -x \"$(dirname $py)/$x\" ] && { echo \"$py\"; exit 0; }; done; "
        "done; done")


def run_train(argv):
    """Shortcut: run a script/command in the fine-tuning conda env (the one with
    unsloth/axolotl/trl), with the GPU, output streaming live. `peek train` with
    no command just points you at the resolved env. This is the one-liner an
    agent uses to drive training without spelling out the env's python path."""
    import base64 as _b64
    import posixpath
    import re
    import shlex
    ap = argparse.ArgumentParser(prog="peek train", description="Run a script/command in the fine-tuning env (GPU), streaming.")
    ap.add_argument("command", nargs=argparse.REMAINDER, help="script.py [args]  OR  a command (torchrun/accelerate/...)")
    ap.add_argument("--env", help="force a conda env name under /root/miniforge3/envs (default: auto-detect)")
    ap.add_argument("--distro", default="Ubuntu-24.04")
    ap.add_argument("--timeout", type=int, default=0, help="seconds (0 = no limit; training runs long)")
    a = ap.parse_args(argv)

    force = f"/root/miniforge3/envs/{a.env}/bin/python" if a.env else None
    ftpy = _wsl_sh(_ft_env_probe(force), a.distro)
    if not ftpy:
        die("no fine-tuning env found (need a conda env with unsloth/axolotl/trl). Run `peek env` to see what's there.")
    envbin = posixpath.dirname(ftpy)
    envname = posixpath.basename(posixpath.dirname(envbin))

    cmd = a.command[1:] if (a.command and a.command[0] == "--") else a.command
    if not cmd:
        print(f"fine-tuning env: {envname}   ({ftpy})")
        models = _wsl_sh("[ -d /root/models ] && { du -sh /root/models 2>/dev/null | cut -f1; ls /root/models 2>/dev/null | tr '\\n' ' '; }", a.distro)
        if models:
            m = models.split("\n", 1)
            print(f"models: /root/models  {m[0]}  ({m[1].strip() if len(m) > 1 else ''})")
        print("usage:")
        print("  peek train /root/ft/run.py --epochs 3         # env-python on a script (cwd = its dir)")
        print("  peek train -- accelerate launch /root/ft/run.py")
        print("  peek train -- torchrun --nproc_per_node 1 run.py")
        return

    def wslpath(p):
        if re.match(r"^[A-Za-z]:", p) or "\\" in p:
            q = p.replace("\\", "/")
            d, _, rest = q.partition(":")
            return f"/mnt/{d.lower()}{rest}"
        return p

    if cmd[0].lower().endswith(".py"):
        script = wslpath(cmd[0])
        rest = cmd[1:]
        workdir = posixpath.dirname(script) or "."
        body = (f"cd {shlex.quote(workdir)}\n"
                f'export PATH={shlex.quote(envbin)}:"$PATH"\n'
                f"exec {shlex.quote(ftpy)} {shlex.quote(script)} " + " ".join(shlex.quote(x) for x in rest))
        print(f"train: [{envname}] python {script} {' '.join(rest)}   (cwd {workdir})")
    else:
        body = (f'export PATH={shlex.quote(envbin)}:"$PATH"\n'
                "exec " + " ".join(shlex.quote(x) for x in cmd))
        print(f"train: [{envname}] " + " ".join(cmd))

    b64 = _b64.b64encode(body.encode()).decode()
    inner = f"echo {b64} | base64 -d | bash"
    proc = None
    try:
        proc = subprocess.Popen(["wsl", "-d", a.distro, "-u", "root", "--exec", "bash", "-lc", inner])
        proc.wait(timeout=a.timeout or None)
    except subprocess.TimeoutExpired:
        proc.kill()
        die(f"train exceeded {a.timeout}s")
    except KeyboardInterrupt:
        if proc:
            proc.terminate()
        die("interrupted", 130)
    print(f"[exit {proc.returncode}]")


def _wsl_run(script_b64, distro, timeout, docker=False, image="alpine", mount=None):
    """Run a base64'd shell script inside WSL (or an ephemeral Docker container
    in WSL). base64 sidesteps all quoting between Windows -> wsl -> bash -> the
    payload. --exec passes argv verbatim (house rule); the payload never touches
    a Windows command line."""
    if docker:
        mnt = f"-v {mount}:/work -w /work " if mount else ""
        inner = f"echo {script_b64} | base64 -d | docker run --rm -i {mnt}{image} sh"
    else:
        inner = f"cd $(mktemp -d) && echo {script_b64} | base64 -d | bash"
    return subprocess.run(
        ["wsl", "-d", distro, "-u", "root", "--exec", "bash", "-lc", inner],
        capture_output=True, text=True, timeout=timeout)


def run_sh(argv):
    """Alternate route #6: a throwaway Linux shell. Runs your command in WSL
    Ubuntu under a fresh mktemp cwd — a real POSIX box, isolated from Windows,
    for when the harness won't let an agent run what it needs. `--` ends flags
    so the command can contain anything."""
    ap = argparse.ArgumentParser(prog="peek sh", description="Run a command in a throwaway WSL shell (fresh temp cwd).")
    ap.add_argument("command", nargs=argparse.REMAINDER, help="the command line to run (everything after `sh`)")
    ap.add_argument("--distro", default="Ubuntu-24.04")
    ap.add_argument("--timeout", type=int, default=120)
    a = ap.parse_args(argv)
    cmd = " ".join(a.command).lstrip("- ").strip() if a.command else ""
    if not cmd:
        die("give a command:  peek sh -- uname -a")
    import base64 as _b64
    b64 = _b64.b64encode(cmd.encode()).decode()
    try:
        r = _wsl_run(b64, a.distro, a.timeout)
    except subprocess.TimeoutExpired:
        die(f"command exceeded {a.timeout}s")
    except Exception as e:
        die(f"wsl unavailable: {e}")
    if r.stdout:
        sys.stdout.write(r.stdout if r.stdout.endswith("\n") else r.stdout + "\n")
    if r.stderr.strip():
        sys.stderr.write("[stderr] " + r.stderr)
    print(f"[exit {r.returncode}]")


def run_sandbox(argv):
    """Alternate route #7: a disposable VM-grade sandbox. Runs your command in
    an EPHEMERAL Docker container (--rm) inside WSL — throwaway, isolated from
    both Windows and WSL, network-capable, gone the instant it exits. This is
    the 'do whatever, I own the machine' box: let an agent build/run/break
    anything without it touching the host. --mount <winpath> exposes a host dir
    at /work (read-write) if you want output back."""
    ap = argparse.ArgumentParser(prog="peek sandbox", description="Run a command in an ephemeral Docker container (WSL).")
    ap.add_argument("command", nargs=argparse.REMAINDER, help="the command line to run (everything after `sandbox`)")
    ap.add_argument("--image", default="alpine", help="container image (default alpine; pulled if missing)")
    ap.add_argument("--mount", metavar="WINPATH", help="expose a Windows dir at /work (read-write)")
    ap.add_argument("--distro", default="Ubuntu-24.04")
    ap.add_argument("--timeout", type=int, default=300)
    a = ap.parse_args(argv)
    cmd = " ".join(a.command).lstrip("- ").strip() if a.command else ""
    if not cmd:
        die("give a command:  peek sandbox -- python3 -c \"print(2**100)\"")
    mount = None
    if a.mount:
        wp = a.mount.replace("\\", "/")
        drive, _, rest = wp.partition(":")
        mount = f"/mnt/{drive.lower()}{rest}" if _ else wp  # C:/x -> /mnt/c/x
    import base64 as _b64
    b64 = _b64.b64encode(cmd.encode()).decode()
    print(f"sandbox: {a.image} (ephemeral, --rm){'  mount ' + a.mount + ' -> /work' if a.mount else ''}")
    try:
        r = _wsl_run(b64, a.distro, a.timeout, docker=True, image=a.image, mount=mount)
    except subprocess.TimeoutExpired:
        die(f"sandbox exceeded {a.timeout}s")
    except Exception as e:
        die(f"sandbox unavailable (WSL/Docker): {e}")
    if r.stdout:
        sys.stdout.write(r.stdout if r.stdout.endswith("\n") else r.stdout + "\n")
    if r.stderr.strip():
        sys.stderr.write("[stderr] " + r.stderr)
    print(f"[exit {r.returncode}]")


def run_env(argv):
    """The pointer. One command that teaches an agent the full capability
    surface of THIS machine — GPU, WSL + its fine-tuning toolchain, Docker,
    the local services already running, and peek's own escape hatches — so it
    knows what it can reach for instead of assuming it's blocked. This is the
    'you have full control here, here's the map' briefing."""
    # machine forms of the map: --json = the manifest (host + every organ's --about),
    # --mcp = the one-line registrations (each organ, and peek's own aggregator)
    if "--json" in argv:
        print(json.dumps(env_manifest(), ensure_ascii=False))
        return
    if "--mcp" in argv:
        print_mcp_registrations()
        return

    def sh(cmd, timeout=8):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
        except Exception:
            return ""

    def wsl(script, distro="Ubuntu-24.04", timeout=15):
        try:
            return subprocess.run(["wsl", "-d", distro, "-u", "root", "--exec", "bash", "-lc", script],
                                  capture_output=True, text=True, timeout=timeout).stdout.strip()
        except Exception:
            return ""

    print("=== this machine — what an agent can actually do here ===\n")

    print("host:")
    print(f"  Windows, python {sys.version.split()[0]}, node {sh(['node', '-v']) or '(none)'}")
    print(f"  peek: {HERE}  (view net fetch ws ports get sh sandbox env)")

    # GPU: prefer vramtop's per-process truth. nvidia-smi under-reports (often
    # omits) per-PID VRAM on WDDM; vramtop reads the OS kernel accounting, so it
    # answers "who holds the silicon", which is the number that actually decides
    # whether a model will fit. Fall back to nvidia-smi totals if it's absent.
    vramtop = next((p for p in (r"C:\GPUz\vramtop.exe", shutil.which("vramtop"))
                    if p and os.path.isfile(p)), None)
    print("\nGPU:")
    if vramtop:
        stamp = sh([vramtop, "--stamp"], timeout=10)
        print("  " + (stamp.splitlines()[0] if stamp else "(vramtop present, no output)"))
        print(f"  who holds it, per process:  {vramtop} --json   (or -w live / --mcp for agents)")
    else:
        gpu = sh(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free",
                  "--format=csv,noheader"])
        print("  " + (gpu.replace("\n", "\n  ") if gpu else "(no nvidia-smi / no GPU)"))

    print("\nWSL (your Linux — `peek sh -- <cmd>` runs here, isolated from Windows):")
    wl = sh(["wsl", "-l", "-v"]).replace("\x00", "")
    for line in [ln.strip() for ln in wl.splitlines() if ln.strip()][1:]:
        print("  " + " ".join(line.split()))
    # Find the ML/fine-tuning rig: a login python3 rarely has torch — it lives
    # in a conda/mamba env. Enumerate every distro's envs and report torch +
    # CUDA + training tools, so an agent sees the training rig and how to reach
    # it (via `peek sh -- <env-python> ...`) instead of assuming there's none.
    rig = wsl(
        "echo SYSPY=$(python3 --version 2>&1 | awk '{print $2}'); "
        "seen=; "
        "for MF in /root/miniforge3 /root/miniconda3 /root/anaconda3 /root/mambaforge "
        "$HOME/miniforge3 $HOME/miniconda3 $HOME/anaconda3; do "
        "  [ -x \"$MF/bin/conda\" ] || continue; "
        "  rp=$(readlink -f \"$MF\"); case \" $seen \" in *\" $rp \"*) continue;; esac; seen=\"$seen $rp\"; "
        "  echo CONDA=$rp; "
        "  for py in $MF/bin/python $MF/envs/*/bin/python; do "
        "    [ -x \"$py\" ] || continue; "
        "    en=$(basename $(dirname $(dirname \"$py\"))); [ \"$en\" = \"$(basename $MF)\" ] && en=base; "
        "    t=$(\"$py\" -c 'import torch;print(torch.__version__, torch.cuda.is_available())' 2>/dev/null); "
        "    [ -n \"$t\" ] || continue; "
        "    tl=; for x in unsloth axolotl trl accelerate torchrun deepspeed llamafactory-cli vllm; do "
        "      [ -x \"$(dirname $py)/$x\" ] && tl=\"$tl $x\"; done; "
        "    echo \"ENV=$en|$py|$t|$tl\"; "
        "  done; "
        "done; "
        "[ -d /root/models ] && echo \"MODELS=$(du -sh /root/models 2>/dev/null|cut -f1)|$(ls /root/models 2>/dev/null|tr '\\n' ' ')\"; "
        "ls -d /root/llama.cpp* 2>/dev/null | head -1 | sed 's/^/LLAMACPP=/'",
        timeout=40)
    conda_root, printed_rig = None, False
    for line in rig.splitlines():
        if line.startswith("SYSPY="):
            print(f"  system python {line[6:]} (no torch here — the rig is in conda, below)")
        elif line.startswith("CONDA="):
            conda_root = line[6:]
            names = sh(["wsl", "-d", "Ubuntu-24.04", "-u", "root", "--exec", "bash", "-lc",
                        f"ls {conda_root}/envs 2>/dev/null | tr '\\n' ' '"])
            print(f"  conda: {conda_root}  (base {names})".rstrip())
        elif line.startswith("ENV="):
            if not printed_rig:
                print("  training rig:")
                printed_rig = True
            body = line[4:]
            en, py, t, tools = (body.split("|", 3) + ["", "", "", ""])[:4]
            ver, cuda = (t.split() + ["?", "?"])[:2]
            cflag = "cuda✓" if cuda == "True" else "cuda✗"
            ft = "   <-- fine-tuning" if any(k in tools for k in ("unsloth", "axolotl", "trl")) else ""
            print(f"    {en:6} torch {ver:14} {cflag}  [{tools.strip()}]{ft}")
            if ft:
                print(f"           run it:  peek sh -- {py} <script.py>")
        elif line.startswith("MODELS="):
            sz, lst = (line[7:].split("|", 1) + [""])[:2]
            print(f"  models: /root/models  {sz}  ({lst.strip()})")
        elif line.startswith("LLAMACPP="):
            print(f"  llama.cpp: {line[9:]}  (HF->GGUF + LoRA->GGUF converters)")
    if not rig.strip():
        print("  (no conda rig detected; `peek sh -- <cmd>` still gives a full Linux shell)")

    dv = wsl("docker version --format 'docker={{.Server.Version}}' 2>/dev/null; "
             "echo images=$(docker images -q 2>/dev/null | wc -l)")
    print("\nDocker (in WSL — `peek sandbox -- <cmd>` = ephemeral --rm container):")
    print("  " + (dv.replace("\n", "\n  ") if dv else "(docker not reachable in WSL)"))

    print("\nlocal services up right now (`peek ports` for the full list):")
    known = {3080: "DSH harness web", 8097: "voice facade (ASR)", 8197: "voice engine",
             8092: "llama embeddings", 2019: "caddy admin", 5432: "postgres", 6379: "redis",
             11434: "ollama", 8080: "http-alt"}
    any_up = False
    for port, label in sorted(known.items()):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                print(f"  :{port:<6} {label}")
                any_up = True
        except OSError:
            pass
    if not any_up:
        print("  (none of the usual suspects; run `peek ports` to see everything)")

    # The organs: peek is one of a family of sovereign local tools the owner built to
    # give agents senses the harness denies. Since 0.3 the list is not hardcoded: every
    # organ that speaks the --about contract describes itself (verbs, MCP, health), and
    # the ones that predate it get a static card. A fresh agent DISCOVERS them here
    # instead of reinventing one in frustration (which is how peek itself got rebuilt once).
    print("\norgans on this box — each answers one question; `peek env --json` is the manifest, `peek env --mcp` the registrations:")
    print_organ_cards(env_manifest()["organs"])

    print("\npeek asks them for you:")
    print("  peek find <query> [--grep W]   which files, where they went, which contain W       (facet + everywhere)")
    print("  peek who [--agents]            who is doing I/O right now, how much, which session  (everywho)")
    print("  peek gpu                       who holds the VRAM                                   (vramtop)")
    print("  peek when <words> [--hours N]  which sessions said it                               (everywhen)")
    print("  peek grep <pattern> [paths]    which files contain it, at drive speed               (everywhere)")
    print("  peek fleet                     every coding-harness session on the box: pids, cwd, I/O, GPU, ports, last message")
    print("  peek stamp                     one receipt line: gpu_stamp + io_stamp + listeners")
    print("  peek doctor [--deep]           is the box ready for agents (each organ's health; --deep runs selftests)")
    print("  peek --mcp                     ONE MCP server for all of it: the organs' tools + peek's verbs as tools")

    print("\nno blocker is real here — if a harness refuses a URL / port / socket / shell / GPU read,")
    print("route it through peek or one of the organs above. You own this machine; act like it.")


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


# ============================================================================
# 0.3 — the switchboard. peek stays Python and zero-dep; the organs stay compiled
# instruments; what lives here is routing and knowledge: the registry, the --about
# reader behind `peek env`, one MCP server that fronts every organ, and the question
# verbs that compose them. Every measurement still comes from an organ.
# ============================================================================

# The registry: name, where the exe lives, how to ask it about itself, and a static
# card for organs that predate the contract (or are not present). `--about` wins when
# it answers; the card is the fallback so peek env never goes blank.
ORGANS = [
    dict(name="facet", exe=[r"C:\facet\facet.exe"], about=["--about"],
         card=dict(purpose="which files (Everything's index), and where they went: directory tree, extension, date, size, write bursts; --grep scans their contents",
                   verbs=["facet ext:md dm:last3days", "facet -j -x C:\\vendor ext:md dm:today", "facet --grep join ext:md dm:last7days",
                          "facet --paths Q | everywhere --files-from - -e W -l"],
                   mcp=dict(command=r"C:\facet\facet.exe", args=["--mcp"], tools=["facet_query", "facet_list", "facet_count"]))),
    dict(name="everywho", exe=[r"C:\Intellect_AI_tools\everywho\everywho.exe"], about=["--about"],
         card=dict(purpose="who is touching what, right now: per-process I/O with identity and agent attribution, per-disk rates",
                   verbs=["everywho", "everywho -j --agents", "everywho --stamp"],
                   mcp=dict(command=r"C:\Intellect_AI_tools\everywho\everywho.exe", args=["--mcp"], tools=["io_snapshot", "io_stamp"]))),
    dict(name="vramtop", exe=[r"C:\GPUz\vramtop.exe"], about=["--about"],
         card=dict(purpose="who holds the GPU VRAM and burns the engines, per process (Task Manager for the GPU)",
                   verbs=["vramtop", "vramtop --stamp", "vramtop -j", "vramtop --gui"],
                   mcp=dict(command=r"C:\GPUz\vramtop.exe", args=["--mcp"], tools=["gpu_snapshot", "gpu_stamp"]))),
    dict(name="everywhen", exe=[r"C:\everywhen\everywhen.exe"], about=["about"],
         card=dict(purpose="the concordance: full-text search over every Claude Code / DSH session transcript, forks deduped; tapes out, locate in",
                   verbs=["everywhen search --hours 48 --query facet", "everywhen search --hours 720 --query Q --paths", "everywhen locate - --json"],
                   mcp=None)),
    dict(name="everywhere", exe=[r"C:\everywhere\build\Release\everywhere.exe"], about=None,
         card=dict(purpose="which files CONTAIN this: GPU multi-pattern grep at drive speed, ripgrep-compatible, tapes in and out",
                   verbs=["everywhere -n -i -e foo -e bar C:\\src", "facet --paths Q | everywhere --files-from - -e word -l",
                          "everywhere --patterns groups.txt --jsonl C:\\Data"],
                   mcp=None)),
    dict(name="everything", exe=[r"C:\Everything\search.py"], about=None, runner="python",
         card=dict(purpose="instant whole-disk name / path search (Everything 1.4 via es.exe); the index facet pivots",
                   verbs=["python C:/Everything/search.py \"ext:md dm:today\""], mcp=None)),
    dict(name="kernel.sh", exe=[r"C:\kernel.sh\kernel.py"], about=None, runner="python",
         card=dict(purpose="disposable browsers, fan out N at once (local headless or cloud)", verbs=["python C:/kernel.sh/kernel.py --help"], mcp=None)),
    dict(name="voice", exe=[r"C:\voice\voice.py"], about=None, runner="python",
         card=dict(purpose="local ASR + TTS, OpenAI-shaped", verbs=["python C:/voice/voice.py status"], mcp=None)),
    dict(name="chunker", exe=[r"C:\chunker\chunker.py"], about=None, runner="python",
         card=dict(purpose="size / split a file too big to read in one shot", verbs=["python C:/chunker/chunker.py <file>"], mcp=None)),
    dict(name="imguard", exe=[r"C:\imguard"], about=None,
         card=dict(purpose="downsize images before a vision call", verbs=["see C:\\imguard\\README.md"], mcp=None)),
    dict(name="earshot", exe=[r"C:\earshot"], about=None,
         card=dict(purpose="audio / video transcripts", verbs=["see C:\\earshot\\README.md"], mcp=None)),
    dict(name="tower", exe=[r"C:\tower"], about=None,
         card=dict(purpose="telemetry bus + append-only tape/ (time-series vitals; the spool lanes land here)", verbs=["see C:\\tower\\README.md"], mcp=None)),
    dict(name="Intercom", exe=[r"C:\Intercom"], about=None,
         card=dict(purpose="multi-agent messaging bus", verbs=["see C:\\Intercom\\README.md"], mcp=None)),
]


def _organ_path(o):
    for p in o["exe"]:
        if os.path.exists(p):
            return p
    return None


def _run(cmd, timeout=15):
    """Run an organ, capture UTF-8 text. Never raises: (code, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
    except FileNotFoundError:
        return 127, "", "not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def organ_about(o):
    """One organ's card: its own --about when it answers, the registry card otherwise.
    Normalised keys: name, present, path, version, purpose, verbs[], mcp, health, source."""
    path = _organ_path(o)
    card = dict(name=o["name"], present=path is not None, path=path, version=None, purpose=o["card"]["purpose"],
                verbs=[dict(verb=v, what="", example=v) for v in o["card"]["verbs"]], mcp=o["card"].get("mcp"),
                health=dict(ok=None, detail="not present" if path is None else "no --about yet (static card)"), source="card")
    if path is None or not o.get("about"):
        return card
    code, out, err = _run([path] + o["about"], timeout=20)
    line = next((ln for ln in out.splitlines() if ln.startswith("{")), "")
    try:
        d = json.loads(line) if line else None
    except ValueError:
        d = None
    if not d:
        card["health"] = dict(ok=None, detail=f"--about failed ({err.strip()[:80] or 'exit ' + str(code)}); static card")
        return card
    card.update(version=d.get("version"), purpose=d.get("purpose") or card["purpose"], verbs=d.get("verbs") or card["verbs"],
                mcp=d.get("mcp") if d.get("mcp") is not None else card["mcp"], health=d.get("health") or card["health"],
                docs=d.get("docs"), tape=d.get("tape"), stage=d.get("stage"), source="about")
    return card


def env_manifest():
    """The machine manifest: host facts + every organ's card. `peek env --json`."""
    def sh(cmd, timeout=8):
        c, out, _ = _run(cmd, timeout)
        return out.strip() if c == 0 else ""
    host = dict(os="Windows", python=sys.version.split()[0], node=sh(["node", "-v"]) or None, peek=str(HERE), peek_version=PEEK_VERSION,
                browser=(find_browser() if callable(globals().get("find_browser")) else None))
    return dict(tool="peek", version=PEEK_VERSION, host=host, organs=[organ_about(o) for o in ORGANS],
                verbs=["view", "net", "fetch", "ws", "ports", "get", "sh", "sandbox", "train", "env", "find", "who", "gpu", "when", "grep",
                       "open", "fleet", "stamp", "doctor", "mcp"])


def print_organ_cards(cards):
    for c in cards:
        if not c["present"]:
            continue
        ver = f" {c['version']}" if c.get("version") else ""
        h = c.get("health") or {}
        mark = "ok" if h.get("ok") else ("!!" if h.get("ok") is False else "  ")
        print(f"  {c['name']:<10}{ver:<8} {c['purpose']}")
        for v in (c.get("verbs") or [])[:4]:
            ex, what = v.get("example", ""), v.get("what", "")
            print(f"             {ex:<46} {what}" if what else f"             {ex}")
        mcp = c.get("mcp")
        if mcp and mcp.get("command"):
            reg = mcp.get("register") or f"claude mcp add {c['name']} -- {mcp['command']} {' '.join(mcp.get('args', []))}"
            print(f"             mcp: {reg}   ({', '.join(mcp.get('tools', []))})")
        if h.get("detail"):
            print(f"             [{mark}] {h['detail']}")


def print_mcp_registrations():
    print(f"claude mcp add peek -- {sys.executable} {HERE / 'peek.py'} --mcp      # everything below through ONE server, plus peek's own verbs")
    for c in env_manifest()["organs"]:
        mcp = c.get("mcp")
        if c["present"] and mcp and mcp.get("command"):
            print(mcp.get("register") or f"claude mcp add {c['name']} -- {mcp['command']} {' '.join(mcp.get('args', []))}")


# ---------------------------------------------------------------- the question verbs
def _organ_exe_or_die(name):
    o = next(x for x in ORGANS if x["name"] == name)
    p = _organ_path(o)
    if not p:
        die(f"{name} is not on this box (looked at {', '.join(o['exe'])})")
    if o.get("runner") == "python":
        return [sys.executable, p]
    return [p]


def _passthrough(cmd):
    """Hand the console to the organ (its TUI, colours and exit code stay its own)."""
    try:
        return subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        return 130


def run_find(argv):
    """facet: which files, and where they went; --grep adds everywhere's contents scan."""
    return _passthrough(_organ_exe_or_die("facet") + argv)


def run_who(argv):
    """everywho: who is doing I/O right now, how much, which session."""
    return _passthrough(_organ_exe_or_die("everywho") + argv)


def run_gpu(argv):
    """vramtop: who holds the VRAM."""
    return _passthrough(_organ_exe_or_die("vramtop") + argv)


def run_grep(argv):
    """everywhere: which files contain it, at drive speed (rg-compatible flags)."""
    return _passthrough(_organ_exe_or_die("everywhere") + argv)


def run_open(argv):
    """everywho --open: who has this file or folder open (ETW tier, Stage 2)."""
    return _passthrough(_organ_exe_or_die("everywho") + ["--open"] + argv)


def run_when(argv):
    """everywhen: which sessions said it. `peek when <words> [--hours N] [--paths|--json|--count] [--limit N]`
    (default window: 168 h — a week — because 'when' questions are rarely about today)."""
    words, extra, hours, i = [], [], "168", 0
    while i < len(argv):
        a = argv[i]
        if a == "--hours" and i + 1 < len(argv):
            hours = argv[i + 1]
            i += 2
            continue
        if a in ("--paths", "--json", "--count", "--limit", "--field", "--project", "--role", "--source"):
            extra.append(a)
            if a in ("--limit", "--field", "--project", "--role", "--source") and i + 1 < len(argv):
                extra.append(argv[i + 1])
                i += 1
        else:
            words.append(a)
        i += 1
    if not words:
        die("peek when <words> [--hours N] [--paths | --json | --count]")
    return _passthrough(_organ_exe_or_die("everywhen") + ["search", "--hours", hours, "--query", " ".join(words)] + extra)


def _listeners():
    """(local, pid, image) for every LISTENING tcp socket — netstat + tasklist, no elevation."""
    try:
        ns = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=15).stdout
        tl = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=15).stdout
    except Exception:  # noqa: BLE001
        return []
    names = {}
    for line in tl.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[1].strip('"').isdigit():
            names[parts[1].strip('"')] = parts[0].strip('"')
    rows = []
    for line in ns.splitlines():
        f = line.split()
        if len(f) >= 5 and f[0] == "TCP" and f[3] == "LISTENING":
            rows.append((f[1], f[4], names.get(f[4], "?")))
    return rows


def _json_line(cmd, timeout=30):
    """Run an organ in its JSON mode and parse the first JSON line (None on failure)."""
    _, out, _ = _run(cmd, timeout)
    for ln in out.splitlines():
        if ln.startswith("{"):
            try:
                return json.loads(ln)
            except ValueError:
                pass
    return None


def run_stamp(argv):
    """One receipt line for a log: vramtop's gpu_stamp + everywho's io_stamp + listener count."""
    want_json = "--json" in argv
    t = time.strftime("%Y-%m-%dT%H:%M:%S")
    parts, obj = [], dict(stamp="peek", t=t)
    v = next((x for x in ORGANS if x["name"] == "vramtop"), None)
    vp = _organ_path(v) if v else None
    if vp:
        _, out, _ = _run([vp, "--stamp"], 15)
        gpu = out.strip().splitlines()[0] if out.strip() else ""
        parts.append(gpu)
        obj["gpu"] = _json_line([vp, "--stamp", "--json"], 15) if want_json else gpu
    w = next((x for x in ORGANS if x["name"] == "everywho"), None)
    wp = _organ_path(w) if w else None
    if wp:
        _, out, _ = _run([wp, "--stamp", "--sample-ms", "1500"], 20)
        io = out.strip().splitlines()[0] if out.strip() else ""
        parts.append(io)
        obj["io"] = _json_line([wp, "--stamp", "--json", "--sample-ms", "1500"], 20) if want_json else io
    ls = _listeners()
    parts.append(f"listen={len(ls)}")
    obj["listeners"] = len(ls)
    if want_json:
        print(json.dumps(obj, ensure_ascii=False))
    else:
        print(f"peek_stamp t={t} | " + " | ".join(p for p in parts if p))


def run_fleet(argv):
    """The fleet: every coding-harness session on this box — its processes, working directory,
    I/O in the last two seconds, VRAM, listening ports, and when it last spoke (everywhen).
    Built from the organs' JSON; peek owns no number here."""
    want_json = "--json" in argv
    wp = _organ_path(next(x for x in ORGANS if x["name"] == "everywho"))
    if not wp:
        die("fleet needs everywho (C:\\Intellect_AI_tools\\everywho)")
    snap = _json_line([wp, "-j", "--sample-ms", "2000", "--top", "2000", "--agents", "--min-mb", "0"], 30) or {}
    procs = [p for p in snap.get("processes", []) if p.get("agent")]
    # vram per pid, when vramtop is here
    vram = {}
    vp = _organ_path(next(x for x in ORGANS if x["name"] == "vramtop"))
    if vp:
        g = _json_line([vp, "-j", "--no-util"], 20) or {}
        for p in g.get("processes", []) or g.get("procs", []) or []:
            pid = p.get("pid")
            b = next((p[k] for k in ("vram_bytes", "resident_bytes", "local_bytes", "vram") if k in p), None)
            if pid is not None and isinstance(b, (int, float)):
                vram[int(pid)] = vram.get(int(pid), 0) + int(b)
    ports = {}
    for local, pid, _name in _listeners():
        ports.setdefault(int(pid), []).append(local.rsplit(":", 1)[-1])
    last = {}
    ep = _organ_path(next(x for x in ORGANS if x["name"] == "everywhen"))
    if ep:
        _, out, _ = _run([ep, "sessions"], 20)
        for ln in out.splitlines():
            f = ln.split()
            if len(f) >= 5 and len(f[0]) == 36:
                last[f[0]] = f[-1]
    groups = {}
    for p in procs:
        a = p["agent"]
        key = (a.get("harness") or "?", a.get("project") or "?", a.get("session") or "")
        g = groups.setdefault(key, dict(harness=key[0], project=key[1], session=key[2], pids=[], write=0, read=0, vram=0, ports=[], cwd=None))
        g["pids"].append(dict(pid=p["pid"], name=p["name"], rule=a.get("rule")))
        g["write"] += p.get("file_write") or 0
        g["read"] += p.get("file_read") or 0
        g["vram"] += vram.get(p["pid"], 0)
        g["ports"] += ports.get(p["pid"], [])
        if p.get("cwd") and (g["cwd"] is None or a.get("rule") in ("image", "cmdline", "tape")):
            g["cwd"] = p["cwd"]
    rows = sorted(groups.values(), key=lambda g: (-g["write"], g["project"]))
    for g in rows:
        g["last_message"] = last.get(g["session"]) if g["session"] else None
    if want_json:
        print(json.dumps(dict(tool="peek", verb="fleet", window_ms=snap.get("window_ms"), sessions=rows), ensure_ascii=False))
        return
    print(f"=== fleet — coding-harness sessions on this box ({len(rows)}; window {snap.get('window_ms', 0)} ms) ===")
    if not rows:
        print("  (no attributed harness processes right now — everywho --agents sees none)")
    for g in rows:
        names = {}
        for p in g["pids"]:
            names[p["name"]] = names.get(p["name"], 0) + 1
        procs_txt = ", ".join(f"{n}×{c}" if c > 1 else n for n, c in sorted(names.items(), key=lambda kv: -kv[1]))
        sess = (g["session"][:8] + "…") if g["session"] else "session ?"
        vram_txt = f"{g['vram'] / 1048576:.0f} MB" if g["vram"] else "-"
        ports_txt = ",".join(sorted(set(g["ports"]))) or "-"
        print(f"  {g['harness']:<12} {g['project']:<18} {sess:<10} {len(g['pids']):>3} procs  w {g['write'] / 1048576:6.1f} MB  r {g['read'] / 1048576:6.1f} MB  vram {vram_txt:>8}  ports {ports_txt}")
        print(f"               {procs_txt}")
        print(f"               cwd {g['cwd'] or '?'}   last message {g['last_message'] or '(not in the concordance yet — everywhen index)'}")


def run_doctor(argv):
    """Is the box ready for agents: every organ's health, plus the substrate (browser, Everything,
    ETW privilege, WSL, Docker). --deep runs each organ's --selftest too."""
    deep = "--deep" in argv
    rows = []
    def row(state, name, detail):
        rows.append((state, name, detail))
    b = find_browser()
    row("OK" if b else "WARN", "browser", b or "no Chrome/Edge found (set PEEK_BROWSER)")
    row("OK" if shutil.which("node") else "WARN", "node", shutil.which("node") or "not on PATH (peek.mjs unavailable; peek.py fine)")
    for c in env_manifest()["organs"]:
        if not c["present"]:
            if c["name"] in ("facet", "everywho", "vramtop", "everywhen", "everywhere"):
                row("WARN", c["name"], "not present")
            continue
        h = c.get("health") or {}
        state = "OK" if h.get("ok") else ("FAIL" if h.get("ok") is False else "OK")
        row(state, f"{c['name']}{' ' + c['version'] if c.get('version') else ''}", h.get("detail") or "present")
    wl = _run(["wsl", "-l", "-q"], 10)[1].replace("\x00", "").strip()
    row("OK" if wl else "WARN", "WSL", (wl.splitlines()[0] if wl else "no distro (peek sh / sandbox / train unavailable)"))
    dv = _run(["wsl", "-d", "Ubuntu-24.04", "-u", "root", "--exec", "bash", "-lc", "docker version --format '{{.Server.Version}}' 2>/dev/null"], 20)[1].strip()
    row("OK" if dv else "WARN", "Docker (WSL)", dv or "not reachable (peek sandbox unavailable)")
    if deep:
        for name in ("facet", "everywho", "vramtop"):
            o = next(x for x in ORGANS if x["name"] == name)
            p = _organ_path(o)
            if not p:
                continue
            code, out, _ = _run([p, "--selftest"], 240)
            tail = next((ln for ln in reversed(out.splitlines()) if "SELFTEST" in ln), f"exit {code}")
            row("OK" if code == 0 else "FAIL", f"{name} --selftest", tail.strip())
    width = max(len(n) for _, n, _ in rows) + 2
    bad = 0
    for state, name, detail in rows:
        bad += state == "FAIL"
        print(f"  {state:<5} {name:<{width}} {detail}")
    print(f"{'ALL GOOD' if not bad else str(bad) + ' FAILED'} — {len(rows)} checks{' (deep)' if deep else ''}")
    return 1 if bad else 0


# ---------------------------------------------------------------- peek --mcp: one server for the whole box
class _McpChild:
    """An organ's --mcp process behind a pipe: initialize, list its tools, forward calls."""

    def __init__(self, name, cmd):
        self.name, self.cmd, self.p, self.next_id, self.tools = name, cmd, None, 1000, []

    def start(self):
        self.p = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "peek", "version": PEEK_VERSION}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        r = self.request("tools/list", {}) or {}
        self.tools = r.get("tools", [])

    def _send(self, obj):
        self.p.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.p.stdin.flush()

    def request(self, method, params, timeout=300):
        rid = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError(f"{self.name} --mcp exited")
            try:
                obj = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if obj.get("id") == rid:
                return obj.get("result") if "result" in obj else {"error": obj.get("error")}
        raise TimeoutError(f"{self.name} did not answer within {timeout}s")

    def stop(self):
        if self.p and self.p.poll() is None:
            try:
                self.p.stdin.close()
                self.p.wait(timeout=3)
            except Exception:  # noqa: BLE001
                self.p.kill()


_OWN_TOOLS = [
    ("peek_view", "Open any URL (localhost / LAN / private IP included) in a throwaway browser: page text, console errors, and the screenshot as an image.",
     {"url": {"type": "string"}, "text_only": {"type": "boolean"}, "full": {"type": "boolean"}, "js": {"type": "string", "description": "run in the page, return its value"},
      "wait": {"type": "number"}, "settle": {"type": "number"}}, ["url"]),
    ("peek_net", "The page's request waterfall: every request, status, failures (why a 200 renders blank).", {"url": {"type": "string"}, "all": {"type": "boolean"}}, ["url"]),
    ("peek_fetch", "Raw HTTP without a browser: redirect hops, Set-Cookie, headers, body; method / data / headers for local APIs.",
     {"url": {"type": "string"}, "method": {"type": "string"}, "data": {"type": "string"}, "headers": {"type": "array", "items": {"type": "string"}}, "head": {"type": "boolean"}}, ["url"]),
    ("peek_ports", "Is host:port up, or list every local listener with its owning process.", {"target": {"type": "string", "description": "host:port; omit to list listeners"}}, []),
    ("peek_get", "Download any URL to a file (private CAs fine).", {"url": {"type": "string"}, "out": {"type": "string"}}, ["url"]),
    ("peek_sh", "Run a command in a throwaway WSL Linux shell (fresh temp cwd), streaming output back.", {"cmd": {"type": "string"}, "timeout": {"type": "integer"}}, ["cmd"]),
    ("peek_sandbox", "Run a command in an ephemeral Docker container inside WSL (--rm, isolated).", {"cmd": {"type": "string"}, "image": {"type": "string"}, "timeout": {"type": "integer"}}, ["cmd"]),
    ("peek_env", "The machine map: host, GPU, WSL rig, Docker, live services, and every organ's card with verbs, MCP and health.", {"json": {"type": "boolean"}}, []),
    ("peek_fleet", "Every coding-harness session on the box: processes, cwd, I/O, VRAM, ports, last message.", {}, []),
    ("peek_stamp", "One receipt line: gpu_stamp + io_stamp + listener count.", {"json": {"type": "boolean"}}, []),
    ("peek_doctor", "Is the box ready for agents: each organ's health and the substrate; deep runs selftests.", {"deep": {"type": "boolean"}}, []),
    ("peek_when", "Which sessions said it (everywhen full-text over session transcripts).", {"words": {"type": "string"}, "hours": {"type": "integer"}, "json": {"type": "boolean"}, "paths": {"type": "boolean"}}, ["words"]),
]


def _own_tool_argv(name, a):
    a = a or {}
    if name == "peek_view":
        v = ["view", a["url"]]
        if a.get("text_only"):
            v.append("--text")
        if a.get("full"):
            v.append("--full")
        if a.get("js"):
            v += ["--js", a["js"]]
        if a.get("wait"):
            v += ["--wait", str(a["wait"])]
        if a.get("settle"):
            v += ["--settle", str(a["settle"])]
        return v
    if name == "peek_net":
        return ["net", a["url"]] + (["--all"] if a.get("all") else [])
    if name == "peek_fetch":
        v = ["fetch", a["url"]]
        if a.get("method"):
            v += ["-X", a["method"]]
        if a.get("data"):
            v += ["--data", a["data"]]
        for h in a.get("headers") or []:
            v += ["-H", h]
        if a.get("head"):
            v.append("--head")
        return v
    if name == "peek_ports":
        return ["ports"] + ([a["target"]] if a.get("target") else [])
    if name == "peek_get":
        return ["get", a["url"]] + ([a["out"]] if a.get("out") else [])
    if name == "peek_sh":
        return ["sh"] + (["--timeout", str(a["timeout"])] if a.get("timeout") else []) + ["--", a["cmd"]]
    if name == "peek_sandbox":
        return ["sandbox"] + (["--image", a["image"]] if a.get("image") else []) + (["--timeout", str(a["timeout"])] if a.get("timeout") else []) + ["--", a["cmd"]]
    if name == "peek_env":
        return ["env"] + (["--json"] if a.get("json") else [])
    if name == "peek_fleet":
        return ["fleet"]
    if name == "peek_stamp":
        return ["stamp"] + (["--json"] if a.get("json") else [])
    if name == "peek_doctor":
        return ["doctor"] + (["--deep"] if a.get("deep") else [])
    if name == "peek_when":
        v = ["when"] + a["words"].split()
        if a.get("hours"):
            v += ["--hours", str(a["hours"])]
        if a.get("json"):
            v.append("--json")
        if a.get("paths"):
            v.append("--paths")
        return v
    return None


def run_mcp(argv):
    """peek --mcp: ONE MCP stdio server. It spawns each organ's own --mcp behind a pipe, merges
    their tools into one catalogue, forwards calls, and exposes peek's verbs as tools beside
    them. One `claude mcp add peek -- python C:/peek/peek.py --mcp` gives an agent the whole box."""
    import msvcrt
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    children, owner = {}, {}

    def out(obj):
        sys.stdout.buffer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()

    def result(rid, res):
        out({"jsonrpc": "2.0", "id": rid, "result": res})

    def text(t, is_error=False, extra=None):
        content = [{"type": "text", "text": t}]
        if extra:
            content += extra
        return {"content": content, "isError": is_error}

    def ensure_children():
        for o in ORGANS:
            if o["name"] in children or not o["card"].get("mcp"):
                continue
            p = _organ_path(o)
            if not p:
                continue
            ch = _McpChild(o["name"], [p] + o["card"]["mcp"]["args"])
            try:
                ch.start()
            except Exception as e:  # noqa: BLE001
                ch.tools = []
                ch.error = str(e)
            children[o["name"]] = ch
            for t in ch.tools:
                owner[t["name"]] = ch

    def own_tool_list():
        return [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": p, "required": r}} for n, d, p, r in _OWN_TOOLS]

    def call_own(name, args):
        argv2 = _own_tool_argv(name, args)
        if argv2 is None:
            return text(f"unknown tool {name}", True)
        t0 = time.time()
        code, o, e = _run([sys.executable, str(HERE / "peek.py")] + argv2, timeout=600)
        body = (o + ("\n[stderr] " + e if e.strip() else "")).strip() or f"(no output, exit {code})"
        extra = []
        if name == "peek_view" and not (args or {}).get("text_only") and SHOTS.exists():
            shots = sorted((p for p in SHOTS.glob("*.png") if p.stat().st_mtime >= t0 - 1), key=lambda p: p.stat().st_mtime)
            if shots and shots[-1].stat().st_size < 6 * 1024 * 1024:
                extra.append({"type": "image", "data": base64.b64encode(shots[-1].read_bytes()).decode("ascii"), "mimeType": "image/png"})
        return text(body, code not in (0, None), extra)

    try:
        for raw in sys.stdin.buffer:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                continue
            rid, method = req.get("id"), req.get("method", "")
            if method == "initialize":
                result(rid, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "peek", "version": PEEK_VERSION}})
            elif method == "ping":
                result(rid, {})
            elif method == "tools/list":
                ensure_children()
                tools = own_tool_list()
                for ch in children.values():
                    for t in ch.tools:
                        t2 = dict(t)
                        t2["description"] = f"[{ch.name}] " + t.get("description", "")
                        tools.append(t2)
                result(rid, {"tools": tools})
            elif method == "tools/call":
                ensure_children()
                params = req.get("params") or {}
                name, args = params.get("name", ""), params.get("arguments") or {}
                if name in {n for n, _, _, _ in _OWN_TOOLS}:
                    result(rid, call_own(name, args))
                elif name in owner:
                    try:
                        result(rid, owner[name].request("tools/call", {"name": name, "arguments": args}))
                    except Exception as e:  # noqa: BLE001
                        result(rid, text(f"{owner[name].name}: {e}", True))
                else:
                    result(rid, text(f"unknown tool: {name}", True))
            elif rid is not None:
                out({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}})
    finally:
        for ch in children.values():
            ch.stop()


def main():
    # Default verb is `view` (so `peek <url>` still works); `fetch` and `net`
    # are the alternate routes for when the browser view is the wrong lens or
    # the thing that's hanging. 0.3 adds the question verbs and the MCP aggregator.
    argv = sys.argv[1:]
    # UTF-8 out whatever we are attached to: a pipe from Git Bash or a harness defaults to the
    # ANSI code page and turns every "·" and "—" into mojibake; the console writer is unaffected.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    routes = {"fetch": run_fetch, "net": run_net, "ws": run_ws, "ports": run_ports,
              "get": run_get, "sh": run_sh, "sandbox": run_sandbox, "train": run_train,
              "env": run_env, "view": run_view,
              "find": run_find, "who": run_who, "gpu": run_gpu, "when": run_when, "grep": run_grep, "open": run_open,
              "fleet": run_fleet, "stamp": run_stamp, "doctor": run_doctor, "mcp": run_mcp, "--mcp": run_mcp}
    if argv and argv[0] in routes:
        return routes[argv[0]](argv[1:])
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        print(f"peek {PEEK_VERSION}")
        print("routes (the harness can't fence these): view (default) | net | fetch | ws | ports | get | sh | sandbox | train")
        print("  view <url>       screenshot + text + console (browser eyes)")
        print("  net <url>        request waterfall + failures (why a 200 boots blank)")
        print("  fetch <url>      raw HTTP: redirect hops + cookies + headers + body")
        print("  ws <url>         open a ws://|wss:// endpoint, send/print frames")
        print("  ports [host:port]  is it up? / list all local listeners")
        print("  get <url> [out]  download anything to a file")
        print("  sh -- <cmd>      run a command in a throwaway WSL shell")
        print("  sandbox -- <cmd> run a command in an ephemeral Docker container")
        print("  train [script]   run in the fine-tuning conda env with the GPU (streaming)")
        print("the map:  env [--json | --mcp]   what this machine can do: host, GPU, WSL rig, Docker, services, every organ's card")
        print("questions (peek asks the organs): find <query> [--grep W] | who | gpu | when <words> [--hours N] | grep <pattern> [paths]")
        print("          open PATH | fleet [--json] | stamp [--json] | doctor [--deep]")
        print("one server: peek --mcp   (every organ's MCP tools + peek's verbs as tools; register with `peek env --mcp`)")
        return
    if argv and argv[0] == "-v" or argv and argv[0] == "--version":
        print(f"peek {PEEK_VERSION}")
        return
    return run_view(argv)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
    except (ConnectionError, TimeoutError, RuntimeError) as e:
        die(str(e))
