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
| `POST /api/v1/publication/proposals` | `request_ref` 和可选 `approve`。治理者可批准或否决，省略决定则只查询。 |
| `POST /api/v1/scopes/{scope_ref}/withdraw` | `document_ref`，写入 tombstone 并从后续查询排除。 |
| `POST /api/v1/scopes/{scope_ref}/rollback` | `generation_ref`，切换回授权 scope 内未含任何撤回文献的 generation。 |

上传 `spec` 为 `kind=upload`、`filename`、`media_type`、`content_b64`（解码后 ≤16 MiB），可附 `doi`。DOI 为 `kind=doi, doi=...`，普通 URL 为 `kind=url, url=..., access_note=...`，说明有权获取及保留相应内容。收录 JSON 上限 24 MiB，其他路由 256 KiB。所有响应 `Cache-Control: no-store`。


## Fetch 原文的个人与 Shared 路径

Host 只能使用受信 Fetch 从 PMC Cloud 官方端点直接获取并校验的原始 `.txt` 和对应 metadata JSON。原文和证据分别用标准 Base64 放在 `content_b64` 与 `metadata_b64`，并传入 `kind=verified_text`、规范化的 `doi`、`pmid`、`pmcid`、`title`、`license_id`、`evidence_url`、`text_url` 与 `sha256`。禁止把摘要、模型生成文本、任意出版商 URL 或用户上传当作原文证明。签名覆盖原始请求路径和完整请求 JSON。两个收录路径都允许 24 MiB 请求 JSON，其中该原文限制 8 MiB，证据限制 256 KiB。

- 受信 Fetch 自动补充共享文献时用 `HostClient.submit_verified_shared(actor, spec, idempotency_key)`，不传 `scope_ref`。Actor 必须有 Shared 读取权限，无需授予治理权限。普通账户可通过 Shared 检索已发布内容，但不能查看其他请求人的任务回执。
- 原先的个人原文兼容路径仍用 `POST /api/v1/ingestion/requests`，只能以 `kind=verified_text` 写入该 Actor 自己的 `personal:<subject>`。这个通用接口拒绝 `verified_text` 写入 Shared，也不接受 worker 内部的 `verified_text_shared` 队列类型。
- 普通上传保持原来的权限和流程。个人文件只对本人可见，普通 Shared 上传、URL、DOI 仍需治理者手动批准。标准上传不能用声明字段绕过审批或将自己的文件自动公开。

Worker 验证官方 PMC Cloud 版本化对象路径、文本和 JSON 的 DOI/PMID/PMCID 等标识、开放、未撤稿、允许再利用的许可及精确字节校验后才索引发布。已发布版本保留官方 URL、许可与校验值。排队的临时载荷于终态清除，操作回执继续保留。原账户相同 key 和相同载荷返回同一回执。对不同请求，精确 DOI 已活跃或仍在处理中时返回 409 `already_present` 或 `already_pending`，不回传其他人的回执；Shared 在全体请求人中去重，个人路径按个人范围去重。如果入队后并行流程先行发布相同 DOI，worker 返回终态 `already_present`，不会覆盖已发布文献。

普通 RAG 只调 search。EvidenceSet 有 `status`、`index_snapshot`、`results`、`coverage` 和 `limitations`。空列表代表当前已授权且已发布集合没有命中，不等于外部没有文献。`partial` 可含可用的关键词证据。服务超时或不可达时，Host 应转换为 `unavailable`，不要伪造证据或把旧查询缓存当成新 generation。若要发现缺口后收录，由 Host 依据上面的签名鉴权与对应的原文或常规收录路径提交一次任务，并用 operation 查询，不能以模型生成的身份直接提交。下一次提问在任务 `published` 后查询新 generation。这里交付的是独立端和客户端合同，尚未改写运行中的 8043 Local RAG 调用点。
