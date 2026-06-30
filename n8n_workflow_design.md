# n8n 云端工作流设计：产品信息表新品建档

## 当前生产结构

工作流名：`产品信息表新品建档 - Intake Compose`

Workflow ID：`3HaNkKXOtPksUDQc`

Cron：

- 每 5 分钟一次。
- 只负责触发处理，不在 Code 节点里堆业务逻辑。

节点：

1. Schedule Trigger
2. HTTP Request -> `POST https://product-intake-fp501.zeabur.app/run`

当前先保持最小稳定链路：n8n 只负责触发；业务日志、单条回放和错误信息由 FastAPI 服务返回。

## 为什么不直接把 compose 逻辑写进 n8n Code

这条链路需要：

- 读写飞书 Base
- 查领星 SKU 列表决定递增序号
- 生成确认卡
- 单条 record 可回放
- 出错时能定位是字段缺失、品牌码缺失、类目配置缺失，还是 API 失败

n8n Code 节点会把这些逻辑塞成不可测试的大块脚本。正式做法是把确定性逻辑放在 `product_intake.py` 或同源 FastAPI 服务中，n8n 只做定时触发和告警。

## 服务接口

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

单条回放同样使用 `POST /run`，带 `record_id`：

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
- `PRODUCT_INTAKE_CONFIRM_CHAT_ID`
- `PRODUCT_INTAKE_SERVICE_TOKEN`

## 人审 gate

确认卡按钮沿用 event-hub 的：

- `confirm_build`
- `reject_build`

按钮回调属于“真正建领星”的 gate，Cron 只负责合成和发卡，不直接建品。

## 工作流二：确认建品后入领星

工作流名：`产品信息表新品建档 - Create Confirmed`

Workflow ID：`iEY21oFaMhPblsjE`

Cron：

- 每 5 分钟一次。

节点：

1. Schedule Trigger 或 Webhook
2. HTTP Request -> `POST https://product-intake-fp501.zeabur.app/create-confirmed`

按钮确认后，event-hub 负责把表格状态改成 `确认建品`；本工作流轮询该状态并建入领星。

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

当前确认卡发到采购群：

- 群名：采购及产品项目部
- chat_id：`oc_73d455d69842f2104da68201dc282677`

不要默认发给 Frankie。Frankie 只收异常升级：按钮回调失败、品牌/类目规则缺口、领星建品失败等。

不要依赖邮箱查 open_id：聪哥3号下 Google 邮箱可能只返回 email，不返回 open_id；不同飞书 App 的 open_id 也不互通。

## 上线验证

- Zeabur 服务健康检查通过。
- `/run` dry-run 通过。
- `/create-confirmed` dry-run 通过。
- 两个 n8n workflow 均已 active，`activeVersionId` 已绑定。
- 首轮 n8n execution 均为 `success`。
