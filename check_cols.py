import sqlite3
db_path = r"comments_data\project_music_audit_new_topic_rekomendasi_ba_20127c9e_20260830_232518_334925\state\topic_1000p_rekomendasi_260830_acab7235_state.sqlite"
conn = sqlite3.connect(db_path)
c = conn.cursor()
c.execute("PRAGMA table_info(engage_tiktok_posts);")
cols = [row[1] for row in c.fetchall()]
print("Columns in engage_tiktok_posts:", cols)

c.execute("SELECT raw_json, evidence_json FROM engage_tiktok_posts LIMIT 1;")
row = c.fetchone()
print("Has raw_json:", 'raw_json' in cols)
print("Has evidence_json:", 'evidence_json' in cols)
