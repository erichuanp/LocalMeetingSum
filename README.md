# LocalMeetingSum

本地中文会议转写与总结工具。**全程在本机 GPU 上跑**——FunASR 2-pass(流式 + 离线重解码,带"鱿鱼自纠"效果)+ CAM++ 说话人区分;总结走任意 OpenAI 兼容端点(LM Studio / vLLM / Ollama)。

支持两种输入:

- **实时录音**——多音轨同时抓,麦克风 + 系统输出(WASAPI loopback)随意组合,每个音轨都有实时绿条电平表
- **文件上传**——苹果/安卓/桌面常见的音视频格式(mp4/mov/m4a/mp3/wav/flac/opus/webm/3gp/aac/...,本质是 ffmpeg 能解的都行)

## 体验亮点

- **流式 partial 实时出字**(~600ms 一段),VAD 检到句末再用离线模型按整句双向 context 重解码 → 自动把"今天我去吃鱿鱼,但是有鱼很臭"修正成"鱿鱼很臭"
- **每个音源独立 STT,不同源绝不会被并成同一个人**(因为本来就是不同人在不同设备说话);某个源静音也不会无中生有冒出发言人
- **说话人分得偏敏感,允许事后校正合并**——模型先按音色分成 ABCDE,你看完后说"AC 是一个人,BD 是另一个人,E 是第三个人",一键合并再总结
- **总结结构化**:LLM 输出 `topics → 每个话题 → 每个发言人的观点` + `todos(谁要做什么)`,严格 JSON

## 系统要求

| | |
|---|---|
| OS | Windows 10/11(要 WASAPI;实时录音必须) |
| GPU | NVIDIA,显存 ≥ 6GB(RTX 50 系 Blackwell 走 cu128 wheel,详见下) |
| Python | 3.11(funasr 在 3.11 最稳;3.12/3.13 deps 有冲突) |
| ffmpeg | 装好且在 PATH 上 |
| 本地 LLM | 任意 OpenAI 兼容 endpoint,默认配 LM Studio |

## 安装

### 1. 装 ffmpeg

```powershell
choco install ffmpeg   # 或者从 https://www.gyan.dev/ffmpeg/builds/ 下载,加进 PATH
ffmpeg -version        # 验证
```

### 2. 创建 conda 环境

```powershell
conda create -y -n lms python=3.11
conda activate lms
```

### 3. 装 Python 依赖

```powershell
pip install -r requirements.txt
```

`requirements.txt` 里固定了 `--extra-index-url https://download.pytorch.org/whl/cu128`——RTX 50 系(sm_120)**必须**用 cu128 wheel,cu124/cu126 装上去能 import 但 CUDA op 全报"no kernel image"。

如果你是 RTX 30/40 系或更老的卡,可以改成 cu124,功能完全一样。

### 4. 配置 LLM endpoint

把 `.env.example` 复制成 `.env` 并改成你自己的:

```env
LLM_BASE_URL=http://你的-llm-地址:1234/v1
LLM_API_KEY=随便填,LM Studio 不验
LLM_MODEL=qwen/qwen3.5-9b   # 或任何你 LM Studio 加载的中文模型
```

### 5. 启动

```powershell
python server.py
```

首次启动会自动从 ModelScope 下载约 1.2GB 的 FunASR 模型到 `~/.cache/modelscope/`。下完后服务监听 `0.0.0.0:788`。

浏览器打开 <http://localhost:788>。

## 使用流程

### A. 实时录音

1. 顶部 **实时录音** 标签
2. 点 **+ 添加音源** → 选 `麦克风` 或 `系统输出` → 从下拉列表选具体设备 → **添加**
3. 添加完源后,**绿条实时跳动**说明该源在收声(系统输出走 WASAPI loopback,需要桌面这时候确实在出声才会跳);你可以加任意多个源
4. 点 **▶ 开始录音转写**,partial 会在橘色框里实时刷,VAD 切到句末就把最终句子带说话人标签 push 到下面的转写列表
5. 录完点 **■ 停止**,会自动弹出"校正发言人"面板

> **Wave Link 用户提示**:你的麦录到的实际是 `Wave Link Stream`(Wave Link 内部混完的最终录音轨);你的系统播放走 `System (Elgato Wave:3)` 的 loopback。开会场景一般这两个都勾,就够了。

### B. 上传文件

1. **上传文件** 标签 → 选文件 → 标签随便填 → **▶ 转写文件**
2. 整段离线 STT + CAM++ 全程一次跑完,直接出转写列表 + 校正面板

### C. 校正发言人

模型分得偏敏感所以可能多分。比如它分出了 `Mic-A` `Mic-B` `System-A` `System-B` 四个发言人,但你听完知道其实 Mic-A 和 Mic-B 都是你,System-A 和 System-B 是对方。把它们都重命名成 `Me` 和 `对方`,点 **应用并生成总结**。

留空表示保持原名。

### D. 总结结果

LLM 返回类似:

```json
{
  "topics": [
    {
      "title": "新登录页发布计划调整",
      "summary": "因压力测试未完成,决定推迟至周三发布生产环境",
      "viewpoints": [
        {"speaker": "Alice", "view": "原计划明天发布到生产环境"},
        {"speaker": "Bob",   "view": "担心压测未完,建议先在 staging 跑一晚"}
      ]
    }
  ],
  "todos": [
    {"owner": "Charlie", "task": "今晚完成 E2E 测试"}
  ]
}
```

页面会渲染成卡片,底部有"查看原始 JSON"可以折叠看。

## 调优(.env)

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | 788 | 服务端口 |
| `STT_DEVICE` | cuda | 改 cpu 也能跑,慢约 30 倍 |
| `SPK_SIM_THRESHOLD` | 0.55 | 说话人聚类余弦阈值,**越低越敏感**(分得越多);事后可以合并所以默认偏低 |
| `VAD_SILENCE_MS` | 500 | 静音多久判定句末 → 触发离线重解码 |
| `MAX_UTTERANCE_MS` | 15000 | 单句最长强制切;长讲话防卡 |
| `STREAM_CHUNK_MS` | 600 | 流式 chunk 大小,**别改**,paraformer-zh-streaming 就训练在 600ms 上 |

## 项目结构

```
LocalMeetingSum/
├── server.py            # FastAPI:设备 API / 文件上传 / WS 流 / LLM 总结
├── stt.py               # FunASR 2-pass 封装(streaming + offline + VAD + cam++)
├── audio_capture.py     # PyAudioWPatch 抓音频,WASAPI loopback
├── audio_decode.py      # ffmpeg 解码上传文件 → 16k mono PCM
├── summarizer.py        # 调 LLM,严格 JSON 输出
├── static/              # 前端单页 HTML + JS + CSS
├── requirements.txt
└── .env.example
```

## 已知坑(全部已绕过,记录在这供参考)

1. **sounddevice 不能做 WASAPI loopback**——它打包的 PortAudio 没编 loopback 符号。换 `pyaudiowpatch`。
2. **子线程开 WASAPI 流会回退到 WDM-KS**——必须先 `CoInitializeEx(NULL, 0)`。每个 `CaptureWorker.run()` 里都做了。
3. **多线程并发 `pa.PyAudio()` 会 segfault**——单例 + 锁。
4. **tqdm `sys.stderr.flush()` 在管道断开时 EINVAL,整推理炸**——用 `_SafeStream` 包 stdout/stderr。

## License

MIT
