# 建筑抗震鉴定与加固排序

依据结构、用途、人员密度和历史缺陷生成鉴定与加固优先级。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、关闭不变量和结构依赖判断（成环/依据效力/施工闸门）。
- `src/repository.py`：SQLite建表、事务、版本控制、审计链和依赖关系存储。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8317
```

默认端口为`8317`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`，返回中附带`upstream_basis`与`invalid_basis`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`
- `POST /api/dependencies`，登记结构依赖边
- `GET /api/dependencies?downstream_item_id=&upstream_item_id=`，按上下游筛选
- `GET /api/items/{id}/dependencies`，单项目依赖视图（上游依据、阻断依据、失效依据）

允许角色：assessor, structural_engineer, review_board, viewer。风险分值和人员密度共同影响排序；审核通过前必须完成评估、设计和施工证据登记。

## 结构依赖（共用连廊/传力体系）

几栋楼共用连廊或同一传力体系时，按下述规则管理加固顺序：

- 登记内容：`upstream_item_id`（上游先加固项目）、`downstream_item_id`（下游项目）、`shared_part`（共用部位）、`basis`（依据，如传力计算书编号）。允许角色：assessor、structural_engineer。
- 同一对上下游只保留一条关系（数据库唯一约束，重复登记返回409）。
- 登记时做全局成环检查：新增边导致成环（含自环）则拒绝，并在错误响应`details.cycle`中给出首尾闭合的回路；并发登记在同一把事务锁内完成查重与成环判断，无法绕过。
- 下游进入`construction`前，**全部**上游必须为`accepted`；否则返回409，`details.blocked_basis`逐条列出未满足依据（含上游当前状态）。
- 上游被驳回（`rejected`）后：尚未开工（未进入construction）的下游在`invalid_basis`中列出失效依据；已开工下游的施工记录与依赖关系保留，不再告警。
- 依据状态：`pending`（上游未验收）、`satisfied`（已验收）、`invalid`（上游被驳回）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
