# 关键装备供应商资格平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景与硬件稳定性准入。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险和审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配和情景分析；
- `src/accelerator_lab/`：加速卡测点导入、排除复核、分析任务租约和准入决定；
- `src/silicon_qualification/`：AI 加速芯片批次、测量、分析与质量审批；
- `src/supplier_qualification/`：关键供应商企业资质与有效范围、获准产品、工厂、质量事件、暂停决定、采购订单交付批次适用性核对与紧急替代双批准；
- `fixtures/`：离线验收使用的结构化协议与测点；
- `tests/`：核心规则、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m compute_fabric.acceptance --workspace .
PYTHONPATH=src python3 -m accelerator_lab.acceptance --workspace .
PYTHONPATH=src python3 -m silicon_qualification.acceptance
PYTHONPATH=src python3 -m supplier_qualification.acceptance --workspace .
```

这些命令使用临时 SQLite 数据库完成站点、资源、预约、分配、测点分析、芯片准入和关键绝缘材料供应商资格/紧急替代流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m compute_fabric.api --database compute.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m accelerator_lab.api --database lab.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m silicon_qualification.api --database silicon.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m supplier_qualification.api --database supplier.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON（供应商资格服务通过 `X-Actor-Id` 头识别操作者）。进程重启后可以继续查询 SQLite 中的业务状态和审计历史。

### 关键供应商资格服务规则

- 企业资质（`certificates`）记录资质类型、有效范围文本与有效期；资质可续期，新证通过 `supersedes_certificate_id` 接替旧证，旧证在自身有效期内仍覆盖，授权核对沿接替链解析当日有效资质。
- 获准产品（`approved_products`）把资质范围落到物料/工厂/规格维度；工厂（`plants`）独立登记，资格核对时校验工厂归属与状态。
- 发货闸门：未发货订单必须先登记交付批次（`order_lots`）且批次检测 `passed`，且当时无暂停、无未关闭严重质量事件、资质与授权当日有效，才能发运；发运时固化合规快照，验收后不随后续资格变化回溯。
- 质量事件（`quality_events`）与暂停决定（`suspensions`）按供应商+物料+工厂范围影响订单：未发货（`open`）订单置 `review_required`，已发货/已验收订单分类为 `retained` 按当时规则保留；`qualification_change_impacts` 记录每次变化影响的订单，可按变化单或按订单双向查询。
- 紧急替代（`emergency_approvals`）须质量（`emergency.quality`）与采购（`emergency.procurement`）分别批准，仅对批准载明的数量、计量单位、物料与有效期限、且仅对所申请订单生效。
- 角色：`qual_engineer`（资质/产品/工厂/续期）、`quality`（事件/暂停/质量批准）、`buyer`（订单/批次/采购批准/复核）、`auditor`（只读与哈希审计链）。
