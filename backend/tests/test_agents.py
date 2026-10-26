import pytest

from aeronexus.agents import AI_DISCLOSURE, ToolValidationError
from aeronexus.services import services


def rt():
    return services().runtime


def test_read_tools_execute_immediately():
    out = rt().call("erp.find_item", {"material": "ASTM A36", "thickness": "6 mm"})
    assert out["status"] == "executed" and out["result"][0]["code"] == "ASTMA36-6MM"


def test_write_tools_require_named_human_approval():
    out = rt().call("erp.create_quote", {"customer": "ACC-001", "part_number": "P1", "amount": 100.0, "lines": []})
    assert out["status"] == "pending_approval"
    assert not rt().erp.quotes  # nothing written yet
    with pytest.raises(PermissionError):
        rt().decide(out["approval_id"], approver="agent", approve=True)
    item = rt().decide(out["approval_id"], approver="m.madaan", approve=True)
    assert item.status == "executed" and len(rt().erp.quotes) == 1


def test_idempotency_no_duplicate_quotes():
    args = {"customer": "ACC-001", "part_number": "P2", "amount": 50.0, "lines": []}
    a = rt().call("erp.create_quote", args)
    b = rt().call("erp.create_quote", args)
    assert b["status"] == "duplicate" and b["approval_id"] == a["approval_id"]
    rt().decide(a["approval_id"], "reviewer", True)
    rt().decide(a["approval_id"], "reviewer", True)  # second click
    assert len(rt().erp.quotes) == 1


def test_rejection_executes_nothing_and_is_audited():
    a = rt().call("comms.send_email", {"to": "x@example.com", "subject": "s", "body": "b"})
    rt().decide(a["approval_id"], "reviewer", False)
    assert not rt().outbox
    assert [e.action for e in rt().audit][-1] == "rejected"


def test_outbound_messages_carry_ai_disclosure():
    a = rt().call("comms.send_email", {"to": "x@example.com", "subject": "s", "body": "hello"})
    rt().decide(a["approval_id"], "reviewer", True)
    assert AI_DISCLOSURE in rt().outbox[-1]["body"]
    c = rt().call("comms.place_call", {"to": "+14035550100", "script": "Confirm coating spec."})
    rt().decide(c["approval_id"], "reviewer", True)
    assert "automated assistant" in rt().outbox[-1]["script"]


@pytest.mark.parametrize("args", [{"material": 5}, {"material": "x", "evil": "rm -rf"}, {}])
def test_schema_validation(args):
    with pytest.raises(ToolValidationError):
        rt().call("erp.find_item", args)


def test_unknown_tool():
    with pytest.raises(ToolValidationError):
        rt().call("bank.transfer", {})
