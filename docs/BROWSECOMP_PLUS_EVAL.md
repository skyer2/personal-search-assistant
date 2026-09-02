# BrowseComp-Plus 真实数据评测

本适配把 Agent 的 `internet_search` 后端切换为 BrowseComp-Plus 官方固定语料，
因此不会混入实时网页变化。项目内计算 Retrieval 指标、成本、延迟和引用指标；
最终 Answer Accuracy 仍使用官方 Qwen3-32B judge，避免把字符串包含率冒充正式分数。

## 1. 安装与数据准备

```bash
# 项目依赖
uv sync

# Benchmark 下载脚本额外依赖
uv pip install datasets

# 获取官方工具（也可下载 zip）
git clone https://github.com/texttron/BrowseComp-Plus.git external/BrowseComp-Plus
git -C external/BrowseComp-Plus checkout 046949032b0328319cc9a02663a759ec601d9402

# 按官方方式在本机解密；不要提交产物
uv run python external/BrowseComp-Plus/scripts_build_index/decrypt_dataset.py \
  --output .benchmark_data/browsecomp_plus/decrypted.jsonl

# 固定抽取 50 条，并流式下载 100,195 篇 corpus 建 SQLite FTS5 索引
uv run python tests/eval/prepare_browsecomp_plus.py \
  --decrypted .benchmark_data/browsecomp_plus/decrypted.jsonl \
  --limit 50
```

默认产物：

- `.benchmark_data/browsecomp_plus/manifest.json`：固定 seed、query IDs、数据集名称；
- `queries.jsonl`：本地明文 query/answer；
- `qrels_gold.txt` / `qrels_evidence.txt`：两套相关性标注；
- `corpus.sqlite3`：完整固定语料的 FTS5 索引。

`.benchmark_data/` 已被 Git 忽略。官方数据约 3GB，首次下载和建索引需要较长时间。

## 2. 先测 Retriever

```bash
# 快速 smoke test
uv run python tests/eval/run_browsecomp_plus.py \
  --mode retrieval --limit 3 --top-k 100

# 固定 50 条正式 retrieval run
uv run python tests/eval/run_browsecomp_plus.py \
  --mode retrieval --top-k 1000
```

报告位于 `tests/eval/results/browsecomp_plus/retrieval_summary.json`，包含：

- Gold / Evidence Recall@5、Recall@100、Recall@1000；
- Gold / Evidence nDCG@10；
- 标准 TREC run 文件 `retrieval.trec`。

本项目 SQLite FTS5 是 Windows/CI 友好的自定义 Retriever，不等于官方
Pyserini/Lucene BM25 排名复现。TREC 指标实现可以用官方命令交叉验证，但排名分数
不应冒充论文 BM25 baseline。

## 3. 跑 Agent Live Eval

先在 `.env` 配置 LLM。Benchmark 模式不需要 `TAVILY_API_KEY`：

```bash
uv run python tests/eval/run_browsecomp_plus.py --mode live --limit 3
uv run python tests/eval/run_browsecomp_plus.py --mode live
```

运行器会在导入 Agent/MCP 之前设置：

```text
BROWSECOMP_PLUS_ENABLED=true
BROWSECOMP_PLUS_CORPUS_DB=.benchmark_data/browsecomp_plus/corpus.sqlite3
BROWSECOMP_PLUS_RETRIEVAL_LOG=tests/eval/results/browsecomp_plus/retrieval_log.jsonl
BROWSECOMP_PLUS_TOP_K=5
```

`app/tools/tavily_core.py` 检测到该模式后，强制走本地 SQLite FTS5，
并保持 Tavily 兼容返回结构，所以主 Agent、network_search_agent 和 MCP Server
无需复制第二套工具实现。

Live 报告额外包含：

- Agent 全轨迹去重后的 `retrieved_docids`；
- 每题搜索次数、端到端延迟；
- phase 级真实 prompt/completion token、成本和 `missing_usage_calls`；
- Citation Coverage Rate、启发式 Hallucination Rate；
- 官方 docid 口径的 Citation Precision/Recall/Cited Ratio；
- `offline_surrogate` 下的 normalized EM、token F1、parse rate 和字符串召回
  （都不是官方 Accuracy）。

检索日志只保存 query SHA-256 和 docid，不写入受保护的 query 明文。Live prompt
要求 Agent 使用真实语料编号 `[docid]`；评测时只接受本次轨迹实际检索到的 docid，
因此 Harness 自动生成的 `[1]`、`[2]` 顺序号不会被误算为官方引用。

## 4. 官方 Answer Accuracy

Live runner 在 `official_runs/` 为每题生成官方格式 JSON：

```bash
cd external/BrowseComp-Plus
uv run python scripts_evaluation/evaluate_run.py \
  --input_dir ../../tests/eval/results/browsecomp_plus/official_runs
```

该步骤使用官方 Qwen3-32B judge，硬件和模型依赖以官方仓库为准。简历只填写这一步
实际跑出的 `Accuracy (%)`，不要把本项目的 `answer_string_recall` 写成 Accuracy。

## 5. 验证 Agent 是否真的有效

至少做四组对照，每组使用同一 `manifest.json`、同一模型、同一语料：

1. `Retrieval-only`：确认 Recall/nDCG，不把 Retriever 差错归咎于 Agent；
2. `Vanilla Agent`：`direct` 单 Agent + tools，无 Brief/Plan/Progress；
3. `Harness-NoReplan`：完整控制面但 `max_replan=0`；
4. `Full Harness`：Progress GAP → Replan。

可选：`Harness-NoCompression`。Parallel/Sequential 优先级低于 Replan ablation。

```bash
uv run python tests/eval/run_eval.py --live --variant vanilla
uv run python tests/eval/run_eval.py --live --variant no_replan
uv run python tests/eval/run_eval.py --live --variant full
```

每次保存代码 commit、配置快照、manifest、summary 和官方 judge 输出。效果结论应同时看：

- 质量：官方 Accuracy、Gold/Evidence Recall、nDCG@10、引用覆盖；
- 效率：P50/P95 延迟、总 token、单题成本、工具调用次数；
- 稳定性：成功率、超时率、`missing_usage_calls == 0`；
- 归因：失败属于检索未命中、Agent 未使用证据、生成错误还是运行时错误。

## 6. 离线回归测试

以下测试不下载官方数据、也不调用 LLM：

```bash
uv run pytest \
  tests/test_browsecomp_plus.py \
  tests/test_harness_phase17_usage.py \
  tests/test_harness_orchestration.py -q
```

微型语料测试覆盖 FTS5 检索、Recall/nDCG；Usage 测试覆盖直接 LLM 调用的 callback
注入；并行状态测试覆盖增量合并且不会覆盖父 `LoopState.phase`。
