#!/usr/bin/env node
/*
 * peek.mjs — the Node engine. Same job as peek.py, zero dependencies.
 *
 * Node 24 ships a global WebSocket and fetch, so this drives a throwaway
 * headless Chrome over the DevTools Protocol with NOTHING installed — no
 * Playwright, no puppeteer, no npm. It's the JS-runtime sibling of peek.py so
 * an agent already in a node shell never has to shell out to python (and vice
 * versa). Both write to the same C:\peek\_shots and _text.
 *
 * Agent browser panes block localhost / LAN / private-IP URLs by policy. This
 * doesn't. Your machine, your browser, your call.
 *
 *   node C:/peek/peek.mjs http://127.0.0.1:3080/
 *   node C:/peek/peek.mjs https://192.168.1.112:8443/ --full
 *   node C:/peek/peek.mjs net http://127.0.0.1:3080/     # request waterfall (why a 200 boots blank)
 *   node C:/peek/peek.mjs fetch http://127.0.0.1:3080/   # raw HTTP: every redirect hop + cookies
 *   node C:/peek/peek.mjs http://127.0.0.1:3080/ --js "document.title"
 *
 * PEEK_BROWSER=<path to chrome.exe> overrides browser discovery.
 */
import { spawn, spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import net from 'node:net'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = fileURLToPath(new URL('.', import.meta.url))
const SHOTS = join(HERE, '_shots')
const TEXTS = join(HERE, '_text')
const die = (m) => { console.error('peek: ' + m); process.exit(1) }
const stamp = () => new Date().toTimeString().slice(0, 8).replace(/:/g, '')
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

function findBrowser() {
  const cands = [
    process.env.PEEK_BROWSER,
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    join(process.env.LOCALAPPDATA || '', 'Google\\Chrome\\Application\\chrome.exe'),
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  ]
  for (const c of cands) if (c && existsSync(c)) return c
  die('no Chrome or Edge found (set PEEK_BROWSER=path-to-chrome.exe)')
}

function launch(url, headful) {
  const prof = join(tmpdir(), 'peek-' + Date.now() + '-' + Math.floor(Math.random() * 1e6))
  mkdirSync(prof, { recursive: true })
  const args = [
    '--remote-debugging-port=0', `--user-data-dir=${prof}`,
    '--no-first-run', '--no-default-browser-check',
    '--disable-features=Translate,MediaRouter,OptimizationHints',
    '--window-size=1440,900',
    // throwaway profile => trust private CAs / self-signed dev certs
    '--ignore-certificate-errors',
    url || 'about:blank',
  ]
  if (!headful) args.unshift('--headless=new')
  const proc = spawn(findBrowser(), args, { stdio: 'ignore', detached: false })
  const portfile = join(prof, 'DevToolsActivePort')
  return { proc, prof, portfile }
}

async function waitPort(portfile, proc) {
  for (let i = 0; i < 200; i++) {
    if (existsSync(portfile)) {
      const lines = readFileSync(portfile, 'utf8').split('\n')
      if (lines[0] && /^\d+$/.test(lines[0].trim())) return parseInt(lines[0].trim(), 10)
    }
    if (proc.exitCode !== null) die('browser exited before exposing DevTools (bad path?)')
    await sleep(100)
  }
  die('browser never exposed its DevTools port within 20s')
}

async function pageWs(port) {
  for (let i = 0; i < 60; i++) {
    try {
      const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json()
      const page = targets.find((t) => t.type === 'page' && t.webSocketDebuggerUrl)
      if (page) return page.webSocketDebuggerUrl
    } catch { /* browser still coming up */ }
    await sleep(100)
  }
  die('browser exposed no page target')
}

class CDP {
  constructor(ws) {
    this.ws = ws
    this.id = 0
    this.pending = new Map()
    this.events = []
    ws.addEventListener('message', (ev) => {
      let m
      try { m = JSON.parse(ev.data) } catch { return }
      if (m.id && this.pending.has(m.id)) {
        const { res, rej } = this.pending.get(m.id)
        this.pending.delete(m.id)
        m.error ? rej(new Error(m.error.message || 'CDP error')) : res(m.result || {})
      } else if (m.method) {
        this.events.push(m)
      }
    })
  }

  static async connect(wsUrl) {
    const ws = new WebSocket(wsUrl)
    await new Promise((res, rej) => {
      ws.addEventListener('open', res, { once: true })
      ws.addEventListener('error', () => rej(new Error('ws connect failed')), { once: true })
    })
    return new CDP(ws)
  }

  call(method, params = {}, timeout = 60000) {
    const id = ++this.id
    return new Promise((res, rej) => {
      this.pending.set(id, { res, rej })
      this.ws.send(JSON.stringify({ id, method, params }))
      setTimeout(() => {
        if (this.pending.has(id)) { this.pending.delete(id); rej(new Error(method + ' timed out')) }
      }, timeout)
    })
  }

  async eval(expr) {
    const r = await this.call('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })
    return r.result?.value
  }

  consoleLines() {
    const out = []
    for (const e of this.events) {
      const p = e.params || {}
      if (e.method === 'Runtime.consoleAPICalled' && (p.type === 'error' || p.type === 'warning')) {
        out.push(`[${p.type}] ` + (p.args || []).map((a) => a.value ?? a.description ?? '').join(' '))
      } else if (e.method === 'Runtime.exceptionThrown') {
        const d = p.exceptionDetails || {}
        out.push('[exception] ' + String(d.exception?.description || d.text || 'exception').split('\n')[0])
      } else if (e.method === 'Log.entryAdded' && p.entry?.level === 'error') {
        out.push('[log] ' + (p.entry.text || ''))
      }
    }
    return out
  }

  close() { try { this.ws.close() } catch { /* ignore */ } }
}

function spill(kind, text, maxChars) {
  if (text.length <= maxChars) return null
  mkdirSync(TEXTS, { recursive: true })
  const p = join(TEXTS, `${kind}_${stamp()}.txt`)
  writeFileSync(p, text, 'utf8')
  return p
}

async function report(cdp, o, url) {
  let title = '', where = url
  try { title = (await cdp.eval("document.title || ''")) || ''; where = (await cdp.eval('location.href')) || url } catch { /* ignore */ }

  if (!o.text) {
    try {
      const shot = await cdp.call('Page.captureScreenshot', { format: 'png', captureBeyondViewport: !!o.full })
      const png = Buffer.from(shot.data, 'base64')
      mkdirSync(SHOTS, { recursive: true })
      const out = join(SHOTS, `peek_${stamp()}.png`)
      writeFileSync(out, png)
      const w = png.length > 24 ? png.readUInt32BE(16) : 0
      const h = png.length > 24 ? png.readUInt32BE(20) : 0
      console.log(`page:  ${title}  --  ${where}`)
      console.log(`shot:  ${out}  (${w}x${h}, ${Math.floor(png.length / 1024)} KB)`)
    } catch (e) {
      console.log(`page:  ${title}  --  ${where}`)
      console.log(`shot:  (failed: ${e.message})`)
    }
  } else {
    console.log(`page:  ${title}  --  ${where}`)
  }

  if (o.js) {
    let code = o.js
    if (code.startsWith('@')) code = readFileSync(code.slice(1), 'utf8')
    try {
      const val = await cdp.eval(code)
      console.log('\njs:')
      console.log(typeof val === 'object' && val !== null ? JSON.stringify(val, null, 2) : (val ?? '(no value)'))
    } catch (e) { console.log(`\njs:  (error: ${e.message})`) }
  }

  if (!o.shot) {
    let txt = ''
    try { txt = (await cdp.eval("document.body ? document.body.innerText : '(no body)'")) || '' } catch { /* ignore */ }
    const s = txt.trim()
    const p = spill('peek', txt, o.maxChars)
    console.log('\ntext:')
    if (p) { console.log(s.slice(0, 1500)); console.log(`\n[... ${txt.length.toLocaleString()} chars total -- full: ${p}]`) }
    else console.log(s || '(empty page)')
  }

  const errs = cdp.consoleLines()
  if (errs.length) {
    console.log('\nconsole (errors/exceptions):')
    for (const l of errs.slice(0, 20)) console.log('  ' + l.slice(0, 300))
  }
}

async function runView(url, o) {
  const { proc, portfile } = launch(url, o.headful)
  const cdp = await CDP.connect(await pageWs(await waitPort(portfile, proc)))
  try {
    for (const m of ['Page.enable', 'Runtime.enable', 'Log.enable']) { try { await cdp.call(m) } catch { /* ignore */ } }
    const end = Date.now() + o.wait * 1000
    while (Date.now() < end) { try { if ((await cdp.eval('document.readyState')) === 'complete') break } catch { /* ignore */ } await sleep(300) }
    await sleep(o.settle * 1000)
    await report(cdp, o, url)
  } finally {
    if (o.keep) { console.log(`\n[kept alive -- pid ${proc.pid}. kill: taskkill /PID ${proc.pid} /F ]`) }
    else { cdp.close(); try { proc.kill() } catch { /* ignore */ } }
  }
}

async function runNet(url, o) {
  const { proc, portfile } = launch('about:blank', o.headful)
  const cdp = await CDP.connect(await pageWs(await waitPort(portfile, proc)))
  try {
    for (const m of ['Network.enable', 'Page.enable', 'Runtime.enable', 'Log.enable']) { try { await cdp.call(m) } catch { /* ignore */ } }
    await cdp.call('Page.navigate', { url })
    const end = Date.now() + o.wait * 1000
    while (Date.now() < end) { try { if ((await cdp.eval('document.readyState')) === 'complete') break } catch { /* ignore */ } await sleep(300) }
    await sleep(o.settle * 1000)
    let title = ''
    try { title = (await cdp.eval("document.title || ''")) || '' } catch { /* ignore */ }

    const reqs = new Map(), order = []
    for (const e of cdp.events) {
      const p = e.params || {}, id = p.requestId
      if (e.method === 'Network.requestWillBeSent' && id) {
        reqs.set(id, { method: p.request?.method || '?', url: p.request?.url || '', type: p.type || '', status: null, err: null }); order.push(id)
      } else if (e.method === 'Network.responseReceived' && reqs.has(id)) reqs.get(id).status = p.response?.status
      else if (e.method === 'Network.loadingFailed' && reqs.has(id)) reqs.get(id).err = p.errorText || 'failed'
    }
    const rows = order.map((id) => reqs.get(id))
    const bad = rows.filter((r) => r.err || (r.status && r.status >= 400) || r.status === null)
    console.log(`page:  ${JSON.stringify(title)}  --  ${rows.length} requests, ${bad.length} problem(s)`)
    for (const r of rows) {
      const core = ['Document', 'Script', 'XHR', 'Fetch'].includes(r.type)
      const problem = r.err || (r.status && r.status >= 400) || r.status === null
      if (!(o.all || core || problem)) continue
      const st = r.err ? 'ERR' : (r.status ? String(r.status) : '...')
      const flag = r.err ? `  <== ${r.err}` : (r.status === null ? '  <== HANGING (no response)' : (r.status >= 400 ? `  <== ${r.status}` : ''))
      console.log(`  ${st.padStart(4)} ${r.method.padEnd(4)} ${r.url.slice(0, 118)}${flag}`)
    }
    const errs = cdp.consoleLines()
    if (errs.length) { console.log('\nconsole (errors/exceptions):'); for (const l of errs.slice(0, 20)) console.log('  ' + l.slice(0, 300)) }
  } finally {
    cdp.close(); try { proc.kill() } catch { /* ignore */ }
  }
}

async function runFetch(url, o) {
  // Raw HTTP, no browser: follow redirects manually so every hop is visible,
  // carrying cookies forward the way a browser would (native fetch drops them).
  const jar = []
  let cur = url, hop = 0, status, headers, body
  const undici = { rejectUnauthorized: false } // best-effort; NODE_TLS below is the real switch
  process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0' // private CAs / self-signed: just work
  void undici
  const t0 = Date.now()
  while (hop < 15) {
    const cookieHdr = jar.length ? { cookie: jar.map((c) => c.split(';')[0]).join('; ') } : {}
    const res = await fetch(cur, {
      method: o.method || (o.data ? 'POST' : 'GET'),
      body: o.data || undefined,
      headers: { ...cookieHdr, ...Object.fromEntries(o.header.map((h) => { const i = h.indexOf(':'); return [h.slice(0, i).trim(), h.slice(i + 1).trim()] })) },
      redirect: 'manual',
    })
    const sc = res.headers.getSetCookie ? res.headers.getSetCookie() : []
    for (const c of sc) jar.push(c)
    status = res.status; headers = res.headers
    if (status >= 300 && status < 400 && res.headers.get('location')) {
      const to = new URL(res.headers.get('location'), cur).href
      console.log(`  ${status}  ${cur}\n        -> ${to}` + (sc.length ? `   [Set-Cookie: ${sc[0].split(';')[0]}]` : ''))
      cur = to; hop++; continue
    }
    body = await res.text()
    break
  }
  console.log(`final: ${status}  ${cur}   (${Date.now() - t0} ms)`)
  for (const k of ['content-type', 'content-length', 'location', 'server']) if (headers.get(k)) console.log(`  ${k}: ${headers.get(k)}`)
  if (jar.length) console.log(`  cookies set: ${jar.map((c) => c.split('=')[0]).join(', ')}`)
  if (!o.head) {
    const p = spill('fetch', body || '', o.maxChars || 8000)
    console.log('\nbody:')
    if (p) { console.log((body || '').slice(0, o.maxChars || 8000)); console.log(`\n[... ${body.length.toLocaleString()} chars total -- full: ${p}]`) }
    else console.log(body || '(empty body)')
  }
}

// ================= WSL verbs (env / sh / sandbox / train) =================
const B64 = (s) => Buffer.from(s).toString('base64')
const DISTRO = 'Ubuntu-24.04'

function wslCap(script, distro = DISTRO, timeout = 40000) {
  try {
    const r = spawnSync('wsl', ['-d', distro, '-u', 'root', '--exec', 'bash', '-lc', script],
      { encoding: 'utf8', timeout, maxBuffer: 16 * 1024 * 1024 })
    return (r.stdout || '').trim()
  } catch { return '' }
}

function streamWsl(inner, distro = DISTRO, timeoutS = 0) {
  return new Promise((res) => {
    const p = spawn('wsl', ['-d', distro, '-u', 'root', '--exec', 'bash', '-lc', inner], { stdio: 'inherit' })
    if (timeoutS) setTimeout(() => { try { p.kill() } catch { /* gone */ } }, timeoutS * 1000)
    p.on('exit', (code) => res(code ?? -1))
  })
}

function splitArgs(raw, valueFlags) {
  const flags = {}, rest = []
  for (let i = 0; i < raw.length; i++) {
    const a = raw[i]
    if (a === '--') continue
    if (a.startsWith('--') && valueFlags.includes(a.slice(2)) && raw[i + 1] !== undefined) { flags[a.slice(2)] = raw[++i]; continue }
    rest.push(a)
  }
  return { flags, rest }
}

const shq = (s) => "'" + String(s).replace(/'/g, "'\\''") + "'"
const toWslPath = (p) => (/^[A-Za-z]:/.test(p) || p.includes('\\'))
  ? (() => { const q = p.replace(/\\/g, '/'); const [d, ...r] = q.split(':'); return `/mnt/${d.toLowerCase()}${r.join(':')}` })()
  : p

// The one rig probe, shared with peek.py: tagged lines a caller parses.
const RIG_BASH =
  "echo SYSPY=$(python3 --version 2>&1 | awk '{print $2}'); seen=; " +
  "for MF in /root/miniforge3 /root/miniconda3 /root/anaconda3 /root/mambaforge $HOME/miniforge3 $HOME/miniconda3 $HOME/anaconda3; do " +
  "[ -x \"$MF/bin/conda\" ] || continue; rp=$(readlink -f \"$MF\"); case \" $seen \" in *\" $rp \"*) continue;; esac; seen=\"$seen $rp\"; echo CONDA=$rp; " +
  "for py in $MF/bin/python $MF/envs/*/bin/python; do [ -x \"$py\" ] || continue; " +
  "en=$(basename $(dirname $(dirname \"$py\"))); [ \"$en\" = \"$(basename $MF)\" ] && en=base; " +
  "t=$(\"$py\" -c 'import torch;print(torch.__version__, torch.cuda.is_available())' 2>/dev/null); [ -n \"$t\" ] || continue; " +
  "tl=; for x in unsloth axolotl trl accelerate torchrun deepspeed llamafactory-cli vllm; do [ -x \"$(dirname $py)/$x\" ] && tl=\"$tl $x\"; done; " +
  "echo \"ENV=$en|$py|$t|$tl\"; done; done; " +
  "[ -d /root/models ] && echo \"MODELS=$(du -sh /root/models 2>/dev/null|cut -f1)|$(ls /root/models 2>/dev/null|tr '\\n' ' ')\"; " +
  "ls -d /root/llama.cpp* 2>/dev/null | head -1 | sed 's/^/LLAMACPP=/'"

const FT_PROBE = (force) => (force ? `[ -x ${shq(force)} ] && { echo ${shq(force)}; exit 0; }; ` : '') +
  "for MF in /root/miniforge3 /root/miniconda3 /root/anaconda3 /root/mambaforge $HOME/miniforge3 $HOME/miniconda3 $HOME/anaconda3; do [ -x \"$MF/bin/conda\" ] || continue; " +
  "for py in $MF/envs/*/bin/python $MF/bin/python; do [ -x \"$py\" ] || continue; " +
  "for x in unsloth axolotl trl; do [ -x \"$(dirname $py)/$x\" ] && { echo \"$py\"; exit 0; }; done; done; done"

function winCap(cmd, args, timeout = 8000) {
  try { const r = spawnSync(cmd, args, { encoding: 'utf8', timeout }); return (r.stdout || '').trim() } catch { return '' }
}

async function runEnv() {
  console.log('=== this machine — what an agent can actually do here ===\n')
  console.log('host:')
  console.log(`  Windows, node ${process.version}, python ${(winCap('python', ['--version']).split(' ')[1]) || '?'}`)
  console.log(`  peek: ${HERE}  (view net fetch ws ports get sh sandbox train env)`)
  const gpu = winCap('nvidia-smi', ['--query-gpu=name,memory.total,memory.used,memory.free', '--format=csv,noheader'])
  console.log('\nGPU:\n  ' + (gpu ? gpu.replace(/\n/g, '\n  ') : '(no nvidia-smi / no GPU)'))

  console.log('\nWSL (your Linux — `peek sh -- <cmd>` runs here, isolated from Windows):')
  const wl = winCap('wsl', ['-l', '-v']).replace(/\0/g, '')
  wl.split('\n').map((s) => s.trim()).filter(Boolean).slice(1).forEach((l) => console.log('  ' + l.split(/\s+/).join(' ')))
  let printedRig = false
  for (const line of wslCap(RIG_BASH).split('\n')) {
    if (line.startsWith('SYSPY=')) console.log(`  system python ${line.slice(6)} (no torch here — the rig is in conda, below)`)
    else if (line.startsWith('CONDA=')) {
      const root = line.slice(6)
      const names = wslCap(`ls ${root}/envs 2>/dev/null | tr '\\n' ' '`)
      console.log(`  conda: ${root}  (base ${names})`.trimEnd())
    } else if (line.startsWith('ENV=')) {
      if (!printedRig) { console.log('  training rig:'); printedRig = true }
      const [en, , t, tools] = line.slice(4).split('|')
      const [ver, cuda] = (t || '').split(' ')
      const ft = /unsloth|axolotl|trl/.test(tools || '') ? '   <-- fine-tuning' : ''
      console.log(`    ${(en || '').padEnd(6)} torch ${(ver || '').padEnd(14)} ${cuda === 'True' ? 'cuda✓' : 'cuda✗'}  [${(tools || '').trim()}]${ft}`)
    } else if (line.startsWith('MODELS=')) {
      const [sz, lst] = line.slice(7).split('|')
      console.log(`  models: /root/models  ${sz}  (${(lst || '').trim()})`)
    } else if (line.startsWith('LLAMACPP=')) console.log(`  llama.cpp: ${line.slice(9)}  (HF->GGUF + LoRA->GGUF converters)`)
  }

  const dv = wslCap("docker version --format 'docker={{.Server.Version}}' 2>/dev/null; echo images=$(docker images -q 2>/dev/null | wc -l)")
  console.log('\nDocker (in WSL — `peek sandbox -- <cmd>` = ephemeral --rm container):\n  ' + (dv ? dv.replace(/\n/g, '\n  ') : '(docker not reachable in WSL)'))

  console.log('\nlocal services up right now (`peek ports` for the full list):')
  const known = { 3080: 'DSH harness web', 8097: 'voice facade (ASR)', 8197: 'voice engine', 8092: 'llama embeddings', 2019: 'caddy admin', 5432: 'postgres', 6379: 'redis', 11434: 'ollama', 8080: 'http-alt' }
  let anyUp = false
  for (const port of Object.keys(known).map(Number).sort((a, b) => a - b)) {
    const up = await new Promise((res) => {
      const s = net.connect({ host: '127.0.0.1', port, timeout: 400 })
      s.on('connect', () => { s.destroy(); res(true) })
      s.on('error', () => res(false))
      s.on('timeout', () => { s.destroy(); res(false) })
    })
    if (up) { console.log(`  :${String(port).padEnd(6)} ${known[port]}`); anyUp = true }
  }
  if (!anyUp) console.log('  (none of the usual suspects; run `peek ports` to see everything)')
  console.log('\nno blocker is real here — if a harness refuses a URL/port/socket/shell, route it through peek.')
}

async function runSh(raw) {
  const { flags, rest } = splitArgs(raw, ['distro', 'timeout'])
  const cmd = rest.join(' ').replace(/^-+\s*/, '').trim()
  if (!cmd) die('give a command:  node C:/peek/peek.mjs sh -- uname -a')
  const code = await streamWsl(`cd $(mktemp -d) && echo ${B64(cmd)} | base64 -d | bash`, flags.distro || DISTRO, parseInt(flags.timeout || '120', 10))
  console.log(`[exit ${code}]`)
}

async function runSandbox(raw) {
  const { flags, rest } = splitArgs(raw, ['image', 'mount', 'distro', 'timeout'])
  const cmd = rest.join(' ').replace(/^-+\s*/, '').trim()
  if (!cmd) die('give a command:  node C:/peek/peek.mjs sandbox -- python3 -c "print(2**100)"')
  const image = flags.image || 'alpine'
  let mount = ''
  if (flags.mount) { const wp = flags.mount.replace(/\\/g, '/'); const [d, ...r] = wp.split(':'); if (r.length) mount = `-v /mnt/${d.toLowerCase()}${r.join(':')}:/work -w /work ` }
  console.log(`sandbox: ${image} (ephemeral, --rm)${flags.mount ? '  mount ' + flags.mount + ' -> /work' : ''}`)
  const code = await streamWsl(`echo ${B64(cmd)} | base64 -d | docker run --rm -i ${mount}${image} sh`, flags.distro || DISTRO, parseInt(flags.timeout || '300', 10))
  console.log(`[exit ${code}]`)
}

async function runTrain(raw) {
  const { flags, rest } = splitArgs(raw, ['env', 'distro', 'timeout'])
  const distro = flags.distro || DISTRO
  const ftpy = wslCap(FT_PROBE(flags.env ? `/root/miniforge3/envs/${flags.env}/bin/python` : ''), distro)
  if (!ftpy) die('no fine-tuning env found (need a conda env with unsloth/axolotl/trl). Run `peek env`.')
  const envbin = ftpy.replace(/\/[^/]*$/, '')
  const envname = envbin.replace(/\/bin$/, '').replace(/^.*\//, '')
  const cmd = rest[0] === '--' ? rest.slice(1) : rest
  if (!cmd.length) {
    console.log(`fine-tuning env: ${envname}   (${ftpy})`)
    const models = wslCap("[ -d /root/models ] && { du -sh /root/models 2>/dev/null|cut -f1; ls /root/models 2>/dev/null|tr '\\n' ' '; }")
    if (models) { const m = models.split('\n'); console.log(`models: /root/models  ${m[0]}  (${(m[1] || '').trim()})`) }
    console.log('usage:\n  node C:/peek/peek.mjs train /root/ft/run.py --epochs 3\n  node C:/peek/peek.mjs train -- accelerate launch /root/ft/run.py')
    return
  }
  let body, header
  if (cmd[0].toLowerCase().endsWith('.py')) {
    const script = toWslPath(cmd[0]), restArgs = cmd.slice(1)
    const workdir = script.replace(/\/[^/]*$/, '') || '.'
    body = `cd ${shq(workdir)}\nexport PATH=${shq(envbin)}:"$PATH"\nexec ${shq(ftpy)} ${shq(script)} ${restArgs.map(shq).join(' ')}`
    header = `train: [${envname}] python ${script} ${restArgs.join(' ')}   (cwd ${workdir})`
  } else {
    body = `export PATH=${shq(envbin)}:"$PATH"\nexec ${cmd.map(shq).join(' ')}`
    header = `train: [${envname}] ${cmd.join(' ')}`
  }
  console.log(header)
  const code = await streamWsl(`echo ${B64(body)} | base64 -d | bash`, distro, parseInt(flags.timeout || '0', 10))
  console.log(`[exit ${code}]`)
}

// ---- arg parsing: `peek.mjs [mode] <url> [flags]`, default mode = view ----
const raw = process.argv.slice(2)
let mode = 'view'
if (['view', 'net', 'fetch', 'env', 'sh', 'sandbox', 'train'].includes(raw[0])) mode = raw.shift()

// WSL/local verbs need no URL — handle them first.
try {
  if (mode === 'env') { await runEnv(); process.exit(0) }
  if (mode === 'sh') { await runSh(raw); process.exit(0) }
  if (mode === 'sandbox') { await runSandbox(raw); process.exit(0) }
  if (mode === 'train') { await runTrain(raw); process.exit(0) }
} catch (e) { die(e.message || String(e)) }

const opt = (name, def) => { const i = raw.indexOf('--' + name); return i >= 0 && raw[i + 1] && !raw[i + 1].startsWith('--') ? raw[i + 1] : def }
const has = (name) => raw.includes('--' + name)
const url = raw.find((a) => !a.startsWith('--') && !['view', 'net', 'fetch'].includes(a))
if (!url) die('give a URL:  node C:/peek/peek.mjs <url>   (or: net|fetch|env|sh|sandbox|train ...)')

const o = {
  headful: has('headful'), full: has('full'), keep: has('keep'), text: has('text'), shot: has('shot'),
  all: has('all'), head: has('head'), js: opt('js', null), method: opt('method', opt('X', null)),
  data: opt('data', null),
  header: raw.reduce((acc, a, i) => (['--header', '-H'].includes(a) && raw[i + 1] ? [...acc, raw[i + 1]] : acc), []),
  wait: parseFloat(opt('wait', mode === 'net' ? '15' : '12')),
  settle: parseFloat(opt('settle', mode === 'net' ? '3' : '2')),
  maxChars: parseInt(opt('max-chars', mode === 'fetch' ? '8000' : '100000'), 10),
}

try {
  if (mode === 'fetch') await runFetch(url, o)
  else if (mode === 'net') await runNet(url, o)
  else await runView(url, o)
} catch (e) {
  die(e.message || String(e))
}
process.exit(0)
