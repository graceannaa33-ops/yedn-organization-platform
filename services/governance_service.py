"""Meetings, voting, support tickets and complaints."""
from extensions import db
from helpers import new_reference, utcnow
from models import CaseResponse, Complaint, Member, SupportTicket, Vote, VoteBallot
from services import audit_service, contribution_service, notification_service


class GovernanceError(Exception):
    pass


# ---------------------------------------------------------------- voting
def member_eligible(member, vote):
    if member.status != "active":
        return False
    if vote.eligibility == "fully_paid":
        s = contribution_service.member_summary(member)
        return s["required"] > 0 and s["paid"] >= s["required"]
    return True


def eligible_members(vote):
    return [m for m in Member.query.filter_by(status="active").all() if member_eligible(m, vote)]


def vote_is_open(vote):
    now = utcnow()
    return vote.status == "Open" and vote.opens_at <= now <= vote.closes_at


def cast_ballot(vote, member, choice):
    if choice not in ("yes", "no", "abstain"):
        raise GovernanceError("Choose Yes, No or Abstain.")
    if not vote_is_open(vote):
        raise GovernanceError("Voting is not open for this proposal.")
    if not member_eligible(member, vote):
        raise GovernanceError("You are not eligible to vote on this proposal.")
    if VoteBallot.query.filter_by(vote_id=vote.id, member_id=member.id).first():
        raise GovernanceError("You have already voted. Votes cannot be changed.")
    db.session.add(VoteBallot(vote_id=vote.id, member_id=member.id, choice=choice))
    # The choice is not written to the audit log, to keep the ballot secret.
    audit_service.log("vote.ballot_cast", "vote", vote.id, user=member.user)


def tally(vote):
    counts = {"yes": 0, "no": 0, "abstain": 0}
    for b in vote.ballots:
        counts[b.choice] += 1
    return counts


def close_vote(vote, user):
    if vote.status != "Open":
        raise GovernanceError("This vote is already closed.")
    counts = tally(vote)
    eligible = len(eligible_members(vote))
    turnout = sum(counts.values())
    vote.yes_count, vote.no_count, vote.abstain_count = counts["yes"], counts["no"], counts["abstain"]
    vote.eligible_count = eligible
    if eligible == 0 or turnout * 100 < vote.quorum_percent * eligible:
        vote.result = "No quorum"
    elif counts["yes"] + counts["no"] and counts["yes"] * 100 > vote.pass_percent * (counts["yes"] + counts["no"]):
        vote.result = "Passed"
    else:
        vote.result = "Rejected"
    vote.status, vote.closed_at, vote.closed_by_id = "Closed", utcnow(), user.id
    audit_service.log("vote.closed", "vote", vote.id, new={**counts, "eligible": eligible, "result": vote.result},
                      user=user)


# ---------------------------------------------------------------- tickets & complaints
def open_ticket(user, subject, category, description):
    t = SupportTicket(ticket_number=new_reference("TKT"), user_id=user.id, subject=subject[:200],
                      category=category[:60], description=description)
    db.session.add(t)
    db.session.flush()
    audit_service.log("support.ticket_opened", "support_ticket", t.ticket_number, user=user)
    return t


def open_complaint(name, email, phone, subject, category, description, user=None):
    c = Complaint(complaint_number=new_reference("CMP"), user_id=user.id if user else None, name=name[:150],
                  email=email[:254], phone=(phone or "")[:20], subject=subject[:200], category=category[:60],
                  description=description)
    db.session.add(c)
    db.session.flush()
    audit_service.log("complaint.opened", "complaint", c.complaint_number, user=user)
    return c


def respond(case, author, body, internal=False, status=None, resolution=None):
    from models import CASE_STATUSES
    if not (body or "").strip() and not status:
        raise GovernanceError("Write a response or change the status.")
    if body and body.strip():
        db.session.add(CaseResponse(ticket_id=case.id if isinstance(case, SupportTicket) else None,
                                    complaint_id=case.id if isinstance(case, Complaint) else None,
                                    author_id=author.id if author else None, body=body.strip(),
                                    is_internal=internal))
    old = case.status
    if status and status in CASE_STATUSES and status != case.status:
        case.status = status
    if resolution is not None and resolution.strip():
        case.resolution = resolution.strip()
    case.updated_at = utcnow()
    number = getattr(case, "ticket_number", None) or case.complaint_number
    audit_service.log("case.updated", type(case).__name__, number, old={"status": old},
                      new={"status": case.status}, user=author)
    if isinstance(case, SupportTicket) and author and author.id != case.user_id and not internal:
        notification_service.notify(case.user, f"Update on ticket {case.ticket_number}",
                                    f"Status: {case.status}. {body.strip()[:300] if body else ''}", "support")
