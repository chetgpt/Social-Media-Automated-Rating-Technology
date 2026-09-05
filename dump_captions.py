import sqlite3
import json
import re

db_path = r"comments_data\project_music_audit_new_topic_rekomendasi_ba_20127c9e_20260830_232518_334925\state\topic_1000p_rekomendasi_260830_acab7235_state.sqlite"
conn = sqlite3.connect(db_path)
c = conn.cursor()

c.execute("SELECT evidence_json FROM engage_tiktok_posts WHERE evidence_json IS NOT NULL;")
rows = c.fetchall()

captions = []
for row in rows:
    try:
        data = json.loads(row[0])
        caption = data.get('caption', '').strip()
        if caption:
            cleaned = re.sub(r'#\w+', '', caption).strip()
            if cleaned:
                captions.append(cleaned)
    except:
        pass

unique_caps = list(set(captions))
with open("captions_dump.txt", "w", encoding="utf-8") as f:
    for cap in unique_caps:
        f.write(cap + "\n---\n")
