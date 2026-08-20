# -*- coding: utf-8 -*-
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from customer_service.engine import CustomerServiceEngine

eng = CustomerServiceEngine(shop_id="weimusi")
print("=" * 60)
print("  线上问题复现：品质高点的，做橱柜，衣柜，和室外鞋柜")
print("=" * 60)

# 第一轮
tag1, ans1 = eng.reply("多少钱一平？")
print(f"\n[第1轮] tag={tag1}, step={eng.bargain_step}")
print(f"回答: {ans1[:100]}...")

# 第二轮
try:
    tag2, ans2 = eng.reply("品质高点的，做橱柜，衣柜，和室外鞋柜")
    print(f"\n[第2轮] tag={tag2}, step={eng.bargain_step}")
    print(f"selected_material: {eng.selected_material}")
    print(f"回答: {ans2}")
except Exception as e:
    import traceback
    print(f"\n❌ 异常: {e}")
    traceback.print_exc()
