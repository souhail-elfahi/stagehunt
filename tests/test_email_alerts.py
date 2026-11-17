"""Offline tests for email_alerts.py on saved alert emails (fixtures). No network.

    .venv/Scripts/python tests/test_email_alerts.py

Fixtures: tests/fixtures/email_alerts/*.eml are synthetic (no personal data). Real alert
emails go in tests/fixtures/email_alerts/private/ (git-ignored: they carry the inbox address);
they are parsed too when present, with their results printed for a manual check.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import email_alerts as ea  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "email_alerts"


def parsed(name: str):
    src, offers, subject = ea.parse_message((FIX / name).read_bytes())
    return src.name if src else None, [o for o in offers if ea.keep(o)], offers, subject


def show(offers):
    for o in offers:
        print(f"    {o['title'][:45]:<45} | {o['company'][:22]:<22} | {o['location'][:22]:<22} | {o['job_url'][:70]}")


def test_fixtures():
    src, kept, raw, _ = parsed("wttj_alert.eml")
    print(f"wttj_alert: source={src}, {len(raw)} parsed, {len(kept)} kept"); show(kept)
    assert src == "wttj" and [o["company"] for o in kept] == ["Decathlon", "Doctolib", "Ipsos"]
    assert kept[0]["job_url"] == ("https://www.welcometothejungle.com/fr/companies/decathlon/jobs/"
                                  "stage-data-analyst-f-h_villeneuve-d-ascq")       # decoded, no tracking
    assert kept[1]["job_url"].endswith("/doctolib/jobs/stagiaire-data-scientist_paris")  # double-encoded
    assert kept[2]["job_url"].startswith("https://u4815.ct.sendgrid.net/")           # opaque: kept as is
    assert [o["location"] for o in kept] == ["Villeneuve-d'Ascq", "Paris", "Lille"]
    assert all(o["date_posted"] == "2026-11-17" for o in kept)

    src, kept, raw, _ = parsed("hellowork_alert.eml")
    print(f"hellowork_alert: source={src}, {len(raw)} parsed, {len(kept)} kept"); show(kept)
    assert src == "hellowork" and len(raw) == 3 and len(kept) == 2       # the alternance is dropped
    assert kept[0]["job_url"] == "https://www.hellowork.com/fr-fr/emplois/61234567.html"
    assert (kept[0]["company"], kept[0]["location"]) == ("Leroy Merlin", "Lezennes - 59")

    src, kept, raw, _ = parsed("jobteaser_autoforward.eml")
    print(f"jobteaser_autoforward: source={src}, {len(kept)} kept"); show(kept)
    assert src == "jobteaser" and [o["company"] for o in kept] == ["Banque de France", "Insee"]

    src, kept, raw, _ = parsed("jobteaser_manual_forward.eml")
    print(f"jobteaser_manual_forward (plain text, sender = the user): source={src}, "
          f"{len(raw)} parsed, {len(kept)} kept"); show(kept)
    assert src == "jobteaser" and len(raw) == 2 and len(kept) == 1          # business developer dropped
    assert kept[0]["company"] == "Société Générale" and kept[0]["location"] == "Paris, France"

    src, kept, raw, _ = parsed("wttj_newsletter.eml")
    print(f"wttj_newsletter: source={src}, {len(raw)} offers (articles must not count)")
    assert raw == []


def test_scores_from_cards():
    from fit_score import fit
    _, kept, _, _ = parsed("wttj_alert.eml")
    f = fit({**kept[1], "age_days": 0})
    print(f"Doctolib card -> pay {f.pay}, reliable {f.reliable}, role {f.role}")
    assert f.pay == 1400 and not f.reliable                  # pay read from the card, title-only score
    _, kept, _, _ = parsed("hellowork_alert.eml")
    short = fit(kept[1])
    print(f"HelloWork statisticien card -> duration {short.duration!r}, short {short.short}")
    assert short.duration == "2 mois" and short.short


class FakeIMAP:
    """Serves the fixtures as a mailbox; records the commands that matter."""
    messages: dict[int, bytes] = {}
    log: list = []

    def __init__(self, host, timeout=None):
        FakeIMAP.log.append(("connect", host, timeout))

    def login(self, user, password):
        FakeIMAP.log.append(("login", user))

    def select(self, folder, readonly=False):
        FakeIMAP.log.append(("select", folder, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [b"777"]

    def uid(self, command, *args):
        FakeIMAP.log.append(("uid", command, args[-1]))
        if command == "SEARCH":
            spec = args[-1]
            start = int(spec.split()[1].split(":")[0]) if spec.startswith("UID") else 1
            uids = [u for u in sorted(self.messages) if u >= start] or [max(self.messages)]  # Gmail quirk
            return "OK", [" ".join(map(str, uids)).encode()]
        return "OK", [(f"{args[0]} (BODY[] {{1}}".encode(), self.messages[int(args[0])]), b")"]

    def logout(self):
        FakeIMAP.log.append(("logout",))


def test_collect():
    names = ["wttj_alert.eml", "hellowork_alert.eml", "jobteaser_autoforward.eml",
             "jobteaser_manual_forward.eml", "wttj_newsletter.eml"]
    FakeIMAP.messages = {10 + i: (FIX / n).read_bytes() for i, n in enumerate(names)}
    os.environ.update(IMAP_USER="job-alerts@example.com", IMAP_APP_PASSWORD="abcd efgh ijkl mnop")
    state = Path(tempfile.mkdtemp()) / "state.json"

    FakeIMAP.log = []
    batch = ea.collect(max_emails=3, state_file=state, imap_factory=FakeIMAP)
    assert ("select", "INBOX", True) in FakeIMAP.log, "the mailbox must be opened read-only"
    fetches = [x for x in FakeIMAP.log if x[:2] == ("uid", "FETCH")]
    assert all(x[2] == "(BODY.PEEK[])" for x in fetches), "PEEK only: never mark as read"
    print(f"collect #1: {batch.emails} emails, offers per source {batch.per_source}, errors {batch.errors}")
    assert batch.emails == 3 and batch.per_source == {"wttj": 3, "hellowork": 2, "jobteaser": 2}
    assert not state.exists(), "state only advances on commit"
    batch.commit()

    batch = ea.collect(max_emails=3, state_file=state, imap_factory=FakeIMAP)
    print(f"collect #2: {batch.emails} emails, offers per source {batch.per_source}")
    assert batch.emails == 2 and batch.per_source == {"jobteaser": 1, "wttj": 0}   # newsletter: read, 0 offers
    batch.commit()
    batch = ea.collect(max_emails=3, state_file=state, imap_factory=FakeIMAP)
    assert batch.emails == 0 and batch.offers.empty, "Gmail returns the last UID for an empty range"

    # Isolation: a broken HelloWork parser must not stop the other sources.
    state.unlink()
    real = ea.parse_html
    ea.parse_html = lambda html, src: (_ for _ in ()).throw(ValueError("layout changed")) \
        if src.name == "hellowork" else real(html, src)
    try:
        batch = ea.collect(max_emails=10, state_file=state, imap_factory=FakeIMAP)
    finally:
        ea.parse_html = real
    print(f"isolation: per source {batch.per_source}, errors {batch.errors}")
    assert "hellowork" not in batch.per_source and batch.per_source["wttj"] == 3
    assert any("source hellowork failed" in e for e in batch.errors)
    assert set(batch.offers.columns) >= {"title", "company", "location", "date_posted", "job_url", "site",
                                         "search_location", "description", "age_days"}

    os.environ.pop("IMAP_USER")
    assert not ea.collect(state_file=state, imap_factory=FakeIMAP).configured


def test_private_fixtures():
    private = sorted((FIX / "private").glob("*.eml")) if (FIX / "private").exists() else []
    for path in private:
        src, offers, subject = ea.parse_message(path.read_bytes())
        kept = [o for o in offers if ea.keep(o)]
        print(f"[private] {path.name}: source={src.name if src else None}, subject={subject[:50]!r}, "
              f"{len(offers)} parsed, {len(kept)} kept"); show(kept)


if __name__ == "__main__":
    test_fixtures()
    test_scores_from_cards()
    test_collect()
    test_private_fixtures()
    print("ALL OK")
