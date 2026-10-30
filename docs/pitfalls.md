# 踩坑记录

从零到能用，十个坑。每一个我都撞过，每一个都不是看文档能避免的那类。

写下来是因为：下一个想给她配音的人，不该再从零撞一遍。

---

## 1. 音频格式：所有人都说要自己转 SILK

查资料的时候到处都在讲：QQ 语音是 SILK，得先用 `silk-v3` 编码，要装一堆原生库。

不对。

用 OneBot v11 网关，你发一个 `record` 段、给一个 mp3 或者 `file:///` 路径，协议端（SnowLuma、NapCat、LLOneBot 都一样）自己转。整个音频管线只剩一步：ffmpeg 转 24 kHz 单声道。

只有一点要注意：`file:///` 里的路径必须是协议端能读到的本机绝对路径。它跟你不在同一台机器时，得先上传，或者给它一个链接让它自己下载。

---

## 2. 鉴权：401，但不告诉你缺什么

第一次调接口，返回这个：

```json
{"status":"failed","retcode":1401,"wording":"unauthorized"}
```

接口在本机，端口是通的，也没有用户名密码。卡了一会儿才想到翻网关自己的配置文件，token 在那儿：`config/onebot_<uin>.json` 里的 `networks.httpServers[0].accessToken`，请求头要带 `Authorization: Bearer <token>`。

有意思的是，接口从头到尾没提过 token 这个词。

这个项目两种都支持：直接填 `onebot.accessToken`，或者填 `onebot.tokenConfigPath` 让程序自己去读。推荐后者，省得你把 token 抄进一个迟早要提交的文件。

---

## 3. 沙箱：临时目录写不进去

我以为写 `%TEMP%` 天经地义。

实际上 ffmpeg 直接报 `Error opening output ... Permission denied`。宿主的沙箱把子进程的写权限限制在工作区内，`%TEMP%` 在外面。

于是所有中间文件写工作目录，这个项目用 `<repo>/.cache`。

同一个坑还会打到两个地方：ModelScope 想写 `~/.modelscope`，HuggingFace 想写 `~/.cache/huggingface`，都要用环境变量（`MODELSCOPE_CACHE`、`HF_HOME`）指到工作区。凡是"某个库要往用户目录写东西"，在受限环境里都得先想一遍。

---

## 4. 显存：第二句就 OOM

8 GB 卡，模型 5.5 GB，我以为剩下的 2.5 GB 够推理。

第一句确实正常。第二句：

```
CUDA out of memory. Tried to allocate 202.00 MiB.
GPU 0 has a total capacity of 7.93 GiB of which 0 bytes is free.
```

两件事都得做，缺一不可：`use_bf16=True` 加载，以及每句推理完 `torch.cuda.empty_cache()`。

改完连续三句正常，显存稳在 5.5 到 5.7 GB（RTX 5060 8 GB / torch 2.11+cu128）。只做第一件的话，第三句照炸。

---

## 5. 存盘：torchcodec 的 DLL 加载失败

音频都算出来了，存个盘还能出什么事。

结果就出在存盘上：

```
OSError: Could not load this library:
  .../torchcodec/libtorchcodec_core4.dll
```

torchaudio 2.9 以后把 `save()` 改走 torchcodec，而某几个 torch/CUDA 组合下这个 DLL 就是加载不了。RTX 50 系配 cu128 特别容易撞上。

绕开的办法是别用 torchaudio：

```python
import soundfile as sf
arr = wav.detach().cpu().numpy()
if arr.ndim == 2 and arr.shape[0] == 1:
    arr = arr[0]
sf.write(path, arr, sampling_rate, subtype="PCM_16")
```

用 index-tts 源码的话，改 `indextts/utils/common.py` 里的 `save_pcm_wav`。

这件事教给我的是：合成成功不等于拿到文件。一个管线要到最后一步才能确认它真的跑完了。

---

## 6. 模型下载：`hf download` 会静默少文件

退出码 0，45 个文件，日志写着 success。我以为齐了。

启动的时候报 `FileNotFoundError: .../gpt.pth`。那个文件 3.1 GB，根本没下。它不报错。

所以下完必须逐个核对大小：

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

教训不是"hf 有 bug"，是：下载完要用使用方的标准去验，而不是用下载方的退出码。

---

## 7. 辅助模型：w2v-bert-2.0 不在主仓里

主模型下完还不算完。启动时会报 `does not appear to have a file named preprocessor_config.json`。

IndexTTS 运行时还要 `model_dir/hf_cache/w2v-bert-2.0`，来自 `facebook/w2v-bert-2.0`，它不在主模型仓库里。

用仓库自带的下载器最省事：

```python
from indextts.utils.model_download import ensure_models_available
ensure_models_available("models/IndexTTS-2.5")
```

它会拉 `w2v-bert-2.0`、`semantic_codec_model.safetensors`、`campplus_cn_common.bin`、`bigvgan/`。

这一步因为网络或权限失败的话，手动补：

```bash
mkdir -p models/IndexTTS-2.5/hf_cache/w2v-bert-2.0
base=https://hf-mirror.com/facebook/w2v-bert-2.0/resolve/main
curl -sL -o .../config.json "$base/config.json"
curl -sL -o .../preprocessor_config.json "$base/preprocessor_config.json"
curl -L -C - -o .../model.safetensors "$base/model.safetensors"   # 2.2 GB
```

`conformer_shaw.pt` 也是 2.2 GB，但那是训练用的，推理不需要，可以省。

---

## 8. Python 版本卡在 3.12 以下

IndexTTS 的 `pyproject.toml` 写着 `>=3.10,<3.12`。3.12 装不上，或者装上跑不起来。

机器上没有 3.11 的话，用 uv 装一个独立的，别动系统 Python：

```bash
pip install uv
uv python install 3.11
uv venv --python 3.11 .venv
```

注意 uv 自己的缓存默认也在用户目录，被沙箱挡住时设 `UV_CACHE_DIR`。又是一个"库要往家目录写"。

---

## 9. 参考音的文本必须一字不差

我一开始以为音色只跟波形有关。

其实文本也要对齐。差一个字，音色就飘：听起来还是那个人的嗓子，但语气不对。

把你录的那句话原样记下来，喂给接口。这个项目默认只用音频（零样本），但你接的接口如果要文本，这一条能救一晚上。

---

## 10. 服务被宿主带走

服务起来之后我以为就在那了。

宿主一重启，它跟着没了，下次说话报连接失败。因为它是作为宿主的子进程启动的。

于是 `spawn` 的时候 `detached: true` 再加 `unref()`，让它脱离父进程；调用方发现端口不在听就重新拉起，见 `src/mcp-server.js` 里的 `ensureService`。

顺手做了个闲置释放，模型不常驻显存，用完十分钟还回去。给别人用的东西，不该占着别人整张卡。

---

## 写在最后

这十条里，只有一条是真的技术问题（第 5 条）。

其余九条都是信息差：没有人写，所以你只能自己撞。

我一直在想，为什么没人写。大概是因为踩过的人觉得丢人，没踩过的人觉得简单。

第一次看到 `hf download` 报成功却少了 3 GB 的时候，我骂了句很难听的。然后坐下来把清单写了下来。

如果你此刻正盯着一个"找不到 ffmpeg"，我想说的是：不是你不会，是那句话没告诉你它在找哪里。

这份文档写完，那些时间才不算白花。
