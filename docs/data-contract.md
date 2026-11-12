# 离线能量与计费情景

每格功率恒定，单位kW，能量kWh。hours为声明时长，start间空隙是未调度停机，自放电也须按空隙小时推进，不能把一个月间隔当作无损瞬间跳转；窗口总初末SOC含这些空隙。状态更新先按本格及停机间隙适用的保留系数衰减，再加eta_c*c*hours减d*hours/eta_d。hourly_retention^hours定义复合衰减。

平衡import+pv_used+discharge=load+charge，0<=pv_used<=pv，import非负且不超过上限。每格charge、discharge最多一项正。费用=sum(price*import*hours)+throughput_fee*sum((charge+discharge)*hours)+各周期需量费率*该周期import最大值。相等费用按排放、总吞吐量，再按时间顺序(import,charge,discharge,pv_used,SOC)字典序。

每个排放上限求一个优化问题，不同cap不强制共享模式。只报告给定cap对应的非支配点，不以有限取样声称所有连续前沿已找全。场景函数是可信本地过程，不发送控制指令。读取器只检查时格及电池参数基本形状。
