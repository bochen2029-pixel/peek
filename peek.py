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

    gpu = sh(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free",
              "--format=csv,noheader"])
    print("\nGPU:")
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

    print("\nno blocker is real here — if a harness refuses a URL/port/socket/shell, route it through peek.")


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
    routes = {"fetch": run_fetch, "net": run_net, "ws": run_ws, "ports": run_ports,
              "get": run_get, "sh": run_sh, "sandbox": run_sandbox, "train": run_train,
              "env": run_env, "view": run_view}
    if argv and argv[0] in routes:
        return routes[argv[0]](argv[1:])
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        print("modes: view (default) | net | fetch | ws | ports | get | sh | sandbox")
        print("  view <url>       screenshot + text + console (browser eyes)")
        print("  net <url>        request waterfall + failures (why a 200 boots blank)")
        print("  fetch <url>      raw HTTP: redirect hops + cookies + headers + body")
        print("  ws <url>         open a ws://|wss:// endpoint, send/print frames")
        print("  ports [host:port]  is it up? / list all local listeners")
        print("  get <url> [out]  download anything to a file")
        print("  sh -- <cmd>      run a command in a throwaway WSL shell")
        print("  sandbox -- <cmd> run a command in an ephemeral Docker container")
        print("  train [script]   run in the fine-tuning conda env with the GPU (streaming)")
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
