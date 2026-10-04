# 临床实验室质量控制与结果拦截

只使用Python标准库和SQLite的模块化服务，默认端口`8339`。覆盖检测项目、质控品批次、质控规则、允许范围、仪器校准、连续偏差、趋势、失控、结果拦截、复测、调查、批次切换、历史更正和工作瓶拆分管理。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：QC规则计算、状态机、校准与放行约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等、工作瓶原子操作和审计查询。
- `src/service.py`：用例编排、权限校验、版本控制和工作瓶业务逻辑。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8339
```

## 核心对象

`assay`为检测项目，`qc_lot`为质控品批次，`instrument`为仪器，`qc_run`为质控结果，`result_batch`为患者结果批次，`working_bottle`为工作瓶。

## 工作瓶管理

质控品批次到货后记录总瓶数（`total_bottles`），分装时逐瓶生成工作瓶（`working_bottle`），每瓶有瓶号（`bottle_no`）、开瓶日期（`opened_at`）和失效时刻（`expires_at`）。

- **分装**：`POST /api/qc_lots/<lot_id>/aliquot`，参数`count`为该批次总分装瓶数。瓶号按批次确定（`wb_<lot_id>_<seq>`），写入失败后重试不重复创建。
- **领用**：`POST /api/working_bottles/<bottle_id>/claim`，参数`instrument_id`。每台仪器每个检测项目的在用瓶数有容量（`bottle_capacity`），超出则排队（`queued`）。
- **退瓶**：`POST /api/working_bottles/<bottle_id>/release`，退回后自动提升同仪器同项目最早排队的瓶子。
- **过期**：`POST /api/working_bottles/<bottle_id>/expire`，重算引用该瓶的在途质控结果并退回重做；已放行的患者结果批次保留原依据。
- **批量过期检查**：`POST /api/working_bottles/expire_all`，将所有过期末处理的瓶子标记过期。
- **查询批次下瓶子**：`GET /api/qc_lots/<lot_id>/bottles`。

并发领用最后一瓶时，数据库事务保证只有一人成功，后到者看到瓶子已被占用（`ConflictError`）。

旧数据缺少瓶号时按兼容处理：`qc_run`的`bottle_id`为可选字段，历史质控结果仍可通过`qc_lot_id`查到原批号。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

质控规则为可运行的简化模型，包含1-3s、连续偏移和趋势检查，但不替代CLIA、ISO 15189、Westgard完整规则集或实验室信息系统接口。
