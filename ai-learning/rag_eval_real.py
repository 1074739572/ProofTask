"""
真正的 RAG 系统评估 —— 检索 + 生成 + 评价 完整链路

检索：rag_search (hybrid: BM25 + embedding)
生成：qwen-turbo (DashScope)
评价：Faithfulness / Answer Relevancy / Context Precision

与之前自闭环的区别：
  - contexts 来自真实的 RAG 检索，不是我编造的
  - answer 由 LLM 基于检索到的 contexts 生成，不是预写的
  - 评价结果反映的是一套真实 RAG 系统的表现
"""

import json
import os
import time

import openai

from rag_eval_questions import RAG_TEST_QUESTIONS

# ── DashScope 客户端 ──
client = openai.OpenAI(
    api_key=os.environ["DASHSCOPE_API_KEY"],
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)


def llm_call(prompt: str, temperature: float = 0.0, max_tokens: int = 1500) -> str:
    resp = client.chat.completions.create(
        model="qwen-turbo",
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content


def _extract_json(raw: str) -> str:
    """从 LLM 返回中提取 JSON 字符串。"""
    if "```json" in raw:
        parts = raw.split("```json", 1)
        if len(parts) > 1:
            inner = parts[1].split("```", 1)
            return inner[0].strip()
    if "```" in raw:
        parts = raw.split("```", 1)
        if len(parts) > 1:
            inner = parts[1].split("```", 1)
            return inner[0].strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        return raw[start : end + 1]
    return raw.strip()


# ═══════════════════════════════════════════════════════════════
# Step 1: 生成（基于检索到的 contexts）
# ═══════════════════════════════════════════════════════════════

RAG_GENERATE_PROMPT = """你是一个 RAG 助手，只能根据下面提供的文档片段回答用户问题。
如果文档中没有相关信息，请如实说"文档中未找到相关信息"，不要编造。

## 检索到的文档片段：
{contexts}

## 用户问题：
{question}

## 要求：
请基于以上文档片段回答问题。用中文回答，简洁准确。
"""


def generate_answer(question: str, contexts: list[str]) -> str:
    """基于检索上下文生成 RAG 答案。"""
    ctx_text = "\n---\n".join(f"[{i + 1}] {c}" for i, c in enumerate(contexts))
    prompt = RAG_GENERATE_PROMPT.format(contexts=ctx_text, question=question)
    return llm_call(prompt, temperature=0.0, max_tokens=600)


# ═══════════════════════════════════════════════════════════════
# Step 2: 评价
# ═══════════════════════════════════════════════════════════════

FAITHFULNESS_PROMPT = """你的任务是评判 RAG 系统生成的答案是否忠实于检索到的文档。

## 上下文（检索到的文档片段）：
{contexts}

## 生成的答案：
{answer}

## 要求：
1. 将"生成的答案"拆解为若干独立的声明（claims/statements）。
2. 对每一条声明，检查它是否可以被"上下文"中的信息支持或推导。
3. 输出 JSON：
{{
  "claims": [
    {{"statement": "声明内容", "supported": true/false, "reason": "说明"}}
  ],
  "faithfulness_score": 0.0
}}

注意：
- 如果声明在上下文中完全没有依据而是答案自己编造的，标记为 false。
- 如果声明对上下文做了合理的概括或转述，标记为 true。
"""

ANSWER_RELEVANCY_PROMPT = """评判生成的答案是否直接、完整地回答了用户的问题。

## 用户问题：
{question}

## 生成的答案：
{answer}

## 要求：
从以下三个维度评判，每项 0-1 分：
1. relevant：答案是否切题
2. complete：是否覆盖核心要点
3. concise：是否紧致无冗余

输出 JSON：
{{"relevant": 0.0, "complete": 0.0, "concise": 0.0, "overall_relevancy": 0.0}}
"""

CONTEXT_PRECISION_PROMPT = """判断检索到的文档片段是否与用户问题相关。

## 用户问题：
{question}

## 文档片段：
{context}

输出 JSON：{{"relevant": true/false, "reason": "说明"}}
"""


def evaluate_faithfulness(question: str, answer: str, contexts: list[str]) -> dict:
    prompt = FAITHFULNESS_PROMPT.format(
        contexts="\n---\n".join(f"[{i+1}] {c}" for i, c in enumerate(contexts)),
        answer=answer,
    )
    try:
        return json.loads(_extract_json(llm_call(prompt)))
    except Exception as e:
        return {"claims": [], "faithfulness_score": 0.0, "error": str(e)}


def evaluate_answer_relevancy(question: str, answer: str) -> dict:
    prompt = ANSWER_RELEVANCY_PROMPT.format(question=question, answer=answer)
    try:
        return json.loads(_extract_json(llm_call(prompt)))
    except Exception as e:
        return {"overall_relevancy": 0.0, "error": str(e)}


def evaluate_context_precision(question: str, contexts: list[str]) -> dict:
    if not contexts:
        return {"precision": 0.0, "per_context": []}
    results = []
    for i, ctx in enumerate(contexts):
        prompt = CONTEXT_PRECISION_PROMPT.format(question=question, context=ctx)
        try:
            r = json.loads(_extract_json(llm_call(prompt)))
        except Exception:
            r = {"relevant": False, "reason": "parse error"}
        r["rank"] = i + 1
        results.append(r)

    relevant_count = sum(1 for r in results if r.get("relevant"))
    if relevant_count == 0:
        precision = 0.0
    else:
        weighted_sum = 0.0
        for k, r in enumerate(results, 1):
            if r.get("relevant"):
                relevant_in_top_k = sum(1 for r2 in results[:k] if r2.get("relevant"))
                weighted_sum += relevant_in_top_k / k
        precision = weighted_sum / relevant_count
    return {"precision": round(precision, 4), "per_context": results}


# ═══════════════════════════════════════════════════════════════
# 主流程：检索 → 生成 → 评价
# ═══════════════════════════════════════════════════════════════


def run_rag_evaluation(pre_retrieved: list[dict]) -> list[dict]:
    """
    pre_retrieved: [{"id", "question", "ground_truth", "contexts": [str, ...]}, ...]
    contexts 已由 rag_search 预先检索好。
    """
    results = []
    print(f"{'='*70}")
    print(f"RAG 系统评估 — 共 {len(pre_retrieved)} 条")
    print(f"检索: rag_search (hybrid) | 生成: qwen-turbo | 评判: qwen-turbo")
    print(f"{'='*70}\n")

    for i, s in enumerate(pre_retrieved):
        sid = s["id"]
        question = s["question"]
        contexts = s["contexts"]

        print(f"[{i+1}/{len(pre_retrieved)}] Q{sid}: {question[:55]}...")
        time.sleep(0.2)

        # 1. 生成答案
        answer = generate_answer(question, contexts)
        print(f"  [ANS] {answer[:100]}...")
        time.sleep(0.3)

        # 2. 评价
        faith = evaluate_faithfulness(question, answer, contexts)
        time.sleep(0.3)
        relevancy = evaluate_answer_relevancy(question, answer)
        time.sleep(0.3)
        ctx_precision = evaluate_context_precision(question, contexts)

        results.append(
            {
                "id": sid,
                "question": question,
                "answer": answer,
                "ground_truth": s.get("ground_truth", ""),
                "contexts": contexts,
                "faithfulness": faith,
                "answer_relevancy": relevancy,
                "context_precision": ctx_precision,
            }
        )

        fs = faith.get("faithfulness_score", 0)
        ar = relevancy.get("overall_relevancy", 0)
        cp = ctx_precision.get("precision", 0)
        print(f"  Faithfulness: {fs:.2f}  Relevancy: {ar:.2f}  Context Precision: {cp:.2f}")

        # 幻觉告警
        for claim in faith.get("claims", []):
            if not claim.get("supported"):
                print(f"  [!] 幻觉: {claim['statement'][:80]}")

        # 无关检索告警
        for ctx_r in ctx_precision.get("per_context", []):
            if not ctx_r.get("relevant"):
                print(f"  [x] 无关检索: [{ctx_r['rank']}] {ctx_r.get('reason', '')[:60]}")

        print()

    # 汇总
    avg_f = sum(r["faithfulness"].get("faithfulness_score", 0) for r in results) / len(results)
    avg_r = (
        sum(r["answer_relevancy"].get("overall_relevancy", 0) for r in results) / len(results)
    )
    avg_p = sum(r["context_precision"].get("precision", 0) for r in results) / len(results)

    print(f"{'='*70}")
    print(f"评估汇总（{len(results)} 条）")
    print(f"{'='*70}")
    print(f"  Faithfulness (忠实度)       : {avg_f:.2f}")
    print(f"  Answer Relevancy (答案相关性) : {avg_r:.2f}")
    print(f"  Context Precision (上下文精度) : {avg_p:.2f}")
    print(f"{'='*70}")

    return results


if __name__ == "__main__":
    # contexts 由外部（harness rag_search）预先检索好，这里从文件读入
    script_dir = os.path.dirname(__file__)
    input_path = os.path.join(script_dir, "retrieved_contexts.json")

    if not os.path.exists(input_path):
        print(f"请先生成检索结果文件: {input_path}")
        print("格式: [{'id': 1, 'question': '...', 'contexts': ['ctx1', 'ctx2', ...]}, ...]")
    else:
        with open(input_path, "r", encoding="utf-8") as f:
            pre_retrieved = json.load(f)
        results = run_rag_evaluation(pre_retrieved)
        output_path = os.path.join(script_dir, "rag_eval_real_results.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n完整结果已保存: {output_path}")
