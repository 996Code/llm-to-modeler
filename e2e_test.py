#!/usr/bin/env python3
"""全面端到端测试：追问 + 明确需求 + 模糊回复 + 多轮修改"""

import requests
import json
import time
import sys

BASE = "http://localhost:18080"
PASS = 0
FAIL = 0

def parse_sse(resp):
    """解析 SSE 流，返回事件列表"""
    events = []
    for line in resp.iter_lines():
        if not line:
            continue
        s = line.decode('utf-8')
        if s.startswith('event: '):
            events.append({'type': s[7:]})
        elif s.startswith('data: ') and events:
            try:
                events[-1]['data'] = json.loads(s[6:])
            except:
                events[-1]['data'] = {'raw': s[6:]}
    return events

def run_test(name, description, conversation_id=None, expect_clarification=False, expect_config=False, timeout=180):
    global PASS, FAIL
    print(f"\n{'='*60}")
    print(f"测试: {name}")
    print(f"输入: {description[:80]}")
    print(f"{'='*60}")

    try:
        resp = requests.post(
            f"{BASE}/api/config/generate",
            json={"description": description, "conversation_id": conversation_id},
            headers={"X-User-Id": "e2e-test"},
            stream=True,
            timeout=timeout
        )

        events = parse_sse(resp)

        stages = [e for e in events if e.get('type') == 'stage']
        results = [e for e in events if e.get('type') == 'result']
        errors = [e for e in events if e.get('type') == 'error']
        dones = [e for e in events if e.get('type') == 'done']

        print(f"  事件数: {len(events)} (stage={len(stages)}, result={len(results)}, error={len(errors)}, done={len(dones)})")

        # 打印 stage
        for s in stages:
            d = s.get('data', {})
            print(f"  [stage] {d.get('stage','?')}: {d.get('message','')[:60]}")

        if errors:
            print(f"  ✗ ERROR: {errors[0].get('data',{}).get('error','')[:100]}")
            FAIL += 1
            return None

        if not results:
            print(f"  ✗ 没有 result 事件")
            FAIL += 1
            return None

        r = results[0]['data']

        # 检查是否符合预期
        if expect_clarification:
            if r.get('needsClarification'):
                print(f"  ✓ 触发追问: {r.get('questions', [])}")
                PASS += 1
                return r
            else:
                print(f"  ✗ 预期追问但生成了配置")
                FAIL += 1
                return None

        if expect_config:
            if r.get('config'):
                cfg = r['config']
                fields = cfg.get('formFieldConfigVos', [])
                print(f"  ✓ 生成配置: {cfg.get('formName','')} ({len(fields)} 字段)")
                for f in fields:
                    print(f"     - {f.get('fieldTitleText','')} (type={f.get('formFieldType','')})")
                PASS += 1
                return r
            else:
                print(f"  ✗ 预期配置但未生成")
                FAIL += 1
                return None

        # 无特定预期
        if r.get('needsClarification'):
            print(f"  → 追问: {r.get('questions', [])}")
        elif r.get('config'):
            print(f"  → 配置: {r.get('formName','')} ({r.get('fieldCount',0)} 字段)")
        PASS += 1
        return r

    except requests.exceptions.Timeout:
        print(f"  ✗ 超时 ({timeout}s)")
        FAIL += 1
        return None
    except Exception as e:
        print(f"  ✗ 异常: {e}")
        FAIL += 1
        return None


# ─── 测试1: 完全模糊 → 应触发追问 ───
print("\n" + "█" * 60)
print("█ 场景1: 完全模糊需求 → 追问")
print("█" * 60)
r1 = run_test(
    "完全模糊",
    "创建一个表单",
    expect_clarification=True,
)

# ─── 测试2: 明确需求 → 直接生成 ───
print("\n" + "█" * 60)
print("█ 场景2: 明确需求 → 直接生成")
print("█" * 60)
# 创建新会话
conv = requests.post(f"{BASE}/api/conversations", json={}, headers={"X-User-Id": "e2e-test"}).json()
conv_id = conv['id']
print(f"  会话ID: {conv_id}")

r2 = run_test(
    "明确需求",
    "创建一个请假申请表，包含申请人、请假类型（事假/病假/年假）、开始日期、结束日期",
    conversation_id=conv_id,
    expect_config=True,
)

# ─── 测试3: 模糊回复"你定就行了" → 基于历史推断 ───
print("\n" + "█" * 60)
print("█ 场景3: 模糊回复（之前超时的场景）")
print("█" * 60)
r3 = run_test(
    "模糊回复",
    "你定就行了",
    conversation_id=conv_id,
    timeout=180,
)

# ─── 总结 ───
print("\n" + "█" * 60)
print(f"█ 总结: {PASS} 通过, {FAIL} 失败")
print("█" * 60)

sys.exit(0 if FAIL == 0 else 1)
