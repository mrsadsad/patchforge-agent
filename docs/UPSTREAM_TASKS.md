# 真实开源缺陷的历史回放

两项任务均来自 [cachetools](https://github.com/tkem/cachetools) 的公开历史，保留修复前实际包源码，使用原始上游补丁作为参考。它们用于开发回归和演示真实多文件包中的定位修复；不是盲测、不是 SWE-bench，也不能据此宣称在未见问题上的泛化成功率。公开补丁可能出现在模型训练数据中。

| 任务 | 问题与来源 | 修复前提交 | 参考修复提交 |
| --- | --- | --- | --- |
| `upstream-cachetools-expired-overwrite` | TLRU 中以已过期值覆盖现有键时，旧值仍然保留。[上游修复](https://github.com/tkem/cachetools/commit/c0fdf6abab38040947d6fe2e38c507401d5e2350) | `978d34d40cdbf3b4cbfce104f3166032cc4ea028` | `c0fdf6abab38040947d6fe2e38c507401d5e2350` |
| `upstream-cachetools-growing-entry` | 按自定义大小计费的值增长时重复预留容量，造成多余驱逐。[上游问题 #405](https://github.com/tkem/cachetools/issues/405)、[上游修复](https://github.com/tkem/cachetools/commit/39b31bc9b63abe98497409945e9d382d8918c8fb) | `16e88894ef7d79b25a68f5a3b5411ed881342725` | `39b31bc9b63abe98497409945e9d382d8918c8fb` |

每个快照包含该父提交中未经修改的五个 Python 包文件，以及完整的原始 MIT LICENSE，版权归 Thomas Kemmer 所有。为了直接从沙箱工作区导入，路径只去掉 `src/` 前缀，文件内容不变。JSON `provenance.files` 记录每个文件的原始路径、固定提交来源链接及 SHA-256；参考编辑应用后，目标文件的摘要必须与上游修复提交完全一致。模型工作区不包含修复后源码或参考编辑。

每项任务各有 3 个可见测试和 4 个保留测试，由 PatchForge 根据问题契约编写。前者复现缺陷并检查基本行为；后者补充容量记账、过期堆的旧记录、重复覆盖、再次插入、驱逐目标键等边界。它们不是复制完整上游测试套件，也不代表覆盖上游项目全部行为。全部测试使用注入时钟和 Python 标准库，无需网络或依赖安装。

在 PowerShell 中检查来源完整性并通过已有 Docker 沙箱验证：

```powershell
$env:PATCHFORGE_DOCKER_TESTS = '1'
python -B -m unittest discover -s tests -p test_upstream_tasks.py -v
```

执行需要已启动的 Docker 以及本地 `python:3.12-slim` 镜像。两项任务都必须满足：原始代码的可见测试出现断言失败、无导入错误；应用精确上游参考补丁后，7 个测试全部通过。未设置环境变量时，只检查来源与补丁摘要，Docker 执行检查明确标为跳过，不作为执行通过。

需要重新取得固定来源时可运行 `python scripts/build_upstream_tasks.py`。构建脚本仅从固定 GitHub 提交下载、校验结构并序列化源码，绝不在宿主机导入或执行上游包。它只写入这两项 `upstream-*.json` 文件。正常运行任务不需要再次访问 GitHub。

任务划分为 `split="upstream-replay"`；应与 `development` 合成任务、`imported` 自有仓库任务分别报告。参考脚本的成功只证明补丁与验证链路可复现，模型能力对比仍需真实模型运行和完整失败记录。
