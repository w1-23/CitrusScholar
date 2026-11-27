# 架构、溯源与旧库审计

## Phase 0 只读事实

| 资产 | 审计结果 | 新系统处理 |
|---|---|---|
| 旧 `runtime/citrusqa/state/data` | 5 类 `chunks.jsonl`，另有元数据索引和 LanceDB active manifest。活跃代 `g20261028T165637Z_851926a9` 自报 126,943 chunks、5 表。 | 原始数据、索引与历史快照保持不变，不自动导入。 |
| `runtime/8043-isolated-corpus` | 只读快照可见 1 intake、5 versions、1 publication、2 bibliography 记录。历史迁移的 4 个来源均标记 `needs_review`。 | 仅作为目录和迁移边界证据。若以后迁移，应逐条确认来源、许可、目标 scope 并重新解析/编码；不得自动变 Shared。 |
| `work/five-repos-host` | 旧 `packages/citrus_corpus` 用目录/SQLite、人工 rights grant、LanceDB generations；旧 `src.engine.embedder` 依赖 Host 配置和硬件单例。 | 不复用旧自进化流程、Host 配置或运行时。新 API/Worker 直接由独立包启动。 |
| E5 缓存 | `qdrant/multilingual-e5-large-onnx` revision `66076b8dc6e367337e3e90e6fb309fb0f3addaf6`，1024 维，旧约定分别用 `query:` 和 `passage:`。 | 已在 Selfcorpus 内复制同 revision 到 `.models`，校验六个文件，实际权重使用不同 inode，旧目录不再是运行时依赖。不要假设旧向量数值、归一化和新索引完全可互换。 |

## 新数据和事务

`requests/events` 记录收录幂等键、租约、阶段和回执。Worker 在 SQLite 写事务外获取原料、解析和计算向量。写入事务内保存版本、解析、切片、向量，构建新成员集合，检查映射、计数和检索样本，再切换 `active`。任务最终回执和指针一起提交。暂未取得全文的记录仅入目录，不能被 RAG 当作全文使用。Shared 的相同处理结果先进入 `pending_review`，活跃集合不变。

文献身份在 scope 内稳定，优先用规范 DOI。没有 DOI 的 URL 使用来源 URI，无 DOI 的上传以内容哈希区分；不同内容不因文件重名而错误合并为一个文献。每个版本保留实际来源、获取时间、许可或访问说明、原料字节和 SHA-256。独立解析记录保存 Markdown、parser 版本、结构化块、页码线索和质量诊断。Chunk 引用解析版本、标题路径、页范围和估算 token 数。正文按章节、段落、表格及大小切分，引用列表不作为正文候选。

检索在同一 SQLite 读快照中解析授权 scope 的活跃指针，关键词和向量候选均受 generation 及元数据条件约束。排名采用词项匹配、精确余弦和倒数排名融合，可注入 reranker。没有跨请求结果缓存；撤回写 tombstone 并生成排除该文献的新 snapshot。历史快照仍可回滚，但任何包含 tombstone 的快照都不能激活。备份中的历史原料并不会被撤回操作物理清除。

## 生产容量边界

SQLite 是单一写数据库，生成新快照复制文献引用而不重写所有向量。精确向量排序会扫描经过权限和元数据过滤的候选。数据库中的快照、原文和向量目前不自动回收。高查询并发、大规模全库迁移前需用代表性语料验证吞吐和延迟，并按 VectorIndex 接口替换后端（如支持预过滤的 ANN）。网络 Fetch 取明确授权的单个 URL 及可信 DOI 开放全文。内置 crawler 可做有页数/深度上限的同源发现，只报告候选，不做不限范围的链接跟随或自动授权。
