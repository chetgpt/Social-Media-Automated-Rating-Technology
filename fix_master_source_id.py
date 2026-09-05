import sqlite3
import hashlib
import os

master_db = r'd:\Kita Co. Lab\Music Audit Tool\Music Audit\TEST Tiktok Scraper Modules\comments_data\tiktok_master\state\tiktok_master.sqlite'

def compute_source_id(path: str) -> str:
    # Most likely it's md5 of lowercase path
    norm = os.path.normcase(os.path.abspath(path))
    return hashlib.md5(norm.encode('utf-8')).hexdigest()

conn = sqlite3.connect(master_db)
cur = conn.cursor()
cur.execute("SELECT database_path FROM tiktok_master_sources")
rows = cur.fetchall()

for row in rows:
    path = row[0]
    correct_id = compute_source_id(path)
    cur.execute("UPDATE tiktok_master_sources SET source_id = ? WHERE database_path = ?", (correct_id, path))

conn.commit()
conn.close()
print("Updated source IDs to match new paths.")
