import sqlite3

master_db = r'd:\Kita Co. Lab\Music Audit Tool\Music Audit\TEST Tiktok Scraper Modules\comments_data\tiktok_master\state\tiktok_master.sqlite'
old_prefix = r'D:\Kita Co. Lab\MAIN Backup for Antigravity\TEST Tiktok Scraper Modules'
new_prefix = r'd:\Kita Co. Lab\Music Audit Tool\Music Audit\TEST Tiktok Scraper Modules'

conn = sqlite3.connect(master_db)
cur = conn.cursor()
cur.execute("SELECT source_id, database_path FROM tiktok_master_sources")
rows = cur.fetchall()

for row in rows:
    row_id, path = row
    if path.startswith(old_prefix):
        new_path = path.replace(old_prefix, new_prefix)
        cur.execute("UPDATE tiktok_master_sources SET database_path = ? WHERE source_id = ?", (new_path, row_id))

conn.commit()
conn.close()
print("Updated master DB source paths.")
