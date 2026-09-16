# 第三方组件与许可证

本项目自行编写的代码采用 [MIT License](LICENSE)。第三方库、字体和原生运行库保留各自的版权和许可；本项目的 MIT 许可不会替代它们。

## 主要组件

| 组件 | 用途 | 许可说明 |
| --- | --- | --- |
| [ttkbootstrap](https://github.com/israel-dryer/ttkbootstrap) | 界面主题 | 安装包声明 `MIT AND (Apache-2.0 OR BSD-2-Clause)` |
| [tkinterdnd2](https://github.com/Eliav2/tkinterdnd2) / [tkdnd](https://github.com/petasis/tkdnd) | 文件拖入 | Python 包 MIT；内附 tkdnd 原生库使用自身的宽松许可 |
| [Matplotlib](https://matplotlib.org/stable/project/license.html) | 图形与底图显示 | Matplotlib 自身许可证；内附字体、FreeType 等另有条款 |
| [NumPy](https://numpy.org/doc/1.26/license.html) | 数值计算 | BSD；wheel 还包含 OpenBLAS、GCC 运行库等第三方许可 |
| [Pillow](https://github.com/python-pillow/Pillow) | 图像处理 | MIT-CMU；wheel 的完整 LICENSE 包含其原生图像库条款 |
| [ezdxf](https://github.com/mozman/ezdxf) | DXF 读写 | MIT |
| [ezdwg](https://github.com/monozukuri-ai/ezdwg) | 简易 DWG 读取 | MIT；Rust/PyO3 扩展的依赖另附许可，见下文 |
| [openpyxl](https://foss.heptapod.net/openpyxl/openpyxl) / [xlrd](https://github.com/python-excel/xlrd) | Excel 坐标导入 | MIT / BSD |
| [pywin32](https://github.com/mhammond/pywin32) | Windows COM 与 CAD 联动 | 保留安装包内各子组件许可 |
| [PyInstaller](https://pyinstaller.org/en/stable/license.html) | Windows 打包 | GPL 及其打包例外；另有 hooks-contrib 许可 |
| [CPython](https://docs.python.org/3/license.html) / [Tcl/Tk](https://www.tcl.tk/software/tcltk/license.html) | Python 与界面运行时 | PSF 及分组件条款 / Tcl/Tk 自身许可 |

准确版本见 `requirements.txt`；发布包中 `licenses/manifest.json` 记录实际构建解释器和依赖版本。表格用于导航，不代替完整许可证。

## Windows 发布包

使用与打包相同的干净虚拟环境运行：

```powershell
python scripts/collect_licenses.py --output build/release-licenses
```

输出目录必须为空。脚本从 `requirements.txt` 出发，按照当前平台、Python 版本和 extras 解析已安装依赖，收集完整的 LICENSE、LICENCE、COPYING、NOTICE 等文件，而非扫描开发者环境内所有包。它还收集 CPython、Tcl/Tk 的许可和固定版本原生组件补充说明，并生成 SHA-256 清单。输出只记录包名、版本和相对路径，不记录开发者安装路径。

发布时将输出目录完整放入压缩包的 `licenses/`，同时保留本文件和主项目 `LICENSE`。依赖包许可证中包含的第三方版权、字体名称和例外条款不能仅替换成表格中的 SPDX 简称。NumPy 等 wheel 的完整许可还说明了原生库源码获取地址及适用条款；重新构建或更换 wheel 时需重新核对其源码提供、替换/重链接等要求。

`licenses/native/ezdwg-0.12.7/` 的补充材料取自 PyPI 官方源代码包 `Cargo.lock` 所列全部 21 个 registry crates，包括构建期和其他平台依赖。crate 源码包按该锁文件的 SHA-256 校验，仅提取许可证；`sources.json` 保留公共下载地址、版本及校验值。这是较宽的通知集合，不意味着每个 crate 都链接进 Windows wheel。未将 ezdwg 误当作 LibreDWG，也未在此项目中附带 Autodesk 或 ODA 的软件。

`licenses/native/tkdnd-2.9.5/` 补充 tkinterdnd2 0.5.0 内 Windows x64 DLL 的上游许可；`licenses/native/tcl-8.6.12/` 补充该版本部分 CPython 安装包缺失的 Tcl 许可。更换对应依赖或 Tcl 版本时，收集脚本会要求更新缺失的补充材料。

AutoCAD 由使用者自行安装并按其许可使用，本仓库及发布包不提供 AutoCAD 程序、商用字体包或真实工程图纸。

这些材料用于记录来源和满足分发时的通知需求；自动收集结果不等同于完整的法律审核，也不能自动证明任意修改后的二进制已经满足全部分发义务。
