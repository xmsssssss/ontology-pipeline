# 规章知识本体平台

当前版本为 1.5 阶段。平台已经具备“规章文件 -> 规则/AI 候选事实 -> 人工审核 -> OWL/SHACL -> 本体版本库 -> GraphDB 发布 -> Agent 上下文”的主流程，并提供 Web 管理界面和 HTTP API。

PostgreSQL 和 Neo4j 尚未接入。GraphDB 适配器、仓库初始化脚本、命名图发布、SPARQL 查询和 Agent 上下文接口已经完成，并已使用 GraphDB Desktop 11.5.1 完成真实写入与查询联调。

## 建设边界

目标架构：

```text
规章文件
  -> 扫描与解析
  -> 条款切分
  -> 规则 / AI / 混合抽取
  -> 人工审核
  -> OWL + SHACL + 来源记录
  -> 本体版本库（草稿、审核、发布历史）
  -> GraphDB（OWL 主库）
  -> Neo4j（可重建的查询投影）
  -> Agent 上下文服务
```

关键约束：

- GraphDB 是 OWL 主库，负责正式本体的推理和 SPARQL 查询。
- Neo4j 不是 OWL 主库，只保存由已发布 OWL 生成的可重建查询投影。
- 历史 Neo4j 数据需要经过映射、对齐和审核，再并入 GraphDB，不做双向实时同步。
- 只有审核批准的事实才能进入正式 `ontology.ttl`。
- 每条正式事实必须带有原文、条款、抽取方法和置信度来源。
- AI 和混合模式生成的事实只能进入待审核队列，不能自动批准。
- 草稿可编辑；发布到 GraphDB 的版本只读，后续修改必须创建新版本。

## 当前能力

- 扫描输入文件并计算 SHA-256，按内容去重，归档原始文件。
- 解析 TXT、Markdown、HTML、DOCX、文本型 PDF、CSV、XLSX、XLS、JSON、XML。
- 按章节和“第 X 条”编号切分非结构化规章。
- 抽取设备、部件、系统、角色、参数上下限、运行条件、动作和责任。
- 支持确定性规则、OntoGPT/SPIRES 风格的两阶段 AI 抽取，以及规则优先的混合抽取。
- 校验 AI 返回的本体类、谓词、对象类型、原文证据和主体/对象边界。
- 使用 YAML 列映射抽取结构化设备参数表。
- 生成事实审核队列，支持逐条批准或拒绝。
- 生成 OWL Turtle、SHACL、列映射、RDF 来源记录、manifest 和 checksums。
- 校验 RDF、类和属性声明、事实 ID、事实来源和 SHACL。
- 导出完整运行目录或 ZIP 制品包。
- 创建、导入、编辑、查询、导出和删除草稿本体。
- 按本体 ID 和版本保存本地版本历史、内容哈希、发布记录和来源运行。
- 通过 Web 页面上传规章文件、查看运行记录、筛选和审核事实。
- 在 Web 构建页配置 AI 服务、查看就绪状态并选择规则、AI 或混合抽取。
- 异步执行构建任务，持久化阶段、百分比、当前/总数、耗时、统计和任务日志。
- 支持取消运行、失败重试、重新构建、删除运行记录，以及服务重启后恢复未完成记录。
- Dashboard 展示活跃、完成、失败、事实、AI 事实和 Token 等任务统计。
- 把已发布 OWL 和 SHACL 写入 GraphDB 独立命名图。
- 通过本地 rdflib 或 GraphDB 执行只读 SPARQL 查询。
- 为 Agent 提供自然语言上下文接口，只检索已发布版本并返回实体、事实、条款原文、来源和置信度。
- 使用可注册的应用规则生成可解释推导，统一输出结论、前提、证明链、证据、冲突状态和置信度。

## 快速开始

环境要求：Python 3.11 或更高版本。

```powershell
cd C:\Users\cj\Desktop\CR\codex\knowledge-pipeline
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

用于联调的一键样例：

```powershell
.\.venv\Scripts\kb.exe run .\samples `
  --workspace .\workspace `
  --run-id sample_v0_4 `
  --auto-approve `
  --base-iri https://example.org/regulation/ `
  --version 0.3.0
```

`--auto-approve` 只适合样例和自动化测试。正式规章应使用下面的分阶段人工审核流程。

## Web 本体管理

启动服务：

```powershell
.\.venv\Scripts\kb.exe serve --workspace .\workspace --port 8000
```

打开：

```text
http://127.0.0.1:8000
http://127.0.0.1:8000/docs
```

页面当前提供：

- 本体资产列表、版本历史、三元组数量和发布状态。
- 新建本体、导入 Turtle/RDF/XML/JSON-LD/N-Triples、编辑 Turtle、下载 OWL。
- 本地 SPARQL 和 GraphDB SPARQL 查询。
- 上传结构化/非结构化文件并执行构建流水线。
- 配置 AI 服务地址、模型、密钥、批大小、并发、超时和重试次数，并在保存前测试模型连接。
- 查看 AI 服务状态，选择规则、AI 或混合抽取模式。
- 查看实时进度、阶段、任务统计和持久化日志，取消、重跑或删除运行记录。
- 查看事实审核队列、批准/拒绝事实、重新构建 OWL 和校验。
- 把通过校验的运行登记成本体草稿版本。
- 把草稿发布到 GraphDB、查看 GraphDB 连接状态、导出运行 ZIP。

工作区默认由 `KB_WORKSPACE` 或 `--workspace` 指定。服务启动命令会优先使用 `--workspace`。

## 运行任务与持久化

Web 构建通过后台任务执行。提交接口立即返回 `202` 和 `run_id`，页面每秒轮询进度并恢复刷新前的活动任务。每次运行至少持久化：

- `run.json`：状态、阶段、进度、当前/总数、取消标记、父运行、任务配置、时间和错误。
- `task_events.jsonl`：带时间戳、级别、阶段和进度的任务日志。
- 常规流水线制品：文档、条款、事实、OWL、SHACL、校验报告、manifest 和 checksums。

任务处于 `queued`、`running` 或 `cancelling` 时不能删除。取消会保留已经生成的阶段产物；服务重启会把未完成任务标记为 `interrupted`，之后可以查看日志并重新提交。失败或已完成的运行可以重跑，新运行通过 `parent_run_id` 保留来源关系。

## AI 抽取

AI 抽取使用 OpenAI 兼容的 `/chat/completions` 协议，可接入 OpenAI、vLLM、Ollama、LM Studio 等提供兼容接口的服务。可以直接在 Web 构建页点击“配置 AI”填写服务地址、模型、API Key、抽取策略、批大小、并发、超时和重试次数。弹窗中的“测试连接”会使用当前表单值发送一个最小请求，不会保存配置；成功时显示模型、耗时和响应摘要，失败时就地显示鉴权、地址、模型或网络错误。

非敏感配置持久化到：

```text
workspace/config/ai_settings.json
```

API Key 只保留在当前服务进程内，不写入工作区文件、运行制品或日志。状态接口只返回 `api_key_configured` 布尔值；服务重启后地址、模型和并发等配置会自动恢复，但密钥需要重新填写。前端保存的非敏感参数会覆盖同名环境变量，密钥仍可由 `AI_API_KEY` 作为启动时的初始值。

也可以通过环境变量配置：

```powershell
$env:AI_ENABLED = "true"
$env:AI_PROVIDER = "openai-compatible"
$env:AI_BASE_URL = "http://localhost:11434/v1"
$env:AI_MODEL = "your-model"
$env:AI_API_KEY = ""
$env:AI_BATCH_SIZE = "8"
$env:AI_CONCURRENCY = "2"
$env:AI_TIMEOUT = "60"
$env:AI_MAX_RETRIES = "2"
$env:AI_EXTRACTION_STRATEGY = "spires"
```

`AI_BASE_URL` 可以填写基础地址，也可以直接填写完整的 `/chat/completions` 地址。`.env.example` 仅作为变量清单，当前 CLI 不会自动加载 `.env`，需要在启动服务的终端中设置环境变量。

### 抽取策略

默认的 `spires` 策略参考 OntoGPT 的 SPIRES 抽取思路，使用项目内的轻量实现，不直接依赖 OntoGPT、LinkML Runtime 或 OAKlib：

1. 从 `schemas/regulation.linkml.yaml` 读取实体类、谓词、domain/range 和说明，动态生成提示词。
2. 第一阶段只识别设备、部件、系统、参数和责任角色，并校验实体名称、类型和逐字证据。
3. 第二阶段把已接受的实体清单与原文一起交给模型，只允许基于这些实体建立关系。
4. 关系谓词必须逐字使用本体白名单中的英文名称；提示词会完整列出白名单。语义明确的中文别名会受控归一化，例如“具有参数”和“包含参数”映射为 `hasParameter`。
5. “检查”“关联部件”“监测/熟悉”等无法无歧义对应本体属性的短语会被拒绝，并作为本体缺口暴露；系统不会为了增加抽取数量而猜测映射。
6. 关系主体和 IRI 对象必须回到第一阶段实体清单，名称和类型不一致的候选会被拒绝。
7. 所有结果继续经过 Pydantic 结构校验、证据原文校验和人工审核，不会自动批准。

模型输出兼容层参考 OntoGPT 的 grounding 思路，在进入本体前先做字段和值规范化：

- 接受 `source_id`/`sourceId`/`clause_id`、`head`/`tail`、`entity_name`、`label`、`relation` 等常见字段别名，再统一转换为内部字段。
- 对截断 JSON 会保留其中已经完整闭合的候选对象，不因最后一个不完整对象丢失全部结果。
- 缺少 `evidence_text` 时回填对应来源条款原文并记录警告；缺少 `confidence` 时使用 `0.5` 并记录警告。
- 中文实体类型会映射到 LinkML 英文类名，例如“设备”映射为 `Equipment`，“运行参数”映射为 `Parameter`。
- `has_parameter` 等 LinkML snake_case 谓词会归一化到对应的本体属性；自由文本谓词不会通过白名单校验。

`single_pass` 保留原有的一次 JSON 抽取方式，便于兼容旧模型或对比效果。Web 配置弹窗、运行详情和状态栏都会显示当前策略。

三种抽取模式：

| 模式 | 行为 | 自动批准 |
| --- | --- | --- |
| `rule` | 使用确定性规则抽取，适合稳定基线和回归测试。 | 可选 |
| `ai` | 仅使用 AI 抽取；所有批次失败时任务失败。 | 禁止 |
| `hybrid` | 先保留规则结果，再用 AI 补充，并做语义去重。 | 禁止 |

命令行示例：

```powershell
.\.venv\Scripts\kb.exe extract .\workspace\runs\regulation_20261019 `
  --extraction-mode hybrid

.\.venv\Scripts\kb.exe run .\samples `
  --workspace .\workspace `
  --run-id sample_ai_v1 `
  --extraction-mode ai `
  --ai-strategy spires
```

也可以使用 `--ai-model`、`--ai-base-url`、`--ai-batch-size`、`--ai-concurrency` 和 `--ai-strategy` 临时覆盖环境变量。API 密钥只从 `AI_API_KEY` 读取，不会写入运行制品。

AI 返回必须是严格 JSON。每条候选事实会经过以下校验：

- `source_id` 必须来自本批次输入。
- 主体类型、对象类和谓词必须在本体白名单内。
- `evidence_text` 必须逐字出现在来源文本中。
- 主体和对象必须出现在证据原文中。
- SPIRES 关系中的主体和 IRI 对象必须对齐到第一阶段已接受的实体及类型。
- 事实以 `extraction_method="ai:<model>"` 记录，并保持 `pending` 审核状态。

`extraction_report.json` 记录抽取模式、策略、规则/AI 事实数、Token 用量、耗时和警告。SPIRES 运行还会写入 `stages.entities` 与 `stages.relations`，分别记录批次成功/失败、候选、接受、拒绝和关系阶段跳过数；单轮模式写入 `stages.single_pass`。运行记录页会把这些字段整理为策略、阶段统计和 Token 摘要，不需要查看原始长字段。AI 或混合模式不能与 `--auto-approve` 同时使用，正式 OWL 只接收人工批准的事实。

## GraphDB 接入

可以使用 Docker Desktop 启动项目自带的 GraphDB，也可以直接使用 GraphDB Desktop。GraphDB 10.x 需要 Java 17 或 21。项目提供 Compose 配置和仓库初始化脚本：

```powershell
docker compose -f .\infra\graphdb\compose.yaml up -d
powershell -ExecutionPolicy Bypass -File .\scripts\init-graphdb.ps1
.\.venv\Scripts\kb.exe graphdb-status
```

默认连接参数：

```text
GraphDB: http://localhost:7200
Repository: regulation
```

可复制 `.env.example` 中的变量，或通过命令行覆盖：

```powershell
.\.venv\Scripts\kb.exe graphdb-status `
  --graphdb-url http://localhost:7200 `
  --repository regulation
```

发布运行制品到 GraphDB：

```powershell
.\.venv\Scripts\kb.exe publish .\workspace\runs\sample_v0_4
```

发布使用两个命名图：

```text
{base_iri}/graph/{ontology_id}/{version}
{base_iri}/graph/{ontology_id}/{version}/shapes
```

Web 页面发布本体版本后，本地版本状态会变为 `published`，并记录 GraphDB 地址、仓库、图 IRI、发布时间和内容哈希。已发布版本不可修改或删除。

## HTTP API

主要接口：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/dashboard` | 首页统计和最近运行 |
| `GET/POST` | `/api/ontologies` | 查询或创建本体 |
| `POST` | `/api/ontologies/import` | 导入 OWL/RDF |
| `GET` | `/api/ontologies/{id}/versions/{version}` | 读取元数据和 Turtle |
| `PUT` | `/api/ontologies/{id}/versions/{version}/turtle` | 编辑草稿 Turtle |
| `GET` | `/api/ontologies/{id}/versions/{version}/download` | 导出 Turtle |
| `POST` | `/api/ontologies/{id}/versions/{version}/publish` | 发布到 GraphDB |
| `POST` | `/api/query` | 本地或 GraphDB SPARQL 查询 |
| `GET` | `/api/inference/rules` | 查询已注册推理规则及其版本、分类和说明 |
| `GET` | `/api/inference/schema` | 获取规则定义和推理结果的机器可读 JSON Schema |
| `POST` | `/api/agent/context` | 为 Agent 检索实体、断言事实、可解释推导、条款和提示词上下文 |
| `POST` | `/api/builds` | 上传文件并异步提交构建，返回 `202` |
| `GET` | `/api/ai/status` | 查看 AI 配置与就绪状态 |
| `PUT` | `/api/ai/settings` | 更新 AI 配置并立即生效 |
| `POST` | `/api/ai/test` | 使用当前表单值测试模型连接，不保存配置 |
| `GET` | `/api/runs` | 查询运行记录 |
| `GET` | `/api/runs/{id}` | 读取运行详情、统计、校验和日志 |
| `GET` | `/api/runs/{id}/progress` | 轮询运行阶段、进度和最近日志 |
| `GET` | `/api/runs/{id}/logs` | 按数量读取持久化任务日志 |
| `POST` | `/api/runs/{id}/cancel` | 请求取消活跃运行 |
| `POST` | `/api/runs/{id}/retry` | 基于原输入和配置创建重跑任务 |
| `DELETE` | `/api/runs/{id}` | 删除非活跃运行记录 |
| `GET` | `/api/runs/{id}/facts` | 筛选和搜索事实 |
| `POST` | `/api/runs/{id}/review` | 审核事实并重建 OWL |
| `POST` | `/api/runs/{id}/register` | 登记为本地本体草稿版本 |
| `GET` | `/api/runs/{id}/export` | 导出运行 ZIP |
| `GET` | `/api/graphdb/status` | 检查 GraphDB 连接和仓库 |

完整请求模型可在 `/docs` 查看。

Agent 不需要生成 SPARQL，只需调用上下文接口：

```powershell
$body = @{
  ontology_id = "regulation"
  version = "0.2.0"
  question = "余热锅炉保证热效率是多少？"
  max_entities = 10
  max_facts = 20
  max_clauses = 8
  max_chars = 12000
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/agent/context `
  -ContentType "application/json; charset=utf-8" `
  -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

响应中的 `context_text` 可直接放入 Agent 的提示词；`entities`、`facts` 和
`clauses` 保留 IRI、条款原文、证据、来源条款、抽取方法和置信度，便于应用层展示或再次核验。

接口把两类知识明确分开：

- `facts`：图谱中由抽取结果直接断言的事实。
- `inferred_facts`：应用规则基于断言事实生成的结论，不是 GraphDB 中已写入的新三元组。

### 可解释推理规则

应用层推理使用可注册规则引擎，和 GraphDB 的 OWL/RDFS 推理分工如下：

- GraphDB 负责类层级、属性层级、domain/range 等通用语义推理。
- 应用规则负责需要向 Agent 展示业务解释的结论，例如参数范围、数据冲突和规章动作。
- 推导结果只出现在 `inferred_facts`，不会自动写回 GraphDB，也不会改变断言事实。

当前内置规则：

| 规则 ID | 结论类型 | 作用 |
| --- | --- | --- |
| `parameter_constraints` | `parameter_range` / `parameter_bound` | 组合设备、参数、上下限和单位，生成参数范围或单边边界。 |
| `parameter_conditions` | `parameter_condition` | 组合条件运算符、阈值和单位，生成运行条件。 |
| `parameter_consistency` | `parameter_bound_conflict` / `parameter_unit_conflict` | 检测下限大于上限、同一参数存在多个单位等冲突。 |
| `regulatory_requirements` | `regulatory_requirement` / `regulatory_prohibition` / `regulatory_permission` | 组合同一主体、同一来源条款下的规范动作和责任角色。 |

每条推导使用 `kb-inference/1.0` 结构。核心字段如下：

```json
{
  "schema_version": "kb-inference/1.0",
  "inferred_id": "inf-...",
  "rule": "parameter_bound",
  "rule_id": "parameter_constraints",
  "rule_version": "1.1.0",
  "rule_label": "设备参数边界组合",
  "category": "parameter",
  "status": "derived",
  "severity": "info",
  "statement": "给水泵的出口压力下限为 1.2 MPa",
  "subject": "给水泵",
  "predicate": "参数下限",
  "object": "1.2 MPa",
  "unit": "MPa",
  "conclusion": {
    "subject": "给水泵",
    "predicate": "参数下限",
    "object": "1.2 MPa",
    "statement": "给水泵的出口压力下限为 1.2 MPa"
  },
  "premises": [
    {
      "premise_index": 1,
      "fact_id": "https://example.org/.../provenance/...",
      "provenance_iri": "https://example.org/.../provenance/...",
      "role": "hasParameter",
      "statement": "给水泵 --具有参数--> 出口压力",
      "confidence": 0.93,
      "source_clause": "https://example.org/.../clause/pump-1",
      "evidence_text": "给水泵出口压力应保持在规定范围内。"
    }
  ],
  "proof_steps": [
    {
      "step": 1,
      "kind": "premise",
      "statement": "给水泵 --具有参数--> 出口压力",
      "premise_indexes": [1]
    },
    {
      "step": 2,
      "kind": "conclusion",
      "statement": "给水泵的出口压力下限为 1.2 MPa",
      "premise_indexes": [1]
    }
  ],
  "source_clauses": ["https://example.org/.../clause/pump-1"],
  "evidence_text": "给水泵出口压力应保持在规定范围内。",
  "confidence": 0.9,
  "confidence_basis": "minimum_of_premises"
}
```

`rule` 表示具体结论类型，兼容原有 Agent 读取方式；`rule_id` 表示注册的规则插件。
`status` 可为 `derived`、`conflict` 或 `warning`，`severity` 可为 `info`、`warning`
或 `error`。冲突规则不会中断其他规则，规则执行异常会记录在 `inference.errors` 中。

可以只执行指定规则：

```json
{
  "ontology_id": "regulation",
  "version": "0.2.0",
  "question": "给水泵出口压力是否有数据冲突？",
  "inference_rules": ["parameter_consistency"]
}
```

新增规则时，在 `src/kb/inference/rules/` 下实现以下接口，并注册到
`RULE_REGISTRY`：

```python
from kb.inference import FactView, InferenceResult, RuleDefinition, RULE_REGISTRY


class ExampleRule:
    definition = RuleDefinition(
        rule_id="example_rule",
        version="1.0.0",
        label="示例规则",
        description="说明该规则的业务含义和前提条件。",
        category="example",
    )

    def evaluate(self, facts: list[FactView]) -> list[InferenceResult]:
        # 只读取断言事实，返回结构化 InferenceResult。
        return []


RULE_REGISTRY.register(ExampleRule())
```

同时把该模块加入 `src/kb/inference/registry.py` 的 `register_builtin_rules()` 导入列表，
保证服务启动时执行注册。注册后可通过 `GET /api/inference/rules` 检查，并在 Agent 请求的
`inference_rules` 中启用。规则应保持只读、可重复执行，并为每条结论保留前提事实和证明链。

检索采用长词优先、问题短语加权和相对相关度阈值，过滤只由“效率”等短词命中的弱相关事实。
因此 `facts` 始终表示已断言事实，`inferred_facts` 才表示推导结论，Agent 不应把两者混为
同一种数据。

可将该接口注册为 Agent 工具：

```json
{
  "type": "function",
  "function": {
    "name": "search_regulation_context",
    "description": "检索已发布规章本体中的实体、结构化事实和条款原文。回答规章问题时先调用此工具。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "ontology_id": {"type": "string"},
        "version": {"type": "string"},
        "question": {"type": "string"},
        "max_entities": {"type": "integer", "minimum": 1, "maximum": 100},
        "max_facts": {"type": "integer", "minimum": 1, "maximum": 200},
        "max_clauses": {"type": "integer", "minimum": 1, "maximum": 100},
        "max_chars": {"type": "integer", "minimum": 500, "maximum": 50000},
        "inference_rules": {
          "type": "array",
          "items": {"type": "string"},
          "maxItems": 50
        }
      },
      "required": ["ontology_id", "version", "question"]
    }
  }
}
```

Agent 的推荐调用顺序是：先用用户问题调用工具，再将响应中的 `context_text` 加入模型上下文；最终回答引用 `facts[].source_clause` 和 `clauses[].clause_iri`。如果 `facts` 和 `clauses` 都为空，应明确说明知识库没有检索到依据。

## 分阶段流程

### 1. 扫描

```powershell
.\.venv\Scripts\kb.exe scan .\samples `
  --workspace .\workspace `
  --run-id regulation_20261019
```

输出运行目录：

```text
workspace/runs/regulation_20261019
```

阶段结果记录在 `scan.json`，原始文件归档在 `workspace/raw/`。

### 2. 解析

```powershell
.\.venv\Scripts\kb.exe parse .\workspace\runs\regulation_20261019
```

生成 `document.jsonl`、`blocks.jsonl` 和 `table_records.jsonl`。

### 3. 切分条款

```powershell
.\.venv\Scripts\kb.exe segment .\workspace\runs\regulation_20261019
```

生成 `clauses.jsonl`。没有条款编号的文本仍会保留，并标记为无条款号。

### 4. 抽取

```powershell
.\.venv\Scripts\kb.exe extract .\workspace\runs\regulation_20261019 `
  --extraction-mode rule
```

生成：

- `entity_mentions.jsonl`：实体提及。
- `facts.jsonl`：候选事实，默认状态为 `pending`。
- `review_items.jsonl`：待审核队列。
- `extraction_report.json`：抽取模式、规则/AI 计数、批次状态、Token 用量和警告。

需要 AI 时，将 `--extraction-mode` 改为 `ai` 或 `hybrid`。AI 只生成待审核候选事实，不改变人工审核、OWL 构建、校验和发布契约。

### 5. 人工审核

先查看 `facts.jsonl` 中的 `fact_id`、`subject`、`predicate`、`object`、`condition`、`evidence_text` 和 `confidence`。

批准指定事实：

```powershell
.\.venv\Scripts\kb.exe review .\workspace\runs\regulation_20261019 `
  --approve fact_xxx `
  --approve fact_yyy
```

拒绝指定事实：

```powershell
.\.venv\Scripts\kb.exe review .\workspace\runs\regulation_20261019 `
  --reject fact_zzz
```

批准全部事实：

```powershell
.\.venv\Scripts\kb.exe review .\workspace\runs\regulation_20261019 --approve-all
```

审核结果写回 `facts.jsonl`。未批准事实保留在运行目录中，但不会写入正式 OWL。

### 6. 构建 OWL

```powershell
.\.venv\Scripts\kb.exe build-owl .\workspace\runs\regulation_20261019 `
  --base-iri https://example.org/regulation/ `
  --ontology-id regulation `
  --version 0.3.0
```

生成：

- `ontology.ttl`：正式 OWL 本体和实例。
- `shapes.ttl`：SHACL 约束。
- `mapping.yaml`：本体、列映射和属性元数据。
- `provenance.jsonl`：事实级 RDF 来源索引。

### 7. 校验

```powershell
.\.venv\Scripts\kb.exe validate .\workspace\runs\regulation_20261019
```

校验通过时命令返回 0；失败时返回 2，并将结果写入 `validation_report.json`。

### 8. 定版与导出

```powershell
.\.venv\Scripts\kb.exe finalize .\workspace\runs\regulation_20261019
.\.venv\Scripts\kb.exe export `
  .\workspace\runs\regulation_20261019 `
  .\workspace\exports\regulation_20261019.zip `
  --zip
```

`finalize` 生成 `manifest.json` 和 `checksums.sha256`。导出前会检查必需制品和校验和。

## 制品说明

| 文件 | 用途 |
| --- | --- |
| `scan.json` | 输入路径、文件数量、扫描警告 |
| `source.json` | 文件清单、SHA-256、归档位置 |
| `document.jsonl` | 文档级记录 |
| `blocks.jsonl` | 段落、标题、表格行块 |
| `clauses.jsonl` | 可引用的条款文本和来源范围 |
| `table_records.jsonl` | 结构化表格行 |
| `entity_mentions.jsonl` | 实体提及与规范化业务键 |
| `facts.jsonl` | 候选事实、审核状态和来源引用 |
| `review_items.jsonl` | 人工审核队列 |
| `extraction_report.json` | 抽取模式、策略、SPIRES 分阶段统计、AI 批次和 Token |
| `ontology.ttl` | 已批准事实生成的 OWL |
| `shapes.ttl` | SHACL 数据质量约束 |
| `mapping.yaml` | 本体元数据、列映射和属性定义 |
| `provenance.jsonl` | 事实级来源记录 |
| `validation_report.json` | 校验结果、计数和 SHACL 报告 |
| `manifest.json` | 本次运行的版本、计数、状态和制品列表 |
| `checksums.sha256` | 制品完整性校验 |
| `run.json` | 运行状态、阶段、进度、任务配置和统计 |
| `task_events.jsonl` | 持久化任务事件与运行日志 |

## 工作区结构

```text
workspace/
  raw/                 # 原始文件归档
  documents/           # 预留的文档存储区
  runs/                # 每次构建的运行目录与持久化任务状态
  uploads/             # Web 上传的构建输入
  ontology/            # 本体版本库
  exports/             # 导出目录或 ZIP
  config/              # 非敏感运行配置（AI 密钥不会写入）
```

本体版本目录：

```text
workspace/ontology/<ontology_id>/<version>/
  metadata.json        # 状态、哈希、三元组数、GraphDB 发布信息
  ontology.ttl         # 当前版本 OWL
  shapes.ttl           # 可选 SHACL
  publication.json     # 可选发布记录
  imports/             # 导入的原始 OWL 文件
```

建议每次正式发布使用新的 `run-id` 和 `version`，不要覆盖历史制品。

## 质量规则

- 参数 IRI 使用“设备 + 参数”业务键，避免不同设备的同名参数被合并。
- 参数上下限、运行条件和动作均保留原文与条款来源。
- `sourceFile`、`hasClause`、`hasParameter`、`hasProvenance` 等谓词在 OWL 中显式声明。
- 每个批准事实使用 RDF 具体化记录 `subject`、`predicate`、`object`、来源条款、证据文本和置信度。
- `validate` 会检查 RDF 可解析性、谓词声明、类型声明、事实 ID 唯一性和事实来源完整性。

## 结构化文件映射

默认列映射位于：

```text
schemas/columns/equipment_parameters.yaml
```

当前识别以下业务字段：

- 设备名称
- 参数名称
- 下限
- 上限
- 单位
- 条款号
- 备注

新增表格类型时，优先扩展列映射和对应测试，不把业务列名硬编码到解析器。

## 测试

```powershell
.\.venv\Scripts\ruff.exe format src tests
.\.venv\Scripts\ruff.exe check src tests
.\.venv\Scripts\python.exe -m pytest -q
```

测试覆盖文件扫描、解析、条款切分、规则抽取、AI JSON 校验、SPIRES 两阶段实体对齐、AI 重试与 Token 统计、规则/AI 混合去重、完整样例流水线、RDF 声明、SHACL 和 checksums。

1.4 阶段同时覆盖本体创建/导入/编辑/导出、发布后只读约束、本地 SELECT/ASK、GraphDB 请求协议、Web AI 配置与密钥保护、模型连接测试、混合构建、异步进度与日志持久化、取消/重跑/删除保护、服务重启中断恢复、运行事实筛选和流水线运行登记。

## 当前限制

- 文本型 PDF 可直接解析；扫描 PDF 需要后续接入 OCR。
- `.doc` 和 `.ppt` 旧格式暂不直接解析，需要先转换。
- 规则和 AI 抽取对复杂长句、跨条款指代和多设备归属仍可能出错，正式事实必须人工审核。
- GraphDB Desktop 11.5.1 已完成真实写入、SPARQL 查询和命名图删除联调；生产部署仍需补充认证、备份和恢复策略。
- AI 抽取使用同步 HTTP 请求和进程内并发；运行状态与日志已经持久化，但多实例生产部署仍需要外部任务队列、任务租约和分布式取消机制。
- AI API Key 只保留在服务进程内；生产部署需要接入密钥管理服务，并在服务重启后重新注入密钥。
- 当前没有 Neo4j 投影、多 OWL 融合和版本差异比较。
- 本地版本库和运行目录采用文件存储；PostgreSQL 元数据目录和对象存储放到后续阶段。
- 页面图标当前从 unpkg 加载 Lucide；完全离线部署时需要改为本地静态资源。

## 后续接入顺序

1. 为 GraphDB 生产部署补充认证、备份、恢复和容量监控策略。
2. 接入 Neo4j 投影：从 GraphDB 已发布版本生成查询图，记录投影版本和重建任务。
3. 接入历史数据库：先做字段映射、实体对齐和人工审核，再生成 OWL 并入 GraphDB。
4. 建设多 OWL 融合：以共享上层类和 IRI 对齐规则合并，冲突进入审核队列，不直接覆盖。
5. 增加版本差异、变更影响分析和发布回滚记录。
