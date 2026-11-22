# Server runbook

One operator, one machine. Long jobs stay in a session that survives a disconnect. API keys stay in an env file. This runbook does not post, proxy, or skip review.

Paths below use `/opt/montaj`. Use the real checkout if it lives somewhere else.

## Env file

Keep keys in the env file, mode `600`, owned by the operator account. Never type a key into the shell, and never commit `.env`.

```bash
install -d -m 700 /opt/montaj
install -m 600 /dev/null /opt/montaj/.env
chmod 600 /opt/montaj/.env
```

Edit `/opt/montaj/.env` in a text editor. Start from `.env.example`. Do not `export` a key in the shell.

## Long jobs in tmux

```bash
tmux new -s montaj
cd /opt/montaj
uv run montaj run 1
```

Detach with Ctrl-b then d. Reattach with `tmux attach -t montaj`.

## systemd

`/etc/systemd/system/montaj-run.service`:

```ini
[Unit]
Description=Montaj pipeline run
After=network-online.target

[Service]
Type=oneshot
User=montaj
WorkingDirectory=/opt/montaj
EnvironmentFile=/opt/montaj/.env
ExecStart=/opt/montaj/.venv/bin/montaj run 1
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

`WorkingDirectory` is the checkout. `EnvironmentFile` is the mode `600` env file. `Restart=on-failure` runs the job again when the process exits with an error. Change `run 1` to the source id for that job. `montaj run` stops at the compliance check. It does not review, export, or post.

```bash
systemctl daemon-reload
systemctl start montaj-run.service
```

## Logs

Montaj writes `data/logs/montaj.log` through a rotating file handler (1 MB, five backups). The handler replaces `DEEPGRAM_API_KEY` and `ANTHROPIC_API_KEY` values with `[redacted]`. Do not log a full transcript. Stage commands record cost in the database, not the transcript text.

Optional logrotate snippet, `/etc/logrotate.d/montaj`:

```text
/opt/montaj/data/logs/montaj.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
```

`copytruncate` keeps the file handle the process already opened.

## Preview over an SSH tunnel

`montaj preview serve` binds `127.0.0.1:8765` and nothing else. On the server, start it in tmux:

```bash
uv run montaj preview serve
```

From the laptop:

```bash
ssh -L 8765:127.0.0.1:8765 user@server
```

Open `http://127.0.0.1:8765/` on the laptop. The tunnel ends at the server's loopback address. Do not change the preview bind address.

## Backup

`montaj backup` writes an online copy of `data/montaj.db` to `data/backups/montaj-<UTC timestamp>.db` and keeps the newest `MONTAJ_BACKUP_KEEP` copies (default 7).

Nightly cron, as the operator user:

```cron
15 3 * * * cd /opt/montaj && /opt/montaj/.venv/bin/montaj backup
```

Open a copy with `sqlite3 data/backups/montaj-<timestamp>.db` to confirm it is readable. Do not copy the live database with `cp` while a stage is writing.

## Clean

`montaj clean` deletes a source `video.mp4` and a clip `raw.mp4` only when the file is older than `MONTAJ_CLEAN_OLDER_THAN_DAYS` (default 14) and every clip for that source is posted or rejected. A source with no clips, or with any clip still in progress, keeps its video. Final renders, exports, transcripts, and the database stay.

```bash
uv run montaj clean --dry-run
uv run montaj clean --older-than-days 14
```

`--dry-run` prints each path and how many bytes it would free, and deletes nothing.

## Disk space

`montaj doctor` warns when free space on the data volume is below `MONTAJ_MIN_FREE_MB` (default 1024). The warning does not fail `doctor` by itself. `montaj ingest` refuses to start a download when free space is below that floor, and refuses a local file larger than the free space, before it copies or downloads.

## One source at a time

Each command that writes a source or its clips takes `data/sources/<source id>/.lock`. The lock is an OS file lock, so it is released when the process exits, including after a crash. A second command prints the holding command and its start time, then exits non-zero. `--wait 30` on that command, or on `montaj run`, waits up to 30 seconds. `montaj run` holds the lock once for the whole run. `costs`, `doctor`, and the list commands do not take it.

## yt-dlp

Pinned version: `2026.10.26`. `montaj doctor` prints the installed version and warns when it is not that release. Update with one command:

```bash
yt-dlp -U
```
