# 部署、备份与回滚

部署在 Linux，Python 3.12 或更新版本，需要 SQLite FTS5。以下命令全部从 Selfcorpus 独立目录运行，不改现有 8043。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install '.[gpu]'   # CPU 专用环境改为 .[cpu]，二者不混装
umask 077
mkdir -p .runtime
python -c 'import secrets; print(secrets.token_hex(32))' > .runtime/host-hmac.key
export SELFCORPUS_DB="$PWD/.runtime/selfcorpus.sqlite3"
export SELFCORPUS_SECRET_FILE="$PWD/.runtime/host-hmac.key"
export SELFCORPUS_MODEL_DIR="$PWD/.models/66076b8dc6e367337e3e90e6fb309fb0f3addaf6"
export SELFCORPUS_DEVICE=cuda
selfcorpus check-model
selfcorpus init
```

服务和 Worker 用相同配置、独立进程运行 `selfcorpus serve` 和 `selfcorpus worker`。HTTP 默认只监听 `127.0.0.1:8061`，不应直接暴露到公网。如果 Host 不在本机，使用受控 TLS 反代/隧道。生产应限制请求并发、请求大小和每用户收录速率。CUDA 模式在实际 CUDA provider 无法工作时启动失败，不自动退 CPU。修改为 `SELFCORPUS_DEVICE=cpu` 后重启两个进程可切换，模型签名和向量维度不变。

- Markdown、纯文本、HTML、JATS、TEI、DOCX 可直接解析。GROBID 学术 PDF 解析通过 `SELFCORPUS_GROBID_URL` 配置可信服务端点；可选 Docling 需安装 `.[docling]`，设置 `SELFCORPUS_DOCLING=1` 及已预置的 `SELFCORPUS_DOCLING_ARTIFACTS`。未配置或无法获取可靠正文时 PDF 不发布。解析器发出的页码线索来自解析结果，不从文本猜造。
- 运营可监测进程存活、SQLite 文件空间和 WAL、任务回执与失败分类。数据库健康路由只验证数据库可读，不代表 Worker 或模型线程未停顿。监控可用签名账户定期提交并收录一篇有授权的合成样本做端到端探针。
- 更换模型版本时，在独立环境备份数据库，用新模型执行 `selfcorpus reembed '<scope_ref>'`，通过校验后原子切换该 scope。切换期间旧模型可提供关键词降级，切完后切换 API 和 Worker 模型。旧 chunker 变更不由此命令处理，需重新走解析/切分或使用已验证的回滚点。

可将 `deploy/selfcorpus-api.service`、`deploy/selfcorpus-worker.service` 用作 systemd 用户服务模板。默认工作目录是 `~/.local/share/selfcorpus`。按上面的变量在 `~/.config/selfcorpus/selfcorpus.env` 创建权限为 `0600` 的配置文件，文件内使用绝对路径且不写 `export`，再核对虚拟环境路径、密钥读权限和本机 GPU。仓库不包含运行配置或密钥，单元也不会随源码自动安装。

## 本机已核验的独立模型快照

验收采用独立的模型快照，版本标识为 `66076b8dc6e367337e3e90e6fb309fb0f3addaf6`。权重、模型许可证及其校验清单应从模型来源独立取得，按来源要求验证后将 `SELFCORPUS_MODEL_DIR` 指向快照目录。源码包不包含权重或 Python 依赖缓存。

`selfcorpus stats` 输出任务阶段聚合和活跃范围数，不输出身份。`selfcorpus discover-links URL --max-pages 5 --depth 1` 在显式授权种子下返回同源候选，不收录、不发布。选定的链接仍须由 Host 带授权独立提交。

## 备份和回滚

在同一项目下保留发布包、独立配置和受限备份。使用 SQLite Backup API 或停止两个 Selfcorpus 进程后做一致性备份，不要直接只复制活动 WAL 数据库的主文件。

```bash
umask 077
mkdir -p .runtime/backups
python - "$SELFCORPUS_DB" .runtime/backups/selfcorpus-before-upgrade.sqlite3 <<'PY'
import pathlib, sqlite3, sys
src = pathlib.Path(sys.argv[1]).resolve(strict=True)
dst = pathlib.Path(sys.argv[2])
if dst.exists():
    raise SystemExit('backup already exists')
with sqlite3.connect(src.as_uri()+'?mode=ro', uri=True) as live, sqlite3.connect(dst) as snapshot:
    live.backup(snapshot)
    assert snapshot.execute('pragma integrity_check').fetchone()[0] == 'ok'
PY
```

数据回滚优先用签名 rollback API，保留撤回记录。发布失败时，复用之前的包和模型配置，对备份副本验证完整性再选择是否恢复。只有在停掉 Selfcorpus API 与 Worker 后方可恢复数据库。若备份早于撤回或 Shared 治理决定，恢复前要重放这些决定，否则可能恢复已撤回资料。停止现有 8043 或替换其发布包不属于这里的操作。
