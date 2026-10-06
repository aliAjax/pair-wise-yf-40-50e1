# 植物病虫害检疫与传播追溯

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8306`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8306
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `consignment`：检疫批次（含 `origin`/`destination`，在口岸与种植点之间调运）。
- `facility`：温室、苗圃或下游种植点。
- `propagation_links`：按生效时间（`effective_from`/`effective_to`）有效的传播有向边；批次创建时按原发地→目的地自动登记。
- `lab_submissions`：实验室结果，按采集号 `sample_id` 先到生效，后到者以 `conflict` 保留现场记录。
- `trace_runs` / `trace_run_items`：按结果生效时间重算的追溯账（分代 generation），含逐批次断点。
- `notifications`：给下游种植点的追溯通知，按 `(批次, 对象)` 维护版本链。

## 追溯账规则

1. **确认带虫即重算**：某批结果 `pest_found=true` 生效后，从该批目的地出发、沿生效时间点上有效的传播边向下游 BFS，对目的地点和每个下游种植点发通知。
2. **通知版本**：重算时未确认（`issued`）的通知置 `voided` 并以新版本重发（`supersedes_id`/`superseded_by_id` 串联）；已确认（`acknowledged`）的通知保留原版本，不重复通知。
3. **晚到结果**：新的生效结果到达时，该批未完成的追溯账（`pending/running/failed`）立即 `invalidated` 并重新算账；已完成账及其结论原样保留可查。晚到的阴性翻案会把旧结论里未确认通知作废，且不再发阳性通知。
4. **采集号冲突**：两个查验员同时提交同一 `sample_id`，数据库部分唯一索引 + 即时事务保证先到的 `effective`，后到的保留为 `conflict` 现场记录并回链胜者，不驱动追溯账。
5. **断点重试**：重算按批次目标逐条处理并写检查点；通知发行以 `(run_id,target)` 台账幂等。失败后账置 `failed`，从断点恢复，处理过的批次不重复通知。
6. **旧数据升级**：建表用 `PRAGMA user_version` 做增量迁移；历史批次缺传播关系时，按 `origin→destination` 回填（`source=migration`，生效时间取批次创建时间），幂等可重复。历史实体、审计与通知只增不删，链接始终可打开。

## 追溯账接口

- `POST /api/links`：手工登记传播边 `{upstream,downstream,consignment_id,effective_from,effective_to}`。
- `GET /api/links` / `GET /api/links?upstream=...`：查传播边。
- `POST /api/links/backfill`（admin）：按原发地/目的地补旧数据的传播边，返回新增条数。
- `POST /api/consignments/<id>/lab`：提交实验室结果 `{sample_id,pest_found,pest_name,finding,effective_at}`；生效则同步重算，冲突则返回 `conflict` 与双方记录。
- `GET /api/consignments/<id>/lab`、`GET /api/lab?sample_id=&status=`：查结果（含 conflict）。
- `GET /api/consignments/<id>/trace-preview?effective_at=`：只算不落地，预览下游。
- `POST /api/consignments/<id>/recompute`：手动重算（取该批最新生效结果）。
- `GET /api/runs?status=`、`GET /api/runs/<id>`：追溯账列表 / 含逐批次断点的详情。
- `POST /api/runs/<id>/resume`：失败账从断点继续。
- `GET /api/consignments/<id>/runs|notifications`：某批的账与通知（通知默认含 voided，可加 `include_void=false`）。
- `GET /api/notifications`：全部通知版本。
- `POST /api/notifications/<id>/acknowledge`：确认通知（voided 版本不可确认，返回 409）。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

植物检疫结论和传播链规则是流程演示，不替代法定检疫标准或实验室鉴定。
