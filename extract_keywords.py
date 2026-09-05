import re
from collections import Counter

with open("captions_dump.txt", "r", encoding="utf-8") as f:
    text = f.read()

# Find all hashtags
hashtags = re.findall(r'#\w+', text)

# Count frequencies
counts = Counter([h.lower() for h in hashtags])

print("Top 30 Hashtags:")
for tag, count in counts.most_common(30):
    print(f"{tag}: {count}")
    
# Let's also look for common phrases like "band indie", "lagu baru", etc.
words = re.findall(r'\b[a-zA-Z]{4,}\b', text.lower())
word_counts = Counter(words)
print("\nTop 30 Words:")
for word, count in word_counts.most_common(30):
    print(f"{word}: {count}")

