# 参考项目、归属与分发说明

## wechat-asr-bridge — 实际运行依赖

- 项目：<https://github.com/kuxiaowo/wechat-asr-bridge>
- 作者 / MIT 版权所有声明：Copyright (c) 2026 kuxiaowo。
- 调研提交：`bce9b6b4825e6fb8c7a9d0d02a0d31a90dd3d2f2`，实际使用 portable v0.3.3。
- 语音桥接和 OpenAI 风格上游服务由该项目实现。本仓库的贡献是部署整合、原接口适配、讯飞风格协议转换和管理控制台，不将其成果归为自研识别引擎。
- 本仓库不打包其源码或二进制。若自行重新分发上游软件，应保留上游 MIT 许可证和声明。许可证原文：<https://github.com/kuxiaowo/wechat-asr-bridge/blob/main/LICENSE>。

## wetype-ime-linux — 调研参考

- 项目：<https://github.com/yu1745/wetype-ime-linux>
- 调研提交：`3a127ef2cb5a9d9754e6307f462b188d8f31b878`。
- 仓库 LICENSE 为 GPL v3。用于了解 Linux 输入法路线的能力边界；本方案最终选择 Windows VM，并未复制、链接或打包其实现。本仓库的 MIT 授权不覆盖该项目。

## 协议、示例和专有软件

- 讯飞 V2 协议文档：<https://www.xfyun.cn/doc/spark/spark_zh_iat_new.html>。只作为兼容格式参考；不代表官方认可，也不实现文档中的全部能力。
- FunASR 示例来源：<https://github.com/modelscope/FunASR/blob/main/README_zh.md>；公开音频地址见 `docs/verification.md`。没有复制 FunASR 模型或源代码，也未重新分发音频。
- WeType / 微信输入法：<https://z.weixin.qq.com/>，腾讯专有软件。Windows 为微软软件；两者均由使用者自行获取、授权和维护。
- 控制台默认使用操作系统字体，不包含商业字体文件。

Python 依赖由 pip 从各自项目安装，各依赖的许可证保留，不会因本仓库使用 MIT 而改变。
