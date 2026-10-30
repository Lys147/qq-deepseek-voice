#!/usr/bin/env node
/**
 * cli.mjs —— 命令行发一条语音
 *
 * 用法：
 *   node src/cli.mjs --text "在。"               # 只合成，留在本地
 *   node src/cli.mjs --text "在。" --send        # 合成并发出
 *   node src/cli.mjs --file out/xxx.wav --send   # 发已有音频（跳过合成）
 *   node src/cli.mjs --text "在。" --out ./a.wav # 指定输出
 */
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const CACHE = path.join(ROOT, '.cache');
fs.mkdirSync(CACHE, { recursive: true });

function arg(name, def = '') {
  const i = process.argv.indexOf('--' + name);
  if (i < 0) return def;
  const v = process.argv[i + 1];
  return v && !v.startsWith('--') ? v : 'true';
}

function loadConfig() {
  const p = process.env.QQVOICE_CONFIG || path.join(ROOT, 'config.json');
  let text;
  try {
    text = fs.readFileSync(p, 'utf8');
  } catch {
    console.error('[cli] 找不到配置文件：' + p);
    console.error('      复制 config.example.json 为 config.json，或设 QQVOICE_CONFIG 指到别处。');
    process.exit(1);
  }
  if (text.charCodeAt(0) === 0xFEFF) text = text.slice(1);
  let cfg;
  try {
    cfg = JSON.parse(text);
  } catch (e) {
    console.error('[cli] config.json 不是合法 JSON：' + e.message);
    process.exit(1);
  }
  let token = cfg?.onebot?.accessToken || '';
  if (!token && cfg?.onebot?.tokenConfigPath) {
    try {
      const j = JSON.parse(fs.readFileSync(cfg.onebot.tokenConfigPath, 'utf8').replace(/^\uFEFF/, ''));
      token = j?.networks?.httpServers?.[0]?.accessToken || '';
    } catch { /* ignore */ }
  }
  return {
    onebotUrl: String(cfg?.onebot?.httpUrl || 'http://127.0.0.1:3000').replace(/\/+$/, ''),
    onebotToken: token,
    target: Number(cfg?.target?.userId || 0),
    ttsUrl: String(cfg?.tts?.url || 'http://127.0.0.1:7880').replace(/\/+$/, ''),
    ffmpeg: cfg?.ffmpeg?.path || 'ffmpeg',
  };
}

const CFG = loadConfig();
const TEXT = arg('text');
const FILE = arg('file');
const OUT = arg('out');
const SEND = arg('send') === 'true';

if (!TEXT && !FILE) {
  console.error('缺少 --text 或 --file');
  process.exit(2);
}

function toMp3(src, dst) {
  execFileSync(CFG.ffmpeg, ['-y', '-i', src, '-ar', '24000', '-ac', '1', '-b:a', '48k', dst], { stdio: 'ignore' });
  return dst;
}

async function sendVoice(file) {
  if (!CFG.onebotToken) throw new Error('没有 OneBot token');
  const body = {
    user_id: CFG.target,
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

const stamp = Date.now();
const wav = OUT || path.join(CACHE, `cli_${stamp}.wav`);
const mp3 = path.join(CACHE, `cli_${stamp}.mp3`);

if (FILE) {
  if (!fs.existsSync(FILE)) throw new Error('找不到文件: ' + FILE);
  console.log('[cli] 直接使用', FILE);
  toMp3(FILE, mp3);
} else {
  const r = await fetch(CFG.ttsUrl + '/tts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: TEXT, output: wav }),
    signal: AbortSignal.timeout(900_000),
  });
  if (!r.ok) throw new Error(`TTS ${r.status}: ${(await r.text()).slice(0, 180)}`);
  const j = await r.json();
  if (!j.ok) throw new Error('TTS 失败: ' + j.error);
  console.log(`[cli] 合成 ${j.seconds}s（耗时 ${j.elapsed}s${j.cold ? '，冷启动' : ''}）→ ${j.path}`);
  toMp3(j.path, mp3);
}

console.log('[cli] mp3', (fs.statSync(mp3).size / 1024).toFixed(0) + 'KB');
if (!SEND) {
  console.log('[cli] 未加 --send，停在本地');
} else {
  const res = await sendVoice(mp3);
  console.log('[cli] 已发送', JSON.stringify(res));
}
