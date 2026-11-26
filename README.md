# 访谈协作产品：可核验的个人数据清除服务

使用 **Python + FastAPI + SQLite** 实现。访谈由文本片段（segment）组成，每个片段显式列出参与者 ID。参与者资料、片段正文、保留理由都由本地调用方通过 HTTP/脚本明确输入，不做自然语言人名识别。

## 1. 三个必须区分的结果

| 状态 | 普通读取/搜索/导出 | 负责人受限保留入口 | SQLite 正文 | 何时出现 |
|---|---|---|---|---|
| `hidden_pending_purge` | 不可见，直取片段 ID 也返回 `404` | 若无有效保留，不提供正文入口 | **仍在 `segments.content`** | 清除任务已提交但清除过程未完成；通常只在崩溃/重启前短暂出现 |
| `retained` | 同样完全不可见 | 持 `X-Owner-Token` 且通过 `/owner/retentions/{id}` 可查看 | 仍在业务表中 | 片段存在一项或多项有效、有理由的保留 |
| `purged` | `404`/导出不含 | 不能查看；补建保留返回 `409 segment_body_purged_cannot_retain` | `segments` 行已物理删除 | 无保留执行清除，或最后一个保留被解除 |

查询申请时，每个 item 还会返回：

- `result`: `hidden` / `restricted_retention` / `cleared`
- `body_state`
- `body_present_in_business_storage`
- `active_retentions` / `active_retention_reasons`
- `purged_at`
- `visibility`

因此不会把“已隐藏但尚未清除”误报成“正文已清除”。

## 2. 启动方式

当前工作区已经包含安装好依赖的 `.venv`，可直接启动：

```bash
cd /workspace

# 写入显式演示数据；--reset 会删除旧 SQLite 文件
.venv/bin/python -m app.seed --reset

# 启动 API（任选一种）
PDC_OWNER_TOKEN=demo-owner-token \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

PDC_OWNER_TOKEN=demo-owner-token PORT=8000 .venv/bin/python main.py
```

如果要在新环境重建虚拟环境：

```bash
cd /workspace
python3 -m venv .venv          # Debian/Ubuntu 可能需先安装 python3-venv
.venv/bin/python -m pip install -r requirements.txt
```

打开：

- Swagger UI：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/health>

负责人请求需带头：

```http
X-Owner-Token: demo-owner-token
```

演示数据库：`data/deletion_demo.db`。

## 3. 一键跨入口验证（推荐）

另一个终端执行：

```bash
cd /workspace
.venv/bin/python scripts/e2e_demo.py
```

该脚本会：

1. 启动真实 uvicorn 进程和临时 SQLite DB；
2. 创建独立的参与者、访谈、片段、两项保留和旧导出请求；
3. 验证预览后新增片段仍被执行覆盖；
4. 验证重复请求只产生一个任务；
5. 验证申请后禁止写资料/新片段；
6. 验证普通读取、原片段 ID、`/search/sessions`、旧导出请求均隐藏；
7. 验证无 owner token 返回 `403`，有 token 才能从保留入口看正文；
8. 验证两项保留并存、解除一项后仍保留、最后一项解除时同事务物理清除；
9. 验证清除后补建保留返回 `409`；
10. 验证撤销 session 不撤销清除事实；
11. 构造 `hidden_pending_purge`，真实重启服务后由 startup recovery 完成清除。

最近一次运行结果：**62 项检查全部通过**。

协作校稿的一键验证：

```bash
cd /workspace
.venv/bin/python scripts/e2e_proofreading.py
```

最近一次运行结果：**61 项检查全部通过**（详见第 6.7 节）。

并发顺序测试：

```bash
.venv/bin/python -m pytest -q
```

包含两组各 25 轮线程竞争：

- 清除执行 vs 创建保留；
- 解除最后旧保留 vs 同时创建新保留。

以及协作校稿的 20 个校稿人并发提交（全部基于同一版，互不冲突，必须全部合入且不丢改动）。

SQLite 使用 `BEGIN IMMEDIATE` 串行化提交顺序。每轮最终只允许两种一致结果：

- 保留事务先提交：正文存在、item=`retained`、受限入口可看；
- 清除/最后解除先提交：正文行删除、item=`purged`，竞争的保留创建收到 `409`。

## 4. 手工 HTTP 验证

### 4.1 演示数据

| ID | 参与者 | 内容/用途 |
|---|---|---|
| `int-alpha` | — | 主演示访谈 |
| `seg-retained` | `p-alice`, `p-bob` | 已有保留 `ret-demo-retained` |
| `seg-purged` | `p-alice`, `p-carol` | 无保留，Alice 申请后物理清除 |
| `seg-visible` | `p-bob`, `p-dave` | 不受 Alice 申请影响，普通入口仍可见 |
| `seg-crash-dave` | `p-dave` | 用于模拟“已隐藏但未清除”和重启恢复 |
| `exp-demo-before` | — | 删除前创建的 all-scope 导出请求；不保存正文快照 |

### 4.2 预览后执行

```bash
BASE=http://127.0.0.1:8000

# 执行前能搜到
curl "$BASE/search?q=MARKER_RETAINED" | python -m json.tool

# 预览：seg-retained -> restricted_retention；seg-purged -> purge_body
curl "$BASE/participants/p-alice/deletion-preview" | python -m json.tool

# 执行；重复执行或带同一个 Idempotency-Key 都返回同一个 request id
curl -i -X POST "$BASE/participants/p-alice/deletion-requests" \
  -H 'Idempotency-Key: alice-demo-key'

curl -s -X POST "$BASE/participants/p-alice/deletion-requests" \
  -H 'Idempotency-Key: alice-demo-key' | python -m json.tool
```

查询申请状态：

```bash
curl "$BASE/participants/p-alice/deletion-request" | python -m json.tool
```

此时摘要应为 `retained=1, purged=1`（若没有预览后新增片段；一键脚本会额外制造 late segment）。

### 4.3 普通入口全部隐藏

```bash
# 410，只返回无资料内容的 tombstone
curl -i "$BASE/participants/p-alice"

# 原片段 ID：保留段和清除段都返回相同 404
curl -i "$BASE/segments/seg-retained"
curl -i "$BASE/segments/seg-purged"

# 访谈中只剩 seg-visible
curl "$BASE/interviews/int-alpha" | python -m json.tool

# 会话/内容搜索搜不到 Alice 相关正文
curl "$BASE/search/sessions?q=MARKER_" | python -m json.tool

# 旧导出请求在“返回内容时”按当前状态生成，不使用旧正文快照
curl "$BASE/exports/exp-demo-before/content"
```

可显式确认：

```bash
curl -s "$BASE/exports/exp-demo-before/content" | grep -E 'MARKER_(RETAINED|PURGED)' || echo 'hidden from export'
curl -s "$BASE/exports/exp-demo-before/content" | grep 'MARKER_VISIBLE'
```

### 4.4 负责人受限保留入口

```bash
# 无 token：403
curl -i "$BASE/owner/retentions/ret-demo-retained"

# 有 token：只能从该受限入口看到 seg-retained 正文
curl -s -H 'X-Owner-Token: demo-owner-token' \
  "$BASE/owner/retentions/ret-demo-retained" | python -m json.tool

# 普通入口仍然 404
curl -i "$BASE/segments/seg-retained"
```

解除最后一个保留：

```bash
curl -s -X POST \
  -H 'X-Owner-Token: demo-owner-token' \
  -H 'Content-Type: application/json' \
  -d '{"release_note":"复核结束"}' \
  "$BASE/owner/retentions/ret-demo-retained/release" | python -m json.tool
```

响应中的 `reconciliation.outcome` 应为 `purged`；解除保留和物理删除在同一个 `BEGIN IMMEDIATE` 事务中提交。

再次查询：

```bash
curl "$BASE/participants/p-alice/deletion-request" | python -m json.tool
```

尝试给已清除片段补建保留：

```bash
curl -i -X POST \
  -H 'X-Owner-Token: demo-owner-token' \
  -H 'Content-Type: application/json' \
  -d '{"reason":"迟到的保留，不应复活正文"}' \
  "$BASE/owner/segments/seg-retained/retentions"
# 409 code=segment_body_purged_cannot_retain
```

撤销 Alice 的演示会话不能撤销清除：

```bash
curl -X DELETE "$BASE/sessions/session-alice"
curl "$BASE/participants/p-alice/deletion-request" | python -m json.tool
```

### 4.5 构造未完成清除并验证重启恢复

保持 uvicorn 运行，在项目目录执行：

```bash
.venv/bin/python scripts/simulate_unfinished.py p-dave
```

此时：

```bash
curl "$BASE/participants/p-dave/deletion-request" | python -m json.tool
curl -i "$BASE/segments/seg-crash-dave"   # 404，普通入口已隐藏
```

你会看到 item 为：

```json
{
  "status": "hidden_pending_purge",
  "body_present_in_business_storage": true,
  "purged_at": null
}
```

这明确表示“已隐藏”，不是“已清除”。

重启服务（Ctrl+C 后重新执行同一条 uvicorn 命令）。startup recovery 会继续处理未完成 item。随后：

```bash
curl "$BASE/participants/p-dave/deletion-request" | python -m json.tool
```

应变为 `status=purged`、`body_present_in_business_storage=false`、`purged_at` 有值。

也可用 Python 确认业务表行已删除：

```bash
.venv/bin/python - <<'PY'
import sqlite3
conn = sqlite3.connect('data/deletion_demo.db')
print(conn.execute("SELECT COUNT(*) FROM segments WHERE id='seg-crash-dave'").fetchone()[0])
PY
# 0
```

`deletion_items` 中仍保留不含正文的处理记录。

## 5. 主要 HTTP 接口

| 方法/路径 | 用途 |
|---|---|
| `POST /participants` | 显式写入参与者资料和演示 session token |
| `PATCH /participants/{id}/profile` | 更新资料；已申请清除后 `409` |
| `POST /interviews` / `POST /segments` | 显式写入访谈和片段；片段必须给 `participant_ids` |
| `GET /interviews/{id}` / `GET /segments/{id}` | 普通读取，自动排除任何涉及清除申请者的不可分割片段 |
| `GET /search` / `GET /search/sessions` | 会话/内容搜索，使用相同隐藏谓词 |
| `GET /participants/{id}/deletion-preview` | 只读预览当前影响范围和保留原因 |
| `POST /participants/{id}/deletion-requests` | 执行清除申请；支持 `Idempotency-Key`，同参与者只产生一个任务 |
| `GET /deletion-requests/{id}` | 查询每次申请的清除/保留状态 |
| `GET /participants/{id}/deletion-request` | 按参与者查询唯一申请 |
| `POST /exports` / `GET /exports/{id}/content` | 普通导出请求；只存 scope，返回时按当前状态生成 |
| `POST /owner/segments/{id}/retentions` | 负责人创建有理由、独立 ID 的保留项 |
| `GET /owner/retentions/{id}` | **受限保留正文入口**，需 owner token |
| `POST /owner/retentions/{id}/release` | 解除保留；最后一项解除时同事务清除正文 |
| `GET /owner/deletion-requests` | 负责人查询全部申请 |
| `GET /owner/audit-events` | 查看不含正文的状态/提交审计事件 |
| `DELETE /sessions/{token}` / `DELETE /participants/{id}/session` | 撤销账号会话；不影响已提交清除事实 |

协作校稿接口见第 6 节；访谈预约与批量改期接口见第 8 节；访谈补贴结算接口见第 9 节。

## 6. 协作校稿（新增）

同一场访谈可以有**多个校稿人并行提交**，正式正文在发布前保持不变。所有校稿接口使用与保留入口相同的负责人凭据（`X-Owner-Token`）。

### 6.1 数据与状态

校稿状态保存在独立的表中，草稿正文、替换文字和批注文字只存在于这些表里：

| 表 | 内容 |
|---|---|
| `proofreading_sessions` | 每场访谈的校稿会话；`status=open/published`，`current_version` 为当前草稿版号。部分唯一索引保证每场访谈**同时最多一份** `open` 会话 |
| `proofreading_session_segments` | 会话圈选的片段及其序号 |
| `proofreading_versions` | 每个版号的元数据（合并者为哪个提交编号） |
| `proofreading_segment_contents` | 每个版号下每个片段的完整草稿文本（v1 为开启时正式正文快照） |
| `proofreading_edits` | 实际合入的编辑操作，按合并前坐标存储，用于把旧版提案重放到当前版 |
| `proofreading_submissions` | 提交编号 → 提案指纹与合入结果，用于幂等重试；不保存提案正文 |
| `proofreading_annotations` | 批注及其锚定的非空文字范围 |

正式正文 `segments.content` 只在发布事务中整体更新。发布前，普通读取、搜索、导出完全看不到草稿。

### 6.2 校稿接口

| 方法/路径 | 用途 |
|---|---|
| `POST /owner/proofreading/sessions` | 负责人圈选同一场访谈的**可见**片段开启校稿；同场访谈已有开放会话时 `409 proofreading_session_exists` |
| `GET /owner/proofreading/sessions` | 列出会话，可按 `interview_id` / `status` 过滤 |
| `GET /owner/proofreading/sessions/{id}?version=N` | 查看指定版（默认当前版）的完整稿件：片段文本、批注、当前版号、`publishable_segment_ids`、`can_publish` |
| `GET /owner/proofreading/sessions/{id}/versions` | 版号列表 |
| `GET /owner/proofreading/sessions/{id}/versions/{n}` | 查看历史版草稿 |
| `POST /owner/proofreading/sessions/{id}/proposals` | 提交提案：`submission_id`、`base_version`、`edits[]`；合入成功 `201` 并返回新草稿，冲突 `409 proofreading_merge_conflict` 并给出冲突片段 |
| `GET /owner/proofreading/sessions/{id}/annotations` | 列出批注（默认只看仍在可见范围内的片段） |
| `POST /owner/proofreading/sessions/{id}/annotations` | 在当前版某个非空文字范围留批注 |
| `POST /owner/proofreading/sessions/{id}/annotations/{aid}/reanchor` | 把未解决批注显式重定位到当前版范围 |
| `POST /owner/proofreading/sessions/{id}/annotations/{aid}/resolve` | 明确解决批注 |
| `POST /owner/proofreading/sessions/{id}/publish` | 指定当前稿件版号发布 |

提案体示例（范围是零起点 Unicode 码点半开区间）：

```json
{
  "submission_id": "sub-001",
  "base_version": 1,
  "edits": [
    {"segment_id": "seg-visible", "start": 0, "end": 4, "replacement": "修订"},
    {"segment_id": "seg-visible", "start": 9, "end": 9, "replacement": "（插入）"}
  ]
}
```

- 空范围 `start == end` 是插入；`replacement` 为空串是删除。
- 同一提案的范围都按 `base_version` 的正文解释，彼此不能冲突。
- `start`/`end` 越界或 `end < start` 返回 `422 proofreading_range_invalid`。

### 6.3 合入与冲突规则

提案基于旧版时，会通过**此后实际发生过的编辑操作**逐步重放到当前版；不冲突的改动合入当前稿件，不覆盖别人的改动。以下情况整份提案不生效（`409`，`details.segments` 给出涉及的片段）：

1. 两个非空范围重叠；
2. 插入点落在另一改动的非空范围**内部**；
3. 两次插入落在同一点。

只接触范围端点不算冲突；端点处的插入留在替换文字之外（插在替换范围两端时，插入文字在替换文字之外）。冲突按**实际发生过的改动**判定——即使后来把文字改回原样，历史冲突仍然存在。

提交编号幂等：

- 同一 `submission_id` 重试**已合入的相同提案**，返回原合入结果（`200`，`idempotent_replay=true`），不再改稿；
- 同一编号换用不同提案返回 `409 submission_id_reused`；
- 被冲突拒绝的提案记录同一编号，重试返回 `409` 且 `details.segments` 一致。

### 6.4 批注

- 批注锚定当前版的一段**非空**文字范围。
- 改动避开所指文字（在其前后修改或端点插入）时，批注随之平移，仍指向原来那段文字；
- **替换或删除**所指文字的任一部分，或**在其内部插字**时，批注显示 `anchor_status=lost`、`start/end` 为空，不猜测新位置；
- 负责人可把未解决批注 `reanchor` 到当前版的任意非空范围（恢复 `anchored`），也可 `resolve`；
- 无论是否失去定位，**未解决的批注都阻止发布**（`409 proofreading_unresolved_annotations`），但**不阻止继续合稿**。

### 6.5 发布

`POST /owner/proofreading/sessions/{id}/publish`，体为 `{"version": N}`：

- `N` 必须等于当前版，否则 `409 proofreading_version_mismatch`；
- 仍有未解决的批注 → `409 proofreading_unresolved_annotations`；
- 没有任何可发布片段（全部因清除退出范围）→ `409 proofreading_no_publishable_segments`；
- 满足条件时，在**一个 `BEGIN IMMEDIATE` 事务**内更新全部可发布片段的 `segments.content`，并标记会话 `published`。普通读取、搜索、导出只会看到更新前或更新后的完整一套，不会看到半套。

### 6.6 与参与者清除的关系

- 参与者申请清除后，相关片段立即退出校稿的**可见与可发布范围**（`publishable_segment_ids` 不再包含它）；
- 针对该片段的新提案返回 `409 proofreading_segment_out_of_scope`；该片段上的批注不再阻止其他片段发布；
- 正式正文实际清除时（同一物理删除事务内），新功能中该片段的**历史草稿正文、替换文字、批注文字**一并删除，已存合入结果中回显的该片段文本也被清除；
- 原有受限保留逻辑不变，只继续用于正式正文；被保留的片段仍因参与者在清除申请中而退出校稿范围。

### 6.7 复跑说明

```bash
cd /workspace

# 单元/并发测试（含协作校稿的并行合入、冲突、批注、发布、清除、重启）
.venv/bin/python -m pytest -q

# 协作校稿完整 HTTP 端到端验证（真实 uvicorn + 临时 SQLite + 真实重启）
.venv/bin/python scripts/e2e_proofreading.py
```

`e2e_proofreading.py` 覆盖：负责人凭据、每场访谈单会话、20 个并行校稿人基于同一版的合入（无丢更新）、旧版提案的重放与冲突拒绝、提交编号幂等、批注平移/失位/阻止发布/重定位/解决、发布版号校验与原子发布、清除后范围收缩与正文删除、以及重启后草稿与结果的继续可用。

`pytest` 额外覆盖：发布期间并发普通读取只看到整旧或整新（无半套）、被保留片段上的批注不阻挡其余片段发布。

复跑结果（本仓库当前状态）：

- `pytest -q`：**71 passed**（含第 9 节访谈补贴结算的 13 个服务/并发测试）；
- `scripts/e2e_proofreading.py`：**61 项检查全部通过**；
- `scripts/e2e_demo.py`（原有功能回归）：**62 项检查全部通过**。

> 说明：本环境没有预装 `.venv`，可用 `python3 -m venv .venv` 后 `.venv/bin/pip install -r requirements.txt` 重建；若系统禁止 `ensurepip`，可用 `python3 -m pip --user --break-system-packages install -r requirements.txt` 后直接运行 `.venv/bin/python`（venv 用 `--system-site-packages` 创建即可）。


## 8. 访谈预约与批量改期（新增）

负责人在现有访谈之上登记**预约**（房间 + 参与者 + UTC 半开区间），并可对一组预约发起**批量改期征询**。征询期间原预约始终是确认结果；只有全部受影响参与者同意后，整套新安排才在一个事务里整体生效。

### 8.1 数据与状态

| 表 | 内容 |
|---|---|
| `rooms` | 会议室标识与名称 |
| `bookings` | 预约：访谈、房间、UTC 起止时间、`confirmed/cancelled`；部分唯一索引保证每场访谈最多一条未取消预约 |
| `booking_participants` | 预约的固定参与者名单（改期不改名单） |
| `reschedule_orders` | 改期单：`pending/approved/rejected/withdrawn/expired/cancelled_booking` 与征询截止时间 |
| `reschedule_items` | 单内每项的原房间/原时段与拟用房间/拟用时段 |
| `reschedule_responses` | 每个参与者每单最多一条不可更改的首次答复 |

### 8.2 接口

负责人接口（`X-Owner-Token`）：

| 方法/路径 | 用途 |
|---|---|
| `POST /owner/rooms` / `GET /owner/rooms` | 登记/列出会议室 |
| `POST /owner/bookings` | 为现有访谈登记预约 |
| `GET /owner/bookings` | 查看预约，可按 `status`/`room_id`/`interview_id` 过滤 |
| `GET /owner/bookings/{id}` | 查看单条预约 |
| `POST /owner/bookings/{id}/cancel?reason=` | 取消尚未开始的预约 |
| `POST /owner/reschedule-orders` | 发起批量改期征询 |
| `GET /owner/reschedule-orders` | 列出改期单，可按 `status` 过滤 |
| `GET /owner/reschedule-orders/{id}` | 改期单详情（含每项、参与者与答复汇总） |
| `POST /owner/reschedule-orders/{id}/withdraw` | 负责人撤回待答复征询 |
| `POST /owner/reschedule-orders/sweep-expired` | 显式结算已到期征询（读路径也会惰性结算） |

参与者接口（现有会话凭据，头 `X-Session-Token` 或 `Authorization: Bearer <token>`）：

| 方法/路径 | 用途 |
|---|---|
| `GET /me/calendar` | 自己的确认预约 + 涉及自己的改期单及整单状态 |
| `GET /me/reschedule-orders` | 涉及自己的改期单列表 |
| `GET /me/reschedule-orders/{id}` | 单张涉及自己的改期单详情 |
| `POST /me/reschedule-orders/{id}/responses` | 替自己答复 `accepted`/`rejected` |

### 8.3 预约规则

- 新预约必须**尚未开始**、`end_at > start_at`，且至少含一名有效（`active`）参与者；时间必须带时区（统一归一化为 UTC）。
- 每场访谈最多一条未取消预约（`409 active_booking_exists`）。
- 任一会议室或参与者在**相交**区间内不能被重复占用；区间为半开 `[start, end)`，首尾相接（一场 `end` 等于另一场 `start`）不算相交。
- 只能取消尚未开始的确认预约（`409 booking_already_started`）。

### 8.4 改期征询规则

- 改期单指定一组**尚未开始的确认预约**、各自的新房间/新时段和征询截止时间；参与者名单不变。
- 截止时间必须晚于当前时间，且**不晚于**任一原时段或新时段的开始（`422 deadline_must_be_future` / `deadline_too_late`）。
- 每项至少改变房间或时段（`422 reschedule_item_unchanged`），单内预约不得重复。
- 同一预约不能同时参加两张待答复单（`409 booking_in_other_pending_order`）。
- 整套新安排必须**内部兼容**（同房间、同参与者均不得重叠），且不与单外确认预约或其它待答复单撞档；但可以使用同单其他预约**将让出**的原时段（见 8.6 的互换示例）。
- 征询期间原安排仍是确认结果：**原时段与拟用时段都被持有**，单外预约既不能占用原时段，也不能占用拟用时段。

### 8.5 答复、生效与失效

- 参与者只能替自己答复；一人涉及多场也只在单上答复一次。首次有效答复不可更改：重复相同答复返回 `200` 且 `idempotent_replay=true`，改成相反答复返回 `409 response_already_recorded`。
- 任一人拒绝（`rejected`）、负责人撤回（`withdrawn`）、到达截止时间（`expired`）或单内任一原预约被取消（`cancelled_booking`）都会结束征询：拟用时段立即释放，原预约保持不变。
- **所有人同意**时，整套新安排在一个 `BEGIN IMMEDIATE` 事务里一次生效（`approved`），不再使用的原时段随即释放；答复重试不会再次改期，结束的单不能因迟到答复生效（`409 reschedule_not_pending`）。
- 取消单内任一原预约只使整单失效，不挪动其余预约。

### 8.6 与清除、会话和重启的关系

- 参与者申请清除时，在**同一事务**内取消其尚未开始的预约，并使涉及这些预约的待答复征询失效（状态 `cancelled_booking`）；其余人的其它预约照常确认。
- 会话失效（注销）本身**不**取消预约；已失效的会话不能再答复。
- 截止时间在进程停机期间到达时，启动恢复（`recover_rescheduling`）会把单结算为 `expired`，拟用时段不再被占用；读路径和每次写操作也都会惰性结算到期单。

### 8.7 复跑说明

```bash
cd /workspace

# 单元/并发测试（含预约撞档、改期状态机、清除集成、重启恢复与线程竞争）
.venv/bin/python -m pytest -q

# 完整 HTTP 端到端验证（真实 uvicorn + 临时 SQLite + 真实重启）
.venv/bin/python scripts/e2e_scheduling.py
```

`e2e_scheduling.py` 的三个重点场景：

1. **原时段互换**：两场访谈在一张单里交换房间，征询期间原/拟时段都被占用，全员同意后一次换档；
2. **征询失败后容量恢复**：拒绝后拟用房间时段立即可被新预约使用；
3. **最终换档对日历查询的影响**：`GET /me/calendar` 在征询期间显示原安排，`approved` 后只显示新安排。

最近一次运行结果：**46 项检查全部通过**。


## 9. 访谈补贴结算（新增）

预约/名单只证明“谁本来要来”，不能直接作为发补贴依据。补贴模块以 `attendance_records` 的实际出席分钟数为唯一计费来源；不接入真实付款渠道，只生成交给线下付款人员的固定清单并登记线下结果。

金额接口以“元”为单位（支持两位小数），数据库用整数分存储；下例均为整数元。

### 9.1 计费与对账口径

1. 只能为**已结束且未取消**的预约登记/更正出席；人必须在预约固定名单内，分钟数不得超过预约时长。
2. 只有已登记出席计费：`floor(attended_minutes / 15) × 10` 元；例如 59 分钟计 30 元。
3. 按预约 `start_at` 的 UTC 日期归入周一开始的自然周。
4. 同一人同一周有 100 元上限；按场次开始先后逐场占用上限。更正较早场次会实时改变较晚场次当前“计入金额”。
5. `(booking_id, participant_id)` 是主键，同一场对同一人最多一条出席计费记录，不能重复计费。
6. 每人每周：
   - `current_payable` / `payable_amount`：按当前出席和周上限计算的当前应付；
   - `pending_amount` / `pending_confirmation`：已交付清单但尚未确认 `paid`/`unpaid` 的行金额；
   - `gross_paid`：已确认付款总额；
   - `refunded_amount`：实收退回总额；
   - `net_paid`：`gross_paid - refunded_amount`；
   - `sendable_amount` / `available_amount` / `now_payable`：`max(0, current_payable - net_paid - pending_amount)`，即现在可提交新批次的完整正金额；
   - `overpaid_amount`：`max(0, net_paid - current_payable)`。
7. 批次任一行不等于当时完整可发正金额时，整批 `409 subsidy_batch_not_full_sendable_amount`，不写入任何清单行。
8. 成立的批次有固定 `line_no`、固定金额和批次编号；待确认行持续占用可发余额。相同请求同编号重试返回 `200 idempotent_replay=true` 和原行 ID；同编号换内容返回 `409 batch_id_reused_for_different_request`。
9. 行结果只能是 `paid` 或 `unpaid`。同一结果重试为幂等重放，改成另一结果返回 `409 payment_result_already_recorded`。`unpaid` 释放预留，可另建新批次重发；原清单和行号仍保留。
10. 已确认 `paid` 的行可登记退回。退回编号重试不重复入账；同编号换行/换金额返回 `409 refund_id_reused_for_different_content`；累计退回超过该行固定付款金额返回 `409 refund_amount_exceeds_line_payment`。
11. 出席更正只改变“当前计入金额/当前应付”，不修改已交付批次行、已确认结果或退回。不同人、不同周的余额分别聚合，不互相抵扣。
12. 个人资料清除后，内部 participant ID、出席、批次、确认和退回仍保留用于对账；这些接口不恢复资料正文、会话 token 或普通参与者入口。会话被撤销后，`/me/subsidies` 仍返回 `401`。

### 9.2 HTTP 接口

负责人接口仍需 `X-Owner-Token`；参与者接口使用现有 `X-Session-Token` 或 `Authorization: Bearer <token>`，且参与者 ID 只从会话解析。

| 方法/路径 | 用途 |
|---|---|
| `PUT/POST /owner/bookings/{booking_id}/attendance` | 批量登记/更正名单内人员分钟数；体为 `{"records":[{"participant_id":"...","attended_minutes":45}]}`，也接受 `attendance`/`items`/`minutes_by_participant`；等价集合路径 `POST /owner/attendance`（体带 `booking_id`） |
| `PUT/POST /owner/bookings/{booking_id}/attendance/{participant_id}` | 登记/更正单人分钟数：`{"attended_minutes":45}` |
| `GET /owner/bookings/{booking_id}/attendance` | 查看名单、未登记人员、原始金额、周上限分摊后本场计入金额 |
| `GET /owner/bookings/{booking_id}/subsidy` | 上一接口的补贴语义别名 |
| `GET /owner/subsidies/session-entries?week_start=YYYY-MM-DD&participant_id=...` | 查看每场当前计入金额及 cap 前后余额 |
| `GET /owner/subsidies/weeks` | 列出所有周及每人每周余额 |
| `GET /owner/subsidies/weeks/{week_start}` | 一周余额、场次明细和批次清单；`week_start` 可传周一或该周任意日期，响应归一化为周一 |
| `GET /owner/subsidies/balances?week_start=...&participant_id=...` | 余额查询 |
| `GET /owner/subsidies/participants/{participant_id}/weeks/{week_start}` | 负责人按内部编号查某人某周 |
| `POST /owner/subsidy-batches` | 创建付款批次 |
| `POST /owner/subsidy-weeks/{week_start}/batches` | 选定一周创建批次的等价路径 |
| `PUT/POST /owner/subsidy-batches/{batch_id}` / `.../issue` | 在路径中指定批次编号 |
| `GET /owner/subsidy-batches?week_start=...` / `GET /owner/subsidy-batches/{batch_id}` | 查看固定清单、行状态和退回；同组路径也提供 `/owner/payment-batches`、`/owner/payout-batches` 别名 |
| `GET /owner/subsidy-batches/{batch_id}/checklist?format=csv\|markdown\|json` | 导出可交给线下付款人员的固定行号/金额清单 |
| `POST /owner/subsidy-batches/{batch_id}/lines/{line_no}/confirm` | 体：`{"result":"paid"}` 或 `{"result":"unpaid"}` |
| `POST .../lines/{line_no}/confirm-paid` / `confirm-unpaid` | 不带体也可确认对应结果 |
| `POST /owner/subsidy-lines/{line_id}/confirm` | 用内部行 ID 确认 |
| `POST /owner/subsidy-batches/{batch_id}/lines/{line_no}/refunds` | 登记退回：`{"refund_id":"rf-1","amount":20}` |
| `POST /owner/subsidy-lines/{line_id}/refunds` | 用内部行 ID 登记退回 |
| `POST /owner/subsidy-refunds/{refund_id}` | 在路径指定退回编号；体给 `amount` 和 `line_id` 或 `batch_id`+`line_no`；别名 `POST /owner/refunds`、`POST /owner/refunds/{refund_id}` |
| `GET /owner/subsidy-refunds/{refund_id}` | 查询单笔退回 |
| `GET /me/subsidies` / `GET /me/subsidies/{week_start}` | 参与者用现有会话只看自己的结算；别名 `/me/subsidy`、`/me/subsidy-settlements`、`/me/settlements` |

创建批次示例：

```bash
curl -s -X POST "$BASE/owner/subsidy-weeks/2026-11-23/batches" \
  -H "X-Owner-Token: $PDC_OWNER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "batch_id": "payout-2026w48-01",
    "lines": [
      {"participant_id": "p-alice", "expected_amount": 100},
      {"participant_id": "p-bob", "expected_amount": 40}
    ]
  }' | python -m json.tool
```

线下人员按返回的 `lines[].line_no` 和 `amount` 付款。例如确认第 1 行已付款：

```bash
curl -s -X POST \
  "$BASE/owner/subsidy-batches/payout-2026w48-01/lines/1/confirm-paid" \
  -H "X-Owner-Token: $PDC_OWNER_TOKEN"
```

收到 20 元退回：

```bash
curl -s -X POST \
  "$BASE/owner/subsidy-batches/payout-2026w48-01/lines/1/refunds" \
  -H "X-Owner-Token: $PDC_OWNER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"refund_id":"return-001","amount":20}'
```

### 9.3 并发、持久化和复跑

所有发单、确认、退回和出席更正都在单个 `BEGIN IMMEDIATE` 事务内完成“读取当前余额/状态 → 校验 → 写入”。因此：

- 两个不同批次竞争同一人的同一笔可发余额时，只有一个整批成立，另一个收到 `409`，不会重复预留；
- 同一个批次编号并发重试时，只有一个请求创建清单，其余得到同一原清单；
- 退回编号和行结果有持久化唯一/状态检查，并发退回不会重复入账或突破行付款上限；
- 批次主表和行表在同一事务提交，不会出现半批清单；
- 所有清单、行结果和退回均落 SQLite，重启后可继续按原批次编号和行号确认。

复跑：

```bash
cd /workspace

# 服务层规则、更正跨场重分配、幂等、退回上限、资料清除、线程竞争、重开 DB
.venv/bin/python -m pytest -q tests/test_subsidies.py

# 全量回归
.venv/bin/python -m pytest -q

# 真实 uvicorn + 临时 SQLite + 真实重启的 HTTP 端到端验证
.venv/bin/python scripts/e2e_subsidies.py
```

端到端脚本明确验证：更正较早场次引起较晚场次计入金额 `40 -> 80`、待确认期间重复发单被拒、未付款释放后另建清单、付款后更正形成多付、部分退回后的对账、资料清除后内部编号仍可发单、HTTP 并发发单以及重启后按原清单确认。

本仓库当前复跑结果：`pytest -q` 为 **71 passed**（其中 `tests/test_subsidies.py` 13 项，含并发发单、同批次号竞争、并发更正、混合确认、退回上限和重开 DB）；`scripts/e2e_subsidies.py` 为 **41 项检查全部通过**。原有 `e2e_demo.py`（62 项）、`e2e_proofreading.py`（61 项）、`e2e_scheduling.py`（46 项）也已复跑通过。


## 10. 一致性设计要点

1. **普通隐藏谓词**：只要片段的任一参与者处于 `deletion_requested`，该片段就从普通 SQL 结果中排除。
2. **执行时重读当前状态**：预览不持锁、不固化片段列表；执行事务重新查询，预览后新增且已提交的相关片段一定会进入任务。
3. **写入顺序竞争**：新片段/资料写入与清除申请都使用 `BEGIN IMMEDIATE`。要么新写入先提交并被清除事务看到，要么清除先提交并阻止新写入。
4. **保留/清除顺序**：创建保留、解除保留、清除均在立即写事务内检查当前 `segments` 行和有效保留。最后一个保留解除与物理删除共用同一事务。
5. **导出不存正文快照**：`export_requests` 只保存 scope；每次 `/content` 都通过 `BEGIN IMMEDIATE` 与清除/保留提交串行排序，并重新执行当前可见性 SQL，不会使用旧正文快照。
6. **重启恢复**：request/item 状态先持久化；启动时扫描 `hidden_pending_purge`/`retained` 并重新协调。只有 `DELETE FROM segments` 与 `status='purged'` 在同一事务提交后才报告已清除。
7. **多参与者共享片段**：删除前把全部参与者 ID 写入 IDs-only `segment_impact_index`。即使片段因第一人申请已物理删除，第二人申请仍能从处理索引中得到 `purged` 记录。
8. **协作校稿并行合入**：提案按 `base_version` 解释，并逐版重放此后**实际存储的编辑操作**；冲突只按操作本身判定，不依赖文本 diff，所以改回原文也抹不掉历史冲突。全部合入与发布都在 `BEGIN IMMEDIATE` 事务内完成。
9. **发布原子性**：一次发布在同一事务内改写所有可发布片段的正式正文；普通入口要么看到全部旧正文，要么看到全部新正文。
10. **批注锚点不做猜测**：只有避开所指文字的改动才平移锚点；替换、删除或内部插入一律标记失位，需负责人显式重定位或解决。
11. **清除与校稿同事务清理**：物理删除片段正文时，在同一事务中删除该片段的草稿历史、替换文字和批注文字，并清洗已存合入结果中的回显文本。
12. **预约/改期全串行提交**：预约创建、取消、改期开单、答复、撤回和清除集成都在单个 `BEGIN IMMEDIATE` 事务内完成检查与写入；日历等多语句查询使用只读快照事务，因此任何查询都只能看到整旧或整新的安排，看不到只换了一部分的中间态。
13. **征询期间双持有**：待答复改期单同时持有每项的原时段与拟用时段（房间维度和参与者维度），单外预约与其它待答复单都无法占用；同单项目可以复用同单将让出的时段，从而支持原时段互换。征询一旦终止（拒绝/撤回/到期/取消），拟用时段随状态翻转立即释放。
14. **到期不靠定时器**：到期结算在每次写事务、读路径和启动恢复中惰性执行；停机期间到期的单在重启时结算为 `expired`，不会继续占用拟用时段。全员同意后的批量换档与改期单状态翻转共用同一事务提交。
15. **补贴当前金额与已交付清单分离**：周 cap 分摊始终由当前 `attendance_records` 重算，因此更正会改变跨场计入金额；批次行金额一旦提交即固化，后续更正只通过当前应付、待确认、净实付和多付余额参与对账，绝不回写原清单。
16. **补贴发单全串行校验**：批次创建在一个 `BEGIN IMMEDIATE` 事务内逐行核对“当前完整可发金额”，通过后同时写批次和全部行；并发批次要么整批预留成功，要么整批拒绝，不会重复预留或出现半批清单。行确认、退回累计和幂等编号也在同一串行锁内检查。
17. **补贴义务不含正文**：补贴表只保存内部 participant ID、预约/行编号、分钟数和金额，不复制姓名、资料或片段正文；资料清除后可继续按内部编号对账，但不会恢复账号、正文或参与者会话入口。

这是产品内约定的保留规则和演示级 owner token；若用于真实部署，应把 `require_owner` 替换为实际身份认证/授权，并评估 SQLite 文件层的安全删除与备份策略。
