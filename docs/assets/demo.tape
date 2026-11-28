# Reproducible Brain Framework terminal demo (recorded with vhs 0.12.1, ttyd 1.7.7, ffmpeg 9.1.0).
# Run from docs/assets/ with `bf` and `jq` on PATH: vhs demo.tape
Require bf
Require jq
Require ttyd
Require ffmpeg

Output demo.gif

Set Shell bash
Set FontFamily "GoogleSansCode Nerd Font Mono"
Set FontSize 18
Set LineHeight 1.25
Set Padding 28
Set BorderRadius 12
Set Margin 1
Set MarginFill "#9AA0A6"
Set Width 1040
Set Height 540
Set TypingSpeed 32ms
Set Framerate 15
Set CursorBlink false

Source theme.tape

# Seed a clean synthetic brain in a fresh temporary workspace before recording starts.
Hide
Type@0ms `unset BF_BRAIN && DEMO="$(mktemp -d)" && export PS1='$ ' && bf init "$DEMO/brain" --name demo >/dev/null && cd "$DEMO/brain" && printf '__M''1__\n'`
Enter
Wait+Screen@10s /__M1__/
Type@0ms `printf '%s\n' '{"id":"brief","title":"Website brief","type":"doc","text":"Visitors need one clear product page before signup.","created":"2026-11-10T09:00:00Z","updated":"2026-11-10T09:00:00Z"}' | bf ingest workspace >/dev/null && printf '__M''2__\n'`
Enter
Wait+Screen@10s /__M2__/
Type@0ms `printf '%s\n' '---' 'type: project' 'status: active' 'created: 2026-11-12' 'updated: 2026-11-15' 'tags: [launch, web]' 'description: Launch a single-page product site.' '---' '' '# Launch site' '' '## Decision {#decision owner=product}' '' 'Ship one product page ([brief](workspace:brief){rel=cites}).' '' '- [ ] Draft hero copy.' > projects/launch.md && clear && printf '__M''3__\n'`
Enter
Wait+Screen@10s /__M3__/
Type@0ms `printf '%s\n' '#!/usr/bin/env python3' 'import json' 'print(json.dumps({"id": "gh-42", "title": "Launch checklist", "type": "issue", "created": "2026-11-20T10:00:00Z"}))' > sensors/github-issues.py && chmod +x sensors/github-issues.py && printf '__M''4__\n'`
Enter
Wait+Screen@10s /__M4__/
Type@0ms `printf '%s\n' '#!/usr/bin/env python3' 'import sys' 'sys.stderr.write("OAuth token expired\n")' 'sys.exit(1)' > sensors/gmail-sync.py && chmod +x sensors/gmail-sync.py && printf '__M''5__\n'`
Enter
Wait+Screen@10s /__M5__/
Type@0ms `printf '%s\n' 'version: 19' 'name: demo' 'loops:' '  github-issues:' '    description: Sync open GitHub issues' '    ingest: upsert' '    when: 5m' '  gmail-sync:' '    description: Sync Gmail threads' '    ingest: upsert' '    when: 5m' '  nightly-review:' '    description: Check stale notes' '    run: [bf, review]' > bf.yaml && clear && printf '__M''6__\n'`
Enter
Wait+Screen@10s /__M6__/
Type@0ms `printf '%s\n' '{"id":"brief","title":"Website brief","type":"doc","text":"Add a pricing table before signup.","created":"2026-11-10T09:00:00Z","updated":"2026-11-20T14:00:00Z"}' > ../brief-v2.json && bf build >/dev/null && bf check >/dev/null && printf '__M''7__\n'`
Enter
Wait+Screen@15s /__M7__/
Type@0ms `clear`
Enter
Sleep 500ms
Show

# Act 1: Ranked BM25 FTS5 search and typed backlinks.
Sleep 400ms
Type `bf query "product page" --format text`
Enter
Wait+Screen@5s /workspace:brief/
Sleep 1.6s

Type `bf query --links-to workspace:brief --format text`
Enter
Wait+Screen@5s /single-page product site/
Sleep 2.2s

# Act 2: Upstream evidence changes -> bf review flags newer_evidence automatically.
Hide
Type@0ms `clear`
Enter
Sleep 500ms
Show

Sleep 350ms
Type `bf review --format text`
Enter
Wait+Screen@5s /FRESH/
Sleep 1.2s

Type `bf ingest workspace --input ../brief-v2.json | jq -c '{ok,source,updated}'`
Enter
Wait+Screen@5s /"updated":1/
Sleep 1.2s

Type `bf review --format text`
Enter
Wait+Screen@5s /newer_evidence/
Sleep 2.4s

# Act 3: Zero-termios failure-first live supervision with bf watch.
Hide
Type@0ms `clear`
Enter
Sleep 500ms
Show

Sleep 350ms
Type `bf watch`
Enter
Wait+Screen@8s /OAuth token expired/
Wait+Screen@8s /\+1 \(1 total\)/
Sleep 3.6s
Ctrl+C
Wait+Screen@5s /stop/
Sleep 1.2s
