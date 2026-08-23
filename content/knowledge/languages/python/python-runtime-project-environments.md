---
id: aikb:knowledge:languages:python:runtime-project-environments
type: knowledge
status: verified
tags: [python, runtime, version-management, virtual-environment, venv, uv, pip, pycharm, vscode, windows]
applicable_versions: "Python 3.9+ 的 venv 工作流；pip、uv、PyCharm 与 VS Code 的行为以 2026-08-24 官方文档为准"
last_verified: 2026-08-24
review_when: "升级 Python 主次版本、切换版本/依赖管理器，或 uv、PyCharm、VS Code 的环境发现与创建行为发生变化时"
supersedes: []
relations: []
---

# 以项目为边界管理 Python 运行时、虚拟环境与依赖

## 背景

Python 开发中的“版本管理”实际涉及三层：机器上可用的 **Python 解释器**、项目的 **虚拟环境** 与项目的 **依赖声明/锁文件**。把这三层混为一谈，容易把依赖误装到全局解释器，或让 IDE、终端、调试器使用不同环境。

本条目把它们固定为以项目为中心的工作模型，并覆盖 Windows 下常见的 `uv`、PyCharm 和 VS Code 使用方式。

## 问题

需要同时满足以下要求：

- 一台机器可并存多个 Python 主次版本，旧项目不会被新解释器自动改变。
- 每个项目拥有隔离、可删除且可重建的运行环境。
- 依赖可由版本控制中的声明和锁文件重建，而不是只存在于某台机器的 `site-packages`。
- IDE、终端、运行和调试始终定位到同一个项目解释器。

## 解决方案

### 1. 固定三层边界

| 层次 | 责任 | 推荐的项目内载体 | 是否提交 Git |
| --- | --- | --- | --- |
| Python 解释器 | 提供某个 Python 版本及标准库，可在机器上并存多个版本 | `requires-python` 和可选 `.python-version` 记录项目约束/偏好 | 是 |
| 虚拟环境 | 隔离当前项目实际安装的包，绑定创建它的基础解释器 | `.venv/` | 否 |
| 依赖声明与解析结果 | 说明项目需要什么，并让环境可重建 | `pyproject.toml`、`uv.lock`（或项目既有的等价文件） | 是 |

不要把 `.venv` 当作项目的权威状态：它是由解释器版本和依赖声明生成的本机运行产物，应加入 `.gitignore`。项目需要的 Python 版本属于项目依赖约束的一部分；`pyproject.toml` 的 `[project].requires-python` 是兼容范围的权威声明。

### 2. 用当前解释器判断包会装到哪里

`pip` 不会根据当前目录自动推断“项目模式”；未显式使用 `--target`、`--user`、`--prefix` 等目标选项时，安装位置取决于运行 pip 的 Python 环境。因此优先使用：

```powershell
python -m pip install <package>
```

而不是不加核对地调用裸 `pip`。先执行下列命令，确认终端与 IDE 终端真正使用的解释器和 pip：

```powershell
python -c "import sys; print('exe:', sys.executable); print('prefix:', sys.prefix); print('base:', sys.base_prefix)"
python -m pip --version
where.exe python
where.exe pip
```

当 `sys.prefix != sys.base_prefix` 时，当前 Python 正在虚拟环境中运行；`sys.base_prefix` 指向创建它的基础 Python。若命令输出的是项目根目录下的 `.venv\\Scripts\\python.exe`，随后通过 `python -m pip` 安装的包会进入这个项目环境。

### 3. 把虚拟环境理解为目录，而非需要常驻“激活”的模式

`venv` 在项目目录（通常为 `.venv`）创建隔离环境。PowerShell 中的 `.venv\\Scripts\\Activate.ps1` 只会修改**当前 shell** 的 `PATH`，使 `python` 和已安装脚本优先解析为 `.venv` 中的可执行文件；关闭终端后该状态自然结束。也可以不激活，直接调用 `.venv\\Scripts\\python.exe`，或让 `uv run`、IDE 使用已选定的项目解释器。

除非有明确的兼容需求，创建项目环境时不要启用“继承基础解释器的包”（PyCharm 中为 `Inherit packages from base interpreter`）。该选项会暴露全局解释器已安装的包，削弱隔离并使项目依赖的来源不透明。

### 4. 默认的 `uv` 工作流

对于常规应用、CLI 和库开发，使用 `uv` 可统一 Python 下载/发现、项目环境和依赖同步：

```powershell
# 按需安装并查看解释器
uv python install 3.13
uv python list --only-installed

# 在项目根目录固定偏好的解释器版本，并由声明/锁文件同步环境
uv python pin 3.13
uv sync
uv run python -c "import sys; print(sys.executable)"
```

`uv python pin` 创建或更新 `.python-version`；项目命令还会遵守 `pyproject.toml` 中的 `requires-python`。为便于与 pyenv 等也读取该文件的工具互操作，`.python-version` 中优先使用简单版本号，例如 `3.13`，不要写 uv 特有的复杂请求格式。

`uv pip` 的环境写入命令依次发现 `VIRTUAL_ENV`、`CONDA_PREFIX` 和当前目录或最近父目录中的 `.venv`；`uv sync`、`uv run` 等项目命令则使用项目环境，其默认路径是项目根目录下的 `.venv`，也可通过 `UV_PROJECT_ENVIRONMENT` 调整。项目操作默认不会采用指向其他路径的 `VIRTUAL_ENV`；确需使用当前激活环境时显式传入 `--active`。若团队已采用 Poetry、Conda、pyenv 等工作流，应维护原有的权威依赖文件和环境创建命令，不要在同一项目混用多个包管理器来修改同一环境。

### 5. 让 IDE 绑定项目解释器，而非全局 Python

- **PyCharm**：在状态栏解释器选择器或 `Settings | Python | Interpreter` 中，将当前项目绑定到 `<project>\\.venv\\Scripts\\python.exe`。创建环境时显式选择基础解释器和项目内 `.venv` 路径；通常不选择 `Inherit packages from base interpreter`，也不将环境开放给所有项目。若需要 IDE 内终端与运行配置一致，启用 `Settings | Tools | Terminal | Activate virtualenv`。
- **VS Code**：在状态栏或命令面板执行 `Python: Select Interpreter`，选择 `<project>\\.venv\\Scripts\\python.exe`。所选环境用于运行、调试和 IntelliSense；默认调试也使用这个选择，只有在 `launch.json` 显式设置 `python` 时才会覆盖。新建环境后再次确认状态栏解释器路径。
- IDE 的“安装包”动作会修改当前所选解释器的环境，但不应成为依赖的唯一真相。通过 `uv add` 或项目约定的包管理命令修改声明和锁文件，再让 IDE 识别/同步结果。

### 6. 迁移 Python 主版本或次版本时重建项目环境

机器新增 Python 版本不会自动改写已有 `.venv`；旧项目可继续使用原解释器。决定把项目从一个 Python 主版本或次版本迁移到另一个版本时，将其当作依赖兼容性升级：

1. 确认 `requires-python`、直接依赖、CI 和目标部署环境支持新版本。
2. 删除或移走可重建的旧 `.venv`，不要尝试在原环境中“就地升级”基础解释器。
3. 用目标解释器创建新环境；对于 uv 项目，更新版本约束/`uv python pin` 后执行 `uv sync`。
4. 运行测试、类型检查、构建及关键运行路径；含 C 扩展的依赖尤其要验证对应 Python ABI 的 wheel 或构建结果。
5. 将 PyCharm/VS Code 的项目解释器和调试配置重新指向新的 `.venv`。

这使旧项目与新项目可在同一机器共存，例如项目 A 继续使用 3.12，而项目 B 使用 3.13；升级是项目的受控变更，不是系统 Python 的连带副作用。

对于 uv 管理的 Python，同一次版本内的补丁升级可使用 `uv python upgrade`；未显式固定到具体补丁版本的相关虚拟环境可随之透明更新。uv 不会跨次版本透明升级，显式固定补丁版本的环境也不会自动迁移，因此不替代上述跨版本重建与兼容性验证。

## 验证

- 2026-08-24 已依据 CPython `venv` 与 pip 官方文档复核：虚拟环境的 `sys.prefix`/`sys.base_prefix` 语义、activation 仅将环境可执行目录前置到当前 shell 的 `PATH`，以及 pip 的显式安装目标选项。
- 已依据 uv 官方文档复核：`uv python install/list/pin`、`.python-version`、`requires-python`、`uv pip` 与项目环境的发现边界、按需下载 Python，以及 uv 管理的 Python 补丁升级行为。
- 已依据 PyCharm 与 VS Code 官方文档复核：项目解释器选择、Virtualenv 创建、基础包继承、IDE 终端激活、运行/调试/IntelliSense 使用所选解释器的行为。
- 本条目是跨项目工作模型，并非对某个现有项目的环境进行过运行验证；具体项目迁移仍须在其锁文件、CI 和测试集上复核。

## 适用范围

- 适用于 Windows 优先、也适用于其他平台的 CPython 项目；命令中的激活路径是 Windows PowerShell 形式。
- `uv` 适合希望整合解释器、虚拟环境与依赖管理的一般 Python 项目。涉及 CUDA、系统级二进制库、特定 Conda channel 或科研分发时，可继续采用 Conda/mamba 工作流，并避免在同一环境里交叉修改。
- `.python-version` 表示解释器选择偏好，`requires-python` 表示项目支持范围；二者不应相互矛盾。面向多版本发布的库还需在 CI 中覆盖声明的版本范围。
- 此条目不替代 Python 发行版、包管理器或 IDE 的当前官方安装说明；遇到工具更新、受控企业镜像、WSL/Docker/远程解释器或遗留项目时应按当时文档复核。

## 关联信息

- [CPython: venv — Creation of virtual environments](https://docs.python.org/3.14/library/venv.html)
- [pip: pip install](https://pip.pypa.io/en/stable/cli/pip_install/)
- [uv: Python versions](https://docs.astral.sh/uv/concepts/python-versions/)
- [uv: Using environments](https://docs.astral.sh/uv/pip/environments/)
- [uv: Configuring projects](https://docs.astral.sh/uv/concepts/projects/config/)
- [PyCharm: Configure a Python interpreter](https://www.jetbrains.com/help/pycharm/configuring-python-interpreter.html)
- [PyCharm: Terminal settings](https://www.jetbrains.com/help/pycharm/settings-tools-terminal.html)
- [VS Code: Python environments](https://code.visualstudio.com/docs/python/environments)
- [VS Code: Python language support](https://code.visualstudio.com/docs/languages/python)
