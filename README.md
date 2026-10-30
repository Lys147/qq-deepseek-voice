# qq-deepseek-voice

给 AI 伙伴一副嗓子：**本地音色克隆 + QQ 语音条**。

文本 → 本地 IndexTTS 合成（零样本克隆，5–10 秒参考音即可）→ OneBot `record` → QQ 语音消息。

适用于任何基于 OneBot v11 的 QQ 网关（SnowLuma / NapCat / LLOneBot 等），可与 DeepSeek Harness 的 MCP 生态配合，也可以单独当命令行工具用。

```
┌─────────┐   text   ┌──────────────┐   wav   ┌──────────┐  mp3  ┌───────────┐
│  Agent  │ ───────► │ TTS 服务     │ ──────► │  ffmpeg  │ ────► │ OneBot    │ ──► QQ 语音条
│ 或 CLI  │          │ IndexTTS-2.5 │         │ 24k mono │       │ (record)  │
└─────────┘          └──────────────┘         └──────────┘       └───────────┘
```

## 这个项目真正解决的是什么

调 TTS 不难，难的是把它接进 QQ。这件事上没人写清楚的部分，这里都写了：

| 坑 | 真相 |
|---|---|
| 音频格式 | QQ 语音是 **SILK**，但**不需要你自己转** —— 交给 OneBot 协议端（SnowLuma/NapCat）处理，你只要给 mp3/wav |
| 鉴权 | OneBot HTTP 接口要 `Authorization: Bearer <token>`，token 在网关自己的配置里，接口本身不会告诉你 |
| 8 GB 显存 | 模型约占 5.5 GB，**bf16 + 每句后 `torch.empty_cache()`** 才能连续跑，否则第二句 CUDA OOM |
| 冷启动 | 模型加载 28–59 秒；常驻会一直占显存。本项目做成**闲置自动释放 + 按需重载** |
| 存盘 | `torchaudio.save` 在新版本走 torchcodec，某些 CUDA 组合下 DLL 加载失败 → 改用 `soundfile` |
| 模型下载 | `hf download` 会**静默跳过** 3 GB 的 `gpt.pth` 却返回成功，必须逐文件核对 |

## 特性

- **零样本克隆**：给一段 5–10 秒参考音，不需要训练
- **显存友好**：闲置 15 分钟自动释放（8 GB 卡从 5.5 GB → 1.3 GB），下次调用懒加载
- **自愈**：MCP 工具发现服务不在会自动拉起，DSH/宿主重启不会带走服务
- **两种用法**：MCP 工具（给 Agent）或命令行（给人）
- **可换参考音**：每句话都能指定不同的 `ref`，支持多人音色

## 快速开始

### 0. 前置

- Windows / Linux，**NVIDIA 显卡 ≥ 8 GB**（bf16 下约 5.5 GB）
- Python 3.10–3.11（IndexTTS 要求 `<3.12`）
- ffmpeg
- 一个 OneBot v11 网关（已在 QQ 上跑通）

### 1. 装环境

```bash
python -m venv .venv
.venv/Scripts/pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
.venv/Scripts/pip install -r server/requirements.txt
```

> 为什么要 cu128：RTX 50 系是 Blackwell（sm_120），cu118/cu121 的轮子跑不了，会报 `no kernel image is available`。

### 2. 拿模型

```bash
git clone https://github.com/index-tts/index-tts.git
pip install -e index-tts --no-deps      # 它会把 torch 钉在 2.8，跳过
# 权重（约 5.2 GB）放 models/IndexTTS-2.5/
```

需要的文件清单见 [docs/pitfalls.md](docs/pitfalls.md) 第 6 条，**下完必须逐个核对大小**。

### 3. 配好

```bash
cp config.example.json config.json
```

| 字段 | 说明 |
|---|---|
| `onebot.httpUrl` | 网关的 OneBot HTTP API，如 `http://127.0.0.1:3000` |
| `onebot.accessToken` | 网关的 access token；或留空并用下面一项 |
| `onebot.tokenConfigPath` | 直接指向网关配置文件，程序会读 `networks.httpServers[0].accessToken`（**推荐**，token 不进仓库） |
| `target.userId` | 语音发给谁（QQ 号） |
| `tts.url` | 合成服务地址，默认 `http://127.0.0.1:7880` |
| `tts.dir` | 部署根目录（含 `index-tts/`、`models/`、`refs/`），默认本仓 |
| `tts.python` | venv 里的解释器，用于**自动拉起**合成服务；留空则不会自动拉起 |
| `tts.idleMinutes` | 闲置多少分钟释放模型，默认 15 |
| `ffmpeg.path` | ffmpeg 可执行文件；留空用 PATH 上的 |

### 4. 跑

```bash
# 启动 TTS 服务（首次请求会加载模型）
.venv/Scripts/python server/tts_server.py --port 7880 --idle-min 15

# 命令行发一条
node src/cli.mjs --text "在。刚才在水底，没听见你叫我。" --send

# 或让 Agent 用（MCP）
node src/mcp-server.js
```

> Windows 用户可以直接双击 `scripts/start-tts.cmd` / `scripts/stop-tts.cmd`；
> macOS / Linux 请直接用上面的命令（那两处 `.cmd` 只用 PowerShell 做进程管理，核心代码本身跨平台）。

## 给 Agent 用（MCP）

在宿主里注册一个 stdio MCP server：

```yaml
- name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: qqvoice
    transport: stdio
    command: 'node'
    args: ['<本仓路径>/src/mcp-server.js']
    toolCallTimeoutMs: 900000
```

暴露两个工具：

| 工具 | 作用 |
|---|---|
| `say_voice(text)` | 合成并发出，返回 message_id / 音频长度 / 耗时 |
| `voice_health()` | 探活（TTS 服务、模型是否加载、QQ 网关） |

如果宿主支持工具收窄（只给某些会话开放工具），建议**只把它给"她"那条会话**，并让她自己决定何时用语音——工具描述里写清楚"文字是常态"。

## 参考音怎么选

这是音色的唯一输入，比模型重要：

| 要求 | 原因 |
|---|---|
| 5–10 秒、单人 | 太短音色不稳，太长稀释 |
| 无 BGM、无混响 | 混响会被当成音色的一部分学走 |
| 正常语速、情绪平稳 | 参考音的韵律会被模仿 |
| 把你说的那句原话记下来 | 有的接口要参考文本，**差一个字音色就飘** |

```bash
# 从一段长音频里切出干净的一段
ffmpeg -ss 17.9 -t 8.1 -i input.mp4 -vn -ac 1 -ar 24000 \
       -af "loudnorm=I=-18:TP=-2:LRA=7" ref.wav
```

> 克隆别人的声音请先取得授权。本仓**不附带任何音色模型或参考音频**。

## 显存与延迟（RTX 5060 8 GB 实测）

| 场景 | 显存 | 延迟 |
|---|---|---|
| 模型已加载 | 5.5 GB | 约 1 倍实时 |
| 闲置 15 分钟 | 1.3 GB | —— |
| 释放后再调用 | 5.5 GB | 冷加载 28 秒 |
| 服务没起（自动拉起） | —— | 59 秒 |

觉得慢可以：缩短句子（< 60 字）、`--idle-min 0` 让它常驻、或换更大的卡。

## 目录

```
src/mcp-server.js      MCP 工具（零依赖，自己实现 JSON-RPC）
src/cli.mjs            命令行
server/tts_server.py   HTTP 合成服务（标准库，模型懒加载 + 闲置释放）
server/requirements.txt
scripts/               启停脚本
docs/pitfalls.md       踩坑记录（强烈建议先读）
```

## 许可

MIT。IndexTTS 模型与其代码另有许可（见其仓库），本仓不打包模型。
