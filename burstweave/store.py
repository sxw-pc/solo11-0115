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
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);db=sqlite3.connect(p);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
