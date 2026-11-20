# 突发拼窗台

离线异常点、校时锚点、设备健康和维护窗口形成版本化故障事件。只处理已识别异常点，不进行异常检测或在线告警。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m burstweave demo`，`python -m burstweave --help`，`python -m pytest -q`。运行与测试使用本地合成数据，不访问网络。

## 可复核的事件版本

Run 版本同时冻结异常点、拼窗间隔、传感器角色、健康契约和维护窗口；其中任一项改变都需要生成新事件版本，旧结论保持可查。同一契约再次加载不会重复健康区间。已停机后重新启动且在故障事件之前恢复的设备，不会因历史停机被误判为仍停机。`reconcile --gap-minutes N` 可显式指定拼窗间隔。

## 跨设备事件与工单

`fleet` 汇总不同资产上可信且重叠的故障事件，保存共因调查或单设备处置结论。`triage` 结合事件证据与跨设备结论生成工单优先级和派发状态；维护影响或证据缺失保持待核。两项结果都入 SQLite，演示命令执行完整链路。

`topology-edge` 冻结资产依赖及传播时延，拒绝环路；`propagation` 追踪可信事件沿依赖边的先后扩散，持久化事件链接与传播决定。`triage` 同时考虑重叠共因和拓扑传播，因此非重叠但有证据的顺序故障也能升级工单。

## 客户服务可用性证书

`service-register` 冻结闭合 run 的服务依赖与区间删失影响观测：每项资产的起始/恢复只是带时区的半开时间界，端点不是精确故障时刻，跨资产起始时差是必须同时满足的联合证据。`service-assess --topology series|redundant_2 --sla-minutes N` 在分钟网格上枚举所有联合可行时间线，按拓扑合同计算真实停服长度的全局最小/最大值：仅下界严格超过 SLA 才 `definitely_breached`，上界不超过 SLA 才 `definitely_clear`，跨限额、约束矛盾、必需资产缺证据或来源失效均为 `indeterminate`。证书保存达到两个极值的可行见证、逐资产影响段、停服段、分钟数和来源摘要，写入 SQLite `service_certificate` 表。

`service-verify` 从闭合 run、原始影响文件和时差合同独立复算见证可行性与极值；任一来源或台账被改写即删除旧证书并报错。服务证书不回写故障 episode 或维修工单。`fixtures/service-impact*.json` 为离线合成影响证据，其中双资产例在同一证据下给出 series 停服 20–25 分钟（15 分钟 SLA 违约）而 redundant_2 停服 5–10 分钟（15 分钟 SLA 免责、7 分钟 SLA 不可判）的不同结论。
