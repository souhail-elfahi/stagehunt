# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Single-script tool (`find_internships.py`) that scrapes Indeed and LinkedIn via `python-jobspy` for data-related internships (data analyst / data scientist / statistics and econometrics) across the configured country, with a dedicated search in the user's priority area, then writes a cleaned, deduplicated, newest-first CSV named `internships_YYYY-MM-DD.csv` in the working directory. CSVs in the repo are run outputs, not inputs.

## Settings: config.yaml

Everything specific to one user lives in `config.yaml` (committed with the author's values; `config.example.yaml` is the generic, commented template), read once by `config.py` (`INTERNSHIP_HUNTER_CONFIG=<path>` reads another file). It holds:
- the search terms, narrow terms, locations with their LinkedIn counts, and the request budget;
- the priority area (`priority`: metro name, region, towns), used by the Lieu score, the 🏠 digest group, the cockpit to-do list and the 🏠 filter view;
- the fit-score weights and the place scores, the legal minimum pay (€/h, **update each January**), freshness and the duration penalty;
- the 3h guard, follow-up days, the schedule, the goal defaults and the time zone.

**Never hard-code a personal value** (a city, a region, a term, a weight) in the code: add it to both config files instead. `tests/test_config.py` checks that the two files have the same structure and that the template works on its own.

## Offer-analyzer pipeline

Run in this order; each step reads the previous step's output:

1. `find_internships.py` → `internships_YYYY-MM-DD.csv` (no descriptions).
2. `fetch_descriptions.py` → `offers_with_descriptions.csv`. LinkedIn: one request per offer via jobspy's private `LinkedIn._fetch_details(job_id)` (it logs failures instead of raising, so a log handler detects 429s and backs off). Indeed: re-runs the searches and joins on the `jk` key, because Indeed search results already carry descriptions. Resumable: rows with `fetch_status == ok` are kept on re-run. Takes ~10 min.
3. `skills_dictionary.py`: `SKILLS` maps canonical skill names → FR/EN regexes. **Canonical names must match the `Skill` column in `profile.md` exactly**, because the gap analysis joins on them. `R` and `SAS` only match in a tool context (they collide with "R&D" and the company legal form "SAS").
4. `offers_structured.jsonl` is written **by Claude**, not by a script: read the descriptions in batches of 10 and write one record per offer (`job_url, title, company, role_cluster, missions[3], required_skills, nice_to_have, profile, level, languages, source`). `role_cluster` ∈ {data analyst, data scientist, quant-economist, AI-ML engineer, other}. Use canonical dictionary names for skills. Give cross-site duplicates **identical mission bullets**, because `analyze_market.py` dedupes on them.
5. `analyze_market.py` → `market_report.md` + `charts/*.png`. It parses every skills table under `## Skills` in `profile.md` (blank tier = not in profile; the parser is `skills_dictionary.parse_profile`, shared with `fit_score`). Gap thresholds are the `REFRESH_MIN` / `LEARN_MIN` / `LOW_DEMAND` constants.

`profile.md` is the user's honest self-assessment. Don't change tiers or wording; only append new skill rows with an empty tier. It and `market_report.md` are git-ignored (personal data). `profile.example.md` is the committed template with fictional data: copy it to `profile.md` to start. When you add a skill to `SKILLS`, add a row to both files.

## Sheet + alerts (scheduled hunt)

A Google Sheet, tab "Offres", is the tracker. `run_hunt.py` (Task Scheduler, every 3h from 08:00 to 22:00) scrapes, upserts the sheet and sends a Telegram digest of the new offers.

- **Secrets:** `.env` (`SHEET_ID`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`) and `credentials/service_account.json` are git-ignored. Never print or log them. `run_hunt`'s `RedactingFormatter` strips the token, which is part of Telegram URLs, from every log line. The sheet must be shared (Editor) with the service account's `client_email`.
- **Column ownership** (`sheet_lib.py`):
  - **Robot columns:** `ROBOT_COLUMNS` (Titre … Nouveau) and `EXTRA_ROBOT_COLUMNS` (Autres liens, Doublon ?, Région, Score, Score fiable, Gratification, Durée, Remarque, Ville, ➜ Prochaine étape). They carry **warning-only protection**, so editing one by hand asks for a confirmation (accidental pastes, Sheets' "replace URL with its title"); the script's writes are unaffected.
  - **Canonical "focus mode" order** (`COLUMNS`), applied once with `setup_sheet.py --arrange`, never by a normal run:
    - **Visible:** Titre (frozen, 380 px, clipped), Entreprise, Ville, Région, Date publication, Âge, Lien, Score, Statut, ➜ Prochaine étape, the rest of the user's columns (Date candidature … Notes), Gratification, Durée, then the user's own extra columns.
    - **Grouped:** the technical columns (`FOCUS_GROUP`: Lieu, Site, detections, En ligne, Nouveau, Autres liens, Doublon ?, Score fiable, Remarque) sit in one collapsible column group, collapsed by default. Groups need adjacent columns: if they've been separated, setup skips the group and says so.
  - **User columns:** `USER_COLUMNS` (Statut … Notes, canonical L–R), plus any extra column the user adds.
  - **Never write the user's columns, and never rewrite the whole sheet.** The only exception is Statut = "À trier" on rows the robot inserts.
  - **Columns are located by header name, never by position** (`sheet_lib.read_layout`, row 1 read at every run), so the user may move columns or add their own. **Every script works through the `Layout`:** `layout.letter(name)`, `layout.cell(row, name)`, `layout.check_robot_range(a1)`, `sync_sheet.block_writes` (one range per run of adjacent robot columns).
  - **Broken header:** a missing or duplicated expected header raises `LayoutError` **before any write**. `run_hunt` then queues the offers and logs an error, and the digest gets "⚠️ Colonnes du Sheet introuvables ou en double … : synchro en attente". `setup_sheet` refuses to run.
  - **`check_robot_range`** checks every column a range covers against the actual layout (user and extra columns refused), on every write, so keep it on any new write path.
  - **New robot columns** are appended after the last column: `setup_sheet` adds missing headers only when every missing one is a robot column; a missing user header is a `LayoutError`, never invented.
  - **Formulas that survive a column move:** Sheets rewrites normal references (dashboard counts, Score, conditional formats) when columns move. Never put column letters inside text, e.g. a QUERY string: "Mes candidatures" queries an array of column references (`Col5`…) for that reason.
  - **Formulas that survive row inserts:** formulas over Offres use **whole columns** (`'Offres'!$I:$I`, `setup_sheet.rng`), never `$I$2:$I`. The sync inserts new rows at row 2, and Sheets would shift such a range to row 3, silently leaving the new offers out of every count.
  - **Lien cells:** Sheets' link preview can "replace the URL with its title". The cell then shows the page title, and the URL only survives as link metadata, which the robot doesn't read. The title|company|city key still matches the offer. `sync_sheet.py --backfill` puts the URL back (it reads the link metadata of the Lien column).
- `setup_sheet.py` is idempotent and safe to re-run. It sets the spreadsheet to the configured time zone + fr_FR plus the theme, and builds three tabs. `--sheet-id <id>` runs it on another spreadsheet: always rehearse layout changes on a rehearsal spreadsheet with synthetic rows first. The user creates it and shares it with the service account, because service accounts have no Drive quota to create files. Syncs and backfills on a rehearsal (`sheet_id=` / `--sheet-id`) keep their backups in `backups/rehearsal/`, never among the tracker's: only the last 20 are kept, and `pending_sync` reads the latest.
  - **Theme (cyberpunk "cockpit", all tabs, original styling with no game assets):**
    - **Palette** (`sheet_lib.COLORS`): background #0D0221, panels #1A0B2E, text #F0E6FF, and neon pink / cyan / yellow / violet / blue / red. **No green anywhere.**
    - **Fonts:** `FONT_DISPLAY` Audiowide (main title), `FONT_TITLE` Orbitron (labels), `FONT_MONO` Share Tech Mono (every number), `FONT_BODY` Roboto.
    - **Contrast:** every text/background pair must pass WCAG AA (≥ 4.5:1). `tests/test_theme.py` checks all of them from the generated requests, and also fails on any green hue. Run it after any colour change.
    - **Scope:** on Offres it only changes formatting, through field masks that list exactly what is set, never values. Colours or fonts set by hand on Offres are replaced on re-run.
  - **Offres:** header, dropdowns (Statut, CV utilisé = `CV_OPTIONS`, Prochaine action = `NEXT_ACTIONS`; non-strict), date formats, frozen header row + Titre column, basic filter, widths, conditional-format rules, the column group, the robot-column protection, and 4 saved **filter views** (`FILTER_VIEWS`, Données > Vues filtrées):
    - 🔍 Triage (À trier, Score ↓);
    - ⚡ À postuler (Score ↓);
    - 🏠 <priority region from config.yaml> (Score ↓);
    - 📨 En cours (applied statuses, Date candidature ↓).

    They're rebuilt on each run, recognised by title. The slicers of earlier versions overlapped the header, so they're removed (`OLD_SLICERS`).
  - **Status colours** (`sheet_lib.STATUS_STYLE`, one hue per family, readable from far away): **the whole row (every column, Score included) is a solid block of the status colour with dark text #0D0221.** That includes links, at 5.2–16.6:1.
    - The families: À postuler yellow, Postulé cyan, Réponse RH blue, the Entretien statuses pink, Offre violet and bold, Refus red.
    - "À trier" keeps the dark row, with a grey Statut block.
    - Ghosté / Pas intéressé / Offre fermée get a dark grey Statut cell and a dimmed row.
    - **Follow-up due** (Postulé for **≥ `FOLLOW_UP_DAYS` = 7 days**, the same test in the colours, the column and the dashboard): the row stays cyan, and the Date candidature cell becomes a solid yellow block.
    - "Mes candidatures" uses the same Statut blocks.
  - **Rule order** (Sheets applies only the first matching rule to a cell, so the order in `status_rules()` is the priority):
    1. the Statut blocks;
    2. the follow-up date block;
    3. the dimmed rows;
    4. the solid-colour rows;
    5. the **Score colour scale** (red → yellow → cyan, min / median / max, dark text), which only "À trier" rows reach, because every other status colours its whole row;
    6. bold for new offers.

    The rules cover every column starting at **row 1**, so inserting rows at row 2 doesn't shift them. Their formulas use the layout's letters.
  - **Recognising our rules on re-run:** by formula *shape*, with the column letters blanked (`shape()`), because Sheets rewrites the letters when columns move. The scale is recognised by its column or by its shape (`is_our_scale`). A formula from an older version that the current rules no longer produce goes in `LEGACY_FORMULAS`; otherwise it stays behind as a stale rule.
  - **Dashboard style:** `> MISSION DU JOUR`, `SYS://ONLINE`, `/// CIBLES PRIORITAIRES`, pink/cyan card borders, yellow ▰▱ progress bars (`bar()`, 10 segments) and yellow accent stripes. The Relances card turns solid red when > 0.
  - **Re-running** replaces only its own rules (see above), filter views (by title), column groups and robot protections (by description). Our rules are recognised by formula shape, with column letters and `#REF!` blanked, so rules broken by a deleted column are cleaned up too.
  - **"🚀 Tableau de bord"** (`DASHBOARD_TAB`, first tab): live formulas over Offres only, 22 rows × 14 columns, gridlines hidden, so it fits a laptop screen.
    - **Contents:** goal bars, 6 KPI cards (the Relances card turns red when > 0), "⚡ À faire maintenant" (overdue follow-ups, then the best "À postuler" by Score, max 5, built with LET/FILTER/VSTACK), the funnel, and a 14-day SPARKLINE.
    - **Status groups:** `APPLIED`, `REPLIED`, `REACHED_INTERVIEW` and `IN_INTERVIEW` in `sheet_lib`, as the user defined them: Ghosté counts as applied but not as a reply, Refus counts as a reply, and Offre fermée isn't applied.
    - **Goal cells D3 (daily, default 3) and J3 (weekly, default 15) are the user's:** setup reads them before rewriting the tab and writes the default only when they're empty. The rest of the tab is the robot's and is fully rebuilt on each run.
  - **fr_FR formula syntax:** `;` between arguments, English function names, and array literals written `{"a"\1;"b"\2}` (`ARRAY_COL_SEP`).
  - **"➜ Prochaine étape"** (`NEXT_STEP`, right after Statut, per `PLACE_AFTER`):
    - **The text:** what to do next, by Statut (`NEXT_STEPS`, the user's wording). For Postulé, it depends on Date candidature: "Ajouter la Date candidature" when it's empty, "Attendre · relance le dd/mm" before 7 days, "🔴 Relancer aujourd'hui" from 7 days.
    - **How it's filled:** it's a **formula column** (`FORMULA_COLUMNS`), meaning **one ARRAYFORMULA in its header cell over whole columns** (`next_step_formula`), rewritten on each setup. It covers every row, including the ones the sync inserts at row 2.
    - **Never write a cell below it:** a typed value breaks the whole column with `#REF!`. `Layout.writable` excludes it, so `check_robot_range` refuses any such write. Its computed text never counts as user data (`Layout.robot`).
    - **Adding such a column:** setup inserts a missing `PLACE_AFTER` column right after its anchor (the columns to the right shift, with their data) and clears any inherited data validation on it.
  - **"📘 Guide"** (`GUIDE_TAB`, second tab): exactly 3 sections (> ROUTINE DU JOUR with real checkboxes, > STATUTS with each status in its colour block, > RÈGLES D'OR, numbered).
    - **Texts:** copied **verbatim** from `guide.fr.yaml` (committed, no personal data), which the user approved. Never reword them without the user's OK. No message templates and no interview checklist: the user writes messages case by case.
    - **Ticks:** they're the user's. Setup reads them first, by their text, and puts them back; everything else on the tab is the robot's and is rebuilt on each run.
  - **"Mes candidatures"** (`APPLICATIONS_TAB`, third tab): one QUERY over Offres (Statut in `APPLIED`, newest application first), protected with a warning. The user edits in Offres, never here.
- `sync_sheet.sync(offers_df)` does the following, in order:
  1. Backs up the sheet to `backups/sheet_YYYY-MM-DD_HHMMSS.csv`, keeping the last 20. The name has seconds because a queue flush and the run's own sync can happen in the same minute.
  2. Matches offers on `Lien`, a link in `Autres liens`, or `find_internships.dedup_key` (title|company|city). Next it applies the fuzzy rules of `dedup.classify`: a "merge" puts the offer's link in the matching row's Autres liens, with no new row and no alert. A "twin" is inserted, and both rows get a `Doublon ?` formula (`MATCH` on the twin's link, so the row number stays right when rows move).
  3. On rows already in the sheet, updates Dernière détection and En ligne cell by cell.
  4. Sets Nouveau to non after 24h, and En ligne to "?" after 7 days without being seen. "?" means "no longer checked", not "closed".
  5. Inserts new offers as a block at row 2 (`insertDimension`, inheriting from the row below).
- **Dedup rules** (`dedup.py`; the user chose them, so don't loosen them without asking):
  - Titles are normalized: no accents or punctuation, no stage/intern/H/F tags. Cities are normalized the same way.
  - **Merge** when all of these hold:
    - same title and same city;
    - publication dates at most `MAX_DAYS` = 2 apart;
    - related companies: a shared token outside `COMPANY_STOP`, or the same group in `COMPANY_ALIASES` (Mobilize→Renault, Boursorama→Société Générale).
  - **Also merge** when it's the same company, same city and same date, with title similarity ≥ 0.8 **on different sites**. On one site, close titles from a company are distinct roles, e.g. "R&D Data Scientist".
  - **Twin** when the title, city and dates match but the companies are unrelated. This needs a title of at least 3 words, because generic titles like "data scientist" would flag unrelated companies.
- `dedup_sheet.py` cleans up the existing rows with the same rules. A dry run comes first: it prints the plan, including near misses that aren't merged, and saves it to `backups/dedup_plan.json`. `--apply` backs up the sheet, refuses to run if the recomputed plan differs from the saved one, and keeps the row holding user data (otherwise the first detected). It deletes a row only if L–R are empty ("À trier" counts as empty), re-checking right before deleting. **Always show the user the dry run and wait for their OK before `--apply`.**

  Dates are read unformatted (serial numbers), so parsing doesn't depend on the locale. `python sync_sheet.py --import internships_YYYY-MM-DD.csv` imports a CSV (detection = file date, Nouveau = non, no alert).
- `run_hunt.py` runs the first scan of each calendar day (`logs/last_full_scan.txt`) on a 7-day window, and the others on 24h with fewer results. **3h guard:** it skips if `logs/last_scrape.txt` is less than 175 min old, unless `--force` is given (manual runs and tests only). **One run at a time:** `logs/hunt.lock` is created atomically around the whole run. A second run that starts meanwhile logs "skipped (another run in progress)". This matters because under memory pressure Python can take minutes just to start, so two overlapping runs would otherwise both pass the guard and sync concurrently. A lock older than 2h was left by a killed run and is removed. `--full` forces the 7-day scan, and `--no-alert` syncs without sending a Telegram message. The latest scan's clean offers are saved to `logs/last_scan.csv`. In quick scans, every search in the priority area (`search.watch_cap`) that returns as many results as it asked for (its cap) logs a "cap reached" warning and counts in the run summary ("priority-area cap hits"). If that count stays above 0, that location needs a higher limit. Each stage is isolated: scrape errors are logged and the sync runs anyway. No message is sent when nothing is new. The log is `logs/hunt.log`.
- **Sheet unreachable (`pending_sync.py`):** every Sheets call has a `(5, 60)` s (connect, read) timeout (`sheet_lib.SHEETS_TIMEOUT`), and the token refresh is covered too. A network-type failure (`sheet_lib.sheets_unreachable`) logs "Sheets unreachable, VPN suspected". When the sync fails:
  - All the run's offers go to `pending_sync.jsonl` (git-ignored, rewritten atomically). Each line holds the clean() fields + `detected` + `alerted`.
  - The digest is still sent, with the note "sheet sync pending". It lists queued offers that are not alerted yet and not in the latest `backups/sheet_*.csv` (exact link/key match only: the fuzzy rules need the live sheet). They're then marked `alerted`. A `--no-alert` run queues its offers as alerted.

  The next run that reaches the sheet **flushes the queue first**: one normal `sync()` per detection time, oldest first, so Première détection keeps the original time and all dedup and ownership rules apply. Each batch is dropped from the queue only after its sync succeeds. Then the run syncs its own offers. Flushed offers that were never alerted go into that run's digest. An offer with any alerted queued copy (same link or key) is never sent twice.
- **7-day scan "done"** (`last_full_scan.txt`) means the scrape worked (at most half the searches errored or returned nothing) **and** the offers are in the sheet or safely queued. A failed scrape makes the next run retry the 7-day scan; a failed sync alone doesn't, since its offers are in the queue.
- **Don't scrape more often than every 3h, and don't add any auto-apply feature.**
- Scheduled task: `schtasks /Create /TN "InternshipHunter" /SC DAILY /ST 08:00 /RI 180 /DU 14:00 /TR "<repo>\.venv\Scripts\pythonw.exe <repo>\run_hunt.py"`. The repo path has no spaces, so no inner quotes are needed, which avoids PowerShell 5.1 quote mangling. Battery: the task may start on battery and keeps running when unplugged. `schtasks` can't set that; it was set with `Get-ScheduledTask` → `Settings.DisallowStartIfOnBatteries = $false`, `StopIfGoingOnBatteries = $false` → `Set-ScheduledTask`. Missed starts: `Settings.StartWhenAvailable = $true` (same method). If the PC was asleep or off at a slot, the task runs once as soon as possible; the 3h guard and the lock still apply. Re-apply both after recreating the task.
- **Geography and request budget** (all in `config.yaml`, `search:`): the searches cover the country nationwide plus a dedicated search in the priority area. Nationwide results are capped and dominated by Paris, and the priority area is where the user can work without moving. A LinkedIn request returns 10 results and an Indeed request returns 100, so each site gets its own `scrape_jobs` call: Indeed always asks for 100 (`INDEED_RESULTS`) and LinkedIn gets the per-location count. Broad terms search nationwide and in the priority area. `narrow_terms` are rarer, so they search nationwide only, with fewer LinkedIn results. Each search costs 1 Indeed request + ceil(LinkedIn n / 10); `find_internships.request_count()` computes it. The author's budget:

  | Run | Broad ×3: nationwide / priority area (LinkedIn n) | Narrow ×3: nationwide (LinkedIn n) | Requests |
  |---|---|---|---|
  | CLI `find_internships.py`, per window | 20 / 20 | 10 | 24 |
  | Daily 7-day scan (`FULL_SCAN`) | 60 / 20 | 20 | 39 |
  | 3-hourly 24h scan (`QUICK_SCAN`) | 10 / 10 | 10 | 18 |

  **Don't exceed it without asking.** The nationwide searches run first, so an offer found by both searches is credited to France (`search_location`).
- **Fit score** (`fit_score.py`; weights and place scores come from `config.yaml` `fit_score:`, chosen by the user):
  - **Points:** Compétences 45 × the smoothed share (held + 1) / (required + 2) of the description's skills (`find_skills`) that are tier 1–2 in `profile.md`; Type de poste 20 / 16 / 6 / 8; Lieu 15 / 10 / 12 / 10 (priority area / rest of the priority region / named regions, e.g. Île-de-France / elsewhere); Rémunération 10 / 6 / 3 / 2 / 0; Fraîcheur 10 / 6 / 3 / 0 (3 if no date). An internship under 4 months gets −15.
  - **Type de poste** (analyst/études vs data scientist vs data or AI engineer vs other) is a keyword classifier on title + description; the description weighs more (MBDA case: an "engineer" title whose work is indicators and market intelligence counts as analyst). It agreed with the `offers_structured.jsonl` labels on ~80% of the 94 saved offers (~86% counting data engineers as engineer). **Finance titles are "autre" (user's call):** M&A, corporate finance and analyste financier / financial analyst titles score 8 whatever the description says, unless the title is about risk, credit or quant work, which stays analyst (`FINANCE_TITLE` / `FINANCE_KEEP`).
  - **Pay:** jobspy's pay fields (lower bound, converted to monthly), else a regex on the description. "Gratification légale / selon la réglementation" means the legal minimum: `fit_score.pay.legal_min_hourly` in `config.yaml` (4.50 €/h for 2026), **update it each January**.
  - **No description** (or under 200 characters): title only, Compétences at the neutral 50%, Score fiable = non. Shown as "~NN" in the digest.
  - **Remarque:** "Réservée RQTH" for `RQTH_COMPANIES` (Groupe Talents Handicap). It's a robot column; nothing goes in the user's Notes.
  - In the sheet, **Score is a formula**: a base constant (everything except freshness) plus freshness from the Âge column (`score_formula`), so it decays as the offer ages.
  - `profile.md`'s tiers are read through `skills_dictionary.parse_profile`, which lives there (not in `analyze_market`) so the hunt doesn't load matplotlib.
- **Descriptions:**
  - `clean()` keeps `description` and the jobspy pay fields (`SCORING_COLUMNS`) in memory and in the queue, but not in the CSVs.
  - Indeed's come with the search. LinkedIn's are fetched only for offers about to be inserted, through `sync(prepare=...)` → `linkedin_access.fetch_descriptions`: at most `MAX_DESCRIPTIONS` = 20 per run (on top of the search budget), best title-only scores first, 5–10 s apart.
  - The pending digest (sheet unreachable) doesn't fetch LinkedIn descriptions.
- **LinkedIn 429** (`linkedin_access.LinkedInGate`): the first 429 of a run (search or description fetch) stops every later LinkedIn request of that run; Indeed continues.
  - `logs/linkedin_skip.txt` makes the next run skip LinkedIn too; that run deletes the file.
  - Skipped searches don't count toward "scrape failed".
  - The digest gets a ⚠️ / ℹ️ line. No message is sent just for a 429.
- **Digest order:** 🏠 the priority region first, then the rest, then internships under 4 months; highest score first inside each group. Each line shows score, title, company, city, age, pay (if known), duration (if known) and Remarque.
- `backfill_scores.py` fills V–Z on existing rows. It writes a row only if its Score is empty, or if it's title-only and a description is now available; reliable scores are never touched.
  - Descriptions come from `offers_with_descriptions.csv`, or from `logs/indeed_descriptions.csv`, written by `--indeed` (the daily scan's searches on Indeed only, one request each, **never LinkedIn**).
  - It does a dry run by default. `--apply` backs up the sheet and refuses to write if rows moved since the read.
  - **After changing a `fit_score` rule, run `--rescore`** (dry run, then `--rescore --apply`). The sheet stores each score's base as a constant in its formula, so existing rows don't follow a rule change until they're rescored. Rescoring rewrites only the rows whose V–Z cells change, and a reliable row only when its description is still cached: a reliable score is never downgraded to title-only.
- **Ville** (`regions.ville_of`):
  - **Derived from Lieu:** the text before the first comma, with arrondissements removed ("Paris 11e" → "Paris"), apostrophes unified and all-caps fixed. "Télétravail" is kept, and a bare "France" gives "(non précisée)".
  - **One spelling per city** (`canonical_villes`, keyed by `ville_key`, which ignores accents, case, hyphens and apostrophes): the most typographic variant wins, e.g. "Villeneuve-d'Ascq".
  - **When it's filled:** on insert, on any empty cell at each sync, and by `python sync_sheet.py --backfill [--apply]`.
- **`sync_sheet.py --backfill`:** a dry run by default; `--apply` backs up first.
  - It fills empty Ville / Région cells, and puts back Lien URLs that were replaced by their page title.
  - It repairs suspect companies (`fit_score.company_suspect`: empty, or equal to the title, e.g. after an accidental paste) from saved data: scans, the queue, and the tracker's backups.
  - When a company can't be recovered, Remarque gets "Entreprise à vérifier" (`fit_score.COMPANY_CHECK`). `fit()` writes that remark too, and the sync adds it to existing rows. The robot never guesses a company.
- **Région** (`regions.py`, `region_of`): the 2016 regions. The source is either LinkedIn's region name (old and English names are mapped too) or Indeed's code. Indeed mixes newer abbreviations (`ARA`, `HDF`, `N`, `NA`, `O`, `BFC`…) with the old GeoNames codes (`A8` = Île-de-France, `A2` = Bretagne, `B5` = Pays de la Loire, `B8` = PACA…). The codes verified on real offers are marked in the file. A large-city fallback covers metro labels. Anything else becomes "?", and the sync logs a warning listing it, so add it to `regions.py`. Région is filled on insert and backfilled wherever it's empty.
- **Email alerts** (`email_alerts.py`), **optional and currently OFF**: the user checks WTTJ, HelloWork and JobTeaser by hand, and the hunt's sources are LinkedIn + Indeed only. The code is kept for later. With no `IMAP_USER` / `IMAP_APP_PASSWORD` in `.env`, `run_hunt` skips it silently: no log line, no warning. Setting both turns it on, and nothing else needs to change.
  - **What it is:** offers from Welcome to the Jungle, HelloWork and JobTeaser come **only from their email alerts**. WTTJ (CGU art. 10) and HelloWork (CGU art. 1, 9.2, 9.3) forbid automated extraction, and their robots.txt disallow search and query URLs. JobTeaser sits behind the user's university login, and its alerts are forwarded. **Never scrape or request these sites, not even to resolve a link or fetch a description.**
  - **Mailbox:** a dedicated account that only receives alerts, at any provider with IMAP (Gmail, GMX, laposte.net…). `.env` holds `IMAP_USER`, `IMAP_APP_PASSWORD` (redacted from logs like the Telegram token), `IMAP_FOLDER` (default INBOX) and `IMAP_HOST` (default imap.gmail.com). Spaces are stripped from the password only for Gmail.
  - **Reading:**
    - IMAP is **read-only** (`select(readonly=True)` + `BODY.PEEK[]`).
    - It reads only UIDs above `logs/email_alerts_state.json`, at most `MAX_EMAILS` = 30 per run. The first run reads the last 7 days.
    - `run_hunt` calls `batch.commit()` only once the offers are in the sheet or the queue.
  - **Parsing:**
    - The source is found by sender domain. Auto-forwarding keeps it; for a manual forward, the source is guessed from the link domains.
    - One offer per distinct offer link. The offer URL is decoded from tracking redirects when they embed it; otherwise the tracking link is kept, and dedup on title|company|city still works.
    - Title, company and location come from the link's "card" (its largest ancestor without another offer's link). Plain-text emails are parsed in blocks.
    - Newsletters are ignored: a subject that isn't alert-like and no recognisable offer URL means no offers.
    - Only data-role titles are kept, and alternance / CDI / CDD are dropped unless the title also says stage.
  - **Output:** the same fields as `clean()`, with `search_location` = "alerte email". The date is the email's date, and the card text becomes the `description`: too short for a reliable score, so the score is title-only, but pay and duration are read from it.
  - **Isolation:** each source is isolated. A parser error logs "source X failed on email uid N", and an alert email with 0 offers logs "layout changed?". An IMAP failure is logged and the run continues.
  - **Tests:** `.venv/Scripts/python tests/test_email_alerts.py` runs offline on `tests/fixtures/email_alerts/*.eml`, which are synthetic: WTTJ tracked links, HelloWork direct links + CTA buttons, JobTeaser auto-forward and manual plain-text forward, and a newsletter. Real alert emails go in `tests/fixtures/email_alerts/private/` (git-ignored: they carry the inbox address). The script also parses them and prints the results, so a layout change can be seen and the parser adjusted.
- **Auto date** (`apps_script/auto_date.gs`, installed by the user in Extensions > Apps Script): a simple `onEdit` trigger.
  - When the user sets Statut to an applied status on Offres and that row's Date candidature is empty, it writes today's date in the spreadsheet's time zone.
  - It finds both columns by header, and does nothing if one is missing or duplicated.
  - It never overwrites or clears a date.
  - No authorisation is needed: a simple trigger only touches its own spreadsheet. It doesn't fire on the robot's API writes.
  - Keep its `APPLIED` list in sync with `sheet_lib.APPLIED`.
- **VPN:** with a VPN on, `sheets.googleapis.com` times out at connect while auth still works. Runs now fail fast and queue (see above), so "Sheets unreachable, VPN suspected" in the log plus a non-empty `pending_sync.jsonl` is the expected state while the VPN is on.

## CV generation

`build_master_cv.py`, `build_cluster_cvs.py` (data analyst / quant-economist / data scientist) and `tailor_cv.py <job_url>` (CV + `_keywords.md` coverage report) write French 1-page `.docx` + `.pdf` files to `cvs/` (git-ignored). Shared logic is in `cv_lib.py`.

- **Scripts never write prose.** Every line comes from `cv_facts.fr.yaml`, the git-ignored fact bank the user approved (template: `cv_facts.example.yaml`). Variants and `aliases` are pre-approved synonyms, never broader claims. Scripts only select, order and label. New wording goes into the bank and must be reviewed by the user first.
- **English CVs:** `tailor_cv.py <job_url> --lang en` writes `..._en.docx` + `.pdf`. The texts come from `cv_facts.en.yaml` (git-ignored; template: `cv_facts.example.en.yaml`), a translation laid over the French bank by id (`cv_lib.translate`). It holds texts only: entries, order, skill tags, dates and tiers stay those of the French bank, and a missing or unknown id stops the build. Translations are faithful (bullets reuse the English wording of `profile.md`; degree titles keep the French name in parentheses), and offer wording goes in as a cluster variant only after the user approves it. Fixed labels (headings, "Tools:", "(advanced)") are in `cv_lib.LOCALES`; sections and categories keep their French names as internal keys.
- `check_honesty` rejects any paragraph not traceable to the bank or the tiers. A bank entry with a `check:` field blocks the build.
- **Skills lines** are built from `profile.md` tiers: categories from the bank, tier 1 before tier 2, tier 3 never shown. "(avancé)" appears only on tier-1 Programmation skills.
- **Header:** every CV shows the bank's `headline` (the user's own words) under the name. Without one, the current degree title is used.
- **Ordering:** Expérience professionnelle is always most recent first (bank `start`, YYYY-MM). Projets are ordered by relevance: cluster demand comes from `analyze_market.skill_table` (same numbers as the report), and Programmation/Outils skills count half (`TOOL_FACTOR`), so methods drive the order. Bullets keep the bank order within an entry.
- **Tailored CVs show a skills shortlist:** the list saved for the offer URL under `tailoring:` in the bank, otherwise the `MAX_SKILLS` most relevant to the offer. A saved list also sets the order within a tier. Master and cluster CVs show every tier-1/2 skill.
- **Design** lives only in `cv_lib._setup` / `render_docx` (tokens at the top of the file), so it applies to every CV: single column, one accent colour, no tables, text boxes or images. Section headings are uppercase by formatting only (the text stays "Formation"). The skills block is a tab plus hanging indent, not a table.
- **One page:** `fit_one_page` uses the largest body size in `BODY_SIZES` that fits, then drops the lowest-scoring bullets if even the smallest overflows. Always look at the PDF after a design change.
- **Page count** uses a private hidden Word instance (`DispatchEx`) on files in `cvs/.build/`. Never attach to the user's Word, and never use the system Temp folder (Protected View breaks the PDF export).

## Environment & commands

- Python 3.13 virtualenv at `.venv` (Windows layout). Dependencies are in `requirements.txt` (`python-jobspy` is pinned because `fetch_descriptions.py` uses a private API).
- Run: `.venv\Scripts\python.exe find_internships.py` (PowerShell) or `.venv/Scripts/python find_internships.py` (Git Bash).
- Reinstall deps if needed: `.venv\Scripts\python.exe -m pip install -r requirements.txt`
- No linter or build step. Four offline test scripts run with plain Python, no pytest: `tests/test_config.py` (config.yaml and the template have the same structure; the template works alone), `tests/test_layout.py` (columns by header: moved columns, the user's extra column, abort on a missing or duplicated header), `tests/test_theme.py` (contrast, no green) and `tests/test_email_alerts.py` (fixtures). A full run makes 2 scrape calls (one per site) per `search_plan()` entry per freshness window with a `PAUSE_SECONDS` sleep between each, so it takes minutes and hits live sites — for quick experiments, temporarily shrink `SEARCH_TERMS`/`LOCATIONS` or call `clean()` on a saved DataFrame instead.

## How the pipeline works

1. **Freshness windows** — `main()` loops over `WINDOWS_HOURS` (7 days, then 30 days). Each pass re-runs *all* searches with that `hours_old`; it stops at the first window yielding ≥ `MIN_OFFERS` cleaned results. The 30-day pass replaces (not merges with) the 7-day result.
2. **Search** — `run_searches()` iterates location × term × site, with France first. `build_query()` prefixes `"stage "` to any term lacking it so every query is internship-oriented. The original (un-prefixed) term is stored in a `search_term` column, which is later dropped by `KEEP_COLUMNS`. `search_location` is kept.
3. **Clean** — `clean()` applies two regex filters: `INTERN_PATTERN` on title+description (must be an internship) and `RELEVANCE_PATTERN` on title only (must look like a data role). Then dedupes by `job_url` and by `dedup_key()` = normalized `title|company|city` (catches the same offer on both sites; only the city is kept because the sites write regions differently), computes `age_days`, and sorts newest-first with undated rows last.
4. **Output** — CSV is written with `utf-8-sig` encoding deliberately so Excel renders French accents; keep it that way.

## Error handling quirk

`jobspy` usually does **not** raise on scraper failures (e.g. LinkedIn HTTP 429 rate-limits) — it logs them at ERROR level and returns an empty/partial frame. `_ErrorCollector` is a logging handler attached to the `jobspy` logger that captures these, tagged with the current query via `collector.context`, so they appear in the end-of-run error summary alongside actually-raised exceptions. Any new scraping call should set `collector.context` before calling `scrape_jobs` so failures stay attributable.

## Tuning knobs

All behavior is controlled by the module-level constants at the top of the script (`SEARCH_TERMS`, `NARROW_TERMS`, `LOCATIONS` / `NARROW_LOCATIONS` = {location: LinkedIn results}, `INDEED_RESULTS`, `WINDOWS_HOURS`, `MIN_OFFERS`, `PAUSE_SECONDS`, and the two regex patterns). Search terms and patterns are French/English bilingual on purpose — French postings use "stage"/"stagiaire" and accented words like "données"/"économétrie". Use the words companies actually write: "économètre" was dropped because Indeed returned nothing for it.
