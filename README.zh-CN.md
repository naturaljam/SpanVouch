<p align="center">
  <img src="assets/spanvouch-logo.png" width="220" alt="SpanVouch 标志">
</p>

<h1 align="center">SpanVouch</h1>

<p align="center"><strong>面向生产级 AI Agent 的证据化失败诊断基础设施。</strong></p>

<p align="center">
  <a href="https://github.com/naturaljam/SpanVouch/actions/workflows/ci.yml">CI</a> &middot;
  <a href="https://www.python.org/">Python 3.12</a> &middot;
  <a href="LICENSE">MIT License</a> &middot;
  <a href="paper/IVAD.pdf">IVAD 技术报告</a> &middot;
  <a href="https://github.com/naturaljam/SpanVouch/releases/tag/v0.7.0">v0.7.0</a>
</p>

<p align="center">
  <a href="README.md"><kbd>English</kbd></a>
  <a href="README.zh-CN.md"><kbd>简体中文</kbd></a>
</p>

SpanVouch 将 Agent 执行轨迹转换为可审计、可恢复、可复现的诊断决策。它把因果主张绑定到不可变证据，先执行确定性完整性检查，再进入可选语义验证或人工复核；每一次状态变化都会写入持久化状态和审计链。

```text
不可变轨迹 -> 净化证据 -> 结构化诊断
           -> 确定性验证 -> 分离式语义验证
           -> 有限修订 / 弃权 / 人工决策
           -> 持久状态 + 签名审计导出
```

[阅读 IVAD 技术报告](paper/IVAD.pdf) | [查看 LaTeX 源码](paper/source/) | [下载 v0.7.0](https://github.com/naturaljam/SpanVouch/releases/tag/v0.7.0)

## 为什么需要证据层

Agent 失败通常不是普通异常。真正的因果步骤可能发生在最终症状之前，跨越模型、工具和多 Agent 边界；自然语言解释即使流畅，也可能引用无关证据、遗漏反证，或复现另一个模型的错误模式。

SpanVouch 的目标不是生成“看起来合理”的解释，而是形成工程上可以追责的诊断对象：

- 每个因果主张都能定位到稳定 span 字段和规范 SHA-256 身份；
- 结构、身份、完整性、时间顺序、作用域和证据覆盖先确定性通过；
- 诊断器、语义验证器和人工复核互相分离，不能静默覆盖彼此；
- 复核状态、租约、幂等键、事件历史和 CAS 转换可跨重启恢复；
- 导出制品绑定代码、运行时、配置、事件和签名，可离线验证。

## IVAD 协议基础

[IVAD: Evidence-Constrained and Risk-Controlled Failure Diagnosis for AI Agents](paper/IVAD.pdf) 是项目技术报告，提出 Independently Verified Agent Diagnosis。IVAD 关心的是一个更接近生产运行的问题：系统何时可以接受一份带证据的诊断，何时必须修订、弃权或交给人类。

v0.7 的正式证据包位于 [evals/reports/reference/phase5-formal-deepseek-only/](evals/reports/reference/phase5-formal-deepseek-only/)，绑定 evaluated-results SHA-256 `bc09f1b134de9370a3b5209fa5e959bce01abbcdf05c8456af1f069fc4cd3088`。本次 DeepSeek-only 矩阵共安排并评估 2,148 个计划，缺失数为 0；B0-B3 完成，B4/B5 为 `policy-skipped` 且没有 Qwen 结果。H1-H5 仍为 unresolved；报告中的风险和覆盖率差异只是观测到的权衡，不是因果证明、跨模型结论或经验 target-risk certificate。

SpanVouch 是 IVAD 的工程实现。核心包括：

- **证据化决策对象**：诊断包含有限因果链、稳定证据引用、状态、来源和未解决证据；
- **分离信任通道**：确定性完整性、可选语义支持、有限修订和人工权限互不混淆；
- **风险感知选择协议**：冻结候选族、同时精确二项界、最小接受组和“无可行运行点”结果。

## 工程能力

| 层面 | 能力 |
| --- | --- |
| Trace 合同 | TraceIR、规范 JSON、稳定 selector、SHA-256 身份 |
| 证据目录 | 净化轨迹投影，排除密钥、隐藏推理、标签和 provider 私有字段 |
| 诊断 | 规则优先引擎，可选 provider adapter，有限因果链 |
| 验证 | 身份、哈希、时间、结构、作用域、冲突和覆盖检查 |
| 复核 | SQLite 持久状态、租约、幂等、不可变事件、人工决策 |
| 安全 | API key 认证、项目隔离、固定 RBAC、审计链 |
| 导出 | Ed25519 签名 checkpoint，离线可验证 audit bundle |

## 本地运行

安装 Python 3.12 和 [uv](https://docs.astral.sh/uv/)：

```bash
git clone https://github.com/naturaljam/SpanVouch.git
cd SpanVouch
uv sync --frozen --group dev
uv run spanvouch dataset generate --output .cache/readme-check --seed 20260715
uv run spanvouch evaluate diagnosis --output .cache/rules.json
uv run spanvouch evaluate review --output .cache/review-rules.json
```

从本地 checkout 离线验证发布交接：

```bash
uv run spanvouch release verify --repo-root . --expected-version 0.7.0
```

启动 API：

```bash
uv run uvicorn spanvouch.api.app:app --host 127.0.0.1 --port 8000
```

服务运行后可访问 `http://127.0.0.1:8000/docs`。

## 生产安全流程

除 `/health` 和 `/ready` 外，所有 HTTP 路由都要求：

```text
Authorization: Bearer <api-key>
```

API key 只显示一次，落库时仅保存加盐 `scrypt` 摘要、非秘密前缀、项目、角色、状态和时间戳。管理员可以创建项目、创建项目 key、轮换 key、吊销 key，并生成签名审计导出。

```bash
bootstrap="$(uv run spanvouch admin bootstrap --database .data/spanvouch.db)"
export SPANVOUCH_API_KEY="$(python -c 'import json,sys; print(json.loads(sys.argv[1])["api_key"])' "$bootstrap")"

uv run spanvouch admin project create --name production-agents
uv run spanvouch admin project list

uv run spanvouch admin key create \
  --project-id "$PROJECT_ID" \
  --roles operator,reviewer

uv run spanvouch admin key rotate --key-id "$KEY_ID"
uv run spanvouch admin key revoke --key-id "$KEY_ID"
```

设置 Ed25519 私钥路径后，可以生成审计导出；私钥只用于签名，不会进入 API 响应或导出包。

```bash
export SPANVOUCH_AUDIT_SIGNING_KEY_PATH=".secrets/audit-signing-key.pem"
export SPANVOUCH_AUDIT_EXPORT_DIR=".data/audit-exports"

uv run spanvouch admin audit export --project-id "$PROJECT_ID"
uv run spanvouch admin audit verify --bundle .data/audit-exports/"$EXPORT_ID"
```

导出包包含 `manifest.json`、`events.jsonl`、`checkpoints.json`、`public-key.pem` 和 `README.md`。离线验证会检查文件哈希、事件链连续性、checkpoint 签名、公钥绑定和终端事件哈希，不需要数据库或 provider 凭证。

## API 表面

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/ready` | 就绪检查 |
| POST | `/v1/traces` | 接收 TraceIR |
| POST | `/v1/traces/{trace_id}/diagnoses` | 诊断轨迹 |
| POST | `/v1/traces/{trace_id}/diagnosis-reviews` | 创建复核案例 |
| GET | `/v1/diagnosis-reviews/{case_id}` | 查看案例时间线 |
| POST | `/v1/diagnosis-reviews/{case_id}/resume` | 恢复可继续工作 |
| POST | `/v1/diagnosis-reviews/{case_id}/decisions` | 写入人工决策 |
| POST | `/v1/admin/projects` | 创建隔离项目 |
| POST | `/v1/admin/projects/{project_id}/api-keys` | 创建项目 API key |
| POST | `/v1/admin/api-keys/{key_id}/rotate` | 轮换 API key |
| POST | `/v1/admin/api-keys/{key_id}/revoke` | 吊销 API key |
| POST | `/v1/admin/projects/{project_id}/audit-exports` | 生成签名审计导出 |

## Docker

```bash
docker compose up --build --detach --wait api
curl --fail http://127.0.0.1:8000/health
bootstrap="$(docker compose exec -T api spanvouch admin bootstrap --database /data/spanvouch.db)"
export SPANVOUCH_API_KEY="$(python -c 'import json,sys; print(json.loads(sys.argv[1])["api_key"])' "$bootstrap")"
docker compose down
```

## 适用场景

SpanVouch 适合构建 Agent 质量平台、生产事故复核、工具调用治理、框架评估、企业审计工作流和可验证实验流水线。MIT 许可允许私有部署、平台集成、托管服务和商业支持。

## 仓库结构

```text
src/spanvouch/   contracts、trace、diagnosis、verification、review、API、CLI
schemas/v1/      公开 JSON Schema 合同
tests/           单元、合同、架构、集成和端到端测试
evals/           冻结数据集、配置和参考报告
paper/           IVAD 技术报告、可复现源码和构建说明
```

## 引用与许可

如使用 IVAD 协议、形式化方法或实验设计，请引用：

```bibtex
@techreport{liu2026ivad,
  title  = {IVAD: Evidence-Constrained and Risk-Controlled Failure Diagnosis for AI Agents},
  author = {Liu, Hanzhe},
  year   = {2026},
  url    = {https://github.com/naturaljam/SpanVouch/blob/main/paper/IVAD.pdf}
}
```

SpanVouch 软件使用 [MIT License](LICENSE)。IVAD 技术报告、图表和源码使用 [CC BY 4.0](paper/README.md)。
