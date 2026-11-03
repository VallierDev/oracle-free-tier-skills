---
name: oracle-free-tier-skills
description: Query Oracle Cloud E2 Micro capacity, run a single-instance grabber with quota checks, and report monthly public outbound traffic for configured OCI accounts. Use for OCI 免费 E2 抢机、容量查询、流量日报或 8 TB 预警。
---

# Oracle Free Tier Skills

在技能目录创建 Python venv 并安装 requirements.txt。运行主机自行配置 ~/.oci/config，API 私钥权限 600。先查看 README.md 的配置示例和验证边界，再按任务选择模式。

## 月流量查询

将 accounts.example.json 复制成用户本地的 accounts.local.json，填写已有 OCI profile 名称。执行：

```bash
.venv/bin/python scripts/traffic.py --accounts accounts.local.json
```

历史月份加 --month YYYY-MM，机器可读输出加 --json。读取的是 UTC 计费月公网出站用量，1 TB 按 1000 GB 换算。报告未知值及错误，保留查询时间和数据延迟说明。

用户要求日报时，将命令交给其已有调度器，先核对同一任务是否已配置。用户要求 8 TB 提醒时，根据 reached_8tb 按 profile 和计费月去重。每日汇报与阈值告警按用户选择区分。脚本输出报告，调度器负责发送。

## E2 Micro 抢机

复制 grab.example.json 到本地 grab.local.json，填写目标账号、主区域、完整 AD、根区间公网子网和 SSH 公钥路径。状态目录只用于这次单实例任务。

```bash
.venv/bin/python scripts/grab.py --settings grab.local.json --preflight
.venv/bin/python scripts/grab.py --settings grab.local.json
```

第一条检查免费名额、总盘量、主区域和创建输入，第二条只查容量。用户明确授权自动创建一台时，才执行：

```bash
.venv/bin/python scripts/worker.py --settings grab.local.json --interval 900
```

确认已有调度器未同时运行。沿用唯一 worker、同一账号和状态目录。提交结果不明时保留 launch.json 并回查原实例，禁止清空凭证再提交。worker 在实例 RUNNING、额度阻碍、未知提交结果或连续三次错误时结束。

E2 Micro 上限检查为两台，新增盘固定 50 GB、10 VPU，主区域启动盘与数据盘累计不超过 200 GB。镜像使用官方 Ubuntu 24.04 x86 平台镜像。实际费用还须结合账号合约、平台标记及其他服务用量核对。

本版支持 E2 Micro。ARM A1、预算创建和断网操作需其他工具。PAYG 不构成永不回收的承诺。参见 README.md 的官方来源。
