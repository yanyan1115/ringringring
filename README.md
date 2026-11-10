# ringringring：AI 拨打你的手机进行语音通话

中文 | [English](README.en.md)

**0 成本实现 iOS 原生通话。** 无需 Mac，无需 99$ 开发者账号，无需购买或编写 iOS 原生 App，无需配置数据库——iPhone 手机上装个免费的 Linphone，你的 AI 就能给你打电话。

<p align="center">
  <img src="docs/images/incoming-lockscreen.jpg" width="240" alt="锁屏来电">
  <img src="docs/images/in-call.jpg" width="240" alt="通话中">
</p>
<p align="center">
  <img src="docs/images/incoming-in-game.jpg" width="560" alt="游戏中来电">
</p>

无需购买号码，无按分钟计费；语音识别与语音合成均使用免费额度。仅需一台已在运行 AI 的 Linux 服务器。iPhone 熄屏或 Linphone 处于后台时同样可以来电。

如果对你有帮助，欢迎点个 Star 鼓励作者～⭐

## 功能

- **双向语音通话**：AI 拨打手机，接通后进行多轮对话。用户语音经识别转为文字交给 AI，AI 的回复经合成后播放。
- **插话打断**：AI 说话期间用户开口，播放立即停止，并返回已说出与未说出的部分。
- **单线路锁**：同一主机上的多个 AI 入口共享一条线路，通话中再次拨号将返回忙线。
- **隐私默认值**：日志仅记录接通、挂断、错误类别与转写文本；手机网络地址默认打码，以降低其进入模型上下文的风险（不构成对服务器管理员的强隔离）。
- **可替换的识别与合成**：语音识别与语音合成均可配置，详见「替换语音识别与语音合成」。

## 前置条件

| 项目 | 说明 | 费用 |
|---|---|---|
| Linux 服务器 | x86_64，Python 3.11；即运行 Claude Code／OpenCode 的主机 | 已有 |
| Linphone | 手机端 App（App Store／各应用商店） | 免费 |
| Linphone 账号 | 1 个必需（手机上用，App 内注册或 subscribe.linphone.org）；第 2 个可选：给 AI 用，你想主动拨给 AI 才需要（见「接电话」） | 免费 |
| Groq API key | 语音识别（console.groq.com） | 免费额度 |

### 云端 CC／官方 App 能用吗

打电话的服务（负责拨号、传声音的插件程序，不是你的 AI 本身）必须跑在 VPS 或自己的电脑上。云端 CC 的容器一般不放行 SIP 用的 UDP，装进去也拨不出去。

- **VPS＋Claude Code**：最方便，按下面的步骤基本一键直通。
- **自己的电脑**：需要按自己的系统（Windows／macOS／Linux）适配一下，可参考 `docs/` 里的各平台说明。
- **云端 CC 或官方 App**：需要在现在的方案上改一下。这个插件本质上是一个 MCP，把现在的本地（stdio）形式改成 HTTPS 形式，打电话的服务照样跑在 VPS／电脑上，再用 Cloudflare 隧道做内网穿透，云端的 AI 就能作为远程连接器连过去打电话。急用的话可以让你的 AI 照这个思路自己改；v0.2.0 已经带了这个版本，见下文「HTTPS 远程 MCP（OAuth）」；一键安装脚本还在计划中。

## 安装

**方式一：在 Claude Code 中安装插件（推荐）**

```
/plugin marketplace add yanyan1115/ringringring
/plugin install ringringring@ringringring
```

安装时按提示填写 SIP 账号、密码与语音识别 key（未提示时可执行 `/plugin configure ringringring@ringringring`）。插件首次启动时自动下载并安装运行环境（实测约 15 秒），无需手动执行脚本。如服务器缺少系统包，插件启动失败并给出一条 `sudo apt-get install …` 命令，执行后在 `/mcp` 中重新连接即可。

**方式二：手动安装**

```bash
git clone https://github.com/yanyan1115/ringringring.git && cd ringringring
# 检查缺失的系统包（不做任何修改）
plugin-native/bin/install.sh --check
# 安装；加 --no-apt 则不调用 apt，仅列出缺失的包
plugin-native/bin/install.sh --pjsua2 https://github.com/yanyan1115/ringringring/releases/download/v0.1.0/pjsua2-2.17-linux-x86_64-cp311.tar.gz --src .
```

两种方式的安装目录均为 `~/.local/share/ringringring`，配置文件为 `~/.config/ringringring/voice.env`（权限 600）。

### 配置

以插件方式安装时，在安装提示中填写即可。也可编辑 `~/.config/ringringring/voice.env`，至少填写以下各项（插件设置中已填写的项优先）：

```bash
export VOICE_SIP_ID="sip:<AI 账号>@sip.linphone.org"
export VOICE_SIP_USER="<AI 账号>"
export VOICE_SIP_PASS="<密码>"                       # 手动配置时凭据仅保存在此文件
export VOICE_SIP_CALLEE="sip:<手机账号>@sip.linphone.org"
export VOICE_DISPLAY_NAME="Claude"                   # 来电显示名称（部分手机不显示 emoji）
export VOICE_STT_KEY="<Groq key>"                    # 或以 VOICE_STT_ENV_FILE 指向单独的文件
export VOICE_STT_LANG="zh"
export VOICE_STUN="stun.linphone.org:3478"            # 让音频穿过 NAT；插件安装时已默认填好
```

### 网络

无需放行端口：服务器先向外发送音频并借助 STUN 穿越 NAT，实测在安全组中拒绝 UDP 4000-4100 入站后，通话仍双向正常。如接通后听不到声音，先确认 Linphone 处于前台；仍无声音时，可尝试在防火墙或安全组中放行 UDP 4000-4100（RTP 音频）。

接通后两边都听不到声音、服务器一直往外发音频却一个包都收不到：多半是手机开着代理／VPN，而它不转发 UDP（国内用户挂梯子时常见）。按这个顺序试：① 关掉代理直接打，国内用户实测不开代理就能打通；② 需要一直开着代理的，在代理里把 linphone.org 和你自己服务器的 IP 设成直连（分流），这一条还没有实测；③ 以上都不行，再在服务器上为通话音频搭一层桥接，多绕一段路，延迟会高一些。

## 接入 AI

**Claude Code**

按上文「安装」方式一安装插件即可。也可以直接加载本地目录：`claude --plugin-dir <仓库路径>/plugin-native`。

加载后提供 `call_start`、`converse`、`call_end` 等工具，以及 `phone-call` skill（拨号、对话、挂断流程与隐私约定）。

**OpenCode 及其他 MCP 客户端**

将 `plugin-native/bin/run-voice-native.sh` 配置为 stdio MCP 服务器。AI 与电话服务不在同一主机时，可通过 SSH 在电话服务所在主机上启动该脚本。

## 替换语音识别与语音合成

**语音识别（STT）**：支持任意兼容 OpenAI `/audio/transcriptions` 接口的服务。

```bash
# 默认：Groq
export VOICE_STT_URL="https://api.groq.com/openai/v1/audio/transcriptions"
export VOICE_STT_MODEL="whisper-large-v3-turbo"
# OpenAI：
# export VOICE_STT_URL="https://api.openai.com/v1/audio/transcriptions"
# export VOICE_STT_MODEL="whisper-1"
export VOICE_STT_KEY="<key>"
```

**语音合成（TTS）**

```bash
# 默认：Microsoft Edge TTS（zh-CN-YunxiNeural）
export VOICE_TTS="edge"
# 自定义命令：{text} 替换为待合成文本，{out} 替换为输出音频路径。
# 命令不经 shell 执行，不支持管道与重定向；如有需要请封装为脚本。
# export VOICE_TTS_CMD="python3 my_tts.py --text {text} --out {out}"
```

## v0.2.0 新功能

以下功能都是可选的，不装不影响原来的拨出电话。变量名与示例见 `voice.env.example` 末尾的 v0.2.0 部分。

### 接电话（来电插件 `plugin-inbound`）

之前只能 AI 拨给你，现在你也可以拨 AI：在 Linphone 里拨 AI 的号，手机一直响，AI 收到 `incoming_call` 事件后调用 `answer` 接听，用 `talk` 聊、`hang_up` 挂；AI 想找你时用 `dial`（同一个号，在你手机上是同一个联系人）。

```
/plugin install ringringring-inbound@ringringring
```

需要先装好上面的 ringringring（它提供 pjsua2 和引擎代码）。AI 的号在 `~/.config/ringringring/inbound.env` 里配置（`VOICE_INBOUND_SIP_ID` / `_USER` / `_PASS`，以及 `VOICE_INBOUND_RING_TIMEOUT`、`VOICE_INBOUND_RTP_PORT`）；`voice.env` 里的 `VOICE_SIP_ID` 是你自己的地址，用来认来电人。这一个号会向 Linphone 服务器登记，才能收到来电。

**两道身份检查**：任何人知道 SIP 地址都能打，来电显示也能伪造，所以来电要同时通过：① 来电的 `用户名@域名` 必须和 `VOICE_SIP_ID` 完全一致；② 来电设备必须在 `~/.config/ringringring/trusted_devices` 里（每行一个 Linphone 设备 id 的哈希，只存 sha256 前缀，每次来电现读，改了不用重连）。没通过的直接回忙线，不会叫醒 AI。名单为空时**不拦任何人**，只在日志里写下 `device=<哈希> (not enforced)`，让你把自己的设备哈希抄进名单；也就是说名单为空时只认 SIP 地址，是可以被伪造的，请填好名单。设备 id 不是密码学证明，通话里不要说密钥之类的隐私。

**打电话前先等几秒**：刚切回 Linphone 时它还没连好，第一个邀请里没有设备 id（日志里是 `device=none`），会被拒绝，表现为响一声就断；等几秒再拨即可。重装 Linphone 会换设备 id，需要重新加入名单。

### 安静听、等声音、有声就推（默认关）

让 AI 在通话里不出声，只听你这边：设 `VOICE_QUIET_LISTEN=1` 后，接通期间会在后台持续录音（只放内存和临时文件，挂断即丢弃）。

- `quiet_listen(seconds)`：攒够这么多秒后返回"分:秒 转写"，全静则返回 `[SILENCE]`。
- `wait_for_voice(max_sec)`：什么都不说地等着，一出声就立刻返回（不转写，录音原样保留，之后可再用 `quiet_listen` 取字）。
- 再设 `VOICE_PUSH_ON_VOICE=1`：声音一响就主动推一条通知给 AI，不用它来轮询。

这些开关默认都是关的。因为会持续录音，请自己决定要不要开。

### ElevenLabs Scribe 识别与听法

除了默认的 Groq／OpenAI 兼容接口，语音识别可换成 ElevenLabs Scribe（`VOICE_STT_11LABS_KEY` 或 `VOICE_STT_11LABS_ENV_FILE`，另有批量与实时流式两种，见 `voice.env.example`）；没有 key 就不启用这一层。

听法 `VOICE_STT_MODE` 有三档：`text` 只给 AI 转写文字（默认）；`audio` 把你说的原声作为音频附上，不给文字；`both` 两者都给。AI 拨号时可以用 `dial(hear=...)` 为整通电话选一档，通话中用 `set_hearing` 改。AI 不是原生多模态、不能听音频时请用 `text`。

### 语音识别推荐与我们的配置

推荐用 ElevenLabs 做语音识别：免费版每月 1 万额度，用来做电话听写完全够用，而且精度很高。

作者自己用的是三层，前一层失败才往下走：
1. ElevenLabs Scribe（代码默认 `scribe_v2`；语言参数用 ISO-639-3，中文是 `zho` 而不是 `zh`）
2. Groq `whisper-large-v3-turbo`
3. 本机 whisper.cpp：默认挂的是 `.en` 英文模型，只能保证断句，**中文基本认不出**，只当最后一道兜底

三层可以只开其中一层：只配 Groq 或只配 ElevenLabs 的 key 都能用，都不配就只走本机。

### HTTPS 远程 MCP（OAuth）

适配 Claude、Gemini、ChatGPT 的 APP/网页端。

给 Gemini 网页版、ChatGPT 等只能连 HTTPS 远程连接器的 AI 用：`plugin-inbound/bin/run_remote.sh`（即 `remote_http.py`）在你的主机上以 HTTPS 远程 MCP 方式提供 `dial`、`talk`、`quiet_listen`、`hang_up`（只打出去，没有接听；只回文字，不传原声）。认证是 OAuth（动态注册、PKCE，外加一个"连接口令"页面：连接时输入你设的口令）。

做法：让服务只监听 `127.0.0.1`，用 Cloudflare 隧道（或任意 HTTPS 反代）把一个域名转发过来；设置 `MCP_OAUTH_ENABLED=1`、`MCP_OAUTH_ISSUER_URL`、`MCP_OAUTH_CONNECT_SECRET`（口令，自己保管）、可选 `MCP_ALLOWED_HOSTS`。OAuth 令牌默认存在 `~/.local/share/ringringring/oauth/`。未开 OAuth 时，服务拒绝监听非本机地址。一键安装脚本在计划中，目前需要手动配置。

致谢：https 远程 MCP 的 OAuth 部分改编自 South 的项目。

### 声音标签接口（实验，默认关）

可选：把每一轮的音频 POST 给你自己运行的声音分类器，把它返回的标签（比如呼吸、咳嗽）以 `[heard: ...]` 附在转写后面。仓库里**不含分类器**，只有调用这一侧：接口是 POST 一段 WAV（请求头 `X-Sound-Key`），返回标签字符串的 JSON 列表。在通话引擎一侧设 `VOICE_SOUND_EVENTS=1`、`VOICE_SOUND_URL`、`VOICE_SOUND_KEY` 即可；任何一项缺失就完全不生效。

### 手机端麦克风小技巧

如果 AI 听不到你这边很轻的声音（呼吸、小声嘀咕），在 Linphone 通话中打开**扬声器（免提）**，并在系统的麦克风模式里选 **Wide Spectrum（宽谱）**（iPhone：通话时拉出控制中心，点"麦克风模式"）。这样手机会收更宽的声音而不是只留人声，轻的声音更容易被听到。

## iPhone 设置

1. 安装后先在前台接听一次，此后熄屏或后台状态均可来电。
2. 设置 → 辅助功能 → 触控 → 开启「防止锁定后结束通话」，避免通话中误触锁屏键挂断。
3. 不要将 AI 的号码存入通讯录，否则来电显示为通讯录名称而非 `VOICE_DISPLAY_NAME`。
4. 通话记录里找不到 AI 的来电：记录在「未知来电」里。设置 → App → 电话 → 来电过滤 → 关闭「未知来电」即可。
5. 锁屏时接听是系统原生通话界面，解锁状态下接听会进入 Linphone 界面；此时按侧边键锁屏即切到原生界面，通话不中断（需开启第 2 条）。

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| 已接听但返回 NO ANSWER | Linphone 在后台被系统挂起；将 App 切至前台后重试 |
| 返回 LINE BUSY | 同一主机上另一 AI 入口正在通话；无需重复拨号 |
| 转写延迟高或为空 | 检查 STT key 与额度 |
| 人名、专有名词识别错误 | 免费模型的已知局限；可替换 STT 服务 |
| 想在 Mac 上运行 | 不是一键安装：Apple Silicon 参考 [docs/SETUP_MACOS.md](docs/SETUP_MACOS.md)，或使用 Docker（[docs/SETUP_DOCKER.md](docs/SETUP_DOCKER.md)）；已有用户在 Mac mini 上跑通。Release 中的预编译包仅支持 Linux x86_64 |
| Mac 上 libInit 报 259804（SRTP 初始化失败） | flat_namespace 使 OpenSSL 符号绑定到系统 LibreSSL；改用 Docker，或编译 SWIG 绑定时加 `-std=c++11`。不要关闭 SRTP，否则对方返回 488 |
| 能接入自建前端吗 | 前端的后端若在 VPS 上以 Claude Code 等运行，在该机安装本插件应可使用（未实测）；前端若支持远程 MCP，远程版完成后可直接添加。打电话的服务仍需运行在 VPS 或自己的电脑上 |

### 自己写或改 pjsua2 配置时

用本插件默认已配好，下面几条在自己写代码、手写 `voice.env` 时才会遇到。

| 现象 | 原因与处理 |
|---|---|
| 注册一直超时，或注册成功后拨号没有下文 | 只向外拨号的账号不要注册：`acfg.regConfig.registrarUri = ""` |
| 拨号没反应，日志也看不出原因 | 先创建 TLS transport，proxy 要带 transport：`acfg.sipConfig.proxies.append("sip:sip.linphone.org;transport=tls")` |
| `makeCall()` 后进程直接退出，退出码 134，没有异常和堆栈（有用户遇到） | SRTP 设在账号的媒体配置上：`acfg.mediaConfig.srtpUse = pj.PJMEDIA_SRTP_MANDATORY`。`EpConfig` 和 `TransportConfig` 上没有这一项 |
| 能接通、有回铃，但双方都听不见，SDP 的 `c=` 行是 `192.168.x.x` 这样的内网地址 | 没配 STUN。手写 `voice.env` 时加 `export VOICE_STUN="stun.linphone.org:3478"`；自己写代码时用 `epCfg.uaConfig.stunServer.append("stun.linphone.org:3478")` |

## 感谢

- **[Linphone](https://www.linphone.org/)**：提供免费 SIP 账号与客户端。sip.linphone.org 仅承载信令，音频由服务器与手机直连（尚未抓包验证）。如使用量较大，建议支持 Linphone 或自建 SIP 服务器。
- **[Ringback](https://github.com/mohitbadwal/ringback)**（Apache 2.0）：本项目上游。

## 致谢

调研与构思：Cora 和 South；实现：Claude

## 许可证

[AGPL-3.0-or-later](LICENSE)。本项目派生自 Ringback，上游代码以 Apache 2.0 授权，原许可证与 NOTICE 保留于 [LICENSE-APACHE-2.0](LICENSE-APACHE-2.0) 和 [NOTICE](NOTICE)。依赖的 pjproject 为 GPL-2.0-or-later。
