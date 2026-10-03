# 导入真实仓库缺陷

导入器把一个已有的本地 Python 仓库裁剪成可复现的修复任务。它不执行仓库代码、不安装依赖、不发起网络请求，也不修改原仓库。导入成功仅说明快照符合格式和大小限制；后续运行仍需先复现可见测试失败。

命令行入口：

```powershell
python -m forge import-task --repo C:/work/my-library --issue-file C:/work/bug-report.txt --editable package/parser.py --visible-test tests/test_parser.py --hidden-test evaluation/test_regression.py --id parser-empty-record --title "修复解析器空记录处理"
python -m forge run --task parser-empty-record --mode live
```

0.2 会建立文件/符号索引，按问题与基线失败词项选择初始源码片段（上限 24,000 个 JSON 字符），模型可通过 read_file 按行补读。运行历史在 60,000 字符预算下压缩完整旧工具回合，保护区仍放不下时返回 `context_limit`。导入的 300 KB 限制不是模型上下文承诺；过大的问题说明或索引也可能触发明确的预算错误。重启工作台后才能加载新导入的任务。工作台与 CLI 需使用不同 `--runs` 目录。

```python
from pathlib import Path
from forge.importer import import_task

task_file = import_task(
    repo=Path("C:/work/my-library"),
    issue_file=Path("C:/work/bug-report.txt"),
    editable=["package/parser.py", "package/normalizer.py"],
    visible_tests=["tests/test_parser.py"],
    hidden_tests=["evaluation/test_regression.py"],
    task_id="parser-empty-record",
    title="修复解析器空记录处理",
    task_dir=Path("tasks"),
)
print(task_file)
```

所有代码和测试路径必须相对于 `repo`，使用 `/` 分隔。问题描述文件可以位于仓库外。任务 ID 使用小写英文开头、小写英文与数字组成的单词，以单个 `-` 连接，最长 64 个字符。已有同名 JSON 不会被覆盖；修改后可换一个任务 ID。生成任务标为 `split="imported"`、`demo_available=false`，没有内置答案补丁，必须使用真实模型运行。

## 快照范围

- 自动包含普通 `.py` 源码，以及仓库根目录的 `README`、`README.md`、`README.rst`、`README.txt`。只读源码可用于检索；只有显式列出的 `editable` 文件能修改。
- 至少选择一个可见测试，当前版本只接受一个隐藏测试文件。测试须采用 `unittest`，文件名为 `test_*.py`，名称仅含英文字母、数字和下划线。
- 可见测试复制到快照根目录，隐藏测试仅写入任务 JSON 的 `hidden_tests` 字段，不写入模型工作区。任意两个选中的测试不能具有相同文件名；`test_hidden.py` 保留给隐藏测试执行器，不能用作可见测试名。
- 未选中的 `test_*.py`、`*_test.py` 和 `tests/`、`test/` 下内容不进入快照。选中的测试可来自这些目录。测试文件不能列为可编辑文件。
- 跳过隐藏目录、`.git`、`.env`、虚拟环境、`__pycache__`、`node_modules`、构建输出和符号链接/Windows junction。显式选中被排除的路径或链接会直接报错。隐藏测试的硬链接别名也不会进入快照。
- 跳过常见秘密文件名和匹配常见密钥、私钥、凭据字面量模式的源码；如果这些内容出现在问题描述或显式选中的代码/测试中，则拒绝导入。此筛选不是完整的秘密检测器；导入前应检查待提供给模型的源码和 README。

限制为合计最多 **60 个文件、300,000 字节**，每个文件最多 **40,000 字节**。合计包含可见和隐藏测试；问题描述另限 **8,000 字节**。文本必须使用 UTF-8，不能含 NUL 等二进制控制字符。目录扫描最多检查 5,000 个条目；超过限制时应先建立更小的源码目录。报错只包含短错误码，不回显源文件内容。

## 测试适配要求

当前沙箱使用 Python 标准库容器，工作区加入导入路径。代码应能直接通过 `import package.module` 从仓库根目录导入；`src/` 布局、第三方依赖、原生扩展、数据文件、安装步骤均需先整理成这个运行环境支持的最小复现。导入器不会自动修改 import 或搬动源码包。

测试会作为根级模块执行，因此不支持测试目录相对导入、测试目录专属 helper、`conftest.py`、pytest fixture 或依赖测试文件原位置的文件读取。每个选中的测试文件应自包含，仅导入 Python 标准库和快照中的源码模块。生产源码的包内相对导入可以保留。隐藏测试不应依赖可见测试模块。

同一个 JSON 中保留隐藏测试是为了运行器验证；具有本地文件读取权限的项目操作者仍可读取它。导入器不把它发送给模型，这不等于为恶意代码提供完整的测试机密隔离。

建议选择一个能稳定复现的小缺陷，准备断言明确的公开复现和额外边界回归，再比较单次补丁与多轮修复策略。真实任务结果应与内置合成开发任务分开展示，并记录来源、许可、模型配置和失败案例。
