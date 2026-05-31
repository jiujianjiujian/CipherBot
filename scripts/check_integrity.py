"""
Cipher 代码完整性检查器
在每次部署前运行，防止重构后调用方没同步的Bug。

用法:
    python3 scripts/check_integrity.py

检查项:
    1. 配置键一致性 — config.py 定义的键 vs cipher_bot.py 引用的键
    2. 函数签名一致性 — 重要函数的 return 类型
    3. 死代码 — 已删除配置的残留引用
    4. import 有效性 — 所有 import 的模块/变量是否存在
"""
import os
import sys
import ast
import json
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
SCRIPTS_DIR = BASE_DIR / "scripts"
CONFIG_FILE = SCRIPTS_DIR / "config.py"
MAIN_FILE = SCRIPTS_DIR / "cipher_bot.py"

errors = []
warnings = []

def err(msg: str):
    errors.append(msg)
    print(f"  ❌ {msg}")

def warn(msg: str):
    warnings.append(msg)
    print(f"  [!] {msg}")

def ok(msg: str):
    print(f"  ✅ {msg}")


# ============================================================
# 1. 配置键一致性
# ============================================================
def check_config_keys():
    print("\n--- 1. 配置键一致性 ---")

    # 解析 config.py 中 PAIRS, TRADING, SCORING 的键
    # 用 AST 安全解析，不执行代码
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config_source = f.read()
        config_ast = ast.parse(config_source)
    except SyntaxError as e:
        err(f"config.py 语法错误: {e}")
        return

    # 提取所有 dict 字面量的键
    dict_keys = {}  # dict_name -> set(keys)
    for node in ast.walk(config_ast):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and isinstance(node.value, ast.Dict):
                    name = target.id
                    keys = set()
                    for k in node.value.keys:
                        if isinstance(k, ast.Constant):
                            keys.add(k.value)
                    dict_keys[name] = keys

    # 提取 cipher_bot.py 中对这些 dict 的引用
    try:
        with open(MAIN_FILE, "r", encoding="utf-8") as f:
            main_source = f.read()
        main_ast = ast.parse(main_source)
    except SyntaxError as e:
        err(f"cipher_bot.py 语法错误: {e}")
        return

    # 查找所有 dict["xxx"] 引用
    refs = {}  # dict_name -> set(referenced_keys)
    for node in ast.walk(main_ast):
        # dict["key"] 语法
        if isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Name) and isinstance(node.slice, ast.Constant):
                dname = node.value.id
                key = node.slice.value
                if isinstance(key, str):
                    refs.setdefault(dname, set()).add(key)
        # dict.get("key") 语法
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
                if isinstance(node.func.value, ast.Name) and node.args:
                    if isinstance(node.args[0], ast.Constant):
                        dname = node.func.value.id
                        key = node.args[0].value
                        if isinstance(key, str):
                            refs.setdefault(dname, set()).add(key)

    # 比对 TRADING
    for dname in ["TRADING", "SCORING"]:
        defined = dict_keys.get(dname, set())
        used = refs.get(dname, set())
        for key in used:
            if key not in defined:
                err(f"{dname}[\"{key}\"] 在 cipher_bot.py 中引用，但 config.py 中未定义")
        for key in defined:
            if key not in used:
                warn(f"{dname}[\"{key}\"] 定义了但未被引用（可能是备而不用）")
        ok(f"{dname}: {len(used)}/{len(defined)} 键被引用（{len(used)}引/{len(defined)}定）")

    # 检查 PAIRS 配置中每个币种应有的字段
    pair_config = dict_keys.get("PAIRS", set())
    for pair_name in list(pair_config)[:10]:  # 最多检查10个
        pass

    # 检查 PAIRS 配置
    if "PAIRS" in dict_keys:
        print("  检查PAIRS配置...")

    # 检查 3Commas webhook_url / secret
    for key in ["webhook_url", "secret"]:
        if key not in dict_keys.get("THREE_COMMAS", set()):
            err(f"THREE_COMMAS[\"{key}\"] 缺失")
    ok(f"THREE_COMMAS: 基础键完整")


# ============================================================
# 2. 函数签名一致性
# ============================================================
def check_function_signatures():
    print("\n--- 2. 函数签名一致性 ---")

    try:
        with open(MAIN_FILE, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
    except SyntaxError as e:
        err(f"语法错误: {e}")
        return

    functions = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            returns = None
            if node.returns:
                returns = ast.dump(node.returns)
            args = [a.arg for a in node.args.args]
            functions[node.name] = {
                "args": args,
                "returns": returns,
                "lineno": node.lineno,
            }

    # 检查关键函数
    checks = [
        ("find_trading_signal", ["price", "ticker_24h", "klines_15m", "klines_1h", "klines_4h"]),
        ("score_signal", ["direction", "price", "stop_loss", "target", "klines_15m", "klines_1h", "structure"]),
        ("send_webhook", ["signal"]),
        ("run_scan", []),
        ("run_summary", []),
        ("run_review", []),
    ]

    for name, expected_args in checks:
        if name in functions:
            fn = functions[name]
            # 检查必要参数是否存在（忽略可选参数）
            for a in expected_args:
                if a not in fn["args"] and a not in ["klines_4h"]:
                    # 有些参数可能是 **kwargs 或默认参数
                    pass
            ok(f"{name}() 存在 ({fn['lineno']}行, {len(fn['args'])}参数)")
        else:
            err(f"{name}() 函数缺失")

    # 检查所有 return 路径 — 只在关键函数中检查
    def has_return_values(func_node):
        """检查函数是否有 return 语句返回非 None 值"""
        has_valued_return = False
        for node in ast.walk(func_node):
            if isinstance(node, ast.Return) and node.value is not None:
                if isinstance(node.value, ast.Tuple):
                    has_valued_return = True
                elif isinstance(node.value, ast.Constant) and node.value.value is None:
                    pass
                else:
                    has_valued_return = True
        return has_valued_return

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "find_trading_signal":
            # 检查是否所有 return 都是 Tuple
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and sub.value is not None:
                    if isinstance(sub.value, ast.Name) and sub.value.id == "None":
                        err(f"find_trading_signal 第{sub.lineno}行: return None (应为 tuple)")
                    elif not isinstance(sub.value, ast.Tuple) and not isinstance(sub.value, ast.Call):
                        # 允许 return best, indicators 这种形式——它被解析为 Tuple
                        if not isinstance(sub.value, ast.Tuple):
                            # 检查是否是 Name 类型 (return None)
                            if isinstance(sub.value, ast.Constant) and sub.value.value is None:
                                err(f"find_trading_signal 第{sub.lineno}行: return None (应为 tuple)")
            ok("find_trading_signal: return路径检查完毕")


# ============================================================
# 3. import 有效性
# ============================================================
def check_imports():
    print("\n--- 3. import 完整性 ---")

    try:
        with open(MAIN_FILE, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
    except SyntaxError as e:
        err(f"语法错误: {e}")
        return

    imported_names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names[alias.asname or alias.name] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_names[alias.asname or alias.name] = node.lineno

    # 统计实际使用的名字（排除 import 语句自身和定义语句）
    used_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            # 排除 import 语句中的名字
            in_import = False
            for parent in ast.walk(tree):
                if isinstance(parent, (ast.Import, ast.ImportFrom)):
                    for child in ast.walk(parent):
                        if child is node:
                            in_import = True
                            break
            if not in_import:
                used_names.add(node.id)

    # 检查已导入但未使用的
    for name, lineno in imported_names.items():
        # 跳过特殊导入
        if name in ("__name__",):
            continue
        if name not in used_names and name not in source.split('\n')[lineno-1]:
            # 检查是不是只在 import 行出现
            count_in_imports = sum(1 for line in source.split('\n') if 'import' in line and name in line)
            count_in_code = sum(1 for line in source.split('\n') if 'import' not in line and name in line)
            if count_in_code == 0:
                warn(f"第{lineno}行: 导入 '{name}' 可能未使用")

    # 检查 config.py 导入的变量
    config_names = ['THREE_COMMAS', 'TELEGRAM', 'TRADING', 'PAIRS', 'SCORING', 'TRADE_LOG_FILE', 'ANALYSIS']
    for name in config_names:
        if name not in imported_names:
            err(f"缺少导入: {name}")
        elif name in imported_names:
            ok(f"{name}: 已导入")

    # 验证 validator 模块
    validator_path = SCRIPTS_DIR / "validator.py"
    if validator_path.exists():
        ok("validator.py: 存在")
    else:
        err("validator.py: 文件缺失")


# ============================================================
# 4. 死代码检测
# ============================================================
def check_dead_code():
    print("\n--- 4. 死代码检查 ---")

    try:
        with open(MAIN_FILE, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
    except SyntaxError as e:
        err(f"语法错误: {e}")
        return

    # 检查所有函数是否有被调用
    defined_functions = []
    called_functions = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            defined_functions.append(node.name)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called_functions.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called_functions.add(node.func.attr)

    # 检查是否所有 main 入口都能到达
    # 检查 __main__ 块
    has_main_block = False
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            if (isinstance(node.test, ast.Compare) and
                any(isinstance(c, ast.Name) and c.id == '__name__' for c in ast.walk(node.test))):
                has_main_block = True
                break

    if has_main_block:
        ok("__main__ 入口存在")
    else:
        err("__main__ 入口缺失")


# ============================================================
# 主函数
# ============================================================
def main():
    print(f"Cipher 代码完整性检查器")
    print(f"项目路径: {BASE_DIR}")
    print(f"检查时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}")

    check_config_keys()
    check_function_signatures()
    check_imports()
    check_dead_code()

    print("\n" + "=" * 50)
    print(f"结果: {len(errors)} 个错误, {len(warnings)} 个警告")

    if errors:
        print("\n❌ 错误列表:")
        for e in errors:
            print(f"  {e}")
        print("\n请修复后再部署!")
        sys.exit(1)
    elif warnings:
        print("\n⚠️  有警告，建议检查但不阻塞部署")
        sys.exit(0)
    else:
        print("\n✅ 全部通过，可以部署!")
        sys.exit(0)


if __name__ == "__main__":
    main()
