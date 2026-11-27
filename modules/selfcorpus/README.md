# Selfcorpus

Selfcorpus 是独立运行的科研语料与证据检索服务。8043 Host 负责认证、项目成员关系和确认收录授权，Selfcorpus 负责文献来源、解析、切分、编码、任务、分域发布及检索。两者通过经签名的 HTTP API 对接，不共用 Python 源码、发布目录或语料数据库。

- 个人、项目收录通过校验后自动发布。Shared 只能由治理者发布。
- 检索回传实际 generation，以及 document、version、chunk、来源地址和哈希。失败或仅元数据的任务不会污染活跃索引。
- 默认使用本地 E5 ONNX 的 CUDA 执行，可配置 CPU。HashEmbedding 仅用于显式开发测试。
- 目前默认是 SQLite WAL、FTS5 和精确向量排序，适用于从小规模逐步新增的个人/项目库。旧 126,943 切片的历史数据尚未迁移或做同规模压测。

```bash
python3 -B -m unittest discover -s tests -v
python3 -m corpus --db .runtime/selfcorpus.sqlite3 init
```

- [启动、备份与回滚](docs/deployment.md)
- [Host 握手与 API](docs/host-contract.md)
- [架构、溯源与旧库审计](docs/architecture.md)
- [验收记录](docs/verification.md)
