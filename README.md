# TCAD Agent CLI

**作者：LiTao** · [Apache-2.0](LICENSE) · 允许商业使用

专供 agent 使用的无界面 2D CAD CLI。支持 DXF 读取、分页查询、精确测量、
JSON 事务编辑、SVG 预览，以及通过 LibreDWG 读取 DWG。
普通命令默认输出 JSON；提供可发现的 JSON Schema、稳定错误码和 JSONL 标准输入服务。
另提供显式映射的完整矩形房间提取、GLB/OBJ/三维 DXF 建模和文件回读核验。
已跑通长宽高事务修改后的完整验收，见 [房间流程与实测结果](docs/room-workflow.md)。

本项目是在分析当前目录的旧 CAD 看图软件分发包后独立实现的工具。
原始包没有源码或已验证的公共 SDK，新工具不依赖其中的 EXE/DLL。
详见 [静态逆向报告](docs/reverse-engineering.md) 与 [GitHub 项目选型](docs/upstream-projects.md)。

![完整房间模型与 CAD 尺寸对照](docs/assets/room-validation.jpg)

完整房间样例已通过 55 次 CLI 调用和 91 项测试。净尺寸从
6000 × 4500 × 3000 mm 事务修改为 6400 × 4800 × 3200 mm 后，
重新打开三维模型的最大净尺寸误差为 0.000191 mm（容差 0.01 mm）。
这是显式映射的受控房间验收，不代表任意施工图自动识别。
公开的验收记录、六个模型和离线交互页位于 [docs/demo/](docs/demo/README.md)。
下载仓库后，可直接用浏览器打开 `docs/demo/viewer.html`。

## 安装与直接运行

Windows PowerShell，Python 3.11+；有 uv 时自动使用 uv。

```powershell
git clone https://github.com/18273778775/tcad-agent.git
cd tcad-agent
.\scripts\bootstrap.ps1 -WithDwg
.\tcad.ps1 doctor
.\tcad.ps1 capabilities
```

`-WithDwg` 在项目 `.tools/` 内下载固定版本的官方 LibreDWG，并校验 SHA-256。
只处理 DXF 时可以省略。脚本不修改系统 PATH。
`tcad.ps1` 自动发现本地转换器，也接受环境变量 `TCAD_DWG2DXF`。

安装后也可使用虚拟环境内命令，或激活环境后直接使用 `tcad`：

```powershell
.\.venv\Scripts\tcad.exe capabilities
$env:TCAD_DWG2DXF = (Resolve-Path '.tools\libredwg-0.14\dwg2dxf.exe').Path
.\.venv\Scripts\python.exe -m agentcad doctor
```

在其他平台执行 `python -m pip install -e .`，将平台对应的 `dwg2dxf` 放到 PATH
或设置 `TCAD_DWG2DXF`。附带转换器安装脚本仅支持 Windows。

## 一次完整的图纸流程

```powershell
New-Item -ItemType Directory -Force artifacts | Out-Null
.\tcad.ps1 apply --plan examples/plate.json --output artifacts/plate.dxf
.\tcad.ps1 info artifacts/plate.dxf
.\tcad.ps1 query artifacts/plate.dxf --type CIRCLE --layer HOLES --limit 10
.\tcad.ps1 render artifacts/plate.dxf --output artifacts/plate.svg
```

`examples/plate.json` 创建 120×80 mm 外轮廓、4 个 R5 孔、参照线和文字。
`query` 返回 DXF 原生 handle；随后将查询到的 handle 传给测量或编辑命令：

```powershell
.\tcad.ps1 measure artifacts/plate.dxf --handle <查询到的handle>
.\tcad.ps1 query artifacts/plate.dxf --bbox 0 0 50 50 --offset 0 --limit 100
.\tcad.ps1 query artifacts/plate.dxf --type TEXT --text '120'
```

DWG 文件可直接传入 `info/query/measure/render/apply --input`，内部会先生成临时 DXF。
也可显式转换并保留中间文件：

```powershell
.\tcad.ps1 convert drawing.dwg --output artifacts/imported.dxf --timeout 60
.\tcad.ps1 info drawing.dwg
```

## 编辑计划

计划是声明式 JSON，不执行 Python 或 shell。获取准确格式：

```powershell
.\tcad.ps1 schema plan
.\tcad.ps1 schema request
.\tcad.ps1 apply --input artifacts/plate.dxf --plan edits.json --output artifacts/revised.dxf --dry-run
.\tcad.ps1 apply --input artifacts/plate.dxf --plan edits.json --output artifacts/revised.dxf
```

每个计划中的操作按顺序执行。全部验证和修改成功后才原子发布输出。
默认拒绝覆盖；明确传 `--overwrite` 可替换已有输出。
失败不会发布部分图纸；`--dry-run` 同样验证实体操作，但不写文件。
输出父目录须已存在。

操作支持：

| 操作 | 参数与用途 |
| --- | --- |
| `layer` | `name`、可选 `color`，创建或设置图层 |
| `add` | `entity`，添加 LINE/CIRCLE/ARC/LWPOLYLINE/TEXT/MTEXT/RECTANGLE |
| `update` | `handles`、`attributes`，修改 layer/color/text |
| `delete` | `handles`，删除 Model 空间中的实体 |
| `transform` | `handles`，可选 scale/rotate/translate，顺序为缩放、绕原点 Z 轴旋转、平移 |

例如：

```json
{
  "version": 1,
  "operations": [
    {"op":"transform","handles":["2F"],"rotate":90,"translate":[20,0]},
    {"op":"update","handles":["30"],"attributes":{"layer":"HOLES","color":1}}
  ]
}
```

这里的 handle 仅为格式示例，必须替换为当前图纸查询所得值。
已有图纸省略 units 时保留单位；显式设置 units 只改单位元数据，不自动缩放。
已有图纸禁止改变 DXF 版本，以防序列化时丢失复杂实体；新图可指定 dxf_version。

## Agent 协议

普通命令成功退出 0，操作失败退出 1，命令参数或 JSON 读取失败退出 2。
`--help`、`--version` 输出人类可读文本；其余正常操作输出 JSON。
成功响应结构：

```json
{"schema_version":"1.0","id":"inspect","ok":true,"result":{}}
```

失败响应结构：

```json
{"schema_version":"1.0","id":"inspect","ok":false,"error":{"code":"FILE_NOT_FOUND","message":"Drawing does not exist."}}
```

持续进程服务：

```powershell
.\tcad.ps1 serve
```

向 stdin 每行发送一个请求，每个输入行都得到一个独立 JSON 响应，id 原样关联：

```json
{"id":"inspect","command":"info","args":{"input":"artifacts/plate.dxf"}}
{"id":"holes","command":"query","args":{"input":"artifacts/plate.dxf","types":["CIRCLE"],"limit":10}}
```

读取 [examples/requests.jsonl](examples/requests.jsonl) 可演示连续调用；请先创建 artifacts，
并使用没有同名输出的目录。`serve` 中的操作失败不会终止服务；EOF 正常退出。
这是一种项目自定义 JSONL 协议，不是 MCP/JSON-RPC 兼容声明。

`tcad batch --file requests.json` 接受最多 1000 项的请求数组，按顺序返回结果。
批次中的单个 apply 是事务；整个批次不是事务，后续请求仍会执行。
输入大小限制 8 MiB；JSON 对象禁止重复键、NaN/Infinity，嵌套上限 64 层。
实体查询最多每页 10000 项；编辑计划最多 10000 个操作。

## 当前边界

- 编辑输出为 DXF；DWG 只提供读取与 DWG→DXF 转换。ODA 目前只检测，不执行转换。
- 精确测量包括直线、圆、圆弧、2D 多段线（含 bulge 弧段）以及 3D 多段线长度。
  闭合多段线面积是代数面积的绝对值，不能当作自交或多个实体的布尔并集面积。
- 文本等不支持测量的实体明确返回 `supported:false`；不把未知几何计为完整测量结果。
- 边界框是近似范围，尤其依赖字体。过滤使用 WCS XY 包围框相交；不支持求交精筛。
- 编辑位于 Model 空间；读取和预览可以指定命名布局。
- 不提供通用 3D 实体建模、几何约束求解、自动标注或真实 AutoCAD 兼容性的保证。
  房间建模限定于显式映射的轴对齐矩形房间和长方体构件网格。
- DWG 后端可能遗漏复杂对象；SVG 为 2D 投影，字体会替代，外部参照不会自动解析，
  链接光栅以矩形表示。关键工程数据应检查转换诊断和输出图纸。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check --select E4,E7,E9,F src tests
```

复现真实 DWG 测试前，运行 `.\scripts\install-libredwg.ps1 -IncludeTestFixture`。
uv 安装路径使用 `uv.lock` 固定依赖；pip 回退路径依据 pyproject.toml 的版本范围解析。

测试覆盖实际 DXF 往返、已知解析几何、失败不写入、覆盖保护、SVG XML、CLI 子进程、
错误 JSONL 的恢复及 DWG 真实转换。DWG 集成测试在外部转换器缺失时跳过，
不能把该跳过算作 DWG 已验证。

## 作者与许可证

作者：**LiTao**。Copyright 2026 LiTao。

原创代码、文档和受控房间示例采用 [Apache License 2.0](LICENSE)，允许商业使用。
分发时请保留适用的许可证与 [NOTICE](NOTICE)。第三方依赖继续适用各自条款，
详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
