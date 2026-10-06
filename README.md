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

- `consignment`：检疫批次；`facility`：温室、苗圃或下游种植点。
- `lab_result`：实验室结果，按 `sample_id` 标识采集号。
- `trace_run`：按生效时间重算的追溯账（`in_progress` / `concluded` / `invalidated`）。
- `notification`：发给下游种植点的通知（`pending` / `confirmed` / `voided`）。

## 追溯账规则

- 某批确认带虫（生效的阳性 `lab_result`）后，沿 `parent_id` 链和传播边找出下游种植点，立即重算追溯账。
- 晚到结果改变阳性集合时，未完成（`in_progress`）的追溯立即作废重算；已有结论（`concluded`）保留可查。
- 重算中断后再次调用会从断点续算，已处理的种植点不重复通知。
- 未确认的通知作废重发（新版本），已确认的通知保留原版本。
- 两个查验员提交同一 `sample_id` 时，先到的结果生效，后到的保留为冲突现场记录并列出冲突。
- 旧数据缺传播关系时，升级按原发地/目的地补齐 `parent_id` 和传播边，历史通知仍能打开。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。
- `POST /api/lab-results`：提交实验室结果（同一 `sample_id` 先到生效，后到列为冲突）。
- `GET /api/lab-results`：查询结果，可用 `?sample_id=`、`?status=` 过滤。
- `GET /api/conflicts`：列出冲突的实验室结果。
- `GET /api/trace-runs`、`GET /api/trace-runs/<id>`：查询追溯账。
- `GET /api/notifications`、`GET /api/notifications/<id>`：查询通知。
- `POST /api/notifications/<id>/confirm`：确认通知（保留原版本）。
- `POST /api/consignments/<id>/recompute`：手动触发追溯重算（断点续算）。
- `POST /api/upgrade`：按原发地/目的地补齐旧数据缺失的传播关系（幂等）。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

植物检疫结论和传播链规则是流程演示，不替代法定检疫标准或实验室鉴定。
