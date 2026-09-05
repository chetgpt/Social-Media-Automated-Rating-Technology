import sqlite3
import os

projects = [
    'project_music_audit_janjijiwa',
    'project_music_audit_pointcoffee2',
    'project_music_audit_tomoro',
    'project_wardahofficial_audit_v2'
]

new_master_db = r'd:\Kita Co. Lab\Music Audit Tool\Music Audit\TEST Tiktok Scraper Modules\comments_data\tiktok_master\state\tiktok_master.sqlite'

for project in projects:
    db_path = f'comments_data/{project}/state/engage_state.sqlite'
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("UPDATE engage_tiktok_runs SET master_database = ?", (new_master_db,))
        conn.commit()
        conn.close()
        print(f"Updated {project}")
    else:
        print(f"{project} not found")
