# 突发拼窗台

离线异常点、校时锚点、设备健康和维护窗口形成版本化故障事件。只处理已识别异常点，不进行异常检测或在线告警。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m burstweave demo`，`python -m burstweave --help`，`python -m pytest -q`。运行与测试使用本地合成数据，不访问网络。

## 可复核的事件版本

Run 版本同时冻结异常点、拼窗间隔、传感器角色、健康契约和维护窗口；其中任一项改变都需要生成新事件版本，旧结论保持可查。同一契约再次加载不会重复健康区间。已停机后重新启动且在故障事件之前恢复的设备，不会因历史停机被误判为仍停机。`reconcile --gap-minutes N` 可显式指定拼窗间隔。

## 跨设备事件与工单

`fleet` 汇总不同资产上可信且重叠的故障事件，保存共因调查或单设备处置结论。`triage` 结合事件证据与跨设备结论生成工单优先级和派发状态；维护影响或证据缺失保持待核。两项结果都入 SQLite，演示命令执行完整链路。

`topology-edge` 冻结资产依赖及传播时延，拒绝环路；`propagation` 追踪可信事件沿依赖边的先后扩散，持久化事件链接与传播决定。`triage` 同时考虑重叠共因和拓扑传播，因此非重叠但有证据的顺序故障也能升级工单。

## 多参考时钟校准资格

`clock-design` 锁定设计文件中的独立参考源、漂移/桥接合同、`fixtures/clockproof/points.jsonl` 的字节哈希以及 `ingest` 已保存的 13 个 anchor ID；源字节或点原始行变化都会被拒绝。`clock-certify` 只用这些冻结的原始参考行重算，不预填期望答案，也不把整台设备统一标好/坏，结果写入 `clock_anchor_verdict`、`clock_timepoint`、`clock_bridge`、`clock_segment`、`clock_certificate` 五张 SQLite 表。`clock-report` 另出 JSON/Markdown 结构化证书，`clock-query` 对任意原始时刻给有界真实 UTC。`python -m burstweave.clockproof` 输出可运行证据。

同一原始时点的每个 `reference_id` 最多投一张独立支持票，同参考重复行不虚增票数、互斥重复使该参考失效；闭合偏移区间存在非空公共交集才算相容。唯一达到 `min_independent_references` 的最大支持集核证该时点，其余锚隔离；同规模互斥的最大支持集输出争议，不选边、不平均。只有明确 `master_attested`、来自 `master_reference` 且无其他参考反证的单锚，才能在 master-fallback 合同下成为临时核证时点；quorum-only 合同保持未核证。

相邻核证时点间隔不超过 `max_bridge_minutes`、且存在遵守 `max_drift_seconds_per_minute` 的连续偏移轨迹才连成校准段；等号边界可连接，交集为空即判时钟跳变，不把前后偏移平均成中间值。段内查询取覆盖它的每座桥“偏移闭区间 ± 漂移额度”的交集，再把设备原始时刻反向平移成真实 UTC 上下界。每个结论都带锚点 ID、参数、原始行和来源哈希，可逐项复算；修改合成来源重新摄入并锁定后，支持集、桥接和证书全部由新原始行重新裁定。
