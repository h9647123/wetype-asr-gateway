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

返回开始确认 `header.status=0`；实时中间结果为 `header.status=1`；确认识别结束为 `header.status=2`。`payload.result.text` 为 base64 UTF-8 JSON，解码后全文位于 `ws[].cw[0].w`。中间及最终全文均使用 `sn=0, pgs="rpl", rg=[0,0]`，客户端必须覆盖同一份草稿，不能将它们连续追加。`payload.result.seq` 是递增 revision，不能代替解码结果中的固定 `sn`。

```json
{"ret":0,"sn":0,"ls":false,"pgs":"rpl","rg":[0,0],"ws":[{"bg":0,"cw":[{"w":"当前整段草稿"}]}]}
```

最终包的 `ls=true`。中间文字可以缩短、改词或补标点；音频 `status=2` 只表示客户端录音结束，仍须等待服务端最终包，不能提前视作识别成功。客户端须同时发送音频与消费结果，避免长录音时结果队列堵塞。没有逐词时间戳，也不保证逐字或每包产出文字。

限制：raw PCM、8/16 kHz、单/双声道、8/16 bit、总音频最长60秒（包含尾音）；仅16 kHz / 单声道 / 16 bit走实时路径，其余格式保留整段 HTTP 兼容。每帧必须按样本宽度及声道数对齐。每个 WebSocket 消息最大1 MiB（需使用 README 的 uvicorn 启动参数），上传总期限90秒、帧间空闲最多15秒；最多4个适配器会话、64个有界待发音频分片，共享一个网关上游槽位。实时排队/上传/识别有150秒总期限，结束后等待最终确认最多60秒；其他 PCM 的旧 HTTP 排队/识别总期限240秒。`dwa` 可出现但不改变原生识别语义；其他未实现选项可能返回错误，语言/口音不强制执行。

实时上游失败会明确报错并关闭当前会话，不把已提交音频自动重放到 HTTP 接口，也不将最终文字动画伪装成实时识别。浏览器/板端应允许用户确认后重新录音或重试。公开版无磁盘录音归档。

错误 HMAC 在握手时拒绝；帧错误返回非零 code 后关闭连接。错误码只是本适配器的兼容输出，不能视为完整复刻讯飞云错误语义。
