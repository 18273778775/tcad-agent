# 当前 CLI 的不足

审计范围为 v0.3.1。下列条目包括已出现的兼容性故障、实测性能成本和当前能力边界。

1. **连续分析大图仍会重复解析。** `measure`、`area`、`render`、`associate`、`axes` 独立调用仍要读取整份 DXF。持久索引加速查询，不缓存可编辑的几何模型。实图冷索引实测约 40.29 秒、356 MiB，不能把热查询的毫秒级耗时当作完整 CLI 耗时。依据：[测量入口](../src/agentcad/engine.py#L739)、[面积入口](../src/agentcad/area.py#L486)、[渲染入口](../src/agentcad/render.py#L485)、[性能实测](large-drawing-performance.md)。

2. **返回少，不等于计算少。** 新几何投影会先为全部匹配实体准备缓存记录，再返回第一页；其他命令的字段筛选、输出裁剪通常发生在结果形成之后。`--limit 10` 或 32 KiB stdout 不代表只处理 10 个实体。已有超时、内存和输出预算，仍需进一步减少前置计算。依据：[查询准备](../src/agentcad/query_cache.py#L315)、[结果裁剪](../src/agentcad/output.py#L156)。

3. **默认 DWG 转换器仍有实图兼容性故障。** 固定的 LibreDWG 0.14 在承台案例中失败，改用显式配置的 0.14.8612 后才跑通。替代版本的成功不等于默认兼容性已解决，也不能证明所有 DWG 对象无损。当前编辑输出仍为 DXF。依据：[默认版本](../src/agentcad/install.py#L21)、[承台案例](cases/takeoff.md)。

4. **分图和图名识别依赖排版规则。** 自动图框发现针对轴对齐矩形，标题依赖位置、字高、文字规则和显式配置。斜放、缺边图框、纸空间视口还原、多标题页面仍需配置或复核；52 张成功样例不能代表任意项目。依据：[图框与标题规则](../src/agentcad/sheets.py#L55)、[分图文档](sheet-workflow.md)。

5. **分出的 DXF 尚未真正裁断跨框几何。** 与图框相交的原生实体完整保留，SVG 才按框裁切。框外实体保存为余料；只拷走单页 DXF、丢失相邻 manifest 后，无法继续可靠复用原图框范围。自动核验相邻清单后按层预览已在 0.3.1 修复。依据：[实体选择](../src/agentcad/sheets.py#L1334)、[单页清单核验](../src/agentcad/render_jobs.py#L68)、[实际分图案例](cases/sheet-split.md)。

6. **SVG 尚不能保证与 CAD 出图一致。** 缺少 SHX、bigfont 或专用符号时可能替代或缺字；外链图片和 OLE 使用占位，xref 不自动解析，自适应填充会简化过密图案。整批案例仍有 104 次缺失图片占位及 20 页字体警告。依据：[渲染限制](../src/agentcad/render.py#L114)、[案例显示边界](cases/sheet-split.md)。

7. **块和复杂曲线的测量能力不统一。** `measure` 不能直接把原生 INSERT 作为完整构件测量；部分 HATCH、ELLIPSE、SPLINE 面积使用曲线离散化，没有可认证的面积误差上界。线性离散容差不能当面积精度承诺。无效、自交面积已拒绝，不属于尚未处理的问题。依据：[不支持测量的实体](../src/agentcad/engine.py#L700)、[面积误差说明](../src/agentcad/area.py#L316)、[几何调用指南](agent-usage.md)。

8. **标注关联和轴网识别尚无通用工程语义。** 包含、最近距离和原生 LEADER 关联不能覆盖任意多段线箭头或 MLEADER 组合；`axes` 只支持水平、竖直 LINE 轴线，未覆盖斜轴和弧形轴网。唯一几何匹配仍需工程含义复核。依据：[关联方法](../src/agentcad/semantics.py#L253)、[轴网规则](../src/agentcad/semantics.py#L601)。

9. **房间建模仍需要显式映射。** 平面、立面、高度和门窗证据需要事先指定，仅支持轴对齐矩形房间。源图、规格和模型一致性验证不等于自动理解任意建筑图或生成通用 BIM。依据：[房间输入验证](../src/agentcad/room.py#L209)、[房间工作流](room-workflow.md)。

10. **Excel 算量仍是案例流程。** CLI 提供标签、实例与尺寸证据，由另写的脚本计算并生成工作簿；没有原生通用 Excel 或工程算量命令。承台案例还保留 CT2 尺寸冲突、CT3 构型假设，且尚未验证原生 Excel 引擎重算。依据：[命令入口](../src/agentcad/cli.py#L24)、[计算表与口径](cases/takeoff.md)。

安装方面，Windows 解释器误选已修复；pip 安装器接受 CPython 3.11–3.14，CI 当前覆盖 3.11–3.13。尚无 PyPI 发布或独立的免 Python 安装包。见[安装说明](installation.md)。

已有 100,000 实体、超过 20 MiB 的分页基准，热分页不再重新解析源图；输出超限也会实际裁剪并报告。上述已完成的保护机制不列为当前缺失项。
