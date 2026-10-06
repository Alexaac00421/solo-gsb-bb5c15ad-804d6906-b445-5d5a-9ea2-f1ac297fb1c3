# Alias Router — 邮件投递前别名路由判定服务

基于 **Python + FastAPI + SQLite** 的 HTTP API：维护一份路由草稿（精确地址规则 + 域兜底规则），
发布为不可变版本后，对给定收件地址做逐跳别名展开，返回去重后的终点邮箱及各自到达路径。

## 功能要点

- **规则模型**：每条规则匹配一个完整邮箱地址（`exact`）或整个域（`domain`），动作二选一：
  - `reject`：拒收（不可带目标）；
  - `route`：指向若干目标，目标分为 `alias`（内部别名，继续逐跳解析）与 `mailbox`（终点邮箱，不再递归）。
- **解析语义**：每一跳先查精确规则、再查域兜底；展开所有分支后按终点去重，返回每个终点的全部到达路径。
  任一分支**拒收 / 无路由 / 成环** → 整次解析失败（422），不返回部分结果。
- **草稿编辑**：整体替换，必须携带所见修订号（乐观锁），冲突返回 409。
  无效地址、重复规则、空目标（`route` 无目标 / `reject` 带目标）在编辑期即拒绝（422）。
- **多人协作的按键合并**：`POST /draft/changes` 按规则键提交一组 `upsert`（设置）/ `delete`（删除）
  操作并携带所见草稿修订号；键与内容沿用整体替换的同一套规范化与编辑期校验。
  所见修订落后时，**仅当本次操作涉及的每个键相对所见快照在当前草稿中均未改变**
  （存在性与完整内容一致）才把整组操作合并并产生一个新修订；否则返回 `409`，附**全部冲突键**与
  当前修订号，草稿不变。重复操作键、删除当前不存在的规则、无效规则一律**整组拒绝（422）**；
  **无法取得所见修订的快照时明确拒绝合并**（409 `revision_snapshot_unavailable`）。
  核对与写入在单个 `BEGIN IMMEDIATE` 事务内原子完成。
- **草稿修订快照**：每次草稿变更（整体替换与按键合并）都留存一份完整规则快照，
  初始空草稿为修订 `0`；`GET /draft/revisions` 与 `GET /draft/revisions/{n}` 可逐一核对。
  旧版本程序生成的数据库没有历史快照，升级后不会臆造：只有升级后新发生的变更（及当前修订上的操作）
  可合并，引用缺失快照的旧所见修订号会被明确拒绝。
- **发布**：在单个事务（`BEGIN IMMEDIATE`）内原子核对修订号，并校验**所有规则目标可解析且无环**；
  成功则生成不可变版本，失败则保持旧版本不变。
- **历史版本**：所有发布版本永久可查，按任意历史版本解析可复现当时的结果。
- **发布前预览**：`POST /preview` 在同一只读快照内对比当前草稿与某个已发布版本，
  列出投递结果有变化的入口地址与域兜底类别（附两侧完整结果）；只读，不改动草稿或历史版本。
- **默认投递策略（灰度）**：管理员用 `PUT /policy` 提交**基线版本、候选版本、候选流量比例（整数 0～100）
  与所见策略修订号**；两个版本都必须是已发布版本，在单个 `BEGIN IMMEDIATE` 事务内核验修订号并保存，
  修订号冲突（409）或引用未发布版本（422）都不会覆盖原策略。
  `POST /deliver` 只输入收件地址：按**规范化地址稳定分桶**（SHA-256，仅依赖地址本身），
  同一地址在同一策略下总选同一版本；`0%` / `100%` 分别全选基线 / 候选。
  选中版本中的**拒收 / 无路由 / 成环仍整次失败，不自动改用另一版本**；
  未配置策略时明确报错（409），**不使用草稿或最新版本兜底**。
  策略只决定“默认用哪个已发布版本”，不影响显式版本解析，草稿与历史版本也不会被策略改写。
- **幂等投递判定**：`POST /deliveries` 由调用方提交**唯一投递编号 + 收件地址**（供可能超时重试的
  投递系统使用）。首次受理时按当前默认策略选定已发布版本，把**规范化地址、策略修订号、选中版本、
  完整终点路径（或拒收 / 无路由 / 成环错误）作为一份判定持久化**并返回。
  相同编号 + 相同规范化地址的并发或后续请求**原样返回首次判定（含原错误状态）**，即使策略随后改变；
  编号相同而地址不同返回 409；**无效地址或未配置策略不占用编号**。
  `GET /deliveries/{id}` 可随时查询已持久化的判定。
- **批量投递判定**：`POST /deliveries/batch` 一次提交**非空、至多 100 笔**「投递编号 + 收件地址」，
  返回**与输入顺序一致**的逐项判定（成功含终点路径，路由失败含原错误详情及状态）。
  **整批先校验**：任一无效地址（422）、批内重复编号（422）、既有编号绑定其他规范化地址（409）、
  或尚无默认策略且存在新编号（409）→ **整批拒绝且不写入**。
  已有同编号同地址的判定**原样重放**；其余项目在**同一事务**中共用受理时的策略修订号与比例，
  分别稳定分桶、解析并持久化（路由失败仍作为判定保存）。
  新判定全部写入后**仅评估一次**该修订号的候选侧熔断（触发只影响后续请求），重放不重复计数。
- **历史投递判定核对报表**：`GET /deliveries/report` 按**带时区的起止时刻（左闭右开）与可选策略
  修订号**筛选已持久化的判定，返回按**选中版本 × 分流侧（基线/候选）× 结果（成功或失败原因）**
  分组的数量，并分页列出投递编号、规范化地址、策略修订号、选中版本、分流侧与状态或错误类型；
  已持久化的失败同样计入，同一编号只计一次。首次查询在单只读快照内固定**截至当时已提交判定的
  边界**并随 `next_cursor` 返回；后续翻页只凭游标，沿用同一筛选与边界——并发新投递不进入旧报表，
  分组总数与明细一致，页间不漏不重。空结果返回零计数与空页；无效时间窗、非法页大小、无效游标
  均明确拒绝（422）。
- **灰度自动熔断**：操作方凭当前策略修订号用 `PUT /policy/circuit-breaker` 设置**失败率阈值
  （1～100%）、最少候选判定数与观察窗口（分钟，均为正整数）**，设置针对该策略修订号生效；
  `GET /policy/circuit-breaker` 查询当前设置与全部熔断记录。每次**首次提交**投递判定持久化后，
  在同一事务内仅统计**同一策略修订号、窗口内已持久化的候选侧判定**：样本达下限且失败数占比
  达到阈值时**只触发一次**——候选比例置 `0`、策略修订号 `+1`，并记录**旧修订号、样本数、
  失败数与触发时间**。触发请求仍返回原判定，后续默认投递按基线执行；同编号重放不重复计数，
  历史判定及响应不改写。人工更新策略（或熔断本身）使修订号 `+1` 后旧设置即失效，须针对新修订
  重新设置；未配置策略、非法参数、修订号冲突均明确报错且不产生熔断。
- **投递结果回执**：投递系统取得**已持久化的成功判定**后，用
  `POST /deliveries/{id}/receipts` 按**投递编号 + 终点邮箱 + 正整数尝试序号**回传实际投递结果
  （`temporary_failure` 暂时失败 / `permanent_failure` 永久失败 / `delivered` 已送达），
  `GET /deliveries/{id}/receipts` 查询该编号**各终点的尝试记录与汇总状态**。
  **只允许成功判定中列出的终点**：未知编号（404）、原判定失败（409）、邮箱不属于该判定的终点
  （409）一律拒绝且**不产生记录**。同一终点的**同序号同结果**重试**原样返回**首次记录，
  **同序号不同结果**报 `409`；新序号必须**严格大于**该终点已记录的最大序号（允许跳号），
  **已送达 / 永久失败为终结结果**，之后不再接收任何新尝试（同序号同结果重放仍原样返回）。
  汇总：**全部终点已送达 → `delivered`；任一终点永久失败 → `failed`；其余（含无任何回执）→
  `pending`**。核对与写入在单个 `BEGIN IMMEDIATE` 事务内完成，并发回传串行化，序号与汇总一致。
  回执只增不改、与判定/策略/熔断完全解耦：既有判定查询、同编号重放内容、核对报表与灰度熔断
  统计均不受回执影响。
- **投递任务领取（终点租约）**：投递系统用 `POST /deliveries/claims` 提交**领取上限（1～100）
  与租约秒数（1～3600）**；服务把所有已持久化成功判定中“已到期且无有效租约”的终点按
  **到期时刻、投递编号、终点稳定排序**，返回至多上限项的**投递编号、终点、下一尝试序号、
  到期时刻、租约到期时刻与租约令牌**。入列规则：**无回执立即到期**（下一尝试序号为 1）；
  最新回执为 `temporary_failure` 时，于该回执时间后按 **`min(60×2^(尝试序号−1), 3600)`** 秒
  到期（下一序号 +1）；最新回执为 `delivered` / `permanent_failure` 的终点**不再入列**；
  路由失败判定没有终点，从不入列。核对与领取在单个 `BEGIN IMMEDIATE` 事务内完成：
  **并发领取不会重复分配有效租约**；到期终点可被重新领取且**旧令牌立即失效**。
  **已被不早于租约序号的回执取代的租约行视为失效**：不再阻塞入列，该终点按最新回执
  重算到期时刻与下一尝试序号（退避未到不返回，到期即可重领并轮换令牌），不会把
  已被直接回执取代的重试拖延到旧租约期满。
  凭有效令牌调 `POST /deliveries/claims/{token}/complete` 提交既有三种回执结果之一时，
  **原子写入回执并结束租约**；若既有回执接口（`POST /deliveries/{id}/receipts`）已先为该
  终点记录了不早于租约序号的新回执，**令牌失效且不得覆盖**。过期、已结束、被新回执抢先或
  未知的令牌一律 **409 `invalid_lease_token`**（附 `reason`：`expired` / `completed` /
  `superseded` / `unknown`）；参数越界 422；暂无到期项返回空列表。写入的回执与既有回执
  接口写入的是同一张表，既有回执查询、判定查询 / 重放、核对报表与熔断统计的约定完全不变。
- **按终点邮箱域名限制同时在途领取数**：管理员可设置（`PUT`）、删除（`DELETE`）并查询
  （`GET` 单项 / 列表）**终点邮箱域名的正整数在途上限**；域名**去空白、大小写归一**
  （`MAIL.Example.com` 与 `mail.example.com` 同一项），非法域名或非正上限一律 **422 拒绝
  且既有配置不变**，**未配置的域名不受限**。领取仍按既有“到期时刻 → 投递编号 → 终点”顺序
  与请求上限进行：某域名在途占用已满时**跳过其到期任务、继续领取其他域名**。占用只统计
  **未完成、未到期且未被更新回执抢先取代**的租约——租约**完成、到期或被既有回执接口抢先
  写入后立即释放名额**。**调低上限不撤销已发出的令牌**：该域名占用降到新上限以下前不再
  发放新任务；调高 / 新设 / 删除上限立即对后续领取生效。领取与配置变更都在单个
  `BEGIN IMMEDIATE` 事务内原子核对，并发下绝不超额；既有回执、令牌完成、判定与报表接口
  的请求 / 响应形状保持不变。
- **终点任务撤销**：投递操作方用 `POST /deliveries/{id}/revocation` 提交撤销原因，
  接口在单个 `BEGIN IMMEDIATE` 事务内**先查该编号是否已有撤销记录**：已有记录时
  （幂等重放）**无论本次原因是否为空、纯空白或与首次不同，都原样返回首次记录的
  原因、时间、逐终点结果与计数，不再校验或改写原因**；仅对尚无撤销记录的编号才
  把原因去空白核定——空原因返回 `422 invalid_reason` 且不留记录，非空则对该编号
  全部终点一次性核定：**已送达 / 永久失败的终点保留原回执**（逐终点结果标记
  `kept_terminal`），其余终点**标记撤销（`revoked`）并使现有租约立即失效**；
  全部终点已终结时返回 `409 nothing_to_revoke` 且**不留撤销记录**。撤销后：领取
  不再返回已撤销终点、被撤销租约的域名在途名额立即释放；直接回执的**新尝试**
  （`409 endpoint_revoked`）与**令牌完成**（`409 invalid_lease_token`，
  `reason=revoked`）对其明确拒绝；**已记录回执的同序号同结果重试仍原样返回**。
  未知编号（404）、原判定失败（409）在原因核定之前即按既有约束报错。
  `GET /deliveries/{id}/revocation` 可查询撤销结果；既有判定重放、
  历史报表与熔断统计不变，回执查询仍只反映实际尝试。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

服务监听 `0.0.0.0:8000`，SQLite 数据落在命名卷 `router-data`（容器内 `/data/router.db`），
首次启动自动建表，无需手工初始化。

也可以只用 Docker：

```bash
docker build -t alias-router .
docker run -p 8000:8000 -v router-data:/data alias-router
```

## 本地开发启动

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000   # 默认库文件 ./router.db
pytest tests/ -q                                   # 运行测试
```

## 配置（环境变量）

| 变量          | 默认值（容器内）     | 说明                 |
| ------------- | -------------------- | -------------------- |
| `ROUTER_DB`   | `/data/router.db`    | SQLite 数据库文件路径 |
| `ROUTER_HOST` | `0.0.0.0`            | 监听地址             |
| `ROUTER_PORT` | `8000`               | 监听端口             |

地址与端口：`http://localhost:8000`（Compose 已映射 8000:8000），交互式文档见 `http://localhost:8000/docs`。

## API 一览

| 方法 | 路径              | 说明                                   |
| ---- | ----------------- | -------------------------------------- |
| GET  | `/health`         | 健康检查                               |
| GET  | `/draft`          | 查看草稿（含当前修订号）               |
| PUT  | `/draft`          | 整体替换草稿，需携带所见修订号         |
| POST | `/draft/changes`  | 多人协作：按规则键合并一组 upsert/delete，需所见修订号 |
| GET  | `/draft/revisions` | 列出全部草稿修订（每次变更的快照）   |
| GET  | `/draft/revisions/{n}` | 查看某个草稿修订的完整规则快照     |
| POST | `/publish`        | 核对修订号 + 全量校验后发布不可变版本  |
| GET  | `/versions`       | 列出全部已发布版本                     |
| GET  | `/versions/{n}`   | 查看某个历史版本的完整规则             |
| POST | `/resolve`        | 按指定版本解析收件地址（显式版本，兼容） |
| POST | `/preview`        | 预览当前草稿相对基线版本的投递变化     |
| GET  | `/policy`         | 查看当前默认投递策略（未配置时 404）   |
| PUT  | `/policy`         | 配置基线/候选版本与候选比例，需策略修订号 |
| PUT  | `/policy/circuit-breaker` | 设置灰度熔断：失败率阈值 / 最少候选判定数 / 观察窗口，需策略修订号 |
| GET  | `/policy/circuit-breaker` | 查询当前熔断设置与全部熔断记录     |
| POST | `/deliver`        | 默认投递：按策略对收件地址稳定分桶解析 |
| POST | `/deliveries`     | 幂等投递：投递编号 + 地址，首次判定持久化、重试原样返回 |
| POST | `/deliveries/batch` | 批量幂等投递：一次至多 100 笔，逐项判定按输入顺序返回，整批先校验 |
| GET  | `/deliveries/report` | 历史判定核对报表：时间窗 + 分组计数 + 游标分页     |
| POST | `/deliveries/{id}/receipts` | 投递结果回执：按终点 + 正整数序号回传实际投递结果 |
| GET  | `/deliveries/{id}/receipts` | 查询各终点尝试记录（按序号升序）与汇总状态         |
| POST | `/deliveries/{id}/revocation` | 撤销成功判定中尚未终结的终点任务（无记录时需非空原因；已撤销编号重放原样返回首次记录，忽略本次原因） |
| GET  | `/deliveries/{id}/revocation` | 查询某投递编号的撤销结果（未撤销或无可撤销项时 404） |
| POST | `/deliveries/claims`        | 投递任务领取：按到期时刻/编号/终点排序领取终点任务并设租约 |
| POST | `/deliveries/claims/{token}/complete` | 凭有效租约令牌提交回执，原子写入并结束租约   |
| GET  | `/deliveries/claims/domain-limits` | 列出全部终点邮箱域名的同时在途领取数上限   |
| PUT  | `/deliveries/claims/domain-limits` | 设置 / 覆盖某终点域名的正整数在途上限（域名大小写归一） |
| GET  | `/deliveries/claims/domain-limits/{domain}` | 查询单个域名的在途上限（未配置 404）   |
| DELETE | `/deliveries/claims/domain-limits/{domain}` | 删除某域名的在途上限（幂等，删除后该域名不受限） |
| GET  | `/deliveries/{id}` | 按投递编号查询已持久化的判定（含失败） |

## 调用示例

### 1. 编辑草稿（当前修订号 0）

```bash
curl -X PUT http://localhost:8000/draft -H 'Content-Type: application/json' -d '{
  "revision": 0,
  "rules": [
    {"match_type": "exact", "match_key": "boss@corp.example.com", "action": "route", "targets": [
      {"kind": "alias",   "value": "assistant@corp.example.com"},
      {"kind": "mailbox", "value": "boss-backup@mail.example.com"}
    ]},
    {"match_type": "exact", "match_key": "assistant@corp.example.com", "action": "route", "targets": [
      {"kind": "mailbox", "value": "alice@mail.example.com"},
      {"kind": "alias",   "value": "team@corp.example.com"}
    ]},
    {"match_type": "exact", "match_key": "team@corp.example.com", "action": "route", "targets": [
      {"kind": "mailbox", "value": "alice@mail.example.com"},
      {"kind": "mailbox", "value": "bob@mail.example.com"}
    ]},
    {"match_type": "exact", "match_key": "blocked@corp.example.com", "action": "reject"},
    {"match_type": "domain", "match_key": "corp.example.com", "action": "route", "targets": [
      {"kind": "mailbox", "value": "catchall@mail.example.com"}
    ]}
  ]}'
# => {"revision": 1, "rules": [...]}
```

修订号过期时返回 `409`：`{"detail": {"error": "revision_conflict", "current_revision": 1, ...}}`。

### 1.1 多人协作：按规则键合并（POST /draft/changes）

多个维护者各自只改动自己负责的规则键时，不必整体回传整份草稿。提交一组操作，每个操作一个规则键：

- `{"op": "upsert", "match_type": ..., "match_key": ..., "action": ..., "targets": ...}`：设置一条规则
  （不存在则新增，已存在则整条覆盖）；规则字段形状与 `/draft` 的规则完全一致。
- `{"op": "delete", "match_type": ..., "match_key": ...}`：删除一条规则，**不可**携带 `action` / `targets`。

规则键（去空白、小写；`exact` 须为合法地址、`domain` 须为合法域）与规则内容沿用上面同一套
规范化与编辑期校验。请求同样携带**所见草稿修订号**：

```bash
curl -X POST http://localhost:8000/draft/changes -H 'Content-Type: application/json' -d '{
  "revision": 1,
  "changes": [
    {"op": "upsert", "match_type": "exact", "match_key": " BOSS@CORP.EXAMPLE.COM ", "action": "route",
     "targets": [{"kind": "alias", "value": "assistant@corp.example.com"}]},
    {"op": "delete", "match_type": "domain", "match_key": "old-corp.example.com"}
  ]}'
# => {"revision": 2, "rules": [...合并后的整份草稿...]}
```

**落后修订的合并判定**（事务内原子完成）：

- 若所见修订已落后，服务取出所见修订的完整快照，逐个比对本次操作涉及的键：
  该键在**所见快照与当前草稿中的完整规则一致**（两边都不存在也算一致）才算“未改变”。
  - 涉及键**全部未改变** → 整组操作合并进当前草稿，产生一个新修订（与他人不相交的改动互不阻塞）；
  - 任一涉及键已被他人改动（内容变化、被删除或被新增）→ `409`，返回**全部冲突键**与当前修订号，
    草稿保持不变，整组操作不落任何一项：

    ```json
    {"detail": {"error": "revision_conflict", "current_revision": 2,
                "conflict_keys": [{"match_type": "exact", "match_key": "boss@corp.example.com"}],
                "message": "revision conflict: 1 involved key(s) changed since the seen revision"}}
    ```

  拿到冲突后，调用方重新 `GET /draft` 拉取当前修订号，解决冲突后带新修订号重试即可。

**整组拒绝（422，不消耗修订号、草稿不变）**：

- 重复操作键（规范化后相同，即使一个 upsert 一个 delete）→
  `{"error": "duplicate_change_key", ...}`；
- 删除当前草稿中不存在的规则 → `{"error": "rule_not_found", "match_type": ..., "match_key": ...}`；
- 无效规则（非法地址 / 域、`route` 无目标、`reject` 带目标、非法目标地址等）→
  `{"error": "invalid_rule", ...}`；`changes` 为空同样 422。

**快照缺失**：所见修订号既不是当前修订、又取不到它的修订快照（如旧版程序遗留的数据库未留存该修订）
→ `409 {"error": "revision_snapshot_unavailable", "revision": ..., "current_revision": ...}`，
绝不按当前状态盲合并。

并发提交在单个 `BEGIN IMMEDIATE` 事务内完成“核对 + 写入”：不相交键的两组操作串行化后各自成功，
相交键的后到者得到冲突；任何失败都整体回滚。合并后的草稿与整体替换后的草稿完全等价，
照旧走 `/publish` 发布，再用 `/resolve`、`/deliver` 等接口解析。

### 1.2 草稿修订快照（GET /draft/revisions）

每次草稿变更（`PUT /draft` 或 `POST /draft/changes`）都产生一个修订号并留存完整规则快照，
初始空草稿为修订 `0`：

```bash
curl http://localhost:8000/draft/revisions
# => {"revisions": [{"revision": 0, "rule_count": 0, "created_at": "..."},
#                   {"revision": 1, "rule_count": 5, "created_at": "..."}, ...]}

curl http://localhost:8000/draft/revisions/1
# => {"revision": 1, "rules": [...该修订的完整规则...]}
```

未知修订号返回 `404 {"error": "draft_revision_not_found", "revision": n}`。

### 2. 发布（原子核对修订号 + 校验目标可解析、无环）

```bash
curl -X POST http://localhost:8000/publish -H 'Content-Type: application/json' -d '{"revision": 1}'
# => {"version": 1, "source_revision": 1, "rule_count": 5}
```

校验失败（如目标别名无路由、成环）返回 `422` 且旧版本不受影响：

```json
{"detail": {"error": "validation_failed", "problems": [{"seed": "...", "error": "cycle", "chain": [...]}]}}
```

### 3. 解析收件地址

```bash
curl -X POST http://localhost:8000/resolve -H 'Content-Type: application/json' \
  -d '{"address": "boss@corp.example.com", "version": 1}'
```

```json
{
  "address": "boss@corp.example.com",
  "version": 1,
  "endpoints": [
    {"mailbox": "alice@mail.example.com", "paths": [
      ["boss@corp.example.com", "assistant@corp.example.com", "alice@mail.example.com"],
      ["boss@corp.example.com", "assistant@corp.example.com", "team@corp.example.com", "alice@mail.example.com"]
    ]},
    {"mailbox": "bob@mail.example.com", "paths": [
      ["boss@corp.example.com", "assistant@corp.example.com", "team@corp.example.com", "bob@mail.example.com"]
    ]},
    {"mailbox": "boss-backup@mail.example.com", "paths": [
      ["boss@corp.example.com", "boss-backup@mail.example.com"]
    ]}
  ]
}
```

域兜底（无精确规则时）：`nobody@corp.example.com` → `catchall@mail.example.com`。

任一分支失败则整次失败（`422`，无部分结果）：

```bash
curl -X POST http://localhost:8000/resolve -H 'Content-Type: application/json' \
  -d '{"address": "blocked@corp.example.com", "version": 1}'
# => {"detail": {"error": "rejected", "address": "blocked@corp.example.com", "chain": [...]}}
```

`error` 取值：`rejected`（命中拒收规则）、`no_route`（别名无路由）、`cycle`（成环）。

### 4. 历史版本查询与复现

```bash
curl http://localhost:8000/versions        # 全部版本
curl http://localhost:8000/versions/1      # 某版本的完整规则快照
```

草稿后续修改、发布新版本后，仍可用 `"version": 1` 调 `/resolve` 复现当时的判定结果。

### 5. 发布前预览：草稿 vs 基线版本

管理员在发布前用 `POST /preview` 对比**当前草稿**与某个**已发布版本**的投递差异。
请求携带基线版本号与所见草稿修订号：

```bash
curl -X POST http://localhost:8000/preview -H 'Content-Type: application/json' \
  -d '{"base_version": 1, "revision": 2}'
```

服务在同一只读事务快照内取出两份规则集（不改动草稿或历史版本），沿用与 `/resolve`
相同的精确优先、逐跳展开、整次失败语义做全量对比，只列出**结果有变化**的项：

- `changed_addresses`：两侧全部**精确规则地址与别名目标**（并集）中结果有变化的地址。
  别名下游的变化会追溯到受影响的入口——例如 `team@` 改投后，`boss@`、`assistant@`
  这些经由它的入口也会列出。每项附 `base` / `draft` 两侧的完整结果：
  成功为 `{"status": "ok", "endpoints": [...]}`（形状同 `/resolve` 响应），
  失败为 `{"status": "failed", "error": {...}}`（形状同 `/resolve` 的 422 详情）。
- `changed_domain_fallbacks`：两侧任一处出现过域规则的每个域，排除上述具体地址后的
  **兜底地址类别**。每类用一个避开两侧精确地址与别名目标的合成示例地址
  （如 `_preview-sample@corp.example.com`）代表，同样附两侧完整结果。

比较时**路径排列顺序不计差异**（按路径集合比较），仅终点集合、到达路径或
拒收 / 无路由 / 成环结果不同才列出；无变化时两个列表均为空。
草稿尚未通过发布校验（如有环、目标无路由）时预览仍正常返回，对应入口展示失败结果。

```json
{
  "base_version": 1,
  "revision": 2,
  "changed_addresses": [
    {
      "address": "team@corp.example.com",
      "base":  {"status": "ok", "endpoints": [{"mailbox": "alice@mail.example.com", "paths": [["team@corp.example.com", "alice@mail.example.com"]]}, {"mailbox": "bob@mail.example.com", "paths": [["team@corp.example.com", "bob@mail.example.com"]]}]},
      "draft": {"status": "ok", "endpoints": [{"mailbox": "carol@mail.example.com", "paths": [["team@corp.example.com", "carol@mail.example.com"]]}]}
    }
  ],
  "changed_domain_fallbacks": [
    {
      "domain": "corp.example.com",
      "sample": "_preview-sample@corp.example.com",
      "base":  {"status": "ok", "endpoints": [{"mailbox": "catchall@mail.example.com", "paths": [["_preview-sample@corp.example.com", "catchall@mail.example.com"]]}]},
      "draft": {"status": "ok", "endpoints": [{"mailbox": "new-catchall@mail.example.com", "paths": [["_preview-sample@corp.example.com", "new-catchall@mail.example.com"]]}]}
    }
  ]
}
```

错误契约与既有接口一致：修订号过期返回 `409`（`revision_conflict`，附当前修订号），
基线版本不存在返回 `404`（`version_not_found`）。

> 上例为节选：该场景下 `boss@`、`assistant@` 等经由 `team@` 的入口也会一并出现在
> `changed_addresses` 中。

## 默认投递策略：已发布版本间的灰度

管理员可以让某个**已发布候选版本**按流量比例可控地成为默认投递结果，基线版本承接其余流量。

### 6. 配置策略（基线 / 候选 / 比例 / 策略修订号）

```bash
curl -X PUT http://localhost:8000/policy -H 'Content-Type: application/json' -d '{
  "baseline_version": 1,
  "candidate_version": 2,
  "candidate_percent": 20,
  "revision": 0
}'
```

- 只接受**已发布版本**：`baseline_version` / `candidate_version` 任一不存在（或指向草稿）→
  `422 {"error": "version_not_published", ...}`，事务回滚，原策略不变。
- `revision` 为客户端**所见策略修订号**；从未配置时修订号为 `0`，每次成功更新 `+1`。
  修订号过期返回 `409`（`revision_conflict`，附 `current_revision`），冲突不覆盖原策略。
- `candidate_percent` 为整数 `0～100`（请求体层强制），越界返回 `422`。

成功响应即当前策略：

```json
{"revision": 1, "baseline_version": 1, "candidate_version": 2, "candidate_percent": 20, "updated_at": "2026-10-04T01:16:03+00:00"}
```

查看当前策略：`GET /policy`（未配置时 `404 {"error": "policy_not_configured", "revision": 0}`）。

策略修订号与草稿修订号相互独立；改策略不会修改草稿、历史版本，也不会让任何已发布版本被改写。

### 7. 默认投递：只给收件地址

```bash
curl -X POST http://localhost:8000/deliver -H 'Content-Type: application/json' \
  -d '{"address": "boss@corp.example.com"}'
```

服务先把地址规范化（去空白、小写），再对地址本身做 SHA-256 稳定分桶（桶号 0–99）：
桶号 `< candidate_percent` 选候选，否则选基线。**同一地址在同一策略下永远选同一版本**；
调整比例只会移动分桶边界（地址随比例单调地从基线侧进入候选侧），不会来回抖动。
`0%` 时所有地址选基线，`100%` 时所有地址选候选。

```json
{
  "address": "boss@corp.example.com",
  "policy_revision": 1,
  "baseline_version": 1,
  "candidate_version": 2,
  "candidate_percent": 20,
  "selection": "candidate",
  "version": 2,
  "endpoints": [
    {"mailbox": "alice@mail.example.com", "paths": [["boss@corp.example.com", "alice@mail.example.com"]]}
  ]
}
```

`endpoints` 的形状与 `/resolve` 完全一致（原有终点与到达路径）；`selection` 为
`"baseline"` / `"candidate"`，`version` 为实际选中的版本号。

失败约定：

- **未配置策略** → `409 {"error": "policy_not_configured", ...}`；不会使用草稿或最新发布版本兜底。
- **选中版本**解析出**拒收 / 无路由 / 成环** → 与 `/resolve` 相同的 `422`
  （`error` 仍为 `rejected` / `no_route` / `cycle`，附原有 `address`、`chain`），
  **整次失败，不自动改用另一版本**；错误详情额外附 `version`、`selection`、`policy_revision`。
- 无效地址 → `422 {"error": "invalid_address", ...}`。

```bash
# 100% 候选后，命中候选拒收规则：失败，不会退回基线
curl -X POST http://localhost:8000/deliver -H 'Content-Type: application/json' \
  -d '{"address": "blocked@corp.example.com"}'
# => 422 {"detail": {"error": "rejected", ..., "policy_revision": 2, "version": 2, "selection": "candidate"}}
```

### 8. 显式版本解析保持兼容

`POST /resolve` 的请求（必须带 `version`）、成功响应与错误约定（`404 version_not_found`、
`422` 的 `rejected` / `no_route` / `cycle` / `invalid_address`）完全不变，不受任何策略影响；
草稿与历史版本同样不随策略变更而改写。需要强制指定版本复现结果时继续使用 `/resolve`。

## 幂等投递判定：超时重试安全的提交

投递系统可能因超时重试同一封邮件的路由判定。`POST /deliveries` 在 `/deliver` 的策略分桶之上
增加**调用方唯一投递编号**：首次受理时把完整判定持久化，之后相同编号的重试 / 并发请求
原样返回首次判定，绝不因策略变更或重试时机不同而给出不同结果。

### 9. 提交投递判定（投递编号 + 收件地址）

```bash
curl -X POST http://localhost:8000/deliveries -H 'Content-Type: application/json' \
  -d '{"delivery_id": "msg-2026-10-04-0001", "address": "boss@corp.example.com"}'
```

```json
{
  "delivery_id": "msg-2026-10-04-0001",
  "address": "boss@corp.example.com",
  "policy_revision": 1,
  "baseline_version": 1,
  "candidate_version": 2,
  "candidate_percent": 20,
  "selection": "candidate",
  "version": 2,
  "endpoints": [
    {"mailbox": "alice@mail.example.com", "paths": [["boss@corp.example.com", "alice@mail.example.com"]]}
  ]
}
```

响应在 `/deliver` 的字段（策略快照、`selection`、`version`、`endpoints`）之外增加 `delivery_id`；
地址同样先规范化（去空白、小写）再分桶与落库。

**重试 / 并发重放**：相同 `delivery_id` 且规范化地址相同的请求（无论并发还是稍后重试，
也无论策略随后如何调整）都返回与首次**完全相同**的响应——同样的状态码与响应体：

```bash
# 即使 PUT /policy 已把比例改为 0%，重放仍返回首次选中的版本与结果
curl -X POST http://localhost:8000/deliveries -H 'Content-Type: application/json' \
  -d '{"delivery_id": "msg-2026-10-04-0001", "address": "  BOSS@CORP.EXAMPLE.COM "}'
# => 与首次响应逐字段相同（version 仍为 2，policy_revision 仍为 1）
```

**失败判定同样持久化**：选中版本拒收 / 无路由 / 成环时，首次与重放都返回相同的 `422`
（`error` 为 `rejected` / `no_route` / `cycle`，附 `delivery_id`、`policy_revision`、
`version`、`selection`），**整次失败，不切换版本**：

```json
{"detail": {"error": "rejected", "address": "blocked@corp.example.com", "chain": ["blocked@corp.example.com"],
            "delivery_id": "msg-2026-10-04-0002", "policy_revision": 1, "version": 2, "selection": "candidate",
            "message": "blocked@corp.example.com rejected by rule"}}
```

**编号冲突**：`delivery_id` 相同而规范化地址不同 → `409`
`{"error": "delivery_id_conflict", "delivery_id": "..."}`，原判定不被改写。

**编号不被无效请求占用**：无效地址（`422 invalid_address`）与未配置策略
（`409 policy_not_configured`）都不会写入判定，修正后可用同一编号重新提交。

### 9.1 批量提交：一次提交多笔判定（POST /deliveries/batch）

投递系统一次提交多笔收件判定时用 `POST /deliveries/batch`：请求体为**非空、至多 100 笔**的
`items`（每项与单笔接口相同：`delivery_id` + `address`），响应 `results` **与输入顺序一致**，
每项含 `status`（`ok` / `failed`）与单笔判定相同的策略快照字段——成功含 `endpoints`
（`error` 为 `null`），路由失败含原错误详情 `error`（形状同单笔的 422 detail，`endpoints` 为 `null`）：

```bash
curl -X POST http://localhost:8000/deliveries/batch -H 'Content-Type: application/json' -d '{
  "items": [
    {"delivery_id": "msg-2026-10-05-0001", "address": "boss@corp.example.com"},
    {"delivery_id": "msg-2026-10-05-0002", "address": "blocked@corp.example.com"},
    {"delivery_id": "msg-2026-10-05-0003", "address": "  BOSS@CORP.EXAMPLE.COM "}
  ]}'
```

```json
{
  "results": [
    {"delivery_id": "msg-2026-10-05-0001", "status": "ok", "address": "boss@corp.example.com",
     "policy_revision": 1, "baseline_version": 1, "candidate_version": 2, "candidate_percent": 20,
     "selection": "candidate", "version": 2,
     "endpoints": [{"mailbox": "alice@mail.example.com", "paths": [["boss@corp.example.com", "alice@mail.example.com"]]}],
     "error": null},
    {"delivery_id": "msg-2026-10-05-0002", "status": "failed", "address": "blocked@corp.example.com",
     "policy_revision": 1, "baseline_version": 1, "candidate_version": 2, "candidate_percent": 20,
     "selection": "candidate", "version": 2, "endpoints": null,
     "error": {"error": "rejected", "message": "blocked@corp.example.com rejected by rule",
               "address": "blocked@corp.example.com", "chain": ["blocked@corp.example.com"],
               "delivery_id": "msg-2026-10-05-0002", "policy_revision": 1, "version": 2, "selection": "candidate"}},
    {"delivery_id": "msg-2026-10-05-0003", "status": "ok", "address": "boss@corp.example.com", "...": "..."}
  ]
}
```

受理语义与单笔接口一致，只是**整批同事务**完成：

- **整批先校验，任一不满足即整批拒绝且不写入**（编号不被占用，修正后可整体重提）：
  - 任一地址无效 → `422 {"error": "invalid_address", "invalid_items": [{"index", "delivery_id", "address"}, ...]}`；
  - 批内重复投递编号（即使地址相同）→ `422 {"error": "duplicate_delivery_id", "delivery_ids": [...]}`；
  - 既有编号绑定了其他规范化地址 → `409 {"error": "delivery_id_conflict", "conflicts": [{"delivery_id", "submitted_address", "stored_address"}, ...]}`；
  - 尚无默认策略且存在新编号 → `409 {"error": "policy_not_configured", ...}`
    （**整批均为已有判定的重放时不需要策略**，照常 200 返回）。
- **重放与新增可混合**：已有同编号同地址的项目原样重放（保持首次的策略修订号与结果）；
  新编号项目共用**受理时**的策略修订号与比例，分别稳定分桶、解析并持久化，
  路由失败（拒收 / 无路由 / 成环）仍作为判定保存，响应中该项 `status` 为 `failed`。
- **熔断只评估一次**：本批新判定全部写入后，按受理时的策略修订号评估一次候选侧熔断；
  触发只影响后续请求，本批响应仍为已持久化的原判定；重放项目不进入统计。
- 批量与单笔接口共用同一份判定存储：单笔提交过的编号在批量中按原样重放，反之亦然；
  `GET /deliveries/{id}` 与 `/deliveries/report` 对批量写入的判定同样可用。

### 10. 查询已持久化的判定

```bash
curl http://localhost:8000/deliveries/msg-2026-10-04-0001
```

```json
{
  "delivery_id": "msg-2026-10-04-0001",
  "address": "boss@corp.example.com",
  "status": "ok",
  "policy_revision": 1,
  "baseline_version": 1,
  "candidate_version": 2,
  "candidate_percent": 20,
  "selection": "candidate",
  "version": 2,
  "endpoints": [
    {"mailbox": "alice@mail.example.com", "paths": [["boss@corp.example.com", "alice@mail.example.com"]]}
  ],
  "error": null,
  "created_at": "2026-10-04T01:16:03+00:00"
}
```

`status` 为 `ok` / `failed`；失败判定的 `error` 为首次 `422` 的完整详情，`endpoints` 为 `null`。
未知编号返回 `404 {"error": "delivery_not_found", ...}`。

> 既有 `/deliver`、`/resolve` 的请求、响应与错误约定完全不变：`/deliver` 仍只做即时分桶解析，
> 不产生持久化判定；需要强制指定版本复现时继续使用 `/resolve`。

## 灰度自动熔断：候选侧失败率达阈值自动回落基线

灰度期间若候选版本出现异常（拒收 / 无路由 / 成环增多），可以让服务按**候选侧投递失败率**
自动熔断：一旦窗口内候选判定样本足够且失败占比达到阈值，候选比例自动置 `0`，
后续默认投递全部回落基线版本，无需人工值守。

### 14. 设置熔断（凭当前策略修订号）

```bash
curl -X PUT http://localhost:8000/policy/circuit-breaker -H 'Content-Type: application/json' -d '{
  "revision": 1,
  "failure_threshold_percent": 50,
  "min_candidate_samples": 10,
  "window_minutes": 5
}'
```

```json
{"policy_revision": 1, "failure_threshold_percent": 50, "min_candidate_samples": 10,
 "window_minutes": 5, "updated_at": "2026-10-04T01:20:00+00:00"}
```

- `revision` 为客户端**所见策略修订号**（即 `GET /policy` 返回的 `revision`）；设置**针对该
  策略修订号生效**。人工更新策略（`PUT /policy`）或熔断触发都会使策略修订号 `+1`，
  旧设置随之失效，须凭新修订号重新设置。
- `failure_threshold_percent` 为整数 `1～100`；`min_candidate_samples`（最少候选判定数）与
  `window_minutes`（观察窗口，分钟）均为正整数——越界返回 `422`，不落任何设置。
- **未配置策略** → `409 {"error": "policy_not_configured", ...}`；**修订号冲突** →
  `409 {"error": "revision_conflict", "current_revision": ...}`；两者都不产生任何熔断。
  核对与保存在单个 `BEGIN IMMEDIATE` 事务内完成，与并发投递提交串行化、互不覆盖。

查询当前设置与全部熔断记录：

```bash
curl http://localhost:8000/policy/circuit-breaker
```

```json
{
  "policy_revision": 1,
  "settings": {"policy_revision": 1, "failure_threshold_percent": 50,
               "min_candidate_samples": 10, "window_minutes": 5,
               "updated_at": "2026-10-04T01:20:00+00:00"},
  "breaks": []
}
```

`settings` 仅在对**当前**策略修订号有效时非空（否则为 `null`，须重新设置）；
`breaks` 按触发顺序列出每次熔断的记录。未配置策略时本接口返回
`404 {"error": "policy_not_configured", ...}`。

### 15. 触发：只统计同修订号窗口内的候选判定，只触发一次

熔断评估发生在 `POST /deliveries` **每次首次提交判定持久化之后**（同一事务内）：
仅统计**同一策略修订号、观察窗口内已持久化的候选侧判定**（含本次刚写入的判定；
基线侧判定不计入，`/deliver` 不产生持久化判定也不参与）。当
`样本数 ≥ min_candidate_samples` 且 `失败数 / 样本数 ≥ failure_threshold_percent` 时触发：

- 候选比例置 `0`、策略修订号 `+1`（与人工更新一样走同一行策略记录，并发下串行化）；
- 写入一条熔断记录：**旧策略修订号、窗口内候选样本数、失败数与触发时间**；
- **本次触发请求仍返回已持久化的原判定**（成功或失败原样返回），后续默认投递按基线执行。

```bash
# 候选版本对 blocked@ 拒收；窗口内候选失败占比达到阈值后：
curl -X POST http://localhost:8000/deliveries -H 'Content-Type: application/json' \
  -d '{"delivery_id": "msg-2026-10-04-0009", "address": "blocked@corp.example.com"}'
# => 触发请求仍返回原判定：422 {"detail": {"error": "rejected", ..., "selection": "candidate",
#                             "policy_revision": 1, "version": 2}}

curl http://localhost:8000/policy
# => {"revision": 2, "baseline_version": 1, "candidate_version": 2, "candidate_percent": 0, ...}

curl http://localhost:8000/policy/circuit-breaker
# => {"policy_revision": 2,
#     "settings": null,            # 旧设置随修订号 +1 失效，须针对新修订重新设置
#     "breaks": [{"id": 1, "policy_revision": 1, "failure_threshold_percent": 50,
#                 "min_candidate_samples": 10, "window_minutes": 5,
#                 "sample_count": 10, "failure_count": 7,
#                 "triggered_at": "2026-10-04T01:26:03+00:00"}]}

# 后续默认投递全部按基线执行
curl -X POST http://localhost:8000/deliver -H 'Content-Type: application/json' \
  -d '{"address": "blocked@corp.example.com"}'
# => 200 {"selection": "baseline", "version": 1, "policy_revision": 2, ...}
```

- **只触发一次**：触发使策略修订号 `+1`，原设置即失效，不会重复熔断；如需继续保护，
  人工调整策略后凭新修订号重新设置。
- **同编号重放不重复计数**：重放直接返回首次判定，不进入统计；历史判定及响应不改写
  （`GET /deliveries/{id}` 与重放仍返回触发前的候选侧结果）。
- 并发首次提交与人工更新策略由 `BEGIN IMMEDIATE` 串行化：同一修订号上只会有一个结果
  先生效，后到者只能看到已提交的新状态（人工更新得到 `409` 冲突或按新策略受理），
  不会互相覆盖。

## 历史投递判定核对报表：时间窗 + 分组计数 + 游标分页

运营核对历史投递判定用 `GET /deliveries/report`：按**带时区的起止时刻**（左闭右开
`[start, end)`，作用于判定的受理时刻 `created_at`）与**可选策略修订号**筛选已持久化的判定，
返回分组数量与分页明细。已持久化的失败判定同样计入；`delivery_id` 是主键，同一编号只计一次。

### 11. 首次查询：固定筛选与快照边界

```bash
curl 'http://localhost:8000/deliveries/report?start=2026-10-01T00:00:00%2B08:00&end=2026-11-01T00:00:00%2B08:00&policy_revision=1&page_size=2'
```

- `start` / `end` 必填，必须是**带时区**的 ISO 8601 时刻（`Z` 或 `+08:00` 等偏移均可，
  服务规范化为 UTC 后比较）；窗口**左闭右开**：恰好等于 `start` 的判定计入，恰好等于 `end` 的不计入。
- `policy_revision` 可选：只核对某个策略修订号下受理的判定；缺省不过滤。
- `page_size` 可选，默认 50，合法范围 1～500。

```json
{
  "window": {"start": "2026-09-30T16:00:00+00:00", "end": "2026-10-31T16:00:00+00:00", "policy_revision": 1},
  "page_size": 2,
  "total": 4,
  "groups": [
    {"version": 1, "selection": "baseline",  "outcome": "ok",       "count": 1},
    {"version": 2, "selection": "candidate", "outcome": "no_route", "count": 1},
    {"version": 2, "selection": "candidate", "outcome": "ok",       "count": 1},
    {"version": 2, "selection": "candidate", "outcome": "rejected", "count": 1}
  ],
  "items": [
    {"delivery_id": "msg-0001", "address": "shared@corp.example.com", "policy_revision": 1,
     "version": 2, "selection": "candidate", "status": "ok", "error_type": null,
     "created_at": "2026-10-04T01:16:03+00:00"},
    {"delivery_id": "msg-0002", "address": "blocked@corp.example.com", "policy_revision": 1,
     "version": 2, "selection": "candidate", "status": "failed", "error_type": "rejected",
     "created_at": "2026-10-04T01:16:03+00:00"}
  ],
  "next_cursor": "eyJ2IjoxLCJzdGFydCI6..."
}
```

- `groups` 按**选中版本 × 分流侧（`baseline` / `candidate`）× 结果**分组计数：
  成功为 `"outcome": "ok"`，失败为失败原因 `"rejected"` / `"no_route"` / `"cycle"`。
- `items` 明细含投递编号、规范化地址、策略修订号、选中版本、分流侧与状态或错误类型
  （`status` 为 `ok` / `failed`，`error_type` 仅失败时非空），按受理顺序排列。
- `total` 为筛选下的判定总数，恒等于各组计数之和；`groups`、`total` 与全部页的明细
  在**同一快照边界**上计算，三者必然一致。
- 首次查询在单只读事务内固定“**截至当时已提交判定的边界**”；空结果返回
  `"total": 0`、`"groups": []`、`"items": []`、`"next_cursor": null`。

### 12. 后续翻页：只凭游标，沿用同一筛选与边界

```bash
curl 'http://localhost:8000/deliveries/report?cursor=eyJ2IjoxLCJzdGFydCI6...'
# 翻页时允许调整页大小：...&page_size=100
```

- `next_cursor` 为 `null` 表示已到最后一页；否则凭它取下一页。
- 游标已携带首次查询的筛选（起止时刻、策略修订号）与快照边界，**翻页时不要再传
  `start` / `end` / `policy_revision`**（同传会被拒绝）；响应中的 `window` 回显实际沿用的筛选。
- **并发新投递不进入旧报表**：首次查询之后受理的判定不会出现在该游标的任何后续页，
  各页的 `groups` / `total` 也保持不变；要纳入新判定请重新发起首次查询。
- 页间按唯一单调的内部序号键集分页：不漏记、不重复；各页明细拼接即为完整筛选结果。

### 13. 明确拒绝（422）

| 场景 | 响应 |
| ---- | ---- |
| 缺 `start` / `end`、不带时区、无法解析、`start >= end` | `{"error": "invalid_time_window", ...}` |
| `page_size` 超出 1～500 | `{"error": "invalid_page_size", ...}` |
| 游标无法解码、形状 / 版本不符、与筛选参数同传 | `{"error": "invalid_cursor", ...}` |

> 该报表只读取已持久化的判定，不改动任何数据；`POST /deliveries` 的幂等判定、
> `GET /deliveries/{id}` 的按编号查询与原样重放语义完全不变。

## 投递结果回执：按终点 + 尝试序号回传实际投递结果

投递系统从 `POST /deliveries` 取得**已持久化的成功判定**（含去重后的全部终点邮箱）并实际发信后，
用回执接口回报每个终点每次尝试的真实结果。回执依附判定存在，但只增不改、与判定和熔断完全解耦。

### 16. 回传一次实际投递结果（POST /deliveries/{id}/receipts）

路径参数为投递编号；请求体给出**判定中的一个终点邮箱、正整数尝试序号与本次结果**
（邮箱与地址一样先去空白、转小写再比对）：

```bash
curl -X POST http://localhost:8000/deliveries/msg-2026-10-05-0001/receipts \
  -H 'Content-Type: application/json' \
  -d '{"mailbox": "alice@mail.example.com", "attempt_no": 1, "result": "temporary_failure"}'
```

```json
{
  "delivery_id": "msg-2026-10-05-0001",
  "mailbox": "alice@mail.example.com",
  "attempt_no": 1,
  "result": "temporary_failure",
  "created_at": "2026-10-05T03:12:00+00:00"
}
```

`result` 三选一：`temporary_failure`（暂时失败，可继续重试）、`permanent_failure`（永久失败）、
`delivered`（已送达）。

**受理前提（任一不满足即拒绝，且不产生任何记录）**：

| 场景 | 状态码 / error |
| ---- | -------------- |
| 未知投递编号（POST 与 GET 均如此） | `404 {"error": "delivery_not_found", ...}` |
| 原判定为路由失败（rejected / no_route / cycle），没有终点 | `409 {"error": "delivery_decision_failed", "decision_status": "failed", "error_type": "...", ...}` |
| 邮箱不在该成功判定列出的终点中（含属于别的投递编号的终点） | `409 {"error": "endpoint_not_allowed", "mailbox": ..., "allowed_endpoints": [...]}` |
| 邮箱格式非法 | `422 {"error": "invalid_address", ...}`；`attempt_no < 1`、非法 `result` 也为 `422`（请求体校验） |

**序号与结果规则（按每个“投递编号 × 终点”独立计算）**：

- **同序号同结果重试原样返回**：超时 / 并发重发完全相同的（序号, 结果）时，返回与首次逐字段相同
  的记录（含首次 `created_at`），不新增记录。
- **同序号不同结果报冲突**：`409 {"error": "attempt_result_conflict", "stored_result": ...,
  "submitted_result": ..., ...}`，首次记录不改写。
- **新序号严格递增**：必须大于该终点已记录的最大序号（允许跳号，如 1 之后直接 3）；
  否则 `409 {"error": "attempt_sequence_conflict", "last_attempt_no": ..., ...}`。
- **终结结果后封口**：某终点已记录 `delivered` 或 `permanent_failure` 后，任何**新序号**都被
  拒绝：`409 {"error": "endpoint_terminal", "terminal_result": ..., "terminal_attempt_no": ...,
  ...}`；该终结序号自身的同结果重放仍原样返回。
- **已撤销终点拒收新尝试**：终点任务被撤销（见章节 25–26）后，任何**新序号**都被
  `409 {"error": "endpoint_revoked", ...}` 拒绝；已记录回执的同序号同结果重试仍原样返回。

```bash
# 第 1 次暂时失败 → 第 2 次送达
curl -X POST .../receipts -d '{"mailbox":"alice@mail.example.com","attempt_no":2,"result":"delivered"}'
# 超时重发：同序号同结果，原样返回 200
curl -X POST .../receipts -d '{"mailbox":"alice@mail.example.com","attempt_no":2,"result":"delivered"}'
# 已送达后再报新尝试 → 409 endpoint_terminal
curl -X POST .../receipts -d '{"mailbox":"alice@mail.example.com","attempt_no":3,"result":"temporary_failure"}'
```

### 17. 查询各终点尝试记录与汇总状态（GET /deliveries/{id}/receipts）

```bash
curl http://localhost:8000/deliveries/msg-2026-10-05-0001/receipts
```

```json
{
  "delivery_id": "msg-2026-10-05-0001",
  "status": "delivered",
  "endpoints": [
    {"mailbox": "alice@mail.example.com", "status": "delivered", "attempts": [
      {"attempt_no": 1, "result": "temporary_failure", "created_at": "2026-10-05T03:12:00+00:00"},
      {"attempt_no": 2, "result": "delivered",         "created_at": "2026-10-05T03:15:00+00:00"}
    ]},
    {"mailbox": "bob@mail.example.com", "status": "delivered", "attempts": [
      {"attempt_no": 1, "result": "delivered", "created_at": "2026-10-05T03:12:01+00:00"}
    ]}
  ]
}
```

- `endpoints` 按判定中的终点逐一列出（顺序与持久化判定一致），`attempts` 按尝试序号升序；
  终点状态 `status`：无记录为 `"pending"`，否则取**最大序号**记录的结果
  （`temporary_failure` / `permanent_failure` / `delivered`，终结结果必为最后一条）。
- 顶层汇总 `status`：
  - **全部终点 `delivered` → `"delivered"`**；
  - **任一终点 `permanent_failure` → `"failed"`**（即使其他终点仍 pending）；
  - **其余 → `"pending"`**——**尚无任何回执时同样为 `pending"`**，各终点 `attempts` 为空。
- 未知编号 → `404 delivery_not_found`；原判定失败 → `409 delivery_decision_failed`。

### 18. 并发与既有功能的边界

- 回传的“全部核对 + 写入”在单个 `BEGIN IMMEDIATE` 事务内完成：并发回传按写锁串行化。
  同一终点同一序号的并发同结果回传收敛为一条记录（所有调用方拿到同一份内容）；
  并发不同结果时先提交者占定该序号，后到者得到 `attempt_result_conflict`；
  较大序号先提交后，迟到的较小序号得到 `attempt_sequence_conflict` 且不写入。
  查询在单只读快照内读取，尝试记录与汇总状态必然一致。
- 第 N+1 次尝试本应只在第 N 次结果已知后发起，因此正常重试压力下每个序号的并发请求都是同一内容。
- **回执不影响任何既有功能**：回执只写独立的 `delivery_attempts` 表，
  `GET /deliveries/{id}` 的判定内容、`POST /deliveries` 的同编号原样重放、
  `/deliveries/report` 报表（只统计判定）与灰度熔断的候选失败率统计（只数判定行）均完全不变。

## 投递任务领取：按终点出租约，凭令牌原子回执

投递系统按“**投递编号 × 终点邮箱**”领取实际发信任务：服务把已持久化成功判定中的终点排成
一个自动到期 / 退避的队列，领取即获得一个限时租约令牌；发信结束后凭令牌原子回执。
路由失败判定（rejected / no_route / cycle）没有终点，从不入列。

### 19. 领取到期任务（POST /deliveries/claims）

请求体给出**领取上限（整数 1～100）与租约秒数（整数 1～3600）**：

```bash
curl -X POST http://localhost:8000/deliveries/claims -H 'Content-Type: application/json' \
  -d '{"max_claims": 10, "lease_seconds": 300}'
```

服务在单个 `BEGIN IMMEDIATE` 事务内扫描全部已持久化的成功判定，挑出“**已到期且当前没有
有效租约**”的终点，按 **到期时刻 → 投递编号 → 终点邮箱** 稳定排序，返回至多 `max_claims` 项：

```json
{
  "tasks": [
    {"delivery_id": "msg-2026-10-05-1001", "mailbox": "alice@mail.example.com",
     "attempt_no": 1, "due_at": "2026-10-05T08:50:51+00:00",
     "lease_expires_at": "2026-10-05T08:55:51+00:00",
     "token": "3caeded1c2062009932119038b4552de"},
    {"delivery_id": "msg-2026-10-05-1001", "mailbox": "bob@mail.example.com",
     "attempt_no": 1, "due_at": "2026-10-05T08:50:51+00:00",
     "lease_expires_at": "2026-10-05T08:55:51+00:00",
     "token": "a0f2db102f2193d6ed56992e384805b8"}
  ]
}
```

- `attempt_no`：**下一尝试序号**——无回执时为 `1`；最新回执为 `temporary_failure`
  （序号 N）时为 `N + 1`。
- `due_at`：该终点本次的到期时刻（UTC ISO 8601）。
- `lease_expires_at`：本次租约到期时刻（领取时刻 + `lease_seconds`）；
  租约有效期内该终点不会再被分配给任何人。
- `token`：租约令牌（32 位十六进制），完成回执时使用。
- **暂无到期项** → `200 {"tasks": []}`；`max_claims` 或 `lease_seconds` 越界 / 缺失 → `422`。
- 若管理员为**终点邮箱域名**配置了同时在途领取数上限，满额域名的任务会被跳过、继续领取
  其他域名（详见下文章节 23–24）；未配置域名不受影响。

**入列与退避规则（按每个“投递编号 × 终点”独立计算，依据该终点最新一条回执）**：

| 最新回执状态 | 到期时刻 | 下一尝试序号 |
| ---- | ---- | ---- |
| 无任何回执 | 判定受理时刻（立即到期） | 1 |
| `temporary_failure`（尝试序号 N） | 该回执时刻 + `min(60×2^(N−1), 3600)` 秒 | N + 1 |
| `delivered` | **永不入列（终结）** | — |
| `permanent_failure` | **永不入列（终结）** | — |

退避序列为 60、120、240、480、960、1920、3600、3600…（**第 7 次暂时失败起封顶 3600 秒**）。
已被操作方撤销的终点任务（见章节 25–26）同样不再入列。

**被回执取代的租约不再阻塞入列**：若既有回执接口已先为某终点记录了不早于租约序号的
回执（`attempt_no >= 租约序号`），该租约行即使尚未到期也视为失效——领取时按**最新回执**
重算该终点的到期时刻与下一尝试序号：退避未到不返回，退避一到即可重领并轮换令牌，
不会把这次重试拖延到旧租约期满；若取代回执是终结结果（`delivered` / `permanent_failure`），
该终点按上表规则**不再入列**。

### 20. 并发领取与到期重领

- **不重复分配有效租约**：领取的“扫描 + 写租约”在单个 `BEGIN IMMEDIATE` 事务内完成，
  并发 / 批量领取由写锁串行化；一个未结束且未到期的租约对应的终点不会出现在任何其他
  领取结果中。紧接着的第二次领取只会返回**新的**到期项（上例再次调用返回 `{"tasks": []}`）。
  有效租约 = 未结束、未到期、且**未被不早于租约序号的回执取代**——租约序号高于该终点
  最新回执序号时（回执尚未取代租约），租约依然有效，不会被重复分配。
- **到期可重领，旧令牌立即失效**：到达 `lease_expires_at` 仍未回执的租约不再视为有效，
  该终点可以被任意调用方重新领取；重领时服务为该终点轮换租约行（**新令牌、新到期时刻**），
  此后持旧令牌的提交一律报冲突。
- **被取代租约的提前重领**：租约被直接回执取代后无需等到租约期满——该终点按最新回执
  退避到期即可重领，同样轮换令牌；旧令牌在重领前提交报 `superseded`，重领后报
  `unknown`，两种情形都**不会覆盖既有回执**。
- 领取本身**不写回执**，对判定、策略、报表、熔断没有任何影响。

### 21. 凭令牌提交回执（POST /deliveries/claims/{token}/complete）

发信结束后，用领取到的令牌提交既有三种回执结果之一（`temporary_failure` /
`permanent_failure` / `delivered`）。尝试序号取自租约，无需调用方提供：

```bash
curl -X POST http://localhost:8000/deliveries/claims/3caeded1c2062009932119038b4552de/complete \
  -H 'Content-Type: application/json' -d '{"result": "temporary_failure"}'
```

```json
{"delivery_id": "msg-2026-10-05-1001", "mailbox": "alice@mail.example.com",
 "attempt_no": 1, "result": "temporary_failure", "created_at": "2026-10-05T08:51:03+00:00"}
```

服务在单个 `BEGIN IMMEDIATE` 事务内**原子写入回执（`delivery_attempts`，与既有回执接口
同一张表、同一套只增不改约定）并结束租约**：

- 写入成功后该终点按结果重新排队：`temporary_failure` 按上表退避后可再次被领取
  （下一序号 +1）；`delivered` / `permanent_failure` 终结，不再入列。
- 回执立刻可通过既有接口查询：`GET /deliveries/{id}/receipts` 汇总随之更新；
  顶层汇总规则（全部送达 → delivered、任一永久失败 → failed、其余 → pending）不变。

**令牌失效一律 409 `invalid_lease_token`**，响应 `detail.reason` 区分：

| reason | 含义 |
| ---- | ---- |
| `expired` | 租约已到期（终点可能已被重新领取给他人）；不写入回执 |
| `completed` | 该令牌已用于提交过一次回执；不重复写入 |
| `superseded` | 既有回执接口已先为该终点记录了不早于租约序号的新回执；**令牌失效且不得覆盖** |
| `revoked` | 终点任务已被操作方撤销（章节 25–26）；**既有租约立即失效且不得再写入回执** |
| `unknown` | 令牌不存在（含到期重领后被轮换掉的旧令牌） |

```json
{"detail": {"error": "invalid_lease_token", "reason": "superseded",
            "message": "a newer receipt for this endpoint was recorded first; the lease is stale and must not overwrite it"}}
```

`result` 非法（不在三种取值内）→ `422`。

### 22. 与既有回执接口的关系

- `POST /deliveries/{id}/receipts`（按投递编号 + 终点 + 序号直接回执）与
  `GET /deliveries/{id}/receipts` 的请求、响应、序号 / 终结 / 重放约定**完全不变**。
- 两条写入路径共享同一张 `delivery_attempts` 表：若直接回执接口在租约结束前先记录了
  该终点的新尝试（序号 ≥ 租约序号），持令牌的提交得到 `409 superseded`，已有回执绝不被覆盖；
  反之，正常“领取 → 发信 → 凭令牌回执”的流程写入的记录，与直接回执写入的在查询侧不可区分。
- 领取与租约完成**不影响**判定查询（`GET /deliveries/{id}`）、同编号原样重放、
  核对报表（`GET /deliveries/report`，只统计判定）与灰度熔断统计（只数判定行）。

典型轮询循环（伪代码）：

```bash
while true; do
  for task in $(POST /deliveries/claims '{"max_claims":20,"lease_seconds":300}'.tasks); do
    result=$(send_mail "$task.delivery_id" "$task.mailbox" "$task.attempt_no")
    POST "/deliveries/claims/$task.token/complete" "{\"result\":\"$result\"}" \
      || handle_409_conflict   # expired/completed/superseded：放弃本轮，等待下一次领取
  done
  sleep 5
done
```

### 22.1 本轮变动：被直接回执取代的租约不再阻塞重试（漏领修复）

**问题**：终点领到长期租约（如 3600 秒）后，投递系统绕过租约、经
`POST /deliveries/{id}/receipts` 先写入了**同序号的 `temporary_failure`**——原令牌随即
失效（提交报 `superseded`），但旧的领取逻辑只按“未结束且未到期”判断租约有效性，
即使该回执的退避时间已到，领取仍被这条未到期的租约行挡住而返回空列表，
重试被无谓地延迟到旧租约期满。

**修复**：领取时把**已被不早于租约序号的回执取代**的租约行视为失效，不再阻塞入列；
该终点的到期时刻与下一尝试序号一律按**最新回执**重算。行为要点：

- 退避未到时仍不返回（不会因为租约失效而提前发任务）；
- 退避一到即可重领，重领照常**轮换令牌**（新令牌、新到期时刻）；
- 旧令牌提交仍按既有冲突处理：重领前报 `superseded`、重领（行轮换）后报 `unknown`，
  两种情形都**不会覆盖既有回执**；
- 取代回执为 `delivered` / `permanent_failure`（终结）时，该终点**不再入列**；
- 有效租约不重复分配、域名在途上限（被取代的租约本就不占名额，重领后重新占用）
  与撤销拦截的行为完全不变。

调用示例（沿用上文已提交的 `msg-2026-10-05-1001` 判定）：

```bash
# 1) 领到 3600 秒长期租约（attempt_no = 1）
curl -X POST http://localhost:8000/deliveries/claims -H 'Content-Type: application/json' \
  -d '{"max_claims": 10, "lease_seconds": 3600}'
# => {"tasks": [{"delivery_id": "msg-2026-10-05-1001", "mailbox": "alice@mail.example.com",
#      "attempt_no": 1, "token": "3caeded1…", ...}]}

# 2) 投递系统绕过租约，直接回执同序号暂时失败（原令牌随即失效）
curl -X POST http://localhost:8000/deliveries/msg-2026-10-05-1001/receipts \
  -H 'Content-Type: application/json' \
  -d '{"mailbox": "alice@mail.example.com", "attempt_no": 1, "result": "temporary_failure"}'

# 3) 旧令牌提交 → 409 superseded，既有回执不被覆盖
curl -X POST http://localhost:8000/deliveries/claims/3caeded1…/complete \
  -H 'Content-Type: application/json' -d '{"result": "delivered"}'
# => 409 {"detail": {"error": "invalid_lease_token", "reason": "superseded", ...}}

# 4) 该回执的退避（60 秒）一到——不必等旧租约的 3600 秒期满——即可重领：
#    下一尝试序号 2，令牌轮换
curl -X POST http://localhost:8000/deliveries/claims -H 'Content-Type: application/json' \
  -d '{"max_claims": 10, "lease_seconds": 300}'
# => {"tasks": [{"delivery_id": "msg-2026-10-05-1001", "mailbox": "alice@mail.example.com",
#      "attempt_no": 2, "token": "a0f2db10…", ...}]}
```

## 按终点邮箱域名限制同时在途领取数

为避免某个接收域（如某家邮件服务商）被同时打太多发信连接，管理员可以按**终点邮箱的域名**
配置“**同时在途领取数**”（未结束、未到期、未被更新回执取代的租约数）上限。未配置的域名
不受限；上限只影响“领取”这一步，**不影响判定、回执、报表与已发出的令牌**。

### 23. 域名在途上限：设置 / 查询 / 删除

**设置或覆盖**（`PUT /deliveries/claims/domain-limits`）：请求体给出域名与正整数上限。
域名自动**去空白、转小写**，因此 `MAIL.Example.com` 与 `mail.example.com` 是同一项；
非法域名（不是合法域名字面量）或非正整数上限一律 `422 invalid_domain` / 请求体校验拒绝，
**任何既有配置都不改变**。

```bash
# mail.example.com 最多同时在途 2 个领取任务
curl -X PUT http://localhost:8000/deliveries/claims/domain-limits \
  -H 'Content-Type: application/json' \
  -d '{"domain": "mail.example.com", "max_in_flight": 2}'
```

```json
{"domain": "mail.example.com", "max_in_flight": 2, "updated_at": "2026-10-05T09:02:11+00:00"}
```

**查询单项**：`GET /deliveries/claims/domain-limits/mail.example.com`（域名同样大小写归一；
非法域名 `422`，**未配置返回 `404 domain_limit_not_found`**）。

**列出全部**（按域名升序；从未配置时为空列表）：

```bash
curl http://localhost:8000/deliveries/claims/domain-limits
```

```json
{"limits": [
  {"domain": "mail.example.com", "max_in_flight": 2, "updated_at": "2026-10-05T09:02:11+00:00"}
]}
```

**删除**（删除后该域名恢复不受限；接口幂等，删除未配置项也返回 `200`，以 `deleted` 区分）：

```bash
curl -X DELETE http://localhost:8000/deliveries/claims/domain-limits/mail.example.com
```

```json
{"domain": "mail.example.com", "deleted": true, "max_in_flight": null}
```

### 24. 上限如何作用于领取

- 领取仍完全遵循第 19 节的**入列 / 退避规则与“到期时刻 → 投递编号 → 终点”排序**，
  `max_claims` 仍是本次返回的总上限。差别只是在按顺序发放时，逐个核对该任务终点域名的
  在途占用：**已满额的域名 → 跳过这一项，继续往后领取其他域名**，直到凑满 `max_claims`
  或候选耗尽。未配置上限的域名永远不跳过。
- **占用名额的租约**必须同时满足：`completed_at IS NULL`（未凭令牌完成）、
  `lease_expires_at > 当前时刻`（未到期）、`delivery_attempts` 中尚不存在
  序号 ≥ 租约序号的回执（未被既有回执接口抢先取代）、且所属终点**未被撤销**。
- **名额在下列时刻立即释放**（无需任何额外操作，下次领取即可见）：
  1. 凭令牌 `POST /deliveries/claims/{token}/complete` 成功写入回执（租约结束）；
  2. 到达 `lease_expires_at` 仍未回执（租约到期，该终点可被重新领取）；
  3. 投递系统绕过租约、直接经 `POST /deliveries/{id}/receipts` 先写入了不早于租约序号的
     新回执（旧令牌随即变为 `superseded`，不再占用名额）；
  4. 终点任务被撤销（既有租约随即失效，不再占用名额；见章节 25–26）。
- **调低上限不撤销已发出的令牌**：例如上限从 3 调到 1 时已经有 2 个在途，这 2 个令牌依然
  有效、可正常完成；只是占用降到 1 以下之前，该域名不再发放任何新任务。
  **调高、新设或删除上限立即对后续领取生效**。
- **原子性**：领取的“扫描占用 + 核对上限 + 写租约”与配置的“写上限”都在单个
  `BEGIN IMMEDIATE` 事务内完成，由 SQLite 写锁互相串行化：并发的多次领取与并发的配置
  变更无论怎样交错，任一域名在任一时刻的在途租约都不会超过其当时生效的上限。
- 域名上限只读取 `domain_claim_limits` 并统计 `endpoint_leases`，**不触碰判定、回执、
  策略、报表与熔断表**；既有接口的请求 / 响应形状、错误码与重放 / 汇总语义完全不变。

举例：`mail.example.com` 上限为 1，队列中按到期顺序有
`msg-a → alice@mail.example.com`、`msg-a → bob@mail.example.com`、
`msg-b → carol@mail.example.com`、`msg-c → dave@other.example.net`（其他域，未配置上限），
则一次领取（`max_claims=10`）返回 `alice` 与 `dave`——`bob`、`carol` 因同域已满被跳过：

```json
{"tasks": [
  {"delivery_id": "msg-a", "mailbox": "alice@mail.example.com", "attempt_no": 1, "...": "..."},
  {"delivery_id": "msg-c", "mailbox": "dave@other.example.net", "attempt_no": 1, "...": "..."}
]}
```

`alice` 的令牌被完成（或其租约到期 / 被直接回执抢先）后再次领取，才会按到期顺序发出
该域名的下一个任务（上例中为 `bob`）。

## 终点任务撤销：一次性撤销尚未终结的终点任务

投递操作方可以撤销某个**已持久化成功判定**中尚未终结的终点任务（例如用户退订、
内容撤回）：一次操作在单个 `BEGIN IMMEDIATE` 事务内核定该编号**全部终点**——
已送达 / 永久失败的终点保留原回执，其余终点标记撤销并使现有租约立即失效。
撤销与领取、回执并发时按事务提交顺序裁决：**撤销提交后不再发出新租约、不再写入新回执**。

### 25. 撤销（POST /deliveries/{id}/revocation）

请求体给出撤销原因。**仅当该编号尚无撤销记录时**，原因才会被去空白并要求非空；
已撤销编号的重放请求原样返回首次记录，本次原因（即使为空或不同）被完全忽略：

```bash
curl -X POST http://localhost:8000/deliveries/msg-2026-10-05-1001/revocation \
  -H 'Content-Type: application/json' -d '{"reason": "  用户退订，停止投递  "}'
```

```json
{
  "delivery_id": "msg-2026-10-05-1001",
  "reason": "用户退订，停止投递",
  "revoked_at": "2026-10-05T10:02:11+00:00",
  "revoked_count": 1,
  "endpoints": [
    {"mailbox": "alice@mail.example.com", "outcome": "kept_terminal",
     "terminal_result": "delivered", "terminal_attempt_no": 2},
    {"mailbox": "bob@mail.example.com", "outcome": "revoked"}
  ]
}
```

- `endpoints` 按判定中的终点顺序给出**逐终点结果**：`kept_terminal` 表示该终点
  已有终结回执（已送达 / 永久失败），**原回执保留**（附终结结果与序号）；
  `revoked` 表示该终点（无回执或最新为暂时失败）**已标记撤销**，其现有租约立即失效。
- **同编号再次撤销原样返回首次操作**的逐终点结果、时间、原因与计数——事务先查
  记录，因此**无论本次提交的原因是空串、纯空白还是与首次不同，都不再校验或改写
  原因**（也不受长度限制），不重复写入；`GET /deliveries/{id}/revocation` 返回
  同一记录。并发撤销按事务提交顺序收敛为同一份首次记录。
- **全部终点已终结** → `409 {"error": "nothing_to_revoke", "endpoints": [...]}`
  （附各终点终结结果），**不留撤销记录**（之后 `GET` 仍 404，再次撤销仍是 409）。
- **明确报错且不留记录**：未知编号 → `404 delivery_not_found`；原判定失败 →
  `409 delivery_decision_failed`（两者在原因核定之前即返回）；**尚无撤销记录时**
  原因去空白后为空 → `422 invalid_reason`。

### 26. 撤销后的行为与查询

- **领取**：`POST /deliveries/claims` 不再返回已撤销终点；被撤销租约占用的域名
  在途名额**立即释放**（其他投递的同域任务随即可以领取）。
- **直接回执**：对己撤销终点的**新尝试**一律
  `409 {"error": "endpoint_revoked", "revoked_at": ..., "revocation_reason": ...}`；
  **已记录回执的同序号同结果重试仍原样返回**（同序号不同结果仍为
  `attempt_result_conflict`）。被保留的终结终点不受影响，仍按 `endpoint_terminal` 封口。
- **令牌完成**：撤销前已发出的租约令牌调
  `POST /deliveries/claims/{token}/complete` 得到
  `409 {"error": "invalid_lease_token", "reason": "revoked"}`，不写入回执。
- **查询**：`GET /deliveries/{id}/revocation` 返回首次撤销记录（形状同上）；
  未撤销（含“全部终结无可撤销项”未留记录）→ `404 revocation_not_found`，
  未知编号 → `404 delivery_not_found`。
- **既有功能不变**：判定查询与同编号重放（`GET /deliveries/{id}`、
  `POST /deliveries`）、历史报表（`GET /deliveries/report`）与熔断统计不受撤销影响；
  `GET /deliveries/{id}/receipts` 仍只反映实际尝试（撤销本身不产生回执）。

```bash
curl http://localhost:8000/deliveries/msg-2026-10-05-1001/revocation
# => {"delivery_id": "msg-2026-10-05-1001", "reason": "用户退订，停止投递",
#     "revoked_at": "2026-10-05T10:02:11+00:00", "revoked_count": 1, "endpoints": [...]}
```

## 项目结构

```
app/
  config.py    # 环境变量配置
  db.py        # SQLite 连接、建表、BEGIN IMMEDIATE 事务、只读快照事务
  routing.py   # 地址校验、规则集、逐跳解析、发布前全量校验
  traffic.py   # 默认投递的稳定分桶：规范化地址 → 0–99 桶 → 基线/候选
  preview.py   # 发布前预览：草稿 vs 基线版本的投递差异对比（纯内存，不落库）
  reporting.py # 核对报表：带时区时间窗（左闭右开）校验、页大小校验、翻页游标编解码
  models.py    # 请求模型与编辑期校验（无效地址/重复规则/空目标/按键合并操作组/投递回执/任务领取/撤销原因）
  store.py     # 草稿、修订快照、发布版本、默认投递策略、幂等投递判定、投递回执、投递任务租约（含按终点域名的在途领取上限）、终点任务撤销、核对报表与灰度熔断的持久化与查询
  main.py      # FastAPI 路由（/draft、/draft/changes、/resolve、/preview、/policy、/policy/circuit-breaker、/deliver、/deliveries、/deliveries/batch、/deliveries/report、/deliveries/{id}/receipts、/deliveries/{id}/revocation、/deliveries/claims、/deliveries/claims/domain-limits 等）
tests/         # pytest：API 全流程 + 解析单元测试 + 发布前预览 + 灰度策略 + 幂等投递判定 + 批量投递判定 + 投递回执 + 任务领取与租约 + 终点域名在途上限 + 终点任务撤销 + 按键合并与修订快照 + 核对报表 + 灰度自动熔断
Dockerfile / docker-compose.yml
```
