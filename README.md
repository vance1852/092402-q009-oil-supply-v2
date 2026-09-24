# 油气供应韧性与现场准入服务

本项目是一套可直接运行的 Python 服务端系统，用于记录原油基准报价、油田与终端设施、输送线路、库存批次、日提名和供应情景，并保留油田巡检机器人统计准入流程。系统面向价格连续波动、关键输油线路恢复、库存调拨和现场设备验证同时发生的运营环境，让调度、风险和审计人员在同一个 SQLite 数据库中获得可追溯结论。

供应调度子域提供以下能力：

- 原油基准报价按交易日和来源修订登记，历史版本不会被覆盖；
- 油田、储罐、终端与炼厂设施建档，线路保存日能力、在途时间和损耗规则；
- 线路停运或降容事件按 UTC 时间区间生效，日分配会计算实际可用能力；
- 库存批次保留油品、牌号、数量、单位成本和接收时间，可计算加权库存成本；
- 托运提名支持载荷级幂等、优先级分配、库存扣减和在途交接；
- 线路按 IANA 时区配置当地营业日、跨午夜交接窗口、节假日与调休例外，依据夏令时规则计算下一可接收时刻，发运时冻结日历版本与时区数据库版本；
- 终端登记部分到货、质量待验和最终签收，扫描码幂等（重复扫描不累计），只有最终签收数量结转提名；超时按冻结承诺判定，日历修订不改变在途承诺但可预览影响；
- 供应情景保存价格变化、线路能力变化和需求变化，审批后产生可重放的确定性结果；
- 关键写操作进入哈希串联审计日志，可离线验证事件顺序和内容完整性。

现场准入子域位于 `robot_trials` 包，负责油田巡检机器人的设备构建登记、不可变试验协议、观测分片导入、异常观测复核、统计任务租约、准入决定和审计报告。该子域不连接机器人硬件，只处理已经结构化的试验记录。

## 目录

- `src/oil_supply/`：报价、设施、线路、库存、提名、供应情景、HTTP API 与离线验收；
- `src/robot_trials/`：油田巡检机器人试验与统计准入；
- `fixtures/`：现场准入演示协议和结构化观测；
- `tests/`：核心规则、错误边界、API 和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 无第三方运行依赖

在依赖已经准备好的容器中安装：

```bash
python3 -m pip install --no-index --no-deps .
```

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试使用内存数据库和临时目录，不访问公网，也不会启动常驻服务。

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m oil_supply.acceptance --workspace .
```

该命令会在内存数据库中登记六个交易日的布伦特报价，创建油田、终端和输送线路，完成库存入账、提名分配、发运及供应情景分析，最后输出一行 JSON。成功时退出码为 `0` 且 `status` 为 `ok`。

现场准入子域也保留独立验收入口：

```bash
PYTHONPATH=src python3 -m robot_trials.acceptance --workspace .
```

## HTTP 服务

```bash
PYTHONPATH=src python3 -m oil_supply.api --database oil_supply.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查为 `GET /health`。除健康检查外，请求通过 `X-Actor-Id` 携带操作者编号。可用接口覆盖报价、设施、线路、停运事件、库存批次、提名、能力分配、发运、供应情景和审计链。服务重启后，SQLite 中的业务状态和历史版本会继续保留。

### 营业日历与终端签收

- `PUT /routes/{route_id}/calendar`（planner）：设置营业日（`business_days`，mon–sun 缩写）、按星期的交接窗口 `windows`（`opens`/`closes` 为终端当地时间，`closes` 早于 `opens` 表示跨午夜班次）、`holidays` 节假日、`extra_workdays` 调休上班日和 `sla_grace_minutes` 宽限分钟。时区必须与终端设施一致。每次内容变化产生新的不可变修订，并记录当时的 IANA 时区数据库版本。
- `GET /routes/{route_id}/calendar` 与 `GET /routes/{route_id}/calendar/revisions`：读取当前版本和修订历史。
- `POST /routes/{route_id}/calendar/preview`（planner）：用候选日历预演所有在途单，返回每个转运单冻结承诺与候选时刻的差异，但不落库、不改变承诺。
- 发运（`POST /transfers`）按当时日历计算窗口开门、下一可接收时刻和承诺截止并冻结到转运记录；此后日历修订不影响该单。
- `POST /transfers/receipts`（dispatcher）：`stage` 为 `partial`、`quality_pending` 或 `final`，`scan_code` 全局唯一——同一扫描码重复提交原样返回且不重复累计；同一扫描码用于其他转运单返回 409。部分到货与质量待验不结转提名，只有最终签收写入提名 `delivered_barrels`。
- `GET /transfers/{transfer_id}`：返回窗口、承诺、超时标记和各类到货数量汇总；`POST /transfers/overdue/sweep` 批量重算超时。超时只依据数据库中冻结的承诺截止时间，因此服务重启后判定一致。

日历换算遵循 PEP 495：春季缺口（不存在的本地时刻）前移到跳时后的第一个真实瞬间；秋季重复小时中，窗口起点取第一次、终点取第二次，跨重复小时的班次物理覆盖两次本地小时。
