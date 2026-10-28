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
| Linphone 账号 ×2 | 手机与 AI 各一个（App 内注册或 subscribe.linphone.org）；AI 账号只需注册，程序只向外拨号、不向服务器登记 | 免费 |
| Groq API key | 语音识别（console.groq.com） | 免费额度 |

### 云端 CC／官方 App 能用吗

打电话的服务（负责拨号、传声音的插件程序，不是你的 AI 本身）必须跑在 VPS 或自己的电脑上。云端 CC 的容器一般不放行 SIP 用的 UDP，装进去也拨不出去。

- **VPS＋Claude Code**：最方便，按下面的步骤基本一键直通。
- **自己的电脑**：需要按自己的系统（Windows／macOS／Linux）适配一下，可参考 `docs/` 里的各平台说明。
- **云端 CC 或官方 App**：需要在现在的方案上改一下。这个插件本质上是一个 MCP，把现在的本地（stdio）形式改成 HTTPS 形式，打电话的服务照样跑在 VPS／电脑上，再用 Cloudflare 隧道做内网穿透，云端的 AI 就能作为远程连接器连过去打电话。急用的话可以让你的 AI 照这个思路自己改；我们也在做这个版本，不想折腾可以等我们做出来，会尽快。

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
```

### 网络

无需放行端口：服务器先向外发送音频并借助 STUN 穿越 NAT，实测在安全组中拒绝 UDP 4000-4100 入站后，通话仍双向正常。如接通后听不到声音，先确认 Linphone 处于前台；仍无声音时，可尝试在防火墙或安全组中放行 UDP 4000-4100（RTP 音频）。

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
| 能接入 Aru 吗 | Aru Host 装在 VPS 上并以 Claude Code 为驱动时，在该机 Claude Code 中安装本插件应可使用（未实测）；Aru App 支持远程 MCP，远程版完成后可直接添加。打电话的服务仍需运行在 VPS 或自己的电脑上 |

## 感谢

- **[Linphone](https://www.linphone.org/)**：提供免费 SIP 账号与客户端。sip.linphone.org 仅承载信令，音频由服务器与手机直连（尚未抓包验证）。如使用量较大，建议支持 Linphone 或自建 SIP 服务器。
- **[Ringback](https://github.com/mohitbadwal/ringback)**（Apache 2.0）：本项目上游。

## 致谢

调研与构思：Cora 和 South；实现：Claude

## 许可证

[AGPL-3.0-or-later](LICENSE)。本项目派生自 Ringback，上游代码以 Apache 2.0 授权，原许可证与 NOTICE 保留于 [LICENSE-APACHE-2.0](LICENSE-APACHE-2.0) 和 [NOTICE](NOTICE)。依赖的 pjproject 为 GPL-2.0-or-later。
