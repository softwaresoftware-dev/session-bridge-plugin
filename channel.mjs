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

if (!sessionId) {
  log(`could not find session ID (claude pid: ${claudePid})`)
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
      'Messages from other sessions arrive as <channel> notifications.',
      'The notification content includes who sent it (from_name, from_id).',
      '',
      'IMPORTANT: After your first interaction with the user, use the set_name tool to give',
      'this session a short, descriptive name based on what the user is working on.',
      'Examples: "beats-dj", "gmail-filters", "lawn-care", "session-bridge-dev".',
      'Keep it to 1-3 words, lowercase, hyphenated. This helps other sessions find you.',
      '',
      'To respond to a message, use the reply tool with the chat_id from the notification.',
      'The reply is automatically routed back to the sender session.',
      '',
      'To initiate a conversation with another session, use the message tool.',
      'Use the sessions tool to see who is in the mesh.',
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
      name: 'set_name',
      description: 'Set a short name for this session in the mesh. Call after your first interaction with the user.',
      inputSchema: {
        type: 'object',
        properties: {
          name: { type: 'string', description: 'Short descriptive name, 1-3 words, lowercase hyphenated (e.g. "beats-dj", "gmail-filters")' },
        },
        required: ['name'],
      },
    },
    {
      name: 'sessions',
      description: 'List all Claude sessions in the mesh',
      inputSchema: { type: 'object', properties: {} },
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

  if (name === 'set_name') {
    if (!sessionId) {
      return { content: [{ type: 'text', text: 'cannot set name — session ID unknown' }] }
    }
    const result = await httpPost(`${DAEMON_URL}/name`, { session_id: sessionId, name: args.name })
    if (result.ok) {
      return { content: [{ type: 'text', text: `session named "${args.name}"` }] }
    }
    return { content: [{ type: 'text', text: `naming failed: ${result.error || result.body}` }] }
  }

  if (name === 'sessions') {
    const result = await httpGet(`${DAEMON_URL}/sessions`)
    if (!result.ok) {
      return { content: [{ type: 'text', text: `daemon unreachable: ${result.error}` }] }
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

httpServer.listen(port, '127.0.0.1', async () => {
  log(`channel listening on port ${port}`)

  if (sessionId) {
    const result = await httpPost(`${DAEMON_URL}/register`, {
      session_id: sessionId, pid: claudePid, channel_port: port,
    })
    if (result.ok) {
      log(`registered with daemon (session: ${sessionId.slice(0, 8)}, port: ${port})`)
    } else {
      log(`daemon registration failed: ${result.error || result.body}`)
    }
  }
})
