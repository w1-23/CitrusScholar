# 8043 Host 握手合同

Selfcorpus 不接受浏览器或模型自报身份。Host 应从真实登录会话构造 `Actor(subject, projects, shared, governor)`，用当前项目成员权限填充 `projects`（`read` 或 `write`），再调用 `corpus.host_client.HostClient`。稳定标识长度 1–128，字符限英数及 `. _ @ -`。项目权限被收回后，Host 必须在后续签名中删除对应项目。收录授权在入队时判定，已受理的任务不会因 Host 后续修改成员关系而隐式取消。

```python
from corpus.auth import Actor
from corpus.host_client import HostClient

client = HostClient(signing_secret, 'http://127.0.0.1:8061')
actor = Actor(session.user_ref, session.project_permissions,
              shared=session.can_read_shared, governor=session.can_govern_corpus)
# `selection` is a requested scope, not a grant.
evidence = client.search(actor, question, {'personal': True, 'shared': False})
```

`GET /api/v1/health` 未签名且不含项目或用户数据。其余请求需要 `X-Corpus-Timestamp`（UTC epoch 整秒）、`X-Corpus-Claims`（无 `=` 的 base64url JSON）及 `X-Corpus-Signature`（小写 SHA-256 HMAC 十六进制）。对签名串逐行拼接且末尾无换行。内容依次为 `selfcorpus-hmac-v1`、大写 HTTP 方法、原始 origin-form 路径、时间戳、claims、原始 body 的 SHA-256。时钟允许 ±60 秒，窗口不提供防重放，不应将这些头记录到日志。收录和治理决定具备幂等处理。

| 方法和路径 | 说明 |
|---|---|
| `POST /api/v1/ingestion/requests` | `spec`、`scope_ref`、`idempotency_key`，返回 HTTP 202 和原 operation。相同键、不同输入返回 409。 |
| `POST /api/v1/ingestion/verified-shared` | `spec`、`idempotency_key`，固定范围 `shared`，返回 HTTP 202 和 operation。需要已获准读取 Shared 的签名 Actor。 |
| `GET /api/v1/ingestion/requests/{request_ref}` | 任务状态、尝试次数、阶段时间、错误分类及输出引用。只有请求人或 Shared 治理者可看。 |
| `POST /api/v1/retrieval/search` | `query`、`scope`，可选 `filters`（`doi/year/document_ref`）和 `limit`（1–50）。有任何未授权 project/scope 请求时报 403。 |
| `GET /api/v1/documents/{document_ref}` | 返回当前活跃且允许读取的文献版本和来源。可加 `/chunks` 查看切片。 |
| `GET /api/v1/index-generations/{generation_ref}` | 查看 generation 摘要及是否活跃，不回传老快照正文。 |
| `GET /api/v1/publication/proposals/{request_ref}` | 查看 Shared 请求和来源，供请求人和治理者追踪。 |
| `POST /api/v1/publication/proposals` | `request_ref` 和可选 `approve`。历史待处理记录只允许治理者否决，省略决定则只查询。 |
| `POST /api/v1/scopes/{scope_ref}/withdraw` | `document_ref`，写入 tombstone 并从后续查询排除。 |
| `POST /api/v1/scopes/{scope_ref}/rollback` | `generation_ref`，切换回授权 scope 内未含任何撤回文献的 generation。 |

上传 `spec` 为 `kind=upload`、`filename`、`media_type`、`content_b64`（解码后 ≤16 MiB），可附 `doi`。DOI 为 `kind=doi, doi=...`，普通 URL 为 `kind=url, url=..., access_note=...`，说明有权获取及保留相应内容。收录 JSON 上限 24 MiB，其他路由 256 KiB。所有响应 `Cache-Control: no-store`。


## Fetch 原文的个人与 Shared 路径

Host 只能使用受信 Fetch 从 PMC Cloud 官方端点直接获取并校验的原始 `.txt` 和对应 metadata JSON。原文和证据分别用标准 Base64 放在 `content_b64` 与 `metadata_b64`，并传入 `kind=verified_text`、规范化的 `doi`、`pmid`、`pmcid`、`title`、`license_id`、`evidence_url`、`text_url` 与 `sha256`。禁止把摘要、模型生成文本、任意出版商 URL 或用户上传当作原文证明。签名覆盖原始请求路径和完整请求 JSON。两个收录路径都允许 24 MiB 请求 JSON，其中该原文限制 8 MiB，证据限制 256 KiB。

- 受信 Fetch 自动补充共享文献时用 `HostClient.submit_verified_shared(actor, spec, idempotency_key)`，不传 `scope_ref`。Actor 必须有 Shared 读取权限，无需授予治理权限。普通账户可通过 Shared 检索已发布内容，但不能查看其他请求人的任务回执。
- 原先的个人原文兼容路径仍用 `POST /api/v1/ingestion/requests`，只能以 `kind=verified_text` 写入该 Actor 自己的 `personal:<subject>`。这个通用接口拒绝 `verified_text` 写入 Shared，也不接受 worker 内部的 `verified_text_shared` 队列类型。
- 普通上传保持原来的权限和流程。个人文件只对本人可见，新的 Shared 普通上传、URL、DOI 一律拒绝。历史待处理回执仍可读和拒绝，不能经审批发布。

Worker 验证官方 PMC Cloud 版本化对象路径、文本和 JSON 的 DOI/PMID/PMCID 等标识、开放、未撤稿、允许再利用的许可及精确字节校验后才索引发布。已发布版本保留官方 URL、许可与校验值。排队的临时载荷于终态清除，操作回执继续保留。原账户相同 key 和相同载荷返回同一回执。对不同请求，精确 DOI 已活跃或仍在处理中时返回 409 `already_present` 或 `already_pending`，不回传其他人的回执；Shared 在全体请求人中去重，个人路径按个人范围去重。如果入队后并行流程先行发布相同 DOI，worker 返回终态 `already_present`，不会覆盖已发布文献。

Host RAG 只调用下方统一 EvidenceSet 接口。旧 search 只用于已有独立兼容调用方。EvidenceSet 有 `status`、`index_snapshot`、`results`、`coverage` 和 `limitations`。空列表代表当前已授权且已发布集合没有通过相关性校验的切片，即使存在活跃文献也不回退到最近邻；不等于外部没有文献。完整 DOI（含 DOI resolver URL）或 document_ref 查询仍可按身份取回文献切片，但同样受 scope、活跃 generation 和可选 filters 限制。对普通主题查询，`doi`、`document_ref` 和 `year` 仅缩小候选范围，不构成主题相关性证据。`partial` 可含可用的关键词证据。服务超时或不可达时，Host 应转换为 `unavailable`，不要伪造证据或把旧查询缓存当成新 generation。若要发现缺口后收录，由 Host 依据上面的签名鉴权与对应的原文或常规收录路径提交一次任务，并用 operation 查询，不能以模型生成的身份直接提交。下一次提问在任务 `published` 后查询新 generation。这里交付的是独立端和客户端合同，尚未改写运行中的 8043 Local RAG 调用点。

## 统一 EvidenceSet v1（0.1.4）

Host 现有可信签名 claims 可新增 `base_sources`，只允许明确授权的五个 `cqa_*` 别名；缺省无旧库权限。所有已批准账号按 Host 授权读取 `shared`；Personal 只归本人，Project 必须提供当下的真实成员授权。签名方法、原始请求目标、正文及 ±60 秒规则不变。

`POST /api/v1/retrieval/evidence-set` 接受 `query`、`sources:[{source_id,topics?}]`、可选 `evolution_scope:{personal,shared,projects}` 和 `filters:{doi,year,document_ref}`，并以 `budget:{max_candidates,max_results,max_chars,timeout_ms}` 统一分配。来源为 `cqa_literature`、`cqa_methods`、`cqa_deng_book`、`cqa_translated`、`cqa_ucr_varieties` 或 `evolution`。无权限的选择在查询前返回 403。Evolution 不支持旧库主题。返回全请求的 `index_snapshot`、`source_outcomes`、`budget_used` 及 `results`，`empty` 只表示请求通道完整且无合格证据，`partial` 可携带可用证据与原因。预算未用部分不重复分配，保留确定性跨来源合并顺序。

`POST /api/v1/retrieval/evidence/read` 接受 `evidence_ref, offset?, max_chars?`，需新签名和相同账号的当下授权。`exact` 摘录是精确段落从 Unicode 偏移 0 开始的原样前缀。`passage_hash` 校验完整可读段落，`source_hash` 只表示已知原始文件。`span/page` 对旧库是可验证重建 Markdown 的绝对字符区间；`offset/next_offset` 始终相对于段落。Evolution 读取已存的校验段落，未声称是原始文件 Markdown 字符范围。历史已发布固定代次仍须通过 ACL 和撤回检查，找不到、越权及校验失败均返回 404。本端点无检索回退。

在独立服务为 `SELFCORPUS_LEGACY_ROOT` 配置旧数据根，并以平台只读方式挂载。不会用隔离旧 Shared SQLite 或 Host 数据库，不建新旧索引，不批量重算已有向量。负责运行的 Selfcorpus 自有数据目录仍需可写以支持独立收录与不透明证据映射。

旧库混合检索使用 `SELFCORPUS_LEGACY_DENSE=1`（或 `serve --legacy-dense`），需要 `[legacy]` 固定依赖 LanceDB 0.37.1、PyArrow 26.0.1，以及带 CPUExecutionProvider 的 ONNX Runtime。旧库认证固定 CPU；Evolution 仍可按既有 CPU/CUDA 配置运行。`SELFCORPUS_MODEL_DIR` 同时提供校验过的独立 E5 文件供两种池化使用。旧库查询使用未单位化的模型输出维持 flat L2 排名，两个已有 IVF 表按其原 cosine 指标；Evolution 延续单位化向量。启动校验不通过时拒绝启用混合模式。未启用时旧库只报告有证据的词法结果与 `legacy_dense_unverified`，不能当作已验证混合部署。
旧库向量候选仍须有段落词法佐证，不把跨语言近邻分数本身当作证据。无可验证充分佐证的跨语言命中暂不自动引用。
