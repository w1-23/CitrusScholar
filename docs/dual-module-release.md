# 双模块初步融合

这是 CitrusScholar、Workbench 和 Selfcorpus 的统一源码发布。三个组件一起交付、分别运行和升级。统一打包不要求共享数据库、Python 环境或权限。

| 组件 | 源码位置 | 运行边界 |
| --- | --- | --- |
| CitrusScholar Host | `01_src`、`packages`、`04_scripts` | 前端入口与授权，独立 Code Worker。现网回环 8060。 |
| Workbench 0.1.0 | `modules/workbench` | 独立图件编辑与离线渲染，Host 通过签名票据和资产回调接入。现网回环 8062。 |
| Selfcorpus 0.1.2 | `modules/selfcorpus` | 独立检索 API、收录 Worker 与数据库，Host 通过版本化签名 HTTP 接入。现网回环 8061。 |

浏览器只访问 Host。同一账号可以使用自己的文件，所有已批准账号可读通过校验的公开共享论文。Workbench 和 Selfcorpus 的本地数据路径各自配置。Workbench 独立服务不打开 Host 或 Selfcorpus 的数据库，Host 不导入 Workbench 或 Selfcorpus 的业务实现。

## 校验和构建

```sh
python3 -B 04_scripts/package_dual_module_source.py --revision HEAD --output /tmp/citrusscholar-dual-module-source.tar.gz
python3 -m pip wheel --no-deps --wheel-dir /tmp/citrusscholar-wheels modules/selfcorpus
```

- Selfcorpus 依赖 Python 3.12+。按 [独立部署说明](../modules/selfcorpus/docs/deployment.md) 配置私有密钥、独立目录和模型快照，运行 `selfcorpus init`、`selfcorpus serve --port 8061` 和 `selfcorpus worker`。CPU/GPU 依赖按同一部署的设备选择安装。应校验下载的模型和镜像。
- Workbench 按 [独立部署说明](../modules/workbench/README.md) 配置独立数据库、签名和渲染环境。通过 `scripts/fetch_tavotto.py` 获取锁定的上游版本。接 Host 时仅在受控回环开启内部 API，并把 Workbench 对应源码下载地址配置给用户。
- Host 沿用 [Apptainer 部署说明](../01_src/apptainer/README.md) 和 `04_scripts/stage_8043_release.py`。显式配置 `external_modules` 的 Selfcorpus 客户端校验值与双模块密钥文件，再运行 Web 和匹配的 Worker。不要直接复制生产部署配置到另一个环境。

`01_src/apptainer/dual-module.example.json` 是 Host 封存命令 `--external-modules` 的输入模板。客户端目录只放 Selfcorpus wheel 中的 `corpus/__init__.py` 和 `corpus/auth.py`，逐个计算 SHA-256，将文件、目录和密钥的绝对路径填入模板，客户端目录设为只读。两个密钥文件各用一个随机值，只授权自己的服务账户读取。Workbench 回调地址与浏览器 Origin 应由同一 Host 入口配置。会话自动补库需要与 Selfcorpus 0.1.2 或兼容版本一起启用。

这是源码包，不含生产 SQLite、用户文献与图件、令牌、私钥、E5 权重或 Apptainer 大镜像。它不会用默认凭据，也不是下载后无需部署即可运行的镜像。现网已验证的 Host 基线为 `977a85941b2420023322d87745052ee057d44b3c`，这次发布只整理源码布局和打包文件。

## 许可证与回滚

Host MIT 许可与 Workbench AGPL-3.0-only 分别保留，详见根许可证和模块许可证。Tavotto 上游源码从锁定提交取得，不混入 Host。Selfcorpus 没有被本次打包另行授予开源许可证，不能把根目录 MIT 许可视作对该目录的授权。各模块还保留各自依赖的许可与声明。

运维回滚点名为 **双模块初步融合**。版本或配置回滚复用已校验的发布包和服务单元。各模块可独立回滚，先停止被替换进程，验证新旧 API 合同兼容；除明确的数据恢复操作外，勿覆盖较新的数据库。运维快照不随 GitHub 发布。
