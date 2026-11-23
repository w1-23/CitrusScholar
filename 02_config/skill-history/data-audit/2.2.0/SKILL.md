---
name: data-audit
description: 对上传表格按适用前提选择检查，给出原始坐标、原值、执行回执和覆盖缺口；按用户需要输出不改数据的标注副本。
license: MIT
metadata:
  version: "2.2.0"
---

# 数据审查

先读取表头、字段类型、单位、原始行列和解析覆盖。用户未说明时先做通用缺失、类型和重复检查；实验设计、合法范围、整数汇总和有序模式需要明确配置。37 项默认执行、38 项包含诊断的能力目录，不是每次必须运行的检测清单。主 Agent 选择规则并解释结果，脚本不创建 Agent，也不限制其他合法工具。

规则执行以显式选择为准：省略 `selected_checks` 保持兼容默认（`checks=None` 时执行目录默认项）；`selected_checks=[]` 表示不运行目录规则，仅保留显式 Code/注释层。`experimental_quality` 只在未提供 `selected_checks` 时注入质量默认项。每条目录规则都出现在回执中，未选为 `not_selected`，不能把未选写成已检查。

托管环境可先发现表格区域，再按实际数据选择批量检查或直接用授权 Code。模式提供默认项，接纳自定义结果取决于 Worker 回执、源文件哈希与区域坐标。

独立运行依赖 `citrusscholar-support[audit]==2.0.0`，通过标准安装使用；不导入旧项目 `src`。方法分组见 [references/methods.md](references/methods.md)，精度与覆盖见 [references/precision-and-coverage.md](references/precision-and-coverage.md)，文件标注见 [references/marking.md](references/marking.md)。

## 操作

1. 用 `scripts/catalog.py` 查看规则及适用前提。选择规则后，写一个配置 JSON（例如 `{"values":["value"],"ranges":{"value":{"min":0,"max":10}}}`）。不可猜测单位、分组、原序、SD 口径或必填项。
2. `python scripts/audit_table.py input.xlsx --snapshot review.json --checks out_of_range --options options.json`。保留源 sha256、逐表信封与真实执行回执。默认规则只覆盖通用问题，不推断实验设计。
3. 默认在对话中逐项给出 sheet / 单元格 / 原值 / 规则 / 实际观察 / 复核建议。缺输入、未选、样本不足、截断和程序错误分别说明；没有发现不能写成数据真实或无问题。
4. 用户需要文件时再执行 `python scripts/annotate_workbook.py review.json --output annotated.xlsx`。读已保存快照和 findings，不重新审查。原文件不覆盖，输出路径已存在时拒绝。

默认保留原工作表名与顺序、数据坐标、值、类型、格式和合并范围；只改命中格填充/边框/批注。零默认附表。`--explain-visible` 在原 max_column+2 处写非数据说明；`--issue-list`、`--overview`、`--rules` 单独显式选择。问题清单保留原有审计列并新增区域，单元格、原值和证据只来自已审区域。CSV 转工作簿是格式转换，不声称与原 CSV 字节相同。一行一份源文件的合并工作簿在一次完整审查后发布；延迟导出复用同一渲染器，不重跑规则。

## 科学解释

信号用于复核，不输出不端判决或合成风险分。原始词法尾数只使用实际保存的 token；Excel 数值的显示格式不能恢复尾零。旧 `decimal_ending_concentration` 为显式格式化诊断，不能解释成原始末位证据。序列必须保持原序，缺失打断窗口；跨列必须按共同原始行配对。

大型/不支持文件或缺少可选读取器应报告实际限制。遇部分覆盖先说明缺口；不要静默丢弃坏行、补值、排序原序或运行全部规则弥补不确定性。

### Explicit audit plans

Pass `checks_json.audit_plan` with `schema: "audit-plan/1"`. Choose
`experimental_quality` for ordinary quality checks or `research_integrity`
for specific reuse and consistency questions. Modes select defaults. Explicit checks still require their own inputs and can be used in either mode. Existing source selection,
sheet, numeric column, and reported summary parameters still apply.
Select the desired `cross_file_methods` from the documented fixed catalogue.
For `comparisons`, bind `left` and `right` to the exact `file_index`, `sheet`,
and the requested coordinate selection. `sample_alignment` requires actual ordered `key_columns` on
each side; no row-number identity inference. `finite_rectangle` requires
explicit 1-based original `rows` on each side; disjoint coordinates are
allowed, automatic unrestricted submatrix search is not performed. Supply
two known `units` and an explicit `right_factor_to_left` for a conversion.
`window_search` takes `left` and `right` with `file_index`, `sheet`, and 1–8
non-overlapping `rectangles` per side. Each rectangle lists original `rows`
and exact `columns`. Specify `window: {rows, columns}` (3–32 by 2–8), and
optionally `max_windows`, `max_pairs`, and a reproducible `seed`. Candidate
hashes are followed by exact, unit-aware checks. Report `windows_scanned`,
`candidate_pairs_examined`, and `limit_reasons` when coverage is partial.
All three methods report skipped or incomplete coverage separately from findings.

### One-off Code Worker findings

For a one-off method in either mode, bind the selected account files into the
existing offline Code Workspace by ID and hash. Declare a JSON output under
`/outputs/`. Pass only its `execution_id` and `path` as
`checks_json.custom_findings` after successful Code Worker acceptance.
Emit a JSON object with `schema` = `custom-audit-report/1`, `method_id`,
`method_version`, `input_sha256` (a map from each Worker input file ID to
its SHA-256), report-level `coverage`, and `findings` (up to 20 records).
Report coverage uses the counts below. Account for every requested unit
in exactly one of `executed`, `skipped`, or `truncated`. Each finding uses
the same method ID and version and has these fields:

- `schema` = `custom-audit-finding/1`, `method_id`, `method_version`
- `source_refs` with `{file_id, sha256}` and `cells` with
  `{file_id, sheet, row, column}` (original 1-based coordinates)
- `parameters`, `numerical_evidence`, and `coverage` with integer counts
  `requested`, `executed`, `skipped`, `truncated`, plus `reason`
- `interpretation_scope` = `review_lead_only`

The host obtains the script hash, offline runtime receipt, output hash and
installed dependency versions from the Worker. It checks locations against
its own selected table snapshots. Treat parameters, numerical results and
coverage counts as script observations. An empty `findings` list can have
complete coverage when a verified offline run executed the full declared
scope on the matching inputs. A legacy array still returns its admitted
findings with partial coverage. Use the existing review record
for decisions. Scripts are one-off and have read-only original inputs.

### 工作簿区域

同一工作表包含多个独立表格时，先查看布局。使用 `checks_json.regions` 精确指定 `file_index`、工作表、A1 范围和绝对表头行；托管路径的区域必须带 `file_index`。区域身份按来源文件与工作表作用域：不同文件可有相同坐标或相同区域 ID，同一文件同一工作表的区域不得重叠，区域 ID 不得重复。

比较和 Code 发现优先用 `region_id`；也可用唯一 `region_bounds` 别名解析，歧义或缺失时明确拒绝，不能猜区域。行级发现只横向展开到该区域实际列，不污染同表其他列或其他工作表；合并单元格只在锚点位于已审范围时标注。

### 统一发现投影

同一 review 的 `data-audit-findings/1` 投影合并目录规则、覆盖诊断、人工注释、Code 发现、显式比较和跨文件数值线索，保留已有 `finding_id`、旧版 `issues` 与原始单元格。投影最多 1000 条或 2 MiB；超出时如实给 `projection_omitted`、扫描/复核遗漏数和 `status=partial`。投影是只读视图，不重写不可变 review。
