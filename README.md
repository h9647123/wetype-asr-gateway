# WeType ASR Gateway

**中文** | [English](README.en.md)

**v0.2.0：真实流式识别。** 配套 Windows bridge 0.3.4 与 Linux 网关已完成 WeType 3.0.0.38 的服务器及 CAM 真人验收，支持录音期间更新草稿。见 [Release](https://github.com/h9647123/wetype-asr-gateway/releases/tag/v0.2.0)、[验收范围](docs/acceptance-20261114.md)与[升级说明](docs/streaming.md)。

把 Windows 微信输入法的语音识别，通过 Ubuntu 网关接成文件 ASR 和讯飞 V2 风格的 WebSocket 接口，并提供局域网运维控制台。

本项目是部署与协议适配层。实际微信语音桥接由 [kuxiaowo/wechat-asr-bridge](https://github.com/kuxiaowo/wechat-asr-bridge) 提供，**不是本项目自行实现的识别引擎**。识别需要腾讯云连接，不是离线模型，也不需要讯飞云账户。

## 能做什么

- 保留 `POST /api/asr`：multipart 文件和 JSON base64 上传。
- 通过 Nginx 暴露上游 OpenAI 风格的 `/v1/models`、`/v1/audio/transcriptions`，模型为 `wetype-voice`。
- `WS /v1`：讯飞 V2 风格 HMAC-SHA256 鉴权、0/1/2 分段音频和 base64 编码结果；16 kHz / 单声道 / 16 bit PCM 可在录音结束前收到可修订的实时全文草稿。
- 局域网控制台：API key 权限和用量、默认隐藏凭据、自动健康检查、失败详情、可选的 ASR / Search 重启按钮。
- 配套 Windows SYSTEM 开机任务、Linux systemd / Nginx 示例，现有博客首页可以保留。
- 可选 Bing RSS Search wrapper，输出 Tavily 风格 JSON；它不是 Tavily 官方服务或 SearXNG。

## 能力边界

**v0.2.0 的实时路径需要配套 stream-capable Windows portable bridge 0.3.4**，不能只升级 Linux 网关后继续使用未扩展的上游 0.3.3。扩展源码及可复现来源随本项目提供，安装与现有部署升级见 [Windows 说明](docs/windows.md) 和 [流式适配](docs/streaming.md)。

16 kHz / 单声道 / 16 bit raw PCM 通过双向 WebSocket 边传边识别，中间结果是可替换的整段草稿，最终结果以原生结束标记确认；不保证每个字或每个音频包都返回文字。其他受支持的 PCM 格式仍缓存完整音频后使用 HTTP 转写。总音频最长 60 秒，包含录音尾音；不支持 MP3/lame、热词、说话人分离或精确时间戳。OpenAI 文件接口和旧 `/api/asr` 保留，由上游解码，最大 25 MiB。公开版**不写入录音归档**，音频和诊断帧时刻仅在请求内存中保留。

语言和口音参数不能强制切换 WeType 识别行为。上游依赖内部接口，升级可能失效。下面列出的实测版本与结果只是验收记录，不是对其他版本或识别准确率的保证。

```mermaid
flowchart LR
    C[客户端 HTTPS / WSS] --> N[Nginx / 可选 Tunnel]
    N --> L[旧接口与局域网控制台]
    N --> I[讯飞格式适配器]
    N --> B[Windows WeChatASRBridge]
    L --> B
    I -->|PCM + snapshot 双向 WS| B
    B --> W[WeType 语音接口]
    W --> T[腾讯云识别]
```

## 快速开始

需要 **Python 3.12**。现有控制台使用标准库 `cgi`，Python 3.13 及以上当前不支持。Windows 桥接器与 Linux 网关分别安装；仅启动本仓库不会自动创建 Windows VM 或安装识别引擎。

```bash
git clone https://github.com/h9647123/wetype-asr-gateway.git
cd wetype-asr-gateway
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# VM_IP 替换为专用 Windows 虚拟机的私有地址
.venv/bin/python scripts/init_config.py --upstream http://VM_IP:8765
```

初始化会生成被 Git 忽略的 `data/`，目录 700、文件 600，包含私有配置、console token、key 摘要和 `bridge-api-key.txt`。**重复初始化会拒绝覆盖现有凭据**。在 Windows 中使用同一个桥接 key，参见 [Windows 安装](docs/windows.md)。已有桥接 key 时可传 `--upstream-key-file /private/key.txt`。

分别在两个终端运行：

```bash
# 终端 1：默认只监听 127.0.0.1:18080
.venv/bin/python -m gateway.console

# 终端 2：默认只监听 127.0.0.1:8768，关闭访问日志以免记录签名 URL
.venv/bin/python -m uvicorn gateway.iat:app --host 127.0.0.1 --port 8768 \
  --no-access-log --ws-max-size 1048576 --ws-max-queue 4 --limit-concurrency 12
```

在本机生成登录链接并在浏览器打开。链接包含管理凭据，勿分享、截图或写入普通日志：

```bash
.venv/bin/python scripts/console_url.py
```

选择 **ASR** 查看配置；选择 **API keys** 查看健康、创建带 `asr` / `search` scope 的调用 key。开箱默认不授予重启权限，按钮显示“重启未启用”。启用方式见 [部署与管理](docs/deployment.md)。

## 接口示例

假设已将 Nginx 配置到 `https://asr.example.com`。环境变量中的 key 应由私有配置读取，下面没有可用的实际凭据。

```bash
# OpenAI 风格；使用 ASR 页的桥接专用 key
curl https://asr.example.com/v1/audio/transcriptions \
  -H "Authorization: Bearer $WETYPE_ASR_API_KEY" \
  -F model=wetype-voice -F file=@recording.wav

# 旧接口；使用拥有 asr scope 的控制台 key
curl https://asr.example.com/api/asr \
  -H "Authorization: Bearer $ASR_CLIENT_KEY" -F file=@recording.wav

# 讯飞格式示例，读取本机私有 config.json，不打印密钥或签名 URL
.venv/bin/python scripts/iat_client.py recording.wav data/config.json

# 模拟实时音频，并显示变化中的整段草稿（会输出识别文本）
.venv/bin/python scripts/iat_client.py recording.wav data/config.json --realtime --partials
```

公网初始化可添加 `--public-base https://asr.example.com`。已有配置应在备份后编辑 `legacy_url`、`openai_base_url`、`iat_url`，不要重新生成 key。后端 `upstream` 仍为私有 VM 地址，不应绕到公网。

签名、帧格式与明确不支持的参数见 [协议说明](docs/protocol.md)。Linux 开机启动、受限重启权限和保留博客的 Nginx 配置见 [部署与管理](docs/deployment.md)。

## 参考项目与来源

| 来源 | 本项目中的作用 | 许可证 / 说明 |
|---|---|---|
| [kuxiaowo/wechat-asr-bridge](https://github.com/kuxiaowo/wechat-asr-bridge) | **实际 ASR 后端**：Windows portable bridge、OpenAI 风格文件接口、WeType 语音桥接；本仓库提供外围部署、流式扩展及协议适配 | MIT；原型 v0.3.3，研究基线提交 `bce9b6b4825e6fb8c7a9d0d02a0d31a90dd3d2f2`；配套发行版提供自建扩展 portable，保留上游归属，不含腾讯组件 |
| [yu1745/wetype-ime-linux](https://github.com/yu1745/wetype-ime-linux) | Linux 微信输入法可行性调研参考；**不作为本项目运行依赖，没有复制其实现** | GPL-3.0；调研提交 `3a127ef2cb5a9d9754e6307f462b188d8f31b878` |
| [讯飞实时语音转写 V2 文档](https://www.xfyun.cn/doc/spark/spark_zh_iat_new.html) | HMAC 签名和 WebSocket 数据格式参考 | 本地只实现列出的协议子集；无讯飞云凭据、账户或额度依赖 |
| [modelscope/FunASR](https://github.com/modelscope/FunASR/blob/main/README_zh.md) | 官方文档列出的公开中文示例音频，用于公网验收 | 仅引用来源和测试结果，不打包模型、音频或 FunASR 源码 |
| [微信输入法官网](https://z.weixin.qq.com/) | Windows 微信输入法安装包来源 | 专有软件，需自行获取并遵守其许可；不随本仓库分发 |

完整归属和分发边界见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。项目不是腾讯或讯飞的官方产品。

## 验证状态

部署原型在 Ubuntu 24.04 / KVM、Windows 10 Enterprise LTSC 2021、WeType 3.0.0.38、WeChatASRBridge 0.3.3 上完成了真实验收：

- 6.28 秒测试音频、43.96 秒重复音频完整转写；VM 重启后无需桌面登录仍可识别。
- 网上下载的 5.547 秒中文示例，通过公网 OpenAI、旧接口、WSS 三种方式都返回相同全文。
- 拒绝无 key、错误 HMAC、错误 App ID 和公网管理请求；重启操作完成后重新验证了公网转写。
- 不公开部署日志、实际域名、IP、凭据、截图或虚拟机镜像。

本仓库将原型配置参数化，并收紧可选重启权限。离线自动化测试不调用腾讯云；真实环境结果与 CI 测试范围分开记录，见 [验证说明](docs/verification.md)。

本轮流式研究另在本机 WeType 3.0.1.35 上以公开预录音验证：500ms 分包、持续 encoder、录音结束前的中间文字与最终结束确认。它不代表任意版本、真人麦克风、所有公网条件或60秒录音已完成验收；现有 VM 版本不因升级网关而自动更换。

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## 许可证

本仓库原创代码采用 [MIT](LICENSE)。上游项目和 Windows / WeType 软件维持各自许可证。使用者需自行提供有合法使用权的 Windows 环境；本仓库不提供安装镜像、激活密钥或激活绕过方法。
