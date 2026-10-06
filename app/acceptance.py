"""投递判定受理的共享核心：单笔与批量接口共用的地址规范化、既有编号冲突检查、
策略快照与路由持久化逻辑（含受理后仅评估一次的候选侧熔断）。

本模块不依赖 Web 框架：全部输入输出都是纯数据 / 异常，须在已取得写锁的
BEGIN IMMEDIATE 事务（或只读事务用于纯校验）内调用。HTTP 状态码与响应字段
的映射由 app.main 完成，从而保证单笔 POST /deliveries 与批量
POST /deliveries/batch 的既有契约完全一致。

受理规则（单笔是批量大小为 1 的特例）：

- 地址规范化：去首尾空白并转小写，随后做合法性校验；无效地址不进入事务、
  不占用编号；批量时收集全部无效项供整批拒绝。
- 既有编号冲突检查：相同编号 + 相同规范化地址 → 原样重放首次判定（含失败）；
  相同编号 + 不同规范化地址 → DeliveryIdConflict，整批拒绝且不写入。
- 策略快照：仅对需要新判定的编号读取一次当前默认策略（不存在则
  PolicyNotConfiguredForAcceptance），同批新判定共用受理时的策略修订号、
  比例与基线 / 候选版本。
- 路由持久化：按规范化地址稳定分桶选定版本，规则集按版本缓存（同批同一版本
  只加载一次）；拒收 / 无路由 / 成环同样作为一份失败判定持久化，不切换版本，
  之后可按原内容重放。
- 熔断：只要本批写入过新判定，全部写入完成后按受理时的策略修订号仅评估一次
  候选侧熔断；纯重放（无新判定）不评估，重放项不进入统计。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import store
from .routing import ResolutionError, RuleSet, is_valid_address, resolve
from .traffic import select_version


class InvalidAddressForAcceptance(Exception):
    """规范化后的地址不合法（单笔拒绝；批量收集全部无效项后整批拒绝）。"""

    def __init__(self, raw: str, normalized: str):
        super().__init__(f"invalid address: {raw!r}")
        self.raw = raw
        self.normalized = normalized


class DeliveryIdConflictForAcceptance(Exception):
    """既有投递编号绑定了其他规范化地址；冲突判定在本次请求中不被改写。"""

    def __init__(self, conflicts: list[dict]):
        super().__init__(f"{len(conflicts)} delivery id(s) already submitted with a different address")
        # [{"delivery_id", "submitted_address", "stored_address"}, ...]（单笔恰好一项）
        self.conflicts = conflicts

    @property
    def delivery_id(self) -> str:
        return self.conflicts[0]["delivery_id"]


class PolicyNotConfiguredForAcceptance(Exception):
    """存在需要新判定的编号但尚未配置默认投递策略；整次受理不写入、不占用编号。"""


class PublishedVersionMissing(Exception):
    """策略引用的版本在库中找不到（防御性：写入时有外键且发布版本不可变）。"""

    def __init__(self, version: int):
        super().__init__(f"published version not found: {version}")
        self.version = version


# ---------- 共享的地址规范化 ----------

def normalize_address(raw: str) -> str:
    """投递地址规范化：去首尾空白、转小写（与规则键、终点邮箱的规范化一致）。"""
    return raw.strip().lower()


def normalize_checked_address(raw: str) -> str:
    """规范化并校验地址合法性；非法时抛 InvalidAddressForAcceptance（不占用编号）。"""
    address = normalize_address(raw)
    if not is_valid_address(address):
        raise InvalidAddressForAcceptance(raw, address)
    return address


@dataclass(frozen=True)
class NormalizedItem:
    """批量输入中单项的规范化结果（保持输入顺序）。"""

    index: int
    delivery_id: str
    address: str


def normalize_batch_items(items: list) -> tuple[list[NormalizedItem], list[dict]]:
    """规范化整批输入，返回 (规范化项, 全部无效项)；不在此处决定是否拒绝。

    无效项形状与既有 422 响应一致：{"index", "delivery_id", "address"}。
    """
    normalized: list[NormalizedItem] = []
    invalid_items: list[dict] = []
    for index, item in enumerate(items):
        address = normalize_address(item.address)
        if not is_valid_address(address):
            invalid_items.append(
                {"index": index, "delivery_id": item.delivery_id, "address": item.address}
            )
        normalized.append(NormalizedItem(index=index, delivery_id=item.delivery_id, address=address))
    return normalized, invalid_items


def find_duplicate_delivery_ids(normalized: list[NormalizedItem]) -> list[str]:
    """批内重复投递编号（即使地址相同也算重复）；返回排序去重后的编号列表。"""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for item in normalized:
        if item.delivery_id in seen:
            duplicates.add(item.delivery_id)
        seen.add(item.delivery_id)
    return sorted(duplicates)


# ---------- 共享的路由结果呈现 ----------

def endpoint_docs(endpoints: dict[str, list[list[str]]]) -> list[dict]:
    """把解析结果转为持久化 / 响应形状：终点按邮箱排序，路径排序后去重保留。"""
    return [
        {"mailbox": mailbox, "paths": sorted(paths)}
        for mailbox, paths in sorted(endpoints.items())
    ]


def failure_error(
    exc: ResolutionError, *, delivery_id: str, policy: dict, selection: str, version: int
) -> dict:
    """失败判定持久化的错误详情：形状同单笔 422 detail（批量项内嵌在 error 字段）。"""
    detail = exc.detail()
    detail.update({
        "delivery_id": delivery_id,
        "policy_revision": policy["revision"],
        "version": version,
        "selection": selection,
    })
    return detail


# ---------- 单笔新判定：策略快照 + 分桶 + 解析 + 持久化 ----------

def evaluate_and_persist(
    conn: sqlite3.Connection,
    *,
    delivery_id: str,
    address: str,
    policy: dict,
    rulesets: dict[int, RuleSet] | None = None,
) -> dict:
    """在受理时策略快照下为一个新编号做分桶、解析并持久化判定。须在写事务内调用。

    policy 必须是本事务内读取的当前策略（BEGIN IMMEDIATE 保证事务期间不变），
    从而同批新判定共用同一策略修订号与比例。rulesets 可传入按版本缓存的规则集
    （批量共用，同一版本只加载一次）。选中版本的拒收 / 无路由 / 成环同样持久化为
    一份 status='failed' 的判定，不自动切换版本。
    """
    rulesets = rulesets if rulesets is not None else {}
    chosen = select_version(address, policy["candidate_percent"])
    version = policy["baseline_version"] if chosen == "baseline" else policy["candidate_version"]
    ruleset = rulesets.get(version)
    if ruleset is None:
        ruleset = store.load_ruleset(conn, version)
        if ruleset is None:  # 防御：策略引用的版本必然已发布且不可变
            raise PublishedVersionMissing(version)
        rulesets[version] = ruleset
    try:
        endpoints = resolve(address, ruleset)
    except ResolutionError as exc:  # 选中版本失败：持久化失败判定，不切换版本
        return store.save_decision(
            conn, delivery_id=delivery_id, address=address, policy=policy,
            selection=chosen, version=version, status="failed", endpoints=None,
            error=failure_error(exc, delivery_id=delivery_id, policy=policy,
                                selection=chosen, version=version),
        )
    return store.save_decision(
        conn, delivery_id=delivery_id, address=address, policy=policy,
        selection=chosen, version=version, status="ok",
        endpoints=endpoint_docs(endpoints), error=None,
    )


# ---------- 单笔 / 批量共用的受理主流程 ----------

def accept_decisions(
    conn: sqlite3.Connection, items: list[tuple[str, str]]
) -> list[dict]:
    """在当前写事务内受理一组 (投递编号, 已规范化地址)，返回与输入顺序一致的判定。

    调用方须已完成地址规范化 / 合法性校验与批内重复编号检查。本函数完成
    单笔与批量共用的全部受理步骤：

    1. 一次性读取全部既有判定：编号已存在且地址一致 → 原样重放（不再读策略、
       不重新解析、不进熔断统计）；地址不一致 → 收集为冲突，全部冲突随
       DeliveryIdConflictForAcceptance 抛出，整批不写入。
    2. 只要存在新编号，读取一次当前策略快照并供全部新判定共用；未配置策略 →
       PolicyNotConfiguredForAcceptance，整批不写入（编号不被占用）。
    3. 新判定逐项稳定分桶、按缓存的规则集解析并持久化（失败同样保存、可重放）。
    4. 有新判定写入时，全部写入完成后按受理时的策略修订号仅评估一次候选侧熔断；
       纯重放批次不评估。熔断触发只影响后续请求，本批返回已持久化的原判定。
    """
    existing = store.get_decisions(conn, [delivery_id for delivery_id, _ in items])
    conflicts = [
        {
            "delivery_id": delivery_id,
            "submitted_address": address,
            "stored_address": existing[delivery_id]["address"],
        }
        for delivery_id, address in items
        if delivery_id in existing and existing[delivery_id]["address"] != address
    ]
    if conflicts:  # 既有编号绑定其他规范化地址：整批拒绝，不写入
        raise DeliveryIdConflictForAcceptance(conflicts)

    decisions: dict[str, dict] = dict(existing)
    new_items = [(did, address) for did, address in items if did not in existing]
    if new_items:
        policy = store.get_policy(conn)
        if policy is None:  # 尚无默认策略且存在新编号：整批拒绝，不占用编号
            raise PolicyNotConfiguredForAcceptance()
        rulesets: dict[int, RuleSet] = {}  # 同批共用受理时策略：规则集按版本缓存
        for delivery_id, address in new_items:
            decisions[delivery_id] = evaluate_and_persist(
                conn, delivery_id=delivery_id, address=address, policy=policy, rulesets=rulesets
            )
        # 新判定全部写入后，仅评估一次受理时修订号的候选侧熔断：
        # 触发只把候选比例置 0 并递增策略修订号（影响后续投递）；
        # 本次响应仍为上面已持久化的原判定，重放项不进入统计。
        store.maybe_trigger_circuit_breaker(conn, policy)
    return [decisions[delivery_id] for delivery_id, _ in items]
