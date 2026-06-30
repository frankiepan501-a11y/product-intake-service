# n8n 云端工作流设计：产品信息表新品建档

## 推荐结构

工作流名：`产品信息表新品建档 - Intake Compose`

Cron：

- 每 5 分钟一次。
- 只负责触发处理，不在 Code 节点里堆业务逻辑。

节点：

1. Schedule Trigger
2. HTTP Request -> `POST https://<product-intake-service>/run`
3. IF -> `handled > 0 or errors > 0`
4. Feishu Notify -> 仅异常或有处理结果时通知 Frankie

## 为什么不直接把 compose 逻辑写进 n8n Code

这条链路需要：

- 读写飞书 Base
- 查领星 SKU 列表决定递增序号
- 生成确认卡
- 单条 record 可回放
- 出错时能定位是字段缺失、品牌码缺失、类目配置缺失，还是 API 失败

n8n Code 节点会把这些逻辑塞成不可测试的大块脚本。正式做法是把确定性逻辑放在 `product_intake.py` 或同源 FastAPI 服务中，n8n 只做定时触发和告警。

## 服务接口建议

`POST /run`

请求：

```json
{
  "dry_run": false,
  "send_card": true
}
```

响应：

```json
{
  "defaulted": 1,
  "composed": 1,
  "cards_sent": 1,
  "errors": []
}
```

`POST /run-one`

请求：

```json
{
  "record_id": "rec27HgJzTYt0u",
  "dry_run": false,
  "send_card": true
}
```

用途：单条回放。

## 环境变量

Zeabur 服务只通过环境变量读取凭据，不写入 repo：

- `FEISHU_APP2_ID`
- `FEISHU_APP2_SECRET`
- `FEISHU_EVENT_APP_ID`
- `FEISHU_EVENT_APP_SECRET`
- `LINGXING_PROXY_URL`
- `LINGXING_PROXY_TOKEN`
- `PRODUCT_INTAKE_CONFIRM_EMAIL`

## 人审 gate

确认卡按钮沿用 event-hub 的：

- `confirm_build`
- `reject_build`

按钮回调属于“真正建领星”的 gate，Cron 只负责合成和发卡，不直接建品。

## 工作流二：确认建品后入领星

工作流名：`产品信息表新品建档 - Create Confirmed`

Cron：

- 每 5 分钟一次，或由 event-hub 按钮确认后异步触发。

节点：

1. Schedule Trigger 或 Webhook
2. HTTP Request -> `POST https://<product-intake-service>/create-confirmed`
3. IF -> `errors > 0`
4. Feishu Notify -> 异常升级给 Frankie / 采购群

接口 payload：

```json
{
  "dry_run": false
}
```

单条回放：

```json
{
  "record_id": "recxxxx",
  "dry_run": false
}
```

## 卡片收件人

给 Frankie 发确认卡优先用 `PRODUCT_INTAKE_CONFIRM_UNION_ID`。不要依赖邮箱查 open_id：聪哥3号下 Google 邮箱可能只返回 email，不返回 open_id。
