import sqlite3
import json
import os
import glob
from collections import defaultdict

def export_known_users(output_path):
    users_by_platform = defaultdict(lambda: defaultdict(dict))

    # Target only project_operasi_pesta_copet
    db_paths = glob.glob(os.path.join("comments_data", "project_operasi_pesta_copet*", "state", "scrape_state.sqlite"))
    print(f"Found {len(db_paths)} project databases matching operasi_pesta_copet.")

    for db_path in db_paths:
        print(f"Processing: {db_path}")
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            # Check if tables exist first to avoid errors on empty DBs
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='content_items'")
            if cursor.fetchone():
                cursor.execute('''
                    SELECT platform, creator
                    FROM content_items
                    WHERE creator != '' AND creator IS NOT NULL
                ''')
                for platform, creator in cursor.fetchall():
                    platform = platform.lower()
                    creator = creator.strip('@')
                    if creator not in users_by_platform[platform]:
                        users_by_platform[platform][creator] = {
                            "role": "creator",
                            "known_content_count": 0,
                            "comments_made": 0
                        }
                    users_by_platform[platform][creator]["known_content_count"] += 1

            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='comments'")
            if cursor.fetchone():
                cursor.execute('''
                    SELECT platform, author, author_id
                    FROM comments
                    WHERE author != '' AND author IS NOT NULL
                ''')
                for platform, author, author_id in cursor.fetchall():
                    platform = platform.lower()
                    author = author.strip('@')

                    if author not in users_by_platform[platform]:
                        users_by_platform[platform][author] = {
                            "role": "commenter",
                            "author_id": author_id,
                            "comments_made": 0,
                            "known_content_count": 0
                        }

                    if "comments_made" not in users_by_platform[platform][author]:
                        users_by_platform[platform][author]["comments_made"] = 0

                    users_by_platform[platform][author]["comments_made"] += 1

                    if "author_id" not in users_by_platform[platform][author] and author_id:
                        users_by_platform[platform][author]["author_id"] = author_id

            conn.close()
        except Exception as e:
            print(f"Error processing {db_path}: {e}")

    print(f"Total platforms found: {list(users_by_platform.keys())}")
    for plat, users in users_by_platform.items():
        print(f"  - {plat}: {len(users)} unique users")

    print(f"Writing data to {output_path}...")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(users_by_platform, f, indent=2, ensure_ascii=False)

    print("Export complete.")

if __name__ == "__main__":
    out_file = "operasi_pesta_copet_users.json"
    export_known_users(out_file)
