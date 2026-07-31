#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件级职责：芮淇讲《资治通鉴》文稿批量导入 Elasticsearch 脚本

核心功能：
    1. 遍历纠错后的文稿目录，把"同一集分成多篇"的文件自动分组合并
    2. 从文件名提取元数据（集数、标题、分类、标签）
    3. 复用 LocalRAG-CS 现有的分块器 / Embedding 客户端 / ES 客户端，
       生成与"周纪1-5网页导入"完全一致的 ES 文档结构，绝不搞两套
    4. 支持 --dry-run / --start / --end / --force / --output-report
    5. 断点续传：ES 中已存在的 doc_id 默认跳过；失败重试；单集失败不影响整体

架构位置：
    属于 LocalRAG-CS 的离线数据导入工具，放在 scripts/ 下，
    通过 sys.path 复用项目 core / ingestion 里已测试通过的组件。

命名规律说明（依据 output/logs 里的实际文件名总结）：
    - 《资治通鉴·大秦纪》0099集｜历史第一丑男逆袭做秦相-壹.txt
    - 资治通鉴783丨人性潜规则：你的价值决定一切.txt
    - 资治通鉴PLUS丨芮淇解说《鬼吹灯之精绝古国》的前世今生.txt
    - 资治通鉴丨周纪一 合集版.txt（周纪1-5，已导入，跳过）
    - 黄河古事 49.txt（番外系列）
    多篇标记：-壹/-贰/-叁 或 -1/-2/-3 或 （上）/（下）
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---- 复用 LocalRAG-CS 项目组件（core / ingestion / config）----
# 脚本在 scripts/ 下，项目根目录是上一级
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402  项目全局配置，ES/索引/维度都在这里，禁止硬编码


# ============================================================
# 一、可配置参数（全部可被环境变量覆盖，禁止散落硬编码）
# ============================================================

# 文稿输入目录：默认取纠错产物目录，可用环境变量覆盖
DEFAULT_INPUT_DIR = os.getenv(
    "HISTORY_INPUT_DIR",
    os.path.expanduser("~/ai-workflow/history-learning-assistant/output/corrected"),
)
# ES 索引名：直接用项目配置，保证和周纪1-5同一个索引
ES_INDEX_NAME = config.ES_INDEX_NAME
# 数据来源标记（写进 source 字段，方便后续按来源过滤）
SOURCE_NAME = os.getenv("HISTORY_SOURCE", "芮淇讲资治通鉴")
# doc_id 前缀，保证与其它业务数据不冲突，且断点续传可稳定复算
DOC_ID_PREFIX = os.getenv("HISTORY_DOC_PREFIX", "ruiqi")
# 每集之间的导入间隔（秒），控制速率避免 ES 过载
IMPORT_DELAY = float(os.getenv("HISTORY_IMPORT_DELAY", "0.2"))
# 单集 ES 写入最大重试次数
MAX_RETRY = int(os.getenv("HISTORY_MAX_RETRY", "3"))

# 分类关键词映射：文件名里出现左边关键词 → 归到右边分类
CATEGORY_RULES = [
    ("大秦纪", "秦纪"),
    ("秦纪", "秦纪"),
    ("汉纪", "汉纪"),
    ("魏纪", "魏纪"),
    ("魏晋", "魏纪"),
    ("晋纪", "晋纪"),
    ("周纪", "周纪"),
    ("PLUS", "Plus"),
    ("番外", "番外"),
    ("特集", "番外"),
    ("黄河古事", "番外"),
]
# 需要跳过的关键词（周纪1-5合集版已经通过网页导入，避免重复）
SKIP_KEYWORDS = ["合集版"]

# 中文数字 → 阿拉伯数字，用于识别 -壹/-贰 这种分篇标记和"周纪一"
CN_NUM = {
    "零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "贰": 2, "两": 2,
    "三": 3, "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5,
    "六": 6, "陆": 6, "七": 7, "柒": 7, "八": 8, "捌": 8,
    "九": 9, "玖": 9, "十": 10,
}


# ============================================================
# 二、文件名解析：提取集数 / 标题 / 分类 / 分篇序号
# ============================================================

def _cn_to_int(text):
    """把简单中文数字（壹/贰/叁/十/二十一 等）转成整数

    入参：text 中文数字串
    返回：int；无法解析返回 None
    仅覆盖讲稿分篇会用到的 1-99 范围，够用即止（ponytail: 不做完整中文数字引擎）
    """
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total = 0
    if "十" in text:
        # 处理 十/十一/二十/二十三
        left, _, right = text.partition("十")
        tens = CN_NUM.get(left, 1) if left else 1
        ones = CN_NUM.get(right, 0) if right else 0
        total = tens * 10 + ones
        return total
    val = CN_NUM.get(text)
    return val


def parse_filename(filename):
    """从文件名解析元数据

    入参：filename 不含路径的文件名（可带 .txt 后缀）
    返回：dict {
        episode_num: str|None  规整后的集数（数字集补零到4位；无数字集为 None）
        raw_episode: str|None  原始集数串
        title: str             清洗后的标题
        category: str          分类（秦纪/汉纪/魏纪/周纪/Plus/番外/正史）
        part: int|None         分篇序号（壹→1，1→1）
        group_key: str         同集分篇归并用的分组键
        should_skip: bool      是否命中跳过规则（如周纪合集版）
    }
    异常处理：任何异常都不抛出，尽量给出可用默认值，保证批量不中断
    """
    name = filename[:-4] if filename.lower().endswith(".txt") else filename

    # 1) 分类判定：按 CATEGORY_RULES 顺序命中第一个
    category = "正史"
    for kw, cat in CATEGORY_RULES:
        if kw in name:
            category = cat
            break

    # 2) 跳过判定：命中跳过关键词（如周纪合集版）
    should_skip = any(kw in name for kw in SKIP_KEYWORDS)

    # 3) 集数提取：优先"数字+集"（0099集），其次"资治通鉴+数字"（资治通鉴783）
    raw_episode = None
    m = re.search(r"(\d{2,4})\s*集", name)
    if m:
        raw_episode = m.group(1)
    else:
        m2 = re.search(r"资治通鉴\s*(\d{2,4})", name)
        if m2:
            raw_episode = m2.group(1)
        else:
            # 番外类"黄河古事 49"这种：取末尾独立数字
            m3 = re.search(r"(\d{1,4})\s*$", name.strip())
            if m3:
                raw_episode = m3.group(1)

    episode_num = raw_episode.zfill(4) if raw_episode else None

    # 4) 分篇序号：结尾的 -壹/-1/（上）/（下）等
    part = None
    part_patterns = [
        r"[-_]([壹贰叁肆伍陆一二三四五六]|[0-9]{1,2})\s*$",  # -壹 / -2
        r"[（(]([上中下])[）)]\s*$",                          # （上）/（下）
        r"第\s*([0-9一二三四五六]{1,2})\s*[篇部分]\s*$",       # 第2篇
    ]
    order_map = {"上": 1, "中": 2, "下": 3}
    for pat in part_patterns:
        pm = re.search(pat, name)
        if pm:
            token = pm.group(1)
            if token in order_map:
                part = order_map[token]
            else:
                part = _cn_to_int(token)
            break

    # 5) 标题清洗：去掉《资治通鉴·xxx》前缀、"NNN集｜"、分篇尾巴，得到干净标题
    title = name
    # 标题清洗分多步，顺序敏感：先去整块书名，再去集号前缀，最后去残留分类标
    # 1) 去掉开头整块《...》书名（贪婪到第一个》，如《资治通鉴·大秦纪》）
    title = re.sub(r"^《[^》]*》", "", title)
    # 2) 去掉"资治通鉴783丨"/"资治通鉴PLUS丨"这种带集号或PLUS的前缀
    title = re.sub(r"^\s*资治通鉴\s*(?:\d{2,4}|PLUS|plus)?\s*[丨|｜]?\s*", "", title)
    # 3) 去掉开头残留的"NNN集｜"
    title = re.sub(r"^\s*\d{2,4}\s*集\s*[｜|丨]?\s*", "", title)
    # 4) 去掉开头残留的“XX纪丨/XXX丨”分类标（第一个分隔符前的短前缀，不含完整标题）
    title = re.sub(r"^\s*[^｜|丨]{0,8}?纪\s*[丨|｜]\s*", "", title)
    # 5) 去掉开头残留分隔符
    title = re.sub(r"^\s*[｜|丨·]\s*", "", title)
    # 去掉结尾分篇标记
    title = re.sub(r"[-_]([壹贰叁肆伍陆一二三四五六]|[0-9]{1,2})\s*$", "", title)
    title = re.sub(r"[（(]([上中下])[）)]\s*$", "", title)
    title = title.strip() or name  # 清洗后为空则回退到原名

    # 6) 分组键：有集数用"分类+集数"，无集数用清洗后标题
    if episode_num:
        group_key = f"{category}_{episode_num}"
    else:
        group_key = f"{category}_{title}"

    return {
        "episode_num": episode_num,
        "raw_episode": raw_episode,
        "title": title,
        "category": category,
        "part": part,
        "group_key": group_key,
        "should_skip": should_skip,
    }


def build_tags(meta):
    """根据元数据生成标签列表

    入参：meta parse_filename 的返回
    返回：list[str] 去重后的标签
    """
    tags = []
    if meta["category"] and meta["category"] not in tags:
        tags.append(meta["category"])
    tags.append(SOURCE_NAME)
    return tags


# ============================================================
# 三、扫描 + 分组合并
# ============================================================

def scan_and_group(input_dir):
    """扫描目录，按集分组合并同一集的多篇文件

    入参：input_dir 文稿目录
    返回：(groups, skipped) —— groups 为按分组键排序的集合列表；skipped 为被跳过的文件名列表
        每个 group: {
            group_key, category, episode_num, title, tags,
            files: [已按分篇序号排序的绝对路径],
        }
    异常处理：目录不存在直接抛 FileNotFoundError，交上层处理
    """
    base = Path(input_dir)
    if not base.exists():
        raise FileNotFoundError(f"文稿目录不存在: {input_dir}")

    groups = {}
    skipped = []
    for entry in sorted(base.iterdir()):
        if not entry.is_file() or not entry.name.lower().endswith(".txt"):
            continue
        meta = parse_filename(entry.name)
        if meta["should_skip"]:
            skipped.append(entry.name)
            continue

        gk = meta["group_key"]
        if gk not in groups:
            groups[gk] = {
                "group_key": gk,
                "category": meta["category"],
                "episode_num": meta["episode_num"],
                "title": meta["title"],
                "tags": build_tags(meta),
                "_parts": [],  # 临时：(part序号, 路径)
            }
        part_order = meta["part"] if meta["part"] is not None else 1
        groups[gk]["_parts"].append((part_order, str(entry)))

    # 分篇排序，落成最终 files 列表
    result = []
    for gk in sorted(groups.keys()):
        g = groups[gk]
        g["_parts"].sort(key=lambda x: (x[0], x[1]))
        g["files"] = [p for _, p in g["_parts"]]
        del g["_parts"]
        result.append(g)
    return result, skipped


def merge_group_content(group):
    """读取并合并一个分组内所有分篇文件内容

    入参：group scan_and_group 产出的单个分组
    返回：str 合并后的完整文本
    异常处理：单个分篇读失败则跳过该篇并继续，不影响整集
    """
    parts = []
    for fp in group["files"]:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                txt = f.read().strip()
            if txt:
                parts.append(txt)
        except Exception as e:  # noqa: BLE001  单篇失败降级，不中断整集
            print(f"    ⚠️ 分篇读取失败，已跳过: {fp} ({e})", flush=True)
    return "\n\n".join(parts)


# ============================================================
# 四、集数范围过滤
# ============================================================

def in_range(episode_num, start, end):
    """判断集数是否落在 [start, end] 范围内

    入参：episode_num 4位补零字符串或 None；start/end int 或 None
    返回：bool
    说明：无集数（番外类）在指定了范围时不参与，除非未限制范围
    """
    if start is None and end is None:
        return True
    if episode_num is None:
        return False
    num = int(episode_num)
    if start is not None and num < start:
        return False
    if end is not None and num > end:
        return False
    return True


# ============================================================
# 五、ES 文档构造 + 写入（复用项目 pipeline 的文档结构）
# ============================================================

def build_documents(group, content, chunks, embeddings):
    """构造与 LocalRAG-CS pipeline 完全一致的 ES 文档列表

    入参：
        group    分组元数据
        content  合并后的完整文本（此处仅用于兜底，主体用 chunks）
        chunks   分块后的文本片段列表
        embeddings 与 chunks 一一对应的向量（None 表示该块向量失败，跳过）
    返回：list[dict] ES 文档
    结构与 ingestion/pipeline.py._prepare_documents 对齐：
        doc_id/question/answer/embedding/category/source_file/chunk_index/total_chunks/create_time
    额外补充历史项目专属字段：source/episode_num/tags（ES 动态映射，兼容不冲突）
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    total = len(chunks)
    # doc_id 基名：优先用分类+集数，保证断点续传时可稳定复算
    if group["episode_num"]:
        base_id = f"{DOC_ID_PREFIX}_{group['category']}_{group['episode_num']}"
    else:
        safe_title = re.sub(r"[^\w\u4e00-\u9fff]+", "_", group["title"])[:40]
        base_id = f"{DOC_ID_PREFIX}_{group['category']}_{safe_title}"

    docs = []
    for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        if emb is None:
            print(f"    ⚠️ 跳过块 {i}：向量生成失败", flush=True)
            continue
        docs.append({
            "doc_id": f"{base_id}_{i:04d}",
            "question": group["title"],   # 标题作为 question，与周纪导入一致
            "answer": chunk,               # 分块内容作为 answer
            "embedding": emb,
            "category": group["category"],
            "source_file": Path(group["files"][0]).name if group["files"] else "",
            "chunk_index": i,
            "total_chunks": total,
            "create_time": now_iso,
            # ---- 历史项目补充字段（不破坏原结构，ES 动态映射）----
            "source": SOURCE_NAME,
            "episode_num": group["episode_num"] or "",
            "tags": group["tags"],
        })
    return docs


def episode_doc_id_base(group):
    """复算某集的 doc_id 基名（断点续传检查用），逻辑与 build_documents 保持一致"""
    if group["episode_num"]:
        return f"{DOC_ID_PREFIX}_{group['category']}_{group['episode_num']}"
    safe_title = re.sub(r"[^\w\u4e00-\u9fff]+", "_", group["title"])[:40]
    return f"{DOC_ID_PREFIX}_{group['category']}_{safe_title}"


def write_documents(es_client, docs):
    """把一集的文档写入 ES，带重试

    入参：es_client 项目 ElasticsearchClient；docs 文档列表
    返回：成功写入条数
    异常处理：单文档失败重试 MAX_RETRY 次，最终失败记 0，不抛出
    """
    ok = 0
    for doc in docs:
        for attempt in range(1, MAX_RETRY + 1):
            try:
                res = es_client.insert_document(ES_INDEX_NAME, doc, doc_id=doc["doc_id"])
                if res:
                    ok += 1
                break
            except Exception as e:  # noqa: BLE001  写入降级重试
                if attempt >= MAX_RETRY:
                    print(f"    ❌ 文档写入最终失败 {doc['doc_id']}: {e}", flush=True)
                else:
                    time.sleep(1.0 * attempt)
    return ok


# ============================================================
# 六、主流程
# ============================================================

def main():
    ap = argparse.ArgumentParser(description="芮淇讲资治通鉴文稿批量导入ES")
    ap.add_argument("--input-dir", default=DEFAULT_INPUT_DIR, help="文稿目录")
    ap.add_argument("--dry-run", action="store_true", help="只统计不实际导入")
    ap.add_argument("--start", type=int, default=None, help="起始集数（含）")
    ap.add_argument("--end", type=int, default=None, help="结束集数（含）")
    ap.add_argument("--force", action="store_true", help="覆盖已存在文档（关闭断点续传跳过）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少集（0=全部）")
    ap.add_argument("--chunk-size", type=int, default=config.DEFAULT_CHUNK_SIZE, help="分块大小")
    ap.add_argument("--chunk-overlap", type=int, default=config.DEFAULT_CHUNK_OVERLAP, help="分块重叠")
    ap.add_argument("--output-report", default=None, help="导入报告输出路径（JSON）")
    args = ap.parse_args()

    print("=" * 60, flush=True)
    print("芮淇讲资治通鉴 文稿批量导入ES", flush=True)
    print(f"输入目录: {args.input_dir}", flush=True)
    print(f"ES索引:  {ES_INDEX_NAME}", flush=True)
    print(f"模式:    {'DRY-RUN(不导入)' if args.dry_run else '实际导入'}", flush=True)
    print("=" * 60, flush=True)

    # 1) 扫描 + 分组
    groups, skipped = scan_and_group(args.input_dir)

    # 2) 集数范围过滤
    selected = [g for g in groups if in_range(g["episode_num"], args.start, args.end)]

    # 分类统计
    cat_count = {}
    for g in selected:
        cat_count[g["category"]] = cat_count.get(g["category"], 0) + 1

    print(f"扫描到分组(集): {len(groups)}  跳过(合集版等): {len(skipped)}", flush=True)
    print(f"范围过滤后待处理: {len(selected)}", flush=True)
    print(f"分类分布: {cat_count}", flush=True)

    if args.limit > 0:
        selected = selected[: args.limit]
        print(f"限制仅处理前 {args.limit} 集", flush=True)

    report = {
        "timestamp": datetime.now().isoformat(),
        "input_dir": args.input_dir,
        "es_index": ES_INDEX_NAME,
        "dry_run": args.dry_run,
        "scanned_groups": len(groups),
        "skipped_files": skipped,
        "category_count": cat_count,
        "success": [],
        "failed": [],
        "skipped_exist": [],
    }

    # 3) DRY-RUN：只打印分组明细
    if args.dry_run:
        print("\n=== DRY-RUN 分组明细 ===", flush=True)
        for g in selected:
            files_disp = [Path(p).name for p in g["files"]]
            print(f"  [{g['category']}] 集{g['episode_num'] or '-'} 《{g['title']}》"
                  f" 分篇{len(files_disp)}: {files_disp}", flush=True)
            report["success"].append({
                "group_key": g["group_key"],
                "category": g["category"],
                "episode_num": g["episode_num"],
                "title": g["title"],
                "parts": len(g["files"]),
            })
        _save_report(args.output_report, report)
        print(f"\nDRY-RUN 完成：待导入 {len(selected)} 集（未写ES）", flush=True)
        return

    # 4) 实际导入：此时才初始化重型客户端（分块/向量/ES）
    from core.embedding import EmbeddingClient
    from core.es_client import ElasticsearchClient
    from ingestion.chunker import ChunkConfig, SmartChunker

    print("\n初始化 ES / Embedding 客户端...", flush=True)
    es_client = ElasticsearchClient()
    embedding_client = EmbeddingClient()
    chunker = SmartChunker(ChunkConfig(chunk_size=args.chunk_size, overlap_size=args.chunk_overlap))

    # 确保索引存在（复用 config 里的 mapping，与周纪1-5同结构）
    if not es_client.index_exists(ES_INDEX_NAME):
        print(f"索引不存在，创建: {ES_INDEX_NAME}", flush=True)
        es_client.create_index(ES_INDEX_NAME, mappings=config.VECTOR_INDEX_MAPPING.get("mappings"))

    ok_cnt, fail_cnt, skip_exist_cnt = 0, 0, 0
    t_start = time.time()

    for idx, g in enumerate(selected, 1):
        tag = f"集{g['episode_num'] or '-'} 《{g['title']}》"
        print(f"\n[{idx}/{len(selected)}] ▶️ [{g['category']}] {tag}", flush=True)

        # 4.1 断点续传：首块 doc_id 已存在则跳过（除非 --force）
        first_id = f"{episode_doc_id_base(g)}_0000"
        if not args.force:
            try:
                exist = es_client.get_document(ES_INDEX_NAME, first_id)
            except Exception:  # noqa: BLE001  查询失败当作不存在，继续导入
                exist = None
            if exist:
                skip_exist_cnt += 1
                report["skipped_exist"].append(g["group_key"])
                print("  ⏭️ 已存在，跳过（--force 可覆盖）", flush=True)
                continue

        try:
            # 4.2 合并内容
            content = merge_group_content(g)
            if not content:
                raise RuntimeError("合并后内容为空")

            # 4.3 分块（带标题前缀，与 pipeline 一致）
            chunks = chunker.chunk_text_with_title(content, title=g["title"])
            if not chunks:
                raise RuntimeError("分块结果为空")

            # 4.4 向量化
            embeddings = embedding_client.encode_batch(chunks)
            if len(embeddings) != len(chunks):
                # 数量不齐时补 None，交由 build_documents 跳过
                embeddings = list(embeddings) + [None] * (len(chunks) - len(embeddings))

            # 4.5 构造文档 + 写 ES
            docs = build_documents(g, content, chunks, embeddings)
            written = write_documents(es_client, docs)

            if written > 0:
                ok_cnt += 1
                report["success"].append({
                    "group_key": g["group_key"],
                    "category": g["category"],
                    "episode_num": g["episode_num"],
                    "title": g["title"],
                    "chunks": len(chunks),
                    "written": written,
                })
                print(f"  ✅ 完成：{len(chunks)}块 → 写入{written}条", flush=True)
            else:
                raise RuntimeError("所有块写入ES失败")

        except Exception as e:  # noqa: BLE001  单集失败不影响整体
            fail_cnt += 1
            report["failed"].append({"group_key": g["group_key"], "error": str(e)})
            print(f"  ❌ 失败: {e}", flush=True)

        time.sleep(IMPORT_DELAY)

    elapsed = (time.time() - t_start) / 60
    print("\n" + "=" * 60, flush=True)
    print(f"导入结束: 成功={ok_cnt} 失败={fail_cnt} 已存在跳过={skip_exist_cnt} "
          f"耗时={elapsed:.1f}分钟", flush=True)
    print("=" * 60, flush=True)

    report["summary"] = {
        "ok": ok_cnt,
        "failed": fail_cnt,
        "skipped_exist": skip_exist_cnt,
        "elapsed_minutes": round(elapsed, 2),
    }
    _save_report(args.output_report, report)


def _save_report(output_path, report):
    """保存导入报告到 JSON

    入参：output_path 目标路径（None 则不保存）；report 报告字典
    返回：None
    """
    if not output_path:
        return
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"📄 导入报告已保存: {output_path}", flush=True)
    except Exception as e:  # noqa: BLE001  报告写失败不影响主流程
        print(f"⚠️ 报告保存失败: {e}", flush=True)


if __name__ == "__main__":
    main()
