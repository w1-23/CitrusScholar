# Selfcorpus 验收记录

验收日期 2026-11-27。仅在远端 Selfcorpus 工作区创建实现、临时测试数据库、依赖及独立模型快照。没有修改历史数据、切换 8043 或安装 systemd 单元。

| 范围 | 结果 |
|---|---|
| 正常收录 | 通过签名 HTTP 提交 → Worker → 原子发布 → 下一次查询命中，结果引用实际 generation。 |
| 权限 | 用户 B 看不到用户 A 的个人文献。项目读者可见但不能写入，非成员请求被拒绝。Shared 审批前不可见，拒绝后仍不可见。 |
| 版本与故障 | DOI 相同且内容变化产生新版本。失败不改变 active；重复提交返回同一 operation；租约失效的 Worker 无权提交；撤回和旧代回滚正确。 |
| 获取与解析 | Fixtures 覆盖 DOI 题录与实际 OA 正文的区别、JATS、TEI、Markdown、HTML、DOCX、GROBID 返回、上传约束及网络错误处理。真实 PLOS 样本从 Crossref / OpenAlex 返回已核对的题录，但出版社 `Crawl-delay` 大于剩余请求预算，Europe PMC 本次未完成。此样本未发布为全文。 |
| 真模型 | 1024 维 multilingual E5-large；独立目录的快照分别在 CUDA、CPU 通过相同的 HTTP 跨语言命中与权限测试。GROBID、Docling 的真实解析服务未上线验证。 |
| 安装包 | 脱离源码目录从 wheel 启动，验证包内包含迁移、解析器和来源模块，不包含模型和历史语料；`init`、`stats` 正常。 |
| 架构 | 在远端实际代码上用 AST 校验相对导入图，未发现循环。核心运行包不导入 Citrus 或 8043。 |

## 复测

```bash
python3 -B -m unittest discover -s tests -v
python3 -B scripts/audit_architecture.py
# 使用具有已安装 ONNX 依赖的 Python
python -B scripts/smoke_model.py "$SELFCORPUS_MODEL_DIR" --device cuda
```

上述初版验收覆盖 52 项自动测试。当前源码包对应 0.1.2，安装包可本地构建，不随源码分发。系统 Python 没有 NumPy 时会跳过对应数值单测。验证公开全文及其 Shared 权限路径的外部夹具不随源码分发，复测办法见 [夹具来源与校验](../tests/fixtures/verified_text/ATTRIBUTION.md)。测试用 HashEmbedding 仅用于确定性、失败注入和权限路径，不作为语义检索的验收证据。

## 非本次生产验证范围

运行中的 8043 尚未切换到新 HostClient。真实学术双栏、扫描/OCR 和复杂表格公式质量、可公开迁移的历史语料清单、历史全量负载压测，以及正式驻留服务的运行监控均需另行验收。自进化的端到端测试是在临时数据库、临时回环端口中执行的。
