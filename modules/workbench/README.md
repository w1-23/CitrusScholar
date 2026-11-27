# Citrus Figure Workbench · API MVP

独立后端，用 Tavotto 的实际 Matplotlib Artist 和已绘制 manifest 实现选取、自动图内分组、原生属性覆盖和导出。R 通过独立 Worker 运行，静态图件不冒充语义可编辑对象。包含浏览器工作台。Host 和 Workbench API 不执行绘图脚本，每次导入、编辑和导出均启动隔离渲染任务。

## 本机式独立测试或部署

需要 Linux、Apptainer、cgroup v2、该服务账号自己的 systemd 用户 manager，以及可信只读 SIF。普通请求不能传入 SIF、脚本路径、宿主路径或运行命令。应用只接受受控引用和上传字节。现有远端验收镜像为 `sandbox-nature-r.sif`，其版本和验收环境见 `docs/VERIFICATION.md`，不将这个混合测试镜像提交入仓库。

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -c requirements.lock
python3 scripts/fetch_tavotto.py .workbench-dev/tavotto
export WORKBENCH_IMAGE=/absolute/path/to/trusted-workbench.sif
export WORKBENCH_TAVOTTO="$PWD/.workbench-dev/tavotto"
export WORKBENCH_DB="$PWD/.workbench-dev/workbench.sqlite3"
# 为本机 HTTP 调试提供测试令牌及非 Secure Cookie，不能复制到网络部署。
export WORKBENCH_STANDALONE_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
export WORKBENCH_SECURE_COOKIE=0
.venv/bin/uvicorn wb.api:create_app --factory --host 127.0.0.1 --port 8058
```

调用 `POST /api/v1/standalone`，以 JSON `{"token":"<configured-token>"}` 登录并保存会话 cookie，再上传、导入和调用图件 API。没有设置独立令牌时，此入口不授权。部署使用 HTTPS，设置固定独立会话密钥 `WORKBENCH_SESSION_KEY`，配置 Host 集成才设置 `WORKBENCH_HOST_KEY` 和 `WORKBENCH_HOST_ORIGIN`。密钥不少于 32 字节，不使用用户 shell 的账号密码。

## 简明调用顺序

1. `POST /api/v1/assets` 上传请求原始字节，`X-Filename` 为 URI 编码文件名，取回 `asset_ref` 和 `source_hash`。
2. `POST /api/v1/figures/import` 提交 `source.kind`、`source.language`、`source.entrypoint_ref` 及 `options.parse_mode`。资料库用 `GET /api/v1/library?type=script` 查看 Python/R 候选，`type=static` 单独看静态图，随后用 `library:` 引用导入。浏览器默认提交 `parse_mode=editable`，没有验证到原生/语义编辑能力就不创建图件。静态预览使用 `rendered_asset` 和 `preview_only`，不把 SVG 拆为可编辑对象。既有 `auto` 调用仍可用。
   - 脚本可选 `source.assets` 指定明确依赖，引用来自上传或 `GET /api/v1/library?type=dependency`。支持 CSV、TSV、JSON、TXT、XLSX、Parquet、NPY，最多 32 个，单个脚本最多 512 KiB，整个输入最多 12 MiB。使用不含目录的原文件名引用数据。文件缺失、名称冲突或校验值不符时不导入。可用 `source.hashes` 映射选定引用到预期 SHA-256，浏览器从选择/上传响应中提交这些值。
   - 导入在图件与首版预览的同一 SQLite 事务中绑定只读输入快照。编辑和导出只用保存的副本，不重新访问 Host，不写回源文件。
3. `GET /api/v1/figures/{id}/objects` 取对象。在 Python 中 `native_manifest` 是 Tavotto 的原生绘制清单，`elements[].editable` 列出由上游提供的属性及值，组由 `groups` 给出。`GET .../preview?revision=1` 查同版本 PNG。
4. `POST .../pick` 传 `x`、`y`（预览图左上角原点的 0–1 分数）、可选 `locked_gids`、`current_gid`、`revision`。没有 `current_gid` 时普通点击，有该字段时按上游重叠顺序轮换。
5. Python `PATCH .../native-overrides` 传 `expected_revision` 及 `patches:[{"gid":"axes_0.lines_0","prop":"linewidth","value":4}]`，可带原生 `identity`。未知对象、被拒属性或版本冲突不会推进版本。`GET .../groups/{gid}` 查组，`POST .../groups/resolve` 查带锁定规则的展开，`POST .../groups/{gid}/move` 和 `POST .../groups/{gid}/scale` 在同一个版本内整体移动或绕中心等比缩放原生自动组全部成员。R 使用 `PATCH .../overrides` 和 `overrides:{"plot.title":{"text":"标题"}}`。
6. `GET .../history` 和 `POST .../restore`（`expected_revision` 与 `revision`）回滚图件，恢复会创建新版本。 `PUT .../history/{revision}/name` 用 `{ "name": "初稿" }` 为该不可变版本单独命名（最多 80 字符，空字符串清除）。列表仅在有名称时返回 `name`。命名不移动当前版本，恢复出来的新版本不继承名称。`POST .../export` 取 PNG/SVG/PDF，应记录响应的 `X-Figure-Revision`、`X-Content-SHA256` 和 `X-Export-Id`。`width_mm`、`height_mm`、`dpi` 独立于浏览器显示缩放。 对于 Python 图件，明确提供的导出宽／高仅覆盖对应轴的物理尺寸，不修改保存的原生画布大小。

Host 薄接入和验证说明见 `docs/HOST_HANDOFF.md`。运行测试前安装 `pytest==8.3.5`，随后执行 `python -m pytest -q`。缺少隔离镜像时，真实渲染与隔离集成测试跳过。未配置时只有不需要真正渲染的协议、鉴权及拾取测试可运行。

## 预览图卡与来源回执

- `GET /api/v1/preview-cards` 返回当前账号及已授权会话范围内的已准备图卡。普通列表、启动票据和缩略图读取都不执行脚本。静态资源仍通过 `GET /api/v1/library?type=static` 分开浏览。准备入口只提供用户明确挑选的脚本和数据，不把源文件名作为图卡标题。
- 用户确认准备后，`POST /api/v1/preview-runs` 接收 `{ "source": { "kind": "script", "language": "python", "entrypoint_ref": "…", "assets": [], "hashes": {} } }`，要求 `Idempotency-Key` 请求头。UI 应提交从选择或上传响应取得的哈希。回执包含 `operation_id`、UTC `created_at`、`status` 和 `cards`。同范围、同键同请求幂等返回已有操作。同键异请求返回 `409`。`GET /api/v1/preview-runs/{operation_id}` 读取状态，不重跑。`failed` 的错误码区分缺少已授权来源、资源库不可用、来源或捕获不匹配、隔离失败、渲染暂不可用及中断。
- 图卡有 `preview_ref`、`preview_version`、零基 `figure_ordinal`、图题或序号 `label`、`thumbnail_url`、`preview_sha256`、`capability` 及 `source_state`。`thumbnail_url` 相对 API v1 根路径。可编辑图卡还公开内容摘要 `bundle_sha256` 和 `runtime_ref`。`POST /api/v1/figures/import` 用 `{ "preview_ref": "…", "preview_version": 1, "options": { "parse_mode": "editable" } }` 选定卡片。不会重跑或另选同名来源。原 `source` 请求仅作 API 兼容，图卡请求与其互斥。
- Workbench 给来源及明确选定数据做只读快照。回执绑定入口、每个逻辑文件名和内容哈希、图序号、捕获签名和隔离运行身份。运行身份涵盖固定 SIF 内容、渲染及沙箱实现、Tavotto 运行源码和锁文件。保存的原生图冷编辑或导出只用该快照，经独立 Worker 验证捕获仍匹配。原环境不可用或重跑捕获改变时拒绝提交。部署者需要保留对应的可信镜像和代码，不会从网络自动安装包或寻找其他运行环境。
- `preview_only` 可保留不可编辑图卡，也可在快照来源缺失而经授权的原预览还在时保存。它只保存已验证的 PNG 及来源回执，不执行原脚本，不把 SVG 或文件名倒推为可编辑图件。Python 一次准备最多八张捕获图。R 原生多图通过显式 `figures` ggplot 对象清单，未映射到原生对象的多页图只提供静态卡片。不同图片候选的独立 Worker 仍受相同 CPU、内存、输出和时间上限。首次准备是同步等待，在同键请求并发时可以读取 `running` 状态。

## 当前不覆盖

- Tavotto 桌面产品的页面级多图组合、手工建组、图层 UI 不是本 API 的交付。原生 Artist 覆盖维持引擎范围，但只承诺已验证路径，不等于桌面产品逐功能同等。
- 一个 Python Figure 可含多 Axes。旧直接导入路径仍只接收一个 Figure，多图请选择图卡。未实现项目包及跨资源多脚本入口、自动装包、源脚本写回。未实现全出版检查。
- R 的 ggplot2 基础范围是已识别的纯文本图题、主轴及图例标题。复杂 R 绘图对象在可静态渲染时降级。base R 只导出静态页。静态 PDF 只导入首页。
- 只交付被测试的 Host 适配函数和 API，不修改正在运行的 Host 或部署包。到生产前需验证独立运行账号、证书、反代路径和资源策略。

上游 Tavotto 源码及移植部分适用 AGPL-3.0-only，保留 `LICENSE` 和源码出处。详细边界见 `LICENSE_REVIEW.md`。部署修改版时给网络用户提供可获取部署版本对应源码的入口。
