#!/usr/bin/env node
/**
 * Session-proxy channel MCP — bidirectional communication for Claude sessions.
 *
 * On startup:
 *   1. Walks process tree to find parent claude PID
 *   2. Reads ~/.claude/sessions/{pid}.json for session UUID
 *   3. Picks a free port and starts HTTP channel server
 *   4. Registers with session-bridge daemon at :8910
 *
 * Provides:
 *   - MCP server with claude/channel capability (stdio transport)
 *   - HTTP POST  /         — push message into this Claude session (JSON: {text, from_name, from_id})
 *   - GET        /health   — health check
 */
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from '@modelcontextprotocol/sdk/types.js'
import http from 'node:http'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import net from 'node:net'
import { randomUUID } from 'node:crypto'
import { execFileSync } from 'node:child_process'

const DAEMON_URL = 'http://127.0.0.1:8910'
const SESSIONS_DIR = path.join(os.homedir(), '.claude', 'sessions')

const log = (msg) => process.stderr.write(`[session-bridge] ${msg}\n`)

// Prevent unhandled rejections from crashing the process
process.on('unhandledRejection', (err) => log(`unhandled rejection: ${err}`))
process.on('uncaughtException', (err) => log(`uncaught exception: ${err}`))

// Keep alive if stdio closes (Claude may restart MCP connections)
process.stdin.on('end', () => log('stdin closed'))
process.stdout.on('error', (err) => log(`stdout error: ${err}`))

// --- Find our session ID by walking up the process tree ---

// Parent pid + command line for a process, via a single `ps` call.
// Portable across Linux and macOS, unlike reading /proc (absent on macOS).
function readProc(pid) {
  try {
    const out = execFileSync('ps', ['-o', 'ppid=,command=', '-p', String(pid)], {
      encoding: 'utf-8', timeout: 5000,
    }).trim()
    if (!out) return { ppid: null, cmd: '' }
    const sep = out.indexOf(' ')
    const ppid = parseInt(out.slice(0, sep), 10)
    return { ppid: Number.isNaN(ppid) ? null : ppid, cmd: out.slice(sep + 1).trim() }
  } catch { return { ppid: null, cmd: '' } }
}

function findClaudePid() {
  let pid = process.pid
  const visited = new Set()
  while (pid && pid > 1 && !visited.has(pid)) {
    visited.add(pid)
    const { ppid, cmd } = readProc(pid)
    if (cmd.includes('claude') && !cmd.includes('channel.mjs')) {
      return pid
    }
    pid = ppid
  }
  return null
}

function readSessionInfo(claudePid) {
  const sessionFile = path.join(SESSIONS_DIR, `${claudePid}.json`)
  try {
    return JSON.parse(fs.readFileSync(sessionFile, 'utf-8'))
  } catch { return null }
}

// --- Pick a free port ---

function findFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer()
    srv.listen(0, '127.0.0.1', () => {
      const port = srv.address().port
      srv.close(() => resolve(port))
    })
    srv.on('error', reject)
  })
}

// --- HTTP helpers ---

function httpPost(url, jsonBody) {
  const body = JSON.stringify(jsonBody)
  return new Promise((resolve) => {
    const req = http.request(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
      timeout: 10000,
    }, (res) => {
      let data = ''
      res.on('data', (c) => data += c)
      res.on('end', () => resolve({ ok: res.statusCode === 200, status: res.statusCode, body: data }))
    })
    req.on('error', (e) => resolve({ ok: false, error: e.message }))
    req.end(body)
  })
}

function httpGet(url) {
  return new Promise((resolve) => {
    http.get(url, { timeout: 5000 }, (res) => {
      let data = ''
      res.on('data', (c) => data += c)
      res.on('end', () => resolve({ ok: res.statusCode === 200, body: data }))
    }).on('error', (e) => resolve({ ok: false, error: e.message }))
  })
}

// Format a daemon error response for an MCP tool reply. Tries the FastAPI
// `{detail: "..."}` body first, falls back to raw body or transport error.
function formatDaemonError(prefix, result) {
  try {
    const detail = JSON.parse(result.body).detail
    if (detail) return `${prefix}: ${detail}`
  } catch {}
  return `${prefix}: ${result.error || result.body || 'unknown error'}`
}

// --- Boot ---

const claudePid = findClaudePid()
const sessionInfo = claudePid ? readSessionInfo(claudePid) : null
const sessionId = sessionInfo?.sessionId

// SESSION_NAME comes from the parent process env (e.g. taskpilot exports it
// when spawning a managed agent, or the user sets SESSION_NAME=foo before
// `claude`). It lets the daemon register a human-friendly name. Optional —
// without it, the daemon derives a name from the cwd basename.
const initialName = (process.env.SESSION_NAME || '').trim() || null

if (!sessionId) {
  log(`could not find session ID (claude pid: ${claudePid})`)
}
if (initialName) {
  log(`initial name from SESSION_NAME: ${initialName}`)
}

// --- Track inbound message senders for reply routing ---
const chatSenders = new Map()  // chat_id -> { from_name, from_id }

// --- Probe daemon health so we can tell the operator if it's missing. ---
async function probeDaemonOnce() {
  return new Promise((resolve) => {
    const req = http.get(`${DAEMON_URL}/health`, { timeout: 1500 }, (res) => {
      res.resume()
      resolve(res.statusCode === 200)
    })
    req.on('error', () => resolve(false))
    req.on('timeout', () => { req.destroy(); resolve(false) })
  })
}
const daemonReachable = await probeDaemonOnce()
if (!daemonReachable) {
  log('daemon at 127.0.0.1:8910 is unreachable — run /session-bridge:setup to install it')
}

// --- MCP Server with channel capability ---
const serverName = 'session-bridge'

const setupHint = daemonReachable
  ? null
  : (
    'IMPORTANT: the session-bridge daemon at 127.0.0.1:8910 is NOT running on this machine. ' +
    'The session mesh is offline until it is installed. Tell the user to run `/session-bridge:setup` ' +
    '(or invoke the setup skill yourself if you have permission). Until then, the message, reply, ' +
    'and sessions tools below will not work.'
  )

const mcp = new Server(
  { name: serverName, version: '0.3.0' },
  {
    capabilities: {
      experimental: { 'claude/channel': {} },
      tools: {},
    },
    instructions: [
      `You are part of a session mesh. Your session ID is ${sessionId || 'unknown'}.`,
      initialName
        ? `Your session name is "${initialName}" (from the SESSION_NAME env var at launch).`
        : 'Your session name is unset. The daemon derives one from your cwd basename.',
      'Messages from other sessions arrive as <channel> notifications.',
      'The notification content includes who sent it (from_name, from_id).',
      '',
      'To respond to a message, use the reply tool with the chat_id from the notification.',
      'The reply is automatically routed back to the sender session.',
      '',
      'To initiate a conversation with another session, use the message tool.',
      'Use the sessions tool to see who is in the mesh.',
      ...(setupHint ? ['', setupHint] : []),
    ].join('\n'),
  },
)

// --- Tools ---
mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: 'reply',
      description: 'Reply to a message from another session. Routes back to the sender automatically.',
      inputSchema: {
        type: 'object',
        properties: {
          chat_id: { type: 'string', description: 'The chat_id from the inbound channel notification' },
          text: { type: 'string', description: 'The reply message' },
        },
        required: ['chat_id', 'text'],
      },
    },
    {
      name: 'message',
      description: 'Send a message to another Claude session. Use session name or UUID prefix.',
      inputSchema: {
        type: 'object',
        properties: {
          to: { type: 'string', description: 'Target session name or UUID prefix' },
          text: { type: 'string', description: 'The message to send' },
        },
        required: ['to', 'text'],
      },
    },
    {
      name: 'sessions',
      description: 'List Claude sessions in the mesh.',
      inputSchema: {
        type: 'object',
        properties: {},
      },
    },
  ],
}))

mcp.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: args } = req.params

  if (name === 'reply') {
    const sender = chatSenders.get(args.chat_id)
    if (!sender?.from_id) {
      return { content: [{ type: 'text', text: `unknown chat_id '${args.chat_id}' — no sender to route the reply to` }] }
    }
    // Route reply back through daemon to sender's channel
    try {
      const result = await httpPost(
        `${DAEMON_URL}/sessions/${encodeURIComponent(sender.from_id)}/message`,
        { text: args.text, from_session: sessionId },
      )
      if (result.ok) {
        return { content: [{ type: 'text', text: `reply sent to ${sender.from_name || sender.from_id}` }] }
      }
      return { content: [{ type: 'text', text: `reply could not be delivered to ${sender.from_name || sender.from_id}: ${result.body || result.error}. The sender may not have a channel.` }] }
    } catch (err) {
      return { content: [{ type: 'text', text: `reply routing error: ${err.message}` }] }
    }
  }

  if (name === 'message') {
    try {
      const result = await httpPost(
        `${DAEMON_URL}/sessions/${encodeURIComponent(args.to)}/message`,
        { text: args.text, from_session: sessionId },
      )
      if (result.ok) {
        return { content: [{ type: 'text', text: `message sent to ${args.to}` }] }
      }
      return { content: [{ type: 'text', text: formatDaemonError('could not send', result) }] }
    } catch (err) {
      return { content: [{ type: 'text', text: `message error: ${err.message}` }] }
    }
  }

  if (name === 'sessions') {
    const result = await httpGet(`${DAEMON_URL}/sessions`)
    if (!result.ok) {
      return { content: [{ type: 'text', text: formatDaemonError('could not list', result) }] }
    }
    try {
      const sessions = JSON.parse(result.body)
      const lines = sessions.map(s => {
        const ch = s.channel_port ? `ch:${s.channel_port}` : 'no-channel'
        const me = s.session_id === sessionId ? ' (you)' : ''
        return `${s.name} (${s.session_id.slice(0, 8)}) — ${s.state} — ${ch}${me}`
      })
      return { content: [{ type: 'text', text: lines.join('\n') || 'no sessions found' }] }
    } catch {
      return { content: [{ type: 'text', text: result.body }] }
    }
  }

  throw new Error(`unknown tool: ${name}`)
})

// --- Handle unexpected notifications from Claude ---
mcp.fallbackNotificationHandler = async (notification) => {
  log(`unhandled notification: ${JSON.stringify(notification)}`)
}

// --- Connect to Claude Code over stdio ---
mcp.onerror = (err) => {
  log(`mcp error: ${String(err?.message || err).slice(0, 200)}`)
}
await mcp.connect(new StdioServerTransport())

// --- HTTP channel server ---

const port = await findFreePort()

const httpServer = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`)

  if (req.method === 'GET' && url.pathname === '/health') {
    res.writeHead(200)
    res.end('ok')
    return
  }

  if (req.method === 'POST') {
    const chunks = []
    for await (const chunk of req) chunks.push(chunk)
    const raw = Buffer.concat(chunks).toString()

    // Parse JSON envelope from daemon, fall back to plain text
    let text, fromName, fromId
    try {
      const msg = JSON.parse(raw)
      text = msg.text || raw
      fromName = msg.from_name || null
      fromId = msg.from_id || null
    } catch {
      text = raw
    }

    const chat_id = randomUUID()

    // Store sender info so reply can route back
    if (fromId) {
      chatSenders.set(chat_id, { from_name: fromName, from_id: fromId })
    }

    // Build content with sender attribution
    let content = text
    if (fromName) {
      content = `[from ${fromName}] ${text}`
    } else if (fromId) {
      content = `[from ${fromId.slice(0, 8)}] ${text}`
    }

    // Bound the notification wait. Without this, a wedged claude stdio (the
    // pipe backs up when the harness stops draining MCP traffic) hangs this
    // POST handler indefinitely, which cascades: the daemon's 5s forward
    // timeout fires, sender sees "channel error", and the failure is silent
    // from the user's POV. 3s is generous for healthy stdio (sub-ms typical)
    // and stays under the daemon's 5s ceiling so we still return 200 in time.
    let notifyTimer
    const notificationPromise = mcp.notification({
      method: 'notifications/claude/channel',
      params: {
        content,
        meta: {
          chat_id,
          path: url.pathname,
          ...(fromName && { from_name: fromName }),
          ...(fromId && { from_id: fromId }),
        },
      },
    })
    const timeoutPromise = new Promise((_, reject) => {
      notifyTimer = setTimeout(
        () => reject(new Error('notification timeout (3s) — claude stdio wedged')),
        3000,
      )
    })
    try {
      await Promise.race([notificationPromise, timeoutPromise])
    } catch (err) {
      log(`notification error: ${err.message || err}`)
    } finally {
      clearTimeout(notifyTimer)
    }
    res.writeHead(200)
    res.end(`ok (chat_id: ${chat_id})`)
    return
  }

  res.writeHead(404)
  res.end('not found')
})

// Build the /register payload once; reused at boot and on every heartbeat
// re-registration when the daemon has lost our entry (restart, crash, etc).
function buildRegisterPayload() {
  const payload = { session_id: sessionId, pid: claudePid, channel_port: port }
  if (initialName) payload.name = initialName
  return payload
}

async function registerWithDaemon(reason) {
  const result = await httpPost(`${DAEMON_URL}/register`, buildRegisterPayload())
  if (result.ok) {
    const parts = [
      `session: ${sessionId.slice(0, 8)}`,
      `port: ${port}`,
      initialName && `name: ${initialName}`,
    ].filter(Boolean)
    log(`${reason} (${parts.join(', ')})`)
    return true
  }
  log(`registration failed (${reason}): ${result.error || result.body}`)
  return false
}

// Heartbeat: ask the daemon if it still knows us. Re-register when:
//   - daemon 404s the session (never saw us, or fully forgot)
//   - daemon 200s but channel_port is null (rediscovered the session from
//     ~/.claude/sessions/{pid}.json after a bounce, but lost the port —
//     the registration step is what carries the port, and the daemon does
//     not persist it)
//   - daemon 200s with a DIFFERENT channel_port than ours: another process
//     resolved to the same session_id and overwrote our registration. Last
//     writer wins at /register today; this heartbeat reclaims the row so
//     mis-routed traffic stops landing on the squatter. Symptom: senders
//     POST messages, daemon forwards, the squatter swallows them, sender
//     gets no reply. Caused by stray `node -e import('./channel.mjs')`
//     instances or any other channel.mjs that walks back to the same
//     parent claude when discovering its session.
// Anything else (network blip, 5xx, malformed body) we let the next tick handle.
const HEARTBEAT_INTERVAL_MS = 30_000

async function heartbeat() {
  if (!sessionId) return
  const result = await httpGet(`${DAEMON_URL}/sessions/${encodeURIComponent(sessionId)}`)
  if (result.error) return
  if (!result.body) return
  let parsed
  try {
    parsed = JSON.parse(result.body)
  } catch {
    return
  }
  if (result.ok) {
    if (parsed?.channel_port == null) {
      await registerWithDaemon('re-registered after daemon rediscovered without channel')
    } else if (parsed.channel_port !== port) {
      await registerWithDaemon(`re-registered after port mismatch (daemon=${parsed.channel_port}, mine=${port})`)
    }
    return
  }
  if (parsed?.detail?.includes('not found')) {
    await registerWithDaemon('re-registered after daemon forget')
  }
}

httpServer.listen(port, '127.0.0.1', async () => {
  log(`channel listening on port ${port}`)

  if (sessionId) {
    await registerWithDaemon('registered with daemon')
    setInterval(heartbeat, HEARTBEAT_INTERVAL_MS)
  }
})
