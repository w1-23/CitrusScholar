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

现有 Host 中间件对 `/api/` 强制账号 Cookie，所以用户的 open 请求保留在 `/api/`，机器资源读取使用签名保护的 `/_internal/workbench/assets`。若 Host 启用了 LanIngress，后续部署应走符合该规则的反代或单独受控内部入口，不在适配层绕过现有 LAN 校验，也不复用用户会话 Cookie。设置可信 HTTPS 入口、代理到 Workbench 的真实前缀和 `WORKBENCH_PUBLIC_ORIGIN` 在后续部署验证。

两个 ASGI 应用的合同回归使用独立临时数据库和文件目录，覆盖外账号、跨会话、非图资源、票据重放、非法 Origin 和 Workbench 隔离导入。独立模块验证不切换正在运行的服务或反代，不需迁移 Host 数据库。Workbench 的 SQLite 文件独立快照。

## 浏览器主动准备图卡

启动票据仍只提供候选授权提示。Workbench 的 `preview-runs` 和 `preview-cards` 在其独立 SQLite 内处理图卡和来源回执。准备时才按现有 `source` / `dependency` 资源角色获取已获授权的原始字节。缩略图、原生选择、补丁和导出无需 Host 识别 Artist。选中图卡后的冷重跑不访问 Host。

## 数据库与回滚兼容性

新增运行回执表不改变既有图件表。普通直接导入的单来源、无显式数据依赖图件，不把值为 `null` 的 `capture` 和 `source.receipt` 写入修订文档，旧版严格读取器可打开该格式。保留既有字段的空值及所有非空捕获或回执字段。不因此批量重写历史行。

带预览回执或 CSV 等显式依赖的图件必须使用认识对应快照与重跑规则的后端。旧后端不能可靠打开或编辑这些图件，不能通过删除回执或从过期备份回填数据库来降级。需要回滚时优先评估切回已验证的界面而保留兼容后端。若只能退回旧后端，须先把无法安全读取或渲染的图件及相关操作在入口侧明确标为暂不可用，并保留当前数据库。
