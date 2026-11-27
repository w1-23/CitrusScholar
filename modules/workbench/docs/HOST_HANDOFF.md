# Host 薄握手合同

Host 继续做身份和资源授权，Workbench 只读取 HTTP 回执和受控资源字节，不访问 Host 数据库。不需要 General Agent，不要求把工作台渲染模块安装到 Host API 进程。

## Host 端

`integrations/citrus_host.py` 中 `make_router` 是可选接入原型，尚未挂载到 8043。所需四个回调与当前 Host 源码形状对应：

- `src.api.main.current_user(request)`
- `src.api.main.require_owned_session(sid, user)`
- `src.session.files.list_account_files(uid, session_id=sid)`
- `src.session.files.get_account_file(ref, uid, touch=False)`

在未来启用时，使用组装层导入并传入回调，只挂接薄路由。桥接层将 Host API 现有 opaque file id 转译成 Workbench 的 `library:<id>`。筛选仅包括 Host 已分类的 SVG 和常用栅格图；PDF、Python、R 需要 Host 资源元数据明确标为 `resource_type=figure`，避免论文 PDF 出现在图件库里。会话级作用域在列表和逐文件访问都匹配 `session_id`。回调按持有者授权，不能依赖传来的用户 ID 或路径。

## 握手和资产

1. 客户端以用户身份调用 Host `POST /api/internal/workbench/open`，可传 `session_ref` 和待展示的 `source_assets`。Host 校验后返回 60 秒内可用的一次性 `launch_ticket`。票据只用于 POST，不放查询参数、日志或响应缓存。
2. `POST /api/v1/launch` 使用 `{"ticket":"..."}` 换取 `workbench_session_ref`、`workbench_url`、`opened_assets`、`return_to` 和 HttpOnly 会话 cookie。`opened_assets` 是已授权候选，不代表导入，也不执行脚本。客户端仍需手动选取导入。
3. Workbench 向固定的 `WORKBENCH_HOST_ORIGIN` 发出短时 `workbench-resource` 断言。断言有单独的 purpose，绑定资源路径、用户、项目与 `figures:read` 范围。HTTP 客户端不跟随重定向，也不用宿主 HTTP 代理设置。
4. Host `GET /_internal/workbench/assets` 支持 `type=figure`、`format`、`limit`。逐项 `.../{ref}/preview` 和 `.../{ref}/source` 返回范围内字节、类型和哈希。Workbench 校验源哈希后持久化副本和来源版本。文件路径不进入 Workbench 合同。

现有 Host 中间件对 `/api/` 强制账号 Cookie，所以用户的 open 请求保留在 `/api/`，机器资源读取使用签名保护的 `/_internal/workbench/assets`。若 Host 启用了 LanIngress，后续部署应走符合该规则的反代或单独受控内部入口，不在适配层绕过现有 LAN 校验，也不复用用户会话 Cookie。设置可信 HTTPS 入口、代理到 Workbench 的真实前缀和 `WORKBENCH_PUBLIC_ORIGIN` 在后续部署验证。

两个 ASGI 应用已验证以上闭环，包括外账号、跨会话、非图资源、票据重放及非法 Origin。额外运行了真实 Host 文件模块，用独立临时数据库和文件目录验证写入、授权列表、预览字节和 Workbench 隔离导入。现网未安装路由，未修改 Host、8043 或反代。将来可通过撤下独立路由和资源桥恢复旧入口，不需迁移 Host 数据库。Workbench 的 SQLite 文件独立快照。
