# Host 薄握手合同

Host 继续做身份和资源授权，Workbench 只读取 HTTP 回执和受控资源字节，不访问 Host 数据库。不需要 General Agent，不要求把工作台渲染模块安装到 Host API 进程。

## Host 端

`integrations/citrus_host.py` 中 `make_router` 是独立仓库的可选接入原型。所需四个回调与当前 Host 源码形状对应：

- `src.api.main.current_user(request)`
- `src.api.main.require_owned_session(sid, user)`
- `src.session.files.list_account_files(uid, session_id=sid)`
- `src.session.files.get_account_file(ref, uid, touch=False)`

在未来启用时，使用组装层导入并传入回调，只挂接薄路由。桥接层将 Host API 现有 opaque file id 转译成 Workbench 的 `library:<id>`。脚本候选和静态预览分开。实际 Host 的资料库入口仅接受用户明确上传且通过校验的 Python/R；普通论文 PDF 不进入图件库。支持的依赖数据也必须单独获准访问。会话级作用域在列表和逐文件访问都匹配 `session_id`。回调按持有者授权，不能依赖传来的用户 ID 或路径。

## 握手和资产

1. 客户端以用户身份调用 Host `POST /api/internal/workbench/open`，可传 `session_ref` 和待展示的 `source_assets`。Host 校验后返回 60 秒内可用的一次性 `launch_ticket`。票据只用于 POST，不放查询参数、日志或响应缓存。
2. `POST /api/v1/launch` 使用 `{"ticket":"..."}` 换取 `workbench_session_ref`、`workbench_url`、`opened_assets`、`return_to` 和 HttpOnly 会话 cookie。`opened_assets` 是已授权候选，不代表导入，也不执行脚本。客户端仍需手动选取导入。
3. Workbench 向固定的 `WORKBENCH_HOST_ORIGIN` 发出短时 `workbench-resource` 断言。断言有单独的 purpose，绑定资源路径、用户、项目与 `figures:read` 范围。HTTP 客户端不跟随重定向，也不用宿主 HTTP 代理设置。
4. Host `GET /_internal/workbench/assets` 支持 `type=figure`、`format`、`limit`。默认只提供脚本，`rendered_only=true` 提供静态预览，`include_rendered=true` 兼容旧列表。逐项 `.../{ref}/preview` 和 `.../{ref}/source` 返回授权字节、类型、文件名和哈希。脚本没有静态预览。
5. `type=dependency` 仅列出获准的数据，`.../{ref}/dependency` 按同一用户与项目边界返回依赖的只读内容，且 `X-Asset-Version` 等于 `X-Asset-SHA256`。Workbench 只读取 `source.assets` 显式选择的引用，不读取同目录其他数据。图件、首版和输入快照绑定在同一事务。文件路径不进入合同，冷启动编辑/导出不读取 Host。

现有 Host 中间件对 `/api/` 强制账号 Cookie，所以用户的 open 请求保留在 `/api/`，机器资源读取使用签名保护的 `/_internal/workbench/assets`。Host 启用 LanIngress 时，同机工作台仅凭与精确资源路径绑定的签名在指定 IPv4 回环地址发起只读请求。资源端仍复核账号、会话、范围和运行状态。浏览器访问继续经过 HTTPS 入口和用户会话校验，不复用机器签名。设置可信 HTTPS 入口、代理到 Workbench 的真实前缀和 `WORKBENCH_PUBLIC_ORIGIN` 在后续部署验证。

两个 ASGI 应用的合同回归使用独立临时数据库和文件目录，覆盖外账号、跨会话、非图资源、票据重放、非法 Origin 和 Workbench 隔离导入。独立模块验证不切换正在运行的服务或反代，不需迁移 Host 数据库。Workbench 的 SQLite 文件独立快照。

## 浏览器主动准备图卡

启动票据仍只提供候选授权提示。Workbench 的 `preview-runs` 和 `preview-cards` 在其独立 SQLite 内处理图卡和来源回执。准备时才按现有 `source` / `dependency` 资源角色获取已获授权的原始字节。缩略图、原生选择、补丁和导出无需 Host 识别 Artist。选中图卡后的冷重跑不访问 Host。

## Worker 冻结 Python 图件

已核验的同轮产物卡可选送 `session_ref` 和 `frozen_project`。Host 只为同账号同会话的初始冻结项目签发该选择；工作台打开时不运行代码。用户点「导入本轮图件并渲染」后，Workbench 使用同一资源断言请求 `/_internal/workbench/frozen-bundles/{ref}/1`。该回执包含原 Python 入口、绑定名和冻结表字节，不包含静态图或文件系统路径。工作台校验各段 SHA-256，将源与依赖保存为自己的不可变输入，然后在现有隔离渲染进程内还原 `data`。冷重开后的编辑或导出使用保存的输入，不再次依赖 Host。

上线时设置 `WORKBENCH_FROZEN_PACKAGES` 为**已封存且已校验的 Host 发布包**中的 `packages` 绝对路径。不要绑定开发目录、账户数据目录或任何密钥目录。只在冻结 Python 任务中只读挂载该路径下的 `citrus_figures` 与 `citrus_audit` 两个包。打包时排除缓存、字节码和符号链接，保留两包的来源与校验清单。Workbench 将隔离镜像、渲染器、Tavotto 及这两个包的 Python 源构成版本摘要。重新发布时保留原运行版本以支持冷重绘；若版本不匹配，拒绝写入修改，不用不同运行环境悄悄重绘。

验证可用现有渲染镜像、Tavotto 与上述路径运行 `tests/test_real_workbench.py::test_real_frozen_import_cold_edit_and_export`。测试只使用临时 SQLite 和进程内 ASGI，无需启动 HTTP 监听。首版 Python 使用原合同。经验证的当前后续 Python 版本使用独立签名路径 `/_internal/workbench/frozen-bundles/{ref}/v2/{revision}`，额外绑定冻结样式和确切版本。自动 R 交接不在该合同内。工作台普通 R 导入接受明确选定的 R 脚本和依赖，使用保存的字节原生重绘，不编辑 SVG。验收见 `test_real_r_script_resolves_only_passed_relative_data`。

## 数据库与回滚兼容性

新增运行回执表不改变既有图件表。普通直接导入的单来源、无显式数据依赖图件，不把值为 `null` 的 `capture` 和 `source.receipt` 写入修订文档，旧版严格读取器可打开该格式。保留既有字段的空值及所有非空捕获或回执字段。不因此批量重写历史行。

带预览回执或 CSV 等显式依赖的图件必须使用认识对应快照与重跑规则的后端。旧后端不能可靠打开或编辑这些图件，不能通过删除回执或从过期备份回填数据库来降级。需要回滚时优先评估切回已验证的界面而保留兼容后端。若只能退回旧后端，须先把无法安全读取或渲染的图件及相关操作在入口侧明确标为暂不可用，并保留当前数据库。

切换时把 `WORKBENCH_PREVIOUS_SOURCE` 设为前一只读发布的 `source` 绝对路径。 若需保留更早一代回执，同时设置 `WORKBENCH_OLDER_SOURCE` 为更早封存源码的绝对路径。冷重绘按已保存的语言和完整运行指纹查找，包括冻结 Python，不会用最新渲染器覆盖旧记录。导入旧预览卡或编辑旧普通脚本仅在前一运行器准确匹配原回执时继续；不批量迁移或改写回执。旧路径被替换、可写或摘要不符时，保持原版本不动。

带 `source.frozen` 的项目也只能由支持冻结方案的后端打开与编辑。回退时保留图件数据库和已接收输入，不删除新字段。

### 后续 Python 版本的发布与回退

先封存支持读取、编辑和导出 v2 图件的兼容发布，保持新导入关闭。Host 部署清单 `external_modules.workbench.styled_import` 缺省为 `false`；Workbench 环境 `WORKBENCH_STYLED_IMPORT` 未设时关闭。开启前使用副本确认兼容发布能读取真实 v2 图件并冷重绘。其后只在两侧同时开启，Host 清单明确设为 `true`、Workbench 环境设为 `1`。回退到同一兼容发布的关闭配置会阻止新交接，不影响已保存图件或首版交接。不要把不认识 v2 数据的原线上版本当作启用后的常规回滚点；紧急切回时，保留数据库并恢复兼容后端后再编辑。不可删样式表、删除回执或回填历史数据库。


## 聊天接入仅接手 Worker 图件

生产聊天接入设置 `WORKBENCH_HOST_WORKER_ONLY=1` 后，从 Host 签发的会话返回 `worker_only: true`。无图件时只提示返回聊天，有图件时仅允许显式导入票据绑定的 Worker 冻结脚本、数据及修订。不展示本机上传、普通导入或预览准备，相关写接口也拒绝该会话。已保存图件仍可打开、编辑和导出，不改数据库。独立令牌模式不受该开关影响。
