"""租约到期点按“实际领取时刻 + 请求秒数”计算（亚秒精度回归）：

问题：领取与完成都把当前时刻截到整秒——秒末（如 xx:00.800）申请 1 秒租约时，
到期点被向下存成 xx:01，约 0.2 秒后（xx:01.000）完成即被判 expired；同一终点
提前可重领（旧令牌轮换），域名在途名额也提前释放。

修复：leased_at / lease_expires_at 与全部有效期比较使用微秒精度 UTC ISO 8601；
到期点严格等于“领取时刻 + lease_seconds”，到期前不重领、令牌可完成且只写一次
回执；到期后才允许重领并轮换旧令牌。域名在途计数使用同一有效期判断；完成、
较新回执抢先（superseded）、撤销释放名额的既有规则不变。
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

# 必须在导入 app 之前设置，config 在导入时设置
os.environ.setdefault("ROUTER_DB", os.path.join(tempfile.mkdtemp(prefix="router-test-"), "test.db"))

from fastapi.testclient import TestClient

from app import db, store
from app.main import app


def _setup_data() -> None:
    db.init_db()
    conn = db.connect()
    conn.executescript(
        "DELETE FROM endpoint_leases;"
        "DELETE FROM delivery_attempts;"
        "DELETE FROM domain_claim_limits;"
        "DELETE FROM delivery_endpoint_revocations;"
        "DELETE FROM delivery_revocations;"
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
]


def _client() -> TestClient:
    return TestClient(app)


def _publish_and_policy(client: TestClient) -> None:
    assert client.put("/draft", json={"revision": 0, "rules": RULES}).status_code == 200
    assert client.post("/publish", json={"revision": 1}).status_code == 200
    assert client.put("/policy", json={
        "revision": 0, "baseline_version": 1, "candidate_version": 1, "candidate_percent": 100,
    }).status_code == 200


def _submit(client: TestClient, delivery_id: str, address: str = "multi@corp.example.com") -> None:
    resp = client.post("/deliveries", json={"delivery_id": delivery_id, "address": address})
    assert resp.status_code == 200, resp.text


def _pin_due_before(delivery_ids: list[str], value: str = "2026-10-06T11:00:00+00:00") -> None:
    """把判定受理时刻固定到注入领取时刻之前，保证“无回执立即到期”确定成立。"""
    conn = db.connect()
    conn.executemany(
        "UPDATE delivery_decisions SET created_at = ? WHERE delivery_id = ?",
        [(value, did) for did in delivery_ids],
    )
    conn.close()


def _claim_at(client: TestClient, moment: datetime, lease_seconds: int = 1) -> list[dict]:
    """在事务内以注入的领取时刻领取（绕过 wall clock，确定性复现秒末场景）。"""
    with db.transaction() as conn:
        return store.claim_due_tasks(conn, max_claims=100, lease_seconds=lease_seconds, now=moment)


def _complete_at(client: TestClient, token: str, result: str, moment: datetime) -> dict:
    with db.transaction() as conn:
        return store.complete_lease(conn, token, result, now=moment)


# ---------- 秒末领取 1 秒租约：真实到期点之前一直有效 ----------

def test_one_second_lease_claimed_near_end_of_second_stays_valid(client=None):
    _setup_data()
    with _client() as c:
        _publish_and_policy(c)
        _submit(c, "msg-1", "single@corp.example.com")
        _pin_due_before(["msg-1"])

        t0 = datetime(2026, 10, 6, 12, 0, 0, 800_000, tzinfo=timezone.utc)  # 秒末 +0.800s
        tasks = _claim_at(c, t0, lease_seconds=1)
        assert len(tasks) == 1
        token = tasks[0]["token"]
        # 到期点 = 实际领取时刻 + 1 秒（微秒精度），而非向下截到 xx:01
        assert tasks[0]["lease_expires_at"] == "2026-10-06T12:00:01.800000+00:00"

        # 约 0.2 秒后完成：旧实现在此误报 expired；修复后必须成功并写入回执
        attempt = _complete_at(c, token, "delivered", t0 + timedelta(milliseconds=200))
        assert attempt["attempt_no"] == 1 and attempt["result"] == "delivered"

        # 回执只写入一次：同一令牌再次提交 → completed，不重复写
        again = None
        try:
            _complete_at(c, token, "delivered", t0 + timedelta(milliseconds=300))
        except store.InvalidLeaseToken as exc:
            again = exc.reason
        assert again == "completed"
        summary = c.get("/deliveries/msg-1/receipts").json()
        attempts = summary["endpoints"][0]["attempts"]
        assert len(attempts) == 1 and attempts[0]["result"] == "delivered"


def test_endpoint_not_reclaimable_before_real_expiry(client=None):
    _setup_data()
    with _client() as c:
        _publish_and_policy(c)
        _submit(c, "msg-1", "single@corp.example.com")
        _pin_due_before(["msg-1"])

        t0 = datetime(2026, 10, 6, 12, 0, 0, 800_000, tzinfo=timezone.utc)
        old = _claim_at(c, t0, lease_seconds=1)[0]

        # 跨过整秒边界但仍在真实到期点（xx:01.800）之前：终点不可重领
        assert _claim_at(c, t0 + timedelta(milliseconds=900), lease_seconds=1) == []
        # 旧令牌此刻仍有效，可完成
        attempt = _complete_at(c, old["token"], "delivered", t0 + timedelta(milliseconds=950))
        assert attempt["result"] == "delivered"


def test_endpoint_reclaimable_only_after_real_expiry_and_old_token_invalidated(client=None):
    _setup_data()
    with _client() as c:
        _publish_and_policy(c)
        _submit(c, "msg-1", "single@corp.example.com")
        _pin_due_before(["msg-1"])

        t0 = datetime(2026, 10, 6, 12, 0, 0, 800_000, tzinfo=timezone.utc)
        old = _claim_at(c, t0, lease_seconds=1)[0]

        # 到期点当刻（边界严格）：旧令牌报 expired，且不写入回执
        at_expiry = t0 + timedelta(seconds=1)
        try:
            _complete_at(c, old["token"], "delivered", at_expiry)
            assert False, "expected expired"
        except store.InvalidLeaseToken as exc:
            assert exc.reason == "expired"
        assert c.get("/deliveries/msg-1/receipts").json()["endpoints"][0]["attempts"] == []

        # 到期之后才允许重领：同一终点、同尝试序号（重发）、令牌轮换
        reclaimed = _claim_at(c, at_expiry + timedelta(milliseconds=1), lease_seconds=1)
        assert len(reclaimed) == 1
        assert reclaimed[0]["delivery_id"] == "msg-1" and reclaimed[0]["attempt_no"] == 1
        assert reclaimed[0]["token"] != old["token"]
        # 旧令牌因行轮换失效（unknown），不得写入回执
        try:
            _complete_at(c, old["token"], "delivered", at_expiry + timedelta(milliseconds=2))
            assert False, "expected unknown"
        except store.InvalidLeaseToken as exc:
            assert exc.reason == "unknown"
        # 新令牌有效，可完成并写入唯一回执
        done = _complete_at(c, reclaimed[0]["token"], "delivered", at_expiry + timedelta(milliseconds=10))
        assert done["result"] == "delivered"
        assert len(c.get("/deliveries/msg-1/receipts").json()["endpoints"][0]["attempts"]) == 1


# ---------- 域名在途计数使用同一有效期判断 ----------

def test_domain_inflight_slot_held_until_real_expiry(client=None):
    _setup_data()
    with _client() as c:
        _publish_and_policy(c)
        _submit(c, "msg-a", "multi@corp.example.com")   # alice, bob 同域
        _submit(c, "msg-b", "single@corp.example.com")  # carol 同域
        _pin_due_before(["msg-a", "msg-b"])
        with db.transaction() as conn:
            assert store.set_domain_claim_limit(conn, "mail.example.com", 1)["max_in_flight"] == 1

        t0 = datetime(2026, 10, 6, 12, 0, 0, 800_000, tzinfo=timezone.utc)
        first = _claim_at(c, t0, lease_seconds=1)
        assert len(first) == 1 and first[0]["mailbox"] == "alice@mail.example.com"

        # xx:01.500：旧实现把 1 秒租约视为已到期 → bob/carol 名额被提前放出；
        # 修复后真实到期点 xx:01.800 未到，同域仍占满 1 个名额 → 不可领
        assert _claim_at(c, t0 + timedelta(milliseconds=700), lease_seconds=1) == []

        # 完成后名额立即释放（既有规则不变）：下一个到期的 bob 可领
        assert _complete_at(c, first[0]["token"], "delivered",
                            t0 + timedelta(milliseconds=750))["mailbox"] == "alice@mail.example.com"
        next_tasks = _claim_at(c, t0 + timedelta(milliseconds=780), lease_seconds=1)
        assert [(t["delivery_id"], t["mailbox"]) for t in next_tasks] == [
            ("msg-a", "bob@mail.example.com"),
        ]


def test_domain_slot_released_after_real_expiry(client=None):
    _setup_data()
    with _client() as c:
        _publish_and_policy(c)
        _submit(c, "msg-a", "multi@corp.example.com")  # alice, bob 同域
        _pin_due_before(["msg-a"])
        with db.transaction() as conn:
            assert store.set_domain_claim_limit(conn, "mail.example.com", 1)["max_in_flight"] == 1

        t0 = datetime(2026, 10, 6, 12, 0, 0, 800_000, tzinfo=timezone.utc)
        old = _claim_at(c, t0, lease_seconds=1)[0]
        # 真实到期点之后：名额释放，alice 可重领，令牌轮换
        reclaimed = _claim_at(c, t0 + timedelta(milliseconds=1100), lease_seconds=1)
        assert len(reclaimed) == 1 and reclaimed[0]["mailbox"] == "alice@mail.example.com"
        assert reclaimed[0]["token"] != old["token"]


# ---------- 亚秒精度下既有规则不回归：superseded / 撤销 ----------

def test_superseded_token_and_slot_still_release_early(client=None):
    # 较新回执抢先取代：租约未到期也立即失效并释放域名名额（既有规则保持）
    _setup_data()
    with _client() as c:
        _publish_and_policy(c)
        _submit(c, "msg-a", "multi@corp.example.com")  # alice, bob
        _pin_due_before(["msg-a"])
        with db.transaction() as conn:
            assert store.set_domain_claim_limit(conn, "mail.example.com", 1)["max_in_flight"] == 1

        t0 = datetime(2026, 10, 6, 12, 0, 0, 800_000, tzinfo=timezone.utc)
        token_alice = _claim_at(c, t0, lease_seconds=3600)[0]["token"]
        assert _claim_at(c, t0 + timedelta(seconds=1), lease_seconds=3600) == []
        # 既有回执接口抢先写入 attempt 1（delivered，终结）：令牌 superseded、名额释放
        resp = c.post("/deliveries/msg-a/receipts", json={
            "mailbox": "alice@mail.example.com", "attempt_no": 1, "result": "delivered",
        })
        assert resp.status_code == 200
        try:
            _complete_at(c, token_alice, "temporary_failure", t0 + timedelta(seconds=2))
            assert False, "expected superseded"
        except store.InvalidLeaseToken as exc:
            assert exc.reason == "superseded"
        tasks = _claim_at(c, t0 + timedelta(seconds=2), lease_seconds=3600)
        assert [(t["delivery_id"], t["mailbox"]) for t in tasks] == [
            ("msg-a", "bob@mail.example.com"),
        ]


def test_revoked_lease_invalidated_and_slot_released(client=None):
    _setup_data()
    with _client() as c:
        _publish_and_policy(c)
        _submit(c, "msg-a", "multi@corp.example.com")    # alice, bob（撤销会标记该判定全部未终结终点）
        _submit(c, "msg-b", "single@corp.example.com")   # carol：同域等待名额
        _pin_due_before(["msg-a", "msg-b"])
        with db.transaction() as conn:
            assert store.set_domain_claim_limit(conn, "mail.example.com", 1)["max_in_flight"] == 1

        t0 = datetime(2026, 10, 6, 12, 0, 0, 800_000, tzinfo=timezone.utc)
        token_alice = _claim_at(c, t0, lease_seconds=3600)[0]["token"]
        assert _claim_at(c, t0 + timedelta(seconds=1), lease_seconds=3600) == []
        # 撤销 msg-a（alice/bob 全部撤销）：alice 既有租约立即 revoked、不得写回执，
        # 同域名名额立即释放——等待中的 carol（msg-b）可以领取
        resp = c.post("/deliveries/msg-a/revocation", json={"reason": "operator cancel"})
        assert resp.status_code == 200, resp.text
        try:
            _complete_at(c, token_alice, "delivered", t0 + timedelta(seconds=2))
            assert False, "expected revoked"
        except store.InvalidLeaseToken as exc:
            assert exc.reason == "revoked"
        assert c.get("/deliveries/msg-a/receipts").json()["endpoints"][0]["attempts"] == []
        tasks = _claim_at(c, t0 + timedelta(seconds=2), lease_seconds=3600)
        assert [(t["delivery_id"], t["mailbox"]) for t in tasks] == [
            ("msg-b", "carol@mail.example.com"),
        ]


# ---------- HTTP 层：接口字段不变，lease_expires_at 为含亚秒的 UTC ISO 8601 ----------

def test_http_claim_and_complete_shapes_unchanged_with_subsecond_expiry():
    _setup_data()
    with _client() as c:
        _publish_and_policy(c)
        _submit(c, "msg-1", "single@corp.example.com")
        resp = c.post("/deliveries/claims", json={"max_claims": 10, "lease_seconds": 1})
        assert resp.status_code == 200, resp.text
        task = resp.json()["tasks"][0]
        assert set(task) == {
            "delivery_id", "mailbox", "attempt_no", "due_at", "lease_expires_at", "token",
        }
        assert task["attempt_no"] == 1 and len(task["token"]) == 32
        # 时间字段仍为 UTC ISO 8601；lease_expires_at 携带亚秒精度且约为 now + 1s
        expires = datetime.fromisoformat(task["lease_expires_at"])
        assert expires.tzinfo is not None
        assert timedelta(milliseconds=900) <= expires - datetime.now(timezone.utc) <= timedelta(seconds=2)

        done = c.post(f"/deliveries/claims/{task['token']}/complete", json={"result": "delivered"})
        assert done.status_code == 200, done.text
        assert done.json() == {
            "delivery_id": "msg-1",
            "mailbox": "carol@mail.example.com",
            "attempt_no": 1,
            "result": "delivered",
            "created_at": done.json()["created_at"],
        }
        # 已终结：不再入列
        assert c.post("/deliveries/claims",
                      json={"max_claims": 10, "lease_seconds": 1}).json()["tasks"] == []
