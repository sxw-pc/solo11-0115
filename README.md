# 突发拼窗台

离线异常点、校时锚点、设备健康和维护窗口形成版本化故障事件。只处理已识别异常点，不进行异常检测或在线告警。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m burstweave demo`，`python -m burstweave --help`，`python -m pytest -q`。运行与测试使用本地合成数据，不访问网络。

## 可复核的事件版本

Run 版本同时冻结异常点、拼窗间隔、传感器角色、健康契约和维护窗口；其中任一项改变都需要生成新事件版本，旧结论保持可查。同一契约再次加载不会重复健康区间。已停机后重新启动且在故障事件之前恢复的设备，不会因历史停机被误判为仍停机。`reconcile --gap-minutes N` 可显式指定拼窗间隔。
