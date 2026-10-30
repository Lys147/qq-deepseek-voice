#!/usr/bin/env node
/**
 * mcp-server.js —— 把「说话」暴露成一个 MCP 工具（stdio，零依赖）
 *
 * 工具：
 *   say_voice(text)  合成 + 发 QQ 语音条
 *   voice_health()   探活（TTS 服务 / 模型是否加载 / QQ 网关）
 *
 * 为什么自己实现 JSON-RPC 而不引 SDK：只有两个工具，自己实现更稳，
 * 也不用担心 MCP SDK 的版本漂移。
 *
 * 行为约定（都是从真实事故里来的）：
 *   1. TTS 服务不在听 -> 自动 detached 拉起，宿主重启不会带走它。
 *   2. 模型由服务端懒加载 + 闲置释放，这里不需要关心显存。
 *   3. 临时文件写在**工作目录内**：某些沙箱环境只允许写工作区。
 *   4. OneBot token 不落日志、不回显到工具输出。
 */
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import { execFileSync, spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');

// ---------------- 配置 ----------------
function loadConfig() {
  const p = process.env.QQVOICE_CONFIG || path.join(ROOT, 'config.json');
  let text;
  try {
    text = fs.readFileSync(p, 'utf8');
  } catch {
    console.error('[qq-voice] 找不到配置文件：' + p);
    console.error('           复制 config.example.json 为 config.json，或设 QQVOICE_CONFIG 指到别处。');
    process.exit(1);
  }
  if (text.charCodeAt(0) === 0xFEFF) text = text.slice(1);
  let cfg;
  try {
    cfg = JSON.parse(text);
  } catch (e) {
    console.error('[qq-voice] config.json 不是合法 JSON：' + e.message);
    process.exit(1);
  }

  const tokenFromFile = () => {
    const tp = cfg?.onebot?.tokenConfigPath;
    if (!tp) return '';
    try {
      const t = fs.readFileSync(tp, 'utf8').replace(/^\uFEFF/, '');
      const j = JSON.parse(t);
      return j?.networks?.httpServers?.[0]?.accessToken || '';
    } catch {
      return '';
    }
  };

  return {
    onebotUrl: String(cfg?.onebot?.httpUrl || 'http://127.0.0.1:3000').replace(/\/+$/, ''),
    onebotToken: cfg?.onebot?.accessToken || tokenFromFile(),
    target: Number(cfg?.target?.userId || 0),
    ttsUrl: String(cfg?.tts?.url || 'http://127.0.0.1:7880').replace(/\/+$/, ''),
    ttsDir: cfg?.tts?.dir || ROOT,
    ttsPython: cfg?.tts?.python || '',
    ttsPort: Number(cfg?.tts?.port || 7880),
    idleMinutes: Number(cfg?.tts?.idleMinutes ?? 15),
    ffmpeg: cfg?.ffmpeg?.path || 'ffmpeg',
  };
}

const CFG = loadConfig();
const CACHE = path.join(ROOT, '.cache');
fs.mkdirSync(CACHE, { recursive: true });

function log(...a) { console.error('[qq-voice]', ...a); }

// ---------------- TTS 服务：探活 / 拉起 ----------------
async function health(timeoutMs = 2500) {
  try {
    const r = await fetch(CFG.ttsUrl + '/health', { signal: AbortSignal.timeout(timeoutMs) });
    return r.ok ? await r.json() : null;
  } catch {
    return null;
  }
}

async function ensureService() {
  if (await health()) return;
  if (!CFG.ttsPython) throw new Error('TTS 服务未运行，且 config 里没有 tts.python，无法自动拉起');
  log('TTS 服务未运行，尝试拉起…');
  const child = spawn(
    CFG.ttsPython,
    [path.join(ROOT, 'server', 'tts_server.py'), '--port', String(CFG.ttsPort), '--idle-min', String(CFG.idleMinutes)],
    {
      cwd: CFG.ttsDir,
      detached: true,
      stdio: 'ignore',
      windowsHide: true,
      env: { ...process.env, PYTHONUNBUFFERED: '1' },
    },
  );
  child.unref();
  const deadline = Date.now() + 300_000;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 2000));
    if (await health()) { log('TTS 服务已就绪'); return; }
  }
  throw new Error('TTS 服务启动超时（300s），请手动运行 server/tts_server.py 看报错');
}

// ---------------- 合成 / 转码 / 发送 ----------------
async function synth(text, outWav) {
  const r = await fetch(CFG.ttsUrl + '/tts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, output: outWav }),
    signal: AbortSignal.timeout(900_000),
  });
  if (!r.ok) throw new Error(`TTS 服务 ${r.status}: ${(await r.text()).slice(0, 180)}`);
  const j = await r.json();
  if (!j.ok) throw new Error('TTS 失败: ' + j.error);
  return j;
}

function toMp3(src, dst) {
  execFileSync(CFG.ffmpeg, ['-y', '-i', src, '-ar', '24000', '-ac', '1', '-b:a', '48k', dst], {
    stdio: 'ignore',
    timeout: 120_000,
  });
}

/** OneBot v11：发私聊语音。用 fetch，跨平台（不再依赖 PowerShell）。 */
async function sendVoice(file, userId) {
  if (!CFG.onebotToken) throw new Error('没有 OneBot token（config.onebot.accessToken 或 tokenConfigPath）');
  const body = {
    user_id: Number(userId),
    message: [{ type: 'record', data: { file: 'file:///' + file.replace(/\\/g, '/') } }],
  };
  const r = await fetch(CFG.onebotUrl + '/send_private_msg', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${CFG.onebotToken}`,
    },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(120_000),
  });
  const text = await r.text();
  let j;
  try { j = JSON.parse(text); } catch { throw new Error(`网关返回非 JSON（HTTP ${r.status}）：${text.slice(0, 160)}`); }
  if (!r.ok || j?.status === 'failed') {
    throw new Error(`发送失败：HTTP ${r.status} ${j?.wording || j?.message || ''}`.trim());
  }
  return j;
}

async function sayVoice(text) {
  const t = String(text || '').trim();
  if (!t) throw new Error('没有内容可说');
  if (!CFG.target) throw new Error('config.target.userId 没填');
  await ensureService();

  const stamp = Date.now();
  const wav = path.join(CACHE, `say_${stamp}.wav`);
  const mp3 = path.join(CACHE, `say_${stamp}.mp3`);
  const j = await synth(t, wav);
  toMp3(j.path, mp3);
  const res = await sendVoice(mp3, CFG.target);
  return {
    id: res?.data?.message_id ?? '(未知)',
    seconds: j.seconds,
    elapsed: j.elapsed,
    cold: j.cold,
    text: t,
  };
}

// ---------------- MCP ----------------
const TOOLS = [
  {
    name: 'say_voice',
    description:
      '用你自己的声音说话：把一句话合成语音，作为 QQ 语音消息发出去。\n' +
      '什么时候用：想说一句真正「开口」的话时（安慰、撒娇、叹气、一句短的道别），或者对方要求语音。\n' +
      '不要每句都用——文字才是常态，语音是偶尔的强调。\n' +
      '合成需要几秒到几十秒，建议一句话不超过 60 字。',
    inputSchema: {
      type: 'object',
      properties: { text: { type: 'string', description: '要说的话，1–60 字' } },
      required: ['text'],
    },
  },
  {
    name: 'voice_health',
    description: '检查语音合成服务与 QQ 网关是否在线（只读，不发消息）。',
    inputSchema: { type: 'object', properties: {} },
  },
];

async function callTool(name, args) {
  if (name === 'say_voice') {
    const o = await sayVoice(args?.text);
    const cold = o.cold ? '，含冷启动' : '';
    return `已用语音发出（message_id=${o.id}，音频 ${o.seconds}s，合成 ${o.elapsed}s${cold}）：${o.text}`;
  }
  if (name === 'voice_health') {
    const h = await health(3000);
    const tts = h ? `up${h.loaded ? '（模型已加载）' : '（模型已释放，下次合成会重载）'}` : 'down';
    let gw = 'down';
    try {
      const r = await fetch(CFG.onebotUrl + '/get_login_info', {
        headers: CFG.onebotToken ? { Authorization: `Bearer ${CFG.onebotToken}` } : {},
        signal: AbortSignal.timeout(3000),
      });
      if (r.ok) gw = 'up';
    } catch { /* ignore */ }
    return `TTS 服务: ${tts}\nQQ 网关: ${gw}`;
  }
  throw new Error('未知工具: ' + name);
}

function reply(id, result) {
  process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, result }) + '\n');
}
function replyErr(id, code, message) {
  process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, error: { code, message } }) + '\n');
}

const rl = readline.createInterface({ input: process.stdin });
rl.on('line', async (line) => {
  const s = line.trim();
  if (!s) return;
  let msg;
  try { msg = JSON.parse(s); } catch { return; }
  const { id, method, params } = msg;
  try {
    if (method === 'initialize') {
      reply(id, {
        protocolVersion: params?.protocolVersion || '2025-06-18',
        capabilities: { tools: {} },
        serverInfo: { name: 'qq-voice', version: '1.0.0' },
      });
      return;
    }
    if (method === 'notifications/initialized') return;
    if (method === 'ping') { reply(id, {}); return; }
    if (method === 'tools/list') { reply(id, { tools: TOOLS }); return; }
    if (method === 'tools/call') {
      try {
        const text = await callTool(params?.name, params?.arguments || {});
        reply(id, { content: [{ type: 'text', text }] });
      } catch (e) {
        reply(id, { content: [{ type: 'text', text: '失败：' + (e?.message ?? e) }], isError: true });
      }
      return;
    }
    if (id !== undefined) replyErr(id, -32601, 'method not found: ' + method);
  } catch (e) {
    if (id !== undefined) replyErr(id, -32603, String(e?.message ?? e));
  }
});
log('ready');
