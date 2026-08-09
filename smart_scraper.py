import argparse
import subprocess
import sys
import json
import os
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description="Smart Scraper: Metrics-first two-stage pipeline.")
    parser.add_argument("--project", type=str, required=True, help="Project name (required to find output folders)")
    parser.add_argument("--top-n", type=int, default=10, help="Number of top posts to deeply scrape")
    # All other arguments are forwarded.
    args, unknown_args = parser.parse_known_args()

    project = args.project
    top_n = args.top_n

    # Reconstruct the command line for the child scripts
    # We include --project because we parsed it explicitly, and pass all unknown args
    base_cmd_args = ["--project", project] + unknown_args

    print(f"\n{'='*50}\n[STAGE 1] Running Broad Metrics Discovery\n{'='*50}")
    metrics_cmd = [sys.executable, "incremental_metrics.py"] + base_cmd_args

    # Run the metrics sweep
    try:
        subprocess.run(metrics_cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Stage 1 failed with exit code {e.returncode}")
        sys.exit(e.returncode)

    print(f"\n{'='*50}\n[STAGE 2] Sorting Top {top_n} Posts by Engagement\n{'='*50}")
    # Locate the compiled metrics JSON
    project_dir = Path("comments_data") / f"project_{project}" / "compiled" / "reports"
    if not project_dir.exists():
        print(f"[ERROR] Could not find reports directory: {project_dir}")
        sys.exit(1)

    # Find the most recently modified combined_all_platforms_metrics.json
    metrics_files = list(project_dir.rglob("combined_all_platforms_metrics.json"))
    if not metrics_files:
        print(f"[ERROR] Could not find any combined_all_platforms_metrics.json in {project_dir}")
        sys.exit(1)

    # Sort by modification time to get the latest
    latest_metrics_file = max(metrics_files, key=os.path.getmtime)
    print(f"[INFO] Analyzing data from: {latest_metrics_file}")

    with open(latest_metrics_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    videos = data.get("videos", [])
    if not videos:
        print("[INFO] No videos were found during discovery. Exiting.")
        sys.exit(0)

    # Calculate engagement score for each video
    for v in videos:
        likes = v.get("like_count") or 0
        views = v.get("view_count") or 0
        shares = v.get("share_count") or 0
        comments = v.get("reported_comment_count") or 0

        # Convert any string metrics to int safely
        def to_int(val):
            try:
                if val is None:
                    return 0
                if isinstance(val, str):
                    val = val.replace(',', '')
                return int(val)
            except (ValueError, TypeError):
                return 0

        engagement = to_int(likes) + to_int(views) + to_int(shares) + to_int(comments)
        v["_total_engagement"] = engagement

    # Sort descending by engagement
    sorted_videos = sorted(videos, key=lambda x: x["_total_engagement"], reverse=True)

    # Take top N
    top_videos = sorted_videos[:top_n]

    # Create candidates.json
    candidates_path = Path("candidates.json")
    with open(candidates_path, "w", encoding="utf-8") as f:
        json.dump(top_videos, f, indent=2, ensure_ascii=False)

    print(f"[SUCCESS] Saved top {len(top_videos)} posts to candidates.json")
    for i, v in enumerate(top_videos):
        url = v.get('url', 'Unknown URL')
        eng = v.get('_total_engagement', 0)
        print(f"  {i+1}. Engagement: {eng:,} | {url}")

    print(f"\n{'='*50}\n[STAGE 3] Running Deep Scrape on Top Performers\n{'='*50}")
    deep_cmd = [sys.executable, "incremental_project.py"] + base_cmd_args + ["--candidate-file", str(candidates_path)]

    try:
        subprocess.run(deep_cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Stage 3 failed with exit code {e.returncode}")
        sys.exit(e.returncode)

    print(f"\n{'='*50}\n[FINISHED] Smart Scraper Pipeline Complete\n{'='*50}")

if __name__ == "__main__":
    main()
