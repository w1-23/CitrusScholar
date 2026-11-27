# Tavotto Phase 0 许可证与引擎复用评估

## 源码基线

以 [b6de338c3b8d104d64930f18828b9675acb261a5](https://github.com/Tavotto/Tavotto/tree/b6de338c3b8d104d64930f18828b9675acb261a5) 为权威基线。`TAVOTTO_UPSTREAM.lock.json` 固定了 Git commit 和核心文件 blob。Windows 压缩包无 Git 元数据，版本字符串为 0.21.3。逐文件比对确认脚本入口、会话、manifest、overrides、排版、历史、导出与该提交匹配，但 `preflight.py` 不匹配。应从锁定提交取源码，不将本机压缩包和 Git 上游混拼。

## 权利和义务

- [LICENSE](https://github.com/Tavotto/Tavotto/blob/b6de338c3b8d104d64930f18828b9675acb261a5/LICENSE) 与包元数据明确为 AGPL-3.0-only。改版上线后，按第 13 条向所有通过网络与其交互的用户显著提供免费获取运行版本对应源码的机会。分发容器、二进制等另按对应条款处理。应保留版权、许可证和适当法律声明，记录修改及日期，并提供适用的构建、安装和运行脚本及接口定义。项目数据、科研图件、用户脚本、令牌和环境秘密不放进源码包。
- [Licensing](https://github.com/Tavotto/Tavotto/blob/b6de338c3b8d104d64930f18828b9675acb261a5/docs/legal/LICENSING.md)、[CONTRIBUTING](https://github.com/Tavotto/Tavotto/blob/b6de338c3b8d104d64930f18828b9675acb261a5/CONTRIBUTING.md) 及其个人、公司 CLA 说明，上游贡献流程有 CLA 检查。自建 AGPL 下游不因使用上游代码而自动签上游 CLA。上游 CLA 并不授予下游商业再许可资格。若选闭源商用路径，应在取得覆盖指定代码、部署方式和第三方边界的书面授权后再依该授权推进。Tavotto 商标另外处理，前端使用自己的品牌。
- [第三方声明](https://github.com/Tavotto/Tavotto/blob/b6de338c3b8d104d64930f18828b9675acb261a5/THIRD_PARTY_NOTICES.md) 涉及 MIT、BSD、Apache、CC0 资产和独立 GPL 调色板等。候选运行闭包需从锁文件按实际构建生成 SBOM，不把桌面资源、上游图标、插件、可选 GPL 调色板和退役测试依赖默认打入交付物。Flask、Matplotlib、NumPy 与 RenderCore 所用 pikepdf、Pillow、lxml、fonttools、uharfbuzz、pypdfium2 按实际分发包和底层二进制分别核对。额外运行时、R 包和 Solar 图标在版本选定后单独记录。现阶段完成源码级识别，未声称已有可发布镜像的完整依赖审计。
- [字体 allowlist](https://github.com/Tavotto/Tavotto/blob/b6de338c3b8d104d64930f18828b9675acb261a5/src/tavotto/rendercore/fonts_allowlist.json) 列出 Liberation 与 Noto，许可均为 OFL-1.1。包内只纳入确实使用的字体和对应声明，校验字体与许可证文件 hash，并处理保留字体名和子集嵌入。用户自带字体不自动纳入分发。

## 推荐的合规与复用路径

在独立仓库构建 AGPL Workbench，包含使用 Tavotto 的适配、实际构建闭包及与其形成同一作品的服务和前端。提供按版本生成的对应源码包及用户可访问的源码入口。Host 保持独立代码与运行时，通过通用的 HTTP、受控资产引用和签名握手传递授权。不得把“另一个进程”或“另一个仓库”当作规避许可的依据。对最终打包和通信边界做发布复核。如果必须保持 Workbench 闭源，另一选项是先取得书面许可，不改用自研路径冒充 Tavotto 复用。

## 引擎边界

| 阶段 | 上游候选 | Workbench 自有部分 |
| --- | --- | --- |
| 脚本加载和 Figure 捕获 | `engine/worker.py`、`figcapture.py`、`figsession.py` 中 `LiveFigureSession` | 接收受控引用，调度隔离进程，收集运行回执。不采用桌面信任模型、自动依赖安装或 native 降级 |
| 对象采集和编辑 | `manifest.py` 的 `build_manifest` 与 `capture_preview_manifest`，`overrides.py`、`axestraversal.py`、`legendmodel.py`、`tickmodel.py` | 映射稳定的公开对象 ID 和属性，限制可写字段，重跑与重放后报告失配。验证真实 Matplotlib 对象并显式降级 |
| 排版和历史 | `layoutannotate.py`、排版文档和 `layoutsession.py`、`versionstore.py` | 重用适合的毫米布局和非破坏编辑能力。不沿用上游本地路径、用户目录或桌面状态来实现多人存储 |
| 导出和出版检查 | `figsession.py` 的 `render`、`preflight.py`、`exportjob.py`、`rendercore` | 评估引擎导出与多面板组合实现，再健康检查真实文件及版本哈希。浏览器缩放不参与物理尺寸计算 |
| R | 无可替代 R 语义的 Tavotto 接口 | 单独在 R Worker 调用 `ggplot_build()`，通过 R overrides 原生重绘。base R 先图形设备输出和受限元数据。不从 SVG 猜回绘图语义 |

通过适配层封装上游内部接口，并以锁定版本的真实测试控制升级。候选不采用 Tavotto 桌面或 Agent 作为骨架，也不把旧 Host 工作台当作新工作台骨架。

## 远端和上线边界

开发目标目录为空，未初始化 Git，也没有已配置的发布仓库 URL。读取了主产品 `main-8043`（`38e0d25c730b503c9330a108f45ef02cd51af45a`）当前源码。现有 `/figure-workbench` 在 Host 验证登录，`/api/v2/figure-workbench` 直接实现旧工作台逻辑。未来保留入口与资源授权和返回导航，工作台的项目、编辑和渲染不进入 Host，切换保留可回滚旧路由。当前没有修改主源码、进程、服务配置或 release。

远端普通账号不能访问 Docker daemon，`bwrap --unshare-all` 的网络 namespace 试运行失败。发现 Apptainer 1.5.5，尚未验证能满足无网络、隔离输入/输出、限额和凭据控制的全部准入条件。先做可信测试样本和隔离验证，禁止自动退回宿主直接执行用户脚本。上线前还要验证两种语言的资源限制、用户隔离和回滚。
