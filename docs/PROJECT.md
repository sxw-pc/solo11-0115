# 突发拼窗台设计

`asset` 一对多 `sensor`；`source` 保存原始 JSONL 字节和哈希，`point` 保存传感器、设备序号、原始时间及内容摘要。`health` 与 `maintenance` 分别记录设备健康和维护区间。`run` 以输入身份摘要确定版本，`episode` 关联资产和替代版本，`evidence` 连接异常点，`issue` 保存解析、身份和序号问题。

点可正常采录或冲突；候选事件先待闭合，经时间锚点、心跳/停机、维护窗口和传感器角色核对后成为可信、待核或维护影响。迟到点生成新运行，旧事件保留替代关系。跨传感器只在同资产且角色不同、时间相邻时关联。无校时锚点、缺心跳、停机、序号断档或部分维护覆盖不会被误判可信。

`python -m burstweave demo` 生成合成异常日志与 JSON/Markdown 事件账本。测试覆盖幂等和身份冲突、坏行隔离、候选段间隔边界、跨源关联、缺锚点、断号、维护覆盖、迟到桥接、心跳与停机。项目不检测原始信号中的异常，也不发送在线告警。

跨设备裁定用 `fleet_decision` 保存同窗可信事件的共因判断；处置工单用 `work_order` 保存派工状态与优先级。单设备可信事件与跨设备共因事件有不同处置结论，维护影响与缺证据事件不会直接派发。

拓扑传播用 `topology_edge` 冻结资产依赖与时延，`propagation_link` 记录实际可信事件的有序链接，`propagation_decision` 记录根事件及扩散范围。派工同时读取共因与传播结果；无重叠的顺序故障在拓扑证据成立时可升级。

多参考时钟校准用 `clock_design` 冻结设计、源字节哈希和 anchor ID 集合。仲裁按原始瞬间分组（`Z`、`+00:00` 等等价写法视为同一时点），每个参考源至多一份独立支持：闭合偏移区间 `offset_seconds ± uncertainty_seconds` 需有非空公共交集，唯一达到 quorum 的最大支持集核证该时点，不相容锚隔离，同规模互斥最大集判争议，同参考重复行不增票。master-fallback 合同仅接受无反证的 `master_attested` 主参考单锚作为临时核证。`clock_anchor_verdict`、`clock_timepoint`、`clock_bridge`、`clock_segment`、`clock_certificate` 保存逐锚理由、桥接/跳变、校准段和每个事件的有界真实 UTC；所有数值由 `source`/`point` 中的原始行重算，原始行或字节被篡改即拒绝，不使用设备级统一好坏标记。
