# MVP 验证记录

## 受测环境

- Linux 远端，普通账号，Apptainer 1.5.5，systemd 用户 manager 和 cgroup v2。
- 已有测试 SIF `sandbox-nature-r.sif`，SHA-256 `f576f83f98f6c18c972babb866e0065ca5d694cb6c9b5fe47dacd4b78dda8f62`。此镜像仅作为本轮的可信验证输入，不包含在独立仓库，也未承诺为生产镜像。Python 3.12.16、Matplotlib 3.10.6、R 4.4.3、ggplot2 4.0.4。现有镜像有 patchwork 1.3.2，无 cowplot。
- Tavotto 依据 `TAVOTTO_UPSTREAM.lock.json`，检查了 8 个引擎文件，另核对许可证、依赖锁、运行时锁及字体清单。上游归档在本轮的 SHA-256 为 `5cc72a4756167660463b80d32ffcae2be963084ed846b779e836c8940f397220`。
- Host 审计从 `main-8043` 的 `38e0d25c730b503c9330a108f45ef02cd51af45a` 开始，最终用 `ec66719a97dc95ef7ad0d83411a006a3be74eba7` 的资料文件模块复验通过。它使用独立临时数据库、目录及测试身份。没有访问生产数据库、Cookie 或服务，没有修改 Host 或反代。
- 最终 42 条测试全部通过，无跳过。远端 AST 架构自检检查 13 个生产 Python 模块、19 条内部依赖边，无循环依赖。

## 验证覆盖

| 项目 | 证据 |
| --- | --- |
| Tavotto Artist 编辑 | 真实 Matplotlib 线、散点、柱、图题、坐标轴、图例。API 验证原生文字、字号、线宽、颜色和拒绝非法属性。原稿 hash 不变，版本恢复返回同一份预览。 |
| 原生点选 | 真实绘制 manifest 上测试题名拾取。专项覆盖路径距离、文字框、图层遮挡、锁定、双轴与重叠轮换。以固定提交的上游 TS 函数作为参考，33 份 manifest 对拍了 4,102 个坐标和 180 个分组/关系案例。此对拍为专项开发验证，仓库回归是 `tests/test_picking.py`，未运行 Tavotto 浏览器全套测试。 |
| 原生组 | 两子图共享色条，三成员原生组。校验结构、锁定色条时全组拒绝、批量平移及绕中心缩放。独立撤销/重做用历史恢复实现。未覆盖桌面 UI 手工建组或页面容器。 |
| Python 导出 | PNG、SVG、PDF。127×76.2 mm 与 100 DPI 得到 500×300 PNG。SVG 含修改后的真文本与颜色，导出 trace 绑定编辑版本和内容 hash。 |
| R 语义范围 | ggplot2 对已有纯文本标题、轴标题、图例标题进行原生编辑。带 facet 的夹具及显式轴、合并和连续图例通过，表达式和无法无歧义映射的对象显式降级。base R 有三格式静态输出，未测试缺失的 cowplot。 |
| 静态资产 | SVG 静态读取、PNG 预览、PDF 导出。真实 Host PNG 记录、列表筛选、源字节及哈希通过，普通论文 PDF 未进图件库。 |
| 隔离 | worker 在独立 net/PID/IPC namespace，无可用外部路由，挂载源与渲染器只读。每任务检验内核限制、64 MiB 私有 scratch、2 GiB 内存、48 tasks、30 秒上限。过限、link/FIFO、坏回包、环境和宿主路径不可见有回归样例。 |
| 访问与重启 | 会话 cookie、一次性 Host 开放票据、资源签名、账户/项目隔离、版本冲突、历史重开及 SHA-256 验证。 |

## 复验

```sh
export WORKBENCH_TAVOTTO="$PWD/.workbench-dev/tavotto"
export WORKBENCH_IMAGE=/absolute/path/to/trusted-workbench.sif
export WORKBENCH_SANDBOX_SMOKE_IMAGE="$WORKBENCH_IMAGE"
export CITRUS_HOST_CHECKOUT=/absolute/path/to/read-only-host-checkout
python -B -m pytest -q
```

生产联调仍需在实际 HTTPS 和反代配置下挂载薄接口。测试不启动或切换 8043，也不覆盖旧 release。
