"""知识图谱构建管线 — 使用大模型从景点文本中抽取实体和关系三元组

用法:
  python scripts/build_kg.py                    # 全量抽取（75个景点）
  python scripts/build_kg.py --spot 故宫         # 抽取单个景点
  python scripts/build_kg.py --limit 5          # 只抽取前5个景点（测试用）
  python scripts/build_kg.py --dry-run          # 预览不写入文件
  python scripts/build_kg.py --output json      # 输出JSON格式

流程:
  1. 读取 scenic_data.py 中的景点文本
  2. 调用LLM进行命名实体识别（NER）— 抽取5类实体
  3. 调用LLM进行关系抽取（RE）— 生成三元组
  4. 汇总结果，输出到 data/kg_extracted.json
  5. 可选：与现有 RELATIONS 合并，生成增强版知识图谱

实体类型:
  - ScenicSpot: 景点名称
  - Dynasty: 朝代
  - Figure: 历史人物
  - Artifact: 文物/建筑
  - EventType: 历史事件

关系类型:
  - 建于朝代、位于、属于、人物相关、事件相关 等
"""
import asyncio
import json
import os
import re
import sys

# 项目根目录加入path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT_DIR, ".env"))

from data.scenic_data import SCENIC_SPOTS, CITIES, DYNASTIES, RELATIONS
from config.prompts import EXTRACTION_NER, EXTRACTION_RELATION
from config.settings import settings

try:
    from openai import AsyncOpenAI
except ImportError:
    print("缺少依赖: pip install openai")
    sys.exit(1)


# ── LLM调用 ──

def get_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY,
        timeout=60.0,
    )


async def extract_ner(client: AsyncOpenAI, text: str) -> dict:
    """调用LLM进行命名实体识别"""
    prompt = EXTRACTION_NER.format(text=text)
    resp = await client.chat.completions.create(
        model=settings.LLM_MODEL,
        messages=[
            {"role": "system", "content": "你是一个景点领域知识图谱构建专家。从文本中精确提取实体，按JSON格式输出。输出格式：{\"entities\": [{\"type\": \"ScenicSpot\", \"name\": \"...\"}, ...]}"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        max_tokens=512,
    )
    raw = resp.choices[0].message.content
    return parse_json(raw)


async def extract_relations(client: AsyncOpenAI, text: str, entities: list) -> list:
    """调用LLM进行关系抽取"""
    entities_str = json.dumps(entities, ensure_ascii=False)
    prompt = EXTRACTION_RELATION.format(text=text, entities=entities_str)
    resp = await client.chat.completions.create(
        model=settings.LLM_MODEL,
        messages=[
            {"role": "system", "content": "你是一个知识图谱关系抽取专家。从文本中提取实体间关系，按JSON格式输出。输出格式：{\"relations\": [{\"head\": \"...\", \"relation\": \"...\", \"tail\": \"...\"}, ...]}"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        max_tokens=512,
    )
    raw = resp.choices[0].message.content
    result = parse_json(raw)
    return result.get("relations", [])


def parse_json(text: str) -> dict:
    """从LLM输出中提取JSON"""
    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 尝试提取```json ... ```
    match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # 尝试提取花括号
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return {"entities": [], "relations": []}


# ── 主流程 ──

async def build_kg(spots: list, dry_run: bool = False) -> dict:
    """对景点列表执行知识抽取管线"""
    client = get_client()
    all_entities = []
    all_relations = []
    errors = []

    print(f"\n开始知识抽取，共 {len(spots)} 个景点...")
    print("=" * 60)

    for i, spot in enumerate(spots):
        name = spot["name"]
        # 拼接景点文本（desc + detail + highlights）
        text_parts = []
        if spot.get("desc"):
            text_parts.append(spot["desc"])
        if spot.get("detail"):
            text_parts.append(spot["detail"])
        if spot.get("highlights"):
            text_parts.append(f"主要看点：{spot['highlights']}")
        if spot.get("dynasty"):
            text_parts.append(f"建造朝代：{spot['dynasty']}")
        text = "\n".join(text_parts)

        # 截断到500字符避免超出上下文
        if len(text) > 500:
            text = text[:500] + "..."

        print(f"\n[{i+1}/{len(spots)}] 抽取: {name}")

        try:
            # 第1步：NER
            ner_result = await extract_ner(client, text)
            entities = ner_result.get("entities", [])
            entity_names = [e.get("name", "") for e in entities if e.get("name")]
            print(f"  实体: {len(entities)} 个 — {', '.join(entity_names[:5])}{'...' if len(entity_names) > 5 else ''}")

            # 第2步：关系抽取
            relations = await extract_relations(client, text, entities)
            print(f"  关系: {len(relations)} 条")
            for rel in relations[:3]:
                print(f"    {rel.get('head','?')} -[{rel.get('relation','?')}]-> {rel.get('tail','?')}")

            # 标注来源
            for e in entities:
                e["source"] = name
            for r in relations:
                r["source"] = name

            all_entities.extend(entities)
            all_relations.extend(relations)

        except Exception as e:
            error_msg = f"{name}: {str(e)[:100]}"
            errors.append(error_msg)
            print(f"  错误: {error_msg}")

        # 避免请求过快
        await asyncio.sleep(0.5)

    # 统计
    result = {
        "metadata": {
            "total_spots": len(spots),
            "total_entities": len(all_entities),
            "total_relations": len(all_relations),
            "errors": errors,
        },
        "entities": all_entities,
        "relations": all_relations,
        "entity_stats": {},
        "relation_stats": {},
    }

    # 按类型统计实体
    from collections import Counter
    entity_types = Counter(e.get("type", "Unknown") for e in all_entities)
    result["entity_stats"] = dict(entity_types)

    # 按关系类型统计
    relation_types = Counter(r.get("relation", "Unknown") for r in all_relations)
    result["relation_stats"] = dict(relation_types)

    # 输出统计
    print("\n" + "=" * 60)
    print("知识抽取完成！")
    print(f"  景点数: {len(spots)}")
    print(f"  抽取实体: {len(all_entities)} 个")
    for t, c in entity_types.most_common():
        print(f"    {t}: {c}")
    print(f"  抽取关系: {len(all_relations)} 条")
    for t, c in relation_types.most_common():
        print(f"    {t}: {c}")
    if errors:
        print(f"  错误: {len(errors)} 个")
        for e in errors:
            print(f"    {e}")

    # 写入文件
    if not dry_run:
        output_path = os.path.join(ROOT_DIR, "data", "kg_extracted.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n已写入: {output_path}")

        # 生成增强版RELATIONS（原始 + 抽取的）
        enhanced_relations = []
        # 原始关系
        for rel in RELATIONS:
            enhanced_relations.append({
                "head": rel["src"],
                "relation": rel["rel"],
                "tail": rel["tgt"],
                "source": "manual",
            })
        # LLM抽取的关系（去重）
        existing = {(r["head"], r["relation"], r["tail"]) for r in enhanced_relations}
        for rel in all_relations:
            key = (rel.get("head", ""), rel.get("relation", ""), rel.get("tail", ""))
            if key not in existing and all(key):
                enhanced_relations.append({
                    "head": key[0],
                    "relation": key[1],
                    "tail": key[2],
                    "source": rel.get("source", "llm"),
                })

        enhanced_path = os.path.join(ROOT_DIR, "data", "kg_enhanced.json")
        with open(enhanced_path, "w", encoding="utf-8") as f:
            json.dump(enhanced_relations, f, ensure_ascii=False, indent=2)
        print(f"增强版知识图谱: {enhanced_path} ({len(enhanced_relations)} 条关系)")

    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description="知识图谱构建管线")
    parser.add_argument("--spot", type=str, help="只抽取指定景点")
    parser.add_argument("--limit", type=int, help="限制抽取景点数量")
    parser.add_argument("--dry-run", action="store_true", help="预览不写入文件")
    parser.add_argument("--output", type=str, default="json", help="输出格式: json")
    args = parser.parse_args()

    # 选择景点
    if args.spot:
        spots = [s for s in SCENIC_SPOTS if args.spot in s["name"]]
        if not spots:
            print(f"未找到景点: {args.spot}")
            return
    elif args.limit:
        spots = SCENIC_SPOTS[:args.limit]
    else:
        spots = SCENIC_SPOTS

    asyncio.run(build_kg(spots, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
