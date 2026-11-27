# 架构、溯源与旧库审计

## Phase 0 只读事实

| 资产 | 审计结果 | 新系统处理 |
|---|---|---|
| 旧 `runtime/citrusqa/state/data` | 5 类 `chunks.jsonl`，另有元数据索引和 LanceDB active manifest。活跃代 `g20261028T165637Z_851926a9` 自报 126,943 chunks、5 表。 | 原始数据、索引与历史快照保持不变，不自动导入。0.1.4 可挂载已核定的五库代次仅读取，并验证 manifest、registry、分块文件和 Lance 内容。 |
| `runtime/8043-isolated-corpus` | 只读快照可见 1 intake、5 versions、1 publication、2 bibliography 记录。历史迁移的 4 个来源均标记 `needs_review`。 | 仅作为目录和迁移边界证据。若以后迁移，应逐条确认来源、许可、目标 scope 并重新解析/编码；不得自动变 Shared。 |
| `work/five-repos-host` | 旧 `packages/citrus_corpus` 用目录/SQLite、人工 rights grant、LanceDB generations；旧 `src.engine.embedder` 依赖 Host 配置和硬件单例。 | 不复用旧自进化流程、Host 配置或运行时。新 API/Worker 直接由独立包启动。 |
| E5 缓存 | `qdrant/multilingual-e5-large-onnx` revision `66076b8dc6e367337e3e90e6fb309fb0f3addaf6`，1024 维，旧约定分别用 `query:` 和 `passage:`。 | 已在 Selfcorpus 内复制同 revision 到 `.models`，校验六个文件，实际权重使用不同 inode，旧目录不再是运行时依赖。不要假设旧向量数值、归一化和新索引完全可互换。 |

## 新数据和事务

`requests/events` 记录收录幂等键、租约、阶段和回执。Worker 在 SQLite 写事务外获取原料、解析和计算向量。写入事务内保存版本、解析、切片、向量，构建新成员集合，检查映射、计数和检索样本，再切换 `active`。任务最终回执和指针一起提交。暂未取得全文的记录仅入目录，不能被 RAG 当作全文使用。Shared 只接受通过可信 Fetch 和全文质量校验的公开原文，合格即自动发布。旧 pending_review 只保留回执查询和拒绝，不可再发布。

文献身份在 scope 内稳定，优先用规范 DOI。没有 DOI 的 URL 使用来源 URI，无 DOI 的上传以内容哈希区分；不同内容不因文件重名而错误合并为一个文献。每个版本保留实际来源、获取时间、许可或访问说明、原料字节和 SHA-256。独立解析记录保存 Markdown、parser 版本、结构化块、页码线索和质量诊断。Chunk 引用解析版本、标题路径、页范围和估算 token 数。正文按章节、段落、表格及大小切分，引用列表不作为正文候选。

检索在同一 SQLite 读快照中解析授权 scope 的活跃指针，关键词和向量候选均受 generation 及元数据条件约束。词项与精确余弦候选必须先通过切片相关性校验，之后才进入倒数排名融合和可选 reranker。现有 FTS 索引仅用于召回，验证使用切片正文，避免标题、章名或单个汉字为整篇文献提供误命中。独立向量证据只对经过实测的 E5 模型签名启用。同文字体系的查询还需正文词项佐证。只有完整标识符查询按身份直接取回，无需运行向量模型。相关性判断不改变索引、schema 或模型快照。没有跨请求结果缓存；撤回写 tombstone 并生成排除该文献的新 snapshot。历史快照仍可回滚，但任何包含 tombstone 的快照都不能激活。备份中的历史原料并不会被撤回操作物理清除。

## 生产容量边界

SQLite 是单一写数据库，生成新快照复制文献引用而不重写所有向量。精确向量排序会扫描经过权限和元数据过滤的候选。数据库中的快照、原文和向量目前不自动回收。高查询并发、大规模全库迁移前需用代表性语料验证吞吐和延迟，并按 VectorIndex 接口替换后端（如支持预过滤的 ANN）。网络 Fetch 取明确授权的单个 URL 及可信 DOI 开放全文。内置 crawler 可做有页数/深度上限的同源发现，只报告候选，不做不限范围的链接跟随或自动授权。

## 统一检索与回读

0.1.4 在独立服务内统一查询旧五库和 Evolution。旧数据以只读根接入，不接入旧隔离 Shared 库和 Host 业务数据库。真实旧库 E5 输出为原始均值向量，不能以单位向量查询三张 flat L2 表；LanceDB 0.37.1 与固定模型及两个 cosine IVF、三个 flat L2 指标按真实数据验收。旧库向量和词法召回按统一预算融合并要求相关性。Evolution 用单独的 Corpus SQLite 快照和已授权的成员。证据映射以命名扩展 `evidence_refs` 保存，保留 `PRAGMA user_version=1` 兼容旧二进制回滚。
