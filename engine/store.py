# -*- coding: utf-8 -*-
"""持久化存储层：sqlite3 + WAL，三张表管会话/转写/问答。

所有写入都走"只追加"语义——转写和问答都只往表里追加行，
不 UPDATE 不 DELETE，保证历史可回溯。

线程安全：Api.ask_async / ask_stream 都在后台线程里复用主线程创建的
这个连接写流式答案。sqlite3 的单个 Connection 不允许并发使用
（会抛 InterfaceError / ProgrammingError），所以这里统一串行化，
并把 check_same_thread 关掉。所有公开函数的签名保持不变。
"""

import functools
import json
import os
import sqlite3
import threading
import time

# 表结构用模块级常量集中管理，方便测试里复用建表
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL    NOT NULL,    -- 开始时间戳（time.time()）
    title      TEXT    DEFAULT ''
);
CREATE TABLE IF NOT EXISTS segments(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    ts         REAL    NOT NULL,    -- 每句话的写入时间
    speaker    TEXT    DEFAULT '',  -- 发言人，预留，当前为空
    text       TEXT    NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS qa(
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     INTEGER NOT NULL,
    ts             REAL    NOT NULL,    -- 提问时间
    question       TEXT    NOT NULL,
    answer         TEXT    DEFAULT '',  -- 流式追加，逐步拼长
    mode           TEXT    DEFAULT '',  -- normal/brief/algo/system
    status         TEXT    DEFAULT 'pending',  -- pending/streaming/done/error/skip
    first_token_ms INTEGER,                -- 首字延迟（毫秒），可为 NULL
    total_ms       INTEGER,                -- 总耗时（毫秒），可为 NULL
    sources_json   TEXT    DEFAULT '[]',   -- 命中知识库条目，JSON 数组
    raw_text       TEXT    DEFAULT '',     -- P7：触发这条问答的「对方」原话
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);
"""

_LOCK = threading.RLock()


def _locked(fn):
    """把单次「执行 + 提交」包成原子操作，避免两个线程把事务交错在一起。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _LOCK:
            return fn(*args, **kwargs)
    return wrapper


# ---------- 连接与建库 ----------

def _db_path():
    """返回 interview.db 的完整路径，与 面试猴.py 的 data_dir() 保持一致。"""
    d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "InterviewMonkey")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "interview.db")


@_locked
def connect():
    """打开/创建数据库连接，开启 WAL，执行建表。返回 sqlite3.Connection。"""
    conn = sqlite3.connect(_db_path(), check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")          # 写入不阻塞读取
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """老库升级：CREATE TABLE IF NOT EXISTS 不会给已存在的表加新列，
    这里按列名检查补 ALTER TABLE（幂等）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(qa)").fetchall()}
    if cols and "raw_text" not in cols:
        conn.execute("ALTER TABLE qa ADD COLUMN raw_text TEXT DEFAULT ''")
        conn.commit()


# ---------- 会话 ----------

@_locked
def new_session(conn, title=""):
    """新建一次会话，返回新分配的 session id（整数）。"""
    cur = conn.execute(
        "INSERT INTO sessions(started_at, title) VALUES (?, ?)",
        (time.time(), title),
    )
    conn.commit()
    return cur.lastrowid


@_locked
def latest_session(conn):
    """返回最近一次会话的 Row（含 id/started_at/title），没有则返回 None。"""
    return conn.execute(
        "SELECT * FROM sessions ORDER BY id DESC LIMIT 1"
    ).fetchone()


# ---------- 转写片段（只追加） ----------

@_locked
def add_segment(conn, session_id, text, speaker="", ts=None):
    """追加一条转写片段。ts 不传则用当前时间。返回 segment id。"""
    cur = conn.execute(
        "INSERT INTO segments(session_id, ts, speaker, text) VALUES (?, ?, ?, ?)",
        (session_id, ts if ts is not None else time.time(), speaker, text),
    )
    conn.commit()
    return cur.lastrowid


@_locked
def list_segments(conn, session_id):
    """按写入顺序返回某次会话的全部转写片段（列表，每项是 dict）。"""
    rows = conn.execute(
        "SELECT * FROM segments WHERE session_id=? ORDER BY id ASC",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- 问答（流式追加答案） ----------

@_locked
def add_qa(conn, session_id, question, mode="", ts=None, raw_text=""):
    """新建一条问答记录，状态 pending。raw_text 存触发它的「对方」原话。返回 qa id。"""
    cur = conn.execute(
        "INSERT INTO qa(session_id, ts, question, mode, status, raw_text) "
        "VALUES (?, ?, ?, ?, 'pending', ?)",
        (session_id, ts if ts is not None else time.time(), question, mode,
         raw_text or ""),
    )
    conn.commit()
    return cur.lastrowid


@_locked
def append_qa_answer(conn, qa_id, piece):
    """把流式片段追加到 answer 末尾（不覆盖）。只追加不替换。"""
    conn.execute(
        "UPDATE qa SET answer = answer || ?, status = 'streaming' WHERE id = ?",
        (piece, qa_id),
    )
    conn.commit()


@_locked
def finish_qa(conn, qa_id, status="done", first_token_ms=None, total_ms=None, sources_json=None):
    """标记问答完成：写 status/耗时/来源。answer 已由 append 累积好。"""
    conn.execute(
        """UPDATE qa SET
             status = ?,
             first_token_ms = ?,
             total_ms = ?,
             sources_json = ?
           WHERE id = ?""",
        (
            status,
            first_token_ms,
            total_ms,
            sources_json if sources_json is not None else "[]",
            qa_id,
        ),
    )
    conn.commit()


@_locked
def list_qa(conn, session_id):
    """按提问顺序返回某次会话的全部问答（列表，每项是 dict）。"""
    rows = conn.execute(
        "SELECT * FROM qa WHERE session_id=? ORDER BY id ASC",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]
