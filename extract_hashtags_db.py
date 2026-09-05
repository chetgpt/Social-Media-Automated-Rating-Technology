import sqlite3
import json
import re
from collections import Counter

db_path = r"comments_data\project_music_audit_new_topic_rekomendasi_ba_20127c9e_20260830_232518_334925\state\topic_1000p_rekomendasi_260830_acab7235_state.sqlite"
conn = sqlite3.connect(db_path)
c = conn.cursor()

c.execute("SELECT evidence_json FROM engage_tiktok_posts WHERE evidence_json IS NOT NULL;")
rows = c.fetchall()

hashtags = []

for row in rows:
    try:
        data = json.loads(row[0])
        caption = data.get('caption', '')
        # Find hashtags
        tags = re.findall(r'#\w+', caption)
        hashtags.extend([t.lower() for t in tags])
    except:
        pass

print("Top 50 Hashtags Used by These Emerging Artists & Curators:\n")
for tag, count in Counter(hashtags).most_common(50):
    print(f"{tag}: {count} posts")
