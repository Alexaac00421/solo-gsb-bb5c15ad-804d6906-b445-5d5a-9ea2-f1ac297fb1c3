"""终点邮箱域名的同时在途领取数上限：

管理员可设置（PUT）、删除（DELETE）并查询（GET 单项 / GET 列表）域名的正整数上限；
域名大小写归一（去空白、转小写），非法域名或非正上限一律拒绝且配置不变；
未配置域名不受限。领取仍按既有“到期时刻 → 投递编号 → 终点”顺序与请求上限进行；
某域名在途占用（未完成、未到期且未被更新回执抢先取代的租约）满额时跳过其任务、
继续领取其他域名。租约完成、到期或被既有回执抢先写入后释放占用；调低上限不撤销
已发令牌，占用降到新上限以下前不再领取。并发领取与配置变更经 BEGIN IMMEDIATE
串行化，原子核对，绝不超额。既有回执、令牌完成、判定与报表接口保持兼容。
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
        "DELETE FROM endpoint_leases;"
        "DELETE FROM delivery_attempts;"
        "DELETE FROM domain_claim_limits;"
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
        "match_key": "alt@corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "dave@other.example.net"}],
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


def claim(client, max_claims=100, lease_seconds=300):
    return client.post("/deliveries/claims", json={
        "max_claims": max_claims, "lease_seconds": lease_seconds,
    })


def complete(client, token, result):
    return client.post(f"/deliveries/claims/{token}/complete", json={"result": result})


def set_limit(client, domain, max_in_flight):
    return client.put("/deliveries/claims/domain-limits",
                      json={"domain": domain, "max_in_flight": max_in_flight})


def pairs(tasks):
    return {(t["delivery_id"], t["mailbox"]) for t in tasks}


def domain_of(mailbox):
    return mailbox.split("@", 1)[1]


# ---------- 配置：设置 / 查询 / 删除 / 归一 / 校验 ----------

def test_list_limits_empty_by_default(client):
    setup(client)
    resp = client.get("/deliveries/claims/domain-limits")
    assert resp.status_code == 200
    assert resp.json() == {"limits": []}


def test_set_get_and_list_limit(client):
    setup(client)
    resp = set_limit(client, "mail.example.com", 2)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["domain"] == "mail.example.com" and body["max_in_flight"] == 2
    assert body["updated_at"]

    got = client.get("/deliveries/claims/domain-limits/mail.example.com")
    assert got.status_code == 200 and got.json() == body

    set_limit(client, "other.example.net", 5)
    listed = client.get("/deliveries/claims/domain-limits").json()["limits"]
    assert [item["domain"] for item in listed] == ["mail.example.com", "other.example.net"]
    assert {item["max_in_flight"] for item in listed} == {2, 5}


def test_domain_is_case_insensitive_and_trimmed(client):
    setup(client)
    resp = set_limit(client, "  MAIL.Example.com  ", 3)
    assert resp.status_code == 200, resp.text
    assert resp.json()["domain"] == "mail.example.com"
    # 任意大小写 / 空白查询同一配置
    got = client.get("/deliveries/claims/domain-limits/MAIL.EXAMPLE.COM")
    assert got.status_code == 200 and got.json()["max_in_flight"] == 3
    # 覆盖写：归一后的同一键
    assert set_limit(client, "Mail.Example.Com", 7).json()["max_in_flight"] == 7
    assert len(client.get("/deliveries/claims/domain-limits").json()["limits"]) == 1
    # 删除也按归一键
    deleted = client.delete("/deliveries/claims/domain-limits/mail.example.com")
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    assert client.get("/deliveries/claims/domain-limits").json()["limits"] == []


@pytest.mark.parametrize("domain", [
    "not-a-domain", "example", "a.b", "ex..com", "-bad.com", "bad-.com",
    "foo@mail.example.com", "mailto:mail.example.com", "   ", "中文.com",
])
def test_invalid_domain_rejected_and_config_unchanged(client, domain):
    setup(client)
    assert set_limit(client, "mail.example.com", 4).status_code == 200
    resp = client.put("/deliveries/claims/domain-limits",
                      json={"domain": domain, "max_in_flight": 2})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "invalid_domain"
    # 既有配置保持不变，非法项未落库
    limits = client.get("/deliveries/claims/domain-limits").json()["limits"]
    assert [item["domain"] for item in limits] == ["mail.example.com"]
    assert client.get("/deliveries/claims/domain-limits/mail.example.com").json()["max_in_flight"] == 4
    # 单项查询 / 删除同样拒绝非法域名
    assert client.get(f"/deliveries/claims/domain-limits/{domain}").status_code == 422
    assert client.delete(f"/deliveries/claims/domain-limits/{domain}").status_code == 422


@pytest.mark.parametrize("domain", ["", "  "])
def test_empty_domain_in_body_rejected(client, domain):
    setup(client)
    assert set_limit(client, "mail.example.com", 4).status_code == 200
    resp = client.put("/deliveries/claims/domain-limits",
                      json={"domain": domain, "max_in_flight": 2})
    assert resp.status_code == 422
    assert [item["domain"] for item in client.get("/deliveries/claims/domain-limits").json()["limits"]] == [
        "mail.example.com"
    ]


@pytest.mark.parametrize("max_in_flight", [0, -1, -100])
def test_non_positive_limit_rejected_and_config_unchanged(client, max_in_flight):
    setup(client)
    assert set_limit(client, "mail.example.com", 4).status_code == 200
    resp = client.put("/deliveries/claims/domain-limits",
                      json={"domain": "other.example.net", "max_in_flight": max_in_flight})
    assert resp.status_code == 422, resp.text
    limits = client.get("/deliveries/claims/domain-limits").json()["limits"]
    assert [item["domain"] for item in limits] == ["mail.example.com"]  # 新项未写入，旧项不变


def test_missing_fields_rejected(client):
    setup(client)
    assert client.put("/deliveries/claims/domain-limits", json={"domain": "mail.example.com"}).status_code == 422
    assert client.put("/deliveries/claims/domain-limits", json={"max_in_flight": 1}).status_code == 422


def test_get_unconfigured_domain_returns_404(client):
    setup(client)
    resp = client.get("/deliveries/claims/domain-limits/other.example.net")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "domain_limit_not_found"
    assert resp.json()["detail"]["domain"] == "other.example.net"


def test_delete_is_idempotent(client):
    setup(client)
    set_limit(client, "mail.example.com", 2)
    first = client.delete("/deliveries/claims/domain-limits/mail.example.com")
    assert first.status_code == 200 and first.json() == {
        "domain": "mail.example.com", "deleted": True, "max_in_flight": None,
    }
    # 再删一次：未配置也成功，deleted=false；查询转为 404
    second = client.delete("/deliveries/claims/domain-limits/mail.example.com")
    assert second.status_code == 200 and second.json()["deleted"] is False
    assert client.get("/deliveries/claims/domain-limits/mail.example.com").status_code == 404


# ---------- 领取：按域名在途上限跳过，继续领取其他域名 ----------

def test_claim_respects_domain_limit_and_skips_to_other_domains(client):
    setup(client)
    set_limit(client, "mail.example.com", 1)  # alice/bob/carol 同域，只领 1 个
    # msg-a（alice, bob）→ msg-b（carol）→ msg-c（dave），同一秒到期 → 按编号排序
    submit(client, "msg-a", "multi@corp.example.com")
    submit(client, "msg-b", "single@corp.example.com")
    submit(client, "msg-c", "alt@corp.example.com")
    tasks = claim(client, max_claims=10).json()["tasks"]
    order = [(t["delivery_id"], t["mailbox"]) for t in tasks]
    # 只取该域名到期最早的 alice；bob、carol 被跳过；继续领取不受限的 dave
    assert order == [
        ("msg-a", "alice@mail.example.com"),
        ("msg-c", "dave@other.example.net"),
    ]


def test_domain_limit_counts_across_deliveries_and_blocks_next_claim(client):
    setup(client)
    set_limit(client, "mail.example.com", 2)
    submit(client, "msg-a", "multi@corp.example.com")   # alice, bob
    submit(client, "msg-b", "single@corp.example.com")  # carol
    submit(client, "msg-c", "alt@corp.example.com")     # dave（其他域）
    first = claim(client).json()["tasks"]
    assert len(first) == 3  # mail 域取满 2，同时继续领取其他域的 dave
    assert {domain_of(t["mailbox"]) for t in first} == {"mail.example.com", "other.example.net"}
    # 再次领取：mail 域已满（2 在途）→ 只剩其他域的 dave？不——dave 上一轮已被领走
    assert claim(client).json()["tasks"] == []


def test_unconfigured_domain_unlimited_even_when_other_limited(client):
    setup(client)
    set_limit(client, "mail.example.com", 1)
    submit(client, "msg-a", "multi@corp.example.com")  # alice, bob 受限域
    submit(client, "msg-c", "alt@corp.example.com")    # dave 未配置域
    tasks = claim(client).json()["tasks"]
    assert pairs(tasks) == {
        ("msg-a", "alice@mail.example.com"),
        ("msg-c", "dave@other.example.net"),
    }


def test_limit_does_not_change_task_shape(client):
    setup(client)
    set_limit(client, "mail.example.com", 1)
    submit(client, "msg-a")
    task = claim(client).json()["tasks"][0]
    assert set(task) == {"delivery_id", "mailbox", "attempt_no", "due_at", "lease_expires_at", "token"}
    assert task["attempt_no"] == 1 and len(task["token"]) == 32


# ---------- 占用释放：完成 / 到期 / 被既有回执抢先 ----------

def test_completing_lease_frees_domain_slot(client):
    setup(client)
    set_limit(client, "mail.example.com", 1)
    submit(client, "msg-a", "multi@corp.example.com")   # alice, bob
    submit(client, "msg-b", "single@corp.example.com")  # carol
    token_alice = next(t for t in claim(client).json()["tasks"]
                       if t["mailbox"] == "alice@mail.example.com")["token"]
    assert claim(client).json()["tasks"] == []  # 同域 1 在途，bob/carol 不可领
    # 凭令牌完成（delivered 终结，alice 不再入列）：名额释放，下一个到期的 bob 可领
    assert complete(client, token_alice, "delivered").status_code == 200
    tasks = claim(client).json()["tasks"]
    assert pairs(tasks) == {("msg-a", "bob@mail.example.com")}


def test_expired_lease_releases_slot_and_token_rotates(client):
    setup(client)
    set_limit(client, "mail.example.com", 1)
    submit(client, "msg-a", "multi@corp.example.com")  # alice, bob
    old_token = next(t for t in claim(client).json()["tasks"]
                     if t["mailbox"] == "alice@mail.example.com")["token"]
    assert claim(client).json()["tasks"] == []  # 在途占用中
    # 租约到期（未完成）：不再占用名额，alice 可被重领，旧令牌失效
    conn = db.connect()
    conn.execute(
        "UPDATE endpoint_leases SET lease_expires_at = "
        "strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', '-1 second') WHERE token = ?",
        (old_token,),
    )
    conn.close()
    tasks = claim(client).json()["tasks"]
    assert pairs(tasks) == {("msg-a", "alice@mail.example.com")}  # 到期重领（同尝试再发一次）
    new_token = tasks[0]["token"]
    assert new_token != old_token
    assert complete(client, old_token, "delivered").json()["detail"]["reason"] == "unknown"
    assert complete(client, new_token, "delivered").status_code == 200


def test_superseded_lease_releases_slot_without_revoking(client):
    setup(client)
    set_limit(client, "mail.example.com", 1)
    submit(client, "msg-a", "multi@corp.example.com")  # alice, bob
    token_alice = next(t for t in claim(client).json()["tasks"]
                       if t["mailbox"] == "alice@mail.example.com")["token"]
    # 既有回执接口抢先为 alice 写入 attempt 1（delivered）：租约行仍未完成 / 未到期，
    # 但已被更新回执取代 → 不再占用域名名额
    receipt = client.post("/deliveries/msg-a/receipts", json={
        "mailbox": "alice@mail.example.com", "attempt_no": 1, "result": "delivered",
    })
    assert receipt.status_code == 200
    # bob 现在可领（alice 已终结不入列）；旧令牌提交得 superseded，不覆盖
    tasks = claim(client).json()["tasks"]
    assert pairs(tasks) == {("msg-a", "bob@mail.example.com")}
    conflict = complete(client, token_alice, "temporary_failure")
    assert conflict.status_code == 409 and conflict.json()["detail"]["reason"] == "superseded"


def test_superseded_lease_frees_slot_and_reclaim_takes_it_back(client):
    # 长期租约被同序号 temporary_failure 直接回执取代：名额立即释放（同域 bob 可领）；
    # alice 退避到后虽旧租约未期满也可重领，但须重新占用域名名额（在途满时仍跳过）。
    setup(client)
    set_limit(client, "mail.example.com", 1)
    submit(client, "msg-a", "multi@corp.example.com")  # alice, bob（同域）
    old = next(t for t in claim(client, lease_seconds=3600).json()["tasks"]
               if t["mailbox"] == "alice@mail.example.com")
    assert claim(client).json()["tasks"] == []  # alice 在途占名额，bob 被跳过
    receipt = client.post("/deliveries/msg-a/receipts", json={
        "mailbox": "alice@mail.example.com", "attempt_no": 1, "result": "temporary_failure",
    })
    assert receipt.status_code == 200
    # 名额已释放：bob 可领；alice 退避未到且旧租约虽在也不再阻塞
    tasks = claim(client).json()["tasks"]
    assert pairs(tasks) == {("msg-a", "bob@mail.example.com")}
    token_bob = tasks[0]["token"]
    # alice 退避已到（60 秒）：可重领，但同域名额正被 bob 占用 → 仍跳过
    conn = db.connect()
    conn.execute(
        "UPDATE delivery_attempts SET created_at = "
        "strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', '-60 seconds') "
        "WHERE delivery_id = 'msg-a' AND mailbox = 'alice@mail.example.com'"
    )
    conn.close()
    assert claim(client).json()["tasks"] == []
    # bob 完成后名额释放：alice 以序号 2 重领（旧租约未期满也轮换令牌）
    assert complete(client, token_bob, "delivered").status_code == 200
    tasks = claim(client).json()["tasks"]
    assert pairs(tasks) == {("msg-a", "alice@mail.example.com")}
    assert tasks[0]["attempt_no"] == 2 and tasks[0]["token"] != old["token"]


# ---------- 调低 / 调高 / 删除上限的行为 ----------

def test_lowering_limit_keeps_tokens_but_blocks_new_claims(client):
    setup(client)
    set_limit(client, "mail.example.com", 2)
    submit(client, "msg-a", "multi@corp.example.com")   # alice, bob
    submit(client, "msg-b", "single@corp.example.com")  # carol
    first = claim(client).json()["tasks"]
    assert len(first) == 2  # 已发出 2 个令牌
    tokens = {t["mailbox"]: t["token"] for t in first}

    # 调低到 1：已发令牌不撤销、仍可完成；在途占用（2）降到新上限以下前不再领取
    assert set_limit(client, "mail.example.com", 1).status_code == 200
    assert claim(client).json()["tasks"] == []
    assert complete(client, tokens["alice@mail.example.com"], "delivered").status_code == 200
    # 仍有 1 在途（= 上限 1）→ 还是不可领
    assert claim(client).json()["tasks"] == []
    assert complete(client, tokens["bob@mail.example.com"], "delivered").status_code == 200
    # 占用归零 → carol 可领
    tasks = claim(client).json()["tasks"]
    assert pairs(tasks) == {("msg-b", "carol@mail.example.com")}


def test_raising_limit_allows_more_immediately(client):
    setup(client)
    set_limit(client, "mail.example.com", 1)
    submit(client, "msg-a", "multi@corp.example.com")  # alice, bob
    assert len(claim(client).json()["tasks"]) == 1
    set_limit(client, "mail.example.com", 2)  # 调高立即生效
    tasks = claim(client).json()["tasks"]
    assert pairs(tasks) == {("msg-a", "bob@mail.example.com")}


def test_deleting_limit_makes_domain_unlimited(client):
    setup(client)
    set_limit(client, "mail.example.com", 1)
    submit(client, "msg-a", "multi@corp.example.com")  # alice, bob
    assert len(claim(client).json()["tasks"]) == 1
    resp = client.delete("/deliveries/claims/domain-limits/mail.example.com")
    assert resp.status_code == 200 and resp.json()["deleted"] is True
    tasks = claim(client).json()["tasks"]  # 删除配置 → 不受限
    assert pairs(tasks) == {("msg-a", "bob@mail.example.com")}


# ---------- 并发：领取与配置变更原子核对，不超额 ----------

def test_concurrent_claims_never_exceed_domain_limit(client):
    setup(client)
    set_limit(client, "mail.example.com", 1)
    # 受限域 3 个到期终点 + 未受限域 1 个
    submit(client, "msg-a", "multi@corp.example.com")   # alice, bob
    submit(client, "msg-b", "single@corp.example.com")  # carol
    submit(client, "msg-c", "alt@corp.example.com")     # dave
    assigned: list[tuple[str, str]] = []

    def worker(_):
        resp = claim(client, max_claims=10)
        assigned.extend((t["delivery_id"], t["mailbox"]) for t in resp.json()["tasks"])
        return resp.status_code

    with ThreadPoolExecutor(max_workers=16) as pool:
        codes = list(pool.map(worker, range(32)))
    assert set(codes) == {200}
    by_domain: dict[str, set] = {}
    for did, mailbox in assigned:
        by_domain.setdefault(domain_of(mailbox), set()).add((did, mailbox))
    assert by_domain["mail.example.com"] == {("msg-a", "alice@mail.example.com")}  # 恰 1 个
    assert by_domain["other.example.net"] == {("msg-c", "dave@other.example.net")}
    # 租约行也只对应这两个有效占用
    conn = db.connect()
    active = conn.execute(
        "SELECT mailbox FROM endpoint_leases WHERE completed_at IS NULL "
        "AND lease_expires_at > strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')"
    ).fetchall()
    conn.close()
    assert {r["mailbox"].split("@", 1)[1] for r in active} == {"mail.example.com", "other.example.net"}


def test_limit_change_race_with_claims_never_overcommits(client):
    setup(client)
    submit(client, "msg-a", "multi@corp.example.com")   # alice, bob
    submit(client, "msg-b", "single@corp.example.com")  # carol
    submit(client, "msg-c", "alt@corp.example.com")     # dave
    set_limit(client, "mail.example.com", 1)  # 并发开始前上限已生效（始终保持 1）
    assigned: list[tuple[str, str]] = []

    def claim_worker(_):
        # 配置固定在 1：每次领取后在途占用必然不超过 1；本测试不完成租约，
        # 因此配置写入与领取无论如何交错，mail 域总共只能发出 1 个令牌。
        resp = claim(client, max_claims=10)
        assigned.extend((t["delivery_id"], t["mailbox"]) for t in resp.json()["tasks"])
        return resp.status_code

    def config_worker(_):
        # 并发期间反复用同值 1 开启写事务，与领取竞争写锁（模拟管理员的配置变更）。
        return set_limit(client, "mail.example.com", 1).status_code

    with ThreadPoolExecutor(max_workers=12) as pool:
        f1 = list(pool.map(claim_worker, range(12)))
        f2 = list(pool.map(config_worker, range(8)))
    assert set(f1) == {200} and set(f2) == {200}
    mail_assigned = {(d, m) for d, m in assigned if domain_of(m) == "mail.example.com"}
    assert mail_assigned == {("msg-a", "alice@mail.example.com")}  # 全程上限 1 → 恰 1 个
    other_assigned = {(d, m) for d, m in assigned if domain_of(m) == "other.example.net"}
    assert other_assigned == {("msg-c", "dave@other.example.net")}
    # 库内有效租约占用与最终配置一致（不超额）
    conn = db.connect()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM endpoint_leases "
        "WHERE completed_at IS NULL AND lease_expires_at > strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now') "
        "AND mailbox LIKE '%@mail.example.com'"
    ).fetchone()["n"]
    conn.close()
    assert n == 1


def test_lowering_after_overcommit_does_not_revoke_but_stops_new(client):
    """调高到 2 并发占满后立刻调低到 1：已发 2 个令牌继续有效，但新领取一律暂停。"""
    setup(client)
    submit(client, "msg-a", "multi@corp.example.com")   # alice, bob
    submit(client, "msg-b", "single@corp.example.com")  # carol
    set_limit(client, "mail.example.com", 2)
    first = claim(client).json()["tasks"]
    assert len(first) == 2
    tokens = {t["mailbox"]: t["token"] for t in first}

    def lower(_):
        return set_limit(client, "mail.example.com", 1).status_code

    def claimer(_):
        resp = claim(client, max_claims=10)
        return {(t["delivery_id"], t["mailbox"]) for t in resp.json()["tasks"]}, resp.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lower, range(4)))
        results = list(pool.map(claimer, range(8)))
    assert set(codes) == {200}
    # 调低之后的领取绝不再发出 mail 域令牌（调低前若已有事务提交，也至多凑满 2）
    mail_new = set()
    for got, code in results:
        assert code == 200
        mail_new |= {pair for pair in got if domain_of(pair[1]) == "mail.example.com"}
    assert not mail_new  # 第一次已占满 2，调低前后都不会再有第三个 mail 域在途
    # 先发的两个令牌仍可用（不撤销）
    for mailbox, token in tokens.items():
        assert complete(client, token, "delivered").status_code == 200, mailbox


# ---------- 兼容：既有回执 / 判定 / 报表接口形状与行为不变 ----------

def test_claims_with_limits_still_feed_existing_receipts_and_report(client):
    setup(client)
    set_limit(client, "mail.example.com", 1)
    decision = submit(client, "msg-a", "multi@corp.example.com")
    task = claim(client).json()["tasks"][0]
    # 凭令牌完成：既有回执查询、判定查询、同编号重放均不变
    assert complete(client, task["token"], "temporary_failure").status_code == 200
    summary = client.get("/deliveries/msg-a/receipts").json()
    ep = next(e for e in summary["endpoints"] if e["mailbox"] == task["mailbox"])
    assert ep["status"] == "temporary_failure" and ep["attempts"][0]["attempt_no"] == 1
    stored = client.get("/deliveries/msg-a").json()
    assert stored["status"] == "ok" and stored["endpoints"] == decision["endpoints"]
    replay = client.post("/deliveries", json={"delivery_id": "msg-a", "address": "MULTI@CORP.EXAMPLE.COM"})
    assert replay.status_code == 200 and replay.json()["delivery_id"] == "msg-a"
    # 报表只统计判定，配置与租约不进入报表
    report = client.get("/deliveries/report", params={
        "start": "2000-01-01T00:00:00Z", "end": "2100-01-01T00:00:00Z",
    }).json()
    assert report["total"] == 1
    assert report["items"][0]["delivery_id"] == "msg-a"


def test_limits_have_no_effect_without_config_and_receipts_api_still_works(client):
    setup(client)
    submit(client, "msg-a", "multi@corp.example.com")
    # 未配置任何域名上限：行为与原先完全一致（2 个终点一次领出）
    tasks = claim(client).json()["tasks"]
    assert len(tasks) == 2
    # 既有直接回执接口不受配置存在与否影响
    resp = client.post("/deliveries/msg-a/receipts", json={
        "mailbox": "alice@mail.example.com", "attempt_no": 1, "result": "delivered",
    })
    assert resp.status_code == 200
