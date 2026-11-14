# -*- coding: utf-8 -*-
"""回答评估：规则化打分（零依赖、离线可用）+ 可选 LLM 参考答案增强

四个评分维度：
  - 要点覆盖（50%）：回答覆盖了多少个参考要点
  - 回答完整度（20%）：字数 / 句数
  - 表达结构（15%）：是否使用结构词（首先/其次/最后…）
  - 语言流畅度（15%）：口头语（嗯/那个…）扣分
"""
import re

FILLERS = ["嗯", "呃", "那个", "就是说", "其实呢", "然后呢"]
STRUCT_MARKS = ["首先", "其次", "然后", "最后", "第一", "第二", "第三",
                "一方面", "另一方面", "总之", "综上", "比如", "例如", "举个例子"]

_STOP = set("的了和与及在要就都很更让被把是我们你们他们这个那个一个就是然后所以因为"
            "但是如果已经还有可以没有不是知道可能应该什么怎么这样那样现在进行出现"
            "使用通过对于以及作为其中由于因此不仅而且同时另外面试猴子你的我的")


def _clean(text):
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text or "")


def _terms(text):
    """提取对比用关键词：英文/数字词（≥2位）+ 中文二字组（去停用词）"""
    text = _clean(text)
    toks = [t.lower() for t in re.findall(r"[a-zA-Z0-9]{2,}", text)]
    han = "".join(re.findall(r"[\u4e00-\u9fff]+", text))
    for i in range(len(han) - 1):
        bg = han[i:i + 2]
        if bg not in _STOP:
            toks.append(bg)
    return toks


def _point_covered(point, answer_lower):
    for t in _terms(point):
        if len(t) >= 2 and t in answer_lower:
            return True
    return False


def rule_based_evaluate(question, ref_points, answer):
    answer = (answer or "").strip()
    ref_points = ref_points or []
    lower = answer.lower()

    # 1) 要点覆盖
    covered, missed = 0, []
    for p in ref_points:
        if _point_covered(p, lower):
            covered += 1
        else:
            missed.append(p)
    total_pts = max(1, len(ref_points))
    coverage = round(covered / total_pts * 100)

    # 2) 完整度
    n = len(answer)
    if n < 20:
        completeness = 30
    else:
        completeness = min(92, 45 + n // 3)

    # 3) 结构
    marks = sorted({m for m in STRUCT_MARKS if m in answer})
    structure = min(100, 40 + 20 * len(marks))

    # 4) 流畅度
    fillers = sum(answer.count(f) for f in FILLERS)
    fluency = max(40, 100 - 15 * fillers)

    total = round(coverage * 0.5 + completeness * 0.2 + structure * 0.15 + fluency * 0.15)

    sents = len(re.findall(r"[。！？!?]", answer)) + (1 if n else 0)
    dims = [
        {"name": "要点覆盖", "score": coverage,
         "detail": "覆盖 %d/%d 个参考要点" % (covered, len(ref_points))},
        {"name": "回答完整度", "score": completeness,
         "detail": "%d 字，约 %d 句" % (n, sents)},
        {"name": "表达结构", "score": structure,
         "detail": ("使用了 " + "、".join(marks)) if marks else "缺少结构词（首先/其次/最后）"},
        {"name": "语言流畅度", "score": fluency,
         "detail": ("口头语 %d 处，注意收一下" % fillers) if fillers else "无明显口头语"},
    ]

    suggestions = []
    if missed:
        suggestions.append("以下要点尚未覆盖，建议补充：" + "；".join(missed[:3]) +
                           ("…" if len(missed) > 3 else ""))
    if n < 60:
        suggestions.append("回答偏短（%d 字），建议按「结论 → 要点展开 → 举例/量化结果」的结构展开" % n)
    if not marks:
        suggestions.append("用「首先 / 其次 / 最后」等结构词组织表达，条理会更清晰")
    if fillers:
        suggestions.append("减少口头语（嗯 / 那个），表达会更干练")
    if re.search(r"\d+%|\d+\s*(倍|毫秒|ms|秒|万|亿)", answer):
        suggestions.append("已有量化表达，很好——面试里数字最有说服力")
    else:
        suggestions.append("尝试加入量化结果（如 提升 X%、耗时降低 Y ms），更有说服力")
    if not suggestions:
        suggestions.append("整体表现不错，继续保持；可以换一题再练")

    return {
        "total": total,
        "dims": dims,
        "suggestions": suggestions,
        "covered": covered,
        "missed": missed,
    }


def llm_reference(llm, question, ref_points):
    """有本地大模型时，生成一段口语化的完整参考答案；失败返回 None。"""
    pts = "\n".join("%d. %s" % (i + 1, p) for i, p in enumerate(ref_points))
    prompt = (
        "你是一位资深的面试教练。面试题目：%s\n"
        "答案需要覆盖以下要点：\n%s\n"
        "请写一段 150 字以内、口语化、可直接口头说出来的中文参考答案，直接输出正文，不要任何前缀。"
        % (question, pts)
    )
    try:
        out = llm.generate(prompt)
        return (out or "").strip() or None
    except Exception:
        return None
