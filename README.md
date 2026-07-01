# 产品信息表新品建档自动化

## 定位

这套脚本接管「采购填写产品信息维护表 -> 系统合成 SKU/品名 -> 采购点卡确认 -> 领星建品」链路。

系统化目标：

- 表单新记录如果已满足关键字段，自动补 `建档状态=待合成`。
- `待合成` 记录自动合成 `ERP SKU`、`ERP品名`、类目字段，并转为 `待确认`。
- 合成后给采购群发确认卡。是否真正建领星仍由确认按钮作为人审 gate。
- 采购退回修改后，改完资料勾选 `采购已修改`，系统会重新进入 `待合成` 并发新确认卡。
- 采购确认后，`确认建品` 记录可由 `create-confirmed` 建入领星并回写 `已建领星`。
- 支持 `--record-id` 单条回放，便于定位某一条为什么没跑。

## 执行形态

- 当前正式入口：Zeabur FastAPI 服务，由 n8n Cron 调 `/run` 和 `/create-confirmed`。
- 本地入口：`product_intake.py` 保留作单条回放和调试。
- 人审 gate：飞书确认卡按钮。SKU 建后不可随意改，不能跳过此 gate。

当前状态：

- 代码已迁出 scratchpad。
- Zeabur 服务已部署：`https://product-intake-fp501.zeabur.app`。
- n8n Cron 已上线并完成首轮 execution 验证。
- 当前不依赖本机定时脚本；本地脚本仅作回放/修复工具。

## 必需环境变量

不要把真实值写入脚本或仓库。

```powershell
$env:FEISHU_APP2_ID="..."
$env:FEISHU_APP2_SECRET="..."
$env:FEISHU_EVENT_APP_ID="..."
$env:FEISHU_EVENT_APP_SECRET="..."
$env:LINGXING_PROXY_URL="..."
$env:LINGXING_PROXY_TOKEN="..."
$env:PRODUCT_INTAKE_SERVICE_TOKEN="..."
```

可选：

```powershell
$env:PRODUCT_INTAKE_CONFIRM_EMAIL="frankiepan501@gmail.com"
$env:PRODUCT_INTAKE_CONFIRM_UNION_ID="on_..."
$env:PRODUCT_INTAKE_CONFIRM_OPEN_ID="ou_..."
$env:PRODUCT_INTAKE_CONFIRM_CHAT_ID="oc_73d455d69842f2104da68201dc282677"
$env:PRODUCT_INTAKE_BASE_TOKEN="MvtZb6OE9aJFaisO913cWSErnFe"
$env:PRODUCT_INTAKE_TABLE_ID="tblTvqipcTBFRUkr"
$env:PRODUCT_INTAKE_CATEGORY_TABLE_ID="tbluZxoiRo1L0BLT"
```

## 常用命令

只预览某条记录：

```powershell
python C:/Users/Administrator/scripts/product_intake/product_intake.py --record-id rec27HgJzTYt0u run
```

只给表单提交的新记录补默认状态：

```powershell
python C:/Users/Administrator/scripts/product_intake/product_intake.py --commit default-status
```

合成并发确认卡：

```powershell
python C:/Users/Administrator/scripts/product_intake/product_intake.py --commit --send-card compose
```

完整执行：

```powershell
python C:/Users/Administrator/scripts/product_intake/product_intake.py --commit --send-card run
```

采购确认后建入领星：

```powershell
python C:/Users/Administrator/scripts/product_intake/product_intake.py --commit create-confirmed
```

已合成但卡片没发出去时，单独补发：

```powershell
python C:/Users/Administrator/scripts/product_intake/product_intake.py --record-id rec27HgJzTYt0u --commit send-card
```

## 云端服务

Zeabur/FastAPI 服务入口：

- `service.py`
- `requirements.txt`
- `Procfile`

服务接口：

- `GET /health`
- `POST /run`
- `POST /send-card`
- `POST /create-confirmed`

服务保护：

- 非 dry-run 的 `/run`、`/send-card`、`/create-confirmed` 会走进程内运行锁，避免并发执行导致 SKU 序号撞号。
- 服务会解析 stdout 里的 `compose_error`、`create_error`、`create_failed`。只要 mutating run 出错，就主动发异常通知到采购群，并尝试抄送 Frankie。
- 异常通知包含 endpoint、run_id、record_id、错误、replay 命令。

`PRODUCT_INTAKE_SERVICE_TOKEN` 设置后，n8n 调用时使用：

```http
Authorization: Bearer <PRODUCT_INTAKE_SERVICE_TOKEN>
```

生产部署：

- GitHub repo: `frankiepan501-a11y/product-intake-service`
- Zeabur service: `product-intake-service` / `6a4391ba22d1fdaf7eb12475`
- Zeabur domain: `https://product-intake-fp501.zeabur.app`
- Zeabur environment: `production` / `69856f0c86311f632dc2c2c9`

n8n 工作流：

- `产品信息表新品建档 - Intake Compose` / `3HaNkKXOtPksUDQc`
  - 每 5 分钟调用 `POST /run`
  - 请求体：`{"dry_run":false,"send_card":true}`
- `产品信息表新品建档 - Create Confirmed` / `iEY21oFaMhPblsjE`
  - 每 5 分钟调用 `POST /create-confirmed`
  - 请求体：`{"dry_run":false}`

上线验证：

- `GET /health` 返回 `{"status":"ok"}`。
- `/run` dry-run 通过。
- `/create-confirmed` dry-run 通过。
- 两个 n8n workflow 均已 active，`activeVersionId` 已绑定，首轮 execution 均为 `success`。

## 品牌口径

`品牌` 字段只填真正对外/内部识别品牌，不填供应商名、工厂名、公司名。

当前品牌码：

- `FUNLAB` -> `FL`
- `POWKONG` -> `PK`
- `联游` -> `LY`
- `白牌` -> `WB`
- `万利` -> `WL`

`万利` 已按 Frankie 确认纳入品牌：它不是供应商名，是朋友公司分销品牌。

## 当前已知校验

脚本会提示但不自动改：

- `兼容平台` 未包含 `主平台`。
- `工厂型号` 末尾像 `-BK` / `-WH`，但 `颜色变体` 为空。

脚本会阻止：

- `品牌` 为空。
- `品牌` 不在品牌码表。
- `品牌` 疑似供应商/工厂/公司名。
- `类目配置` / `款式` 缺失。

## 退回修改后的再提交规则

卡片点 `退回修改` 后，采购按以下步骤处理：

1. 打开记录，修改品牌、类目配置、款式、平台、颜色、尺寸等错误字段。
2. 勾选 `采购已修改`。
3. 不需要找 Frankie 或手动补发卡；n8n 下一轮调用 `/run` 后，服务会把记录重新置为 `待合成`。
4. 系统重新合成 ERP SKU/ERP 品名并发一张新的确认卡。

实现口径：

- 触发条件：`建档状态=待修改` 且 `采购已修改=true`。
- 服务动作：回写 `建档状态=待合成`，并清空 `采购已修改=false`。
- 下一步：同一轮继续执行 compose，重新发确认卡。

## 确认卡收件人规则

当前正确口径：

- 采购提交表单后，确认卡发给录入采购确认，不默认发给 Frankie。
- 如果拿不到录入人的 event-hub App open_id / union_id，短期发到采购所在业务群，并在群里让录入采购确认。
- 当前默认采购群：`采购及产品项目部` / `oc_73d455d69842f2104da68201dc282677`。
- Frankie 只收异常升级：品牌未知、SKU 冲突、类目缺失、按钮回调失败、领星建品失败。

实现注意：

- 聪哥3号发卡才能让按钮回调进入 n8n event-hub。
- 不同飞书 App 的 open_id 不互通；不要把聪哥1号/2号 open_id 直接拿给聪哥3号发私聊。
- 长期应在表中补「录入人/提交人」字段，或维护「采购 -> union_id / 聪哥3号 open_id」映射。
