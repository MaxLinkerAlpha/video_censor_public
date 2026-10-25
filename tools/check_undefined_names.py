#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_undefined_tmp.py - 临时脚本: 静态排查模块内"引用了未定义的全局名"

背景:
    v5.4.0 新增的 _flip_source_priority / _resurrect_source_videos 依赖若干
    模块级常量 (BILI_TABLE_ID / YT_HUMAN_REJECTED 等), 但这些名字只在其它函数的
    局部 import 中出现, 导致运行到该分支时抛 NameError 并被 except 吞掉。
    本脚本用标准库 symtable 做作用域分析, 一次性列出所有"被引用但未在模块级
    绑定(赋值/import/def/class)且不是内置"的全局符号。

用法:
    python tools/check_undefined_tmp.py [目标文件...]   # 默认检查 core/workflow_orchestrator.py
"""

import builtins
import symtable
import sys
from pathlib import Path
from typing import List, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_TARGETS = ["core/workflow_orchestrator.py"]


def _module_bound_names(table: symtable.SymbolTable) -> Set[str]:
    """收集模块级已绑定的符号名 (赋值/import/def/class/参数等)"""
    bound: Set[str] = set()
    for sym in table.get_symbols():
        if sym.is_assigned() or sym.is_imported() or sym.is_namespace() or sym.is_parameter():
            bound.add(sym.get_name())
    return bound


def _walk_tables(table: symtable.SymbolTable):
    """递归遍历所有子作用域表"""
    for child in table.get_children():
        yield child
        yield from _walk_tables(child)


def check_file(path: Path) -> List[Tuple[str, str, int]]:
    """返回 [(作用域名, 未定义符号, 该符号首次引用的行号?)] —— symtable 不提供行号, 行号置 0"""
    source = path.read_text(encoding="utf-8")
    top = symtable.symtable(source, str(path), "exec")
    module_bound = _module_bound_names(top)
    builtin_names = set(dir(builtins))

    problems: List[Tuple[str, str, int]] = []
    for table in _walk_tables(top):
        # 类作用域内的名字解析规则不同, 跳过以降低误报
        if table.get_type() == "class":
            continue
        seen: Set[str] = set()
        for sym in table.get_symbols():
            name = sym.get_name()
            if name in module_bound or name in builtin_names or name in seen:
                continue
            # 只关心"引用模块级全局"的符号 (is_global), 且未被本地绑定
            if sym.is_global() and sym.is_referenced():
                seen.add(name)
                problems.append((table.get_name(), name, 0))
    return problems


def main() -> int:
    targets = sys.argv[1:] or DEFAULT_TARGETS
    total = 0
    for rel in targets:
        path = Path(rel)
        if not path.is_absolute():
            path = PROJECT_ROOT / rel
        if not path.exists():
            print(f"[FAIL] 文件不存在: {path}", flush=True)
            continue
        problems = check_file(path)
        print(f"\n[*] {rel}: 疑似未定义全局名 {len(problems)} 处", flush=True)
        for scope, name, _ in problems:
            print(f"    - [{scope}] {name}", flush=True)
        total += len(problems)
    print(f"\n[*] 合计 {total} 处", flush=True)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())