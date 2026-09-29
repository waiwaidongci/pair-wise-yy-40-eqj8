# 建筑抗震鉴定与加固排序

依据结构、用途、人员密度和历史缺陷生成鉴定与加固优先级。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/dependencies.py`：结构依赖图判断（成环检查、开工闸门、失效判定）。
- `src/repository.py`：SQLite建表、事务、版本控制、依赖关系存储和审计链。
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
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/dependencies`：为下游项目（路径id）登记上游项目、共用部位和依据（`upstream_id`、`shared_part`、`basis`）
- `GET /api/items/{id}/dependencies?direction=upstream|downstream|both&status=active|invalidated`
- `GET /api/audit`

允许角色：assessor, structural_engineer, review_board, viewer。风险分值和人员密度共同影响排序；审核通过前必须完成评估、设计和施工证据登记。

### 结构依赖规则

几栋楼共用连廊或同一传力体系时，下游楼必须登记对上游楼的依赖（共用部位与依据）：

- 同一对（上游、下游）关系只保留一条，重复登记返回409；自依赖与任何闭环均拒绝（`CycleError`，响应中带`cycle`回路）。
- 下游转入`construction`（开工）前，其**全部**上游必须已`accepted`（验收），任一未验收或依据已失效都会阻止开工。
- 上游被驳回（accepted→rejected）后：尚未开工（未进入construction）的下游，其依据标记为`invalidated`并在失效依据清单中列出，需重新登记；已开工下游的关系记录保留为active。
- 失效关系按同一对重新登记即重新生效，不新增重复行。
- 成环检查与关系插入在同一立即事务内完成，并发登记无法绕过。


## 测试

```bash
python3 -m unittest discover -s tests -v
```
