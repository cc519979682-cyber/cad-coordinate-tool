# 开发与贡献

请先用 `examples/` 内的虚构数据复现问题。问题反馈包括 Windows、Python（源码运行时）、AutoCAD 版本，软件版本，以及具体操作步骤。公开附件应清除项目名称、真实坐标、文件路径和其他业务内容。

## 环境

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py --demo
.\.venv\Scripts\python.exe scripts/run_tests.py
```

标准回归每个模块使用独立 Python 进程，以隔离 Tk/ttkbootstrap 的全局样式状态；请使用测试运行器，而不是把全部 UI 测试塞进同一进程。报告写入 `reports/`，不进入 Git。

需要 AutoCAD 的测试默认跳过，须在本机安装 AutoCAD Core Console，并显式设置相应 `COORDTOOL_NATIVE_*` 测试开关。具体变量和跳过原因以对应测试文件为准；测试使用合成图纸，不操作正在使用的 CAD 文档。GitHub 托管运行器未安装 AutoCAD，因此 CI 不覆盖实际 CAD 捕捉、交互或原生转换。

## 修改原则

- 内部坐标使用米；保持 E/N、CAD X/Y、图纸比例与偏移一致。
- 坐标导入不能猜测投影或静默丢弃无效行。
- 已确认取点、撤销、恢复和 ZB 续接保持日志一致性；取消未确认标注不能留下孤立数据。
- 不把真实工程文件、个人部署脚本、运行日志、凭据或绝对工作路径加入仓库。
- 示例和回归数据必须可独立构造；涉及格式或几何行为的改动需增加对应回归覆盖。

## 构建

```powershell
pwsh -File build.ps1 -PythonExe .\.venv\Scripts\python.exe
```

`build.ps1` 使用 PyInstaller 构建完整文件夹，并收集第三方许可。请整体压缩 `dist/坐标生成器v22.8`，同时提供 SHA-256 校验文件。发布前从干净环境运行标准回归并验证示例、启动和导出。

提交贡献即表示你有权按本仓库的 MIT 许可证提供这些改动。第三方代码保留其原许可和署名，并更新第三方说明。
