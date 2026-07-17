#!/usr/bin/env python3
"""测试追问机制的端到端流程"""

import requests
import json
import time

BASE_URL = "http://localhost:18080"

def test_clarification_flow():
    """测试模糊需求触发追问"""
    print("=" * 60)
    print("测试 1: 模糊需求触发追问")
    print("=" * 60)
    
    # 发送模糊需求
    response = requests.post(
        f"{BASE_URL}/api/config/generate",
        json={"description": "创建一个表单"},
        headers={"Content-Type": "application/json"},
        stream=True
    )
    
    print(f"\n用户输入: '创建一个表单'")
    print("\nSSE 事件流:")
    
    for line in response.iter_lines():
        if line:
            line_str = line.decode('utf-8')
            if line_str.startswith('data: '):
                event_data = json.loads(line_str[6:])
                event_type = event_data.get('type')
                
                if event_type == 'stage':
                    stage = event_data.get('stage')
                    message = event_data.get('message')
                    print(f"  [stage] {stage}: {message}")
                    
                elif event_type == 'result':
                    result = event_data.get('data')
                    needs_clarification = result.get('needsClarification')
                    
                    if needs_clarification:
                        print(f"\n  ✓ 触发追问机制!")
                        print(f"  问题列表:")
                        for i, q in enumerate(result.get('questions', []), 1):
                            print(f"    {i}. {q}")
                    else:
                        print(f"\n  ✗ 未触发追问，直接生成了配置")
                        print(f"  配置摘要: {result.get('summary')}")
                        
                elif event_type == 'done':
                    print(f"\n  [done] 流程结束")
                    break
    
    print("\n" + "=" * 60)
    print("测试 2: 明确需求直接生成")
    print("=" * 60)
    
    # 发送明确需求
    response = requests.post(
        f"{BASE_URL}/api/config/generate",
        json={"description": "创建一个请假申请表，包含申请人、请假类型、开始日期、结束日期"},
        headers={"Content-Type": "application/json"},
        stream=True
    )
    
    print(f"\n用户输入: '创建一个请假申请表，包含申请人、请假类型、开始日期、结束日期'")
    print("\nSSE 事件流:")
    
    for line in response.iter_lines():
        if line:
            line_str = line.decode('utf-8')
            if line_str.startswith('data: '):
                event_data = json.loads(line_str[6:])
                event_type = event_data.get('type')
                
                if event_type == 'stage':
                    stage = event_data.get('stage')
                    message = event_data.get('message')
                    print(f"  [stage] {stage}: {message}")
                    
                elif event_type == 'result':
                    result = event_data.get('data')
                    needs_clarification = result.get('needsClarification')
                    
                    if needs_clarification:
                        print(f"\n  ✗ 意外触发追问")
                        for i, q in enumerate(result.get('questions', []), 1):
                            print(f"    {i}. {q}")
                    else:
                        print(f"\n  ✓ 直接生成配置")
                        print(f"  配置摘要: {result.get('summary')}")
                        print(f"  字段数量: {result.get('fieldCount')}")
                        
                elif event_type == 'done':
                    print(f"\n  [done] 流程结束")
                    break

if __name__ == "__main__":
    test_clarification_flow()
