import sqlite3
from pathlib import Path
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS asset(id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS sensor(id TEXT PRIMARY KEY,asset_id TEXT NOT NULL REFERENCES asset(id),role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source(id INTEGER PRIMARY KEY,hash TEXT NOT NULL UNIQUE,body BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS point(id INTEGER PRIMARY KEY,source_id INT REFERENCES source(id),line_no INT,sensor TEXT REFERENCES sensor(id),device TEXT,seq INT,at TEXT,kind TEXT,value REAL,raw TEXT,hash TEXT,UNIQUE(device,seq));
CREATE TABLE IF NOT EXISTS health(id INTEGER PRIMARY KEY,sensor TEXT REFERENCES sensor(id),start TEXT,end TEXT,status TEXT,origin TEXT DEFAULT 'contract');
CREATE TABLE IF NOT EXISTS maintenance(id INTEGER PRIMARY KEY,asset TEXT REFERENCES asset(id),start TEXT,end TEXT);
CREATE TABLE IF NOT EXISTS run(id INTEGER PRIMARY KEY,version TEXT NOT NULL,status TEXT NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS episode(id INTEGER PRIMARY KEY,run_id INT REFERENCES run(id),asset TEXT REFERENCES asset(id),start TEXT,end TEXT,status TEXT,replaces TEXT);
CREATE TABLE IF NOT EXISTS evidence(episode_id INT REFERENCES episode(id),point_id INT REFERENCES point(id),PRIMARY KEY(episode_id,point_id));
CREATE TABLE IF NOT EXISTS issue(id INTEGER PRIMARY KEY,code TEXT NOT NULL,subject TEXT,detail TEXT);
CREATE TABLE IF NOT EXISTS fleet_decision(run_id INT PRIMARY KEY REFERENCES run(id),status TEXT NOT NULL,assets TEXT NOT NULL,episode_ids TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS work_order(episode_id INT PRIMARY KEY REFERENCES episode(id),run_id INT NOT NULL REFERENCES run(id),status TEXT NOT NULL,priority TEXT NOT NULL,reason TEXT NOT NULL,evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS topology_edge(source_asset TEXT NOT NULL REFERENCES asset(id),target_asset TEXT NOT NULL REFERENCES asset(id),min_lag_minutes INT NOT NULL,max_lag_minutes INT NOT NULL,PRIMARY KEY(source_asset,target_asset));
CREATE TABLE IF NOT EXISTS propagation_link(run_id INT NOT NULL REFERENCES run(id),source_episode INT NOT NULL REFERENCES episode(id),target_episode INT NOT NULL REFERENCES episode(id),lag_minutes REAL NOT NULL,PRIMARY KEY(run_id,source_episode,target_episode));
CREATE TABLE IF NOT EXISTS propagation_decision(run_id INT NOT NULL REFERENCES run(id),root_episode INT NOT NULL REFERENCES episode(id),status TEXT NOT NULL,episode_ids TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(run_id,root_episode));
CREATE TABLE IF NOT EXISTS repair_roster(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS repair_plan(id TEXT PRIMARY KEY,run_id INT NOT NULL REFERENCES run(id),strategy TEXT NOT NULL,digest TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS repair_step(plan_id TEXT NOT NULL REFERENCES repair_plan(id),seq INT NOT NULL,kind TEXT NOT NULL,episode_id INT,asset TEXT,crew TEXT,skill TEXT,minutes INT,spare TEXT,spare_count INT,window_start TEXT,window_end TEXT,observes TEXT,after_seq INT,PRIMARY KEY(plan_id,seq));
CREATE TABLE IF NOT EXISTS repair_event(id INTEGER PRIMARY KEY,plan_id TEXT NOT NULL REFERENCES repair_plan(id),seq INT NOT NULL,kind TEXT NOT NULL,at TEXT NOT NULL,detail TEXT NOT NULL,prev_hash TEXT NOT NULL,hash TEXT NOT NULL,UNIQUE(plan_id,seq,kind));
CREATE TABLE IF NOT EXISTS spare_ledger(id INTEGER PRIMARY KEY,plan_id TEXT NOT NULL REFERENCES repair_plan(id),spare TEXT NOT NULL,delta INT NOT NULL,event_id INT NOT NULL REFERENCES repair_event(id),UNIQUE(event_id));
CREATE TABLE IF NOT EXISTS repair_outcome(plan_id TEXT NOT NULL REFERENCES repair_plan(id),episode_id INT NOT NULL REFERENCES episode(id),status TEXT NOT NULL,basis TEXT NOT NULL,PRIMARY KEY(plan_id,episode_id));
CREATE TABLE IF NOT EXISTS repair_certificate(plan_id TEXT NOT NULL REFERENCES repair_plan(id),episode_id INT NOT NULL REFERENCES episode(id),status TEXT NOT NULL,issued_at TEXT NOT NULL,basis TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(plan_id,episode_id));
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);db=sqlite3.connect(p);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
