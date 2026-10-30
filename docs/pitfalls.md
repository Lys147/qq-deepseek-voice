# 踩坑记录

从零到能用，十个坑。每一个我都撞过，每一个都不是"看文档就能避免"的那类。

写下来是因为：**下一个想给她配音的人，不该再从零撞一遍。**

按撞上的顺序排。每条都是「我以为…→ 实际上是…→ 于是」。

---

## 1. 音频格式：所有人都说要自己转 SILK

**我以为**：QQ 语音是 SILK，得先用 `silk-v3` 编码，要装一堆原生库。

**实际上是**：用 OneBot v11 网关时根本不用。你发一个 `record` 段、给一个 mp3 或 `file:///` 路径，协议端（SnowLuma / NapCat / LLOneBot）自己转。

**于是**：整个 audio pipeline 只剩 ffmpeg 转 24 kHz 单声道这一步。

⚠️ 唯一要注意的：`file:///` 里的路径必须是**协议端能读到的本机绝对路径**。协议端和你不在同一台机器时，得先上传，或者给它一个 URL 让它自己下载。

---

## 2. 鉴权：401，但不告诉你缺什么

**我以为**：接口在本机，直接 POST 就行。

**实际上是**：`{"status":"failed","retcode":1401,"wording":"unauthorized"}`。

**于是**：翻了网关的配置文件，发现 token 在那里：
`config/onebot_<uin>.json` → `networks.httpServers[0].accessToken`，请求头要 `Authorization: Bearer <token>`。

这个项目两种都支持：直接填 `onebot.accessToken`，或者填 `onebot.tokenConfigPath` 让程序自己去读。**推荐后者**——省得你把 token 抄进一个迟早要提交的文件里。

---

## 3. 沙箱：临时目录写不进去

**我以为**：写 `%TEMP%` 天经地义。

**实际上是**：ffmpeg 报 `Error opening output ... Permission denied`。因为宿主（我用的是 DSH）把**子进程**的写权限限制在工作区内，`%TEMP%` 在外面。

**于是**：所有中间文件写工作目录（本项目用 `<repo>/.cache`）。

同一个坑还会打到两个地方：

- ModelScope 想写 `~/.modelscope` → 设 `MODELSCOPE_CACHE`
- HuggingFace 想写 `~/.cache/huggingface` → 设 `HF_HOME`

凡是"某个库要往用户目录写东西"，在受限环境里都得先想一遍。

---

## 4. 显存：第二句就 OOM

**我以为**：8 GB 卡，模型 5.5 GB，剩下 2.5 GB 够推理了。

**实际上是**：第一句正常，第二句直接——

```
CUDA out of memory. Tried to allocate 202.00 MiB.
GPU 0 has a total capacity of 7.93 GiB of which 0 bytes is free.
```

**于是**：两件事都要做，缺一不可：

1. `use_bf16=True` 加载
2. 每句推理完 `torch.cuda.empty_cache()`

改完连续三句正常，显存稳在 5.5–5.7 GB（RTX 5060 8 GB / torch 2.11+cu128）。**只做第一件的话，第三句还是会炸。**

---

## 5. 存盘：torchcodec 的 DLL 加载失败

**我以为**：音频都算出来了，存个盘还能出什么事。

**实际上是**：合成成功，最后一步——

```
OSError: Could not load this library:
  .../torchcodec/libtorchcodec_core4.dll
```

torchaudio 2.9+ 的 `save()` 改走 torchcodec，而某个 torch/CUDA 组合下这个 DLL 就是加载不了（RTX 50 系 + cu128 尤其容易撞上）。

**于是**：绕开 torchaudio，直接写 PCM：

```python
import soundfile as sf
arr = wav.detach().cpu().numpy()
if arr.ndim == 2 and arr.shape[0] == 1:
    arr = arr[0]
sf.write(path, arr, sampling_rate, subtype="PCM_16")
```

用 index-tts 源码的话，改 `indextts/utils/common.py` 里的 `save_pcm_wav`。

**这件事教会我的**：合成成功不等于拿到文件。一个管线要在最后一步才能确认它真的跑完了。

---

## 6. 模型下载：`hf download` 会静默少文件

**我以为**：退出码 0、45 个文件、日志说 success，那就是齐了。

**实际上是**：启动时报

```
FileNotFoundError: .../gpt.pth
```

那个文件 3.1 GB，根本没下下来。它不报错。

**于是**：下完**必须逐个核对大小**：

| 文件 | 大小 |
|---|---|
| `gpt.pth` | ≈ 3108 MB |
| `codec.pth` | ≈ 579 MB |
| `s2mel.pth` | ≈ 396 MB |
| `qwen0.6bemo4-merge/model.safetensors` | ≈ 1137 MB |

缺了就用 curl 直连补，支持断点续传：

```bash
curl -L -C - -o models/IndexTTS-2.5/gpt.pth \
  https://hf-mirror.com/IndexTeam/IndexTTS-2.5/resolve/main/gpt.pth
```

**教训不是"hf 有 bug"**，是：任何下载完之后，都要用**使用方的标准**去验，而不是用下载方的退出码。

---

## 7. 辅助模型：w2v-bert-2.0 不在主仓里

**我以为**：主模型下完就完事了。

**实际上是**：

```
... does not appear to have a file named preprocessor_config.json
```

IndexTTS 运行时还要 `model_dir/hf_cache/w2v-bert-2.0`（来自 `facebook/w2v-bert-2.0`），它不在主模型仓库里。

**于是**：用仓库自带的下载器：

```python
from indextts.utils.model_download import ensure_models_available
ensure_models_available("models/IndexTTS-2.5")
```

它会拉 `w2v-bert-2.0`、`semantic_codec_model.safetensors`、`campplus_cn_common.bin`、`bigvgan/`。

这一步如果因为网络或权限失败，手动补：

```bash
mkdir -p models/IndexTTS-2.5/hf_cache/w2v-bert-2.0
base=https://hf-mirror.com/facebook/w2v-bert-2.0/resolve/main
curl -sL -o .../config.json "$base/config.json"
curl -sL -o .../preprocessor_config.json "$base/preprocessor_config.json"
curl -L -C - -o .../model.safetensors "$base/model.safetensors"   # 2.2 GB
```

`conformer_shaw.pt`（2.2 GB）是训练用的，**推理不需要**，省下这两个 G。

---

## 8. Python 版本：IndexTTS 卡在 3.12 以下

**我以为**：3.12 比 3.11 新，应该更好。

**实际上是**：`requires-python = ">=3.10,<3.12"`。3.12 装不上，或者装上跑不起来。

**于是**：用 3.11。机器上没有就装一个独立的，别动系统 Python：

```bash
pip install uv
uv python install 3.11
uv venv --python 3.11 .venv
```

`uv` 自己的缓存也可能在用户目录，被沙箱挡住时设 `UV_CACHE_DIR`——又是一个"库要往家目录写"。

---

## 9. 参考音的文本必须一字不差

**我以为**：音色只跟波形有关，文本无所谓。

**实际上是**：文本用于对齐。差一个字，音色就"飘"——听起来还是那个人的嗓子，但语气不是。

**于是**：把你录的那句话原样记下来，喂给接口。

本项目默认只用音频（零样本），但如果你接的接口要文本，这一条能救你一晚上。

---

## 10. 服务被宿主带走

**我以为**：服务起起来就在那了。

**实际上是**：它作为宿主的子进程启动，宿主一重启，它也一起没了。下次说话报"连接失败"。

**于是**：`spawn` 时 `detached: true` + `unref()`，脱离父进程；调用方发现端口不在听就重新拉起（见 `src/mcp-server.js` 的 `ensureService`）。

顺带做个闲置释放：模型不在显存里常驻，用完十分钟还回去。**给别人用的东西，不该占着别人整张卡。**

---

## 写在最后

这十条里，只有一条是真的"技术问题"（第 5 条）。

其余九条都是**信息差**：没有人写，所以你只能自己撞。

如果这些记录帮你省下一天，那就够了。
