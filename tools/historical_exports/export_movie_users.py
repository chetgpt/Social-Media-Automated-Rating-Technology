import sqlite3
import json
import os
from collections import defaultdict

def is_noise(username):
    ui_noise_exact = {
        "photo", "video", "author", "top fan", "follow", "share",
        "like", "reply", "replies", "view more comments", "view previous comments",
        "hidden", "edited", "remove", "see more", "see translation"
    }
    ui_noise_contains = ["top fan", "replied ·", "· reply", "author", "follow"]
    u_lower = username.lower().strip()
    if u_lower in ui_noise_exact: return True
    if u_lower.isdigit(): return True
    if len(u_lower) <= 1: return True
    if len(u_lower) > 40: return True
    for noise in ui_noise_contains:
        if noise in u_lower: return True
    if username.startswith('· ') or username.startswith(' - ') or '\n' in username or '\r' in username: return True
    if '\u200e' in username or '\u200f' in username or '\u202a' in username or '\u202c' in username or '\u206e' in username: return True
    return False

def export_movie_users(db_path, output_path):
    print(f"Processing: {db_path}")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 1. Determine valid content keys
    cursor.execute("SELECT content_key, platform, keyword, description, caption FROM content_items")
    valid_content_keys = set()
    movie_identifiers = ["operasi", "copet", "imajinari", "pesta pora", "iqbaal", "ijal", "film", "bioskop", "movie", "cinema", "trailer", "teaser"]

    for row in cursor.fetchall():
        content_key, platform, keyword, desc, cap = row
        k_lower = keyword.lower() if keyword else ""
        desc_lower = (desc.lower() if desc else "") + " " + (cap.lower() if cap else "")

        # If the keyword itself is highly specific to the movie
        if "operasi" in k_lower or "copet" in k_lower or "imajinari" in k_lower or "pesta pora" in k_lower:
            valid_content_keys.add(content_key)
        else:
            # Vague keyword like "OPC" or "Iqbal OPC". Check the description/caption.
            is_valid = False
            for ident in movie_identifiers:
                if ident in desc_lower:
                    is_valid = True
                    break
            if is_valid:
                valid_content_keys.add(content_key)

    print(f"Total valid movie-related content items: {len(valid_content_keys)}")

    users_by_platform = defaultdict(lambda: defaultdict(dict))

    # 2. Extract creators of valid content items
    cursor.execute("SELECT platform, creator, content_key FROM content_items WHERE creator != '' AND creator IS NOT NULL")
    for platform, creator, ckey in cursor.fetchall():
        if ckey not in valid_content_keys:
            continue

        platform = platform.lower()
        creator = creator.strip('@')
        if platform == 'facebook' and is_noise(creator):
            continue

        if creator not in users_by_platform[platform]:
            users_by_platform[platform][creator] = {"role": "creator", "known_content_count": 0, "comments_made": 0}
        users_by_platform[platform][creator]["known_content_count"] += 1

    # 3. Extract commenters on valid content items
    cursor.execute("SELECT platform, author, author_id, content_key FROM comments WHERE author != '' AND author IS NOT NULL")
    for platform, author, author_id, ckey in cursor.fetchall():
        if ckey not in valid_content_keys:
            continue

        platform = platform.lower()
        author = author.strip('@')
        if platform == 'facebook' and is_noise(author):
            continue

        if author not in users_by_platform[platform]:
            users_by_platform[platform][author] = {"role": "commenter", "author_id": author_id, "comments_made": 0, "known_content_count": 0}
        if "comments_made" not in users_by_platform[platform][author]:
            users_by_platform[platform][author]["comments_made"] = 0
        users_by_platform[platform][author]["comments_made"] += 1
        if "author_id" not in users_by_platform[platform][author] and author_id:
            users_by_platform[platform][author]["author_id"] = author_id

    conn.close()

    print(f"Total platforms found: {list(users_by_platform.keys())}")
    total_users = 0
    for plat, users in users_by_platform.items():
        print(f"  - {plat}: {len(users)} movie-related users")
        total_users += len(users)

    print(f"Writing {total_users} valid users to {output_path}...")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(users_by_platform, f, indent=2, ensure_ascii=False)

if __name__ == "__main__":
    db = os.path.join("comments_data", "project_operasi_pesta_copet", "state", "scrape_state.sqlite")
    export_movie_users(db, "operasi_pesta_copet_users.json")
