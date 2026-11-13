# 储能移峰的费用排放前沿与调度证据

本项目是**离线方案分析工具**：给定负荷、电价与排放强度情景，比较费用与
排放的可达前沿，解释两种目标的冲突。它不控制任何设备，也不发布投资建议。

核心结论（由可核对的小日历演示得到）：

- 储能"省的钱"来自把购电从贵时段移到便宜时段、以及压低计费周期需量峰值；
  但便宜时段往往排放更高，所以**减排与省钱可能相互抵消**。
- 离散的"每格至多充或放一种模式"会让费用-排放前沿**非凸**：两个可行
  调度的凸组合可能不可行（同一格同时半充半放），因此仅用加权和找不全前沿。
- 负电价下，删除模式互斥的连续松弛会同时充放、把负电价当可重复补贴，
  给出过低的费用下界；整数最优费用可能明显更高。
- 末态 SOC、往返效率与跨计费周期需量费都会改变可达的排放下界与前沿形状。

## 安装与运行

Python 3.11 以上，无第三方依赖。

```sh
python -m unittest discover -s tests -v          # 全部测试
python -m compileall -q analysis_data tests
python -m analysis_data.cli inspect data/normal.json
python -m analysis_data.cli frontier demo_data/small-calendar.json --cache out/cache
python -m analysis_data.cli demo                  # 生成并报告全部演示资料
```

`demo_data/` 由 `analysis_data.demo` 生成，可随时重建。

## 数据与口径

- 情景配置（`*.json`）引用时间格资产（`*.jsonl`），并带字节数、SHA-256
  与行数；读取时会核对。有理数一律以字符串保存（`"1/2"`、`"0.25"`）。
- 时间格可不等长；相邻 `start` 之间的空隙是未调度停机，自放电按真实小时
  推进。详见 [资料说明](docs/data-contract.md)。
- 费用 = 购电费 + 吞吐衰减费 + 各计费周期需量费；排放 = 逐格实际购电能量
  × 情景强度。禁止外送，光伏可弃。
- 每个排放上限独立求解，不共享能量或模式。DP 破同分：费用 → 排放 →
  吞吐量 → 逐格决策字典序。

## 代码结构

| 模块 | 职责 |
| --- | --- |
| `analysis_data/numeric.py` | 精确有理数解析与规范化输出 |
| `analysis_data/scenario.py` | 冻结情景的严格读取、形状核对与摘要 |
| `analysis_data/lp.py` | 精确有理数两阶段单纯形（Bland 规则），带证书核对 |
| `analysis_data/model.py` | 变量、约束、目标、状态推进与解映射 |
| `analysis_data/optimizer.py` | 精确分支定界、完整模式枚举、下界与间隙 |
| `analysis_data/verify.py` | 独立逐格重放校验（不复用求解器矩阵） |
| `analysis_data/frontier.py` | 逐上限前沿、拆格对照 |
| `analysis_data/report.py` | 钱省在哪、排放移到哪、哪些目标达不到 |
| `analysis_data/demo.py` | 演示资料与穷举报告 |
| `analysis_data/cache.py` | 绑定格边界/合同/情景摘要的结果缓存 |
| `analysis_data/cli.py` | `inspect` / `frontier` / `verify` / `demo` |

## 演示资料

| 名称 | 要看的现象 |
| --- | --- |
| `small-calendar` | 三格小日历：便宜时段高排放，储能移峰省钱但增排 |
| `negative-price-simultaneous` | 负电价下连续松弛同时充放，下界与整数费用差 3/2 |
| `cross-billing` | 月末充电、次月放电，压低次月需量峰值 |
| `roundtrip-loss` | 往返效率 1/4，每放 1 kWh 要买 4 kWh |
| `terminal-soc-one` / `terminal-soc-zero` | 末态约束改变可达最小排放（6 vs 5） |
| `nonconvex-midpoint` | 两个可行调度，凸组合不可行 → 费用-排放集非凸 |
| `combined` | 跨期 + 需量费 + 效率损失 + 负电价同时出现 |
| `split-one` / `split-two` | 同一恒定时段拆格：独立选模式 vs 绑定同一模式 |

## 已知边界

- 求解器为精确算法；复杂大日历可能触达预算，此时输出 `timeout`、可行调度
  与合法下界，不宣称最优，也不宣称无解。
- 报告只覆盖业务登记的有限上限，不代表连续前沿已找全。
- 地区、家庭、合同、价格及排放数据均为合成，不代表真实政策评价。
- Dockerfile 固定 Linux 镜像；`docker build -t climate-analysis .` 构建。
