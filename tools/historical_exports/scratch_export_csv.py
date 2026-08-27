import sqlite3
import json
import csv
import sys

def main():
    conn = sqlite3.connect(r'comments_data\project_somethinc_music_audit\state\engage_state.sqlite')
    cursor = conn.execute("SELECT post_id, url, evidence_json FROM engage_tiktok_posts")

    rows = []
    for post_id, url, ev_json in cursor:
        try:
            data = json.loads(ev_json)
        except Exception:
            continue

        # Try to flatten basic data
        if isinstance(data, list): data = data[0] if data else {}

        caption = data.get("desc", data.get("itemInfo", {}).get("itemStruct", {}).get("desc", ""))

        # Metrics
        stats = data.get("stats", data.get("itemInfo", {}).get("itemStruct", {}).get("stats", {}))
        plays = stats.get("playCount", 0)
        likes = stats.get("diggCount", 0)
        comments = stats.get("commentCount", 0)
        shares = stats.get("shareCount", 0)

        # Music Data
        music = data.get("music", data.get("itemInfo", {}).get("itemStruct", {}).get("music", {}))
        music_title = music.get("title", "")
        music_author = music.get("authorName", "")

        # Extract comments we scraped
        comments_array = data.get('comments', {}).get('comments', [])
        scraped_comments_count = len(comments_array) if isinstance(comments_array, list) else 0

        rows.append({
            "post_id": post_id,
            "url": url,
            "caption": caption.replace('\n', ' ').replace('\r', ''),
            "plays": plays,
            "likes": likes,
            "comments_metric": comments,
            "shares": shares,
            "music_title": music_title,
            "music_author": music_author,
            "scraped_comments_count": scraped_comments_count
        })

    with open("somethinc_music_audit.csv", "w", newline="", encoding="utf-8") as f:
        if not rows: return
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    print(f"Exported {len(rows)} rows to somethinc_music_audit.csv")

if __name__ == "__main__":
    main()
