# 统一绘图执行链（F-04 / F-05 / F-08）

本文件说明 `nature-figure` Skill 在统一执行架构下的运行方式。它是实现与维护说明，
不是模型的工作流入口；工具名由宿主运行时能力表注入，Skill 不得猜测未注册的工具。

## 一个入口，两种模式

唯一绘图入口是 `run_research_figure_script`：

| 模式 | 用途 | 必填 |
|---|---|---|
| `mode="script"`（默认） | 任意 Matplotlib / Seaborn 科研绘图 | `script` 或 `script_ref_json` |
| `mode="template"` | 五种固定科研图型 | `template_id` + `template_parameters_json` + 单张表绑定 |

模板清单（`packages/citrus_figures/template_registry.py`）：

- `volcano`：`gene_col`、`effect_col`、`p_col`（校正 P 值）、`p_threshold`、`effect_threshold`、`top_labels`
- `roc`：`fpr_col`、`tpr_cols`（1–12 条曲线）
- `dotplot`：`row_col`、`column_col`、`size_col`、`color_col`
- `marginal`：`x_col`、`y_col`、`group_col`（可选）、`bins`
- `paired`：`id_col`、`condition_col`、`value_col`

模板模式的数据绑定只能有一张表：`inputs_json`（`{"data": {"file_id": "..."}}`、
`{"tidy": {"dataset_ref": "..."}}`）或 `analysis_result_ref_json`。模板模式不接受
`script`、`script_ref_json`、`outputs_json`：输出契约由注册表生成，参数或绑定错误在
派发前返回 `not_run`（`reason_code="template_request_rejected"`）。

## 两种模式共享什么

- 同一份冻结输入快照（`snapshot_policy="immutable_bytes_new_source_for_changed_data"`）；
- 同一个隔离 Worker（Docker / WSL-Docker / Apptainer），不联网、不建子进程；
- 同一套 Worker 时限策略（`citrus_figures/timeouts.py`）；
- 同一输出合同（版本 2：冻结表 + 独立 `render_script`，`normalize_output_contract` 校验）；
- 同一 current-QA 与发布链；
- 同一个 Figure Workbench 项目对象模型（`source_kind="script"`，模板来源写在
  `source_json.template`，不新增第二种项目类型）；
- 同一字体策略（`citrus_figures/typography.py`）。

模板编译写入脚本首行的 `# citrus-template/v1 {...}` 是唯一来源标记；执行器据此记录
`mode`、`template` 与 `timeout` 回执。

## 旧入口：只转发，不再调度

`run_nature_plot` / `collect_nature_plot` 已标记 deprecated。它们：

- 不创建独立 workspace、不使用 Code Worker、不产生第二种执行记录；
- 把同一请求交给统一工具模板模式，返回的 `execution_id` 就是统一运行 id；
- `collect_nature_plot` 只读取该统一运行回执并重新核验产物链接，自身不发布；
- R 后端明确返回 `unsupported`（`nature_plot_r_backend_not_unified`），不自动切换 Python。

## Worker 时限（F-05）

`resolve_timeout()` 依据任务类型、输入字节数、图数量、模板复杂度与账号 deadline 计算
wall-clock 秒数，并夹在 30–900 秒之间。它与模型输出预算无关，回执用
`unit="worker_wall_clock_seconds"` 标明。历史回执中的 90 秒仍可读取核验
（`nature_skill_worker.HISTORICAL_TIMEOUTS`），但不再产生新的 90/120 秒预算。

## 预览缓存（F-05）

Apptainer 以 SIF 文件的 SHA-256 作为不可变环境身份；只有校验通过（本地文件摘要一致，
或运维显式声明 `CITRUS_APPTAINER_SIF_VERIFIED=1`）才允许复用预览缓存。缓存 key 绑定
owner、figure_id、render_version、source、data refs、环境 hash、渲染 profile 与 edits；
预览是低 DPI 草稿（`CITRUS_FIGURES_PREVIEW_DPI`），正式导出保持出版质量，二者不会互相
冒充。

## 字体（F-08）

通用脚本入口默认注入版本化 `configure_style()`（`citrus-manuscript/1`）；脚本自行设置
`font.family` / `configure_style()` / `CITRUS_STYLE_OVERRIDE` 时尊重用户选择。发布前的
QA receipt 记录请求族、实际解析族、font file 与各格式嵌入状态；容器缺字体时给出
`[CITRUS_FONT_MISSING]` warning 并写明 fallback。工作台字体列表收敛为 Worker 已验证字体
（`typography.VERIFIED_FAMILIES` ∩ 镜像已装），中英文字体分开配置。

## R 能力范围

本轮统一执行只保证 Python / Matplotlib 完整。R 固定模板仍只覆盖有限的 marginal 能力，
且尚未接入统一身份、预算、deadline、网络策略、回执与工作台合同，因此对 Agent 一律
报告为不可用；不得宣称“通用 R 绘图可用”，也不得用 Python 冒充 R。
