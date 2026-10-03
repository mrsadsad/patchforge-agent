# 0.2 验证记录

2026-10-03，Windows、Python 3.12.3、Docker Desktop Linux containers。完整 0.2 服务运行在 [8771](http://127.0.0.1:8771/)，健康接口返回十项任务和可用的真实模型配置。

- 设置 `PATCHFORGE_DOCKER_TESTS=1` 后执行 `python -m unittest discover -s tests -q`：**74 项全部通过，无跳过**。覆盖上下文与消息配对、批量编辑回滚、检查点、源码版本、文件接口、ZIP 校验、评测分母、目录互斥和原有边界。
- 全部八项合成任务原始实现可见测试失败，参考修改通过所有 56 项可见与保留测试定义。完整性测试只在宿主运行仓库内可信的参考源码；模型生成代码由 Docker 执行。
- 两项真实 cachetools 缺陷在 Docker 中复现失败，精确上游补丁各通过 7 项测试；校验原始源码和修复后的摘要。
- **20 次真实模型固定版本对照完成**，5 个独立任务、每项两策略两重复，实际 46 次请求；记录在 `eval/v02-repeated/`。开发探测失败另存，首版 `eval/first-live/` 保留。
- `python scripts/smoke_ui.py --url http://127.0.0.1:8771 --require-v02`：Edge headless **14 项检查通过**，包含历史记录、源码与版本切换、补丁与测试、版本摘要、ZIP 清单、分来源评测、演示、轨迹、上下文与来源标记。无 JavaScript 异常。
- 390px 无水平溢出。截图：`workbench-live.png`、`workbench-code.png`、`workbench-mobile.png`。
- 已添加 Windows/Linux、Python 3.11/3.12 的 GitHub Actions 配置和独立 Docker 回放任务；远端 CI 尚未触发，实际本轮执行环境为上述 Windows 主机。
- 原有 OpsLens 文件未修改；本次在独立运行目录与 8771 启动完整新版服务。

未验证：生产负载、多租户攻击、任意第三方依赖项目、对抗式测试作弊、其他模型供应商。已有测试通过不等于这些能力已实现。

8770 是首版遗留运行实例，本轮没有终止该进程。需要所有新功能时使用 8771。
