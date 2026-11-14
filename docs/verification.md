# 验证范围

v0.2.0 的 VM、服务器、60秒边界、取消恢复及 CAM 真人流式验收见 [2026-11-14 验收记录](acceptance-20261114.md)。

## 原型真实验收

2026-10-24 的部署原型使用 Ubuntu 24.04、Windows LTSC 2021、WeType 3.0.0.38 和 WeChatASRBridge v0.3.3。用户环境相关地址、管理凭据和原始日志不会上传。

公开中文音频来源是 [FunASR 官方 README](https://github.com/modelscope/FunASR/blob/main/README_zh.md) 的示例：

<https://isv-data.oss-cn-hangzhou.aliyuncs.com/ics/MaaS/ASR/test_audio/asr_example_zh.wav>

- 177572 字节；5.547 秒；16 kHz / 单声道 / PCM 16 bit。
- SHA-256：`a1bd32dc78493c123f9625a66deee562aed2895f53fbc39f2cca3be7e6f4f20f`。
- HTTPS OpenAI、HTTPS 原接口和 WSS 均返回：“欢迎大家来体验达摩院推出的语音识别模型。”
- 三次公网调用分别约 15.770、4.256、4.758 秒；第一次发生在 Windows 更新后的启动阶段。VM 重启后再次经公网上传约 8.922 秒。
- 这些记录仅说明样例经过真实链路转写，不是通用准确率、延迟或并发承诺。未做 CER/WER 基准。
- 另有 6.28 秒录音、43.96 秒重复录音、无授权拒绝、错误 HMAC / App ID、无桌面登录自启和局域网重启恢复验收。
- 后续复测发现 ACPI 关机可能被 Windows 忽略，因此不能仅凭 systemd active / InvocationID 改变认定 VM 真正重启；部署方案增加了可选的 Windows 原生关机 helper，并要求额外核对实际 OS 启动时间。

音频按需下载用于验证，本仓库不附带音频文件。

## 开源版自动化测试

测试使用临时配置、随机测试凭据、模拟上游和本机 HTTP / WebSocket，验证签名、帧校验、实时中间 snapshot、最终替换结果、错误/取消/断开后的清理、有界超时、非实时 PCM 的 HTTP 兼容、共享槽位、API key scope / 并发计数、管理鉴权及初始化不覆盖凭据。测试不会实际重启 systemd 服务、修改 Windows、使用用户密钥或请求腾讯云。

自动化测试不能替代你自己的 Windows 版本、网络条件和真实音频验收。开源参数化版本不应被描述为已在所有示例配置上完整部署验证。

## 2026-11-14 流式研究基线

本机 Windows WeType 3.0.1.35 使用公开中文预录音模拟实时发送：约5.56秒有效音频、278个20ms帧，500ms一批；9个非 final 回包含文字，首个非空文字约2.33秒，最终原生 end_flag 约7.27秒。持续 encoder 单次初始化、分块编码，首片带头、后续不重复流头，帧数/字节数与整段编码一致；最终识别与 owner 清理通过。

这是特定版本的原生能力证据，不是无人声丢失、逐字承诺、所有版本兼容或公开网关已在所有部署完成端到端验收。60秒、真人、CAM 固件及 VM 版本的验收应独立记录；不公开原始用户录音、个人会话、私有地址或签名 URL。
