"""YEDN identity: branding everywhere, honest statistics, programs and opportunities, legal safety."""
import os
import re
from datetime import date, timedelta

import pytest

from conftest import login

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FULL = "Youth Enterprise &amp; Development Network"
TAGLINE = "Empowering Youth. Building Enterprises. Creating Opportunities."

PUBLIC_PAGES = ["/", "/about", "/what-we-do", "/how-it-works", "/programs", "/projects", "/opportunities",
                "/membership", "/member-rights", "/governance", "/organization-structure", "/project-funding",
                "/partners", "/transparency", "/reports", "/faq", "/contact", "/complaints", "/login", "/register",
                "/legal/terms", "/legal/privacy", "/legal/risk-disclosure", "/legal/contribution-rules",
                "/legal/distribution-rules", "/legal/refund-policy", "/legal/complaints-policy"]


def test_every_public_page_is_branded(client):
    for url in PUBLIC_PAGES:
        r = client.get(url)
        assert r.status_code == 200, url
        body = r.get_data(as_text=True)
        assert "YEDN" in body, url
        assert TAGLINE in body, url              # footer tagline on every page
        assert "<title>" in body and ("YEDN" in body.split("<title>")[1].split("</title>")[0]), url


def test_homepage_hero_and_buttons(client):
    body = client.get("/").get_data(as_text=True)
    assert f"<h1>{TAGLINE}</h1>" in body
    assert "connects young people to enterprise development" in body
    for label in ("Join YEDN", "Explore Opportunities", "View Transparency"):
        assert label in body
    assert "Transparency &amp; Accountability" in body
    assert "does not guarantee financial returns" in body


def test_homepage_statistics_come_from_the_database(client, factory):
    body = client.get("/").get_data(as_text=True)
    stats = dict(re.findall(r'<div class="label">([^<]+)</div>\s*<div class="value">([^<]+)</div>', body))
    assert stats["Registered members"] == "0" and stats["Active projects"] == "0"
    assert stats["Total verified contributions"] == "KSh 0"
    m = factory.member()
    factory.pay(m, 250000)
    factory.member()
    body = client.get("/").get_data(as_text=True)
    stats = dict(re.findall(r'<div class="label">([^<]+)</div>\s*<div class="value">([^<]+)</div>', body))
    assert stats["Registered members"] == "2" and stats["Total verified contributions"] == "KSh 2,500"


def test_formal_documents_use_full_name_and_configured_contacts(app, client, factory):
    from services import report_service
    from services.settings_service import set_setting
    from extensions import db
    set_setting("contact_email", "info@yedn.example")
    db.session.commit()
    m = factory.member()
    factory.pay(m, 100000)
    rep = report_service.member_statement(m)
    assert rep["organization"] == "Youth Enterprise & Development Network (YEDN)"
    assert "info@yedn.example" in rep["contact"]
    csv = report_service.to_csv(rep)
    assert csv.startswith("Youth Enterprise & Development Network (YEDN)")
    assert report_service.to_pdf(rep)[:4] == b"%PDF"
    login(client, m.email)
    ref = m.payments[0].reference
    assert FULL in client.get(f"/payments/{ref}/receipt").get_data(as_text=True)


def test_notifications_are_branded(app, factory):
    from models import Notification
    m = factory.member()
    factory.pay(m, 100000)
    note = Notification.query.filter_by(user_id=m.user_id, kind="payment").first()
    assert note.body.startswith("YEDN: Payment received: KSh 1,000")


def test_programs_are_never_invented(client, factory):
    from models import Program
    body = client.get("/programs").get_data(as_text=True)
    assert "No programs have been published yet" in body
    pm = factory.staff("project_manager")
    login(client, pm.email)
    client.post("/admin/programs", data={"action": "add_drafts"})
    assert Program.query.count() == 12 and Program.query.filter_by(is_published=True).count() == 0
    assert "No programs have been published yet" in client.get("/programs").get_data(as_text=True)
    p = Program.query.filter_by(title="Mentorship").one()
    client.post(f"/admin/programs/{p.id}", data={"title": "Mentorship", "description": "Monthly mentor circles.",
                                                 "status": "Open for applications", "eligibility": "Members",
                                                 "application_info": "Apply at the office", "is_published": "on"})
    body = client.get("/programs").get_data(as_text=True)
    assert "Monthly mentor circles." in body and "Open for applications" in body
    assert client.get(f"/programs/{p.id}").status_code == 200
    other = Program.query.filter_by(title="Leadership Development").one()
    assert client.get(f"/programs/{other.id}").status_code == 404     # drafts stay hidden


def test_opportunities_publication_and_members_only(client, factory):
    from models import Opportunity
    pm = factory.staff("project_manager")
    m = factory.member()
    login(client, pm.email)
    base = {"opportunity_type": "Internship", "description": "Three-month paid internship.", "is_published": "on",
            "link": "javascript:alert(1)"}
    client.post("/admin/opportunities", data=dict(base, title="Public internship",
                                                  deadline=(date.today() + timedelta(days=10)).isoformat()))
    client.post("/admin/opportunities", data=dict(base, title="Members internship", members_only="on"))
    client.post("/admin/opportunities", data=dict(base, title="Old grant", opportunity_type="Grant",
                                                  deadline=(date.today() - timedelta(days=1)).isoformat()))
    assert Opportunity.query.filter_by(title="Public internship").one().link == ""   # unsafe links dropped
    client.post("/logout")
    body = client.get("/opportunities").get_data(as_text=True)
    assert "Public internship" in body and "Members internship" not in body and "Closed opportunities" in body
    login(client, m.email)
    assert "Members internship" in client.get("/opportunities").get_data(as_text=True)
    assert client.get("/admin/opportunities").status_code == 403


def test_identity_settings_editable(client, factory):
    from services.settings_service import get_setting
    admin = factory.staff("super_admin")
    login(client, admin.email)
    data = {"section": "identity", "organization_name": "Youth Enterprise & Development Network",
            "organization_short_name": "YEDN", "organization_tagline": TAGLINE,
            "organization_supporting_message": "x", "organization_website": "https://yedn.example",
            "organization_registration": "", "university_relationship": "", "organization_description": "",
            "vision_text": "", "mission_text": "", "founding_text": "", "leadership_text": "Chair: to be announced",
            "founding_team_text": ""}
    client.post("/admin/settings", data=data)
    assert get_setting("organization_website") == "https://yedn.example"
    assert "Chair: to be announced" in client.get("/organization-structure").get_data(as_text=True)
    assert "To build a generation" in client.get("/about").get_data(as_text=True)   # empty field -> default kept


UNSUPPORTED = [r"\bguaranteed returns? (?:of|are|is)\b", r"\blicensed by\b", r"\bregulated by\b",
               r"\bapproved by the (?:government|central bank|cma)\b", r"\bendorsed by\b", r"\bregistered sacco\b",
               r"\btax[- ]exempt\b", r"\bis a registered (?:ngo|sacco|charity)\b",
               r"\b(?:YEDN|\{short\}) is a (?:bank|sacco|licensed)\b"]


def test_no_unsupported_claims_or_old_names_in_source():
    hits = []
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ("venv", ".venv", "__pycache__", ".pytest_cache", "uploads",
                                                "instance", "tests", "migrations")]
        for f in files:
            if not f.endswith((".py", ".html", ".md", ".example", ".json")):
                continue
            text = open(os.path.join(folder, f), encoding="utf-8").read()
            for old in ("Umoja", "Community Investment Group", "Pooling our contributions"):
                if old in text:
                    hits.append((f, old))
            for pattern in UNSUPPORTED:
                if re.search(pattern, text, re.I):
                    hits.append((f, pattern))
    assert not hits, hits


def test_staff_dashboards_branded(client, factory):
    org, partner_user = factory.partner()
    for user, url in ((factory.staff("super_admin"), "/admin/"), (partner_user, "/partner/"),
                      (factory.member(), "/member/")):
        login(client, user.email)
        body = client.get(url).get_data(as_text=True)
        assert "YEDN" in body, url
        client.post("/logout")
