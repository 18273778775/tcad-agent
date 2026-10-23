# 给 agent 的图纸基础能力

CLI 负责可靠地选择图纸、返回数据及其来源、计算支持的几何和生成可检查的预览。agent 决定取哪些证据、如何解释图纸，以及调用什么工具完成计算表或模型。

## 可以直接完成什么

| 任务 | CLI 提供的基础能力 |
| --- | --- |
| 了解图纸 | 单位元数据、布局、图层、实体类型、数量、可计算范围、转换诊断 |
| 找到相关图 | 图框、图名与文字证据；按楼号、图纸类型、标题筛选或显式选页 |
| 建立任务范围 | 单页、单个 bbox、多页及多个不相邻 bbox 的工作集；筛选、去重和来源校验 |
| 提取建模数据 | 支持实体的原生 XY/XYZ 坐标、长度、半径、顶点、块实例变换、尺寸定义和文字证据 |
| 测量与面积 | 指定实体的完整长度/面积、轮廓并集与重叠量，明确单位和近似方式 |
| 关联标注 | 包含、距离、LEADER / MLEADER 与显式折线引线的几何关系、候选及歧义 |
| 查询轴网 | 水平、竖直和旋转直轴，轴号证据、有限线段交点、保存后的点/范围查询 |
| 查看图纸 | SVG / PNG、按层/按范围显示、本地图片嵌入、字体映射、质量报告和严格模式 |
| 修改图纸 | 声明式 JSON 计划、预检查、版本条件、原子输出 DXF |
| 控制上下文 | 字段投影、分页、裁剪报告、完整结果文件和 JSONL 导出 |

二维平面不会自动提供建筑高度。三维建模需要从立面、剖面、标高、实体 Z 坐标或明确标注中取得高度证据，并核对它对应哪个构件。`DIMENSION.measurement`、缓存值和覆盖文字是不同证据；包围框也不能替代真实尺寸。单位未确定时应保持未确定。

## 先选相关图，再反复调用

```sh
tcad capabilities
tcad schema request
tcad sheets building.dwg --output sheets.json
tcad workset building.dwg --manifest sheets.json --building '1#楼' --title-regex '基础|承台' --output-dir foundation
tcad query --workset foundation --type DIMENSION --fields handle,dimension_type,measurement,measure_kind,text_override --limit 20
tcad render --workset foundation --sheet all --output-dir foundation-previews
```

楼号是清单中的明确值，标题正则是 agent 提供的筛选条件。`kind` 当前表示 `plan/detail/schedule/notes/unknown`，不把“基础、柱、梁、板”等业务类别凭空补出来。没有匹配项会报错；可查看图名证据后明确指定 ID。

```sh
tcad workset building.dxf --manifest sheets.json --sheet sheet-041 --sheet sheet-042 --output-dir related
tcad workset building.dxf --bbox 0 0 1000 700 --bbox 2000 0 3000 700 --output-dir regions
tcad query --workset related --sheet sheet-042 --type LINE --fields handle,start,end --limit 10
tcad measure --workset related --sheet sheet-042 --handle <该子图查询返回的handle>
tcad render --workset related --sheet sheet-041 --bbox 100 200 900 700 --output local.svg
```

上述坐标和图号是格式示例。多范围按并集选择后去重，类型、图层和 handle 筛选再取交集；单页和 bbox 组合时取范围交集。**bbox 选择完整实体，不裁断实体。** 一条线跨出范围时，`measure` 返回它完整的长度。

`workset.json` 保存来源、选页、范围、未知边界、交叉实体和原图到子图的 handle 映射。后续命令使用子图自己的 handle。`--sheet all` 为这些选页一次生成 SVG、PNG、清单和 HTML，浏览时不再调用 CAD 命令。仅由 bbox 建立的工作集使用 `region-001` 等 ID。

每次复用会核对原图与子图 SHA-256；原图变更、缺失或子图被修改会返回 `STALE_WORKSET`，需要重新准备。只做文件摘要校验，不再次解析原整图。原图保持全图调用方式；工作集中的图保持原 WCS、单位和标高，不重新排布。

## SVG 内容与质量

```sh
tcad render --workset related --sheet sheet-041 --layers AXIS AXIS_LABEL COLUMN COLUMN_LABEL --resource-path ./images --font-path ./fonts --output columns.svg
tcad render drawing.dxf --font-map font-map.json --output mapped.svg
tcad render drawing.dxf --bbox 0 0 1000 700 --strict --output checked.svg
```

`font-map.json` 是 CAD 样式名或原字体文件名到本地字体文件的对象，例如 `{"ENGINEERING":"C:/fonts/engineering.ttf"}`。字体目录只扫描直接文件，最多 256 个，不递归扫描磁盘。资源和字体输入默认共用 8 MiB 预算，必要时用 `--max-resource-bytes` 明确提高。

图片只从源文件目录或显式资源目录读取，解码后作为 PNG 嵌入 SVG。不会自动访问 URL、网络共享或图纸指定的任意系统目录。遗失的绝对图片链接可以从允许目录按文件名找回。PNG、分图缩略图与 HTML 使用同一份自包含 SVG。

检查 `preview_quality.content_complete` 和 `fidelity_complete`。前者表示已报告的选中内容有无缺失，后者还检查字体替代、bigfont、填充简化等外观变化；都不是与原 CAD 的像素等价证明。`--strict` 要求两者均通过，否则返回 `PREVIEW_INCOMPLETE`，不发布或替换输出。需要补资源时，从诊断定位问题，不必盲目反复重绘。

## 关联与轴网的使用范围

```sh
tcad associate --workset related --label-layers LABEL --target-layers COLUMN --method leader --leader-layers LEADERS --ambiguity-tolerance 0.5 --output relations.json
tcad axes --workset related --line-layers AXIS --label-layers AXIS_LABEL --output axes.json
tcad axes --workset related --axes axes.json --point 200 300
```

原生引线保留文字引用或端点证据；普通多段线只有明确指定 `--leader-layers` 才参与，端点方向属于几何假设。关联返回竞争候选、距离及差值，容差内的竞争关系保留为歧义。轴网支持旋转直线与共线开多段线，曲轴和弯折路径显式报告不支持。工程语义由 agent 结合图名、图层、文字、SVG 等证据解释。

Excel 算量和房间模型是 agent 使用这些基础能力的验收案例，不把没有通用 Excel 或 BIM 命令当作本工具的功能缺项。案例中的图纸矛盾、单位和高度证据仍须保留，不能为了业务结果而略去。
