# 验证记录

日期：2026-10-20；Windows x64，Python 3.12.13。

执行 `.venv\Scripts\python.exe -m pytest -q`：**91 passed，9 subtests passed**。
执行 `.venv\Scripts\ruff.exe check --select E4,E7,E9,F src tests`：通过。
存在 21 条第三方 pyparsing 弃用提示，未影响测试结果。
`uv build` 成功构建 wheel 和源码分发包；原始闭源分发包和本地 LibreDWG 下载不包含在内。

完整房间的新增验收与三维复测见 [房间工作流](room-workflow.md)。
初版 51 项基线检查已扩展至上述 91 项；房间端到端验收另保存 55 次 CLI 调用证据。

## 已验证的真实流程

- `scripts/bootstrap.ps1` 使用 uv.lock 配置当前虚拟环境。
- 官方 LibreDWG 0.14 发布包下载、SHA-256 校验及重复安装。
- 使用包含中文和空格的路径执行 CLI 创建、查询、测量及 SVG 导出。
- 示例 plate：7 个实体；外轮廓 120×80，周长 400、面积 9600；4 个 R5 圆孔。
- 独立官方 DWG 样本经 CLI 转换为 AC1015 DXF，保留 6 个模型空间实体：
  CIRCLE×1、TEXT×1、LWPOLYLINE×1、LINE×3。圆半径 20，三条线长度
  分别为 100、100、sqrt(20000)。
- 直接 DWG 输入经过临时转换后可查询、测量和渲染。
- 从真实 DWG 导入并添加示例几何，输出 DXF 回读获得 13 个实体。
- 失败计划不发布输出；默认禁止覆盖；dry-run 不写图纸。
- 无效 UTF-8、孤立 Unicode 代理字符、重复 JSON 键、NaN/Infinity、嵌套过深
  等输入有结构化错误，JSONL 服务能够继续处理后续请求。

## 复核发现与回归

1. 跨 DXF 版本设置曾使 ezdxf 丢弃 ACIS 实体。现在禁止已有图纸改变版本，
   并在发布前比较包含块定义的实体清单。
2. 导入 POLYLINE、INSERT 属性和 HATCH 边界的变换溢出曾绕过有限值检查。
   现在检查实际导出的 DXF 数值标签及附属实体，并将附属实体纳入序列化清单。
3. 位于 `(1e9,1e9)` 的单位方形曾因全局坐标叉积抵消而得到面积 0。
   现在以局部原点计算并用 math.fsum 累加，回归结果为 1。
4. 隐式 DWG 导入曾丢失转换诊断。现在将转换信息和限长诊断保留在结果中。

## 证据边界

真实 DWG 测试只覆盖一个固定的公开样本，不能据此声称对所有 DWG 版本、
复杂代理对象、ACIS 或行业专用对象都无损。样本与 GPL 授权及来源摘要位于
`artifacts/dwg-fixtures/`；通过 `scripts/install-libredwg.ps1 -IncludeTestFixture`
可重新下载相同来源和哈希的文件。

尝试使用 LibreDWG dxf2dwg 写回本项目生成的 DXF 时出现坏句柄，转换后无法回读。
因此本版本不开放 DWG 写回，也不宣称该链路通过验证。
