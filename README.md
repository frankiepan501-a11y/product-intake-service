# 产品信息表新品建档自动化

## 定位

这套脚本接管「采购填写产品信息维护表 -> 系统合成 SKU/品名 -> 采购点卡确认 -> 领星建品」链路。

系统化目标：

- 表单新记录如果已满足关键字段，自动补 `建档状态=待合成`。
- `待合成` 记录自动合成 `ERP SKU`、`ERP品名`、类目字段，并转为 `待确认`。
- 合成后给采购群发确认卡。是否真正建领星仍由确认按钮作为人审 gate。
- 确认卡优先发给「录入采购」本人；采购群只发摘要。若无法识别录入采购或私聊失败，采购群兜底收到完整确认卡。
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
$env:PRODUCT_INTAKE_BRAND_TABLE_ID="tblYKn7n7DURgwBM"
$env:PRODUCT_INTAKE_SUBMITTER_FIELD="录入采购"
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
- 异常通知使用飞书交互卡片，不再发送 raw log 文本。卡片必须包含：北京时间、影响环节、影响产品、具体报错的运营解释、下一步处理人和动作、技术 replay/run_id。
- 运行锁属于系统自动保护，短暂重叠默认不发群卡；只有上一轮任务持续占锁超过 `PRODUCT_INTAKE_LOCK_ALERT_AFTER_SEC`（默认 600 秒）才发 `LOG·P2` 卡，并受 `PRODUCT_INTAKE_LOCK_ALERT_COOLDOWN_SEC`（默认 1800 秒）限流。
- 资料/建品失败标 `LOG·P1` 并要求采购或系统负责人处理。

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

品牌码来源：

- 正式来源：同一产品库 Base 的「品牌配置表」`tblYKn7n7DURgwBM`。
- 代码内置品牌码只作兜底，避免飞书配置表临时不可读时生产链路直接中断。

当前启用品牌码：

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
- 产品表已新增系统字段「录入采购」`fld6jY5RaQ`，类型为创建人，用来识别表单/记录录入人。
- 服务通过聪哥2号读取记录和人员字段，转成 union_id 后用聪哥3号私聊发完整确认卡。
- 私聊成功后，采购群只收到无按钮摘要；如果拿不到录入人或私聊失败，采购群收到完整确认卡兜底。
- 当前默认采购群：`采购及产品项目部` / `oc_73d455d69842f2104da68201dc282677`。
- Frankie 只收异常升级：品牌未知、SKU 冲突、类目缺失、按钮回调失败、领星建品失败。

实现注意：

- 聪哥3号发卡才能让按钮回调进入 n8n event-hub。
- 不同飞书 App 的 open_id 不互通；不要把聪哥1号/2号 open_id 直接拿给聪哥3号发私聊。
- 若后续发现表单创建人仍是机器人而非采购本人，再把「录入采购」改为表单必填人员字段或维护「采购 -> union_id」映射。

## 配置审计

只读审计命令：

```powershell
python C:/Users/Administrator/scripts/product_intake/audit_config.py --format markdown
```

审计范围：

- 品牌配置表：启用品牌是否有品牌码、品牌码格式、重复品牌/重复品牌码。
- 类目配置表：必填字段、品类码/平台码格式、平台+品类组合重复、cid 是否数字、配置名重复、产品类型词是否过泛。
