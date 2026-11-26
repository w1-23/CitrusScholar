# 8043 生产实现审计快照

本目录补齐 GitHub 之前无法直接看到的主机侧源码与启动配置。它是只读审计资料，不是新的部署入口。不要在生产上直接运行快照里的切换脚本。

| 服务 | 生产加载的源码提交 | 审计入口 |
| --- | --- | --- |
| Web / Code Worker | `a865b07d9f525bd121a056bb3cad53293c0030ce` | `systemd/` 中 merged units 与 drop-ins，`release/launch.py`、`release/deployment.json`，主仓库 `01_src/scripts/apptainer_launch.py`、`configured_local.py` 与 `code_worker.py` |
| Corpus intake / publisher | `619180208def17f0a1a8c2ac7de44cbb562109f9` | `systemd/` 中 corpus units；在该提交审查 `packages/citrus_corpus`，不能据当前 main 的同名文件推断实例行为 |
| Watchdog / 日志导出 | 见 units 与 `tools/` 包装器 | 自动导出、主机导出包装器和旁置 public delivery 快照；导出包装器实际固定调用 `619180208def17f0a1a8c2ac7de44cbb562109f9` 的 `04_scripts/export_8043_turns.py` |

- `cutover/` 是该次生产部署实际使用的准备、封存和切换源码。通用封存、验签和回滚工具仍位于上级目录。
- `release/` 保留当前部署的字段、生成的 launcher、发布清单和源码清单。地址与路径是审计输入，不代表附带对应运行文件。SIF 只有固定摘要，不包含二进制镜像。
- `skill-provenance/` 是当前实际挂载 Skill 包的来源、批准脚本和逐文件摘要，包括 Fusion 包映射。宿主来源锁与构建适配在 `02_config/standalone-skills.lock.json`、`02_config/skill-fusion.lock.json` 和同目录补丁中，构建器在 `04_scripts/`。第三方完整发布包不随此快照重新分发。可按锁定来源和清单复核。
- `SHA256SUMS` 固定从主机取出的 26 份文件。运行 `sha256sum -c SHA256SUMS` 只校验快照，不会启动服务。
- 没有纳入账户库、聊天记录、导出日志、凭据、模型配置内容、用户数据、旧候选或完整镜像。GitHub 本次新增审计资料不会更改已运行的只读生产包。
