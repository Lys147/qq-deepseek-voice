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

PATH = {}


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
        if self.path == '/unload':
            with _lock:
                _free('(manual)')
            self._json(200, {'ok': True})
            return
        if self.path != '/tts':
            self._json(404, {'ok': False, 'error': 'not found'})
            return
        try:
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(n) or b'{}')
        except Exception as e:
            self._json(400, {'ok': False, 'error': 'bad json: %s' % e})
            return

        text = str(body.get('text') or '').strip()
        if not text:
            self._json(400, {'ok': False, 'error': 'empty text'})
            return
        ref = str(body.get('ref') or PATH['ref'])
        out = str(body.get('output') or os.path.join(PATH['out'], 'tts_%d.wav' % int(time.time() * 1000)))
        lang = str(body.get('lang') or 'ZH')
        if not os.path.exists(ref):
            self._json(400, {'ok': False, 'error': 'ref not found: %s' % ref})
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
    global _bf16, _idle_sec
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=7880)
    ap.add_argument('--idle-min', type=float, default=15, help='闲置多少分钟释放模型；0=常驻')
    ap.add_argument('--no-bf16', action='store_true', help='显存够但精度要求高时用')
    ap.add_argument('--preload', action='store_true')
    ap.add_argument('--tts-dir', default=os.environ.get('TTS_DIR', os.getcwd()))
    ap.add_argument('--ref', default='')
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

    if args.preload:
        _load()
    threading.Thread(target=_idle_watch, daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print('[tts] listening http://%s:%d idle=%gmin bf16=%s' % (args.host, args.port, args.idle_min, _bf16), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
