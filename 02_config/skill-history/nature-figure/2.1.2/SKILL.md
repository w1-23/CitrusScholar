---
name: nature-figure
description: Create, revise, package and audit scientific Matplotlib figures using explicit data and final artifact bytes. Apply pinned Nature figure guidance with honest per-check coverage, reproducible source/data bundles and frozen style edits.
version: 2.1.2
---

# CitrusScholar 托管绘图

1. 分析入口用 `inputs_json` 声明授权文件，并用 `input_contract.raw` 将原件绑定为 `Path`。分析脚本可直接读取这些文件，也可以用授权 Code 做计算。
2. 在 `input_contract.tables` 中声明需由 Worker 预载的表格，或在分析脚本中将数据读为 `DataFrame`，用 `emit.table` 登记需要冻结的表。记录单位、实验单元、样本量、误差定义和统计参数。
3. 独立 `render_script` 只接收冻结的 `data[name]` 表格。每项必须是 `DataFrame`，脚本生成一张 Figure。样式修改复用冻结数据，不重算统计。已声明表格的 CSV/Excel 加载示例见 [托管接口](ADAPTER.md) 和 [绘图方法](references/figure-methods.md)。

默认交付 SVG。用户要求 PNG、PDF 或多种格式时，按实际生成的成果交付。预览、正式和期刊模式选择相应的校验要求。QA 必须绑定最终文件的摘要，保留真实的 FAIL、未运行项及覆盖缺口。独立调用方式见 [standalone CLI](references/standalone-cli.md)。

The vendored snapshot is the fixed 44-file upstream set (40 core files plus four pinned execution peripherals) at commit `7bff1d0`; a separately supplied user ZIP is a different upstream snapshot (8 of 39 same-named files differ) and is not packaged. Python paths are the `citrus_figures` library and the `scripts/citrus/` adapters. R execution and OpenRouter image generation are disabled and never invoked by the service; image-generation networks, private templates, gallery images, and external models are not automatically enabled. Keep acquisition, local execution authorization and network authorization distinct. Consult the [resource bridge](references/bridge.md), [release scope](RELEASE.md) and [vendor provenance](vendor/nature-skills/provenance.json). Do not claim a complete upstream repository or availability of excluded assets.
