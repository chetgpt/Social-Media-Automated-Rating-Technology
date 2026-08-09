# TikTok Comments JSON Compiler

This folder contains scripts to compile TikTok comments from multiple JSON files in the `../comments_data` directory into a single comprehensive file (JSON or CSV).

## Scripts

Two scripts are provided:

1. `compile_comments.py` - Compiles comments to JSON only
2. `compile_comments_to_csv.py` - Compiles comments to both JSON and CSV (with format options)
3. `run_compiler.bat` - Simple batch interface for Windows users

## Usage

### Quick Start

For Windows users, simply run the batch file:

```
run_compiler.bat
```

This will provide a menu to choose compilation options.

### JSON Compilation (compile_comments.py)

```bash
python compile_comments.py
```

#### Available Options

- `--comments_dir`: Directory containing comments data (default: "../comments_data")
- `--output`: Output JSON file path (default: "compiled_comments.json")
- `--filter_username`: Filter comments by username (case insensitive)
- `--no_session_info`: Don't add session info to comments

### JSON and CSV Compilation (compile_comments_to_csv.py)

```bash
python compile_comments_to_csv.py
```

#### Available Options

- `--comments_dir`: Directory containing comments data (default: "../comments_data")
- `--output_json`: Output JSON file path (default: "compiled_comments.json")
- `--output_csv`: Output CSV file path (default: "compiled_comments.csv")
- `--filter_username`: Filter comments by username (case insensitive)
- `--no_session_info`: Don't add session info to comments
- `--format`: Output format: "json", "csv", or "both" (default: "both")

### Examples

Filter comments by username and save to CSV only:
```bash
python compile_comments_to_csv.py --filter_username "npureofficial" --format csv
```

Specify a different comments directory:
```bash
python compile_comments.py --comments_dir "C:/path/to/comments_data"
```

## Output Format

### JSON Output

The compiled JSON file has the following structure:

```json
{
  "compiled_date": "2025-04-16 19:30:45",
  "total_videos_processed": 1250,
  "total_comments": 45390,
  "comments": [
    {
      "username": "example_user",
      "commentText": "Example comment text",
      "timeStamp": null,
      "video_id": "201",
      "session": "session_20250416_185038"
    },
    ...
  ]
}
```

### CSV Output

The CSV file contains all comments in tabular format with headers for each field. The fields are ordered with important fields first:

1. `username`
2. `commentText`
3. `video_id`
4. `session`
5. Additional fields (alphabetically ordered)

This format is ideal for importing into spreadsheet applications or data analysis tools like Pandas. 