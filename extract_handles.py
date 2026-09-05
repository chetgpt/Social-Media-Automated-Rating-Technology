import sqlite3
import re
from collections import Counter

db_path = r"comments_data\project_music_audit_new_topic_rekomendasi_ba_20127c9e_20260830_232518_334925\state\topic_1000p_rekomendasi_260830_acab7235_state.sqlite"
conn = sqlite3.connect(db_path)
c = conn.cursor()

# Get schema to find where captions are
c.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = [row[0] for row in c.fetchall()]

target_table = None
target_col = None

for t in tables:
    c.execute(f"PRAGMA table_info({t});")
    cols = [col[1] for col in c.fetchall()]
    if 'caption' in cols:
        target_table = t
        target_col = 'caption'
    elif 'description' in cols and target_table is None:
        target_table = t
        target_col = 'description'
    elif 'text' in cols and target_table is None:
        target_table = t
        target_col = 'text'

if not target_table:
    # try evidence table
    if 'tiktok_evidence' in tables:
        target_table = 'tiktok_evidence'
        target_col = 'caption_or_description'
    elif 'audit_evidence' in tables:
        target_table = 'audit_evidence'
        target_col = 'caption'

if target_table:
    try:
        c.execute(f"SELECT {target_col} FROM {target_table} WHERE {target_col} IS NOT NULL;")
        rows = c.fetchall()
        captions = [r[0] for r in rows if r[0]]
        
        handles = Counter()
        for cap in captions:
            # simple regex for handles
            found = re.findall(r'@[\w\.]+', cap)
            for h in found:
                handles[h.lower()] += 1
                
        print(f"Found {len(captions)} posts with text.")
        print("Top 30 mentioned handles:")
        for h, count in handles.most_common(30):
            print(f"{h}: {count}")
    except Exception as e:
        print("Error reading captions:", e)
else:
    print("Could not find table with captions. Tables available:", tables)
