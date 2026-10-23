# TCAD Agent CLI

让 agent 用命令行处理 DWG / DXF：查实体、量尺寸、改图、按图框拆分，再生成可直接查看的 SVG。无需启动 CAD 界面；命令返回 JSON，预览、模型和批量数据可写入文件。

作者 **LiTao** · [Apache-2.0](LICENSE) · [当前版本 v0.4.0](https://github.com/18273778775/tcad-agent/releases/tag/v0.4.0)

## 安装

已有 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和 Git，运行一行命令：

```sh
uv tool install --python 3.12 git+https://github.com/18273778775/tcad-agent.git@v0.4.0
```

Windows 也可用 PowerShell 安装，需要已有 CPython 3.11–3.14：

```powershell
irm https://raw.githubusercontent.com/18273778775/tcad-agent/v0.4.0/scripts/install.ps1 | iex
```

安装后先运行 `tcad capabilities`、`tcad schema request`。uv 用户若找不到命令，执行 `uv tool update-shell` 后重开终端。升级、卸载、离线 wheel 和其他安装方式见[安装文档](docs/installation.md)。目前从 GitHub 安装，尚未发布到 PyPI。

读取 DWG 还需要转换器。Windows x64 可运行 `tcad setup-dwg`，随后用 `tcad doctor` 检查。该命令安装固定的官方 LibreDWG 0.14；下列工程案例使用另行配置的 0.14.8612，默认后端未能读通其中的承台图。DXF 无需转换器。

## 实际案例

### 从基础平面图取数，列出 CT2–CT5 承台体积

CLI 用于查找承台名称、实例和尺寸证据，再由案例计算脚本生成 Excel。四类承台数量分别为 **13、11、3、1**；图中列出单个计算式和合计，便于逐项核对。

![工作簿数据摘录：CT2至CT5承台的数量、单个体积计算式和合计](docs/assets/takeoff-case.png)

按本次选定口径，合计为 **50.8158099 m³**。CT2 平面与详图长度矛盾，本次按用户指定的平面尺寸 1800 × 800 × 900 mm 计算；CT3 含待复核的截角构型假设，因此合计是条件计算结果。这个案例展示 agent 如何把 CLI 的标签、实例和尺寸证据交给计算脚本，形成 Excel。[查看计算口径与核验范围](docs/cases/takeoff.md)。

### 一张结构总图，拆成 52 张有名字的图纸

这份实图包含 **52 个图框、30,433 个原生实体**。一次 `split` 生成 52 份独立 DXF、52 张 SVG、52 张 PNG，以及可搜索的离线 HTML 图集；文件名取自框内图名。多标题页面保留主标题和子标题。

![实际分图成果：图框位置、读取到的图名及对应的DXF、SVG和PNG文件](docs/assets/split-case.png)

拆完后，可以只打开其中一张 DXF，按图层保留轴网、轴号、柱和柱标注。下图来自该单页的柱图局部，保留了 **10 个柱轮廓、10 个柱标注和 20 个轴号**。

![已拆分单页的分层SVG预览：保留轴网、轴号、柱和柱标注](docs/assets/split-layer-preview.png)

52 张导出文件均做了回读检查；另有 25 个未归属实体保存在余料文件中。源图的 104 处外链图片缺失，20 张图有字体警告，生成预览不代表这些内容已恢复。[查看分图结果与显示边界](docs/cases/sheet-split.md)。

### 房间图纸与三维模型核对长、宽、高

受控房间样例把平面和立面中的尺寸显式映射到模型。净尺寸由 6000 × 4500 × 3000 mm 改为 **6400 × 4800 × 3200 mm** 后，重新读取源图和模型进行比较，最大净尺寸误差为 **0.000191 mm**，验收容差为 0.01 mm。

![房间CAD尺寸与三维模型对照](docs/assets/room-validation.jpg)

这项验证适用于显式映射的轴对齐矩形房间。公开样图、六个模型和交互查看页见 [房间演示](docs/demo/README.md)，操作方法见[房间工作流](docs/room-workflow.md)。工程案例仅公开脱敏预览，原始施工图不随仓库分发。

## 开始处理一张图

```sh
tcad info drawing.dxf
tcad query drawing.dxf --type CIRCLE --layer HOLES --fields handle,type,layer --limit 20
tcad render drawing.dxf --output preview.svg
tcad split drawing.dwg --output-dir sheets
```

图层名需要换成当前图纸中的名称。`split` 的目标目录必须是新目录，父目录须已存在；默认同时生成预览和 HTML 图集。

先拆图，再对单张图做分层预览：

```sh
tcad info "sheets/柱配筋图.dxf"
tcad render "sheets/柱配筋图.dxf" --layers AXIS AXIS_LABEL COLUMN COLUMN_LABEL --foreground monochrome --output columns.svg
```

这里的文件名和图层名是用法示例。保留分图目录中的 `manifest.json`，CLI 会核验该单页的 SHA-256 并沿用原图框。需要进一步框选时可加 `--bbox`。完整参数见[分图与预览工作流](docs/sheet-workflow.md)。

需要反复分析相关图纸时，先准备工作集，后续不再解析原整图：

```sh
tcad workset drawing.dxf --manifest sheets/manifest.json --building '1#楼' --title-regex '基础|承台' --output-dir foundation
tcad query --workset foundation --type LINE --fields handle,type,layer --limit 10
tcad render --workset foundation --sheet all --output-dir foundation-previews
```

楼号和图名须以当前清单为准，也可重复传 `--sheet` 或 `--bbox` 明确选择多个范围。工作集中的查询、测量、面积、关联、轴网和预览共用同一批实体；多张图保持原坐标，不移动或叠放。操作细节和完整能力清单见[图纸基础能力](docs/agent-basis.md)。

## 已有功能

| 需要做什么 | 命令 | 当前能提供的结果 |
| --- | --- | --- |
| 了解工具与请求格式 | `capabilities` / `schema` / `doctor` | 能力、限制、JSON Schema、转换器状态 |
| 读取图纸 | `info` / `convert` | 单位、布局、图层、实体数量；DWG 转 DXF |
| 找实体和文字 | `index` / `query` / `export` | 图层、类型、文字、范围查询，分页与 JSONL 文件导出 |
| 复用相关图纸范围 | `workset` | 将多个图幅或范围保存为有来源校验的子图，供后续命令复用 |
| 测量与面积计算 | `measure` / `area` | 支持实体的长度、面积；轮廓面积并集与重叠量 |
| 拆图与命名 | `sheets` / `split` | 图框、标题证据、独立 DXF、预览和 HTML 图集 |
| 查看整图或局部 | `render` | SVG / PNG，按图幅、范围和有效图层筛选 |
| 关联标注与轴网 | `associate` / `axes` | 包含、距离、引线关联；水平、竖直和旋转直轴及交点 |
| 修改图纸 | `apply` | 声明式 JSON 编辑，先验证再原子发布 DXF |
| 连续调用 | `serve` / `batch` | JSONL 请求响应，按顺序执行的批量请求 |

建模和 Excel 是 agent 组合这些基础能力完成任务的案例。仓库保留 `room-*` 受控验收适配器，用于演示源图尺寸与模型的核对；CLI 的职责是提供可追溯的数据和图形证据。

## 给 agent 的调用约定

正常操作的 stdout 只含 JSON。判断 `ok` 和 `error.code`；默认响应上限为 **32 KiB**，先选 `--fields`、`--no-provenance` 和合理的 `--limit`，避免把整张图灌进上下文。

分页沿用 `next_cursor`，同时保持筛选和字段不变。看到 `output.truncated`、`truncation` 或 `geometry_page` 时，不能把当前片段当成完整数据。大量数据用 `export --output selected.jsonl` 写入文件；`--result-file` 只保存本次结果或当前页。

测量前确认单位；近似包围框不能当尺寸证据。DWG 转换或导出后重新查询 handle。编辑先用 `--dry-run`，默认保留原图并拒绝覆盖；DWG 编辑输出为 DXF。单个编辑计划是事务，整个 `batch` 不是事务。

详细协议、输出预算和资源上限见 [Agent 调用指南](docs/agent-usage.md)。

## 当前还做得不够好的地方

- **DWG 兼容性依赖外部后端。** 复杂对象可能在转换时丢失，默认转换器也未能覆盖所有已测实图；当前不能写回 DWG。
- **整图读取仍有成本。** 工作集准备后，分析只解码子图；直接调用原整图仍需解析。32 KiB 输出上限约束返回体积，不保证只计算少量数据。
- **图框与图名识别仍靠规则。** 斜放、缺边图框和纸空间视口还原不在自动识别保证内，多标题仍需复核。
- **SVG 外观仍取决于可用资源。** 已支持本地图片嵌入、字体目录和映射，以及选区质量报告和严格模式；缺失的原始字体、bigfont 组合和 xref 仍需核对，密集填充可能简化。
- **几何计算还不是构件算量。** `measure` 尚不能直接测量整个 INSERT 块；部分曲线面积需要离散化，未提供可认证的面积误差上界。标注关联和正交轴网也不能替代结构语义识别。
- **关联结果需要 agent 解释。** 已支持 MLEADER、指定图层折线引线和旋转直轴，保留竞争候选；弧轴尚未支持，几何关系不自动解释为工程语义。

当前已改进的部分与[剩余边界](docs/current-limitations.md)分开记录，房间和 Excel 不再列为 CLI 的功能缺项。

## 文档与开发

[安装](docs/installation.md) · [Agent 调用](docs/agent-usage.md) · [分图与 SVG](docs/sheet-workflow.md) · [房间建模](docs/room-workflow.md) · [性能实测](docs/large-drawing-performance.md) · [验证说明](docs/validation.md) · [更新记录](CHANGELOG.md)

开发环境运行 `scripts/bootstrap.ps1`，测试使用 `.venv\Scripts\python.exe -m pytest -q`。0.4.0 本地完整回归为 454 项通过、1 项可选测试跳过、9 subtests 通过。验证记录见 [CI](https://github.com/18273778775/tcad-agent/actions/workflows/ci.yml) 和 [0.4.0 更新](docs/releases/0.4.0.md)。查询基准与实图工作集的端到端对比单独记录在性能文档中。

项目独立实现，不依赖原看图软件的 EXE / DLL。分析依据见[静态逆向报告](docs/reverse-engineering.md)和[开源项目选型](docs/upstream-projects.md)。

Copyright 2026 **LiTao**。原创代码、文档和受控房间示例采用 [Apache License 2.0](LICENSE)，允许商业使用。分发时保留适用的许可证与 [NOTICE](NOTICE)；第三方依赖的条款见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
