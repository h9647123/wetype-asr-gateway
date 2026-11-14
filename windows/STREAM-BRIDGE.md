# Windows 流式桥接扩展的来源与复现

本项目 v0.2.0 配套的 stream-capable portable bridge 标记为 **0.3.4**。它基于 [kuxiaowo/wechat-asr-bridge](https://github.com/kuxiaowo/wechat-asr-bridge) 的 MIT 实现，是本项目提供的本地修改构建，不表示上游官方已经发布或维护这一扩展版本。

- 上游固定提交：`bce9b6b4825e6fb8c7a9d0d02a0d31a90dd3d2f2`。
- 变更交付：本目录 [`bridge-stream.patch`](bridge-stream.patch)，共10个源码、测试及构建文件；不包含 `dist`、构建缓存、用户配置或凭据。
- 上游 MIT 许可证：[`UPSTREAM-BRIDGE-LICENSE.txt`](UPSTREAM-BRIDGE-LICENSE.txt)，保留 Copyright (c) 2026 kuxiaowo。
- 新能力：带 Bearer 鉴权的 `/v1/audio/stream`、每个会话持续 encoder、500ms 原生分包、可修订 snapshot、明确 final 与安全清理；原 multipart 转写行为保留。补丁内 `docs/WETYPE-STREAM.md` 说明完整协议及限制。

本次生产部署与 CAM 真人验收已完成，覆盖范围见[验收记录](../docs/acceptance-20261114.md)。配套 portable ZIP 作为 [v0.2.0 Release](https://github.com/h9647123/wetype-asr-gateway/releases/tag/v0.2.0) 资产；这不代表所有输入法版本或网络环境均已验证。

## 固定源码并应用补丁

在 Windows x64 上安装 Git、Python 3.12 和可选 Node.js，然后在同一个父目录获取两个源码仓库。已有工作区请先备份，不要将下列示例当作覆盖现有部署的升级脚本。

```powershell
git clone https://github.com/h9647123/wetype-asr-gateway.git
git clone https://github.com/kuxiaowo/wechat-asr-bridge.git
Set-Location wechat-asr-bridge
git checkout --detach bce9b6b4825e6fb8c7a9d0d02a0d31a90dd3d2f2
git rev-parse HEAD
$streamPatch = (Resolve-Path ../wetype-asr-gateway/windows/bridge-stream.patch).Path
git apply --check $streamPatch
git apply $streamPatch
```

`git apply --check` 不通过时应停止并核对上游提交及工作区状态，不能以忽略冲突或覆盖文件绕过。补丁只针对上述固定提交验证。

## 依赖、测试与构建

```powershell
py -3.12 -m venv .venv-build
& .venv-build/Scripts/python.exe -m pip install -r requirements.txt -r requirements-build.txt

# Python 单测和原生回调所有权模拟；不附加输入法、不采集麦克风
& .venv-build/Scripts/python.exe -m unittest discover -s tests -p 'test_wetype*.py' -q
node tests/verify_wetype_agent.cjs

# 可运行完整 unittest discovery，覆盖其他后端和管理逻辑
& .venv-build/Scripts/python.exe -m unittest discover -s tests -q

# 生成 portable 目录、ZIP、依赖许可及 SHA256SUMS
& .venv-build/Scripts/python.exe packaging/build_portable.py
```

默认产物位于上游工作区的 `dist/`。不同 Python、依赖或打包器版本可能改变二进制哈希；上述步骤是源码级可复现路线，不承诺不同机器逐字节生成相同 ZIP。分发时必须同时保留生成的上游 MIT 与各依赖许可证；FFmpeg 等依赖维持各自分发要求。

构建测试使用 mock 原生 agent，不能证明特定 WeType 版本或网络可用。不得自动下载/升级微信输入法、变更现有版本锁定或把未识别出文字的连接握手当作真实 ASR 验收。

## 部署与分发边界

本扩展及 portable **绝不包含腾讯输入法二进制、Windows 系统、腾讯账户、云端票据或用户配置**。使用者需自行取得合法授权的软件及网络服务访问权。原生识别仍通过已初始化 Windows WeType 进程完成，并非离线模型。

升级只替换经过备份和校验的 bridge 文件，保留私有 key、监听地址、任务配置和当前 WeType 版本锁定。等待旧会话安全清理后再停止 bridge，先验收 Windows 流式与原 HTTP，再启用 Linux 新实时路径；不要自动重装输入法或重置凭据。Linux配对与协议见 [`../docs/streaming.md`](../docs/streaming.md)。
