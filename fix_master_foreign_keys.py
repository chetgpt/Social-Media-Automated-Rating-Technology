import sqlite3
import hashlib
import os

master_db = r'd:\Kita Co. Lab\Music Audit Tool\Music Audit\TEST Tiktok Scraper Modules\comments_data\tiktok_master\state\tiktok_master.sqlite'
old_prefix = r'D:\Kita Co. Lab\MAIN Backup for Antigravity\TEST Tiktok Scraper Modules'
new_prefix = r'd:\Kita Co. Lab\Music Audit Tool\Music Audit\TEST Tiktok Scraper Modules'

def compute_source_id(path: str) -> str:
    norm = os.path.normcase(os.path.abspath(path))
    return hashlib.md5(norm.encode('utf-8')).hexdigest()

conn = sqlite3.connect(master_db)
cur = conn.cursor()
cur.execute("SELECT database_path FROM tiktok_master_sources")
rows = cur.fetchall()

for row in rows:
    path = row[0]
    if path.startswith(new_prefix):
        # The path was already updated. Let's find what the old path was to get the old source_id.
        old_path = path.replace(new_prefix, old_prefix)
        old_id = compute_source_id(old_path)
        new_id = compute_source_id(path)
        
        # Update snapshots
        cur.execute("UPDATE tiktok_master_snapshots SET source_id = ? WHERE source_id = ?", (new_id, old_id))
        
        # Also tiktok_master_runs has a source_id column maybe?
        # Let's check tables.
        # tiktok_master_runs, tiktok_master_run_posts, etc.
        try:
            cur.execute("UPDATE tiktok_master_runs SET source_id = ? WHERE source_id = ?", (new_id, old_id))
        except sqlite3.OperationalError:
            pass

conn.commit()
conn.close()
print("Updated all related source_ids.")
