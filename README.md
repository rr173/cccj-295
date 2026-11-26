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

并发顺序测试：

```bash
.venv/bin/python -m pytest -q
```

包含两组各 25 轮线程竞争：

- 清除执行 vs 创建保留；
- 解除最后旧保留 vs 同时创建新保留。

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

## 6. 一致性设计要点

1. **普通隐藏谓词**：只要片段的任一参与者处于 `deletion_requested`，该片段就从普通 SQL 结果中排除。
2. **执行时重读当前状态**：预览不持锁、不固化片段列表；执行事务重新查询，预览后新增且已提交的相关片段一定会进入任务。
3. **写入顺序竞争**：新片段/资料写入与清除申请都使用 `BEGIN IMMEDIATE`。要么新写入先提交并被清除事务看到，要么清除先提交并阻止新写入。
4. **保留/清除顺序**：创建保留、解除保留、清除均在立即写事务内检查当前 `segments` 行和有效保留。最后一个保留解除与物理删除共用同一事务。
5. **导出不存正文快照**：`export_requests` 只保存 scope；每次 `/content` 都通过 `BEGIN IMMEDIATE` 与清除/保留提交串行排序，并重新执行当前可见性 SQL，不会使用旧正文快照。
6. **重启恢复**：request/item 状态先持久化；启动时扫描 `hidden_pending_purge`/`retained` 并重新协调。只有 `DELETE FROM segments` 与 `status='purged'` 在同一事务提交后才报告已清除。
7. **多参与者共享片段**：删除前把全部参与者 ID 写入 IDs-only `segment_impact_index`。即使片段因第一人申请已物理删除，第二人申请仍能从处理索引中得到 `purged` 记录。

这是产品内约定的保留规则和演示级 owner token；若用于真实部署，应把 `require_owner` 替换为实际身份认证/授权，并评估 SQLite 文件层的安全删除与备份策略。
