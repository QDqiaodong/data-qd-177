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

### 成本登记、当前成本与连续关账

每根初始原材的采购成本独立登记、整根替换；成本沿切割来源链逐层按毫米整数分摊，关账把两个修订版截点、当时价格、完整成本明细与桥接表一并冻结。

```sh
.venv/bin/stock-workspace init --source fixtures/stock.json --workspace run/ws-cost
.venv/bin/stock-workspace price register --workspace run/ws-cost --source fixtures/price-s1.json --output run/price-s1.json
.venv/bin/stock-workspace price register --workspace run/ws-cost --source fixtures/price-s2.json --output run/price-s2.json
.venv/bin/stock-workspace plan --workspace run/ws-cost --source fixtures/batch1.json --output run/cplan1.json
.venv/bin/stock-workspace commit --workspace run/ws-cost --plan run/cplan1.json --output run/cresult1.json
.venv/bin/stock-workspace plan --workspace run/ws-cost --source fixtures/batch2.json --output run/cplan2.json
.venv/bin/stock-workspace commit --workspace run/ws-cost --plan run/cplan2.json --output run/cresult2.json
.venv/bin/stock-workspace cost --workspace run/ws-cost
.venv/bin/stock-workspace close run --workspace run/ws-cost --source fixtures/close-1.json --output run/close-1.json
.venv/bin/stock-workspace price register --workspace run/ws-cost --source fixtures/price-s1-correction.json --output run/price-s1-correction.json
.venv/bin/stock-workspace close run --workspace run/ws-cost --source fixtures/close-2.json --output run/close-2.json
.venv/bin/stock-workspace close status --workspace run/ws-cost --close CLOSE-2026-01
.venv/bin/stock-workspace price status --workspace run/ws-cost
```

预期：`cost` 显示两根各自守恒（S1 120000分＝成品116000＋损耗4000，S2 90000分＝成品80000＋损耗10000，全工作区210000分）；首轮关账 CLOSE-2026-01 历史价差全为0；S1 更正为118800分后，CLOSE-2026-02 的 S1 历史价差合计 −1200、生产转移合计 0（如 chair 每件 40000→39600 归历史价差）；`close status --close CLOSE-2026-01` 仍返回首轮冻结结果。

- `price register` 输入非空 `price_id`、初始原材 `stock_id`、非负严格整数 `amount_cents`（整根全部采购成本，含以后修掉的头尾）与非负严格整数 `expected_price_revision`。每次新登记把该根总成本替换为新金额，成本修订版（从0起）推进一次；期待版本必须等于当前成本版本。相同 `price_id` 相同完整输入重试返回原结果（优先于版本检查）；同 id 不同输入、未知根、非法金额退出2。登记不推进库存修订版，也不使已有计划过期。零价合法；有根缺价时 `cost` 与 `close run` 列出缺价根并退出2，不以0估价。
- 成本沿来源链逐层传递：每张已提交切割票把所消耗条目的整数成本按物理长度分给头修边、每件成品、每道锯缝、余料与尾修边（忽略零长度段），先取整数下限再按精确余数从大到小各补1分（余数相同则物理起点小者优先，全程整数运算）。可回收余料携带分得成本进入下一代重新分摊；不可回收余料、修边与锯缝归入损耗。任意截点每根满足：成品＋损耗＋未消耗库存＝该根当时价格。已消耗条目不再计价；被隔离未消耗条目仍持有库存成本。
- `cost` 按当前库存修订版与各根最新价格输出明细：成品以 `(job_id, entry_id, order_id, unit)` 标识，损耗以工单、来源条目、类别与半开坐标标识，存量以 `entry_id` 标识；含每票输入成本、逐段分摊与按根/材种/整体的守恒汇总。改价会传递到已消耗余料的后代成品与损耗。
- `close run` 输入非空 `close_id`、`previous_close_id`（首轮为 null，此后必须是最近一次成功关账）与两个期待修订版。成功时冻结截点、各根价格、完整明细与桥接表；之后的切割、质量事件、改价都不改变已冻结结果。桥接分两步可分别核对：上一轮截点用本轮价格重算减去上轮冻结明细得历史价差，本轮截点减去该重算得本轮生产转移；每行及汇总满足 期初＋价差＋转移＝期末，每根生产转移合计为0。相同 `close_id` 相同输入重试返回原冻结结果；同 id 不同输入、上一轮身份或期待版本过期退出2，不能覆盖或分叉关账链。关账在提交锁内取一致截点，状态原子持久化，中断后按原输入重试补出同一结果，不重复记账。

### 成品配套交付与质量召回

`delivery` 在已提交切割票的真实成品之上做整套配货、出库与召回定位。预览不产生任何库存或出库副作用；提交在同一原子更新中冻结交付清单、登记 `dispatch_id` 并占用所选成品。

```sh
.venv/bin/stock-workspace delivery plan --workspace run/ws --source fixtures/delivery-1.json --output run/dplan.json
.venv/bin/stock-workspace delivery commit --workspace run/ws --plan run/dplan.json --output run/dresult.json
.venv/bin/stock-workspace delivery status --workspace run/ws
.venv/bin/stock-workspace incident register --workspace run/ws --source fixtures/incident.json --output run/incident.json
.venv/bin/stock-workspace delivery status --workspace run/ws --dispatch DSP-2026-01
.venv/bin/stock-workspace delivery affected --workspace run/ws --incident QC-2026-111
```

预期：KIT-A 的两个同纹槽位配上同根 S1 的 chair#3/#4，KIT-B 配上余料切出的 slat#1（总余量30mm最小），成品修订版升到3；对 S1 登记质量事件后，`delivery status --dispatch DSP-2026-01` 把 KIT-A、KIT-B 都标记为召回并只列出真正来自 S1 的实物，`delivery affected --incident QC-2026-111` 从事件一侧定位到 DSP-2026-01。

- 预览输入：非空 `dispatch_id`、1至32个互不重复的候选实物引用 `(job_id, entry_id, order_id, unit)`（`unit` 为正严格整数，其余为非空字符串）、1至8个套件。套件含唯一 `kit_id`、非负严格整数 `priority`（越小越优先）与1至4个槽位（全请求不超过24个）；槽位含套内唯一 `slot_id`、`material`、正严格整数 `min_mm`/`max_mm`（前者不大于后者）与 `grain_group`。字段错误、布尔冒充整数、规模越界均退出2；输入数组重排不改变语义与最终方案。
- 只认已提交切割票中的真实成品（材种、长度 `end_mm-start_mm`、根原材均取自原始事实）；未提交切件、缺件、余料与损耗不能充数，同名件号在不同工单或条目上是不同实物。未知或重复引用退出2；已出库或根被活动事件覆盖的已知件列入排除清单（分别给出占用交付身份与活动事件），不参与配货。
- 每槽用一件同材且长度落在闭区间的成品，不截短、不拼接、不重复；同套同 `grain_group` 的槽位必须同根（多代余料按根判断），组名跨套互不关联；套件只整套选中或不配。配货求全局唯一最优：先比完整套件数，再比按 `(priority, kit_id)` 排列的交付0/1向量，再比总余量 `成品长度-min_mm`，最后比逐槽实物引用序列（`unit` 按整数比较）；不是逐套贪心。允许配成0套。
- 预览输出含工作区身份、完整规范化请求、依据的成品修订版、选中套逐槽实物/根/长度、未选套、排除件及原因、剩余候选，以及可独立核对四层比较的指标。提交时重核工作区、成品修订版与完整方案：手改配货、约束遗漏、非规范最优、版本过期均退出2；成功即冻结清单、登记 `dispatch_id`、占用成品并推进成品修订版（0套同样登记并推进一次）。同 `dispatch_id` 同请求同计划重试优先返回原冻结结果；换请求、换方案或换来源版本冲突。输出原子新建，冲突先判不写状态；中断后按原计划恢复，不重复出库。
- 成品修订版独立于库存/成本修订版：新切割提交、质量事件首次登记或解除、新交付提交各推进一次（幂等重放不推进）；事件即使不改变可用库存也推进；价格登记与关账不推进。交付不推进库存或成本修订版、不改动切割票与成本明细、不使切割计划过期。
- `delivery status` 列出全部成品的归属与 `available`/`isolated`/`delivered` 状态及活动事件——已交付与质量覆盖是两个维度，召回不把实物放回待配池。`delivery status --dispatch` 返回原冻结清单并另列当前受影响套件：任一槽位根被活动事件覆盖则整套召回，只列真正来自被覆盖根的实物。`delivery affected --incident` 按根与真实交付事实定位全部历史交付（活动或已解除事件均可），并显示事件是否仍活动；未知事件或交付退出2。

## 契约与边界

详见[数据契约](docs/contract.md)。采用确定性best-fit decreasing可行启发式，不保证最优。缺件是有效业务结果，见summary.complete及unfilled。

样例均为本仓原创合成数据，来源见[说明](fixtures/SOURCES.md)，代码/样例MIT许可。业务演示和测试不访问网络，不需要容器或外部服务。
