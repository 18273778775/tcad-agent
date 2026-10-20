# GitHub 项目调研与依赖决策

核查日期：2026-10-20。新代码为独立实现，通过公开库和外部工具接口集成。

| 项目 | 本项目的处理 | 选择理由与边界 |
| --- | --- | --- |
| [mozman/ezdxf](https://github.com/mozman/ezdxf) | 核心运行依赖，固定 1.4.3 | 提供 DXF 读写、实体模型及 SVG 绘图后端；MIT。它不是 DWG 原生解析器或几何约束求解器。 |
| [mikedh/trimesh](https://github.com/mikedh/trimesh) | 房间网格运行依赖，版本由 uv.lock 固定 | 用于 GLB/OBJ 网格构造及文件回读；MIT。房间模块限定于明确映射的矩形构件。 |
| [LibreDWG/libredwg](https://github.com/LibreDWG/libredwg) | 可选独立 `dwg2dxf` 进程，固定 0.14 | 官方提供 Windows 二进制；GPL-3.0-or-later。先进 DWG 对象可能遗漏，转换必须检查输出并保留诊断。 |
| [LibreCAD/LibreCAD](https://github.com/LibreCAD/LibreCAD) | 比较对象，未复制或链接代码 | 适合桌面 2D 编辑；本交付优先无界面、可部署的接口，未引入其 GUI 运行时。 |
| [CadQuery/cadquery](https://github.com/CadQuery/cadquery) | 后续 3D 方向，未引入 | OCCT 参数化建模框架；当前用户已选择 2D 图纸处理。 |

官方资料：

- [ezdxf SVG 绘图接口](https://ezdxf.readthedocs.io/en/stable/addons/drawing.html)
- [ezdxf ODA 转换接口说明](https://ezdxf.readthedocs.io/en/stable/addons/odafc.html)
- [LibreDWG 0.14 发布](https://github.com/LibreDWG/libredwg/releases/tag/0.14)
- [ezdxf MIT 许可证](https://github.com/mozman/ezdxf/blob/master/LICENSE)

运行时没有复制闭源 CAD 包中的 DLL。LibreDWG 下载由独立脚本显式执行，保留原包
授权材料；外部进程边界不构成对所有再分发场景的许可证法律结论。
ODA 只作为兼容性调研对象，未随项目提供，实际支持情况以 capabilities/doctor 返回为准。
