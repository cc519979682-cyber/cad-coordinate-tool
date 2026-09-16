# 原生依赖的补充许可

这里只保存 Python wheel 中没有完整附带、因此需要额外随发布包提供的固定版本通知材料。普通 Python 依赖的完整许可由 `scripts/collect_licenses.py` 从实际构建环境收集。

- `native/ezdwg-0.12.7/`：发布源代码包 Cargo.lock 列出的 21 个 registry crates 的许可。各 crate 下载包已通过锁文件中的 SHA-256 校验；公共来源和摘要见 `sources.json`。
- `native/tkdnd-2.9.5/`：tkinterdnd2 0.5.0 内 Windows x64 原生拖入库的上游许可。
- `native/tcl-8.6.12/`：适用于缺少 Tcl `license.terms` 的相应版本 CPython 安装包。

这些文件仅为许可证文本和来源记录，不包含第三方程序二进制。升级相关依赖时应重新核对并更新，不能将旧通知目录直接当作新版验证结果。
