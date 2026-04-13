#!/usr/bin/env node
/**
 * Session-proxy channel MCP — bidirectional communication for Claude sessions.
 *
 * On startup:
 *   1. Walks process tree to find parent claude PID
 *   2. Reads ~/.claude/sessions/{pid}.json for session UUID
 *   3. Picks a free port and starts HTTP channel server
 *   4. Registers with session-proxy daemon at :8910
 *
 * Provides:
 *   - MCP server with claude/channel capability (stdio transport)
 *   - HTTP POST  /         — push message into this Claude session
 *   - GET        /events   — SSE stream of replies
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

const log = (msg) => process.stderr.write(`[session-proxy] ${msg}\n`)

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

// --- Register with daemon ---

async function registerWithDaemon(sessionId, pid, channelPort) {
  const body = JSON.stringify({ session_id: sessionId, pid, channel_port: channelPort })
  return new Promise((resolve) => {
    const req = http.request(`${DAEMON_URL}/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
      timeout: 5000,
    }, (res) => {
      let data = ''
      res.on('data', (c) => data += c)
      res.on('end', () => resolve({ ok: res.statusCode === 200, body: data }))
    })
    req.on('error', (e) => resolve({ ok: false, error: e.message }))
    req.end(body)
  })
}

// --- Boot ---

const claudePid = findClaudePid()
const sessionInfo = claudePid ? readSessionInfo(claudePid) : null
const sessionId = sessionInfo?.sessionId

if (!sessionId) {
  log(`could not find session ID (claude pid: ${claudePid})`)
  // Continue anyway — channel still works, just can't register
}

// --- Outbound: SSE listeners for replies ---
const listeners = new Set()
function send(text) {
  const chunk = text.split('\n').map(l => `data: ${l}\n`).join('') + '\n'
  for (const emit of listeners) emit(chunk)
}

// --- MCP Server with channel capability ---
const serverName = sessionId ? `session-${sessionId.slice(0, 8)}` : 'session-proxy'

const mcp = new Server(
  { name: serverName, version: '0.1.0' },
  {
    capabilities: {
      experimental: { 'claude/channel': {} },
      tools: {},
    },
    instructions: [
      `You are part of a session mesh. Your session ID is ${sessionId || 'unknown'}.`,
      'Messages arrive as <channel> notifications from other Claude sessions or external systems.',
      'Use the reply tool to send responses back to the sender.',
      'Use the message tool to send messages to other sessions in the mesh.',
    ].join('\n'),
  },
)

// --- Tools ---
mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: 'reply',
      description: 'Reply to an inbound channel message',
      inputSchema: {
        type: 'object',
        properties: {
          chat_id: { type: 'string', description: 'The chat_id from the inbound channel tag' },
          text: { type: 'string', description: 'The reply message' },
        },
        required: ['chat_id', 'text'],
      },
    },
    {
      name: 'message',
      description: 'Send a message to another Claude session via session-proxy. Use session name or UUID prefix.',
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
      description: 'List all Claude sessions in the mesh',
      inputSchema: { type: 'object', properties: {} },
    },
  ],
}))

mcp.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: args } = req.params

  if (name === 'reply') {
    send(`[${args.chat_id}] ${args.text}`)
    return { content: [{ type: 'text', text: 'sent' }] }
  }

  if (name === 'message') {
    const body = JSON.stringify({ text: args.text })
    return new Promise((resolve) => {
      const r = http.request(`${DAEMON_URL}/sessions/${encodeURIComponent(args.to)}/message`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
        timeout: 10000,
      }, (res) => {
        let data = ''
        res.on('data', (c) => data += c)
        res.on('end', () => {
          if (res.statusCode === 200) {
            resolve({ content: [{ type: 'text', text: `message sent to ${args.to}` }] })
          } else {
            const detail = JSON.parse(data).detail || data
            resolve({ content: [{ type: 'text', text: `error: ${detail}` }], isError: true })
          }
        })
      })
      r.on('error', (e) => resolve({ content: [{ type: 'text', text: `error: ${e.message}` }], isError: true }))
      r.end(body)
    })
  }

  if (name === 'sessions') {
    return new Promise((resolve) => {
      http.get(`${DAEMON_URL}/sessions`, { timeout: 5000 }, (res) => {
        let data = ''
        res.on('data', (c) => data += c)
        res.on('end', () => {
          try {
            const sessions = JSON.parse(data)
            const lines = sessions.map(s => {
              const ch = s.channel_port ? `ch:${s.channel_port}` : 'no-channel'
              return `${s.name} (${s.session_id.slice(0, 8)}) — ${s.state} — ${ch}`
            })
            resolve({ content: [{ type: 'text', text: lines.join('\n') || 'no sessions found' }] })
          } catch {
            resolve({ content: [{ type: 'text', text: data }] })
          }
        })
      }).on('error', (e) => {
        resolve({ content: [{ type: 'text', text: `daemon unreachable: ${e.message}` }], isError: true })
      })
    })
  }

  throw new Error(`unknown tool: ${name}`)
})

// --- Connect to Claude Code over stdio ---
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
    const body = Buffer.concat(chunks).toString()

    const chat_id = String(nextId++)
    await mcp.notification({
      method: 'notifications/claude/channel',
      params: {
        content: body,
        meta: { chat_id, path: url.pathname },
      },
    })
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
    const result = await registerWithDaemon(sessionId, claudePid, port)
    if (result.ok) {
      log(`registered with daemon (session: ${sessionId.slice(0, 8)}, port: ${port})`)
    } else {
      log(`daemon registration failed: ${result.error || result.body} — will retry on next poll`)
    }
  }
})
