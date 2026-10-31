"""Assert the AI API key never persists into the database (audit helper)."""
import os
import sqlite3

DB = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "terminal.db"))
MARKERS = ("8f59a428", "sk-8f59", "zyxapi")

con = sqlite3.connect(DB)
cur = con.cursor()
tables = [r[0] for r in cur.execute("select name from sqlite_master where type='table'")]
hits: dict[str, list[str]] = {}

for table in tables:
    try:
        rows = cur.execute(f'select * from "{table}"').fetchall()
    except sqlite3.Error:
        continue
    columns = [d[0] for d in cur.description] if cur.description else []
    for row in rows:
        for col, val in zip(columns, row):
            if isinstance(val, str) and any(m in val for m in MARKERS):
                hits.setdefault(table, []).append(col)

con.close()

if hits:
    print("DB LEAK:", {k: sorted(set(v)) for k, v in hits.items()})
else:
    print("DB LEAK: none")
print("tables scanned:", len(tables))
