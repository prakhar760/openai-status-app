# OpenAI Status Watcher

A lightweight Python watcher that prints new OpenAI API incident/outage/degradation updates from `https://status.openai.com`.

It avoids inefficient full-page refresh loops by using:
- conditional HTTP requests (`If-None-Match` / `If-Modified-Since`)
- adaptive backoff with jitter
- update-level deduplication

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python status_watcher.py
```

## Example output

```text
[2025-11-03 14:32:00] Product: OpenAI API - Chat Completions
Status: Degraded performance due to upstream issue
```

## Options

```bash
python status_watcher.py --help
```

- `--url` status page base URL (default: `https://status.openai.com`)
- `--label` output label (default: `OpenAI API`)
- `--min-interval` minimum check interval in seconds (default: `20`)
- `--max-interval` maximum check interval in seconds (default: `300`)
