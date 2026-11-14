# v0.2.0 实时 CUSTOM IAT 适配

## 组件配对与升级

实时识别需要本项目配套的 **stream-capable Windows portable bridge 0.3.4**，在私有 VM 地址提供已鉴权的 `WS /v1/audio/stream`。原版上游 0.3.3 只有文件 HTTP 接口，不能提供该能力。扩展保持上游 MIT 归属，发行包不附带 Windows、微信输入法或腾讯二进制。验收后 portable 将随 [v0.2.0 Release](https://github.com/h9647123/wetype-asr-gateway/releases/tag/v0.2.0) 提供；源码补丁与复现方式见 [Windows 扩展说明](../windows/STREAM-BRIDGE.md)。

升级前备份现有配置、bridge 目录与 VM。停止接收新会话并等当前原生任务清理后，部署兼容 bridge，再更新 Linux 网关；保留当前 WeType 版本和锁定策略，不运行新的 WeType 安装器。原 `upstream` 地址和 API key 保持不变，默认从 `upstream` 的 HTTP/HTTPS 地址推导对应 WS/WSS 地址。使用 HTTP upstream 时仅在受控的私有网络传输；不要把未经 TLS 保护的 Bearer 凭据发到公网。

如果旧 `upstream` 指向另一个只支持 HTTP 的包装服务，可在私有 `config.json` 增加可选 `stream_upstream`，值为真正 Windows bridge 的 HTTP/HTTPS根地址，例如 `http://VM_IP:8765`。网关优先使用它推导 `/v1/audio/stream`，不改变旧 `upstream`、控制台、文件 ASR 或 API key。省略或留空时沿用 `upstream`；不要填写完整 WebSocket 路径或将新初始化当作覆盖旧配置的方法。

## 两层协议

客户端继续使用 `/v1` 的讯飞风格 HMAC、JSON/base64 PCM 和 status 0/1/2。仅16 kHz、单声道、s16le走实时路径；其他原支持格式保留缓存后 HTTP 转写。原 `/api/asr`、文件转写与控制台不改为流式。

网关与 Windows 使用 Bearer 鉴权的双向 WebSocket：上游先给 `ready`，网关发送二进制 PCM，Windows 用同一个 owner 和 encoder 持续编码、按约500ms批次发给原生 WeType。回包为 `snapshot`（revision、整段 text、final）；输入结束发送 `{"type":"end"}`，取消时尽力发送 `{"type":"cancel"}` 并关闭连接。网关把 snapshot 映射为固定 `sn=0` 的替换结果。完整外部协议见 [protocol.md](protocol.md)。

原生内部的鉴权、连接和业务 task 留在 Windows 进程；网关不把 raw PCM 直接转发到某个腾讯云 URL。并非复制一个识别链接即可绕过桥接。

## 资源和失败边界

一个会话拥有独立 encoder 和 voice ID，按序提交所有音频，只在音频排空后结束。网关接收、上传和结果消费分别运行，队列上限64分片，最大总音频60秒（包含录音尾音）。原生部分回包为空时不保证逐字输出；不把 RPC 成功当最终识别成功。

客户端断开会触发任务取消和上游关闭；Windows 必须等已提交原生回调的所有权安全释放，不能直接释放仍在 native 使用的缓冲或为恢复速度强杀输入法。出现超时、格式变化或顺序错误时明确失败，不悄悄补空白、跳帧、截尾或自动整段重放。

兼容 PCM 的 HTTP 回退只是原有格式路径，不是上游流式失败后的自动重试。公开网关不保存录音文件，不复制生产环境的录音归档模块；请求内存仍包含音频及识别结果，应限制访问与进程转储。

## 验收

先运行自动测试，再用公开固定音频和真人分别检查：录音未结束前收到变化中的 snapshot、改口后覆盖同一草稿、末尾字及标点完整、明确 final、60秒含尾音边界、取消/慢网/断线恢复。`/health` 的 `streaming_implemented=true` 只表示实现存在，`native_health_verified=false` 明确说明该接口没有实际发起原生识别自检；不能将 HTTP 200 或 mock 测试当作上游当前可用。

样例客户端的 `--realtime --partials` 只模拟预录音的发送节奏，会把整段识别草稿输出到终端；它不采集麦克风，也不是延迟或准确率承诺。
