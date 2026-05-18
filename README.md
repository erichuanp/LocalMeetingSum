# LocalMeetingSum

本地中文会议转写与总结。**STT 和 LLM 在你这台 GPU 机器上跑,音频从打开网页的设备上抓**——所以你可以从手机/Mac/任何电脑访问同一个服务,各自用各自的麦克风。

> 想象成 OpenAI Whisper 的私有部署 + Otter 的发言人区分 + LLM 自动总结,**全程数据不出你的局域网**。

## 关键特性

- **跨平台前端**:用浏览器 `getUserMedia` / `getDisplayMedia` 在前端抓音频,服务器端只做 STT 和总结
  - 手机 / 平板 → 自带麦克风
  - 桌面 Chrome → 任意麦克风 + 可选"屏幕音频(系统声音)"
- **流式 partial 实时出字**(~600ms 一段),VAD 检到句末用离线模型按整句双向 context 重解码 → 自动把"今天我去吃鱿鱼,但是有鱼很臭"修正成"鱿鱼很臭"
- **每个音源独立 STT,不同源绝对不会被并成同一个人**;某个源静音也不会无中生有冒发言人
- **说话人分得偏敏感,允许事后合并**——模型先分 ABCDE,你看完一键合并成"我 / 对方"再总结
- **总结结构化**:LLM 输出 `topics → 每个话题 → 每个发言人的观点` + `todos(谁要做什么)`,严格 JSON

## 架构

```
   ┌──────────────────────────┐                  ┌────────────────────────────┐
   │ 任何设备的浏览器          │                  │ 你的 GPU 机器(Windows)   │
   │                          │                  │                            │
   │ getUserMedia (麦克风)     │   WS 文本/二进制 │ FastAPI :788               │
   │   ↓                      │  ←──────────────→│  ↓                         │
   │ AudioWorklet 切 100ms 块 │                  │ FunASR 2-pass STT + CAM++ │
   │   ↓                      │                  │  ↓                         │
   │ 推 float32 PCM 给服务器  │                  │ OpenAI 兼容 LLM 端点(LM  │
   │ 显示 partial / 转写 / 总结│                  │ Studio / vLLM / Ollama)   │
   └──────────────────────────┘                  └────────────────────────────┘
```

## 系统要求

| 端 | 要求 |
|---|---|
| **服务器**(GPU 机器) | Windows 10/11 + NVIDIA GPU(≥6GB 显存)+ Python 3.11 + ffmpeg + 一个 OpenAI 兼容 LLM |
| **客户端**(任意设备) | 现代浏览器(Chrome / Edge / Firefox / Safari);要用麦克风必须 HTTPS 或 localhost |

> RTX 50 系(sm_120)的 PyTorch wheel 必须用 cu128,详见下方安装步骤。

## 安装(服务器端)

### 1. 装 ffmpeg

```powershell
choco install ffmpeg   # 或从 https://www.gyan.dev/ffmpeg/builds/ 解压加 PATH
ffmpeg -version
```

### 2. 装 Python 依赖

```powershell
conda create -y -n lms python=3.11
conda activate lms
pip install -r requirements.txt
```

`requirements.txt` 锁了 `--extra-index-url https://download.pytorch.org/whl/cu128`。RTX 50 系必须 cu128;30/40 系可以改成 cu124,功能一样。

### 3. 配置 LLM 端点

把 `.env.example` 复制成 `.env`,改成你自己的:

```env
LLM_BASE_URL=http://你的-llm-地址:1234/v1
LLM_API_KEY=任意值,LM Studio 不验
LLM_MODEL=qwen/qwen3.5-9b   # 任何你加载的中文模型
```

### 4. 启动

```powershell
python server.py
```

首次启动会自动从 ModelScope 下载约 1.2GB FunASR 模型到 `~/.cache/modelscope/`,加载完会打印 `[startup] FunASR models loaded`。

浏览器打开:

- 本机: <http://localhost:788>
- 局域网其他电脑: `http://<GPU 机器 IP>:788`
- 手机/平板 / 远程: **需要 HTTPS**,看下面的 HTTPS 一节

## 开机自启 + 局域网开放

```powershell
# 在管理员 PowerShell 里跑一次,把服务设成"用户登录后自动起",并开放防火墙端口 788
.\scripts\install_autostart.ps1
```

它会:

1. 注册一个名为 `LocalMeetingSum` 的计划任务,你登录 Windows 后自动起;崩溃自动重启 5 次
2. 在 Windows 防火墙开放 788/tcp 入站(域 + 专用网络;不开放 Public profile)

需要无登录的真"开机启动"(比如远程接入前):

```powershell
.\scripts\install_autostart.ps1 -AtStartup
```

卸载:

```powershell
.\scripts\install_autostart.ps1 -Uninstall
```

查看 / 操作任务:

```powershell
Get-ScheduledTask -TaskName LocalMeetingSum | Get-ScheduledTaskInfo
Start-ScheduledTask -TaskName LocalMeetingSum    # 立即起一次
Stop-ScheduledTask  -TaskName LocalMeetingSum
```

## HTTPS(手机访问麦克风必备)

`getUserMedia` 在非 HTTPS 域名下被现代浏览器禁用,除了 `localhost`。所以从手机/平板/远程机访问时,必须走 HTTPS。

### 方案 A:Tailscale Serve(强烈推荐,真证书,零配置)

```powershell
# 在 GPU 机器上,装好 Tailscale 后:
tailscale serve --bg --https=443 http://localhost:788
```

然后手机装 Tailscale 客户端登录同一个 tailnet,访问 `https://<机器名>.<tailnet>.ts.net`。Let's Encrypt 真证书,无警告。

公网放出(谨慎):

```powershell
tailscale funnel --bg --https=443 http://localhost:788
```

### 方案 B:自签证书(本地一次性绕过)

```powershell
.\scripts\make_self_signed_cert.ps1
# 把它打印的两行 SSL_CERTFILE / SSL_KEYFILE 加到 .env
# 重启 server.py
```

访问 `https://<IP>:788`,浏览器会警告"不安全",点"高级 → 继续访问"例外一次即可。手机也一样。

## 使用流程

### 实时录音

1. 打开 `https://...:788`,顶部 **实时录音** 标签
2. 点 **+ 添加音源** → 选 **麦克风** 或 **屏幕音频**
   - **麦克风**:浏览器请求权限(第一次),给完之后下拉列表里选具体设备 → **添加**
   - **屏幕音频**:浏览器弹分享对话框,**必须勾选"分享系统声音"**,然后选要分享的屏幕/标签
3. 添加完源,绿条实时跳动说明在收声;可以加任意多个源
4. 点 **▶ 开始录音转写**,partial 实时刷在橘色框,VAD 切到句末就把最终句子带说话人标签写进下面
5. 点 **■ 停止**,会自动弹"校正发言人"

### 上传文件

实时录音不便时(开过会的录音文件、Zoom 录像、微信语音转的 mp4...),走这个 tab。任何 ffmpeg 能解的格式都行(mp4/mov/mkv/m4a/mp3/wav/flac/opus/webm/3gp/aac...)。

### 校正发言人

模型分得偏敏感所以可能多分。比如它分出了 `Mic-A` `Mic-B` `Screen:sys-A` `Screen:sys-B`,但你听完知道其实 Mic-A 和 Mic-B 都是你,Screen 的两个是对方。把它们重命名成 `Me` 和 `对方` → **应用并生成总结**。

留空表示保持原名。

### 总结输出

```json
{
  "topics": [
    {
      "title": "新登录页发布计划调整",
      "summary": "因压力测试未完成,推迟到周三发布",
      "viewpoints": [
        {"speaker": "Me",   "view": "原计划明天上生产"},
        {"speaker": "对方", "view": "压测未完,建议先在 staging 跑一晚"}
      ]
    }
  ],
  "todos": [
    {"owner": "Charlie", "task": "今晚补 E2E 测试"}
  ]
}
```

## 调优(.env)

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | 788 | 服务端口 |
| `HOST` | 0.0.0.0 | 监听地址 |
| `SSL_CERTFILE` / `SSL_KEYFILE` | 空 | 设了就启用 HTTPS |
| `STT_DEVICE` | cuda | 改 cpu 也能跑,慢约 30 倍 |
| `SPK_SIM_THRESHOLD` | 0.55 | 说话人聚类阈值,越低越敏感 |
| `VAD_SILENCE_MS` | 500 | 静音多久判句末 |
| `MAX_UTTERANCE_MS` | 15000 | 单句最长强制切 |

## 项目结构

```
LocalMeetingSum/
├── server.py            # FastAPI:文件上传 / WS(JSON 控制 + 二进制 PCM) / LLM 总结
├── stt.py               # FunASR 2-pass(streaming + offline + VAD + cam++)
├── audio_decode.py      # ffmpeg 解码上传文件
├── summarizer.py        # 调 LLM,严格 JSON
├── static/              # 前端
│   ├── index.html
│   ├── app.js           # getUserMedia / getDisplayMedia 抓音频,WS 推 PCM
│   ├── pcm-worklet.js   # AudioWorklet 切 100ms 帧
│   └── style.css
├── scripts/
│   ├── install_autostart.ps1     # 注册计划任务 + 防火墙
│   └── make_self_signed_cert.ps1 # 生成自签证书
├── requirements.txt
└── .env.example
```

## 已知坑(记录在这供参考)

1. **手机 HTTP 不能用麦克风**——浏览器策略,必须 HTTPS。用 Tailscale Serve 最省事。
2. **iOS Safari 在后台会挂起 AudioContext**——把页面切回前台即可恢复。
3. **AudioContext 必须在用户手势(click)里创建**——已在"+ 添加音源 → 添加"按钮处理。
4. **tqdm `sys.stderr.flush()` 在管道断开时 EINVAL,会把整个推理炸**——已用 `_SafeStream` 包 stdout/stderr。
5. **第一次启动模型加载 ~80s**——已在 startup 钩子里后台预加载,启动后 `/api/health` 会返回 `models_loaded`。

## License

MIT
