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
 *   - GET        /events   — SSE stream of outbound messages
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

function readPpid(pid) {
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf-8')
    return parseInt(stat.split(' ')[3], 10)
  } catch { return null }
}

function readCmdline(pid) {
  try {
    return fs.readFileSync(`/proc/${pid}/cmdline`, 'utf-8').replace(/\0/g, ' ').trim()
  } catch { return '' }
}

function findClaudePid() {
  let pid = process.pid
  const visited = new Set()
  while (pid && pid > 1 && !visited.has(pid)) {
    visited.add(pid)
    const cmd = readCmdline(pid)
    if (cmd.includes('claude') && !cmd.includes('channel.mjs')) {
      return pid
    }
    pid = readPpid(pid)
  }
  return null
}

function readCmdlineArgs(pid) {
  try {
    return fs.readFileSync(`/proc/${pid}/cmdline`, 'utf-8').split('\0').filter(Boolean)
  } catch { return [] }
}

function parseNameFlag(pid) {
  const argv = readCmdlineArgs(pid)
  const idx = argv.indexOf('--name')
  if (idx >= 0 && argv[idx + 1]) return argv[idx + 1]
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

// --- Boot ---

const claudePid = findClaudePid()
const sessionInfo = claudePid ? readSessionInfo(claudePid) : null
const sessionId = sessionInfo?.sessionId
const initialName = claudePid ? parseNameFlag(claudePid) : null

// SESSION_NAMESPACE / SESSION_LABELS come from the parent process env
// (e.g. taskpilot exports them when spawning a managed agent). They are
// read here so the daemon can scope name lookups by namespace and run
// label-based selectors. Both are optional.
const initialNamespace = (process.env.SESSION_NAMESPACE || '').trim() || null
const initialLabels = (process.env.SESSION_LABELS || '')
  .split(',')
  .map(s => s.trim())
  .filter(Boolean)

if (!sessionId) {
  log(`could not find session ID (claude pid: ${claudePid})`)
}
if (initialName) {
  log(`initial name from claude --name: ${initialName}`)
}
if (initialNamespace) {
  log(`initial namespace from SESSION_NAMESPACE: ${initialNamespace}`)
}
if (initialLabels.length) {
  log(`initial labels from SESSION_LABELS: ${initialLabels.join(',')}`)
}

// --- Outbound: SSE listeners ---
const listeners = new Set()
function emitSSE(text) {
  const chunk = text.split('\n').map(l => `data: ${l}\n`).join('') + '\n'
  for (const emit of listeners) emit(chunk)
}

// --- Track inbound message senders for reply routing ---
const chatSenders = new Map()  // chat_id -> { from_name, from_id }

// --- MCP Server with channel capability ---
const serverName = 'session-bridge'

const mcp = new Server(
  { name: serverName, version: '0.1.0' },
  {
    capabilities: {
      experimental: { 'claude/channel': {} },
      tools: {},
    },
    instructions: [
      `You are part of a session mesh. Your session ID is ${sessionId || 'unknown'}.`,
      `Your session name is "${initialName || 'unset'}" (derived from claude --name at launch).`,
      initialNamespace ? `Your namespace is "${initialNamespace}". Address sessions in your namespace as "name.${initialNamespace}".` : 'You have no namespace assigned.',
      'Messages from other sessions arrive as <channel> notifications.',
      'The notification content includes who sent it (from_name, from_id).',
      '',
      'To respond to a message, use the reply tool with the chat_id from the notification.',
      'The reply is automatically routed back to the sender session.',
      '',
      'To initiate a conversation with another session, use the message tool.',
      'Use the sessions tool to see who is in the mesh — supports namespace and label-selector filters.',
      'Use the label tool to tag this session (or another) with key:value labels.',
      'Use the broadcast tool to message every session matching a selector (e.g. "kind:service").',
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
      description: 'List Claude sessions in the mesh. Optionally filter by namespace and/or label selector.',
      inputSchema: {
        type: 'object',
        properties: {
          namespace: { type: 'string', description: 'Restrict to a specific namespace (e.g. "taskpilot")' },
          selector: { type: 'string', description: 'Comma-separated key:value label selector (AND), e.g. "kind:service,tier:critical"' },
        },
      },
    },
    {
      name: 'label',
      description: 'Replace the labels on a session. Each label is a "key:value" string.',
      inputSchema: {
        type: 'object',
        properties: {
          session_id: { type: 'string', description: 'Target session UUID; defaults to this session' },
          labels: { type: 'array', items: { type: 'string' }, description: 'Full label set to apply (replaces existing)' },
        },
        required: ['labels'],
      },
    },
    {
      name: 'broadcast',
      description: 'Send a message to every session matching a label selector and/or namespace.',
      inputSchema: {
        type: 'object',
        properties: {
          text: { type: 'string', description: 'The message to send' },
          selector: { type: 'string', description: 'Comma-separated key:value label selector (AND)' },
          namespace: { type: 'string', description: 'Restrict to a namespace' },
        },
        required: ['text'],
      },
    },
  ],
}))

mcp.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: args } = req.params

  if (name === 'reply') {
    const sender = chatSenders.get(args.chat_id)
    // Also emit to SSE for external consumers
    emitSSE(`[${args.chat_id}] ${args.text}`)

    if (sender?.from_id) {
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
        return { content: [{ type: 'text', text: `reply routing error: ${err.message}. SSE-only reply emitted.` }] }
      }
    }

    // No sender info — SSE-only reply (external consumer)
    return { content: [{ type: 'text', text: 'reply sent (SSE only — no sender to route back to)' }] }
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
      try {
        const detail = JSON.parse(result.body).detail
        return { content: [{ type: 'text', text: `could not send: ${detail}` }] }
      } catch {
        return { content: [{ type: 'text', text: `could not send: ${result.error || result.body}` }] }
      }
    } catch (err) {
      return { content: [{ type: 'text', text: `message error: ${err.message}` }] }
    }
  }

  if (name === 'sessions') {
    const params = new URLSearchParams()
    if (args?.namespace) params.set('namespace', args.namespace)
    if (args?.selector) params.set('selector', args.selector)
    const qs = params.toString()
    const url = qs ? `${DAEMON_URL}/sessions?${qs}` : `${DAEMON_URL}/sessions`
    const result = await httpGet(url)
    if (!result.ok) {
      try {
        const detail = JSON.parse(result.body).detail
        return { content: [{ type: 'text', text: `could not list: ${detail}` }] }
      } catch {
        return { content: [{ type: 'text', text: `daemon unreachable: ${result.error || result.body}` }] }
      }
    }
    try {
      const sessions = JSON.parse(result.body)
      const lines = sessions.map(s => {
        const ch = s.channel_port ? `ch:${s.channel_port}` : 'no-channel'
        const ns = s.namespace ? `${s.name}.${s.namespace}` : s.name
        const labels = (s.labels && s.labels.length) ? ` [${s.labels.join(',')}]` : ''
        const me = s.session_id === sessionId ? ' (you)' : ''
        return `${ns} (${s.session_id.slice(0, 8)}) — ${s.state} — ${ch}${labels}${me}`
      })
      return { content: [{ type: 'text', text: lines.join('\n') || 'no sessions found' }] }
    } catch {
      return { content: [{ type: 'text', text: result.body }] }
    }
  }

  if (name === 'label') {
    const target = args.session_id || sessionId
    if (!target) {
      return { content: [{ type: 'text', text: 'no session_id available — pass one explicitly' }] }
    }
    const result = await httpPost(`${DAEMON_URL}/label`, { session_id: target, labels: args.labels })
    if (result.ok) {
      return { content: [{ type: 'text', text: `labels set on ${target.slice(0, 8)}: ${args.labels.join(', ') || '(empty)'}` }] }
    }
    try {
      const detail = JSON.parse(result.body).detail
      return { content: [{ type: 'text', text: `could not set labels: ${detail}` }] }
    } catch {
      return { content: [{ type: 'text', text: `could not set labels: ${result.error || result.body}` }] }
    }
  }

  if (name === 'broadcast') {
    const payload = { text: args.text, from_session: sessionId }
    if (args.selector) payload.selector = args.selector
    if (args.namespace) payload.namespace = args.namespace
    const result = await httpPost(`${DAEMON_URL}/broadcast`, payload)
    if (!result.ok) {
      try {
        const detail = JSON.parse(result.body).detail
        return { content: [{ type: 'text', text: `broadcast failed: ${detail}` }] }
      } catch {
        return { content: [{ type: 'text', text: `broadcast failed: ${result.error || result.body}` }] }
      }
    }
    try {
      const body = JSON.parse(result.body)
      const summary = `broadcast: ${body.sent}/${body.matched} delivered`
      const skipped = body.deliveries.filter(d => d.status !== 'sent')
      const detail = skipped.length
        ? '\n' + skipped.map(d => `  ${d.name} — ${d.status}${d.reason ? ' (' + d.reason + ')' : ''}`).join('\n')
        : ''
      return { content: [{ type: 'text', text: summary + detail }] }
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
let nextId = 1

const port = await findFreePort()

const httpServer = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`)

  if (req.method === 'GET' && url.pathname === '/events') {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
    })
    res.write(': connected\n\n')
    const emit = (chunk) => res.write(chunk)
    listeners.add(emit)
    req.on('close', () => listeners.delete(emit))
    return
  }

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

    const chat_id = String(nextId++)

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

    try {
      await mcp.notification({
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
    } catch (err) {
      log(`notification error: ${err}`)
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
  if (initialNamespace) payload.namespace = initialNamespace
  if (initialLabels.length) payload.labels = initialLabels
  return payload
}

async function registerWithDaemon(reason) {
  const result = await httpPost(`${DAEMON_URL}/register`, buildRegisterPayload())
  if (result.ok) {
    const parts = [
      `session: ${sessionId.slice(0, 8)}`,
      `port: ${port}`,
      initialName && `name: ${initialName}`,
      initialNamespace && `namespace: ${initialNamespace}`,
      initialLabels.length && `labels: ${initialLabels.join(',')}`,
    ].filter(Boolean)
    log(`${reason} (${parts.join(', ')})`)
    return true
  }
  log(`registration failed (${reason}): ${result.error || result.body}`)
  return false
}

// Heartbeat: ask the daemon if it still knows us. If it 404s, re-register.
// Catches the orphan-on-daemon-restart case — channel.mjs only registered at
// boot historically, so a daemon restart left every live session unreachable
// until each was manually relaunched.
const HEARTBEAT_INTERVAL_MS = 30_000

async function heartbeat() {
  if (!sessionId) return
  const result = await httpGet(`${DAEMON_URL}/sessions/${encodeURIComponent(sessionId)}`)
  if (result.ok) return
  // 404 → daemon never saw or has forgotten this session. Anything else
  // (network blip, 5xx) we let the next tick handle.
  if (result.error || !result.body) return
  try {
    const status = JSON.parse(result.body)
    if (status?.detail?.includes('not found')) {
      await registerWithDaemon('re-registered after daemon forget')
    }
  } catch {
    // body wasn't JSON — daemon may be transitioning. Try again next tick.
  }
}

httpServer.listen(port, '127.0.0.1', async () => {
  log(`channel listening on port ${port}`)

  if (sessionId) {
    await registerWithDaemon('registered with daemon')
    setInterval(heartbeat, HEARTBEAT_INTERVAL_MS)
  }
})
