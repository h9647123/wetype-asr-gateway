# 讯飞 V2 风格协议子集

默认本机地址为 `ws://127.0.0.1:8768/v1`，部署后可使用 `wss://asr.example.com/v1`。

根据[官方文档](https://www.xfyun.cn/doc/spark/spark_zh_iat_new.html)，以私有配置中的 API Secret 对下面的 UTF-8 字符串计算 HMAC-SHA256 并 base64 编码：

```text
host: asr.example.com
date: <RFC1123 GMT date>
GET /v1 HTTP/1.1
```

授权字符串为：

```text
api_key="<local-api-key>", algorithm="hmac-sha256", headers="host date request-line", signature="<base64-hmac>"
```

将授权字符串整体 base64 编码，和 `host`、`date` 一起以 URL 编码放入查询参数。日期容差 300 秒，Host 必须和实际请求一致，非默认端口必须包含端口号。签名 URL 在有效期内属于敏感授权材料，不要记录。

首帧：

```json
{
  "header": {"app_id": "<local-app-id>", "status": 0},
  "parameter": {"iat": {"domain": "spark_asr_v2", "language": "zh_cn", "accent": "mandarin", "result": {"encoding": "utf8", "compress": "raw", "format": "json"}}},
  "payload": {"audio": {"encoding": "raw", "sample_rate": 16000, "channels": 1, "bit_depth": 16, "seq": 1, "status": 0, "audio": "<base64-pcm>"}}
}
```

中间帧 status=1，最后一帧 status=2，header 与 audio 的 status 必须一致，seq 严格递增。最后一帧可以使用空 audio。音频格式不能在会话内改变。

返回开始确认 header.status=0；识别结束后返回 header.status=2。`payload.result.text` 为 base64 UTF-8 JSON，解码后全文位于 `ws[].cw[].w`。只有一个最终结果，没有 partial、时间戳或修正结果。

限制：raw PCM、8/16 kHz、单/双声道、8/16 bit、最长 60 秒；每个 WebSocket 消息最大 1 MiB（需使用 README 的 uvicorn 启动参数），上传总期限 90 秒、帧间空闲最多 15 秒；最多 4 个适配器会话，在适配器内串行调用上游，排队与识别共 240 秒。`dwa` 可出现但不产生动态修正；其他未实现选项可能返回错误，语言/口音不强制执行。

错误 HMAC 在握手时拒绝；帧错误返回非零 code 后关闭连接。错误码只是本适配器的兼容输出，不能视为完整复刻讯飞云错误语义。
