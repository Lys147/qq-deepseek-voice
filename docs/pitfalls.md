# 踩坑记录

这个项目真正难的部分都在这里。按遇到顺序写，每条都说明**现象 → 原因 → 解法**。

---

## 1. 音频格式：以为要自己转 SILK

**现象**：查资料都在说"QQ 语音是 silk 格式，要用 silk-v3 编码"。

**真相**：用 OneBot v11 网关时**不需要**。你发 `record` 段、给一个 mp3/wav 的路径或 `file:///` URI，协议端（SnowLuma / NapCat / LLOneBot）自己转。

**注意**：`file:///` 里的路径必须是**协议端能读到的本地绝对路径**。协议端和你不在同一台机器时，得先上传或让它去下载 URL。

---

## 2. 鉴权：401 但不告诉你为什么

**现象**：`POST /send_private_msg` 返回 `{"status":"failed","retcode":1401,"wording":"unauthorized"}`。

**原因**：OneBot HTTP 接口配了 access token，请求头少 `Authorization: Bearer <token>`。

**解法**：token 在**网关自己的配置文件**里，例如 SnowLuma 的
`config/onebot_<uin>.json` → `networks.httpServers[0].accessToken`。

本项目两种都支持：直接填 `onebot.accessToken`，或填 `onebot.tokenConfigPath` 让程序去读（推荐，避免把 token 抄进仓库）。

---

## 3. 沙箱：临时目录写不进去

**现象**：ffmpeg 报 `Error opening output ... Permission denied`，路径在 `%TEMP%`。

**原因**：某些宿主（如 DSH）对**子进程**的写权限限制在工作区内，`%TEMP%` 在工作区外。

**解法**：所有中间文件写**工作目录内**（本项目用 `<repo>/.cache`）。

同类问题还会打到：
- ModelScope 想写 `~/.modelscope` → 设 `MODELSCOPE_CACHE`
- HuggingFace 想写 `~/.cache/huggingface` → 设 `HF_HOME`

---

## 4. 显存：第二句就 CUDA OOM

**现象**：8 GB 卡，第一句正常，第二句：
`CUDA out of memory. GPU 0 has a total capacity of 7.93 GiB of which 0 bytes is free.`

**原因**：模型常驻占 5.5 GB，推理用的激活值没释放，第二句就撞墙。

**解法**（两个都要）：
1. `use_bf16=True` 加载
2. 每句推理完 `torch.cuda.empty_cache()`

实测：改完后连续三句都正常，显存稳定在 5.5–5.7 GB。

---

## 5. 存盘：torchcodec 的 DLL 加载失败

**现象**：合成成功，最后一步报
`OSError: Could not load this library: .../torchcodec/libtorchcodec_core4.dll`

**原因**：torchaudio 2.9+ 的 `save()` 改走 torchcodec，而某些 torch/CUDA 组合下这个 DLL 加载不了（RTX 50 系 + cu128 尤其容易遇到）。

**解法**：绕开 torchaudio，直接写 PCM：

```python
import soundfile as sf
arr = wav.detach().cpu().numpy()
if arr.ndim == 2 and arr.shape[0] == 1:
    arr = arr[0]
sf.write(path, arr, sampling_rate, subtype="PCM_16")
```

如果用的是 index-tts 源码，改 `indextts/utils/common.py` 里的 `save_pcm_wav`。

---

## 6. 模型下载：`hf download` 会静默少文件

**现象**：`hf download IndexTeam/IndexTTS-2.5 --local-dir ...` 退出码 0、45 个文件，但启动时报
`FileNotFoundError: ...gpt.pth`。

**原因**：3.1 GB 的 `gpt.pth` 没下下来，命令却没报错。

**解法**：下完**必须核对**这几个文件的大小：

| 文件 | 大小 |
|---|---|
| `gpt.pth` | ≈ 3108 MB |
| `codec.pth` | ≈ 579 MB |
| `s2mel.pth` | ≈ 396 MB |
| `qwen0.6bemo4-merge/model.safetensors` | ≈ 1137 MB |

缺的话用 curl 直连补（可断点续传）：

```bash
curl -L -C - -o models/IndexTTS-2.5/gpt.pth \
  https://hf-mirror.com/IndexTeam/IndexTTS-2.5/resolve/main/gpt.pth
```

---

## 7. 辅助模型：w2v-bert-2.0 要单独下

**现象**：`... does not appear to have a file named preprocessor_config.json`

**原因**：IndexTTS 运行时需要 `model_dir/hf_cache/w2v-bert-2.0`（`facebook/w2v-bert-2.0`），它不在主模型仓库里。

**解法**：仓库自带下载器：

```python
from indextts.utils.model_download import ensure_models_available
ensure_models_available("models/IndexTTS-2.5")
```

它会下 `w2v-bert-2.0`、`semantic_codec_model.safetensors`、`campplus_cn_common.bin`、`bigvgan/`。
如果这一步因为网络/权限失败，手动补：

```bash
mkdir -p models/IndexTTS-2.5/hf_cache/w2v-bert-2.0
base=https://hf-mirror.com/facebook/w2v-bert-2.0/resolve/main
curl -sL -o .../config.json "$base/config.json"
curl -sL -o .../preprocessor_config.json "$base/preprocessor_config.json"
curl -L -C - -o .../model.safetensors "$base/model.safetensors"   # 2.2 GB
```

`conformer_shaw.pt`（2.2 GB）是训练用的，推理不需要。

---

## 8. Python 版本：IndexTTS 要求 < 3.12

**现象**：`requires-python = ">=3.10,<3.12"`，3.12 装不上或运行异常。

**解法**：用 3.11。没有就装一个独立的（不污染系统）：

```bash
pip install uv
uv python install 3.11
uv venv --python 3.11 .venv
```

注意 `uv` 默认缓存目录可能也在用户目录，被沙箱挡住时设 `UV_CACHE_DIR`。

---

## 9. 参考音的文本必须一字不差

**现象**：音色"飘"，听起来像别人。

**原因**：参考文本用于对齐，差一个字就会错位。

**解法**：把你录的那句话原样记下来，喂给接口。本项目默认只用音频（零样本），但如果你接的接口要文本，务必准确。

---

## 10. 服务被宿主带走

**现象**：重启宿主（DSH 等）之后，语音服务没了，下次调用报连接失败。

**原因**：服务作为宿主的子进程启动，宿主退出时一起被清理。

**解法**：`spawn` 时 `detached: true` + `unref()`，让服务脱离父进程；调用方发现端口不在听就重新拉起（本项目 `src/mcp-server.js` 的 `ensureService`）。
