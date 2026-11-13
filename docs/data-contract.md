# 离线能量与计费情景

本文件是模型口径的唯一来源。所有数值以字符串保存的有理数读写，求解器内部
使用 `fractions.Fraction`，不使用浮点数。

## 时间格与能量

每格功率恒定，单位 kW，能量 kWh。`hours` 是声明时长。相邻 `start` 之间的
空隙是**未调度停机**：自放电仍按这些小时推进，不能把一个月间隔当作无损瞬间
跳转。窗口首末 SOC 也包含这些空隙。

状态更新（`z_i` 为格末 SOC）：

```
z_i = z_{i-1} * r^{gap_i + hours_i} + eta_c * charge_i - discharge_i / eta_d
```

- `gap_i` = 本格开始 − 上一格结束（首格为 0；单位小时，可含分数）。
- `eta_c * charge_i`：充电能量按充电效率存入电池。
- `discharge_i / eta_d`：为交付 `discharge_i` kWh，电池侧要减少这么多。
- `r^{hours}` 的复合语义是 `(r)^{hours}`；若保留系数与分数时长使幂无精确
  有理数表示，读取器/模型明确报错，不使用浮点近似。

`charge`、`discharge` 均按**电池侧**能量计（充入量 / 放出量），与平衡式一致。

## 平衡与约束

```
import + pv_used + discharge = load + charge      逐格成立
0 <= pv_used <= pv * hours
0 <= import <= import_limit * hours
0 <= charge <= charge_limit * hours
0 <= discharge <= discharge_limit * hours
0 <= soc <= capacity
soc_0 = initial_soc,  soc_n = terminal_soc
```

禁止外送（`import >= 0` 且光伏只能自用或弃光）。**每格最多充电或放电一种
模式**：`charge_i * discharge_i = 0`。这是唯一的离散决策，即使负电价也不
删除。求解器用分支定界精确处理；小日历用完整 3^n 模式枚举交叉核对。

## 费用

```
费用 = sum(price_i * import_i)
     + throughput_fee * sum(charge_i + discharge_i)
     + sum_over_billing_periods(demand_fee_p * peak_import_power_p)
```

- `import_i` 已是能量（kWh），`price_i` 按 kWh 计。
- 需量费按**计费周期内最大购电功率**计：`peak_p = max_i(import_i / hours_i)`
  仅取该周期内的格。
- `throughput_fee` 按充电与放电的电池侧能量之和计。

## 排放

```
emission = sum(emission_factor_i * import_i)
```

逐格使用情景强度，不使用平均排放因子。上限是对该和的约束；每个上限独立
求解，不同 cap 之间不共享能量或模式。

## 破同分

费用相同时依次比较：排放 → 总吞吐量 → 按时间顺序的
`(import, charge, discharge, pv_used, soc)` 字典序。完整字典序只在最终最优
模式上做；搜索中用 (费用, 排放, 吞吐量) 三元组筛选，结果等价。

## 结果与证据

- 每个上限报告：可行性、最优费用、证明下界、间隙、逐格调度、独立重放校验。
- 下界来自连续松弛（删除模式互斥）或未处理分支的最小节点下界。
- 预算耗尽时状态为 `timeout`，给出可行调度与合法下界；**绝不**把"没找到解"
  当作无解。`infeasible` 仅在连续松弛不可行或全部分支处理完毕后给出。
- 离散模式会让费用-排放前沿非凸（可达点不在凸包上，或两个可行调度的
  凸组合不可行）。因此不能仅靠费用与排放加权和声称找全前沿；本工具对
  每个登记上限逐点求解。
- 每条调度都可用 `verify` 独立重放：逐格平衡、光伏、功率上限、模式互斥、
  状态推进（含间隙）、容量、首末状态、逐格排放、需量峰值与三类费用。
  校验器按定义重算保留系数，不复用求解器矩阵。

## 拆格

同一恒定条件时段拆成均匀小格后，只有在**声明仍允许每格独立选择
闲/充/放**时，才可把拆格结果与整格结果直接比较。若声明要求子格保持同一
模式，则结果只能与整格口径对照，不能混用。

## 缓存

结果缓存键包含：情景文件摘要、时间格资产摘要、逐格 `(id, start, hours)`、
电池参数、合同费率、排放上限清单与求解器限制。任何一项变化都会使旧结果
失效。

## 边界

场景函数是可信本地过程，只做离线方案分析，不发送控制指令。读取器做严格
形状核对，但不替分析者补默认值；`null` 不等于 0。
