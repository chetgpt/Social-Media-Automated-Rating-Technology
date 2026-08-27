import json

def is_noise(username):
    # Common UI artifacts picked up by raw web scrapers on Facebook
    ui_noise_exact = {
        "photo", "video", "author", "top fan", "follow", "share",
        "like", "reply", "replies", "view more comments", "view previous comments",
        "hidden", "edited", "remove", "see more", "see translation"
    }

    ui_noise_contains = [
        "top fan", "replied ·", "· reply", "author", "follow"
    ]

    u_lower = username.lower().strip()

    # 1. Exact match against known UI artifacts
    if u_lower in ui_noise_exact:
        return True

    # 2. Check for numeric only (sometimes IDs leak into names)
    if u_lower.isdigit():
        return True

    # 3. Very short names
    if len(u_lower) <= 1:
        return True

    # 4. Very long names (usually parsing errors capturing comment text or spam)
    if len(u_lower) > 40:
        return True

    # 5. Containing weird UI patterns or appended UI text (e.g. "John Doe Top Fan")
    for noise in ui_noise_contains:
        if noise in u_lower:
            return True

    # 6. Starts with a weird character often caused by parsing errors
    if username.startswith('· ') or username.startswith(' - ') or '\n' in username or '\r' in username:
        return True

    # 7. Check for consecutive zero-width characters or extreme repetition (e.g., Học⁮⁮⁮)
    if '\u200e' in username or '\u200f' in username or '\u202a' in username or '\u202c' in username or '\u206e' in username:
        return True

    return False

def filter_users(input_file, output_file):
    print(f"Loading {input_file}...")
    with open(input_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    filtered_data = {}
    total_removed = 0

    for platform, users in data.items():
        filtered_data[platform] = {}
        removed_for_platform = 0

        for username, details in users.items():
            if platform == 'facebook' and is_noise(username):
                removed_for_platform += 1
                total_removed += 1
            else:
                filtered_data[platform][username] = details

        print(f"{platform.capitalize()}: Kept {len(filtered_data[platform])}, Removed {removed_for_platform}")

    print(f"Total Facebook noise removed: {total_removed}")

    print(f"Saving to {output_file}...")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(filtered_data, f, indent=2, ensure_ascii=False)

    print("Done!")

if __name__ == "__main__":
    filter_users('operasi_pesta_copet_users.json', 'operasi_pesta_copet_users_filtered.json')
