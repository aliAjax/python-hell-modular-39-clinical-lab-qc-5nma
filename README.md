# 临床实验室质量控制与结果拦截

只使用Python标准库和SQLite的模块化服务，默认端口`8339`。覆盖检测项目、质控品批次（到货总瓶数）、工作瓶分装与领用、仪器×项目在用容量与排队、开瓶失效、过期重算与退回重做、质控规则、允许范围、仪器校准、连续偏差、趋势、失控、结果拦截、复测、调查、批次切换和历史更正。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：QC规则计算、状态机、工作瓶失效时刻计算、校准与放行约束。
- `src/repository.py`：SQLite持久化、乐观锁、行级占用、业务唯一键（分装单/领用单/瓶号）、幂等和审计查询。
- `src/service.py`：用例编排、权限校验和版本控制。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8339
```

## 核心对象

`assay`为检测项目，`qc_lot`为质控品批次（`total_bottles`记录到货总瓶数），`instrument`为仪器，
`instrument_assay`为仪器×项目绑定（`bottle_capacity`为该槽位允许的在用瓶数，默认1），
`working_bottle`为分装出的工作瓶（瓶号、开瓶日期、失效时刻；状态`in_stock/in_use/consumed/expired`），
`dispense_order`为分装单，`bottle_claim`为领用记录（`fulfilled/queued`），
`qc_run`为质控结果，`result_batch`为患者结果批次。

## 工作瓶流程

1. 到货：创建`qc_lot`时给出`total_bottles`与`open_vial_days`（默认7天）。
2. 分装：`POST /api/qc_lots/<id>/dispense`，按分装单`dispense_order_no`逐瓶给出`bottle_no`、
   `opened_at`和可选`expires_at`；未显式给失效时刻时按开瓶稳定期计算，并以批号失效期封顶；
   累计分装超过到货总瓶数返回409。
3. 领用：
   - 按槽位申领`POST /api/bottle_requests`，有库存且在用数未超容量即按近效期先出占用，否则`queued`排队；
   - 指定瓶`POST /api/working_bottles/<id>/claim`，两人抢最后一瓶时仅一人成功，另一人409并看到占用者；
   - 瓶子消耗/过期后释放槽位，队列按申请顺序自动提升。
4. 过期：`POST /api/working_bottles/<id>/expire`或`POST /api/expired_bottles/sweep`。
   引用该瓶且尚未支撑放行批次的质控结果置为`redo_required`重新计算并退回重做，
   其`waiting`患者批次自动拦截；已`released`批次保留放行时快照的原依据（瓶号/失效时刻/质控run版本）不变。
5. 写入安全：分装单与领用单均为业务幂等键，重试返回已记录单据，不重复生成瓶号、占用或审计/记账；
   旧数据无瓶号/总瓶数时按兼容处理，历史质控仍可按原批号检索。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`及数据字段过滤，如`/api/qc_runs?qc_lot_id=<lot>`
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `POST /api/qc_lots/<id>/dispense`
- `POST /api/bottle_requests`
- `POST /api/working_bottles/<id>/{claim,consume,expire}`
- `POST /api/expired_bottles/sweep`
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建；
分装单和领用单的重试幂等由各自的单号保证。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

质控规则为可运行的简化模型，包含1-3s、连续偏移和趋势检查，但不替代CLIA、ISO 15189、Westgard完整规则集或实验室信息系统接口。
