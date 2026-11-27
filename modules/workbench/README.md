# Citrus Figure Workbench · API MVP

独立后端，用 Tavotto 的实际 Matplotlib Artist 和已绘制 manifest 实现选取、自动图内分组、原生属性覆盖和导出。R 通过独立 Worker 运行，静态图件不冒充语义可编辑对象。包含独立工作台前端。Host 通过签名协议提供登录和资料授权，不运行 Workbench 的项目、图件编辑或渲染逻辑。

## 独立测试或部署

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
.venv/bin/uvicorn wb.api:create_app --factory --host 127.0.0.1 --port 8062
```

调用 `POST /api/v1/standalone`，以 JSON `{"token":"<configured-token>"}` 登录并保存会话 cookie，再上传、导入和调用图件 API。没有设置独立令牌时，此入口不授权。部署使用 HTTPS，设置固定独立会话密钥 `WORKBENCH_SESSION_KEY`，配置 Host 集成才设置 `WORKBENCH_HOST_KEY` 和 `WORKBENCH_HOST_ORIGIN`。密钥不少于 32 字节，不使用用户 shell 的账号密码。

## 简明调用顺序

1. `POST /api/v1/assets` 上传请求原始字节，`X-Filename` 为 URI 编码文件名，取回 `asset_ref`。
2. `POST /api/v1/figures/import` 提交 `source.kind`、`source.language`、`source.entrypoint_ref` 及 `options.parse_mode`。资料库路径先请求 `GET /api/v1/library`，再用 `library:` 引用导入。导入会保留来源指纹，出厂不写回源脚本。
3. `GET /api/v1/figures/{id}/objects` 取对象。在 Python 中 `native_manifest` 是 Tavotto 的原生绘制清单，`elements[].editable` 列出由上游提供的属性及值，组由 `groups` 给出。`GET .../preview?revision=1` 查同版本 PNG。
4. `POST .../pick` 传 `x`、`y`（预览图左上角原点的 0–1 分数）、可选 `locked_gids`、`current_gid`、`revision`。没有 `current_gid` 时普通点击，有该字段时按上游重叠顺序轮换。
5. Python `PATCH .../native-overrides` 传 `expected_revision` 及 `patches:[{"gid":"axes_0.lines_0","prop":"linewidth","value":4}]`，可带原生 `identity`。未知对象、被拒属性或版本冲突不会推进版本。`GET .../groups/{gid}` 查组，`POST .../groups/resolve` 查带锁定规则的展开，`POST .../groups/{gid}/move` 和 `POST .../groups/{gid}/scale` 在同一个版本内整体移动或绕中心等比缩放原生自动组全部成员。R 使用 `PATCH .../overrides` 和 `overrides:{"plot.title":{"text":"标题"}}`。
6. `GET .../history` 和 `POST .../restore`（`expected_revision` 与 `revision`）回滚图件，恢复会创建新版本。`POST .../export` 取 PNG/SVG/PDF，应记录响应的 `X-Figure-Revision`、`X-Content-SHA256` 和 `X-Export-Id`。`width_mm`、`height_mm`、`dpi` 独立于浏览器显示缩放。

Host 薄接入和验证说明见 `docs/HOST_HANDOFF.md`。运行测试前安装 `pytest==8.3.5`，随后执行 `python -m pytest -q`。缺少隔离镜像时，真实渲染与隔离集成测试跳过。未配置时只有不需要真正渲染的协议、鉴权及拾取测试可运行。

## 当前不覆盖

- Tavotto 桌面产品的页面级多图组合、手工建组、图层 UI 不是本 API 的交付。原生 Artist 覆盖维持引擎范围，但只承诺已验证路径，不等于桌面产品逐功能同等。
- 一个 Python 源文件可含多 Axes，当前只接受一个 Figure。未实现项目包及跨资源多脚本入口、自动装包、源脚本写回。未实现全出版检查。
- R 的 ggplot2 基础范围是已识别的纯文本图题、主轴及图例标题。复杂 R 绘图对象在可静态渲染时降级。base R 只导出静态页。静态 PDF 只导入首页。
- 只交付被测试的 Host 适配函数和 API，不修改正在运行的 Host 或部署包。到生产前需验证独立运行账号、证书、反代路径和资源策略。

上游 Tavotto 源码及移植部分适用 AGPL-3.0-only，保留 `LICENSE` 和源码出处。详细边界见 `LICENSE_REVIEW.md`。部署修改版时给网络用户提供可获取部署版本对应源码的入口。
