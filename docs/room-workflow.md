# 完整房间 CAD→数据→三维模型验收

日期：2026-10-21。输入是用户同意采用的**受控完整房间验收样例**，不是外部施工项目。
样例有平面、南北立面、地面/天花参照、门窗、墙厚和尺寸标注，共 71 个实体。

## 实测结果

| 指标 | 原始 DWG/DXF 的 CLI 几何值 | 事务编辑后的 DXF 几何值 |
| --- | ---: | ---: |
| 室内净长 X | 6000 mm | 6400 mm |
| 室内净宽 Y | 4500 mm | 4800 mm |
| 地面完成面至天花底净高 Z | 3000 mm | 3200 mm |
| 净面积 | 27 m² | 30.72 m² |
| 净轮廓周长 | 21000 mm | 22400 mm |
| 墙厚 / 地板厚 / 天花厚 | 200 / 150 / 150 mm | 200 / 150 / 150 mm |
| 南侧门洞 | 偏移 1000，宽 900，高 2100，门槛 0 mm | 同左 |
| 北侧窗洞 | 偏移 2200，宽 1600，高 1200，窗台 900 mm | 同左 |

原始 DWG 与 DXF 提取的尺寸、构造和开口数据一致。修改前后分别输出 GLB、OBJ、
三维 DXF，六个模型均从当前源图重新提取规格并重新加载模型核验，
`source_verified` 和 `model_verified` 均为 true。GLB 的最大净尺寸偏差为 **0.000190735 mm**；
OBJ 和 DXF 的最大偏差约 9.09e-13 mm。本次容差为 **0.01 mm**。

初次完整验收保存于 `artifacts/room-validation/acceptance-001/`，共 55 次公开 CLI 调用。
修正 GLB 轴向、增加严格源图核验后的交付目录为 `artifacts/room-validation/acceptance-004/`。
上述目录是开发机器上的历史记录，不纳入 Git 仓库。已归一化本机路径的公开副本、
模型和交互页见 [docs/demo/](demo/README.md)；原始 CAD 和模型文件字节及其哈希保留。
`commands.json` 保存命令、退出码及响应路径；`responses/` 保存每次 stdout JSON 和 stderr。
`summary.json` 保存净尺寸、面积、源哈希、验证结果及负例检查结果。
本轮全量测试为 204 passed、1 项可选隔离安装测试跳过、9 subtests passed，Ruff 检查通过。
本次离线 `viewer.html` 从六个新模型重新生成；历史浏览器检查与截图属于较早验收，
不作为本轮浏览器交互复测的替代证据。

## 证据如何连接

1. 图纸构造工厂只生成 CAD 输入和独立预期值；提取器、模型构建器不导入该工厂。
2. `query` 返回原生端点/顶点；`measure` 检查净轮廓面积、周长、净高、厚度及开口宽度。
3. `room-extract` 使用明确的图层/handle 映射。长宽来自净轮廓，高度来自立面中的
   `ROOM_HEIGHT` 参照线，并与天花/地板参照及尺寸文字交叉检查。
4. 映射关联的门窗平面线与立面轮廓必须等宽；窗台相对完成面计算。
5. 模型只接受提取后的规格 JSON，保留来源哈希和实体证据。内部建筑坐标和 OBJ 为 Z-up；
   GLB 采用版本标记 `tcad-room-2.0` 的标准 glTF 右手 Y-up：建筑 `(X,Y,Z)` 映射为
   `(X,Z,-Y)`。GLB、OBJ 坐标为米，三维 DXF 坐标保留图纸单位。未标记的旧 GLB 明确拒绝，须重新生成。
6. 严格核验先检查当前源图 SHA，再用相同映射重新提取尺寸、构造及开口数据。
   校验器重新打开模型文件，应用 GLB 节点变换并还原建筑轴向，从实际三角面恢复构件边界。
   净长是东西墙内表面的距离，净宽是南北墙内表面的距离，净高是地板上表面
   到天花下表面的距离。模型整体包围盒包含墙厚和楼板厚，不用于冒充净尺寸。
7. 实际墙面区间的占据/空缺用于核对门窗宽、高、偏移和窗台；堵塞门洞、篡改尺寸、
   删除面或错误窗台均应验证失败。每个构件检查封闭性、外法线及正体积，
   是独立长方体，不声明整体布尔融合。

## 复现与直接使用

```powershell
.\scripts\bootstrap.ps1 -WithDwg
.\.venv\Scripts\python.exe scripts\validate_room_workflow.py --output artifacts\room-validation\new-run
.\.venv\Scripts\python.exe scripts\build_room_viewer.py --run-dir artifacts\room-validation\new-run
```

使用新的空输出目录。验收脚本会原生生成测试 DWG；这是受限验收夹具生成器，
不表示 CLI 已支持任意 DXF→DWG 写回。工具来自固定哈希的官方 LibreDWG 0.14 包。

单独对房间运行：

```powershell
.\tcad.ps1 room-extract examples\room\controlled-complete-room.dxf --mapping examples\room\room-map.json --output artifacts\room.json
.\tcad.ps1 room-model --spec artifacts\room.json --output artifacts\room.glb
.\tcad.ps1 room-verify --spec artifacts\room.json --model artifacts\room.glb --source examples\room\controlled-complete-room.dxf --mapping examples\room\room-map.json --tolerance-mm 0.01
```

`schema room-map` 和 `schema room-spec` 给出准确格式，JSONL 和 batch 同样支持三个命令。
规格 JSON 输出为普通对象，可直接交给 `room-model --spec`。
省略 `--source` 和 `--mapping` 只能验证模型与规格的一致性；此时 `source_verified=false`，
不能据此判断模型与当前图纸一致。

## 失败行为与外推范围

验收实际执行了：晚序操作引用无效 handle 时回滚且无输出；dry-run 不写入；
已有输出禁止覆盖；删除高度证据后返回 `ROOM_EVIDENCE_MISSING`；把高度文字改成
9000 mm 后返回 `ROOM_DIMENSION_MISMATCH`。原始 DWG/DXF 的 SHA-256 在结束时保持不变。

本次证明了显式映射、轴对齐矩形房间的完整链路可以使用。它不能证明任意施工图自动识别，
也不能替代建筑尺寸审查。旋转/异形房间、含斜墙或复杂剖面、图块内未展开对象、
未指定的单位或无高度证据须补充映射/处理能力或明确拒绝，不能填入常用层高。
平面、立面属于同一房间的关联来自显式映射，不是算法自动推断的事实。
本次长宽高事务修改采用整张受控房间图的重建，不作为外部项目无关实体保留能力的证据。
另有局部映射平移回归，检查无关 INSERT/ATTRIB、XDATA、DIMENSION 匿名块和 SPLINE
标签及 handle 保留；该结果不能外推为任意房间尺寸修改。当前没有通用墙体、门窗、
立面与尺寸标注依赖求解器，不能声称任意真实建筑图的完整 resize 已解决。
SVG 采用最长边 420 mm 的预览页，以保持建筑尺度图纸的线条可见；不能据其物理尺寸作 1:1 打印。
