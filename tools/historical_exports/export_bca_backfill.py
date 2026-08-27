import sqlite3
import json

con = sqlite3.connect('comments_data/tiktok_master/state/tiktok_master.sqlite')
con.row_factory = sqlite3.Row
cur = con.cursor()

cur.execute("""
    SELECT o.post_id, o.music_evidence_json
    FROM tiktok_master_music_backfill_observations o
    JOIN tiktok_master_music_backfill_runs r ON o.run_id = r.run_id
    WHERE r.project LIKE 'bca_music_backfill_explicit_%'
""")
rows = cur.fetchall()

count = 0
with open('bca_backfill_full_export.jsonl', 'w', encoding='utf-8') as f:
    for row in rows:
        f.write(json.dumps({'post_id': row['post_id'], 'music_evidence': json.loads(row['music_evidence_json'])}) + '\n')
        count += 1

print(f"Exported {count} posts with music evidence.")
