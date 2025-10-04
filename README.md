# Python 依赖一键安装工具

<!-- PROJECT SHIELDS -->
[![Python][python-shield]][python-url]
[![MIT License][license-shield]][license-url]

<!-- PROJECT LOGO -->
<br />
<div align="center">
  <h3 align="center">Python 依赖一键安装工具</h3>
  <p align="center">
    自动扫描并安装项目所需的全部第三方库
    <br />
    <a href="#快速开始">快速开始</a>
    ·
    <a href="#功能特性">功能特性</a>
    ·
    <a href="#命令参数">命令参数</a>
    ·
    <a href="#常见问题">常见问题</a>
  </p>
</div>

---

## 📖 目录

- [快速开始](#快速开始)
- [功能特性](#功能特性)
- [安装与使用](#安装与使用)
- [命令参数](#命令参数)
- [示例](#示例)
- [常见问题](#常见问题)
- [贡献指南](#贡献指南)
- [许可证](#许可证)

---

## 🚀 快速开始

1. 将脚本放入项目根目录
2. 运行以下命令即可自动扫描并安装依赖：

```bash
python install_deps.py
```

---

## ✨ 功能特性

- ✅ **自动扫描**：递归扫描项目内所有 Python 文件，提取 import 依赖
- ✅ **智能过滤**：自动排除标准库与无效包名
- ✅ **多镜像源**：自动切换国内镜像，提升安装成功率
- ✅ **安全校验**：禁止在系统目录运行，防止误操作
- ✅ **交互确认**：安装前展示计划，用户确认后执行
- ✅ **模拟运行**：支持 `--dry-run` 预览安装计划
- ✅ **进度美化**：支持 `rich` 库彩色进度条（可选）
- ✅ **虚拟环境感知**：自动检测并提示当前虚拟环境
- ✅ **生成 requirements.txt**：自动生成标准依赖文件

---

## 📦 安装与使用

### 环境要求

- Python 3.6 及以上版本
- 推荐使用虚拟环境（venv / conda）

### 一键运行

```bash
# 基本用法
python install_deps.py

# 升级已安装包
python install_deps.py --upgrade

# 强制重新安装
python install_deps.py --force

# 模拟运行（不实际安装）
python install_deps.py --dry-run
```

---

## 🧪 命令参数

| 参数         | 说明                           | 示例                         |
|--------------|--------------------------------|------------------------------|
| `--upgrade`  | 升级所有已安装的第三方库       | `python install_deps.py --upgrade` |
| `--force`    | 强制重新安装所有包             | `python install_deps.py --force`   |
| `--dry-run`  | 模拟运行，仅展示安装计划       | `python install_deps.py --dry-run` |

---

## 📚 示例

### 示例 1：基本安装

```bash
$ python install_deps.py
========================================
Python 依赖安装工具
自动扫描并安装项目所需第三方库
========================================
项目目录: /home/user/my_project
Python解释器: /home/user/my_project/venv/bin/python
操作系统: Linux 5.15.0
发现 5 个第三方库: numpy, pandas, requests, rich, tqdm
将安装 3 个新包: numpy, pandas, requests
是否继续? [Y/n] y
[████████████████████████████████] 3/3 安装完成
✓ 所有依赖安装成功
```

### 示例 2：模拟运行

```bash
$ python install_deps.py --dry-run
模拟运行模式，不会实际安装任何包
将安装 3 个新包: numpy, pandas, requests
将升级 2 个包: rich, tqdm
✓ 模拟运行完成
```

---

## ❓ 常见问题

### Q1：如何跳过某些目录或文件？
在脚本顶部修改 `EXCLUDE_DIRS` 和 `EXCLUDE_FILES`：

```python
EXCLUDE_DIRS = {"venv", "env", ".venv", "__pycache__", "tests"}
EXCLUDE_FILES = {"install_deps.py", "setup.py"}
```

### Q2：如何添加私有 PyPI 源？
在 `MIRRORS` 列表中追加你的私有源：

```python
MIRRORS = [
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://your-private-pypi.com/simple",
]
```

### Q3：脚本是否支持 Conda 环境？
支持！只要激活 Conda 环境后运行脚本即可，脚本会自动识别当前 Python 解释器。

---

## 🤝 贡献指南

欢迎提交 Issue 和 Pull Request！

1. Fork 本项目
2. 创建你的功能分支 (`git checkout -b feature/AmazingFeature`)
3. 提交你的修改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 打开一个 Pull Request

---

## 📄 许可证

本项目基于 MIT 许可证开源，详见 [LICENSE](LICENSE)。

---

## 🙋‍♂️ 作者

**LuiKlee**  


---

<!-- MARKDOWN LINKS & IMAGES -->
[python-shield]: https://img.shields.io/badge/python-3.6+-blue?style=flat&logo=python
[python-url]: https://www.python.org/
[license-shield]: https://img.shields.io/badge/license-MIT-green
