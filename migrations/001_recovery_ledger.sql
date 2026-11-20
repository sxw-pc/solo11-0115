-- 001_recovery_ledger.sql — 序号缺口恢复账（GF(257) 校验帧解码台账）。
-- 新建数据库由 burstweave/store.py 的 SCHEMA 自动带上同样两张表；
-- 已有数据库执行本文件完成迁移。所有语句幂等，可重复执行。
--
-- recovery_run：一次解码运行的冻结结论。version 是全部输入（封存 parity_case
-- 负载、协议名、协议版本、当前实收点 seq/hash/value、块内身份冲突）的摘要，
-- 输入变化产生新版本行，绝不改写旧行。
-- recovery_item：该运行下块内每个序号的处置——received（实收，引用点哈希）、
-- recovered（恢复值，只入账不回写 point）、undetermined（未定，附约束与值域）。
CREATE TABLE IF NOT EXISTS recovery_run(
    id INTEGER PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES parity_case(id),
    protocol TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL,
    created TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(case_id,protocol,version)
);
CREATE TABLE IF NOT EXISTS recovery_item(
    run_id INT NOT NULL REFERENCES recovery_run(id),
    seq INT NOT NULL,
    state TEXT NOT NULL,
    value INT,
    detail TEXT,
    PRIMARY KEY(run_id,seq)
);
