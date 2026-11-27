# 三模块接口合同 v1

## 共用边界

主体负责会话、身份、授权、同源网关和唯一 Agent 循环。两模块只通过版本化 HTTP 互办事务，互不读库。module 内的存储、任务执行及索引状态不进 Host。浏览器通过 Host，Worker 与数据源不直暴露。调用方身份由校验通过的会话和成员授权生成，不能来自浏览器或模型自报。没有可靠项目成员授权时默认拒绝，不能把会话 ID 当成员凭据。文件和证据回读必须再次授权，禁止用目录路径假冒引用。

## Resource v1

资源主键为不透明 `asset_ref`；可选 `project_ref`，`user_ref` 由可信授权提供。传输凭据绑定用途、方法、路径、主体及作用域，短时有效。文件快照使用 SHA-256 作为 `source_hash`，明确大小、类型、用途和版本。业务操作返回稳定 `operation_id`、UTC `created_at`、明确 `status`；状态单调，重试用幂等键，状态未知不能包装成成功。拒绝、不可用和无结果分开。公共返回不传宿主路径、数据库行、密钥、私有模型协议或原始凭据。

## Launch v1

沿用现有同源短期一次性票据换工作台会话和受限资源读取。候选项是用户看得懂的图件渲染预览，不是脚本列表。只有可验证来源的预览才应展示“可编辑”，需绑定确切脚本快照、显式依赖、渲染入口与多图序号/阶段、内容哈希和运行环境。未验证脚本允许用户明确渲染后生成候选；静态 SVG/PDF/PNG 不通过文件名猜绑定，不宣称可编辑。默认可编辑，另外提供仅预览。保存原始文件、依赖、运行环境/包锁、目录/环境白名单、Overrides 和修订；隔离新 Worker 冷重跑。导出引用只给用户有权限的版本。Workbench 不进 Agent 工具目录。右上返回会话，助手建议须经本地预览与用户确认，不启动第二个 Agent 循环。

## Retrieval v1

Agent 只见一个 Corpus 检索能力，Corpus 按可信 Scope、Collection 选择、一个请求总预算检索。迁移期间可在模块内部挂载旧五库只读适配器，不能把两套搜索路由散落在 Host 或暴露成六个工具。Snapshot 可以绑定多个物理代次，原子发布逻辑世代，旧引用允许授权精确回读。EvidenceSet 版本化，含稳定文献/版本/段落/引用、摘要片段和回读授权，明确 hit/empty/partial/unavailable 与预算；不得把相关性不足的正余弦向量变成命中。只对已消费的实际证据生成引用，回答不强制列出查库报告。元数据层以规范 DOI、原始内容哈希、来源与版本联合识别，不仅靠标题合并不同记录；ACL 过滤先于可见证据。Personal/Project 有可核验授权且校验通过即可发布。Shared 保持自动发布。经 Fetch 核实且允许共享的公开文献，通过质量及权限校验后自动进入已发布快照，所有已批准账号可检索。不增设人工审批，私人上传不会进入 Shared，现有已发布资料不自动撤回。旧索引不重建，只有模型版本/预处理/前缀/维度/归一化/距离契合且对照通过时才能直接复用向量。

## 联合验收

同一批普通/未授权账号和项目，真 Python/R 与 CSV、重开/补丁/导出、无权依赖失败；旧索引真实芽变证据、增量发布后的下一轮命中、未通过共享校验的待处理资料不可见、合格 Shared 自动发布后跨账号可见、两个模块单独停机不拖垮主体。浏览器展示主会话和工作台并排、选中编辑、画布时间线和 Python/R 导入；不以某一方的测试代替。保留现网和命名回滚点，联合门槛过后才部署。

## v1 传输约定

- Launch 保留 `POST /api/internal/workbench/open` 请求 `{session_ref?,source_assets?}`，响应 `{status,workbench_url,launch_endpoint,launch_ticket}`，沿用同源一次性换票。提示资源不隐式执行。现有 `session:<id>` 仅为 Host 会话范围，不是 Corpus Project 权限。
- Resource 继续用账号或已授权会话作用域与签名 GET，列表 `type=figure|dependency`，具体资源角色 `preview|source|dependency`。响应以 `X-Asset-Filename/SHA256/Version` 提供不可变校验；保留原鉴权兼容性。工作台可在内部提供预览图卡的创建/读取，不要求 Host 理解 Artist，原路径不自动猜配。未知 `project_ref` 拒绝。任务回执字段不强加给纯 GET。
- `POST /api/v1/retrieval/evidence-set` 通过现有 Host Actor 签名。请求 `{query,sources:[{source_id,topics?:[]}],evolution_scope?:{personal,shared,projects},filters?:{doi,year,document_ref},budget:{max_candidates,max_results,max_chars,timeout_ms}}`。源标识为现有五个 `cqa_*` + `evolution`，只是模块内定位，不作为多个 Agent 工具。未知或重复的选择拒绝；请求作用域不能扩大可信 Actor 授权。预算默认 240/12/24000/4000，最大 600/50/64000/12000；分配给召回通道与授权范围，不随来源数倍增。
- EvidenceSet `{schema_version:1,status,index_snapshot:{snapshot_ref,sources:[{source_id,generations:[{scope_ref?,generation_ref}]}]},source_outcomes:[{source_id,status,reason_codes,candidate_count,returned_count}],budget_used:{candidates,results,chars},results:[{source_id,scope_ref?,generation_ref,document_ref,version_ref,passage_ref,evidence_ref,title,doi?,year?,excerpt,readback}],limitations,coverage}`。全局状态 `hit|empty|partial|unavailable`，仅在全部请求来源完成且无相关证据时为 `empty`。`readback` 为 `exact|excerpt_only`。Host 不构造模块内部引用，不做跨库混排或隐式旧库回退。
- `POST /api/v1/retrieval/evidence/read` 用新的签名 `{evidence_ref,offset?,max_chars?}`，需同主体、当前有效 ACL、已发布成员关系，稳定绑定引用版本、内容哈希与半开字符区间。缺失、越权、撤销、哈希不一致统一 404。旧历史引用保留原只读验证路径直到精确回读通过。展示引用可核验，不强制输出来源报告。
- `POST /api/v1/ingestion/verified-shared` 保留原可核验 Fetch 自动发布，不加审批，也不允许私有上传借此改变可见性。已发布后下一查询可见。未配置确实可信的项目授权时 Project 拒绝，不把会话 ID 用作替代。

### 精确回读与身份补充

Actor 保留 v1 签名 claims `{subject,projects,shared,governor,base_sources?}`。缺省 base_sources 无权限，明确与五库 allowlist 求交。Evidence `source_hash` 与 `passage_hash` 分别标识原始文档对象及可读段落，exact 必须有 passage_hash；excerpt_only 不登记持久引用。回读响应 `{schema_version:1,evidence_ref,source_id,generation_ref,document_ref,version_ref,passage_ref,passage_hash,text,offset,next_offset,total_chars,span?:{start,end},page?:{start,end}}`，offset/next_offset 相对段落，其余区间为半开 Unicode 字符偏移。Budget 与 generation/count 进行 Host 校验。

### Workbench 预览卡合同

- 显式用户动作 `POST /api/v1/preview-runs` 接收 `{source:{entrypoint_ref,assets,hashes,language}}` 和幂等键，返回 `{operation_id,created_at,status}`。库列表展示已准备的预览，不自行执行任何脚本。没有已有预览时，在独立准备流程选脚本和依赖再渲染。
- `GET /api/v1/preview-runs/{operation_id}` 返回卡 `{preview_ref,preview_version,figure_ordinal,label,thumbnail_url,preview_sha256,capability,source_state,bundle_sha256?,runtime_ref?}`。用户选渲染图，重复打开继续用固定版本。单卡以明确捕获序号标识；暂不可唯一捕获的脚本明确失败或降级，不能静默选错图。
- `POST /api/v1/figures/import` 接受 `{preview_ref,preview_version,options:{parse_mode}}`，与旧直接 `source` 形态互斥。服务端核对用户、卡片版本、包及缩略图哈希、环境和完整源快照。不信任浏览器提交的捕获索引或磁盘路径。
- 导出或已有 SVG 可以充当授权视觉底图，但缺乏已验证来源只在“仅预览”中出现。没有匹配的可编辑对象则不提供相应控件；可选助手仅持有预览建议并明确确认，不能通过 DOM 事件执行任意脚本。
