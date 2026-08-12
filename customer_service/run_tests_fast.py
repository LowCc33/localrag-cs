# -*- coding: utf-8 -*-
"""
客服系统快速测试脚本
用法：
  python3 customer_service/run_tests_fast.py          # 纯关键词模式（mock LLM，测兜底）
  python3 customer_service/run_tests_fast.py --llm    # LLM主分类模式（真实调用LLM）
"""
import sys
import os
from unittest.mock import MagicMock

# 确保项目根目录在path里
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# 判断模式
llm_mode = "--llm" in sys.argv

if not llm_mode:
    # ========== 纯关键词模式：mock掉Redis和LLM，强制走关键词兜底 ==========
    from customer_service.engine import CustomerServiceEngine

    def _mock_get_redis(self):
        return None
    CustomerServiceEngine._get_redis = _mock_get_redis

    import customer_service.engine as engine_module
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.json.return_value = {}
    engine_module.requests.post = MagicMock(return_value=mock_resp)
    mode_name = "纯关键词模式"
else:
    # ========== LLM模式：真实调用LLM，走新架构主流程 ==========
    from customer_service.engine import CustomerServiceEngine
    mode_name = "LLM主分类模式"


# 62条测试用例：(输入, 预期分类包含的字符串, 说明)
TEST_CASES = [
    # ===== 议价类（10条）=====
    ("能不能便宜点", "bargain", "议价-能便宜点"),
    ("做一个衣柜多少钱", "bargain", "议价-小单问价"),
    ("全屋定制多少钱", "bargain", "议价-大单问价"),
    ("能打折吗", "bargain", "议价-能打折吗"),
    ("最低价多少", "bargain", "议价-最低价"),
    ("太贵了", "bargain", "议价-太贵了"),
    ("有没有优惠", "bargain", "议价-有没有优惠"),
    ("颗粒板什么价", "bargain", "议价-选材料问价"),
    ("10平方多少钱", "bargain", "议价-面积问价"),
    ("再便宜点", "bargain", "议价-再便宜点"),

    # ===== 材料环保类（10条）=====
    ("环保等级是多少", "material_detail", "环保等级"),
    ("E0和ENF有什么区别", "material_compare", "E0 vs ENF"),
    ("有检测报告吗", "material_detail", "检测报告"),
    ("装完多久能住", "measurement", "入住时间"),
    ("孕妇能住吗", "material_detail", "孕妇小孩"),
    ("封边用的什么胶", "process_question", "封边胶"),
    ("板材是真的吗", "material_detail", "板材真假"),
    ("环保吗", "material_detail", "环保吗-短句"),
    ("用的什么板材", "material_detail", "什么板材"),
    ("板材是什么牌子的", "material_detail", "板材牌子"),

    # ===== 计价方式类（8条）=====
    ("按投影还是展开算", "pricing_method", "投影vs展开"),
    ("投影面积包含什么", "pricing_method", "投影包含什么"),
    ("抽屉多少钱", "bargain", "抽屉多少钱"),
    ("有没有隐形消费", "pricing_method", "隐形消费"),
    ("设计费多少钱", "bargain", "免费服务"),
    ("怎么算价格", "pricing_method", "计价方式"),
    ("抽屉要加钱吗", "pricing_method", "抽屉加钱"),
    ("哪些要加钱", "pricing_method", "额外收费"),

    # ===== 店铺信息/品牌信任（6条）=====
    ("你们公司在哪", "shop_info", "公司地址"),
    ("工厂在哪", "shop_info", "工厂地址"),
    ("你们是本地的吗", "shop_info", "本地店"),
    ("没听过你们牌子", "never_heard", "牌子没听过"),
    ("跟索菲亚比怎么样", "compare_big_brand", "比大牌"),
    ("你们是小作坊吧", "small_workshop", "小作坊"),

    # ===== 五金配件（3条）=====
    ("五金用的什么铰链", "hardware_detail", "五金铰链"),
    ("五金是什么牌子", "hardware_detail", "五金品牌"),
    ("标配五金有哪些", "hardware_detail", "标配五金"),

    # ===== 售后质保类（7条）=====
    ("有售后吗", "after_sales", "有售后吗"),
    ("坏了怎么办", "after_sales", "坏了怎么办"),
    ("质保几年", "after_sales", "质保几年"),
    ("五金坏了", "after_sales", "五金坏了"),
    ("板子开胶了", "after_sales", "板子开胶"),
    ("售后找谁", "after_sales", "售后找谁"),
    ("终身维护什么意思", "after_sales", "终身维护"),

    # ===== 工期安装类（6条）=====
    ("工期多久", "measurement", "总工期"),
    ("能加急吗", "measurement", "加急"),
    ("延期了怎么办", "after_sales", "延期赔偿"),
    ("安装是你们自己的人吗", "process_question", "安装团队"),
    ("安装弄脏了怎么办", "after_sales", "安装损坏赔偿"),
    ("垃圾清理谁负责", "after_sales", "垃圾清理"),

    # ===== 工艺/特殊需求（5条）=====
    ("可以做圆弧吗", "process_question", "圆弧工艺"),
    ("能做榻榻米吗", "product_type", "榻榻米"),
    ("可以做玻璃门吗", "process_question", "玻璃门"),
    ("异形能做吗", "process_question", "异形"),
    ("开放式厨房可以做吗", "material_recommend_kitchen", "开放式厨房"),

    # ===== 闲聊/打招呼（7条）=====
    ("你好", "greeting", "打招呼-你好"),
    ("在吗", "greeting", "打招呼-在吗"),
    ("有人吗", "greeting", "打招呼-有人吗"),
    ("谢谢", "thanks", "致谢"),
    ("感谢", "thanks", "致谢-感谢"),
    ("再见", "bye", "告别"),
    ("拜拜", "bye", "告别-拜拜"),
]


def run_tests():
    eng = CustomerServiceEngine()
    passed = 0
    failed = 0
    failures = []

    for i, (text, expected, desc) in enumerate(TEST_CASES, 1):
        eng.clear()
        tag, answer = eng.reply(text)
        ok = expected in tag
        if ok:
            passed += 1
        else:
            failed += 1
            failures.append((i, text, expected, tag, desc))

    total = passed + failed
    rate = passed / total * 100

    print("=" * 70)
    print(f"  全屋定制AI客服 快速测试（{mode_name}）")
    print("=" * 70)
    print(f"  总用例: {total}  |  通过: {passed}  |  失败: {failed}")
    print(f"  通过率: {rate:.1f}%")
    print("=" * 70)

    if failures:
        print(f"\n❌ 失败用例 ({len(failures)}条):")
        for idx, text, expected, actual, desc in failures:
            print(f"  {idx}. [{desc}] {text}")
            print(f"     预期: {expected}")
            print(f"     实际: {actual}")

    print()
    return rate >= 85 if not llm_mode else rate >= 95


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
