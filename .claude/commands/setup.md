---
description: Walk a new user through installing internship-hunter, step by step
---
Guide the user through installing internship-hunter on their own machine.

How to run this guide:
- Reply in the user's language. If they write in French, the whole guide is in French. Otherwise English.
- One step at a time. Before each step, say in one or two sentences why it's needed. Then give the exact actions. Wait until the user says it's done (or shows an error) before moving on.
- Check what you can yourself (files present, versions, test commands) instead of asking.
- Never ask for a secret in the chat: tokens, keys and passwords go straight into `.env` or `credentials/`, typed by the user. Never print them, never read them back.
- If a step fails, find the cause before suggesting a retry.
- Read CLAUDE.md first: it explains how every part works.

## 1. Prerequisites
Why: the scheduler, the CV page counter and the PDF export are Windows tools, and the PDFs come out of Microsoft Word.
- Windows 10 or 11, Microsoft Word installed (only needed for the CV PDFs).
- Python 3.13: check with `py -3.13 --version`.
- In the repo folder: `py -3.13 -m venv .venv`, then `.venv\Scripts\python.exe -m pip install -r requirements.txt`.
- Warn the user if the repo path contains spaces: the scheduled task command in step 9 is simpler without them.

## 2. Google Cloud: a service account
Why: the script writes to the user's Google Sheet through the Sheets API, with its own robot account, so it never needs the user's password.
- The Google account must have 2-step verification turned on.
- Create a new project in console.cloud.google.com. Never start the paid trial or add billing: the Sheets and Drive APIs are free at this volume.
- Enable the Google Sheets API and the Google Drive API.
- Create a service account (no role needed), then a JSON key for it. Save the file as `credentials/service_account.json`. That folder is git-ignored.
- Read the `client_email` from that file (it's not a secret) and show it to the user: they need it in step 3.

## 3. The Google Sheet
Why: the sheet is the tracker the user works in; the robot only fills its own columns.
- The user creates an empty Google Sheet and shares it with the `client_email`, role Editor.
- Create `.env` with these three empty lines: `SHEET_ID=`, `TELEGRAM_TOKEN=`, `TELEGRAM_CHAT_ID=`. The user pastes the sheet ID (the part of the URL between `/d/` and `/edit`) after `SHEET_ID=`.
- Check access: open the sheet with `sheet_lib.open_spreadsheet()` and print its title only.

## 4. Telegram
Why: a digest of the new offers arrives on the user's phone after each run, so they don't have to open the sheet to know something came in.
- In Telegram, the user talks to @BotFather, sends `/newbot` and follows the prompts.
- The user pastes the token after `TELEGRAM_TOKEN=` in `.env` themselves. Say clearly that the token never goes in this chat.
- The user sends any message to their new bot, then runs `.venv\Scripts\python.exe get_chat_id.py`. It stores the chat ID in `.env` and prints only the user's first name.

## 5. Set up the sheet
Why: this builds the Offres tab, the cockpit, the guide and the colours, and it can be re-run safely.
- Run `.venv\Scripts\python.exe setup_sheet.py` and ask the user to open the sheet.
- Install the auto date: in the sheet, Extensions > Apps Script, replace the content of `Code.gs` with `apps_script/auto_date.gs`, save. No authorization is asked. It writes today's date in "Date candidature" when the user sets an applied status.

## 6. The profile
Why: the fit score compares each offer's skills with the user's, and the CV scripts only use facts the user has approved.
- Copy `profile.example.md` to `profile.md` and `cv_facts.example.yaml` to `cv_facts.fr.yaml` (and `cv_facts.example.en.yaml` to `cv_facts.en.yaml` for English CVs).
- Fill them by interviewing the user: education, experience, projects, skills. Ask, don't guess.
- Honesty rule: every line is approved by the user before it's written. Nothing is invented or inflated. Skill tiers are the user's own honest rating (1 = can use it live in an interview, 2 = used it, needs a refresh, 3 = not yet). Degree titles are copied exactly.
- These files are git-ignored: they stay on the user's machine.

## 7. config.yaml
Why: the search terms, the priority area and the scoring weights are personal.
- Copy `config.example.yaml` to `config.yaml`. Go through it with the user:
  - `search.terms` and `narrow_terms`: what they'd type in a job site.
  - The priority area: `priority.name`, `priority.region` (spelled as in `regions.py`), `priority.towns`, and its location in the scans and `watch_cap`.
  - The fit-score weights and place scores, if they want to change them.
  - `pay.legal_min_hourly`: the French legal minimum, to update each January.
- Check the request budget: `find_internships.request_count()` for the full and the quick scan. A run should stay at 40 requests or fewer.
- Run `.venv\Scripts\python.exe tests\test_config.py`, then `setup_sheet.py` again (the priority region appears in the cockpit and a filter view).

## 8. First run
Why: it checks the whole chain once, with the user watching.
- Run `.venv\Scripts\python.exe run_hunt.py`. The first run of the day scans the last 7 days (around 40 requests, a few minutes).
- Check together: new rows in Offres, a Telegram digest, no error in `logs/hunt.log`.

## 9. The scheduled task
Why: the hunt then runs by itself every 3 hours during the day.
- Build the command from `config.yaml` `runs.schedule` (start, every_hours, for_hours) and the real repo path, for example:
  `schtasks /Create /TN "InternshipHunter" /SC DAILY /ST 08:00 /RI 180 /DU 14:00 /TR "<repo>\.venv\Scripts\pythonw.exe <repo>\run_hunt.py"`
- Show the exact command and ask before creating the task.
- Then, with the user's OK, in PowerShell: `$t = Get-ScheduledTask -TaskName "InternshipHunter"`, set `$t.Settings.DisallowStartIfOnBatteries = $false`, `$t.Settings.StopIfGoingOnBatteries = $false` and `$t.Settings.StartWhenAvailable = $true`, then `Set-ScheduledTask -InputObject $t`. That lets a laptop run on battery and catch up after sleep.
- Test with `schtasks /Run /TN InternshipHunter`: a new line must appear in `logs/hunt.log`. "skipped (3h guard)" is fine, it proves the task runs.

## 10. The rules
Say these plainly before finishing:
- Never use `--force` for real scans: it bypasses the 3-hour minimum between scrapes, and LinkedIn blocks you if you ask too often. No auto-apply: the tool finds offers, the user applies.
- Turn the VPN off at run times: a VPN can block the Sheets API (the offers then wait in a local queue until the next run).
- Don't move or edit the robot columns. They're found by their header name, so moving one by mistake doesn't break anything, but editing one by hand gets overwritten or confuses the matching. That's why they ask for confirmation.
- Close heavy apps (Excel with big files) at run times on a laptop with little memory: Python can take minutes to start otherwise.
