"""终点任务撤销（POST /deliveries/{id}/revocation、GET /deliveries/{id}/revocation）：

一次操作在事务内核定该编号全部终点：已送达 / 永久失败的终点保留原回执
（kept_terminal），其余终点标记撤销（revoked）并使现有租约失效；全部终点已终结
返回 409 nothing_to_revoke 且不留记录。同编号再次撤销原样返回首次操作的逐终点
结果、时间与原因。撤销后领取不再返回已撤销终点，直接回执的新尝试与令牌完成
明确拒绝，已记录回执的同序号同结果重试仍原样返回。未知编号（404）、原判定失败
（409）、空原因（422）明确报错且不留记录。判定重放、历史报表与熔断统计不变，
回执查询仍只反映实际尝试。
"""
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

# 必须在导入 app 之前设置，config 在导入时设置
os.environ.setdefault("ROUTER_DB", os.path.join(tempfile.mkdtemp(prefix="router-test-"), "test.db"))

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture()
def client():
    db.init_db()
    conn = db.connect()
    conn.executescript(
        "DELETE FROM delivery_endpoint_revocations;"
        "DELETE FROM delivery_revocations;"
        "DELETE FROM endpoint_leases;"
        "DELETE FROM delivery_attempts;"
        "DELETE FROM draft_rules;"
        "DELETE FROM delivery_policy;"
        "DELETE FROM delivery_decisions;"
        "DELETE FROM circuit_breaker_settings;"
        "DELETE FROM circuit_breaks;"
        "DELETE FROM version_rules;"
        "DELETE FROM versions;"
        "DELETE FROM sqlite_sequence WHERE name = 'versions';"
        "DELETE FROM draft_revisions;"
        "INSERT INTO draft_revisions (revision, rules, rule_count, created_at)"
        " VALUES (0, '[]', 0, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'));"
        "UPDATE draft_meta SET revision = 0 WHERE id = 1;"
    )
    conn.close()
    with TestClient(app) as c:
        yield c


RULES = [
    {
        "match_type": "exact",
        "match_key": "multi@corp.example.com",
        "action": "route",
        "targets": [
            {"kind": "mailbox", "value": "alice@mail.example.com"},
            {"kind": "mailbox", "value": "bob@mail.example.com"},
        ],
    },
    {
        "match_type": "exact",
        "match_key": "single@corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "carol@mail.example.com"}],
    },
    {
        "match_type": "exact",
        "match_key": "blocked@corp.example.com",
        "action": "reject",
        "targets": [],
    },
]


def setup(client):
    assert client.put("/draft", json={"revision": 0, "rules": RULES}).status_code == 200
    assert client.post("/publish", json={"revision": 1}).status_code == 200
    assert client.put("/policy", json={
        "revision": 0, "baseline_version": 1, "candidate_version": 1, "candidate_percent": 100,
    }).status_code == 200


def submit(client, delivery_id, address="multi@corp.example.com"):
    resp = client.post("/deliveries", json={"delivery_id": delivery_id, "address": address})
    assert resp.status_code == 200, resp.text
    return resp.json()


def revoke(client, delivery_id, reason):
    return client.post(f"/deliveries/{delivery_id}/revocation", json={"reason": reason})


def receipt(client, delivery_id, mailbox, attempt_no, result):
    return client.post(
        f"/deliveries/{delivery_id}/receipts",
        json={"mailbox": mailbox, "attempt_no": attempt_no, "result": result},
    )


def claim(client, max_claims=100, lease_seconds=300):
    return client.post("/deliveries/claims", json={
        "max_claims": max_claims, "lease_seconds": lease_seconds,
    })


def complete(client, token, result):
    return client.post(f"/deliveries/claims/{token}/complete", json={"result": result})


def tasks_by(client, key):
    return {t[key]: t for t in claim(client).json()["tasks"]}


# ---------- 撤销操作：逐终点结果与记录 ----------

def test_revoke_marks_all_non_terminal_endpoints(client):
    setup(client)
    submit(client, "msg-1")
    resp = revoke(client, "msg-1", "  user requested stop  ")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["delivery_id"] == "msg-1"
    assert body["reason"] == "user requested stop"  # 原因去空白
    assert body["revoked_at"]
    assert body["revoked_count"] == 2
    assert body["endpoints"] == [
        {"mailbox": "alice@mail.example.com", "outcome": "revoked"},
        {"mailbox": "bob@mail.example.com", "outcome": "revoked"},
    ]


def test_revoke_keeps_terminal_endpoints_and_their_receipts(client):
    setup(client)
    submit(client, "msg-1")
    assert receipt(client, "msg-1", "alice@mail.example.com", 1, "delivered").status_code == 200
    assert receipt(client, "msg-1", "bob@mail.example.com", 1, "temporary_failure").status_code == 200
    body = revoke(client, "msg-1", "stop").json()
    # 已送达终点保留原回执；暂时失败不是终结，仍被撤销
    assert body["revoked_count"] == 1
    assert body["endpoints"] == [
        {"mailbox": "alice@mail.example.com", "outcome": "kept_terminal",
         "terminal_result": "delivered", "terminal_attempt_no": 1},
        {"mailbox": "bob@mail.example.com", "outcome": "revoked"},
    ]
    summary = client.get("/deliveries/msg-1/receipts").json()
    alice = next(e for e in summary["endpoints"] if e["mailbox"] == "alice@mail.example.com")
    assert alice["status"] == "delivered" and len(alice["attempts"]) == 1


def test_permanent_failure_endpoint_is_kept(client):
    setup(client)
    submit(client, "msg-1")
    assert receipt(client, "msg-1", "bob@mail.example.com", 2, "permanent_failure").status_code == 200
    body = revoke(client, "msg-1", "stop").json()
    assert body["endpoints"] == [
        {"mailbox": "alice@mail.example.com", "outcome": "revoked"},
        {"mailbox": "bob@mail.example.com", "outcome": "kept_terminal",
         "terminal_result": "permanent_failure", "terminal_attempt_no": 2},
    ]
    assert body["revoked_count"] == 1


def test_all_terminal_returns_nothing_to_revoke_and_leaves_no_record(client):
    setup(client)
    submit(client, "msg-1")
    assert receipt(client, "msg-1", "alice@mail.example.com", 1, "delivered").status_code == 200
    assert receipt(client, "msg-1", "bob@mail.example.com", 1, "permanent_failure").status_code == 200
    resp = revoke(client, "msg-1", "too late")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "nothing_to_revoke"
    assert detail["delivery_id"] == "msg-1"
    assert detail["endpoints"] == [
        {"mailbox": "alice@mail.example.com", "terminal_result": "delivered", "terminal_attempt_no": 1},
        {"mailbox": "bob@mail.example.com", "terminal_result": "permanent_failure", "terminal_attempt_no": 1},
    ]
    # 不留撤销记录；再次撤销仍是 nothing_to_revoke
    got = client.get("/deliveries/msg-1/revocation")
    assert got.status_code == 404 and got.json()["detail"]["error"] == "revocation_not_found"
    assert revoke(client, "msg-1", "again").status_code == 409


def test_revoke_replay_returns_first_operation_verbatim(client):
    setup(client)
    submit(client, "msg-1")
    first = revoke(client, "msg-1", "first reason").json()
    again = revoke(client, "msg-1", "a different later reason")
    assert again.status_code == 200
    assert again.json() == first  # 首次操作的逐终点结果、时间与原因
    assert client.get("/deliveries/msg-1/revocation").json() == first


@pytest.mark.parametrize("replay_reason", ["", "   ", "\t\n ", "  different reason  "])
def test_revoke_replay_with_blank_or_changed_reason_returns_first_record(client, replay_reason):
    # 已成功撤销后再次提交：即使本次原因为空 / 纯空白 / 与首次不同，也原样返回首次
    # 记录（原因、时间、逐终点结果与计数），不再次校验或改写原因
    setup(client)
    submit(client, "msg-1")
    first = revoke(client, "msg-1", "  first reason  ").json()
    assert first["reason"] == "first reason"
    replay = revoke(client, "msg-1", replay_reason)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first
    assert client.get("/deliveries/msg-1/revocation").json() == first
    # 存储中的原因与计数仍以首次为准，未被改写
    stored = client.get("/deliveries/msg-1/revocation").json()
    assert stored["reason"] == "first reason" and stored["revoked_count"] == 2


def test_revoke_replay_with_oversized_reason_is_not_revalidated(client):
    # 模型层不再按长度校验：首次记录存在时，超长原因的重放仍原样返回
    setup(client)
    submit(client, "msg-1")
    first = revoke(client, "msg-1", "short").json()
    replay = revoke(client, "msg-1", "x" * 5000)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first


def test_concurrent_revokes_converge_to_first_record(client):
    setup(client)
    submit(client, "msg-1")
    bodies = []

    def worker(i):
        resp = revoke(client, "msg-1", f"reason-{i}")
        assert resp.status_code == 200, resp.text
        bodies.append(resp.json())

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(worker, range(8)))
    assert len(bodies) == 8
    assert all(b == bodies[0] for b in bodies)  # 并发撤销收敛为同一份首次记录
    assert client.get("/deliveries/msg-1/revocation").json() == bodies[0]


# ---------- 明确报错且不留记录 ----------

def test_revoke_unknown_delivery_404(client):
    setup(client)
    resp = revoke(client, "nope", "reason")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "delivery_not_found"
    got = client.get("/deliveries/nope/revocation")
    assert got.status_code == 404 and got.json()["detail"]["error"] == "delivery_not_found"


def test_revoke_failed_decision_409(client):
    setup(client)
    resp = client.post("/deliveries", json={"delivery_id": "msg-bad", "address": "blocked@corp.example.com"})
    assert resp.status_code == 422  # 失败判定同样持久化
    resp = revoke(client, "msg-bad", "reason")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "delivery_decision_failed"
    assert detail["decision_status"] == "failed" and detail["error_type"] == "rejected"
    got = client.get("/deliveries/msg-bad/revocation")
    assert got.status_code == 404 and got.json()["detail"]["error"] == "revocation_not_found"


@pytest.mark.parametrize("reason", ["", "   ", "\t\n "])
def test_revoke_blank_reason_422_and_no_record(client, reason):
    setup(client)
    submit(client, "msg-1")
    resp = revoke(client, "msg-1", reason)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_reason"
    assert client.get("/deliveries/msg-1/revocation").status_code == 404
    # 修正后可用同一编号正常撤销
    assert revoke(client, "msg-1", "now a real reason").status_code == 200


@pytest.mark.parametrize("reason", ["", "   ", "\t\n "])
def test_blank_reason_on_unknown_and_failed_and_terminal_deliveries_still_rejected(client, reason):
    # 尚无撤销记录时先去空白：空原因对未知编号 / 失败判定 / 全部终结编号同样 422，
    # 且不改变这些编号既有的无记录状态（后续非空原因仍按原约束处理）
    setup(client)
    submit(client, "msg-1")
    bad_resp = client.post(
        "/deliveries", json={"delivery_id": "msg-bad", "address": "blocked@corp.example.com"}
    )
    assert bad_resp.status_code == 422  # 失败判定同样持久化
    assert receipt(client, "msg-1", "alice@mail.example.com", 1, "delivered").status_code == 200
    assert receipt(client, "msg-1", "bob@mail.example.com", 1, "permanent_failure").status_code == 200

    assert revoke(client, "nope", reason).status_code == 422
    assert revoke(client, "msg-bad", reason).status_code == 422
    assert revoke(client, "msg-1", reason).status_code == 422

    # 非空原因后，既有未知编号（404）、失败判定（409）、全终结（409）约束保持不变
    assert revoke(client, "nope", "real reason").status_code == 404
    bad = revoke(client, "msg-bad", "real reason")
    assert bad.status_code == 409 and bad.json()["detail"]["error"] == "delivery_decision_failed"
    terminal = revoke(client, "msg-1", "real reason")
    assert terminal.status_code == 409 and terminal.json()["detail"]["error"] == "nothing_to_revoke"
    # 全程未留任何撤销记录
    for delivery_id in ("nope", "msg-bad", "msg-1"):
        got = client.get(f"/deliveries/{delivery_id}/revocation")
        assert got.status_code == 404 and got.json()["detail"]["error"] in (
            "delivery_not_found", "revocation_not_found",
        )


def test_get_revocation_404_when_none(client):
    setup(client)
    submit(client, "msg-1")
    resp = client.get("/deliveries/msg-1/revocation")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "revocation_not_found"


# ---------- 撤销对领取 / 租约 / 回执的约束 ----------

def test_claims_never_return_revoked_endpoints(client):
    setup(client)
    submit(client, "msg-1")                            # alice, bob
    submit(client, "msg-2", "single@corp.example.com")  # carol
    assert revoke(client, "msg-1", "stop").status_code == 200
    tasks = claim(client).json()["tasks"]
    assert {(t["delivery_id"], t["mailbox"]) for t in tasks} == {
        ("msg-2", "carol@mail.example.com"),
    }


def test_lease_issued_before_revocation_is_invalidated(client):
    setup(client)
    submit(client, "msg-1")
    token = tasks_by(client, "mailbox")["alice@mail.example.com"]["token"]
    assert revoke(client, "msg-1", "stop").status_code == 200
    # 既有租约立即失效：令牌完成明确拒绝，且不写入回执
    resp = complete(client, token, "delivered")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_lease_token" and detail["reason"] == "revoked"
    summary = client.get("/deliveries/msg-1/receipts").json()
    assert all(e["attempts"] == [] for e in summary["endpoints"])
    # 撤销后不再发出新租约
    assert claim(client).json()["tasks"] == []


def test_new_receipt_rejected_but_recorded_replay_still_returned(client):
    setup(client)
    submit(client, "msg-1")
    first = receipt(client, "msg-1", "alice@mail.example.com", 1, "temporary_failure").json()
    assert revoke(client, "msg-1", "stop").status_code == 200
    # 已记录回执的同序号同结果重试：原样返回（含首次 created_at）
    replay = receipt(client, "msg-1", "alice@mail.example.com", 1, "temporary_failure")
    assert replay.status_code == 200 and replay.json() == first
    # 同序号不同结果：仍是记录冲突，不写入
    conflict = receipt(client, "msg-1", "alice@mail.example.com", 1, "delivered")
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "attempt_result_conflict"
    # 新尝试（含从未有回执的 bob）：明确拒绝
    for mailbox, attempt_no in [("alice@mail.example.com", 2), ("bob@mail.example.com", 1)]:
        resp = receipt(client, "msg-1", mailbox, attempt_no, "delivered")
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "endpoint_revoked"
        assert detail["mailbox"] == mailbox
        assert detail["revocation_reason"] == "stop"
        assert detail["revoked_at"]
    summary = client.get("/deliveries/msg-1/receipts").json()
    bob = next(e for e in summary["endpoints"] if e["mailbox"] == "bob@mail.example.com")
    assert bob["attempts"] == []
    alice = next(e for e in summary["endpoints"] if e["mailbox"] == "alice@mail.example.com")
    assert [a["attempt_no"] for a in alice["attempts"]] == [1]


def test_kept_terminal_endpoint_keeps_terminal_semantics(client):
    setup(client)
    submit(client, "msg-1")
    assert receipt(client, "msg-1", "alice@mail.example.com", 1, "delivered").status_code == 200
    assert revoke(client, "msg-1", "stop").status_code == 200
    # 被保留的终结终点不是“已撤销”：新尝试仍按终结封口拒绝
    resp = receipt(client, "msg-1", "alice@mail.example.com", 2, "temporary_failure")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "endpoint_terminal"


def test_revocation_releases_domain_in_flight_slot(client):
    setup(client)
    assert client.put("/deliveries/claims/domain-limits", json={
        "domain": "mail.example.com", "max_in_flight": 1,
    }).status_code == 200
    submit(client, "msg-a")                             # alice, bob（同域）
    submit(client, "msg-b", "single@corp.example.com")  # carol（同域）
    first = claim(client, max_claims=10).json()["tasks"]
    assert len(first) == 1 and first[0]["delivery_id"] == "msg-a"  # 域名在途上限 1
    assert revoke(client, "msg-a", "stop").status_code == 200
    # 撤销使既有租约失效、名额立即释放：同域其他投递的到期任务随即可以领取
    tasks = claim(client, max_claims=10).json()["tasks"]
    assert {(t["delivery_id"], t["mailbox"]) for t in tasks} == {
        ("msg-b", "carol@mail.example.com"),
    }


# ---------- 并发：按事务提交顺序裁决 ----------

def test_no_new_receipts_after_revocation_under_concurrency(client):
    setup(client)
    submit(client, "msg-1")
    assert revoke(client, "msg-1", "stop").status_code == 200

    def worker(i):
        return receipt(client, "msg-1", "alice@mail.example.com", i + 1, "delivered").status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(worker, range(8)))
    assert set(codes) == {409}  # 撤销后的新尝试一律拒绝
    summary = client.get("/deliveries/msg-1/receipts").json()
    assert all(e["attempts"] == [] for e in summary["endpoints"])


def test_revoke_and_receipt_race_serialized_by_commit_order(client):
    setup(client)
    submit(client, "msg-1", "single@corp.example.com")  # carol 单终点
    results = []

    def submit_receipt(i):
        resp = receipt(client, "msg-1", "carol@mail.example.com", i + 1, "temporary_failure")
        results.append(resp.status_code)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(submit_receipt, i) for i in range(4)]
        revoke(client, "msg-1", "stop")
        for future in futures:
            future.result()
    # 提交顺序裁决：成功的回执都已持久化，失败的（被撤销拒绝或序号冲突）都没写入
    summary = client.get("/deliveries/msg-1/receipts").json()
    recorded = summary["endpoints"][0]["attempts"]
    assert len(recorded) == results.count(200)
    # 撤销之后不再接收任何新尝试
    resp = receipt(client, "msg-1", "carol@mail.example.com", 99, "delivered")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "endpoint_revoked"


def test_concurrent_claims_after_revocation_never_issue_revoked(client):
    setup(client)
    submit(client, "msg-1")
    submit(client, "msg-2", "single@corp.example.com")
    assert revoke(client, "msg-1", "stop").status_code == 200
    seen = []

    def worker(_):
        for task in claim(client, max_claims=10).json()["tasks"]:
            seen.append((task["delivery_id"], task["mailbox"]))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(worker, range(8)))
    assert all(delivery_id != "msg-1" for delivery_id, _ in seen)


# ---------- 与既有功能解耦 ----------

def test_revocation_does_not_change_decision_replay_report_or_receipts_view(client):
    setup(client)
    decision = submit(client, "msg-1")
    assert receipt(client, "msg-1", "alice@mail.example.com", 1, "temporary_failure").status_code == 200
    assert revoke(client, "msg-1", "stop").status_code == 200
    # 判定查询与同编号重放完全不变
    stored = client.get("/deliveries/msg-1").json()
    assert stored["status"] == "ok" and stored["endpoints"] == decision["endpoints"]
    replay = client.post("/deliveries", json={"delivery_id": "msg-1", "address": " MULTI@CORP.EXAMPLE.COM "})
    assert replay.status_code == 200 and replay.json() == decision
    # 回执查询仍只反映实际尝试（撤销不产生回执）
    summary = client.get("/deliveries/msg-1/receipts").json()
    assert summary["status"] == "pending"
    alice = next(e for e in summary["endpoints"] if e["mailbox"] == "alice@mail.example.com")
    bob = next(e for e in summary["endpoints"] if e["mailbox"] == "bob@mail.example.com")
    assert [a["result"] for a in alice["attempts"]] == ["temporary_failure"]
    assert bob["attempts"] == [] and bob["status"] == "pending"
    # 历史报表只统计判定，不受撤销影响
    report = client.get("/deliveries/report", params={
        "start": "2020-01-01T00:00:00Z", "end": "2100-01-01T00:00:00Z",
    }).json()
    assert report["total"] == 1
    assert report["groups"] == [
        {"version": 1, "selection": "candidate", "outcome": "ok", "count": 1},
    ]
