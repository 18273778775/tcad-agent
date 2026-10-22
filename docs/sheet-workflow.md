# 图框分图、分层预览与完整性核验

TCAD 0.3.0 支持 `sheets`、`split` 和按图幅重绘。先运行
`tcad capabilities --detail full --fields sheet_workflow` 与 `tcad schema request`，
确认安装版本和可接受参数。以下图层、图名、handle 和坐标均为示例。

## 一次生成整套成果

```powershell
.\tcad.ps1 split 'project.dwg' --output-dir 'artifacts/project-sheets'
```

输出目录的父目录必须存在，目标目录必须是新目录。默认生成每张图的独立 DXF、
自包含字形路径 SVG、最长边 1280 像素的 PNG 缩略图、完整 `manifest.json` 和
离线 `index.html`。HTML 使用缩略图浏览，放大时读取 SVG；查看、搜索、缩放和下载
成果不需要再次调用 CLI。请保留 HTML 和相邻成果文件。

DWG 使用外部转换器。分图准备阶段读取一次源 DXF，复用图框、文本和实体范围；
发布前另行回读全部导出的 DXF，并解析 SVG、核验 PNG。整批成功才原子发布目录，
任一导出、渲染、缩略图或核验失败均返回结构化错误。原图不变。

`split` 默认监督时限为 1800 秒，默认内存上限为 1024 MiB；其他普通命令默认
60 秒。使用 `--timeout-seconds`、`--max-memory-mb` 调整预算。严格回读需要实际
I/O，不能将一次成功或提高预算理解为任意复杂图纸的速度保证。

```powershell
.\tcad.ps1 split 'project.dxf' --output-dir 'artifacts/dxf-only' --no-preview
.\tcad.ps1 split 'project.dxf' --output-dir 'artifacts/vector-only' --no-thumbnail
.\tcad.ps1 split 'project.dxf' --output-dir 'artifacts/small-thumbs' --thumbnail-width 640
```

`--no-preview` 同时跳过 SVG 和 PNG；`--no-thumbnail` 保留 SVG。缩略图最长边参数
范围为 16–4096 像素，单张 PNG 上限为 4 MiB。缩略图使用 `resvg_py` 纯代码渲染，
无需浏览器截图、Node 或交互式 CAD。毫米和百分比 SVG 根尺寸会转换成确定的像素
尺寸，源 SVG 的 viewBox 和文件内容不变。

## 先发现图框，再调整命名

```powershell
.\tcad.ps1 sheets 'project.dxf' --output 'artifacts/frames.json'
.\tcad.ps1 sheets 'project.dxf' --frame-layer 'FRAMES' --frame-block-regex '^FRAME_' --frame-size 841x594 --title-layer 'TITLE' --number-layer 'SHEET_NO' --output 'artifacts/configured-frames.json'
```

几何兜底识别闭合矩形多段线、四边直线组合和块内经变换后的矩形，并剔除内框与
表格单元。坐标为源图 WCS。`--frame-layer` 限定候选几何图层；
`--frame-block-regex` 给匹配块名增加证据，`--frame-size` 只比较宽高比，允许约 3%
偏差及横竖方向互换。后两项是提示，不会仅因块名不匹配而丢弃有效几何，也不会
推断比例或单位。JSON 请求的 `frame_size` 还可传两个正数的数组。

普通斜放图框、缺边框、多边形框和未解析外部参照中的图框不属于自动识别保证
范围。经变换后仍轴对齐的 90 度旋转可识别；没有任意角旋转或纸空间视口还原能力。
`--layout` 选择指定布局，不等同于将其所有视口中的模型内容重建成纸张。

标题依据框内文字、图名图层、属性标签、位置和文字高度。`title_evidence` 保存
原生 handle、父 INSERT 和位置。`primary_title` 为选定主标题，`subtitles` 保存其他
主要标题；复合 `title` 保留这些标题的确定顺序。文件名使用主标题，重复时增加
唯一后缀，非法路径字符会替换。无可靠标题时使用稳定序号并报告。

`building`、`elevation_range`、`kind` 带 `metadata_evidence` 和
`metadata_confidence`。这些字段来自文字规则，未确定或有冲突时保持 null/unknown，
不会把楼层名称换算成标高。置信标签表示规则证据，不是经过统计校准的概率。

可用 `--title-regex` 限定标题，命名捕获组支持 `title`、`number`、`building`、
`elevation_range`、`kind`。未指定的字段沿用检测结果，原始文字证据仍保留：

```powershell
.\tcad.ps1 sheets 'project.dxf' --title-layer 'TITLE' --title-regex '(?P<building>示例楼) (?P<title>.*施工图)' --output 'artifacts/named-frames.json'
```

只改某张图时使用 `--names names.json`，无需重新列出全部图框：

```json
{
  "sheet-001": {"name": "基础平面图_复核", "title": "基础平面图"},
  "ABC": {"number": "S-02"}
}
```

键可为发现清单中的 sheet ID 或原生图框 handle。字符串值同时覆盖名称与标题；
对象值支持 `name`、`title`、`number`，只修改指定项。找不到键或同一张图收到冲突
覆盖会报错。覆盖名称不修改源图，也不能消除原图标题矛盾。

```powershell
.\tcad.ps1 split 'project.dxf' --names 'names.json' --output-dir 'artifacts/renamed-sheets'
```

`--frames frames.json` 接受原生矩形图框 handle 或显式 bbox/name/number 对象数组。
显式 bbox 绕过自动内框剔除，可用于裁取约定区域：

```json
[
  {"bbox": [0, 0, 1000, 700], "name": "基础平面图", "number": "S-01"},
  {"bbox": [1200, 0, 2200, 700], "name": "柱配筋图", "number": "S-02"}
]
```

`--shared-frame largest` 为默认，保留最大外框。`--shared-frame split` 只有在实际
检测到至少两个有标题、互不重叠、总面积覆盖外框至少 85% 的闭合子框时才拆带。
只有多个图名而没有明确子框，或子框交叠时，保留最大框并报告
`SHARED_FRAME_AMBIGUOUS`，不会凭文字位置制造分割边界。

## 柱图只保留轴网、轴号、柱及柱号

先通过 `info`、`query` 识别实际图层，再按图幅和多图层白名单重绘：

```powershell
.\tcad.ps1 query 'project.dxf' --sheet sheet-001 --manifest 'artifacts/project-sheets/manifest.json' --type INSERT --limit 20
.\tcad.ps1 render 'project.dxf' --sheet sheet-001 --manifest 'artifacts/project-sheets/manifest.json' --layers AXIS AXIS_LABEL COLUMN COLUMN_LABEL --foreground monochrome --output 'artifacts/columns.svg'
.\tcad.ps1 render 'project.dxf' --sheet sheet-001 --manifest 'artifacts/project-sheets/manifest.json' --layers AXIS AXIS_LABEL COLUMN COLUMN_LABEL --width 1600 --output 'artifacts/columns.png'
```

图层名称必须与源图对应。`--layers` 可包含多个图层，`--exclude-layers` 排除指定
图层，排除优先。渲染按块的叶实体图层过滤，并处理嵌套块的 layer 0 继承；只需
某类构件时不会因外层 INSERT 混有其他图层而保留整个混合块。文字、标注的实际
绘制图层及缺失诊断请核对 `render_stats`、`diagnostics`，不能仅凭文件生成成功
推断柱号和轴号全部可读。原始 DXF 不会因预览过滤而改动。

`--sheet` 可用唯一 ID、图名或名称；重复标题应改用 ID。`--manifest` 校验原始
来源/规范化 DXF SHA，不一致返回 `STALE_MANIFEST`。已有 manifest 时优先复用；
未提供时会重新发现图框。`render` 的 `--sheet` 与 `--bbox` 互斥。

```powershell
.\tcad.ps1 render 'project.dxf' --sheet all --manifest 'artifacts/project-sheets/manifest.json' --layers AXIS AXIS_LABEL COLUMN COLUMN_LABEL --output-dir 'artifacts/column-previews'
```

`--sheet all` 使用新输出目录，一次读取源文档，为每个图幅生成 SVG/PNG 和
`render-manifest.json`，默认 PNG 最长边 1600。此命令重绘已有图幅，不重新导出
DXF。默认整批预览上限为 512 MiB。

分图时也能直接透传 SVG 外观和图层参数：

```powershell
.\tcad.ps1 split 'project.dxf' --layers AXIS AXIS_LABEL COLUMN COLUMN_LABEL --exclude-layers NOTES --foreground monochrome --background white --hatch-mode adaptive --lineweight-scale 0.7 --output-dir 'artifacts/selected-previews'
```

`split` 的过滤仅影响预览；每张独立 DXF 仍保留该图框选中的完整原生实体。
JSON 请求使用 `render_options` 对象，支持 `layers`、`exclude_layers`、`foreground`、
`background`、`hatch_mode`、`lineweight_scale`、`font_family`。

单图 SVG/PNG 还支持 `--annotate labels|areas|axes`、`--legend`、
`--max-annotations`。叠加层用于查看 handle/文字标签、有效轮廓的逐实体面积或
轴网，不修改 CAD。面积气泡不是去重并集结果；轴网规则和歧义应以 `axes` 结果为准。
叠加有数量上限，检查返回记录是否完整。没有 PDF 输出。

## 显示、保留与核验的边界

SVG 按图框裁切适配，不能用于量尺，也不是 1:1 出图。默认白底，支持黑底和
layout 背景；`--foreground monochrome` 使用单色。
`--font-family` 可指定已安装字体。文字转换成路径后不要求浏览器安装相同字体，
但替代字体字形/字宽、缺失 SHX/bigfont 和专用钢筋符号仍可能不同。

默认 `--hatch-mode adaptive` 会将低于预览分辨率的密集图案转成完整边界实填，
记录 `HATCH_SIMPLIFIED`；`accurate` 保留原始图案，复杂度超限即失败。局部 bbox
放大可保留更多细节，两种模式均不修改 DXF 填充数据。

DXF 保留与图框相交的整个原生实体及引用资源；跨框实体仅在 SVG 中裁切。
因此独立 DXF 的全部几何范围可能超过纸张范围，导出后 handle 也可能改变。
图层默认值、文字/标注资源、完整扩展字体参数、`$LTSCALE`/`$PSLTSCALE` 等显示
变量和不同 IMAGEDEF 身份会保留。缺失的链接图片仍使用占位提示，不自动下载。

`manifest.verification` 来自实际检查，而不是空的展示字段：

- 回读每张 DXF，比较所选实体数量、类型和几何，递归核对 INSERT/DIMENSION 引用
  块、资源参数、图片定义及 reactor、约定的显示 header。
- 解析每张 SVG，检查有效 viewBox、非空路径、外链和文件 SHA；生成 PNG 时核验
  PNG 格式、像素尺寸、字节数和 SHA。
- 检查源 SHA、所选实体与未归属实体的覆盖。未归属实体保存为
  `__tcad_unassigned__.dxf` 与 `unassigned.json`，不计为额外图纸。

标题证据在框内、存在主标题，不代表独立证明图名语义正确。多视图标题可保留为
主标题加子标题；标题顺序也不代表施工顺序。SVG XML 和路径有效不等于原始 DWG
逐像素一致。材质/应用对象图、关联性、自动生成内部标识及扩展元数据可能变化。
DWG 完整性以转换后 DXF 为参考，转换警告必须保留。

最大 200 张图，默认每张最多 100,000 个原生实体、SVG 最多 64 MiB、整套目录最多
512 MiB。`--max-sheets`、`--max-entities`、`--max-svg-bytes`、`--max-total-bytes`
可调整范围内上限。PNG、manifest 和 HTML 同样计入目录预算。未知或部分实体范围、
未解析外部参照、代理对象及部分三维内容必须核对清单；预算或核验失败不会静默
裁掉内容后宣称完成。分图与可读预览不构成结构构件自动识别或工程量审定。
