# data-qd-177 · 锯材切割工单

将订单逐件映射到同材原木，生成毫米切割坐标、锯缝损耗和可回收余料。

## 独立安装与测试

Python >=3.11，本批实测3.12.14。运行与unittest仅用标准库，构建工具精确固定。每仓独立venv，可复用公共下载缓存。

```sh
uv venv --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
```

以上离线命令需要缓存已有setuptools==80.9.0和wheel==0.45.1；无缓存时先准备这两个公共构建工具，不声称空缓存可离线安装。修改源码后重装本仓。

## 端到端演示

在仓库根目录执行，输出路径必须不存在。重复演示请换新路径。

```sh
.venv/bin/cut-ticket --source fixtures/input.json --output run/report.json
```

预期：完成5件、锯缝15mm、可回收余料171mm；用掉2根原材。

输入或业务规则无效、输出已存在时退出2，保留原目标；成功退出0。报告/数据库先完整生成再原子新建，无覆盖。run/为忽略目录。

## 跨批次库存工作区

`stock-workspace` 在单批命令之上提供跨批次余料库存：可回收余料在提交时入库，后续批次可直接使用。单批 `cut-ticket` 行为不变。

```sh
.venv/bin/stock-workspace init --source fixtures/stock.json --workspace run/ws
.venv/bin/stock-workspace plan --workspace run/ws --source fixtures/batch1.json --output run/plan1.json
.venv/bin/stock-workspace commit --workspace run/ws --plan run/plan1.json --output run/result1.json
.venv/bin/stock-workspace plan --workspace run/ws --source fixtures/batch2.json --output run/plan2.json
.venv/bin/stock-workspace commit --workspace run/ws --plan run/plan2.json --output run/result2.json
.venv/bin/stock-workspace status --workspace run/ws
.venv/bin/stock-workspace status --workspace run/ws --job batch-1
```

预期：batch-1 消耗2根原材并入库一段171mm橡木余料；batch-2 只用该余料切出160mm板条，库存修订版升到2。

- `init` 从初始原材清单（沿用 `stock` 行契约）建立工作区，目标路径必须不存在。
- `plan` 在当前库存修订版上生成预览计划，不改变库存；计划记录所依据的修订版与完整请求身份。批次输入沿用 `orders`、`kerf_mm`、`min_offcut_mm` 规则，另需非空稳定的 `job_id`。
- `commit` 仅当计划的修订版仍是当前版本才生效：消耗实际分配到切割票的库存条目，同批可回收余料同时入库。过期计划、篡改版计划、跨工作区计划退出2，库存不变。
- `job_id` 是幂等身份：相同请求与计划重试返回原结果且不重复扣料；同一 `job_id` 携带不同请求、计划或来源修订版退出2。
- `status` 查询当前库存（含已消耗条目及其消费工单），`--job` 查询已提交批次结果。
- 工作区状态单文件原子替换，提交经文件锁串行化：进程中途崩溃不产生半次扣料或半份余料，并发提交同一修订版至多一个成功。所有失败退出2并保留已有工作区、提交记录和输出文件。

### 质量事件追溯与隔离

发现某批原材质量问题后，登记事件即可追溯其已进入哪些切割票，并隔离其剩余余料后代。

```sh
.venv/bin/stock-workspace incident register --workspace run/ws --source fixtures/incident.json --output run/incident.json
.venv/bin/stock-workspace incident status --workspace run/ws
.venv/bin/stock-workspace incident status --workspace run/ws --incident QC-2026-111
.venv/bin/stock-workspace incident release --workspace run/ws --incident QC-2026-111 --output run/release.json
```

- 事件输入 `incident_id` 与一个或多个初始原材 `stock_id`（`stock_ids`，兼容单值 `stock_id`），覆盖这些根原材及其逐批产生的全部余料后代。
- `register` 生成稳定报告：每个根下当前及历史全部库存条目，以及所有已提交切割票中由该根切出的成品件（批次、订单、件号、库存条目身份、切割坐标）。已提交切件与既有批次报告保持不变；事件只标记影响，不撤销切割或库存消耗。
- `status` 区分 `available`、`consumed`、`isolated`（因活动质量事件暂不可用），并显示每个条目的 `active_incidents`。
- 根原材仍有活动事件时，其全部可用余料后代不得进入新计划。`release` 只关闭指定事件，仍被其他事件覆盖的根继续隔离。
- 首次登记或解除使可用库存变化时推进库存修订版，使此前生成的旧计划因过期而无法提交。事件永远不会把已消耗条目恢复为可用。
- 同一事件重放返回原结果；同 id 不同原材集合、已解除事件重新激活、未知原材、空/重复集合、非法输入、已存在且内容不同的输出均退出2，并保留此前状态。

## 契约与边界

详见[数据契约](docs/contract.md)。采用确定性best-fit decreasing可行启发式，不保证最优。缺件是有效业务结果，见summary.complete及unfilled。

样例均为本仓原创合成数据，来源见[说明](fixtures/SOURCES.md)，代码/样例MIT许可。业务演示和测试不访问网络，不需要容器或外部服务。
