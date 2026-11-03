---
name: oracle-free-tier-skills
description: Query Oracle Cloud E2 Micro or ARM A1 2C12G/4C24G capacity, run a single-instance grabber with quota checks, report monthly public outbound traffic, and configure root monthly budget email alerts. Use for OCI 免费抢机、ARM 抢机、容量查询、流量日报、8 TB 预警或月预算告警。
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

## E2 Micro / ARM A1 抢机

按规格选择一个配置模板复制到本地 grab.local.json，填写目标账号、主区域、完整 AD、根区间公网子网和 SSH 公钥路径。E2 用 grab.example.json，ARM 2C12G 用 grab.a1-2c12g.example.json，ARM 4C24G 用 grab.a1-4c24g.example.json。只启动一个创建任务。状态目录绑定账号和规格。

```bash
.venv/bin/python scripts/grab.py --settings grab.local.json --preflight
.venv/bin/python scripts/grab.py --settings grab.local.json
```

第一条检查免费名额、总盘量、主区域和创建输入，第二条只查容量。用户明确授权自动创建一台时，才执行：

```bash
.venv/bin/python scripts/worker.py --settings grab.local.json --interval 900
```

确认已有调度器未同时运行。沿用唯一 worker、同一账号和状态目录。提交结果不明时保留 launch.json 并回查原实例，禁止清空凭证再提交。worker 在实例 RUNNING、额度阻碍、未知提交结果或连续三次错误时结束。

E2 Micro 上限检查为两台。A1 检查目标和现有 A1 的 OCPU/内存总量，并从计费 API 查询当月已使用的 OCPU 小时和 GB 小时，预留 24 小时的计量延迟余量。默认权益为 1500/9000。4C24G 需要账号确有 3000/18000 权益，并由用户核对后设置 verified_from_account=true。缺失或无法验证用量时停止。

新增盘固定 50 GB、10 VPU，主区域启动盘与数据盘累计不超过 200 GB。镜像选择官方 Ubuntu 24.04，与 E2 的 x86 或 A1 的 aarch64 架构匹配。实际费用还须结合账号合约、平台标记及其他服务用量核对。

自动断网需其他工具。PAYG 不构成永不回收的承诺。参见 README.md 的官方来源。

## 根区间小额月预算告警

将 budget.example.json 复制为 budget.local.json，填写目标 profile、收件邮箱和金额。先核对账号账单币种，再设置 billing_currency 和 currency_confirmed=true；金额 1 是该账号账单币种的 1 元，脚本不能转换币种。每个账号分别配置和执行。

```bash
.venv/bin/python scripts/budget.py --settings budget.local.json
.venv/bin/python scripts/budget.py --settings budget.local.json --apply
```

默认只读预览。用户要求设置预算后才用 --apply 创建。范围为根区间及子区间，按自然月重复，实际消费达到 100% 时向配置邮箱告警。先列出现有预算和规则，匹配则复用；存在其他根区间预算时提示用户选择既有名称，不另建重复预算，不覆盖或删除既有设置。未知写入结果停止，先回读控制台，禁止并发配置同一账号。

预算 API 在账号主区域操作，需要根区间预算管理权限。告警约每 24 小时评估，不能自动停机、断网或封顶消费，也不能保证零扣费。已有消费已达阈值时可能触发邮件。此入口支持任意 OCI profile，不包含账号凭据。
