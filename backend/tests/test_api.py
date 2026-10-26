from fastapi.testclient import TestClient

from aeronexus.api import app, bucket

client = TestClient(app)


H = {"x-user": "qa.approver", "x-role": "approver", "x-tenant": "demo"}


def gql(query, variables=None, headers=None):
    r = client.post("/graphql", json={"query": query, "variables": variables or {}}, headers=headers or H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "errors" not in body, body
    return body["data"]


def test_health_and_sheets():
    d = gql("{ health sheets(limit: 3) { id title tags } }")
    assert d["health"]["status"] == "ok" and len(d["sheets"]) == 3


def test_quote_then_approve_flow():
    from aeronexus.services import services

    sid = next(s.id for s in services().golden_sheets if not any(v for v in s.meta["difficulty"].values())
               and s.meta["truth"]["material"] in {"ASTM A36", "ASTM A1011", "ASTM A572 GR 50"})
    gql("mutation($id: String!) { quote(sheetId: $id) }", {"id": sid})
    pend = gql("{ approvals { id tool status } }")["approvals"]
    assert pend
    a = gql("mutation($id: String!) { decideApproval(id: $id, approve: true) { status decidedBy } }",
            {"id": pend[0]["id"]})["decideApproval"]
    assert a["status"] == "executed" and a["decidedBy"] == "qa.approver"


def test_viewer_cannot_run_or_approve():
    r = client.post("/graphql", json={"query": "mutation { openChangeOrder(pairId: \"CH000\") }"},
                    headers={"x-role": "viewer", "x-user": "v"})
    assert "errors" in r.json() and "cannot" in r.json()["errors"][0]["message"]


def test_change_order_flow_over_graphql_with_replay():
    out = gql("mutation { openChangeOrder(pairId: \"CH000\") }")["openChangeOrder"]
    done = gql("mutation($id: String!) { decideCase(id: $id, approve: true, note: \"ok\") }", {"id": out["id"]})["decideCase"]
    assert done["status"] == "executed"
    rep = gql("query($id: String!) { replayCase(id: $id) }", {"id": out["id"]})["replayCase"]
    assert rep["identical"] and rep["receipts_reproduced"]
    assert any(c["id"] == out["id"] for c in gql("{ cases }")["cases"])


def test_chat_returns_grounded_answer_with_instances():
    d = gql("mutation { chat(question: \"Why does thickness fail?\") { answer decision groundedness instances sources chart } }")["chat"]
    assert d["decision"] in {"pass", "pruned"} and d["groundedness"] >= 0.7
    assert d["sources"] and d["chart"]["type"] == "bar"


def test_research_and_architecture():
    d = gql("{ research architecture }")
    assert all(c["verified"] for c in d["research"])
    assert len(d["architecture"]["layers"]) == 8


def test_metrics_endpoint_prometheus_format():
    client.post("/graphql", json={"query": "{ health }"})
    txt = client.get("/metrics").text
    assert 'aeronexus_counter{name="http.requests"}' in txt


def test_rate_limit_returns_429():
    old = (bucket.rate, bucket.burst)
    bucket.rate, bucket.burst, bucket.state = 0.0, 2, {}
    try:
        codes = [client.post("/graphql", json={"query": "{ health }"}).status_code for _ in range(4)]
        assert codes.count(429) >= 1
    finally:
        bucket.rate, bucket.burst, bucket.state = old[0], old[1], {}


def test_unknown_field_is_rejected_cleanly():
    r = client.post("/graphql", json={"query": "{ dropTables }"})
    assert r.json()["errors"]
