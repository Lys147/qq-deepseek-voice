#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
tts_server.py —— IndexTTS-2.5 的本地合成服务（只用标准库，无 Web 框架依赖）

为什么要单独一个服务：
  模型加载要 28–59 秒。每次合成开新进程 = 每句话都多等半分钟。
  这里加载一次、进程常驻，但**模型不常驻显存**——闲置若干分钟后释放，
  下次请求再懒加载。8 GB 卡上模型约占 5.5 GB，空闲时要还回去。

接口：
  POST /tts     {"text","ref"?,"output"?,"lang"?}
                → {"ok","path","seconds","elapsed","cold"}
  GET  /health  → {"ok","model","loaded"}
  POST /unload  → 立即释放显存

启动：
  python tts_server.py [--port 7880] [--idle-min 15] [--no-bf16] [--preload]

依赖目录约定（可用 --tts-dir / 环境变量覆盖）：
  <dir>/index-tts/          index-tts 仓库（pip install -e 过）
  <dir>/models/IndexTTS-2.5 权重
  <dir>/refs/               参考音
  <dir>/out/                输出
"""
import argparse
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_tts = None
_last_use = 0.0
_bf16 = True
_idle_sec = 15 * 60
_stopping = False
_lock = threading.Lock()          # 单卡串行：加载 / 推理 / 释放互斥

_token = ''                       # 设置后所有 POST 都要 Bearer；用于非本机部署
MAX_TEXT_CHARS = 600              # 与上游模型建议的输入上限一致
MAX_BODY_BYTES = 256 * 1024       # 正常请求只有几百字节，给足余量即可

PATH = {}


def _inside(path, root):
    """path 是否落在 root 内（解析符号链接后比较，防 ../ 与链接逃逸）。"""
    try:
        p = os.path.realpath(os.path.abspath(path))
        r = os.path.realpath(os.path.abspath(root))
        return os.path.commonpath([p, r]) == r
    except Exception:
        return False


def _free(msg=''):
    global _tts
    if _tts is not None:
        _tts = None
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass
        print('[tts] model unloaded %s' % msg, flush=True)


def _load():
    global _tts
    from indextts.infer_v2_5 import IndexTTS2
    t0 = time.time()
    _tts = IndexTTS2(cfg_path=PATH['cfg'], model_dir=PATH['model'], use_bf16=_bf16)
    print('[tts] model loaded in %.1fs (bf16=%s)' % (time.time() - t0, _bf16), flush=True)


def _idle_watch():
    while not _stopping:
        time.sleep(30)
        if _idle_sec > 0 and _tts is not None and (time.time() - _last_use) > _idle_sec:
            with _lock:
                if _tts is not None and (time.time() - _last_use) > _idle_sec:
                    _free('(idle %.0f min)' % (_idle_sec / 60.0))


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _authed(self):
        h = self.headers.get('Authorization') or ''
        return h == 'Bearer ' + _token

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/health':
            self._json(200, {'ok': True, 'model': 'IndexTTS-2.5', 'loaded': _tts is not None})
        else:
            self._json(404, {'ok': False, 'error': 'not found'})

    def do_POST(self):
        global _last_use
        # 先把请求体读完再判断：HTTP/1.1 长连接下，未读的 body 会被当成下一个请求，
        # 导致后续请求收到莫名其妙的 501（本机实测踩到过）。
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except Exception:
            n = 0
        if n > MAX_BODY_BYTES:
            self.close_connection = True
            self._json(413, {'ok': False, 'error': 'request too large'})
            return
        raw = self.rfile.read(n) if n > 0 else b''

        if _token and not self._authed():
            self._json(401, {'ok': False, 'error': 'unauthorized'})
            return
        if self.path == '/unload':
            with _lock:
                _free('(manual)')
            self._json(200, {'ok': True})
            return
        if self.path != '/tts':
            self._json(404, {'ok': False, 'error': 'not found'})
            return
        try:
            body = json.loads(raw or b'{}')
        except Exception as e:
            self._json(400, {'ok': False, 'error': 'bad json: %s' % e})
            return

        text = str(body.get('text') or '').strip()
        if not text:
            self._json(400, {'ok': False, 'error': 'empty text'})
            return
        if len(text) > MAX_TEXT_CHARS:
            self._json(400, {'ok': False, 'error': 'text too long (max %d chars)' % MAX_TEXT_CHARS})
            return
        ref = str(body.get('ref') or PATH['ref'])
        out = str(body.get('output') or os.path.join(PATH['out'], 'tts_%d.wav' % int(time.time() * 1000)))
        lang = str(body.get('lang') or 'ZH')
        if not os.path.exists(ref):
            self._json(400, {'ok': False, 'error': 'ref not found: %s' % ref})
            return
        # 只允许写到输出目录内：接口不接受任意路径，避免覆盖服务账户有权限的任何文件
        if not _inside(out, PATH['out']):
            self._json(400, {'ok': False, 'error': 'output must be inside %s' % PATH['out']})
            return

        t0 = time.time()
        cold = False
        try:
            with _lock:
                cold = _tts is None
                if cold:
                    _load()
                _tts.infer(spk_audio_prompt=ref, text=text, lang=lang,
                           output_path=out, verbose=False, text_normalization=True)
                _last_use = time.time()
                try:
                    import torch
                    torch.cuda.empty_cache()   # 不清，第二句就会 OOM（8 GB 卡实测）
                except Exception:
                    pass
        except Exception as e:
            traceback.print_exc()
            self._json(500, {'ok': False, 'error': str(e)[:300]})
            return

        elapsed = time.time() - t0
        seconds = 0.0
        try:
            import soundfile as sf
            info = sf.info(out)
            seconds = float(info.frames) / float(info.samplerate)
        except Exception:
            pass
        print('[tts] done %s (%.1fs, %.2fs%s)' % (out, elapsed, seconds, ', cold' if cold else ''), flush=True)
        self._json(200, {'ok': True, 'path': out, 'seconds': round(seconds, 2),
                         'elapsed': round(elapsed, 2), 'cold': bool(cold)})

    def log_message(self, *args):
        pass


def main():
    global _bf16, _idle_sec, _token
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=7880)
    ap.add_argument('--idle-min', type=float, default=15, help='闲置多少分钟释放模型；0=常驻')
    ap.add_argument('--no-bf16', action='store_true', help='显存够但精度要求高时用')
    ap.add_argument('--preload', action='store_true')
    ap.add_argument('--tts-dir', default=os.environ.get('TTS_DIR', os.getcwd()))
    ap.add_argument('--ref', default='')
    ap.add_argument('--token', default=os.environ.get('TTS_TOKEN', ''),
                    help='设置后所有 POST 需 Authorization: Bearer <token>。暴露到非本机时务必设置')
    args = ap.parse_args()

    root = os.path.abspath(args.tts_dir)
    PATH['root'] = root
    PATH['repo'] = os.path.join(root, 'index-tts')
    PATH['model'] = os.path.join(root, 'models', 'IndexTTS-2.5')
    PATH['cfg'] = os.path.join(PATH['model'], 'config.yaml')
    PATH['ref'] = args.ref or os.path.join(root, 'refs', 'ref.wav')
    PATH['out'] = os.path.join(root, 'out')
    os.makedirs(PATH['out'], exist_ok=True)

    # 这两个缓存目录必须可写；有些沙箱只允许写工作区
    os.environ.setdefault('MODELSCOPE_CACHE', os.path.join(root, 'cache', 'modelscope'))
    os.environ.setdefault('HF_HOME', os.path.join(root, 'cache', 'hf'))
    sys.path.insert(0, PATH['repo'])

    _bf16 = not args.no_bf16
    _idle_sec = max(0.0, args.idle_min * 60.0)
    _token = args.token or ''

    loopback = args.host in ('127.0.0.1', 'localhost', '::1')
    if not loopback and not _token:
        print('[tts] !!! 绑定在 %s 且未设 --token：任何能访问该端口的人都可以让这台机器合成音频。' % args.host, flush=True)
        print('[tts] !!! 对外提供时请加 --token，或用防火墙限制来源。', flush=True)

    if args.preload:
        _load()
    threading.Thread(target=_idle_watch, daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print('[tts] listening http://%s:%d idle=%gmin bf16=%s auth=%s'
          % (args.host, args.port, args.idle_min, _bf16, 'on' if _token else 'off'), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
