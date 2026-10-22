# 安装、升级和发布

TCAD Agent CLI 0.3.0 的发行来源是本仓库的 `v0.3.0` GitHub tag。命令安装到独立 Python
环境，不要求下载整个工作区、不要求管理员权限。程序需要 Python 3.11+；uv 可以管理所需 Python。

## 推荐：uv 或 pipx

```sh
uv tool install --python 3.12 git+https://github.com/18273778775/tcad-agent.git@v0.3.0
```

该推荐形式需要本机已有 uv 与 Git；无需手动 clone。已有 pipx、Git 与 Python 3.11+：

```sh
pipx install git+https://github.com/18273778775/tcad-agent.git@v0.3.0
```

也可使用带 `.zip` 后缀的 canonical archive URL（部分网络中 GitHub archive 重定向较慢）：

```sh
uv tool install --python 3.12 https://github.com/18273778775/tcad-agent/archive/refs/tags/v0.3.0.zip
```

安装后执行 `tcad --version`、`tcad capabilities`、`tcad schema request`。
如果命令不在 PATH，uv 用户执行 `uv tool update-shell`，pipx 用户执行 `pipx ensurepath`，然后重开终端。
程序正常操作只向 stdout 输出 JSON；安装器的下载与安装日志不属于运行时协议。

安装设计参考 [uv tools 官方指南](https://docs.astral.sh/uv/guides/tools/) 与
[pipx 官方文档](https://pipx.pypa.io/stable/)。使用其隔离工具环境，无需引入 CLI 框架或绑定某个 IDE。

## 无 uv 的 Windows PowerShell

已有 Python 3.11+：

```powershell
irm https://raw.githubusercontent.com/18273778775/tcad-agent/v0.3.0/scripts/install.ps1 | iex
```

下载的是本项目固定 tag 的安装脚本；也可先保存并检查脚本再运行。
脚本在 `%LOCALAPPDATA%\tcad-agent-cli\venvs\` 创建独立环境，成功后才切换
`%LOCALAPPDATA%\tcad-agent-cli\bin\tcad.cmd`，同时更新当前进程与用户 PATH。
失败不会切换已有 shim。不会修改系统 PATH、注册计划任务或要求管理员权限。
此脚本不要求 Git；默认从 `codeload.github.com` 的固定 tag 直链下载到私有临时 `.zip` 后安装，
避免 archive 重定向与无扩展名 URL 的包管理器识别问题。单次下载限时 60 秒，成功或失败均清理该临时下载。

可指定路径或本地 wheel，并禁止持久修改用户 PATH：

```powershell
.\install.ps1 -PackageSource C:\packages\tcad_agent-0.3.0-py3-none-any.whl -InstallRoot C:\Tools\tcad -NoModifyUserPath
```

可以加 `-UsePip` 明确使用 Python 自带 pip，便于没有 uv 的环境或管理员已有 pip 配置的环境。

本地 wheel 的依赖仍需能够从包索引解析，或预先设置 `PIP_NO_INDEX` / `PIP_FIND_LINKS`
指向可信依赖目录。脚本不自动下载另一个软件的安装脚本。

## 无 uv 的 Linux / macOS

已有 Python 3.11+、venv 和 pip 时：

```sh
curl -fsSL https://raw.githubusercontent.com/18273778775/tcad-agent/v0.3.0/scripts/install.sh | sh
```

安装结束后执行 `. "$HOME/.local/share/tcad-agent-cli/env"`。该操作可放入 shell profile。
脚本不会主动改写 shell profile。可以通过 `TCAD_INSTALL_ROOT` 与 `TCAD_INSTALL_SOURCE`
指定安装目录与本地 wheel；使用 XDG_DATA_HOME 时 env 文件也位于该目录下。
默认同样使用固定 tag 的 codeload 直链，经 curl 下载为私有临时 `.zip` 后安装，不要求 Git；
正常退出与安装失败都会清理临时下载。已带后缀的 URL、本地 wheel/ZIP 与显式 Git source 仍可覆盖使用。

## DWG 转换器

DWG 是可选外部后端；DXF 及模型工作流不依赖它。Windows x64 安装：

```sh
tcad setup-dwg
tcad doctor
```

安装固定官方 LibreDWG 0.14 win64 ZIP 与官方 COPYING，分别验证固定 SHA-256。
写入用户数据目录并保留来源、许可、可执行文件哈希记录；发现安装时再次验证可执行文件。
不会把第三方 EXE/DLL 放入本项目 wheel，也不会自动启动原始看图软件。

数据目录由 `TCAD_DATA_HOME` 覆盖，默认 Windows 为 `%LOCALAPPDATA%\tcad-agent`，
其他平台为 `${XDG_DATA_HOME:-~/.local/share}/tcad-agent`。普通读取不会触发下载。
Linux/macOS 自行安装 LibreDWG，把 `dwg2dxf` 放入 PATH 或设置 `TCAD_DWG2DXF` 为绝对路径。
通过 `tcad doctor` 查看实际后端。不同转换器的复杂对象兼容性仍需逐图验证。

## 升级和卸载

固定 URL 安装不会悄悄追踪 main。升级时把版本替换成新的已发布 tag，并重新安装：

```sh
uv tool install --force --python 3.12 git+https://github.com/18273778775/tcad-agent.git@v0.3.0
uv tool uninstall tcad-agent
```

pipx 对应 `pipx install --force <新版本URL>` 和 `pipx uninstall tcad-agent`。
独立安装脚本的环境均保存在其明确的安装目录内；卸载时移除该目录及相应用户 PATH 项。
DWG 后端与查询缓存位于用户数据目录，可单独清理；请先确认其中没有需要保留的数据。

## 维护者：CI 与可选 PyPI 发布

CI 在 Windows/Linux、Python 3.11/3.12/3.13 执行测试、Ruff、构建和独立 wheel 安装。
独立安装测试在源码目录外通过 `uv tool install <wheel>` 安装并执行建图、查询，验证不依赖
源码 checkout 或 scripts。Windows 的真实 DWG 作业显式安装后端与独立上游 fixture，
并拒绝将 skipped 算作通过。

尚未完成 PyPI 账户授权时，不应对外使用 `pip install tcad-agent` 作为已验证安装命令。
仓库提供 `publish-pypi.yml` 的手动工作流，但默认禁用。维护者需在 PyPI 配置本项目
Trusted Publisher（owner `18273778775`、repository `tcad-agent`、workflow `publish-pypi.yml`、
environment `pypi`），在 GitHub 创建对应环境，并设置仓库变量 `TCAD_PYPI_ENABLED=true`。
然后手动选择已经测试的发行 tag 发布。参见 [PyPI 官方 Trusted Publishing 文档](https://docs.pypi.org/trusted-publishers/using-a-publisher/)。
该流程使用 OIDC，无需把长期 PyPI token 放进源码或 CI。
