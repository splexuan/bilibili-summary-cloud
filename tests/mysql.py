"""
MySQL 兼容性校验 —— 不连真库，把模型与查询编译成 MySQL 方言检查。

覆盖三件事：
1. 所有建表 DDL 能编译成合法 MySQL（索引长度、TEXT 不能当键等）
2. 所有查询语句能编译成 MySQL 方言（ilike / limit-offset 等差异）
3. 确认没有 Postgres 专有语法残留

真正连 MySQL 的验证必须上服务器做，这里拦掉的是「编译期」就能发现的错。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 用 MySQL URL 导入，让 settings.db_backend 走 mysql 分支
os.environ["DATABASE_URL"] = "mysql+aiomysql://bsum:pass@127.0.0.1:3306/bsum?charset=utf8mb4"
os.environ["DATABASE_URL_SYNC"] = "mysql+pymysql://bsum:pass@127.0.0.1:3306/bsum?charset=utf8mb4"
os.environ["SECRET_KEY"] = "uL9k2YqWm3nR8tVx5zA7bC1dE4fG6hJ0kL2mN4oP6qR="
os.environ["JWT_SECRET"] = "mysql_test_secret_long_enough_hs256"

from sqlalchemy import func, select
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.core.config import settings

fails, warns = [], []


def chk(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def warn(msg):
    print("  WARN " + msg)
    warns.append(msg)


print("=== 0. 后端识别 ===")
chk(settings.db_backend == "mysql", f"db_backend = {settings.db_backend}")
chk(settings.is_mysql, "is_mysql = True")

import app.db.models as M  # noqa: E402

print("\n=== 1. 建表 DDL 编译为 MySQL ===")
dialect = mysql.dialect()
for table in M.Base.metadata.sorted_tables:
    try:
        ddl = str(CreateTable(table).compile(dialect=dialect))
        chk(True, f"{table.name} 建表语句编译通过")
    except Exception as exc:
        chk(False, f"{table.name} 建表语句编译失败: {exc}")

print("\n=== 2. 索引编译（MySQL 对键长有 3072 字节限制）===")
# utf8mb4 = 4 字节/字符，DYNAMIC 行格式上限 3072 字节
for table in M.Base.metadata.sorted_tables:
    for idx in table.indexes:
        try:
            ddl = str(CreateIndex(idx).compile(dialect=dialect))
            # 计算键长
            total = 0
            detail = []
            for col in idx.columns:
                if hasattr(col.type, "length") and col.type.length:
                    b = col.type.length * 4
                else:
                    b = 4  # INT 等定长
                total += b
                detail.append(f"{col.name}({b}B)")
            if total > 3072:
                chk(False, f"{table.name}.{idx.name} 键长 {total}B 超过 3072B：{detail}")
            else:
                chk(True, f"{table.name}.{idx.name} 键长 {total}B")
        except Exception as exc:
            chk(False, f"{table.name}.{idx.name} 编译失败: {exc}")

print("\n=== 3. 唯一约束检查（TEXT/BLOB 列不能做键）===")
# MySQL 的限制是 TEXT/BLOB 不能进索引。INTEGER / DATETIME 这类定长类型没问题，
# 它们没有 length 属性但完全合法，所以要按类型判断而不是「有没有 length」。
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
)

_FIXED_TYPES = (Integer, BigInteger, SmallInteger, DateTime, Date, Boolean, Numeric, Float)


def _key_safe(col) -> tuple[bool, str]:
    t = col.type
    name = type(t).__name__
    if isinstance(t, _FIXED_TYPES):
        return True, f"{col.name}:{name}(定长)"
    if isinstance(t, (String,)):
        if t.length:
            return True, f"{col.name}:{name}({t.length})"
        return False, f"{col.name}:{name} 无长度"
    if isinstance(t, Text):
        return False, f"{col.name}:Text(TEXT 不能做键)"
    # 其他类型交给 DDL 编译去判定
    return True, f"{col.name}:{name}(待编译验证)"


for table in M.Base.metadata.sorted_tables:
    for cons in table.constraints:
        name = getattr(cons, "name", None)
        if name and "uq" in str(name).lower():
            cols = list(getattr(cons, "columns", []))
            bad, detail = [], []
            for c in cols:
                ok, d = _key_safe(c)
                detail.append(d)
                if not ok:
                    bad.append(d)
            if bad:
                chk(False, f"{table.name}.{name} 含不能做键的列：{bad}")
            else:
                chk(True, f"{table.name}.{name} 键列均合法（{', '.join(detail)}）")

print("\n=== 4. 查询语句编译为 MySQL 方言 ===")
from app.db.models import Article, Chat, Job, UsageMonthly, User, Video

queries = {
    "ilike 搜索（Video.title/uploader）":
        select(Video).where(Video.title.ilike("%x%") | Video.uploader.ilike("%x%")),
    "ilike 搜索（Article.title）":
        select(Article).where(Article.title.ilike("%x%")),
    "ilike 搜索（User.username/display_name）":
        select(User).where(User.username.ilike("%x%") | User.display_name.ilike("%x%")),
    "分页 limit+offset":
        select(Video).order_by(Video.processed_at.desc()).limit(21).offset(20),
    "计数聚合":
        select(func.count(Job.id)).where(Job.status.in_(["pending", "running"])),
    "用量按月查询":
        select(UsageMonthly).where(UsageMonthly.user_id == 1,
                                   UsageMonthly.year_month == "2026-09"),
    "Chat 按 kind 过滤":
        select(Chat).where(Chat.user_id == 1, Chat.kind == "video"),
    "Video 唯一键查询":
        select(Video).where(Video.user_id == 1, Video.vid == "BV1xx"),
}

for label, q in queries.items():
    try:
        sql = str(q.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
        chk(True, f"{label} 编译通过")
    except Exception as exc:
        chk(False, f"{label} 编译失败: {exc}")

print("\n=== 5. ilike 在 MySQL 上的实际表现 ===")
q = select(Video).where(Video.title.ilike("%测试%"))
sql = str(q.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
if "lower(" in sql.lower():
    warn("ilike 生成了 lower() 包裹 —— MySQL 下无法用索引，搜索会全表扫描。")
    warn("  数据量小可接受；量大时建议改用全文索引或 LIKE（MySQL 默认不区分大小写）。")
    print(f"       生成 SQL：{sql.splitlines()[0][:100]}…")
else:
    chk(True, "ilike 未产生 lower() 包裹")

print("\n=== 6. 检查 Postgres 专有语法残留 ===")
import re
pg_patterns = [
    (r"\bRETURNING\b", "RETURNING"),
    (r"\bILIKE\b", "原生 ILIKE"),
    (r"::\w+", "类型转换 ::type"),
    (r"\bON CONFLICT\b", "ON CONFLICT"),
    (r"\bjsonb\b", "jsonb 类型"),
    (r"\bARRAY\[", "数组"),
    (r"\bDISTINCT ON\b", "DISTINCT ON"),
]
hit = False
for label, q in queries.items():
    sql = str(q.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
    for pat, nm in pg_patterns:
        if re.search(pat, sql, re.I):
            chk(False, f"{label} 含 Postgres 专有语法 {nm}")
            hit = True
if not hit:
    chk(True, "所有查询无 Postgres 专有语法")

print("\n=== 7. Boolean / BigInteger 映射 ===")
from app.db.models import SystemSetting, UsageMonthly as UM

for tbl, col, label in [
    (SystemSetting, "is_encrypted", "Boolean"),
    (UM, "ai_tokens", "BigInteger"),
]:
    c = tbl.__table__.c[col]
    try:
        ddl = str(CreateTable(tbl.__table__).compile(dialect=dialect))
        chk(True, f"{tbl.__name__}.{col} ({label}) MySQL DDL 正常")
    except Exception as exc:
        chk(False, f"{tbl.__name__}.{col} 失败: {exc}")

print("\n" + "=" * 56)
print(f"失败 {len(fails)}   警告 {len(warns)}")
if fails:
    print("\n失败清单：")
    for f in fails:
        print("  - " + f)
if warns:
    print("\n警告清单：")
    for w in warns:
        print("  - " + w)
print("=" * 56)
sys.exit(1 if fails else 0)
