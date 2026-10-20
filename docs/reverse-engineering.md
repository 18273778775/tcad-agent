# 本地 CAD 包的静态逆向结论

分析日期：2026-10-20。输入目录：`cad快速看图破解版/`。
这是静态架构和接口分析；没有执行包内 EXE/DLL、恢复源代码或验证私有 IPC 协议。

## 观察到的事实

分发包共 27 个文件、72,568,865 bytes。主程序 `cad快速看图.exe` 为
17,337,736 bytes 的 32 位原生 PE，版本 5.4.0.40，无 CLR。
Qt 4.8.5、VC++2010、WebKit、OpenGL、FreeImage 3.16.0、libxl 3.5.3
构成主要依赖。图像插件仅见 GIF/JPEG；没有源码、SDK 头文件、类型库文件、开发文档
或 DWG/DXF 样本，也没有单独的 CAD 引擎 DLL。

主程序导出仅见两个标准库相关符号，未发现公开 CAD C API。
`CoCreateInstance` 导入不足以证明其提供 COM 自动化服务。
`QLocalServer`、`QLocalSocket`、`CADReaderServer`、`newConnection()`、
`readyRead()` 提供了本地 IPC 线索，但协议和可支持操作尚未验证。

DWG/DXF 文件选择过滤器及 `doSaveAs()`、`doPartiallyExportDrawing()`、
`doExportToExcel()` 和打印 Qt 槽说明 GUI 包含相关功能；不能把内部槽等同于
稳定公共 API。存在 `GetCommandLineW`，但静态搜索未发现公开的
`--help`、`/automation`、`/convert`、`/export` 约定。此结果不能证明全部命令行入口不存在。

## 推断与边界

`OdaTeigha`、`TeighaDWG` 和 Open Design Alliance 字符串提示包含 ODA 相关代码。
静态集成是合理推测；没有证据表明可以独立加载和合法再分发其 CAD 解析代码。
IPC 更可能用于单实例和打开文件传递，但这也只是推断。
`.vmp0` 节提示保护或混淆的可能性，单凭节名不能判定保护产品或恶意行为。

主程序 Authenticode 状态为 HashMismatch。与其等大小的 `CADReader.exe.BAK`
具有不同 SHA-256，且签名检查出现证书有效期错误；它不能被称为已验证的原版。

| 文件 | SHA-256 |
| --- | --- |
| cad快速看图.exe | 8AC97C8CF0CEDAA5124EA1655E6822C556A2D15C06A45CA4A298492617DEBC8D |
| CADReader.exe.BAK | A854E4C7F9865568B4EF8D567869FCB862E54F9226C1BD58BD17F14CC2A3F9C3 |

## 对新实现的直接影响

无法仅凭该包得到一个可靠的无界面 CAD SDK。CLI 因此采用独立实现，以公开 DXF
模型执行图层、实体、查询、测量和编辑；DWG 经独立转换器进入该模型。
旧软件包不参与新 CLI 的安装、运行、分发或测试。

若后续研究官方版本的自动化，应优先查阅官方文档并验证文件路径启动等公开行为。
本次交付没有声称已恢复原程序算法、内部对象模型或私有协议。
