# Oracle Free Tier Skills

把 Oracle Cloud 免费 E2 Micro 容量查询、单实例抢机和多账号出站流量日报交给 AI agent。

项目由日常管理两个 OCI 账号的脚本整理而来。面向能执行 Python 的 Codex、Claude Code、Dot 等 agent，技能入口是 [SKILL.md](SKILL.md)。

## 功能与验证状态

| 功能 | 行为 | 验证 |
| --- | --- | --- |
| 免费 E2 Micro 预检 | 核对主区域、已有实例数、启动盘和数据盘总量、公网子网及平台镜像 | 真实账号只读预检通过 |
| 容量查询 | 调用 OCI Compute Capacity Report | 真实接口返回过 OUT_OF_HOST_CAPACITY |
| 单实例抢机 | 容量可用才提交，固定 50 GB、10 VPU，保存提交意图并回查 | 模拟测试覆盖防重和未知结果，真实成功创建分支仍待库存验证 |
| 多账号流量日报 | 查询 UTC 计费月公网出站用量，支持 JSON 和 8 TB 标记 | 两个账号历史月数据与官方用量表核对一致 |
| 通知和定时运行 | worker 默认每 15 分钟检查。日报由用户已有调度器发送 | 通知发送由 agent 或外部调度器完成 |

目前抢机支持 **VM.Standard.E2.1.Micro**。ARM A1 的配额随账号与政策变化，本版没有实现 A1 自动创建。Linux 和 macOS 可运行抢机模块，其他平台需提供 fcntl 文件锁兼容层。

## 安装与凭据

```bash
git clone https://github.com/VallierDev/oracle-free-tier-skills.git
cd oracle-free-tier-skills
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

在运行主机的 `~/.oci/config` 配置 API 签名身份。多个账号使用不同 profile。配置示意如下，示例值均需替换为自己的配置。

```ini
[DEFAULT]
user=YOUR_USER_OCID
fingerprint=YOUR_KEY_FINGERPRINT
tenancy=YOUR_TENANCY_OCID
region=YOUR_HOME_REGION
key_file=/absolute/path/to/api-signing-key.pem
```

API 私钥保留在运行主机上，权限设为 `600`。SSH 公钥用于登录新建实例，与 API 签名密钥分开。先确认 IAM 授权与查询范围。抢机预检会检查根区间和可枚举的子区间，需要完整资源可见性。

## 流量日报与 8 TB 提醒

```bash
cp accounts.example.json accounts.local.json
# 填写本机配置中的 profile 名称
.venv/bin/python scripts/traffic.py --accounts accounts.local.json
.venv/bin/python scripts/traffic.py --accounts accounts.local.json --month 2026-10 --json
```

脚本汇总公网出站 SKU `B88327`、`B93455`、`B93456` 的使用量，以 `GB` 或 `GB Months` 为单位。1 TB 按 1000 GB 显示，8000 GB 触发 `reached_8tb`。其他用量、费用金额和网卡流量均不纳入该汇总。分页结果会继续读取，缺失数据、未知单位及请求错误显示为 `unknown` 并以退出码 2 结束。

当前月查询使用完整 UTC 月边界，只返回 Oracle 已汇总的实际使用量。报告里的 queried_at 是查询时间。Oracle 账单数据可能延迟最多 24 小时，8 TB 标记用于预警，无法保证在超额前实时阻断。

给 agent 的日报请求可以这样写：

> 每天早上 9 点执行此技能的 traffic.py，按账号报当月累计公网出站 TB 和 GB。数据未知时说明原因。达到 8 TB 时标注提醒，并按 profile 和 UTC 计费月去重。先核对已有排程，再创建日报任务。

## 免费 E2 Micro 抢机

```bash
cp grab.example.json grab.local.json
# 填写 expected_tenancy、主区域、完整 AD、公网子网、SSH 公钥和本次任务的 state_dir
.venv/bin/python scripts/grab.py --settings grab.local.json --preflight
.venv/bin/python scripts/grab.py --settings grab.local.json
```

预检和容量查询不创建实例。有库存后自动创建一台的运行方式如下，执行前确认用户已授权且同账号没有其他抢机调度器。

```bash
.venv/bin/python scripts/worker.py --settings grab.local.json --interval 900
```

固定检查包含主区域、E2 Micro 少于两台，以及新增 50 GB 后启动盘和数据盘合计不超过 200 GB。只选官方 Ubuntu 24.04 x86 平台镜像和根区间的公网子网。该检查覆盖指定账号主区域内能完整枚举的区间，不是对全账号所有收费服务的费用保证。账号的免费权益和合约应先在控制台确认。

`state_dir/launch.json` 在外部提交前写入 token。若网络超时或收到未明确失败的结果，后续回查同一创建意图，禁止另发创建请求。实例 RUNNING 后 worker 结束，已提交实例若失败或被终止，则结束并留待人工检查。提交前的配额和资源检查仍存在外部并发变化窗口，请保持同账号只有一个创建调度器。

worker 可用 Ctrl+C 或 SIGTERM 停止。它会等待正在执行的单次调用完成。状态和日志可能含实例标识，应保存在运行主机的私有目录中。

## 预防意外收费

- 在每个账号的根区间设置小额月预算和实际消费邮件告警。预算金额遵循账号账单币种，SGD 1 与 USD 1 不同。预算告警通常每 24 小时评估一次，作用是提醒。
- 每日读取公网出站用量，提前在 8 TB 留出处理时间。按 Oracle 当前文档，公网出站有每月 10 TB 免费额度，具体计费范围以账号与价目表为准。
- 新建资源前核对 Always Free 资格、主区域和账号累计用量。启动盘、保留的旧盘和数据盘需要合并计数。
- 需要创建门禁时，可针对区间设置资源配额。配额约束与免费计费资格应分别核对，避免把单个可用性域的配额误当成账号全局额度。

预算告警、资源配额和自动断网需要另行配置。本技能提供查询、创建预检及提醒信号。PAYG 允许继续使用免费权益，但不构成零费用、可用容量或永不回收保证。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试不创建云资源。包括流量单位与分页、免费容量门禁、预检无写入云端、单次提交、已知实例回查及未知结果防重。

## 来源

- [Oracle Always Free 资源](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [Oracle 预算](https://docs.oracle.com/en-us/iaas/Content/Billing/Concepts/budgetsoverview.htm)
- [Oracle 网络价格](https://www.oracle.com/cloud/price-list/)
- [Oracle 成本与用量报告](https://docs.oracle.com/en-us/iaas/Content/Billing/Concepts/costusagereportsoverview.htm)
- [Oracle 区间配额](https://docs.oracle.com/en-us/iaas/Content/Quotas/Concepts/resourcequotas.htm)
- [Oracle Usage API](https://docs.oracle.com/en-us/iaas/tools/python/latest/api/usage_api/client/oci.usage_api.UsageapiClient.html)
- [free-oracle-arm-skill](https://github.com/gokuscraper/free-oracle-arm-skill) 与 [oci-help](https://github.com/lemoex/oci-help) 提供了工作流参考。本仓库由自有脚本整理，未打包其执行文件或凭据。

## License

MIT
